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


def _log_unhandled_exception(exc_type, exc_value, exc_tb):
    """Catch all unhandled exceptions and write them to the log file."""
    log.critical("UNHANDLED EXCEPTION", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _log_unhandled_exception

try:
    import pythoncom
    import win32com.client
except ImportError:
    log.error("pywin32 not installed.  Run:  pip install pywin32")
    sys.exit(1)

import ctypes
import ctypes.wintypes
_user32 = ctypes.windll.user32

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
ORBIT_SENSITIVITY = 0.02

# Fraction of eye-target distance added per TrackPoint delta unit for pan.
PAN_SENSITIVITY = 0.001

# Flat pan sensitivity for drawing files (units per TrackPoint delta).
DRAWING_PAN_SENSITIVITY = 0.5

# Inertia / coast factor applied when the TrackPoint stops moving.
# 0.0 = stops instantly, 0.90 = long coast.
ORBIT_INERTIA = 0.2

# After this many seconds of inactivity the server re-reads the camera from
# Inventor so that scroll/ViewCube changes are picked up on the next orbit.
RESYNC_IDLE = 0.6

RECONNECT_INTERVAL = 2.0   # seconds between Inventor reconnect attempts

# Camera update rate in Hz.
FRAME_HZ = 60

# Input smoothing: exponential moving average applied to raw TrackPoint deltas.
# 0.0 = no smoothing, 0.5 = good default, 0.7 = heavy/velvety
INPUT_SMOOTH = 0.5

# Inventor document type constants
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291
kDrawingDocumentObject  = 12292

# World-up axis for your Inventor templates (Y-up by default).
WORLD_UP = np.array([0.0, 1.0, 0.0])

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistent camera state
# ---------------------------------------------------------------------------
_cs = {
    "valid":  False,
    "eye":    None,
    "target": None,
    "up":     None,
    "r":      None,
    "last_t": 0.0,
}


def _rodrigues(v: np.ndarray, k: np.ndarray, theta: float) -> np.ndarray:
    """Rotate vector v by angle theta (radians) around unit vector k."""
    c = math.cos(theta)
    s = math.sin(theta)
    return v * c + np.cross(k, v) * s + k * (np.dot(k, v) * (1.0 - c))


def _read_cam(cam):
    """Read Inventor Camera into our state dict."""
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
# Inventor connection
# ---------------------------------------------------------------------------
def get_inventor():
    """Return the running Inventor.Application COM object, or None."""
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception as e:
        log.debug("GetActiveObject failed: %s", e)
        return None


def _active_view(app):
    """Return (cam, tg) for the active document view, or None."""
    try:
        doc = app.ActiveDocument
        if doc is None:
            return None
        if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject,
                                    kDrawingDocumentObject):
            return None
        cam = doc.Views.Item(1).Camera
        return cam, app.TransientGeometry
    except Exception as exc:
        log.debug("_active_view: %s", exc)
        return None


_apply_cam_logged = False


def _apply_cam(cam) -> None:
    """Apply camera without Inventor's blocking view-transition animation."""
    global _apply_cam_logged
    try:
        cam.ApplyWithoutTransition()
        if not _apply_cam_logged:
            log.info("Camera method: ApplyWithoutTransition()")
            _apply_cam_logged = True
    except Exception:
        if not _apply_cam_logged:
            log.info("Camera method: Apply() fallback")
            _apply_cam_logged = True
        cam.Apply()


