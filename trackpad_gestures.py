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

import collections
import ctypes
import ctypes.wintypes
import logging
import math
import os
import socket
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

# Fraction of eye→target distance panned when the two-finger centroid moves
# the FULL width (pan_x) or height (pan_z) of the pad.
# pan_x / pan_z are in normalised 0–1 space (divided by X_MAX / Y_MAX).
# Examples: 0.8 → a full-width swipe pans 80 % of the eye→target distance.
# Tune up if motion feels sluggish, down if it's too jumpy.
PAN_SENSITIVITY   = 0.8

# Fraction of eye→target distance the eye travels for a full-width pinch.
# zoom is the change in normalised finger separation (0–~1.4).
# A typical pinch (spread→touch) covers ~0.2–0.4 normalised units.
# 1.5 → a half-width pinch moves the eye ~30 % of the way to the target.
ZOOM_SENSITIVITY  = 1.5

# Y-axis orbit: radians per radian of measured finger twist.
TWIST_SENSITIVITY = 0.8

# --- Motion direction flags ---
# Set to True to flip the corresponding axis if it feels backwards.
PAN_INVERT_X  = False   # flip left/right pan
PAN_INVERT_Z  = False   # flip up/down pan
ZOOM_INVERT   = False   # True = spread fingers → zoom in  (standard pinch)
TWIST_INVERT  = True    # True = flip rotation direction

# Single-finger orbit sensitivity (radians per full pad width / height).
# 2.0 → dragging one full pad width rotates the model ~115°.
ORBIT_SENSITIVITY_H = 3.0
ORBIT_SENSITIVITY_V = 3.0
ORBIT_INVERT_H      = False   # flip horizontal orbit direction
ORBIT_INVERT_V      = True   # flip vertical   orbit direction

# Minimum distance between the two fingers (in logical units) before we treat
# them as a valid two-finger gesture.  Filters accidental grazes.
MIN_FINGER_SEPARATION = 50

# How long (seconds) a finger must be continuously absent before we consider
# the gesture ended and reset gesture state.
LIFTOFF_DEBOUNCE = 0.08

# How long (seconds) after the last HID report before the cursor/scroll
# suppression hooks are force-released.  Many WPT drivers do NOT send a
# final count=0 report on finger lift — they just stop sending — so the
# flags would otherwise stay set forever.  120 ms is safely above the
# ~10 ms WPT report interval but quick enough to feel instant on release.
LIFTOFF_WATCHDOG = 0.12

# ---------------------------------------------------------------------------
# TrackPoint / UDP configuration  (mirrors inventor_camera_server.py)
# ---------------------------------------------------------------------------
# UDP socket the AHK script sends TrackPoint packets to.
TP_UDP_HOST = "127.0.0.1"
TP_UDP_PORT = 9999

# Radians per TrackPoint delta unit for orbit.
TP_ORBIT_SENSITIVITY = 0.02

# Fraction of eye-target distance per delta unit for 3D-view pan.
TP_PAN_SENSITIVITY = 0.001

# Flat units per delta unit for Drawing-document pan.
TP_DRAWING_PAN_SENSITIVITY = 0.5

# Input smoothing (EMA).  0 = raw, 0.5 = default, 0.7 = heavy/velvety.
TP_INPUT_SMOOTH = 0.5

# Inertia decay when TrackPoint stops.  0 = instant stop, 0.2 = light coast.
TP_ORBIT_INERTIA = 0.2

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

# Scale factor converting a pinch zoom_delta to WM_MOUSEWHEEL zDelta units.
# Standard scroll notch = 120 units.  Using a smaller value (e.g. 40) means
# ~3 pinch frames to equal one notch — smooth zoom without discrete jumps.
# Increase if zoom feels too slow; decrease if too fast or jumpy.
ORTHO_ZOOM_SCROLL_SCALE = 40

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

# Low-level mouse hook — used to suppress WM_MOUSEWHEEL / WM_MOUSEHWHEEL
# while a two-finger gesture is active, preventing Inventor from also acting
# on the same touchpad events natively.
WH_MOUSE_LL    = 14
WM_MOUSEMOVE   = 0x0200
WM_MOUSEWHEEL  = 0x020A
WM_MOUSEHWHEEL = 0x020E
_LL_HOOKPROC   = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,   # LRESULT
    ctypes.c_int,       # nCode
    ctypes.c_size_t,    # wParam
    ctypes.c_ssize_t,   # lParam
)

# Event set while ≥2 fingers are on the pad; cleared when pad is idle.
# The mouse hook reads this to decide whether to block scroll events.
_two_fingers_active = threading.Event()

