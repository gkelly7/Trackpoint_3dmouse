#!/usr/bin/env python3
"""
Inventor 3D Mouse Camera Server
================================
Receives TrackPoint deltas from the AHK script via UDP and applies
cursor-independent orbit / pan to Autodesk Inventor 2024's active camera.

Dependencies:  pip install pywin32 numpy

Tune ORBIT_SENSITIVITY and PAN_SENSITIVITY to taste.
Log file is written next to this script: inventor_camera.log
"""

import logging
import math
import os
import socket
import struct
import sys
import time

# Log file defined before basicConfig so the FileHandler can use it
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventor_camera.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

try:
    import pythoncom
    import win32com.client
except ImportError:
    log.error("pywin32 not installed.  Run:  pip install pywin32")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    log.error("numpy not installed.  Run:  pip install numpy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UDP_HOST = "127.0.0.1"
UDP_PORT = 9999

# Radians added to azimuth/elevation per TrackPoint delta unit.
# Increase for faster orbit.  Start here and tune with the log (azim/elev lines).
ORBIT_SENSITIVITY = 0.04

# Fraction of eye-target distance added per TrackPoint delta unit for pan.
PAN_SENSITIVITY = 0.008

# After this many seconds of inactivity the server re-reads the camera from
# Inventor so that scroll/ViewCube changes are picked up on the next orbit.
RESYNC_IDLE = 0.6

RECONNECT_INTERVAL = 2.0   # seconds between Inventor reconnect attempts

# Inventor document type constants
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291

# World-up axis for your Inventor templates.
# Default Inventor part/assembly templates are Y-up.
# Change to [0, 0, 1] if your models use Z-up.
WORLD_UP = np.array([0.0, 1.0, 0.0])
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistent camera state (avoids re-reading mid-animation camera position)
# ---------------------------------------------------------------------------
_cs = {           # camera state dict
    "valid":   False,
    "target":  None,   # np.array  -- pan target point
    "r":       None,   # float     -- eye-to-target distance
    "azim":    0.0,    # float rad -- horizontal orbit angle
    "elev":    0.0,    # float rad -- vertical orbit angle
    "last_t":  0.0,    # monotonic time of last write
}


def _world_axes():
    """Return (up, east, north) unit vectors for the current WORLD_UP."""
    up  = WORLD_UP / np.linalg.norm(WORLD_UP)
    ref = np.array([1., 0., 0.]) if abs(np.dot(up, [1., 0., 0.])) < 0.9 \
          else np.array([0., 0., 1.])
    east  = np.cross(up, ref);  east  /= np.linalg.norm(east)
    north = np.cross(east, up)
    return up, east, north


def _read_cam(cam):
    """Decompose Inventor Camera into our (target, r, azim, elev) state."""
    eye    = np.array([cam.Eye.X,    cam.Eye.Y,    cam.Eye.Z   ], dtype=float)
    target = np.array([cam.Target.X, cam.Target.Y, cam.Target.Z], dtype=float)
    offset = eye - target
    r      = np.linalg.norm(offset)
    if r < 1e-9:
        return False

    up, east, north = _world_axes()
    elev = math.asin(np.clip(np.dot(offset / r, up), -1.0, 1.0))
    proj     = offset - up * np.dot(offset, up)
    proj_len = np.linalg.norm(proj)
    azim     = math.atan2(np.dot(proj / proj_len, east),
                          np.dot(proj / proj_len, north)) if proj_len > 1e-9 else 0.0

    _cs.update(target=target.copy(), r=r, azim=azim, elev=elev,
               valid=True, last_t=time.monotonic())
    log.debug("resync  azim=%.1fdeg  elev=%.1fdeg  r=%.2f",
              math.degrees(azim), math.degrees(elev), r)
    return True


def _eye_from_state():
    """Reconstruct eye position from current _cs azim/elev/r/target."""
    up, east, north = _world_axes()
    cos_e = math.cos(_cs["elev"])
    offset = _cs["r"] * (
        up    * math.sin(_cs["elev"]) +
        north * cos_e * math.cos(_cs["azim"]) +
        east  * cos_e * math.sin(_cs["azim"])
    )
    return _cs["target"] + offset, offset


def _up_from_eye(offset):
    """Camera up vector: project WORLD_UP onto plane perpendicular to view."""
    up   = WORLD_UP / np.linalg.norm(WORLD_UP)
    nfwd = offset / np.linalg.norm(offset)
    new_up = up - nfwd * np.dot(nfwd, up)
    n = np.linalg.norm(new_up)
    return new_up / n if n > 1e-9 else up


# ---------------------------------------------------------------------------
# Inventor helpers
# ---------------------------------------------------------------------------
def get_inventor():
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception:
        return None


def _active_view(app):
    """Return (cam, tg) for active Part/Assembly, or None."""
    try:
        doc = app.ActiveDocument
        if doc is None:
            return None
        if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject):
            return None
        cam = doc.Views.Item(1).Camera
        return cam, app.TransientGeometry
    except Exception as exc:
        log.debug("_active_view: %s", exc)
        return None


def _apply_cam(cam) -> None:
    """Apply camera without Inventor's blocking view-transition animation.

    Inventor's cam.Apply() triggers a smooth fly-to animation that blocks
    the calling thread for ~1 s, causing stale UDP packets to pile up.
    ApplyWithoutTransition() is instant and non-blocking.
    Falls back to Apply() for older Inventor builds that lack the method.
    """
    try:
        cam.ApplyWithoutTransition()
    except Exception:
        cam.Apply()