# ---------------------------------------------------------------------------
# Orbit / Pan
# ---------------------------------------------------------------------------
def do_orbit(app, dx: float, dy: float) -> None:
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    # Drawing documents are 2-D — redirect orbit to pan.
    try:
        if app.ActiveDocument.DocumentType == kDrawingDocumentObject:
            do_pan(app, dx, dy)
            return
    except Exception:
        pass

    now = time.monotonic()
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    offset = _cs["eye"] - _cs["target"]
    up     = _cs["up"].copy()

    # Horizontal: rotate around the current up vector.
    if dx:
        h_angle = -dx * ORBIT_SENSITIVITY
        offset  = _rodrigues(offset, up, h_angle)

    # Vertical: rotate around the camera's right vector.
    if dy:
        fwd   = -offset / np.linalg.norm(offset)
        right = np.cross(fwd, up)
        rlen  = np.linalg.norm(right)
        if rlen < 1e-9:
            right = np.cross(fwd, np.array([1., 0., 0.]))
            rlen  = np.linalg.norm(right)
        if rlen > 1e-9:
            right  /= rlen
            v_angle = -dy * ORBIT_SENSITIVITY
            offset  = _rodrigues(offset, right, v_angle)
            up      = _rodrigues(up,     right, v_angle)
            up     /= np.linalg.norm(up)

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
    try:
        if app.ActiveDocument.DocumentType == kDrawingDocumentObject:
            scale = DRAWING_PAN_SENSITIVITY
    except Exception:
        pass

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

    app          = None
    last_reconnect = 0.0
    FRAME_S      = 1.0 / FRAME_HZ

    vel_ox = vel_oy = 0.0   # orbit velocity
    vel_px = vel_py = 0.0   # pan velocity
    STOP   = 0.25

    _heartbeat_t = 0.0
    HEARTBEAT_S  = 5.0

    try:
        while True:
            try:
                now = time.monotonic()

                # Connect / reconnect to Inventor
                if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                    last_reconnect = now
                    _cs["valid"] = False
                    app = get_inventor()
                    if app:
                        log.info("Connected to Inventor.")
                    else:
                        log.info("Waiting for Inventor to start...")

                # Periodic heartbeat
                if (now - _heartbeat_t) >= HEARTBEAT_S:
                    _heartbeat_t = now
                    log.info("alive  app=%s  vel=(%.2f,%.2f,%.2f,%.2f)",
                             "connected" if app is not None else "disconnected",
                             vel_ox, vel_oy, vel_px, vel_py)

                # Accumulate raw input for one frame window
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
                    log.debug("pkt  mode=%d x=%+d y=%+d", mode, x, y)
                    if mode == 0:
                        orbit_dx += x;  orbit_dy += y
                    else:
                        pan_dx   += x;  pan_dy   += y

                # Cap to prevent runaway from stale packet accumulation
                MAX_DELTA = 30
                orbit_dx = max(-MAX_DELTA, min(MAX_DELTA, orbit_dx))
                orbit_dy = max(-MAX_DELTA, min(MAX_DELTA, orbit_dy))
                pan_dx   = max(-MAX_DELTA, min(MAX_DELTA, pan_dx))
                pan_dy   = max(-MAX_DELTA, min(MAX_DELTA, pan_dy))

                # Blend new input into running velocity (EMA smoothing + inertia)
                if orbit_dx or orbit_dy:
                    vel_ox = vel_ox * INPUT_SMOOTH + float(orbit_dx) * (1.0 - INPUT_SMOOTH)
                    vel_oy = vel_oy * INPUT_SMOOTH + float(orbit_dy) * (1.0 - INPUT_SMOOTH)
                else:
                    vel_ox *= ORBIT_INERTIA;  vel_oy *= ORBIT_INERTIA
                    if abs(vel_ox) < STOP: vel_ox = 0.0
                    if abs(vel_oy) < STOP: vel_oy = 0.0

                if pan_dx or pan_dy:
                    vel_px = vel_px * INPUT_SMOOTH + float(pan_dx) * (1.0 - INPUT_SMOOTH)
                    vel_py = vel_py * INPUT_SMOOTH + float(pan_dy) * (1.0 - INPUT_SMOOTH)
                else:
                    vel_px *= ORBIT_INERTIA;  vel_py *= ORBIT_INERTIA
                    if abs(vel_px) < STOP: vel_px = 0.0
                    if abs(vel_py) < STOP: vel_py = 0.0

                # When fully idle, ping Inventor to detect disconnects
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

                # Drain stale packets accumulated during the COM call
                sock.settimeout(0)
                _drained = 0
                try:
                    while True:
                        sock.recvfrom(64)
                        _drained += 1
                except (BlockingIOError, socket.timeout):
                    pass
                if _drained > 3:
                    log.debug("drained %d stale packets", _drained)

            except Exception as _loop_exc:
                log.exception("Unhandled error in main loop (will retry): %s", _loop_exc)
                app = None
                _cs["valid"] = False
                time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        sock.close()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
