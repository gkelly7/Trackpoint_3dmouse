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
import winreg

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

# Use ctypes directly for foreground-window detection (no extra pywin32 modules needed)
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
# Increase for faster orbit.  Start here and tune with the log (azim/elev lines).
ORBIT_SENSITIVITY = 0.02

# Fraction of eye-target distance added per TrackPoint delta unit for pan.
PAN_SENSITIVITY = 0.001

# Flat pan sensitivity for drawing files (units per TrackPoint delta).
# Drawing camera geometry is orthographic / differently scaled, so a fixed
# value works better than the r-proportional scale used for 3D views.
# Increase if panning feels too slow in drawings, decrease if too fast.
DRAWING_PAN_SENSITIVITY = 0.5

# Inertia / coast factor applied when the TrackPoint stops moving.
# 0.0 = stops instantly, 0.90 = long coast.  Applies to both orbit and pan.
ORBIT_INERTIA = 0.2

# After this many seconds of inactivity the server re-reads the camera from
# Inventor so that scroll/ViewCube changes are picked up on the next orbit.
RESYNC_IDLE = 0.6
 
RECONNECT_INTERVAL = 2.0   # seconds between Inventor reconnect attempts
INSTANCE_POLL_INTERVAL = 1.0  # seconds between active-instance checks (multi-instance)

# Camera update rate in Hz.  Higher = smoother motion, more COM calls per second.
# 30 Hz is safe if Inventor is using Apply() fallback (~96 ms per call).
# If ApplyWithoutTransition() is available (check the log at startup), you can
# raise this to 60 or even 90 for noticeably smoother movement.
# Rule of thumb: keep FRAME_HZ < 1000 / <COM call duration in ms>.
FRAME_HZ = 60

# Input smoothing: exponential moving average applied to raw TrackPoint deltas.
# 0.0 = no smoothing (raw, may feel jerky on hard surfaces).
# 0.4 = light smoothing -- good starting point.
# 0.7 = heavy smoothing (responsive but velvety; adds slight lag).
INPUT_SMOOTH = 0.5

# Inventor document type constants
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291
kDrawingDocumentObject  = 12292

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
PROCESS_QUERY_LIMITED_INFO = 0x1000  # for QueryFullProcessImageNameW


def _hwnd_to_inventor_pid(hwnd):
    """Return the PID if hwnd belongs to Inventor.exe, otherwise 0.

    Uses ctypes / kernel32 directly; no pywin32 modules required.
    """
    try:
        pid_buf = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
        pid = pid_buf.value
        if not pid:
            return 0
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFO, False, pid)
        if not h:
            return 0
        try:
            buf  = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size))
            if ok and buf.value.lower().endswith("inventor.exe"):
                return pid
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return 0


# CLSID for "Inventor.Application" -- resolved once at startup so the
# ROT scan can match by CLSID (class-moniker names look like
# "clsid:{XXXXXXXX-...}", not like the ProgID string).
#
# pythoncom.CLSIDFromProgID uses the Win32 CLSIDFromProgID API which can miss
# per-user COM registrations (HKCU\SOFTWARE\Classes).  pywintypes.IID is more
# robust -- try it first, then fall back to the traditional API.
_INV_CLSID = None
try:
    import pywintypes as _pw
    _INV_CLSID = str(_pw.IID("Inventor.Application")).lower()
except Exception:
    try:
        _INV_CLSID = str(pythoncom.CLSIDFromProgID("Inventor.Application")).lower()
    except Exception:
        pass  # will warn at startup


