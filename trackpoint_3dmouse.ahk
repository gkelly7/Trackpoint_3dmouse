#Requires AutoHotkey v2.0
#SingleInstance Force

if not A_IsAdmin {
    try {
        Run('*RunAs "' A_ScriptFullPath '"')
    }
    ExitApp()
}
#Include Lib\AutoHotInterception.ahk

global AHI := AutoHotInterception()

; --- 1. HARDWARE IDS ---
global tpVid := 0x0000  ; UPDATE THIS: Your TrackPoint VID
global tpPid := 0x0000  ; UPDATE THIS: Your TrackPoint PID

global extVid := 0
global extPid := 0
global extId  := 0

global tpId := AHI.GetMouseId(tpVid, tpPid)
if (!tpId) {
    MsgBox("Could not find TrackPoint! Please check your VID and PID.")
    ExitApp()
}

; --- 2. SAFETY HOOKS (SLEEP, WAKE & LOCK FIXES) ---
OnExit(OnScriptExit)
OnMessage(0x0218, OnPowerEvent) ; Windows Power Events
DllCall("Wtsapi32.dll\WTSRegisterSessionNotification", "Ptr", A_ScriptHwnd, "UInt", 0)
OnMessage(0x02B1, OnSessionChange)

OnScriptExit(ExitReason, ExitCode) {
    global gPythonPID, gUdpSocket
    ; ==================== 3D MOUSE ====================
    if (gPythonPID != 0) {
        ProcessClose(gPythonPID)
        gPythonPID := 0
    }
    if (gUdpSocket != -1 && gUdpSocket != 0) {
        DllCall("ws2_32\closesocket", "Ptr", gUdpSocket)
        DllCall("ws2_32\WSACleanup")
    }
    ; ==================================================
    ToggleTrackpad(true)
    ToggleTouchscreen(true)
}

OnPowerEvent(wParam, lParam, msg, hwnd) {
    global manualOverride
    if (wParam == 0x0004) {
        ToggleTrackpad(true)
    }
    else if (wParam == 0x0007 || wParam == 0x0012) {
        manualOverride := true
        ApplyMode()
    }
}

OnSessionChange(wParam, lParam, msg, hwnd) {
    global manualOverride
    if (wParam == 0x7) {
        ToggleTrackpad(true)
    }
    else if (wParam == 0x8) {
        manualOverride := true
        ApplyMode()
    }
}

; --- 3. STATE VARIABLES ---
global isSubscribed    := false
global extSubscribed   := false
global manualOverride  := false
global wasMouseConnected := false

global isShiftDown      := false
global isDragging       := false
global activeDragButton := ""
global centerBtnDownTime := 0
global escSuppressed    := false

; ==================== 3D MOUSE: globals ====================
global gUdpSocket  := 0
global gSockAddr   := 0
global gPythonPID  := 0

; Initialise Windows Sockets
wsaData := Buffer(408, 0)
if DllCall("ws2_32\WSAStartup", "UShort", 0x0202, "Ptr", wsaData) != 0 {
    MsgBox("Failed to initialise Windows Sockets (WSAStartup).")
    ExitApp()
}

; Create UDP socket (AF_INET=2, SOCK_DGRAM=2, IPPROTO_UDP=17)
gUdpSocket := DllCall("ws2_32\socket", "Int", 2, "Int", 2, "Int", 17, "Ptr")
if (gUdpSocket == -1) {
    MsgBox("Failed to create UDP socket.")
    ExitApp()
}

; Build server address structure (SOCKADDR_IN, 16 bytes)
gSockAddr := Buffer(16, 0)
NumPut("UShort", 2,                                                                       gSockAddr, 0)  ; sin_family = AF_INET
NumPut("UShort", DllCall("ws2_32\htons",    "UShort", 9999,      "UShort"),               gSockAddr, 2)  ; sin_port
NumPut("UInt",   DllCall("ws2_32\inet_addr","AStr",   "127.0.0.1","UInt"),                gSockAddr, 4)  ; sin_addr

; Launch the Python camera server (pythonw = no console window)
_pyScript := A_ScriptDir "\inventor_camera_server.py"
if FileExist(_pyScript) {
    Run('pythonw.exe "' _pyScript '"', A_ScriptDir, "Hide", &gPythonPID)
} else {
    MsgBox(
        "inventor_camera_server.py not found.`n" .
        "Place it in the same folder as this AHK script:`n" .
        A_ScriptDir,
        "3D Mouse: missing file", "Icon!"
    )
}
; ===========================================================

SetTimer(CheckEnvironment, 1000)

; --- 4. ENVIRONMENT & STATE MANAGER ---
CheckEnvironment() {
    global manualOverride, wasMouseConnected

    mouseConnected := CheckForExternalMouse()

    if (mouseConnected && !wasMouseConnected) {
        manualOverride := false
        ApplyMode()
    } else if (!mouseConnected && wasMouseConnected) {
        manualOverride := true
        ApplyMode()
    }

    wasMouseConnected := mouseConnected
}