# Event set while exactly 1 finger is on the pad (orbit gesture).
# The mouse hook suppresses WM_MOUSEMOVE so the touchpad doesn't move the
# system cursor while the user is orbiting the model.
_single_finger_active = threading.Event()

# ---------------------------------------------------------------------------
# TrackPoint UDP input buffer  (filled by background thread, drained on main)
# ---------------------------------------------------------------------------
_tp_buf  = collections.deque(maxlen=120)  # (x, y, mode) 3-tuples
_tp_lock = threading.Lock()

# 3D gesture mode flag.
# True  = Desk Mode: apply 3D gestures to Inventor (set by AHK mode packet
#         OR by default when running standalone without AHK).
# False = Couch/Kid Mode: touchpad is a normal mouse; never suppress events.
# The AHK sends a 2-byte packet [0xFF, 0x01/0x00] via UDP when switching modes.
_3d_mode_active = threading.Event()
_3d_mode_active.set()   # default: assume 3D mode (standalone / Desk Mode)

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

# Declare Win32 API functions with pointer-sized types so 64-bit lParam values
# don't overflow on x64 Windows (c_long in wintypes is 32-bit even on 64-bit).
_u32.CallNextHookEx.restype  = ctypes.c_ssize_t
_u32.CallNextHookEx.argtypes = [
    ctypes.wintypes.HANDLE,  # hhk (can be NULL)
    ctypes.c_int,            # nCode
    ctypes.c_size_t,         # wParam (UINT_PTR — 64-bit on x64)
    ctypes.c_ssize_t,        # lParam (LONG_PTR — 64-bit on x64)
]

# WNDCLASSW must be defined manually — ctypes.wintypes does not include it.
#
# On 64-bit Windows, LRESULT / WPARAM / LPARAM are all 64-bit pointer-sized
# integers.  ctypes.wintypes.WPARAM/LPARAM are defined as c_long (32-bit) in
# Python's ctypes, which causes OverflowError when Windows passes pointer-sized
# values as lparam.  Use the proper 64-bit types explicitly.
WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,        # LRESULT  (LONG_PTR  — 64-bit on x64)
    ctypes.wintypes.HWND,
    ctypes.c_uint,           # UINT     message
    ctypes.c_size_t,         # WPARAM   (UINT_PTR  — 64-bit on x64)
    ctypes.c_ssize_t,        # LPARAM   (LONG_PTR  — 64-bit on x64)
)

# Declare DefWindowProcW with matching 64-bit types so passing large lparam
# values through the default-call-handling path also works.
_u32.DefWindowProcW.restype  = ctypes.c_ssize_t
_u32.DefWindowProcW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_uint,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
]

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

    # --- Calibrated for ThinkPad WPT (25-byte report, 4 contacts × 5 bytes) ---
    # Report layout:
    #   Byte  0    : Report ID = 3
    #   Bytes 1-5  : Contact 0  (5 bytes: tip_id + X_lo + X_hi + Y_lo + Y_hi)
    #   Bytes 6-10 : Contact 1
    #   Bytes 11-15: Contact 2
    #   Bytes 16-20: Contact 3
    #   Bytes 21-22: Scan time (uint16 LE, ignored)
    #   Byte  23   : Contact count (active fingers)
    #   Byte  24   : Button state
    REPORT_ID      = 3     # Report ID byte (first byte of HID report)
    CONTACT_STRIDE = 5     # bytes per contact: tip_id(1) + X(2) + Y(2)
    TIP_OFFSET     = 0     # byte within contact: bit0=Tip, bit1=InRange, bits2-7=ID
    X_OFFSET       = 1     # byte within contact: X low byte (uint16 LE)
    Y_OFFSET       = 3     # byte within contact: Y low byte (uint16 LE)
    COUNT_OFFSET   = -2    # index from end: -1 = button, -2 = contact count

    # Logical coordinate maximums.
    # HOW TO CALIBRATE:
    #   1. Run:  python trackpad_gestures.py --debug-hid
    #   2. Press one finger firmly into the FAR BOTTOM-RIGHT corner of the pad.
    #   3. Look for a log line like:
    #          fingers: [{'id':0, 'x_raw':NNNN, 'y_raw':MMMM, ...}]
    #   4. Set X_MAX = that x_raw value, Y_MAX = that y_raw value.
    X_MAX = 1150   # approximate — calibrate with a corner touch (see above)
    Y_MAX = 770   # approximate — calibrate with a corner touch (see above)

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

        # Contact count lives at COUNT_OFFSET from the end of the stripped data.
        count = data[self.COUNT_OFFSET]
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

        if fingers:
            log.debug("fingers (%d active): %s", len(fingers),
                      [(f["id"], f["x_raw"], f["y_raw"]) for f in fingers])

        return fingers


