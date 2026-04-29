#!/usr/bin/env python3
"""
Trackpad Gesture 3D Camera Controller
=======================================
Reads Windows Precision Touchpad (WPT) raw HID data and maps two-finger
gestures to Autodesk Inventor camera movements:

    Two-finger translate  →  pan   (X / Z axes in camera space)
    Pinch / spread        →  zoom  (dolly in / out)
    Two-finger twist      →  orbit Y axis

Designed to run ALONGSIDE the existing trackpoint_3dmouse.ahk +
inventor_camera_server.py pair:
    • The TrackPoint script keeps handling X / Z orbit.
    • This script handles the remaining three degrees of freedom.

Because this script uses Windows Raw Input directly (not AutoHotInterception),
there is no conflict with the AHK script.  Both can run at the same time.

Dependencies:
    pip install pywin32 numpy

Usage:
    python trackpad_gestures.py
    (stop with Ctrl-C or close the console window)

Log file:  trackpad_gestures.log  (next to this script)
"""

import ctypes
import ctypes.wintypes
import logging
import math
import os
import struct
import sys
import threading
import time

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "trackpad_gestures.log")
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


def _log_unhandled(et, ev, tb):
    log.critical("UNHANDLED EXCEPTION", exc_info=(et, ev, tb))

sys.excepthook = _log_unhandled

try:
    import pythoncom
    import win32com.client
except ImportError:
    log.error("pywin32 not installed.  Run: pip install pywin32")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    log.error("numpy not installed.  Run: pip install numpy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration  — tune these to taste
# ---------------------------------------------------------------------------

# Fraction of eye→target distance panned per logical touchpad unit.
# WPT logical units are typically 0–4095 or 0–32767 depending on the device.
# Start conservative; we'll calibrate once we see real data.
PAN_SENSITIVITY   = 0.0003

# Zoom: fraction of eye→target distance per logical unit of pinch distance change.
ZOOM_SENSITIVITY  = 0.0005

# Y-axis orbit: radians per radian of measured finger twist.
TWIST_SENSITIVITY = 0.8

# Minimum distance between the two fingers (in logical units) before we treat
# them as a valid two-finger gesture.  Filters accidental grazes.
MIN_FINGER_SEPARATION = 50

# How long (seconds) a finger must be continuously absent before we consider
# the gesture ended and reset gesture state.
LIFTOFF_DEBOUNCE = 0.08

# ---------------------------------------------------------------------------
# Inventor document type constants
# ---------------------------------------------------------------------------
kPartDocumentObject     = 12290
kAssemblyDocumentObject = 12291
kDrawingDocumentObject  = 12292

# ---------------------------------------------------------------------------
# Persistent camera state (shared with orbit calls)
# ---------------------------------------------------------------------------
_cs = {
    "valid":  False,
    "eye":    None,
    "target": None,
    "up":     None,
    "r":      None,
    "last_t": 0.0,
}

RESYNC_IDLE = 0.5  # re-read camera from Inventor after this many seconds idle


def _rodrigues(v, k, theta):
    c, s = math.cos(theta), math.sin(theta)
    return v * c + np.cross(k, v) * s + k * (np.dot(k, v) * (1.0 - c))


def _read_cam(cam):
    eye    = np.array([cam.Eye.X,      cam.Eye.Y,      cam.Eye.Z     ], dtype=float)
    target = np.array([cam.Target.X,   cam.Target.Y,   cam.Target.Z  ], dtype=float)
    up_raw = np.array([cam.UpVector.X, cam.UpVector.Y, cam.UpVector.Z], dtype=float)
    r      = np.linalg.norm(eye - target)
    up_len = np.linalg.norm(up_raw)
    if r < 1e-9 or up_len < 1e-9:
        return False
    _cs.update(eye=eye.copy(), target=target.copy(), up=up_raw / up_len,
               r=r, valid=True, last_t=time.monotonic())
    return True


def _apply_cam(cam):
    try:
        cam.ApplyWithoutTransition()
    except Exception:
        cam.Apply()


def _active_view(app):
    try:
        doc = app.ActiveDocument
        if doc is None:
            return None
        if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject,
                                    kDrawingDocumentObject):
            return None
        return doc.Views.Item(1).Camera, app.TransientGeometry
    except Exception as e:
        log.debug("_active_view: %s", e)
        return None