# ---------------------------------------------------------------------------
# Orbit / Pan
# ---------------------------------------------------------------------------
def do_orbit(app, dx: int, dy: int) -> None:
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    now = time.monotonic()

    # Re-sync from Inventor when idle long enough (picks up scroll/ViewCube)
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    # Update our own state -- no re-reading from Inventor
    # Negate dx so pushing right orbits to show the model's right side
    _cs["azim"] -= dx * ORBIT_SENSITIVITY
    _cs["elev"]  = float(np.clip(
        _cs["elev"] + dy * ORBIT_SENSITIVITY,
        -math.pi / 2 + 0.02,
         math.pi / 2 - 0.02,
    ))
    _cs["last_t"] = now

    new_eye, offset = _eye_from_state()
    new_up = _up_from_eye(offset)

    cam.UpVector = tg.CreateUnitVector(
        float(new_up[0]), float(new_up[1]), float(new_up[2]))
    cam.Eye = tg.CreatePoint(
        float(new_eye[0]), float(new_eye[1]), float(new_eye[2]))
    _apply_cam(cam)

    log.debug("orbit dx=%3d dy=%3d  azim=%6.1fdeg  elev=%5.1fdeg",
              dx, dy, math.degrees(_cs["azim"]), math.degrees(_cs["elev"]))


def do_pan(app, dx: int, dy: int) -> None:
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    now = time.monotonic()
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    # Build camera-right and camera-up from current eye direction
    eye    = np.array([cam.Eye.X,    cam.Eye.Y,    cam.Eye.Z   ], dtype=float)
    target = _cs["target"]
    cam_up = np.array([cam.UpVector.X, cam.UpVector.Y, cam.UpVector.Z], dtype=float)

    fwd   = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(fwd, cam_up)
    r_len = np.linalg.norm(right)
    if r_len < 1e-9:
        return
    right /= r_len

    scale  = _cs["r"] * PAN_SENSITIVITY
    delta  = -right * (dx * scale) + cam_up * (-dy * scale)

    new_eye    = eye + delta
    new_target = target + delta

    _cs["target"] = new_target
    _cs["last_t"] = now

    cam.Eye    = tg.CreatePoint(float(new_eye[0]),    float(new_eye[1]),    float(new_eye[2]))
    cam.Target = tg.CreatePoint(float(new_target[0]), float(new_target[1]), float(new_target[2]))
    _apply_cam(cam)

    log.debug("pan dx=%3d dy=%3d", dx, dy)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    pythoncom.CoInitialize()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_HOST, UDP_PORT))

    log.info("Inventor camera server  UDP %s:%d", UDP_HOST, UDP_PORT)
    log.info("Orbit sensitivity: %.4f  Pan sensitivity: %.4f",
             ORBIT_SENSITIVITY, PAN_SENSITIVITY)

    app            = None
    last_reconnect = 0.0
    FRAME_S        = 0.033   # 30 Hz max -- prevents flooding Inventors COM queue

    try:
        while True:
            now = time.monotonic()

            # Reconnect to Inventor if not connected
            if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                last_reconnect = now
                _cs["valid"] = False
                app = get_inventor()
                if app:
                    log.info("Connected to Inventor.")
                else:
                    log.info("Waiting for Inventor to start...")

            # Drain ALL pending UDP packets within one frame window,
            # accumulating into a single orbit+pan delta.
            # This caps cam.Apply() calls at 30/s regardless of TrackPoint rate.
            orbit_dx = orbit_dy = 0
            pan_dx   = pan_dy   = 0
            deadline = time.monotonic() + FRAME_S

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(max(remaining, 0.001))
                try:
                    data, _ = sock.recvfrom(64)
                except socket.timeout:
                    break
                if len(data) < 9:
                    continue
                x, y = struct.unpack_from("<ii", data, 0)
                mode = struct.unpack_from("<B",  data, 8)[0]
                if mode == 0:
                    orbit_dx += x;  orbit_dy += y
                else:
                    pan_dx   += x;  pan_dy   += y

            # Ping Inventor if idle (detect crashes)
            if app is not None and orbit_dx == 0 and orbit_dy == 0                     and pan_dx == 0 and pan_dy == 0:
                if (time.monotonic() - last_reconnect) > RECONNECT_INTERVAL * 5:
                    last_reconnect = time.monotonic()
                    try:
                        _ = app.Caption
                    except Exception:
                        log.info("Lost connection to Inventor.")
                        app = None
                        _cs["valid"] = False
                continue

            if app is None:
                continue

            # Safety cap: prevent runaway delta from stale-packet accumulation.
            # At 125 Hz, one normal fast swipe is ~5–10 units; cap at 30.
            MAX_DELTA = 30
            orbit_dx = max(-MAX_DELTA, min(MAX_DELTA, orbit_dx))
            orbit_dy = max(-MAX_DELTA, min(MAX_DELTA, orbit_dy))
            pan_dx   = max(-MAX_DELTA, min(MAX_DELTA, pan_dx))
            pan_dy   = max(-MAX_DELTA, min(MAX_DELTA, pan_dy))

            # Single camera update per frame
            try:
                if orbit_dx or orbit_dy:
                    do_orbit(app, orbit_dx, orbit_dy)
                if pan_dx or pan_dy:
                    do_pan(app, pan_dx, pan_dy)
            except Exception as exc:
                log.warning("Camera command failed: %s", exc)
                app = None
                _cs["valid"] = False

            # Drain any packets that piled up while the COM call executed.
            # Without this, the next frame inherits a stale backlog and produces
            # a massive single-frame delta (the original "spin-out" bug).
            sock.settimeout(0)
            _drained = 0
            try:
                while True:
                    sock.recvfrom(64)
                    _drained += 1
            except (BlockingIOError, socket.timeout):
                pass
            if _drained:
                log.debug("drained %d stale UDP packets after COM call", _drained)

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        sock.close()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
