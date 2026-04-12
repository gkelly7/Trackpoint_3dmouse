#!/usr/bin/env python3
"""
Inventor 3D Mouse Camera Server
================================
Receives TrackPoint deltas from the AHK script via UDP and applies
cursor-independent orbit / pan to Autodesk Inventor 2024's active camera.

Dependencies
------------
    pip install pywin32 numpy

Usage
-----
    python inventor_camera_server.py
    (or launch automatically via the AHK script)

Tune ORBIT_SENSITIVITY and PAN_SENSITIVITY to taste.
"""

import logging
import math
import os
import socket
import struct
import sys
import time

# Log file — defined early so logging.basicConfig can use it
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventor_camera.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ]
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

# - Configuration -
UDP_HOST = "127.0.0.1"
UDP_PORT = 9999

# Radians per TrackPoint delta unit.
# Increase for faster orbit, decrease for finer control.
ORBIT_SENSITIVITY = 0.008

# Fraction of eye-target distance per TrackPoint delta unit.
# Increase for faster pan.
PAN_SENSITIVITY = 0.003

# Seconds between Inventor reconnect attempts
RECONNECT_INTERVAL = 2.0

# Inventor document type constants (from Inventor API)
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291

# Which world axis points "up" in your Inventor templates.
# Inventor default part/assembly templates are Y-up.
# Change to np.array([0.0, 0.0, 1.0]) if your models use Z-up.
WORLD_UP = np.array([0.0, 1.0, 0.0])
# -


def get_inventor():
    """Return a handle to a running Inventor instance, or None."""
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception:
        return None


def _active_3d_view(app):
    """Return (view, camera, tg) for the active Part/Assembly doc, or None."""
    try:
        doc = app.ActiveDocument
        if doc is None:
            return None
        if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject):
            return None
        view = doc.Views.Item(1)
        return view, view.Camera, app.TransientGeometry
    except Exception as exc:
        log.debug("_active_3d_view: %s", exc)
        return None


def do_orbit(app, dx: int, dy: int) -> None:
    """
    Orbit camera using spherical coordinates relative to WORLD_UP.
    Avoids any roll accumulation. Works correctly for Y-up (default Inventor).
    """
    result = _active_3d_view(app)
    if result is None:
        return
    view, cam, tg = result

    eye    = np.array([cam.Eye.X,    cam.Eye.Y,    cam.Eye.Z   ], dtype=float)
    target = np.array([cam.Target.X, cam.Target.Y, cam.Target.Z], dtype=float)

    offset = eye - target
    r      = np.linalg.norm(offset)
    if r < 1e-9:
        return

    up = WORLD_UP / np.linalg.norm(WORLD_UP)

    # Build two floor-plane axes orthogonal to WORLD_UP
    ref   = np.array([1., 0., 0.]) if abs(np.dot(up, [1., 0., 0.])) < 0.9 else np.array([0., 0., 1.])
    east  = np.cross(up, ref);  east  /= np.linalg.norm(east)
    north = np.cross(east, up)

    # Decompose current offset into spherical angles
    elev = math.asin(np.clip(np.dot(offset / r, up), -1.0, 1.0))
    proj = offset - up * np.dot(offset, up)
    proj_len = np.linalg.norm(proj)
    azim = math.atan2(np.dot(proj / proj_len, east),
                      np.dot(proj / proj_len, north)) if proj_len > 1e-9 else 0.0

    # Increment angles
    azim += dx * ORBIT_SENSITIVITY
    elev  = float(np.clip(elev + dy * ORBIT_SENSITIVITY,
                          -math.pi / 2 + 0.02, math.pi / 2 - 0.02))

    # Reconstruct eye position
    cos_e      = math.cos(elev)
    new_offset = r * (up * math.sin(elev) +
                      north * cos_e * math.cos(azim) +
                      east  * cos_e * math.sin(azim))
    new_eye    = target + new_offset

    # Keep up vector perpendicular to view direction, no roll
    nfwd   = new_offset / np.linalg.norm(new_offset)
    new_up = up - nfwd * np.dot(nfwd, up)
    nup_n  = np.linalg.norm(new_up)
    if nup_n > 1e-9:
        new_up /= nup_n
        cam.UpVector = tg.CreateUnitVector(
            float(new_up[0]), float(new_up[1]), float(new_up[2]))

    cam.Eye = tg.CreatePoint(float(new_eye[0]), float(new_eye[1]), float(new_eye[2]))
    cam.Apply()
    log.debug("orbit dx=%d dy=%d  azim=%.1fdeg elev=%.1fdeg", dx, dy,
              math.degrees(azim), math.degrees(elev))


def do_pan(app, dx: int, dy: int) -> None:
    """Pan camera (translate eye + target together in the camera plane)."""
    result = _active_3d_view(app)
    if result is None:
        return
    view, cam, tg = result

    eye    = np.array([cam.Eye.X,    cam.Eye.Y,    cam.Eye.Z   ], dtype=float)
    target = np.array([cam.Target.X, cam.Target.Y, cam.Target.Z], dtype=float)
    up     = np.array([cam.UpVector.X, cam.UpVector.Y, cam.UpVector.Z], dtype=float)

    offset = eye - target
    dist   = np.linalg.norm(offset)
    if dist < 1e-9:
        return

    fwd   = offset / dist
    right = np.cross(fwd, up)
    r_len = np.linalg.norm(right)
    if r_len < 1e-9:
        return
    right /= r_len

    cam_up = np.cross(right, fwd)
    cu_n   = np.linalg.norm(cam_up)
    if cu_n < 1e-9:
        return
    cam_up /= cu_n

    scale = dist * PAN_SENSITIVITY
    delta = right * (dx * scale) + cam_up * (dy * scale)

    ne = eye    + delta
    nt = target + delta
    cam.Eye    = tg.CreatePoint(float(ne[0]), float(ne[1]), float(ne[2]))
    cam.Target = tg.CreatePoint(float(nt[0]), float(nt[1]), float(nt[2]))
    cam.Apply()
    log.debug("pan dx=%d dy=%d", dx, dy)


def main() -> None:
    pythoncom.CoInitialize()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.settimeout(RECONNECT_INTERVAL)

    log.info("Inventor camera server listening on UDP %s:%d", UDP_HOST, UDP_PORT)
    log.info("Orbit sensitivity : %.4f rad/unit", ORBIT_SENSITIVITY)
    log.info("Pan sensitivity   : %.4f - dist/unit", PAN_SENSITIVITY)

    app            = None
    last_reconnect = 0.0

    try:
        while True:
            now = time.monotonic()

            # Reconnect to Inventor if not connected
            if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                last_reconnect = now
                app = get_inventor()
                if app:
                    log.info("Connected to Inventor.")
                else:
                    log.info("Waiting for Inventor to start-")

            # Receive UDP packet from AHK
            try:
                data, _ = sock.recvfrom(64)
            except socket.timeout:
                # Use the timeout to ping Inventor and detect crashes
                if app is not None:
                    try:
                        _ = app.Caption  # lightweight ping
                    except Exception:
                        log.info("Lost connection to Inventor - will reconnect.")
                        app = None
                continue

            if len(data) < 9:
                continue

            x, y = struct.unpack_from("<ii", data, 0)
            mode  = struct.unpack_from("<B",  data, 8)[0]

            if app is None:
                continue

            try:
                if mode == 0:
                    do_orbit(app, x, y)
                else:
                    do_pan(app, x, y)
            except Exception as exc:
                log.debug("Camera command failed (%s) - forcing reconnect.", exc)
                app = None

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        sock.close()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