# ---------------------------------------------------------------------------
# Windows Raw Input API — structs and constants
# ---------------------------------------------------------------------------
WM_INPUT          = 0x00FF
RIM_TYPEHID       = 2
RIDEV_INPUTSINK   = 0x00000100  # receive even when not in foreground
RIDEV_DEVNOTIFY   = 0x00002000
HID_USAGE_PAGE_DIGITIZER = 0x0D
HID_USAGE_TOUCHPAD       = 0x05

# RAWINPUTDEVICE
class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage",     ctypes.wintypes.USHORT),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("hwndTarget",  ctypes.wintypes.HWND),
    ]

# RAWINPUTHEADER
class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  ctypes.wintypes.DWORD),
        ("dwSize",  ctypes.wintypes.DWORD),
        ("hDevice", ctypes.wintypes.HANDLE),
        ("wParam",  ctypes.wintypes.WPARAM),
    ]

# RAWHID (variable-length tail handled separately)
class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", ctypes.wintypes.DWORD),
        ("dwCount",   ctypes.wintypes.DWORD),
        # bRawData follows in memory
    ]

# RAWINPUT (HID variant only; keyboard/mouse tails differ but we don't use them)
class RAWINPUT_HID(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("hid",    RAWHID),
        # variable-length bRawData follows
    ]

SIZEOF_RAWINPUT_HID = ctypes.sizeof(RAWINPUT_HID)

_u32 = ctypes.windll.user32

# WNDCLASSW must be defined manually — ctypes.wintypes does not include it.
WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.wintypes.HWND,
    ctypes.c_uint,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style",         ctypes.c_uint),
        ("lpfnWndProc",   WNDPROCTYPE),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     ctypes.wintypes.HANDLE),
        ("hIcon",         ctypes.wintypes.HANDLE),
        ("hCursor",       ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HANDLE),
        ("lpszMenuName",  ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
    ]


