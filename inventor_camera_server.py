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

# Inertia / coast factor applied when the TrackPoint stops moving.
# 0.0 = stops instantly, 0.90 = long coast.  Applies to both orbit and pan.
ORBIT_INERTIA = 0.80

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
    "valid":  False,
    "eye":    None,   # np.array(3) -- camera eye position
    "target": None,   # np.array(3) -- orbit/pan target point
    "up":     None,   # np.array(3) -- camera up unit vector
    "r":      None,   # float       -- |eye - target| (used to scale pan)
    "last_t": 0.0,    # monotonic time of last write
}


def _rodrigues(v: np.ndarray, k: np.ndarray, theta: float) -> np.ndarray:
    """Rotate vector v by angle theta (radians) around unit vector k."""
    c = math.cos(theta)
    s = math.sin(theta)
    return v * c + np.cross(k, v) * s + k * (np.dot(k, v) * (1.0 - c))


def _read_cam(cam):
    """Read Inventor Camera into our state dict (eye, target, up, r)."""
    eye    = np.array([cam.Eye.X,      cam.Eye.Y,      cam.Eye.Z     ], dtype=float)
    target = np.array([cam.Target.X,   cam.Target.Y,   cam.Target.Z  ], dtype=float)
    up_raw = np.array([cam.UpVector.X, cam.UpVector.Y, cam.UpVector.Z], dtype=float)
    r      = np.linalg.norm(eye - target)
    up_len = np.linalg.norm(up_raw)
    if r < 1e-9 or up_len < 1e-9:
        return False
    _cs.update(eye=eye.copy(), target=target.copy(), up=up_raw / up_len,
               r=r, valid=True, last_t=time.monotonic())
    log.debug("resync  r=%.2f", r)
    return True


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


_apply_cam_logged = False  # log the chosen method only once


def _apply_cam(cam) -> None:
    """Apply camera without Inventor's blocking view-transition animation.

    Tries ApplyWithoutTransition() (instant) first; falls back to Apply()
    (runs a smooth ~1 s animation) for older Inventor builds.
    """
    global _apply_cam_logged
    try:
        cam.ApplyWithoutTransition()
        if not _apply_cam_logged:
            log.info("Camera method: ApplyWithoutTransition() -- no animation lag")
            _apply_cam_logged = True
    except Exception:
        if not _apply_cam_logged:
            log.info("Camera method: Apply() fallback -- ApplyWithoutTransition() unavailable")
            _apply_cam_logged = True
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

    offset   = _cs["eye"] - _cs["target"]
    up       = _cs["up"].copy()

    # Horizontal orbit: rotate around the camera's current up vector.
    # This keeps left/right relative to what you currently see, even after
    # vertical tilting (both axes are now fully camera-relative).
    if dx:
        h_angle = -dx * ORBIT_SENSITIVITY
        offset  = _rodrigues(offset, up, h_angle)
        # Rotating around the up vector leaves the up vector unchanged

    # Vertical orbit: rotate around the camera's current right vector.
    # This always tilts up/down relative to whatever you're looking at,
    # regardless of how much you've already orbited horizontally.
    # It also has no elevation clamp -- you can orbit freely over the top.
    if dy:
        fwd   = -offset / np.linalg.norm(offset)   # unit vector pointing toward target
        right = np.cross(fwd, up)
        rlen  = np.linalg.norm(right)
        if rlen < 1e-9:                             # degenerate: fwd ≈ world up
            right = np.cross(fwd, np.array([1., 0., 0.]))
            rlen  = np.linalg.norm(right)
        if rlen > 1e-9:
            right   /= rlen
            v_angle  = -dy * ORBIT_SENSITIVITY
            offset   = _rodrigues(offset, right, v_angle)
            up       = _rodrigues(up,     right, v_angle)
            up      /= np.linalg.norm(up)

    new_eye       = _cs["target"] + offset
    _cs["eye"]    = new_eye
    _cs["up"]     = up
    _cs["last_t"] = now

    cam.UpVector = tg.CreateUnitVector(float(up[0]),      float(up[1]),      float(up[2]))
    cam.Eye      = tg.CreatePoint(     float(new_eye[0]), float(new_eye[1]), float(new_eye[2]))
    _apply_cam(cam)

    log.debug("orbit dx=%3d dy=%3d", dx, dy)


