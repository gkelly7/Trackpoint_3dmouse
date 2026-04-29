# Trackpad Gesture Controller — Setup & Calibration
=====================================================

## Quick start

```
python trackpad_gestures.py
```

Run this **alongside** the existing `trackpoint_3dmouse.ahk` — no conflict.
The TrackPoint keeps doing X/Z orbit.  The trackpad handles the rest.

---

## Step 1 — Verify raw data is arriving

Run with `--debug-hid` to print every raw HID byte from the touchpad:

```
python trackpad_gestures.py --debug-hid
```

Touch the trackpad with one or two fingers and check the log.  You should
see lines like:

```
DEBUG  HID raw (31 bytes): 01 01 4e 0a 00 3a 07 00 00 00 02 ...
```

If you see **nothing**, the Raw Input registration is not reaching the
touchpad.  This can happen if:

  - The touchpad driver does not expose a WPT HID device (unlikely on a
    ThinkPad but possible with old/non-WPT drivers).
  - A security policy blocks raw HID registration.
  - Another application has exclusive HID access (uncommon).

---

## Step 2 — Calibrate the HID parser

The standard WPT report layout is:

```
Byte  0   : Report ID (should be 0x01 for touch)
Byte  1   : Tip switch (bit 0) + Contact ID (bits 2–7)
Bytes 2–3 : X coordinate (little-endian uint16)
Bytes 4–5 : Y coordinate (little-endian uint16)
Bytes 6–9 : Width / height / pressure (optional, varies by device)
...         (above block repeated per finger)
Last byte : Contact count
```

Compare your raw bytes to this layout.  If your device uses a different
stride or field order, update these constants in `trackpad_gestures.py`:

```python
class WPTParser:
    REPORT_ID      = 1
    CONTACT_STRIDE = 10   # bytes per finger entry
    TIP_OFFSET     = 0    # byte within entry: tip + contact-id
    X_OFFSET       = 1    # byte within entry: X low byte (uint16 LE)
    Y_OFFSET       = 3    # byte within entry: Y low byte (uint16 LE)
    COUNT_OFFSET   = -1   # offset from end of report: contact count
    X_MAX          = 32767
    Y_MAX          = 32767
```

**How to calibrate X_MAX / Y_MAX:**
Touch the very bottom-right corner of the trackpad with one finger, read the
`x_raw` and `y_raw` values from the debug log.  Set `X_MAX` and `Y_MAX` to
those values.

---

## Step 3 — Tune sensitivities

Once gestures are detected and connected to Inventor, tune these in
`trackpad_gestures.py`:

```python
PAN_SENSITIVITY   = 0.0003   # two-finger translate → pan
ZOOM_SENSITIVITY  = 0.0005   # pinch distance → zoom
TWIST_SENSITIVITY = 0.8      # twist angle (radians) → Y-orbit
```

Start with the defaults.  If pan feels too slow, increase `PAN_SENSITIVITY`.
If a small twist causes a huge rotation, decrease `TWIST_SENSITIVITY`.

---

## Gesture mapping

| Gesture                        | Inventor action         |
|--------------------------------|-------------------------|
| Two fingers, same direction    | Pan (X / Z camera axes) |
| Two fingers, pinch / spread    | Zoom in / out           |
| Two fingers, rotate (twist)    | Orbit Y axis            |
| One finger or three+ fingers   | Normal cursor / ignored |

The TrackPoint (via `trackpoint_3dmouse.ahk`) still handles X / Z orbit.

---

## Known limitations & next steps

1. **Cursor still moves during gestures.**  Two-finger pan will also
   move the cursor.  Fix: add AHK input suppression for two-finger events,
   or register as the WPT exclusive consumer (complex, requires a separate
   kernel-side step).

2. **Twist is the hardest gesture.**  The angular resolution on a small
   trackpad is low.  If twist feels unreliable, consider mapping it to a
   two-finger hold + TrackPoint X instead.

3. **No inertia yet.**  The current implementation applies deltas raw.
   Once the parser and sensitivities are calibrated, adding an EMA + coast
   loop (same as `inventor_camera_server.py`) is straightforward.

4. **Combining the scripts.**  Once this feels good, both the TrackPoint
   orbit and the trackpad gestures can be merged into one Python process
   sharing a single COM connection, with a lock around camera writes.