ApplyMode() {
    global manualOverride, isSubscribed, extSubscribed, tpId, extId, extVid, extPid, AHI
    global isDragging, activeDragButton, isShiftDown

    if (manualOverride) {
        ; COUCH MODE
        ToggleTrackpad(true)

        if (isSubscribed) {
            AHI.UnsubscribeMouseMoveRelative(tpId)
            AHI.UnsubscribeMouseButton(tpId, 0)
            AHI.UnsubscribeMouseButton(tpId, 1)
            AHI.UnsubscribeMouseButton(tpId, 2)
            isSubscribed := false

            if (isDragging && activeDragButton != "") {
                Send("{" activeDragButton " Up}")
                isDragging := false
            }
            if (isShiftDown) {
                Send("{Shift Up}")
                isShiftDown := false
            }
        }

        if (extVid && extPid && CheckForExternalMouse()) {
            extId := AHI.GetMouseId(extVid, extPid)
            if (extId && !extSubscribed) {
                AHI.SubscribeMouseMoveRelative(extId, false, OnExtMouseWakeMove)
                AHI.SubscribeMouseButton(extId, 0, false, OnExtMouseWakeBtn)
                extSubscribed := true
            }
        } else {
            extId        := 0
            extSubscribed := false
        }

        ToolTip("Couch Mode: Trackpad ON")
        SetTimer(() => ToolTip(), -1500)
        TraySetIcon("couch.ico", 1, false)

    } else {
        ; DESK MODE
        ToggleTrackpad(false)

        if (!isSubscribed) {
            AHI.SubscribeMouseMoveRelative(tpId, true, OnTrackpointMove)
            AHI.SubscribeMouseButton(tpId, 0, true, OnLeftBtn)
            AHI.SubscribeMouseButton(tpId, 1, true, OnRightBtn)
            AHI.SubscribeMouseButton(tpId, 2, true, OnCenterBtn)
            isSubscribed := true
        }

        if (extId && extSubscribed) {
            AHI.UnsubscribeMouseMoveRelative(extId)
            AHI.UnsubscribeMouseButton(extId, 0)
            extSubscribed := false
        }

        ToolTip("Desk Mode: Trackpad OFF")
        SetTimer(() => ToolTip(), -1500)
        TraySetIcon("3d.ico", 1, false)
    }
}

; --- 5. HARDWARE SCANNER ---
CheckForExternalMouse() {
    global extVid, extPid
    try {
        wmi   := ComObjGet("winmgmts:")
        query := wmi.ExecQuery("SELECT * FROM Win32_PointingDevice")

        for device in query {
            id := device.PNPDeviceID

            if !InStr(id, "ACPI") && !InStr(id, "DRIVERENUM") {
                if RegExMatch(id, "i)VID[_\&]([0-9A-F]{4}).*PID[_\&]([0-9A-F]{4})", &matches) {
                    extVid := Integer("0x" . matches[1])
                    extPid := Integer("0x" . matches[2])
                    return true
                }
            }
        }
    }
    return false
}

; --- 6. TRACKPAD MASTER SWITCH ---
ToggleTrackpad(state) {
    regPath := "HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\PrecisionTouchPad\Status"
    try {
        RegWrite(state ? 1 : 0, "REG_DWORD", regPath, "Enabled")
        SendMessage(0x1A, 0, StrPtr("PrecisionTouchPad"), , "ahk_id 0xFFFF")
    }
}

; --- 7. EXTERNAL MOUSE WAKE LOGIC ---
OnExtMouseWakeMove(x, y) {
    global manualOverride
    if (manualOverride && (Abs(x) > 5 || Abs(y) > 5)) {
        manualOverride := false
        ApplyMode()
    }
}

OnExtMouseWakeBtn(state) {
    global manualOverride
    if (manualOverride && state) {
        manualOverride := false
        ApplyMode()
    }
}

; --- 8. BUTTON REMAPS & OVERRIDE ---
OnLeftBtn(state) {
    global isShiftDown, manualOverride, escSuppressed, isKidMode
    if (isKidMode)
        return
    if (state) {
        if (isShiftDown) {
            escSuppressed := true
            manualOverride := !manualOverride
            ApplyMode()
            return
        }
        Send("{Esc Down}")
    } else {
        if (escSuppressed) {
            escSuppressed := false
            return
        }
        Send("{Esc Up}")
    }
}

OnRightBtn(state) {
    global isKidMode
    if (isKidMode)
        return
    if (state) {
        Send("{Enter Down}")
    } else {
        Send("{Enter Up}")
    }
}