# ---------------------------------------------------------------------------
# Gesture recogniser
# ---------------------------------------------------------------------------
class GestureRecognizer:
    """
    Per-frame finger list → gesture dict.

    1 finger  → {"type": "orbit",   "dx": float, "dy": float}
    2 fingers → {"type": "gesture", "pan_x": float, "pan_z": float,
                                    "zoom": float,  "twist_y": float}
    other     → None

    Coordinate conventions (before Inventor mapping):
        orbit dx    : positive = finger moved right
        orbit dy    : positive = finger moved down
        pan_x       : positive = centroid moved right
        pan_z       : positive = centroid moved down
        zoom        : positive = fingers spread apart (zoom in)
        twist_y     : positive = fingers rotated counter-clockwise (roll CW on screen)
    """

    def __init__(self):
        self.reset()

    def reset(self):
        # 1-finger state
        self._prev_1x    = None
        self._prev_1y    = None
        # 2-finger state
        self._prev_cx    = None
        self._prev_cy    = None
        self._prev_dist  = None
        self._prev_ang   = None
        # shared
        self._last_count = 0
        self._last_seen  = 0.0

    def process(self, fingers: list) -> dict | None:
        now   = time.monotonic()
        count = len(fingers)

        # All-lift: debounce briefly then reset
        if count == 0:
            if now - self._last_seen < LIFTOFF_DEBOUNCE:
                return None
            self.reset()
            return None

        self._last_seen = now

        # Finger-count change: clear old state so first frame of new gesture
        # just records the baseline (no spurious jump).
        if count != self._last_count:
            self._prev_1x = self._prev_cx = None   # invalidate both
            self._last_count = count

        # ---- one finger: orbit ----
        if count == 1:
            cx = fingers[0]["x"]
            cy = fingers[0]["y"]
            if self._prev_1x is None:
                self._prev_1x, self._prev_1y = cx, cy
                return None
            dx = cx - self._prev_1x
            dy = cy - self._prev_1y
            self._prev_1x, self._prev_1y = cx, cy
            return {"type": "orbit", "dx": dx, "dy": dy}

        # ---- two fingers: pan / zoom / twist ----
        if count == 2:
            p1, p2 = fingers[0], fingers[1]
            sep = math.hypot(p2["x_raw"] - p1["x_raw"],
                             p2["y_raw"] - p1["y_raw"])
            if sep < MIN_FINGER_SEPARATION:
                self._prev_cx = None
                return None

            cx   = (p1["x"] + p2["x"]) * 0.5
            cy   = (p1["y"] + p2["y"]) * 0.5
            dist = math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])
            ang  = math.atan2(p2["y"] - p1["y"], p2["x"] - p1["x"])

            if self._prev_cx is None:
                self._prev_cx, self._prev_cy = cx, cy
                self._prev_dist = dist
                self._prev_ang  = ang
                return None

            pan_x = cx - self._prev_cx
            pan_z = cy - self._prev_cy
            zoom  = dist - self._prev_dist
            d_ang = ang - self._prev_ang
            if d_ang >  math.pi: d_ang -= 2 * math.pi
            if d_ang < -math.pi: d_ang += 2 * math.pi

            self._prev_cx, self._prev_cy = cx, cy
            self._prev_dist = dist
            self._prev_ang  = ang
            return {"type": "gesture",
                    "pan_x": pan_x, "pan_z": pan_z,
                    "zoom": zoom,   "twist_y": d_ang}

        # 3+ fingers: ignore
        self.reset()
        return None


# ---------------------------------------------------------------------------
# Inventor camera commands
# ---------------------------------------------------------------------------
_apply_logged = False