def _get_all_inventor_apps():
    """Enumerate every Inventor.Application instance from the Windows ROT.

    Matches by CLSID if the registry lookup succeeded; otherwise falls back to
    searching for "inventor" anywhere in the moniker display name (handles
    per-user COM registration where CLSIDFromProgID can fail).

    NOTE: Python must run at the SAME integrity level as Inventor (both
    non-elevated is ideal).  An elevated Python process cannot see
    non-elevated COM objects via GetActiveObject or the ROT.
    """
    apps = []
    try:
        ctx  = pythoncom.CreateBindCtx(0)
        rot  = pythoncom.GetRunningObjectTable()
        enum = rot.EnumRunning()
        all_names = []
        while True:
            monikers = enum.Next(1)
            if not monikers:
                break
            try:
                name = monikers[0].GetDisplayName(ctx, None).lower()
                all_names.append(name)
                # Primary match: by CLSID (exact).  Fallback: "inventor" substring.
                if _INV_CLSID:
                    is_match = (_INV_CLSID in name)
                else:
                    is_match = ("inventor" in name)
                if not is_match:
                    continue
                obj = rot.GetObject(monikers[0])
                apps.append(win32com.client.Dispatch(obj))
                log.info("ROT: found Inventor entry: %s", name)
            except Exception as e:
                log.debug("ROT entry error: %s", e)
        if not apps:
            log.debug("ROT: %d total entries, none matched Inventor", len(all_names))
            for n in all_names[:15]:
                log.debug("  ROT: %s", n)
    except Exception as e:
        log.warning("ROT scan exception: %s", e)
    return apps


def get_inventor():
    """Return the Inventor instance whose main window is in the foreground.

    Scans the Windows Running Object Table for all Inventor instances, then
    matches the foreground window's process ID to an instance's MainFrameHWND.
    Falls back to the first available instance, or None if Inventor is not running.

    IMPORTANT: This only works when Python runs at the same integrity level as
    Inventor (non-elevated).  See the AHK script for the required launch method.
    """
    apps = _get_all_inventor_apps()
    if not apps:
        # Legacy fallback: single-instance via GetActiveObject
        try:
            return win32com.client.GetActiveObject("Inventor.Application")
        except Exception as e:
            log.debug("GetActiveObject('Inventor.Application') failed: %s", e)
            return None

    # Try to find the instance whose main window is currently in the foreground.
    # Use ctypes directly to avoid any pywin32 DLL-load side-effects.
    try:
        fg_hwnd = _user32.GetForegroundWindow()
        if fg_hwnd:
            fg_pid = ctypes.c_ulong(0)
            _user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid))
            for a in apps:
                try:
                    inv_hwnd = int(a.MainFrameHWND)
                    if not inv_hwnd:
                        continue
                    inv_pid = ctypes.c_ulong(0)
                    _user32.GetWindowThreadProcessId(inv_hwnd, ctypes.byref(inv_pid))
                    if inv_pid.value == fg_pid.value:
                        return a
                except Exception:
                    pass
    except Exception:
        pass

    return apps[0]  # fallback: first registered instance


