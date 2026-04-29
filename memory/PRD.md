# TrackPoint → Inventor 3D Mouse Emulator

## Problem Statement
User wants to use the ThinkPad TrackPoint as a cursor-independent 3D mouse
while working in Autodesk Inventor 2024, without relying on cursor movement.
Orbit on bare TrackPoint movement; pan with center button held; zoom via scroll wheel.

## Architecture
- **trackpoint_3dmouse.ahk** — AHK v2 script (builds on user's existing script).
  Uses AutoHotInterception to intercept raw TrackPoint input by device VID/PID.
  In Desk Mode + Inventor active: sends x,y deltas via UDP (9 bytes, 127.0.0.1:9999)
  instead of mouse_event. All other apps use the original middle-drag behaviour.
- **inventor_camera_server.py** — Python UDP server.
  Connects to Inventor 2024 via win32com COM API (`GetActiveObject`).
  `do_orbit()`: Rodrigues rotation of Eye around Target (horizontal=WORLD_UP, vertical=camera-right).
  `do_pan()`: translate Eye+Target together in camera plane.

## IPC Protocol
9-byte UDP packet from AHK → Python:
  bytes 0-3: x delta  (int32, little-endian)
  bytes 4-7: y delta  (int32, little-endian)
  byte    8: mode     (0=orbit, 1=pan)

## Modes
- Desk (default): TrackPoint = 3D camera in Inventor / middle-drag elsewhere
- Couch (center+left): Trackpad on, normal mouse
- Kids (Ctrl+Alt+K): Everything off, only Esc works

## Implemented
- 2025-01: AHK UDP socket init (WSA), Send3DMove(), modified OnTrackpointMove()
- 2025-01: OnCenterBtn patched to suppress Shift/Home in Inventor
- 2025-01: Python camera server with orbit + pan math
- 2025-01: Auto-launch Python server from AHK; auto-kill on exit
- 2025-01: SETUP.md with prerequisites, VID/PID instructions, sensitivity tuning
- 2026-02: Replaced spherical-coord orbit with camera-relative Rodrigues rotation
  - Horizontal orbit: rotates around world up (turntable, always left/right relative to view)
  - Vertical orbit: rotates around camera's current right vector (always tilts relative to view)
  - Removed hard elevation clamp -- full 360 degrees vertical now possible
- 2026-02: Fixed pan up/down reversal (dy sign was negated, now positive)
- 2026-02: _apply_cam() logs which method is used on first call
- 2026-02: Removed 3D Mouse (SpaceMouse) mode entirely from AHK — only Desk/Couch/Kid modes remain
- 2026-02: Multi-instance Inventor support in Python server
  - _get_all_inventor_apps(): enumerates all Inventor instances via Windows ROT
  - get_inventor(): matches foreground window PID to instance MainFrameHWND
  - Main loop polls every INSTANCE_POLL_INTERVAL (1s) and auto-switches to focused instance
  - last_reconnect reset to 0 on instance switch so reconnect is immediate (no 2s delay)
- 2026-02: Fixed elevation mismatch (root cause of "no response in Inventor")
  - AHK script was launching pyw.exe elevated (inherited from *RunAs admin)
  - Elevated Python cannot see non-elevated Inventor in the Windows ROT
  - Fix: launch pyw.exe via Shell.Application.ShellExecute (always non-elevated)
  - PID discovery moved to async WMI query via SetTimer (2s after launch)
  - Added diagnostic logging: CLSID at startup, ROT entries on scan miss, GetActiveObject exception message
- 2026-02: AppSwitcher AHK helper provided (InventorMainWindows + CycleInventorInstances)
  - Filters Inventor sub-windows via: no owner + non-empty title + "Inventor" in title

## User Personas
- Power CAD user with ThinkPad, no dedicated 3D mouse, familiar with AHK+Python

## Trackpad Gesture Script (trackpad_gestures.py)
Standalone script reading raw WPT HID reports via RegisterRawInputDevices.
Maps 2-finger gestures to Inventor camera: pan / pinch-zoom / twist-rotate.
- 2026-02: Created hidden Win32 message window + RegisterRawInputDevices for WPT
- 2026-02: Calibrated WPTParser for ThinkPad: REPORT_ID=3, CONTACT_STRIDE=5,
  5-byte format: tip_id(1) + X_u16_LE(2) + Y_u16_LE(2), COUNT_OFFSET=-2
- 2026-02: Added debug logging in parse() showing finger (id, x_raw, y_raw) tuples
- 2026-02: Added 2-finger debug log in on_fingers() callback
- 2026-02: Added X_MAX/Y_MAX calibration instructions in code comments

### Next steps for trackpad
1. User runs: python trackpad_gestures.py --debug-hid
2. Verify "fingers (N active): [(id, x_raw, y_raw), ...]" lines appear
3. Calibrate X_MAX / Y_MAX using far-corner touch (read x_raw / y_raw from log)
4. Test 2-finger gestures in Inventor; tune PAN_SENSITIVITY / ZOOM_SENSITIVITY
5. (Stretch) Twist-to-rotate: test if angle noise is acceptable on hardware

## Backlog / Enhancements
- P1: Test with actual Inventor 2024 and tune default sensitivities
- P1: Add separate ORBIT_SENSITIVITY_H / ORBIT_SENSITIVITY_V config
- P1: Calibrate trackpad X_MAX / Y_MAX on hardware; verify pan/zoom feel
- P2: Support Presentation documents (kPresentationDocumentObject=12293)
- P2: Optional momentum/inertia after TrackPoint release
- P2: Tray notification showing connected/disconnected status
- P3: Twist-to-rotate (Y-axis orbit) via 2-finger angle — depends on noise level
- P3: 3DConnexion virtual HID emulation (full 6DOF, needs kernel driver)