def apply_gesture(app, pan_x, pan_z, zoom, twist_y):
    """Apply one frame of gesture deltas to the Inventor camera."""
    global _apply_logged

    # Apply direction inversions (configure at top of file)
    if PAN_INVERT_X:  pan_x   = -pan_x
    if PAN_INVERT_Z:  pan_z   = -pan_z
    if ZOOM_INVERT:   zoom    = -zoom
    if TWIST_INVERT:  twist_y = -twist_y

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

    # --- Zoom ---
    # Perspective: dolly the eye (works because perspective frustum scales with
    # eye-target distance).
    # Orthographic: cam.ViewScale and ActiveView.Scale are not exposed by this
    # Inventor build's COM dispatch.  Inject WM_MOUSEWHEEL directly to the
    # viewport window instead — PostMessage bypasses our scroll-suppression hook
    # and uses Inventor's own zoom handler, which works in both view modes.
    # Sub-120 zDelta values give smooth, continuous zoom (no discrete notch jumps).
    _ortho_zoom = 0.0
    if zoom:
        try:
            is_perspective = cam.Perspective
        except Exception:
            is_perspective = True   # safer default: try dolly first
        log.info("zoom check: perspective=%s  zoom_delta=%.4f", is_perspective, zoom)

        if is_perspective:
            dolly   = fwd * (zoom * r * ZOOM_SENSITIVITY)
            new_eye = eye + dolly
            new_r   = np.linalg.norm(new_eye - target)
            if new_r > 1e-4:
                eye     = new_eye
                changed = True
        else:
            # Ortho: accumulate for scroll injection after cam write.
            _ortho_zoom = zoom

    # --- Roll (two-finger twist → rotate around the camera's forward/view axis) ---
    # Spinning two fingers around the pad centre maps naturally to rolling the
    # model around the axis that points straight through the lens (fwd).
    # Eye and target positions stay fixed; only the camera up-vector rotates,
    # which Inventor interprets as the model spinning on the view axis.
    if twist_y:
        angle = twist_y * TWIST_SENSITIVITY
        up    = _rodrigues(up, fwd, angle)
        up   /= np.linalg.norm(up)
        changed = True

    if not changed:
        return

    # Write back.
    r_new = np.linalg.norm(eye - target)
    _cs.update(eye=eye, target=target, up=up, r=r_new, last_t=now, valid=True)

    cam.UpVector = tg.CreateUnitVector(float(up[0]),     float(up[1]),     float(up[2]))
    cam.Eye      = tg.CreatePoint(float(eye[0]),    float(eye[1]),    float(eye[2]))
    cam.Target   = tg.CreatePoint(float(target[0]), float(target[1]), float(target[2]))
    _apply_cam(cam)

    # Ortho zoom: inject scroll AFTER the cam write so the camera position
    # is stable before Inventor processes the wheel event.
    if _ortho_zoom:
        _inject_wheel_zoom(app, _ortho_zoom)

    if not _apply_logged:
        log.info("First gesture applied to Inventor.")
        _apply_logged = True


def apply_orbit(app, dx, dy):
    """Apply one-finger orbit deltas to the Inventor camera."""
    if ORBIT_INVERT_H: dx = -dx
    if ORBIT_INVERT_V: dy = -dy

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

    fwd   = (target - eye)
    fwd  /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    right_len = np.linalg.norm(right)
    if right_len < 1e-9:
        return
    right /= right_len

    angle_h = -dx * ORBIT_SENSITIVITY_H   # drag right → orbit right
    angle_v =  dy * ORBIT_SENSITIVITY_V   # drag down  → tilt down

    # Horizontal orbit around camera-up (screen-aligned turntable).
    # Using 'up' instead of world [0,1,0] keeps rotation correct regardless
    # of model orientation (Y-up vs Z-up, or any tilted view).
    offset = eye - target
    offset = _rodrigues(offset, up, angle_h)
    # Rotating around 'up' leaves 'up' unchanged — no update needed.
    up_new = up.copy()

    # Vertical orbit around camera-right (tilt)
    offset = _rodrigues(offset, right, angle_v)
    up_new = _rodrigues(up_new, right, angle_v)
    up_new /= np.linalg.norm(up_new)

    eye   = target + offset
    r_new = np.linalg.norm(eye - target)
    _cs.update(eye=eye, target=target, up=up_new, r=r_new, last_t=now, valid=True)

    # Freeze the cursor so the touchpad finger motion doesn't visibly
    # move the pointer while orbiting.
    saved_pt = ctypes.wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(saved_pt))

    cam.UpVector = tg.CreateUnitVector(float(up_new[0]), float(up_new[1]), float(up_new[2]))
    cam.Eye      = tg.CreatePoint(float(eye[0]),    float(eye[1]),    float(eye[2]))
    cam.Target   = tg.CreatePoint(float(target[0]), float(target[1]), float(target[2]))
    _apply_cam(cam)

    _u32.SetCursorPos(saved_pt.x, saved_pt.y)


# ---------------------------------------------------------------------------
# TrackPoint camera commands  (share _cs with the touchpad functions above)
# ---------------------------------------------------------------------------