# ---------------------------------------------------------------------------
# WPT HID report parser
# ---------------------------------------------------------------------------
class WPTParser:
    """
    Parses Windows Precision Touchpad HID reports.

    WPT reports follow the Microsoft Precision Touchpad specification
    (https://docs.microsoft.com/en-us/windows-hardware/design/component-guidelines/
     windows-precision-touchpad-required-hid-top-level-collections).

    The standard report layout (Report ID 1, single-finger collection × N):
        Per-finger (typically 10 bytes each):
            Byte 0: Tip Switch (bit 0) + Contact ID (bits 2-7)
            Bytes 1-2: X (little-endian, logical 0–32767 or 0–4095)
            Bytes 3-4: Y (little-endian)
            Bytes 5-6: Width  (optional, may be absent)
            Bytes 7-8: Height (optional, may be absent)
        Last byte: Contact Count

    NOTE: The exact byte layout varies by manufacturer firmware.
          Run with --debug-hid to print raw report bytes so you can
          calibrate CONTACT_STRIDE, X_OFFSET, Y_OFFSET below.
    """

    # Adjust these if raw report logging shows a different layout.
    REPORT_ID    = 1     # Report ID byte (first byte of HID report)
    CONTACT_STRIDE = 10  # bytes per finger contact in the report
    TIP_OFFSET   = 0     # byte offset within a contact: tip-switch + contact-id
    X_OFFSET     = 1     # byte offset within a contact: X low byte
    Y_OFFSET     = 3     # byte offset within a contact: Y low byte
    COUNT_OFFSET = -1    # byte offset from end of report: contact count (-1 = last byte)

    # Maximum logical coordinate value (used to normalise; set to actual value after calibration)
    X_MAX = 32767
    Y_MAX = 32767

    def parse(self, report_bytes: bytes):
        """
        Parse one HID report.  Returns a list of active finger dicts:
            [{"id": int, "x": float (0-1), "y": float (0-1)}, ...]
        Returns None if this is not a recognised touch report.
        """
        if not report_bytes:
            return None

        # First byte is the Report ID
        if report_bytes[0] != self.REPORT_ID:
            return None

        data = report_bytes[1:]  # strip report ID
        if len(data) < self.CONTACT_STRIDE:
            return None

        # Contact count is the last byte (or COUNT_OFFSET from end)
        count = data[self.COUNT_OFFSET] if self.COUNT_OFFSET == -1 else data[self.COUNT_OFFSET]
        count = max(0, min(count, len(data) // self.CONTACT_STRIDE))

        fingers = []
        for i in range(count):
            base = i * self.CONTACT_STRIDE
            if base + self.Y_OFFSET + 2 > len(data):
                break
            tip_byte   = data[base + self.TIP_OFFSET]
            tip_switch = bool(tip_byte & 0x01)
            contact_id = (tip_byte >> 2) & 0x3F

            x_raw = struct.unpack_from("<H", data, base + self.X_OFFSET)[0]
            y_raw = struct.unpack_from("<H", data, base + self.Y_OFFSET)[0]

            if tip_switch:
                fingers.append({
                    "id": contact_id,
                    "x": x_raw / self.X_MAX,  # normalised 0–1
                    "y": y_raw / self.Y_MAX,
                    "x_raw": x_raw,
                    "y_raw": y_raw,
                })

        return fingers


# ---------------------------------------------------------------------------
# Gesture recogniser
# ---------------------------------------------------------------------------
class GestureRecognizer:
    """
    Converts per-frame finger lists into (pan_x, pan_z, zoom_delta, twist_y).

    Coordinate conventions (before Inventor mapping):
        pan_x   : positive = fingers moved right
        pan_z   : positive = fingers moved down (towards user)
        zoom    : positive = fingers spread apart (zoom in)
        twist_y : positive = fingers rotated counter-clockwise (orbit left)
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._prev_cx   = None
        self._prev_cy   = None
        self._prev_dist = None
        self._prev_ang  = None
        self._last_seen = 0.0

    def process(self, fingers: list) -> tuple | None:
        """
        Call once per raw-input event with the current active finger list.
        Returns (pan_x, pan_z, zoom, twist_y) deltas, or None if no valid
        two-finger gesture.
        """
        now = time.monotonic()

        if len(fingers) != 2:
            # Debounce: allow brief gaps (finger re-contact on edge of pad)
            if fingers and now - self._last_seen < LIFTOFF_DEBOUNCE:
                return None  # keep state; might recover within debounce window
            self.reset()
            return None

        self._last_seen = now

        p1, p2 = fingers[0], fingers[1]
        sep = math.hypot(p2["x_raw"] - p1["x_raw"],
                         p2["y_raw"] - p1["y_raw"])
        if sep < MIN_FINGER_SEPARATION:
            self.reset()
            return None

        cx = (p1["x"] + p2["x"]) * 0.5
        cy = (p1["y"] + p2["y"]) * 0.5
        dist = math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])
        ang  = math.atan2(p2["y"] - p1["y"], p2["x"] - p1["x"])

        if self._prev_cx is None:
            # First frame — record baseline, emit nothing
            self._prev_cx, self._prev_cy = cx, cy
            self._prev_dist = dist
            self._prev_ang  = ang
            return None

        pan_x = cx - self._prev_cx
        pan_z = cy - self._prev_cy
        zoom  = dist - self._prev_dist

        d_ang = ang - self._prev_ang
        # Wrap to [-π, π]
        if d_ang >  math.pi: d_ang -= 2 * math.pi
        if d_ang < -math.pi: d_ang += 2 * math.pi

        self._prev_cx, self._prev_cy = cx, cy
        self._prev_dist = dist
        self._prev_ang  = ang

        return pan_x, pan_z, zoom, d_ang


# ---------------------------------------------------------------------------
# Inventor camera commands
# ---------------------------------------------------------------------------
_apply_logged = False


def apply_gesture(app, pan_x, pan_z, zoom, twist_y):
    """Apply one frame of gesture deltas to the Inventor camera."""
    global _apply_logged

    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    now = time.monotonic()
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    eye    = _cs["eye"].copy()
    target = _cs["target"].copy()
    up     = _cs["up"].copy()
    r      = _cs["r"]

    fwd   = (target - eye)
    fwd  /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    right_len = np.linalg.norm(right)
    if right_len < 1e-9:
        return
    right /= right_len

    changed = False

    # --- Pan (translate eye + target together) ---
    if pan_x or pan_z:
        scale = r * PAN_SENSITIVITY
        # pan_x: right/left in camera space
        # pan_z: up/down in camera space  (trackpad Y-down → negate so swiping
        #        downward on the pad pans the model downward on screen)
        delta = right * (-pan_x * scale) + up * (pan_z * scale)
        eye    += delta
        target += delta
        changed = True

    # --- Zoom (dolly: move eye along view axis, keep target fixed) ---
    if zoom:
        # zoom > 0 means fingers spread = zoom in (eye moves toward target)
        dolly = fwd * (zoom * r * ZOOM_SENSITIVITY)
        new_eye = eye + dolly
        new_r   = np.linalg.norm(new_eye - target)
        if new_r > 1e-4:   # don't allow eye to pass through target
            eye = new_eye
            changed = True

    # --- Orbit Y (twist around world-up axis, keeping target fixed) ---
    if twist_y:
        world_up = np.array([0.0, 1.0, 0.0])
        angle    = twist_y * TWIST_SENSITIVITY
        offset   = eye - target
        offset   = _rodrigues(offset, world_up, angle)
        up       = _rodrigues(up,     world_up, angle)
        up      /= np.linalg.norm(up)
        eye      = target + offset
        changed  = True

    if not changed:
        return

    # Write back
    r_new = np.linalg.norm(eye - target)
    _cs.update(eye=eye, target=target, up=up, r=r_new, last_t=now, valid=True)

    cam.UpVector = tg.CreateUnitVector(float(up[0]),     float(up[1]),     float(up[2]))
    cam.Eye      = tg.CreatePoint(float(eye[0]),    float(eye[1]),    float(eye[2]))
    cam.Target   = tg.CreatePoint(float(target[0]), float(target[1]), float(target[2]))
    _apply_cam(cam)

    if not _apply_logged:
        log.info("First gesture applied to Inventor.")
        _apply_logged = True


# ---------------------------------------------------------------------------
# Raw Input message loop (runs in its own thread)
# ---------------------------------------------------------------------------
class TouchpadListener:
    """
    Creates a hidden message-only window, registers it for raw WPT HID input,
    and calls on_fingers(finger_list) from the message thread whenever a new
    HID report arrives.
    """

    def __init__(self, on_fingers, debug_hid=False):
        self._on_fingers = on_fingers
        self._debug_hid  = debug_hid
        self._parser     = WPTParser()
        self._hwnd       = None
        self._thread     = None
        self._ready      = threading.Event()

    # ---------- public API ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="TouchpadListener")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("TouchpadListener: message loop did not start in time")
        log.info("TouchpadListener started (HWND 0x%X).", self._hwnd)

    def stop(self):
        if self._hwnd:
            _u32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT

    # ---------- internals ----------

    def _run(self):
        """Message loop — runs entirely on this thread."""
        WCLASS_NAME = "TrackpadGestureWindow"

        wndproc_cb = WNDPROCTYPE(self._wndproc)   # must stay alive
        self._wndproc_cb = wndproc_cb              # keep reference so GC can't collect it

        wc = WNDCLASSW()
        wc.lpfnWndProc   = wndproc_cb
        wc.hInstance     = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = WCLASS_NAME
        _u32.RegisterClassW(ctypes.byref(wc))     # ignore failure (re-register is OK)

        HWND_MESSAGE = ctypes.wintypes.HWND(-3)
        self._hwnd = _u32.CreateWindowExW(
            0, WCLASS_NAME, "TrackpadGesture",
            0, 0, 0, 0, 0,
            HWND_MESSAGE, None,
            ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )
        if not self._hwnd:
            log.error("CreateWindowExW failed: %d", ctypes.GetLastError())
            self._ready.set()
            return

        # Register for raw HID input from WPT touchpads
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = HID_USAGE_PAGE_DIGITIZER
        rid.usUsage     = HID_USAGE_TOUCHPAD
        rid.dwFlags     = RIDEV_INPUTSINK
        rid.hwndTarget  = self._hwnd

        ok = ctypes.windll.user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(rid))
        if not ok:
            log.warning("RegisterRawInputDevices failed: %d. "
                        "Trying without INPUTSINK flag...", ctypes.GetLastError())
            rid.dwFlags    = 0
            rid.hwndTarget = self._hwnd
            ok = ctypes.windll.user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(rid))
            if not ok:
                log.error("RegisterRawInputDevices failed again: %d",
                          ctypes.GetLastError())

        self._ready.set()

        # Standard Win32 message pump
        msg = ctypes.wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw_input(lparam)
            return 0
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        # Query required buffer size
        size = ctypes.wintypes.UINT(0)
        ctypes.windll.user32.GetRawInputData(
            lparam, 0x10000003,  # RID_INPUT
            None, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if size.value == 0:
            return

        buf = (ctypes.c_byte * size.value)()
        read = ctypes.windll.user32.GetRawInputData(
            lparam, 0x10000003,
            buf, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if read == ctypes.wintypes.UINT(-1).value:
            return

        # Check that this is a HID (not keyboard/mouse) raw input message
        header = RAWINPUTHEADER.from_buffer(buf)
        if header.dwType != RIM_TYPEHID:
            return

        # Extract the HID report bytes
        hid_offset = ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWHID)
        report_bytes = bytes(buf[hid_offset:read])

        if self._debug_hid:
            log.debug("HID raw (%d bytes): %s", len(report_bytes),
                      report_bytes.hex(" "))

        fingers = self._parser.parse(report_bytes)
        if fingers is not None:
            self._on_fingers(fingers)


# ---------------------------------------------------------------------------
# Inventor connection
# ---------------------------------------------------------------------------
def get_inventor():
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception as e:
        log.debug("GetActiveObject failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trackpad gesture Inventor controller")
    parser.add_argument("--debug-hid", action="store_true",
                        help="Print every raw HID report to the log (for layout calibration)")
    args = parser.parse_args()

    pythoncom.CoInitialize()

    log.info("Trackpad gesture controller starting.")
    log.info("Pan sensitivity: %.6f  Zoom: %.6f  Twist: %.4f",
             PAN_SENSITIVITY, ZOOM_SENSITIVITY, TWIST_SENSITIVITY)
    if args.debug_hid:
        log.info("--debug-hid mode: raw HID bytes will be printed.")
    log.info("Use Ctrl-C to stop.")

    gesture = GestureRecognizer()
    app     = None
    last_reconnect = 0.0
    RECONNECT_INTERVAL = 2.0

    def on_fingers(fingers):
        nonlocal app
        if app is None:
            return

        result = gesture.process(fingers)
        if result is None:
            return

        pan_x, pan_z, zoom, twist_y = result
        try:
            apply_gesture(app, pan_x, pan_z, zoom, twist_y)
        except Exception as e:
            log.warning("apply_gesture failed: %s", e)
            # Don't disconnect here; let the main loop handle reconnect

    listener = TouchpadListener(on_fingers, debug_hid=args.debug_hid)
    listener.start()

    log.info("Listening for touchpad input.  Connecting to Inventor...")

    _heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()

            # Reconnect / connect to Inventor
            if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                last_reconnect = now
                _cs["valid"] = False
                app = get_inventor()
                if app:
                    log.info("Connected to Inventor.")
                else:
                    log.info("Waiting for Inventor...")

            # Heartbeat
            if now - _heartbeat >= 10.0:
                _heartbeat = now
                log.info("alive  app=%s",
                         "connected" if app else "disconnected")

            # Ping Inventor to detect disconnects
            if app is not None and (now - last_reconnect) > RECONNECT_INTERVAL * 10:
                last_reconnect = now
                try:
                    _ = app.Caption
                except Exception:
                    log.info("Lost connection to Inventor.")
                    app = None
                    _cs["valid"] = False

            time.sleep(0.1)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        listener.stop()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