; ==================== 3D MOUSE: modified OnCenterBtn ====================
; When Inventor is active, suppress Shift and Home so they don't interfere
; with the 3D orbit.  isShiftDown still acts as the pan-mode flag.
OnCenterBtn(state) {
    global isShiftDown, centerBtnDownTime, isKidMode
    if (isKidMode)
        return
    if (state) {
        centerBtnDownTime := A_TickCount
        isShiftDown := true
        if !WinActive("ahk_exe Inventor.exe")
            Send("{Shift Down}")
    } else {
        isShiftDown := false
        if WinActive("ahk_exe Inventor.exe") {
            ; No Shift Up / Home sent to Inventor
        } else {
            Send("{Shift Up}")
            if (A_TickCount - centerBtnDownTime < 250)
                Send("{Home}")
        }
    }
}
; ========================================================================

; --- 9. MOVEMENT LOGIC ---

; ==================== 3D MOUSE: UDP send helper ====================
Send3DMove(x, y, mode) {
    global gUdpSocket, gSockAddr
    data := Buffer(9)
    NumPut("Int",   x,    data, 0)  ; bytes 0-3: x delta (signed int32 LE)
    NumPut("Int",   y,    data, 4)  ; bytes 4-7: y delta (signed int32 LE)
    NumPut("UChar", mode, data, 8)  ; byte    8: 0=orbit  1=pan
    DllCall("ws2_32\sendto",
        "Ptr",  gUdpSocket,
        "Ptr",  data,
        "Int",  9,
        "Int",  0,
        "Ptr",  gSockAddr,
        "Int",  16)
}
; ===================================================================

OnTrackpointMove(x, y) {
    global isDragging, activeDragButton, isShiftDown, isKidMode
    if (isKidMode)
        return

    ; ==================== 3D MOUSE ====================
    ; In Inventor desk mode: route deltas to Python server.
    ; Center button held → pan mode (mode=1); otherwise orbit (mode=0).
    ; No cursor movement, no button click — fully cursor-independent.
    if WinActive("ahk_exe Inventor.exe") {
        Send3DMove(x, y, isShiftDown ? 1 : 0)
        return
    }
    ; ==================================================

    if (!isDragging) {
        if WinActive("ahk_exe orca-slicer.exe") {
            if (isShiftDown) {
                Send("{Shift Up}")
                activeDragButton := "LButton"
            } else {
                activeDragButton := "RButton"
            }
        } else {
            activeDragButton := "MButton"
        }
        Send("{" activeDragButton " Down}")
        isDragging := true
    }

    DllCall("mouse_event", "UInt", 0x0001, "Int", x, "Int", y, "UInt", 0, "UPtr", 0)

    if (isShiftDown) {
        MouseGetPos(&mouseX, &mouseY)
        if (mouseX < 50 || mouseX > A_ScreenWidth - 50 || mouseY < 50 || mouseY > A_ScreenHeight - 50) {
            DllCall("SetCursorPos", "int", A_ScreenWidth/2, "int", A_ScreenHeight/2)
        }
    }
    SetTimer(StopMovement, -100)
}

StopMovement() {
    global isDragging, activeDragButton, isShiftDown
    if (isDragging) {
        Send("{" activeDragButton " Up}")
        isDragging := false
        if (WinActive("ahk_exe orca-slicer.exe") && isShiftDown) {
            Send("{Shift Down}")
        }
    }
}

; --- 10. TOUCHSCREEN MANAGER ---
global isTouchScreenOn := false
ToggleTouchscreen(false) ; Force off on script startup

^!t:: {
    global isTouchScreenOn
    isTouchScreenOn := !isTouchScreenOn
    ToggleTouchscreen(isTouchScreenOn)
    ToolTip("Touchscreen: " . (isTouchScreenOn ? "ON" : "OFF"))
    SetTimer(() => ToolTip(), -2000)
}

ToggleTouchscreen(state) {
    action     := state ? "Enable" : "Disable"
    targetName := "*touchskærm*"
    psCmd      := "Get-PnpDevice -FriendlyName '" targetName "' | " action "-PnpDevice -Confirm:$false"
    RunWait('powershell.exe -NoProfile -WindowStyle Hidden -Command "' psCmd '"', , "Hide")
}

; --- 11. KID MODE ---
global isKidMode := false
global kidHook   := InputHook("V0")
kidHook.KeyOpt("{All}", "S")
kidHook.KeyOpt("{Esc}", "E")
kidHook.OnEnd := ExitKidMode

^!k:: {
    global isKidMode, kidHook
    if (isKidMode)
        return

    isKidMode := true
    ToolTip("KID MODE ACTIVE`nPress ESC to exit")
    SetTimer(() => ToolTip(), -3000)

    ToggleTouchscreen(false)
    kidHook.Start()
    TraySetIcon("kid.ico", 1, false)
}

ExitKidMode(hook, *) {
    global isKidMode, isTouchScreenOn
    isKidMode := false

    ApplyMode()
    ToggleTouchscreen(isTouchScreenOn)

    ToolTip("Kid Mode Deactivated")
    SetTimer(() => ToolTip(), -2000)
}