def _inject_wheel_zoom(app, zoom: float) -> None:
    """
    Post a WM_MOUSEWHEEL to Inventor's main window for ortho-mode zoom.
    Uses app.MainFrameHWND so we always target the right window regardless
    of where the cursor happens to be.  Accumulates sub-threshold deltas
    and fires in 120-unit increments so Inventor's zoom handler actually
    triggers — but the firing rate is proportional to pinch speed so zoom
    feels smooth rather than stepping.

    zoom > 0  (spread)  → positive zDelta → zoom IN
    zoom < 0  (pinch)   → negative zDelta → zoom OUT
    """
    try:
        hwnd = int(app.MainFrameHWND)
        if not hwnd:
            return

        _inject_wheel_zoom._acc += zoom * ORTHO_ZOOM_SCROLL_SCALE

        # Fire full WHEEL_DELTA (120) increments only — Inventor ignores smaller
        # values.  The accumulator ensures firing rate tracks gesture speed.
        WHEEL_DELTA = 120
        steps = int(_inject_wheel_zoom._acc / WHEEL_DELTA)
        if steps == 0:
            return
        _inject_wheel_zoom._acc -= steps * WHEEL_DELTA

        z = steps * WHEEL_DELTA
        z16    = ctypes.c_int16(max(-32768, min(32767, z))).value
        wparam = ctypes.c_uint32((z16 & 0xFFFF) << 16).value

        pt     = ctypes.wintypes.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))
        lparam = ctypes.c_int32(((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)).value

        log.debug("ortho wheel zoom: hwnd=0x%X z=%+d acc=%.1f",
                  hwnd, z, _inject_wheel_zoom._acc)
        _u32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, lparam)
    except Exception as e:
        log.debug("_inject_wheel_zoom: %s", e)

_inject_wheel_zoom._acc = 0.0   # per-function accumulator (survives call boundaries)


def apply_tp_orbit(app, dx: float, dy: float) -> None:
    """Orbit from a TrackPoint move delta, sharing the camera state with touchpad."""
    result = _active_view(app)
    if result is None:
        return
    cam, tg = result

    now = time.monotonic()
    if not _cs["valid"] or (now - _cs["last_t"]) > RESYNC_IDLE:
        if not _read_cam(cam):
            return

    offset = _cs["eye"] - _cs["target"]
    up     = _cs["up"].copy()

    # Horizontal: turntable around current up vector.
    if dx:
        h_angle = -dx * TP_ORBIT_SENSITIVITY
        offset  = _rodrigues(offset, up, h_angle)

    # Vertical: tilt around camera-right vector.
    if dy:
        fwd   = -offset / np.linalg.norm(offset)
        right = np.cross(fwd, up)
        rlen  = np.linalg.norm(right)
        if rlen < 1e-9:
            right = np.cross(fwd, np.array([1., 0., 0.]))
            rlen  = np.linalg.norm(right)
        if rlen > 1e-9:
            right  /= rlen
            v_angle = -dy * TP_ORBIT_SENSITIVITY
            offset  = _rodrigues(offset, right, v_angle)
            up      = _rodrigues(up,     right, v_angle)
            up     /= np.linalg.norm(up)

    new_eye = _cs["target"] + offset
    _cs.update(eye=new_eye, up=up, last_t=now, valid=True)

    cam.UpVector = tg.CreateUnitVector(float(up[0]),      float(up[1]),      float(up[2]))
    cam.Eye      = tg.CreatePoint(     float(new_eye[0]), float(new_eye[1]), float(new_eye[2]))
    _apply_cam(cam)