def _active_view(app):
    """Return (cam, tg) for active Part/Assembly, or None."""
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
def do_orbit(app, dx: float, dy: float) -> None:
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    # Drawing documents are 2-D — orbit makes no sense; redirect to pan.
    try:
        if app.ActiveDocument.DocumentType == kDrawingDocumentObject:
            do_pan(app, dx, dy)
            return
    except Exception:
        pass

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
    try:
        if app.ActiveDocument.DocumentType == kDrawingDocumentObject:
            scale = DRAWING_PAN_SENSITIVITY
    except Exception:
        pass
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

    # --- Startup diagnostics ---------------------------------------------------
    # 1. Elevation check
    try:
        is_elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        if is_elevated:
            log.warning("ELEVATED: Python is running as Administrator.")
            log.warning("  Non-elevated Inventor cannot be seen in this ROT.")
            log.warning("  Fix: ensure AHK launches pyw.exe via Shell.Application.")
        else:
            log.info("Integrity: non-elevated -- correct for non-elevated Inventor.")
    except Exception as _e:
        log.debug("Elevation check failed: %s", _e)

    # 2. CLSID / registry check
    log.info("Inventor CLSID (pywintypes.IID): %s", _INV_CLSID or "NOT RESOLVED")
    try:
        k = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Inventor.Application\CLSID")
        log.info("Registry HKCR\\Inventor.Application\\CLSID = %s",
                 winreg.QueryValue(k, ""))
    except FileNotFoundError:
        log.warning("Registry: HKCR\\Inventor.Application\\CLSID not found.")
        # Also check per-user classes
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"SOFTWARE\Classes\Inventor.Application\CLSID")
            log.info("Registry HKCU\\...\\Inventor.Application\\CLSID = %s",
                     winreg.QueryValue(k, ""))
        except FileNotFoundError:
            log.warning("Registry: HKCU\\...\\Inventor.Application\\CLSID not found either.")
            log.warning("  Inventor may not be installed, or uses a different ProgID.")
    except Exception as _e:
        log.warning("Registry check error: %s", _e)
    # ---------------------------------------------------------------------------

    app                 = None
    last_reconnect      = 0.0
    last_instance_check = time.monotonic()  # defer first poll by INSTANCE_POLL_INTERVAL
    cur_app_hwnd        = 0        # MainFrameHWND of current instance
    cur_app_pid         = 0        # PID of current instance (for multi-instance switch)
    FRAME_S             = 1.0 / FRAME_HZ   # derived from config

    # Running velocity (floats) that carry inertia between frames.
    # Set directly from input when packets arrive; decayed by ORBIT_INERTIA otherwise.
    vel_ox = vel_oy = 0.0   # orbit
    vel_px = vel_py = 0.0   # pan
    STOP   = 0.25           # velocity below this is snapped to zero
    _heartbeat_t = 0.0
    HEARTBEAT_S  = 5.0      # log "alive" every this many seconds

    try:
        while True:
            try:
                now = time.monotonic()

                # Reconnect to Inventor if not connected
                if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                    last_reconnect = now
                    _cs["valid"] = False
                    app = get_inventor()
                    if app:
                        try:
                            cur_app_hwnd = int(app.MainFrameHWND)
                        except Exception:
                            cur_app_hwnd = 0
                        # Resolve PID so instance poll can detect a focus switch
                        if cur_app_hwnd:
                            _pid_buf = ctypes.c_ulong(0)
                            _user32.GetWindowThreadProcessId(
                                cur_app_hwnd, ctypes.byref(_pid_buf))
                            cur_app_pid = _pid_buf.value
                        log.info("Connected to Inventor (HWND %d, PID %d).",
                                 cur_app_hwnd, cur_app_pid)
                    else:
                        log.info("Waiting for Inventor to start...")

                # Heartbeat: log every HEARTBEAT_S seconds to confirm the server is alive
                if (now - _heartbeat_t) >= HEARTBEAT_S:
                    _heartbeat_t = now
                    log.info("alive  app=%s  vel=(%.2f,%.2f,%.2f,%.2f)",
                             "connected" if app is not None else "disconnected",
                             vel_ox, vel_oy, vel_px, vel_py)

                # Multi-instance: if a DIFFERENT Inventor.exe is now in the
                # foreground, force reconnect so GetActiveObject can return it.
                if app is not None and (now - last_instance_check) >= INSTANCE_POLL_INTERVAL:
                    last_instance_check = now
                    try:
                        fg_hwnd    = _user32.GetForegroundWindow()
                        fg_inv_pid = _hwnd_to_inventor_pid(fg_hwnd) if fg_hwnd else 0
                        if fg_inv_pid and fg_inv_pid != cur_app_pid:
                            log.info("Foreground Inventor changed (PID %d -> %d). "
                                     "Reconnecting...", cur_app_pid, fg_inv_pid)
                            app = None           # triggers reconnect on next iteration
                            last_reconnect = 0   # reconnect immediately, don't wait
                    except Exception as _ie:
                        log.warning("Instance poll error (non-fatal): %s", _ie)

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
                    log.debug("pkt  mode=%d x=%+d y=%+d", mode, x, y)
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

                # Blend new input into running velocity.
                # INPUT_SMOOTH > 0 applies an EMA to soften jittery deltas;
                # 0.0 = raw (fastest response), 0.7 = very smooth (slight lag).
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
