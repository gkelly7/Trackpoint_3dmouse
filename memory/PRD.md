# TrackPoint → Inventor 3D Mouse Emulator

## Problem Statement
User wants to use the ThinkPad TrackPoint as a cursor-independent 3D mouse
while working in Autodesk Inventor 2024, without relying on cursor movement.
Orbit on bare TrackPoint movement; pan with center button held; zoom via scroll wheel.

## Architecture  (current — single unified script)
- **trackpad_gestures.py** — Main script. Handles everything:
  - Raw WPT HID input via `RegisterRawInputDevices` (one-finger orbit, two-finger pan/zoom/twist)
  - TrackPoint UDP listener on port 9999 (orbit + pan from AHK packets)
  - Autodesk Inventor COM camera manipulation (`win32com.client.GetActiveObject`)
  - `WH_MOUSE_LL` hook to suppress cursor movement (1-finger orbit) and scroll events (2-finger gestures) inside Inventor
  - Mode-switching via 2-byte AHK UDP control packets `[0xFF, 0x01/0x00]`
- **trackpoint_3dmouse.ahk** — AHK v2 script. Keyboard/TrackPoint hooks, UI overlays, UDP sender. Sends 9-byte move packets and 2-byte mode packets to Python.
- **inventor_camera_server.py** — OBSOLETE. All logic merged into trackpad_gestures.py.

## IPC Protocol
9-byte UDP move packet (AHK → Python):
  bytes 0-3: x delta  (int32, little-endian)
  bytes 4-7: y delta  (int32, little-endian)
  byte    8: mode     (0=orbit, 1=pan)

2-byte UDP mode packet (AHK → Python):
  byte 0: 0xFF (sentinel)
  byte 1: 0x01 = Desk Mode (3D gestures ON), 0x00 = Couch/Kid Mode (gestures OFF)

## Modes
- Desk (default): TrackPoint = 3D camera in Inventor / normal mouse elsewhere
- Couch (center+left toggle): Trackpad acts as normal mouse, 3D gestures disabled
- Kids (Ctrl+Alt+K): Everything off, only Esc works
- Ctrl+Alt+M: AHK kill-switch toggles trackpad driver on/off

## Implemented
- 2025-01: AHK UDP socket init (WSA), Send3DMove(), modified OnTrackpointMove()
- 2025-01: OnCenterBtn patched to suppress Shift/Home in Inventor
- 2025-01: Python camera server with orbit + pan math
- 2025-01: Auto-launch Python server from AHK; auto-kill on exit
- 2025-01: SETUP.md with prerequisites, VID/PID instructions, sensitivity tuning
- 2026-02: Replaced spherical-coord orbit with camera-relative Rodrigues rotation
- 2026-02: Fixed pan up/down reversal
- 2026-02: Multi-instance Inventor support (ROT enumeration, PID matching)
- 2026-02: Fixed elevation mismatch (non-elevated Shell.Application.ShellExecute launch)
- 2026-02: **ARCHITECTURE MERGE** — `inventor_camera_server.py` UDP logic fully merged into
  `trackpad_gestures.py`. Both WPT touchpad and TrackPoint share a single `_cs` camera state.
  `inventor_camera_server.py` is now obsolete.
- 2026-02: Raw WPT HID input (RegisterRawInputDevices) for touchpad gestures
- 2026-02: 1-finger orbit gesture (WH_MOUSE_LL suppresses cursor during orbit)
- 2026-02: 2-finger pan / pinch-zoom (perspective) / twist-to-roll gestures
- 2026-02: WH_MOUSE_LL hook suppresses scroll events during 2-finger gestures
- 2026-02: LIFTOFF_WATCHDOG timer force-releases suppression flags when WPT stops reporting
- 2026-02: AHK Ctrl+Alt+M kill-switch to toggle trackpad driver
- 2026-02: AHK Couch Mode: sends [0xFF, 0x00] UDP packet to disable 3D gestures;
  Desk Mode: sends [0xFF, 0x01]. Normal mouse behaviour restored in Couch/Kid Mode.
- 2026-02: Ortho zoom via WM_MOUSEWHEEL injection attempted and reverted —
  caused flash-back and broke pan/twist in ortho mode. Accepted trade-off:
  **ortho zoom not supported**; pan and twist work in both perspective and ortho.

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
- P1: Tune default sensitivities (PAN_SENSITIVITY, ZOOM_SENSITIVITY, TWIST_SENSITIVITY, ORBIT_*)
- P1: Calibrate trackpad X_MAX / Y_MAX on hardware; verify pan/zoom feel
- P2: Support Presentation documents (kPresentationDocumentObject=12293)
- P2: Optional momentum/inertia after TrackPoint release
- P2: Tray notification showing connected/disconnected status
- P3: Ortho zoom — currently unsupported (cam.ViewScale inaccessible via COM;
  WM_MOUSEWHEEL injection caused flash-back; accepted trade-off)
- P3: 3DConnexion virtual HID emulation (full 6DOF, needs kernel driver)
