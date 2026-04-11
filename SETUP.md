# TrackPoint → Inventor 3D Mouse  —  Setup Guide

## How it works

```
TrackPoint hardware
        │
        ▼  AutoHotInterception (raw HID, device-filtered)
AHK script  ──────────────────────────────────────────────────────►  all other apps
        │  Inventor.exe active?
        │  Yes: send x,y,mode as 9-byte UDP packet to 127.0.0.1:9999
        │
        ▼
inventor_camera_server.py  (Python UDP server)
        │
        ▼  win32com.client  (Inventor COM API)
Inventor camera  ──  cursor-independent orbit / pan
```

**Controls in Inventor (Desk Mode)**

| Gesture | Action |
|---------|--------|
| TrackPoint alone | Orbit (rotate model) |
| Center button held + TrackPoint | Pan |
| Mouse wheel | Zoom (unchanged) |

All other apps keep the existing middle-mouse-drag behaviour.

---

## Prerequisites

### 1. Python (3.9+)
```
winget install Python.Python.3.12
```

### 2. Python packages
```
pip install pywin32 numpy
```

### 3. AutoHotkey v2
```
winget install AutoHotkey.AutoHotkey
```

### 4. AutoHotInterception  
Download from https://github.com/evilC/AutoHotInterception/releases  
and place the `Lib\` folder next to the AHK script.

### 5. Interception driver  
AutoHotInterception needs the Interception kernel driver.  
Run the installer from the AHI release package (requires restart).

---

## File layout

Place these two files in the **same folder**:

```
📁 YourScriptFolder\
    trackpoint_3dmouse.ahk
    inventor_camera_server.py
    📁 Lib\
        AutoHotInterception.ahk
        AutoHotInterception.dll
```

---

## Step 1 — Find your TrackPoint VID & PID

Open **Device Manager** → *Mice and other pointing devices* → double-click the TrackPoint entry → **Details** tab → `Hardware IDs`.

You'll see something like:
```
HID\VID_17EF&PID_6047&...
```

Edit the AHK script and update:
```ahk
global tpVid := 0x17EF   ; example Lenovo ThinkPad TrackPoint
global tpPid := 0x6047   ; example
```

Common ThinkPad values:
- VID `0x17EF`  (Lenovo)
- PID varies by model — check Device Manager

---

## Step 2 — Run the script

1. **Start Inventor 2024** first (or it will auto-connect when you open it later).
2. **Right-click** `trackpoint_3dmouse.ahk` → *Run as Administrator*  
   (the script self-elevates, but running as admin avoids the UAC popup).

The Python camera server starts automatically in the background.  
A tray icon shows the current mode.

---

## Step 3 — Verify

- Open a part or assembly in Inventor.
- Switch to **Desk Mode** (default on start, tray icon shows `3d.ico`).
- Move the TrackPoint — the model should orbit **without the cursor moving**.
- Hold the **center button** and move the TrackPoint — the model should pan.

If the model doesn't move, check the console:
```
python inventor_camera_server.py
```
You should see `Connected to Inventor.` when a document is open.

---

## Tuning sensitivity

Edit the top of `inventor_camera_server.py`:

```python
ORBIT_SENSITIVITY = 0.010  # radians per TrackPoint unit — increase for faster orbit
PAN_SENSITIVITY   = 0.003  # × eye-target distance per unit — increase for faster pan
```

The TrackPoint typically reports ±2–5 units per gentle move and ±10–20 for fast sweeps.  
`0.010 rad/unit` → a fast sweep (~15 units) ≈ 8.5° of rotation.

---

## Modes quick reference

| Mode | How to enter | TrackPoint | Trackpad |
|------|-------------|------------|---------|
| **Desk** (default) | Script start / plug in ext. mouse removed | 3D camera (Inventor) / middle-drag (others) | OFF |
| **Couch** | Hold center + press left TrackPoint button | Normal mouse | ON |
| **Kid** | Ctrl+Alt+K | OFF | OFF |

---

## Troubleshooting

**"Could not find TrackPoint"**  
→ Check VID/PID in Device Manager and update the AHK script.

**Camera doesn't move in Inventor**  
→ Run `python inventor_camera_server.py` manually and check for errors.  
→ Make sure a Part or Assembly document is open (Drawing documents are skipped).  
→ Make sure `pythonw.exe` is on your PATH (`where pythonw` in cmd).

**Orbit feels inverted**  
→ Negate `dx` or `dy` in `Send3DMove` inside the AHK script, or flip the sign  
  of `dx`/`dy` inside `do_orbit()` in the Python server.

**Roll accumulates over many orbits**  
→ The horizontal orbit uses World Z-up to prevent this. If your model uses  
  Y-up, change `WORLD_UP = np.array([0.0, 1.0, 0.0])` in the Python server.

**Pan speed wrong for my model scale**  
→ `PAN_SENSITIVITY` is proportional to eye-target distance, so it auto-scales.  
  If still off, adjust the constant.

**Shift key leaks into Inventor**  
→ Already suppressed. If you still see interference, check that you're running  
  the modified AHK script (the one with the `WinActive` check in `OnCenterBtn`).
