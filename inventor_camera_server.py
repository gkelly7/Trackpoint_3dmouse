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
import socket
import struct
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
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

# ─── Configuration ────────────────────────────────────────────────────────────
UDP_HOST = "127.0.0.1"
UDP_PORT = 9999

# Radians per TrackPoint delta unit.
# Increase for faster orbit, decrease for finer control.
ORBIT_SENSITIVITY = 0.010

# Fraction of eye-target distance per TrackPoint delta unit.
# Increase for faster pan.
PAN_SENSITIVITY = 0.003

# Seconds between Inventor reconnect attempts (when Inventor is not yet open)
RECONNECT_INTERVAL = 2.0

# Inventor document type constants (from Inventor API)
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291

# World up vector (Inventor default is Z-up)
WORLD_UP = np.array([0.0, 0.0, 1.0])
# ──────────────────────────────────────────────────────────────────────────────


def get_inventor():
    """Return a handle to a running Inventor instance, or None."""
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception:
        return None


def rodrigues(v: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    """Rotate vector *v* around *axis* by *angle* radians (Rodrigues' formula)."""
    norm = np.linalg.norm(axis)
    if norm < 1e-10:
        return v.copy()
    axis = axis / norm
    c, s = math.cos(angle), math.sin(angle)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1.0 - c)


def _get_camera_vectors(cam):
    """Return (eye, target, up, offset, fwd, right) as numpy arrays, or raise."""
    eye    = np.array([cam.Eye.X,    cam.Eye.Y,    cam.Eye.Z   ], dtype=float)
    target = np.array([cam.Target.X, cam.Target.Y, cam.Target.Z], dtype=float)
    up     = np.array([cam.UpVector.X, cam.UpVector.Y, cam.UpVector.Z], dtype=float)

    offset = eye - target
    dist   = np.linalg.norm(offset)
    if dist < 1e-9:
        raise ValueError("Eye coincides with target")

    fwd   = offset / dist
    right = np.cross(fwd, up)           # points camera-LEFT (see SETUP.md note)
    r_len = np.linalg.norm(right)
    if r_len < 1e-9:
        raise ValueError("Camera looking along world-up axis")
    right /= r_len

    return eye, target, up, offset, fwd, right


def do_orbit(app, dx: int, dy: int) -> None:
    """
    Orbit the camera around the target point.
    - dx > 0  →  orbit right  (see right face of model)
    - dy > 0  →  orbit down   (see bottom of model, matches Inventor cursor convention)
    """
    doc = app.ActiveDocument
    if doc is None:
        return
    if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject):
        return

    view = doc.Views.Item(1)
    cam  = view.Camera
    tg   = app.TransientGeometry

    eye, target, up, offset, fwd, right = _get_camera_vectors(cam)

    # Horizontal orbit: rotate around WORLD_UP to prevent camera roll
    if dx:
        offset = rodrigues(offset, WORLD_UP, dx * ORBIT_SENSITIVITY)

    # Vertical orbit: rotate around camera-right axis (pitch)
    if dy:
        offset = rodrigues(offset, right, dy * ORBIT_SENSITIVITY)

    new_eye = target + offset

    # Recompute camera up: project WORLD_UP onto the plane ⊥ to new view dir,
    # keeping the horizon stable across many orbit steps.
    nfwd   = offset / np.linalg.norm(offset)
    new_up = WORLD_UP - nfwd * np.dot(nfwd, WORLD_UP)
    nup_n  = np.linalg.norm(new_up)
    if nup_n > 1e-9:
        new_up /= nup_n
        cam.UpVector = tg.CreateUnitVector(
            float(new_up[0]), float(new_up[1]), float(new_up[2])
        )

    cam.Eye = tg.CreatePoint(float(new_eye[0]), float(new_eye[1]), float(new_eye[2]))
    cam.Apply()


def do_pan(app, dx: int, dy: int) -> None:
    """
    Pan the camera (translate eye + target in the camera plane).
    - dx > 0  →  pan right  (model shifts right in view)
    - dy > 0  →  pan down   (model shifts down in view)
    """
    doc = app.ActiveDocument
    if doc is None:
        return
    if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject):
        return

    view = doc.Views.Item(1)
    cam  = view.Camera
    tg   = app.TransientGeometry

    eye, target, up, offset, fwd, right = _get_camera_vectors(cam)

    # Camera up in world space: cross(right, fwd)
    # (right = cross(fwd, up) so right × fwd points upward in the view)
    cam_up = np.cross(right, fwd)
    cu_n   = np.linalg.norm(cam_up)
    if cu_n < 1e-9:
        return
    cam_up /= cu_n

    # Scale pan speed by distance so it feels consistent at any zoom level
    dist  = np.linalg.norm(offset)
    scale = dist * PAN_SENSITIVITY

    # right points camera-LEFT, so dx>0 moves eye left → model appears to move right
    delta  = right * (dx * scale) + cam_up * (dy * scale)
    ne     = eye    + delta
    nt     = target + delta

    cam.Eye    = tg.CreatePoint(float(ne[0]), float(ne[1]), float(ne[2]))
    cam.Target = tg.CreatePoint(float(nt[0]), float(nt[1]), float(nt[2]))
    cam.Apply()


def main() -> None:
    pythoncom.CoInitialize()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.settimeout(RECONNECT_INTERVAL)

    log.info("Inventor camera server listening on UDP %s:%d", UDP_HOST, UDP_PORT)
    log.info("Orbit sensitivity : %.4f rad/unit", ORBIT_SENSITIVITY)
    log.info("Pan sensitivity   : %.4f × dist/unit", PAN_SENSITIVITY)

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
                    log.info("Waiting for Inventor to start…")

            # Receive UDP packet from AHK
            try:
                data, _ = sock.recvfrom(64)
            except socket.timeout:
                # Use the timeout to ping Inventor and detect crashes
                if app is not None:
                    try:
                        _ = app.Caption  # lightweight ping
                    except Exception:
                        log.info("Lost connection to Inventor — will reconnect.")
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
                log.debug("Camera command failed (%s) — forcing reconnect.", exc)
                app = None

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        sock.close()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