def do_pan(app, dx: float, dy: float) -> None:
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    now = time.monotonic()
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    eye    = _cs["eye"]
    target = _cs["target"]
    up     = _cs["up"]

    fwd   = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(fwd, up)
    r_len = np.linalg.norm(right)
    if r_len < 1e-9:
        return
    right /= r_len

    scale = _cs["r"] * PAN_SENSITIVITY
    # dx > 0 = trackpoint right → negate so camera pans right (model moves left)
    # dy > 0 = trackpoint down  → positive up so camera pans down (model moves up)
    #          (sign was previously negated, which reversed the direction)
    delta = right * (-dx * scale) + up * (dy * scale)

    new_eye    = eye + delta
    new_target = target + delta

    _cs["eye"]    = new_eye
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
    FRAME_S        = 0.033   # 30 Hz -- rate-limits COM calls

    # Running velocity (floats) that carry inertia between frames.
    # Set directly from input when packets arrive; decayed by ORBIT_INERTIA otherwise.
    vel_ox = vel_oy = 0.0   # orbit
    vel_px = vel_py = 0.0   # pan
    STOP   = 0.25           # velocity below this is snapped to zero

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

            # Accumulate raw input for one frame window.
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

            # Safety cap: prevent runaway from any stale-packet accumulation.
            MAX_DELTA = 30
            orbit_dx = max(-MAX_DELTA, min(MAX_DELTA, orbit_dx))
            orbit_dy = max(-MAX_DELTA, min(MAX_DELTA, orbit_dy))
            pan_dx   = max(-MAX_DELTA, min(MAX_DELTA, pan_dx))
            pan_dy   = max(-MAX_DELTA, min(MAX_DELTA, pan_dy))

            # New input overrides velocity; no input decays it (inertia / coast).
            if orbit_dx or orbit_dy:
                vel_ox, vel_oy = float(orbit_dx), float(orbit_dy)
            else:
                vel_ox *= ORBIT_INERTIA;  vel_oy *= ORBIT_INERTIA
                if abs(vel_ox) < STOP: vel_ox = 0.0
                if abs(vel_oy) < STOP: vel_oy = 0.0

            if pan_dx or pan_dy:
                vel_px, vel_py = float(pan_dx), float(pan_dy)
            else:
                vel_px *= ORBIT_INERTIA;  vel_py *= ORBIT_INERTIA
                if abs(vel_px) < STOP: vel_px = 0.0
                if abs(vel_py) < STOP: vel_py = 0.0

            # Ping Inventor when truly idle (motion fully stopped)
            if app is not None and not (vel_ox or vel_oy or vel_px or vel_py):
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

            # Apply camera
            try:
                if vel_ox or vel_oy:
                    do_orbit(app, vel_ox, vel_oy)
                if vel_px or vel_py:
                    do_pan(app, vel_px, vel_py)
            except Exception as exc:
                log.warning("Camera command failed: %s", exc)
                app = None
                _cs["valid"] = False
                vel_ox = vel_oy = vel_px = vel_py = 0.0

            # Drain stale packets accumulated during the COM call.
            sock.settimeout(0)
            _drained = 0
            try:
                while True:
                    sock.recvfrom(64)
                    _drained += 1
            except (BlockingIOError, socket.timeout):
                pass
            if _drained > 3:
                log.debug("drained %d stale packets (COM call ~%d ms)",
                          _drained, round(_drained * 8))

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        sock.close()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