def apply_tp_pan(app, dx: float, dy: float) -> None:
    """Pan from a TrackPoint move delta, sharing the camera state with touchpad."""
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
    rlen  = np.linalg.norm(right)
    if rlen < 1e-9:
        return
    right /= rlen

    scale = _cs["r"] * TP_PAN_SENSITIVITY
    try:
        if app.ActiveDocument.DocumentType == kDrawingDocumentObject:
            scale = TP_DRAWING_PAN_SENSITIVITY
    except Exception:
        pass

    delta      = right * (-dx * scale) + up * (dy * scale)
    new_eye    = eye    + delta
    new_target = target + delta
    _cs.update(eye=new_eye, target=new_target, last_t=now, valid=True)

    cam.Eye    = tg.CreatePoint(float(new_eye[0]),    float(new_eye[1]),    float(new_eye[2]))
    cam.Target = tg.CreatePoint(float(new_target[0]), float(new_target[1]), float(new_target[2]))
    _apply_cam(cam)


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

        # Install a low-level mouse hook to swallow WM_MOUSEWHEEL /
        # WM_MOUSEHWHEEL while two fingers are on the pad.  This prevents
        # Inventor from receiving the native scroll-to-zoom / pinch-to-zoom
        # events that the WPT driver synthesises from the same HID data.
        # The hook fires on THIS thread (inside GetMessageW) so the Event flag
        # set by on_fingers (also this thread) is always up-to-date.
        def _ll_mouse_hook(nCode, wParam, lParam):
            if nCode >= 0:
                # Suppress scroll events while two fingers are on the pad (pinch / pan)
                if wParam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL) and _two_fingers_active.is_set():
                    return 1   # consume — do NOT forward to apps
                # Suppress cursor movement while one finger is on the pad (orbit).
                # This prevents the touchpad from moving the system cursor while
                # the user is orbiting the model with a single finger.
                if wParam == WM_MOUSEMOVE and _single_finger_active.is_set():
                    return 1   # consume
            return _u32.CallNextHookEx(None, nCode, wParam, lParam)

        self._ll_hook_cb = _LL_HOOKPROC(_ll_mouse_hook)  # keep ref alive
        self._ll_hook = _u32.SetWindowsHookExW(
            WH_MOUSE_LL, self._ll_hook_cb, None, 0)
        if self._ll_hook:
            log.info("Mouse-scroll suppression hook installed.")
        else:
            log.warning("SetWindowsHookExW(WH_MOUSE_LL) failed (err %d) — "
                        "native scroll may interfere.", ctypes.GetLastError())

        # Standard Win32 message pump
        msg = ctypes.wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

        if self._ll_hook:
            _u32.UnhookWindowsHookEx(self._ll_hook)
            self._ll_hook = None

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

        # c_ubyte (unsigned) is required here; c_byte is signed (-128..127) and
        # bytes() will raise ValueError on any byte value above 127.
        buf = (ctypes.c_ubyte * size.value)()
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
# Elevation helpers
# ---------------------------------------------------------------------------
def _is_elevated() -> bool:
    """True if this process has admin / high-integrity privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Inventor connection
# ---------------------------------------------------------------------------
def _inventor_is_foreground() -> bool:
    """
    True when an Inventor window (or any ancestor/child) has keyboard focus.
    Walks up the parent chain because Inventor child widgets (toolbars, panels,
    viewport) have focus but their own window title may be empty.
    """
    try:
        hwnd = _u32.GetForegroundWindow()
        buf  = ctypes.create_unicode_buffer(512)
        while hwnd:
            _u32.GetWindowTextW(hwnd, buf, 512)
            if "Inventor" in buf.value:
                return True
            hwnd = _u32.GetParent(hwnd)
        return False
    except Exception:
        return True   # fail open — apply gesture rather than drop it


def get_inventor():
    try:
        return win32com.client.GetActiveObject("Inventor.Application")
    except Exception as e:
        log.debug("GetActiveObject failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# TrackPoint UDP listener  (background thread)
# ---------------------------------------------------------------------------
def _start_tp_udp_listener() -> threading.Thread:
    """
    Listens for 9-byte TrackPoint packets from the AHK script on UDP 9999.
    Appends (x, y, mode) tuples to _tp_buf for consumption by the main loop.
    """
    def _run():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((TP_UDP_HOST, TP_UDP_PORT))
            sock.settimeout(0.5)
            log.info("TrackPoint UDP listener on %s:%d", TP_UDP_HOST, TP_UDP_PORT)
            while True:
                try:
                    data, _ = sock.recvfrom(64)
                    if len(data) == 2 and data[0] == 0xFF:
                        # Mode-control packet from AHK: [0xFF, enable_byte]
                        if data[1]:
                            _3d_mode_active.set()
                            log.info("AHK: Desk Mode — 3D gestures ENABLED")
                        else:
                            _3d_mode_active.clear()
                            # Release any held suppression immediately
                            _single_finger_active.clear()
                            _two_fingers_active.clear()
                            log.info("AHK: Couch/Kid Mode — 3D gestures DISABLED")
                    elif len(data) >= 9:
                        x, y = struct.unpack_from("<ii", data, 0)
                        mode = struct.unpack_from("<B",  data, 8)[0]
                        with _tp_lock:
                            _tp_buf.append((x, y, mode))
                except socket.timeout:
                    pass
        except Exception as e:
            log.warning("TrackPoint UDP listener stopped: %s", e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True, name="TrackPointUDP")
    t.start()
    return t


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

    # Inventor must be accessible via COM (GetActiveObject / Windows ROT).
    # If Inventor runs elevated (as Administrator), this script must also run
    # elevated — both processes must be at the same integrity level.
    if _is_elevated():
        log.info("Running as Administrator.")
    else:
        log.info("Running as standard user. "
                 "If Inventor is elevated, start this script from an admin prompt.")

    log.info("Trackpad gesture controller starting.")
    log.info("Touchpad — Pan: %.2f  Zoom: %.2f  Twist: %.2f",
             PAN_SENSITIVITY, ZOOM_SENSITIVITY, TWIST_SENSITIVITY)
    log.info("TrackPoint — Orbit: %.4f  Pan: %.4f",
             TP_ORBIT_SENSITIVITY, TP_PAN_SENSITIVITY)
    if args.debug_hid:
        log.info("--debug-hid mode: raw HID bytes will be printed.")
    log.info("Use Ctrl-C to stop.")

    gesture = GestureRecognizer()
    app     = None
    last_reconnect = 0.0
    RECONNECT_INTERVAL = 2.0

    # Start TrackPoint UDP listener (background thread).
    _start_tp_udp_listener()

    # TrackPoint velocity state (EMA smoothing + inertia).
    tp_vel_ox = tp_vel_oy = 0.0
    tp_vel_px = tp_vel_py = 0.0
    TP_STOP = 0.25

    # Capped ring-buffer: holds at most 5 frames.
    # If Inventor is slow, old frames are silently dropped so there is never
    # more than ~50 ms of backlog and motion stops promptly on finger lift.
    _finger_buf  = collections.deque(maxlen=5)
    _finger_lock = threading.Lock()
    _last_touch_t = [0.0]   # time of last non-empty HID report (for watchdog)

    def on_fingers(fingers):
        """Called from the TouchpadListener thread — only buffer, never touch COM."""
        n = len(fingers)
        now_t = time.monotonic()

        # Suppress cursor/scroll only when BOTH conditions are true:
        #   1. AHK is in Desk Mode (_3d_mode_active set) — or standalone (default)
        #   2. Inventor is the foreground window
        # Outside Inventor in Desk Mode, the touchpad behaves as a normal mouse.
        # In Couch/Kid Mode (_3d_mode_active clear), never suppress anything.
        active_3d = _3d_mode_active.is_set() and _inventor_is_foreground()

        was_active = _single_finger_active.is_set() or _two_fingers_active.is_set()

        if n >= 2 and active_3d:
            _two_fingers_active.set()
            _single_finger_active.clear()
            _last_touch_t[0] = now_t
            if not was_active:
                _cs["valid"] = False
        elif n == 1 and active_3d:
            _single_finger_active.set()
            _two_fingers_active.clear()
            _last_touch_t[0] = now_t
            if not was_active:
                _cs["valid"] = False
        else:
            # Couch/Kid Mode, Inventor not in front, or no fingers — pass through.
            _single_finger_active.clear()
            _two_fingers_active.clear()

        with _finger_lock:
            _finger_buf.append(fingers)

    listener = TouchpadListener(on_fingers, debug_hid=args.debug_hid)
    listener.start()

    log.info("Listening for touchpad input.  Connecting to Inventor...")

    _heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()

            # Reconnect / connect to Inventor (main thread = COM thread)
            if app is None and (now - last_reconnect) >= RECONNECT_INTERVAL:
                last_reconnect = now
                _cs["valid"] = False
                app = get_inventor()
                if app:
                    log.info("Connected to Inventor.")
                else:
                    log.info("Waiting for Inventor...")

            # Drain the finger buffer on the COM thread.
            # Accumulate all queued deltas into ONE apply_gesture call so
            # Inventor is only poked once per 10 ms tick — smooth & backlog-free.
            with _finger_lock:
                batch = list(_finger_buf)
                _finger_buf.clear()

            if batch and app is not None:
                # Skip gesture processing when in Couch/Kid Mode or Inventor
                # is not the foreground window.
                if not _3d_mode_active.is_set() or not _inventor_is_foreground():
                    if log.isEnabledFor(logging.DEBUG):
                        hwnd = _u32.GetForegroundWindow()
                        buf  = ctypes.create_unicode_buffer(256)
                        _u32.GetWindowTextW(hwnd, buf, 256)
                        log.debug("gesture skipped: 3d_mode=%s foreground='%s'",
                                  _3d_mode_active.is_set(), buf.value)
                    gesture.reset()
                else:
                    pan_x_t = pan_z_t = zoom_t = twist_t = 0.0
                    orbit_dx = orbit_dy = 0.0
                    had_gesture = had_orbit = False

                    for fingers in batch:
                        result = gesture.process(fingers)
                        if result is None:
                            continue
                        if result["type"] == "orbit":
                            orbit_dx   += result["dx"]
                            orbit_dy   += result["dy"]
                            had_orbit   = True
                        elif result["type"] == "gesture":
                            pan_x_t    += result["pan_x"]
                            pan_z_t    += result["pan_z"]
                            zoom_t     += result["zoom"]
                            twist_t    += result["twist_y"]
                            had_gesture = True

                    if had_orbit:
                        try:
                            apply_orbit(app, orbit_dx, orbit_dy)
                        except Exception as e:
                            log.warning("apply_orbit failed: %s", e)

                    if had_gesture:
                        try:
                            apply_gesture(app, pan_x_t, pan_z_t, zoom_t, twist_t)
                        except Exception as e:
                            log.warning("apply_gesture failed: %s", e)

            # --- TrackPoint (UDP) ---
            # The AHK script only sends packets when Inventor is active, so no
            # additional foreground check is needed here.
            with _tp_lock:
                tp_batch = list(_tp_buf)
                _tp_buf.clear()

            if tp_batch and app is not None:
                raw_ox = raw_oy = raw_px = raw_py = 0
                for x, y, mode in tp_batch:
                    if mode == 0:
                        raw_ox += x;  raw_oy += y
                    else:
                        raw_px += x;  raw_py += y

                # Cap runaway deltas (same as inventor_camera_server.py)
                MAX_D = 30
                raw_ox = max(-MAX_D, min(MAX_D, raw_ox))
                raw_oy = max(-MAX_D, min(MAX_D, raw_oy))
                raw_px = max(-MAX_D, min(MAX_D, raw_px))
                raw_py = max(-MAX_D, min(MAX_D, raw_py))

                # EMA smoothing + inertia (identical to inventor_camera_server.py)
                if raw_ox or raw_oy:
                    tp_vel_ox = tp_vel_ox * TP_INPUT_SMOOTH + float(raw_ox) * (1.0 - TP_INPUT_SMOOTH)
                    tp_vel_oy = tp_vel_oy * TP_INPUT_SMOOTH + float(raw_oy) * (1.0 - TP_INPUT_SMOOTH)
                else:
                    tp_vel_ox *= TP_ORBIT_INERTIA;  tp_vel_oy *= TP_ORBIT_INERTIA
                    if abs(tp_vel_ox) < TP_STOP: tp_vel_ox = 0.0
                    if abs(tp_vel_oy) < TP_STOP: tp_vel_oy = 0.0

                if raw_px or raw_py:
                    tp_vel_px = tp_vel_px * TP_INPUT_SMOOTH + float(raw_px) * (1.0 - TP_INPUT_SMOOTH)
                    tp_vel_py = tp_vel_py * TP_INPUT_SMOOTH + float(raw_py) * (1.0 - TP_INPUT_SMOOTH)
                else:
                    tp_vel_px *= TP_ORBIT_INERTIA;  tp_vel_py *= TP_ORBIT_INERTIA
                    if abs(tp_vel_px) < TP_STOP: tp_vel_px = 0.0
                    if abs(tp_vel_py) < TP_STOP: tp_vel_py = 0.0

                try:
                    if tp_vel_ox or tp_vel_oy:
                        apply_tp_orbit(app, tp_vel_ox, tp_vel_oy)
                    if tp_vel_px or tp_vel_py:
                        apply_tp_pan(app, tp_vel_px, tp_vel_py)
                except Exception as e:
                    log.warning("TrackPoint apply failed: %s", e)

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

            # Watchdog: if no HID touch data for LIFTOFF_WATCHDOG seconds,
            # force-clear the cursor/scroll suppression flags.  Many WPT
            # drivers stop sending reports on lift without a final count=0
            # report, which would otherwise permanently block the mouse cursor.
            if _single_finger_active.is_set() or _two_fingers_active.is_set():
                if (now - _last_touch_t[0]) > LIFTOFF_WATCHDOG:
                    log.debug("watchdog: releasing touch flags "
                              "(%.0f ms since last HID report)",
                              (now - _last_touch_t[0]) * 1000)
                    _single_finger_active.clear()
                    _two_fingers_active.clear()
                    gesture.reset()

            time.sleep(0.01)   # 10 ms tick

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        listener.stop()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
