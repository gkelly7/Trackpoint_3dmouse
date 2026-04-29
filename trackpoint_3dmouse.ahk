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

; Dead zone for 3D orbit/pan in Inventor.
; Packets where both |x| and |y| are below this value are ignored.
; Lower = more sensitive (lightest touch responds).  0 = no filtering.
; Higher = filters more drift.  Range 0-3, default 1.
global tpDeadzone := 1

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
    if (gPythonPID != 0) {
        ProcessClose(gPythonPID)
        gPythonPID := 0
    }
    if (gUdpSocket != -1 && gUdpSocket != 0) {
        DllCall("ws2_32\closesocket", "Ptr", gUdpSocket)
        DllCall("ws2_32\WSACleanup")
    }
    ToggleTrackpad(true)
    ToggleTouchscreen(true)
}

OnPowerEvent(wParam, lParam, msg, hwnd) {
    global manualOverride, isKidMode
    if (wParam == 0x0004) {
        if (!isKidMode)
            ToggleTrackpad(true)
    }
    else if (wParam == 0x0007 || wParam == 0x0012) {
        manualOverride := true
        ApplyMode()
    }
}

OnSessionChange(wParam, lParam, msg, hwnd) {
    global manualOverride, isKidMode
    if (wParam == 0x7) {
        if (!isKidMode)
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
global isKidMode        := false
global isTouchScreenOn  := false
; --- UDP socket globals ---
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

; -----------------------------------------------------------------------
; _LaunchNonElevated(exe, args, workDir)
;   Launches exe at medium (non-elevated) integrity from an elevated script.
;   Tries three methods in order; returns true if any succeeds.
;
;   WHY: AHK must run elevated for AutoHotInterception raw-input access.
;        Inventor runs at medium integrity and registers in the medium ROT.
;        Windows keeps SEPARATE Running Object Tables per integrity level,
;        so Python must also be medium-integrity to reach Inventor via COM.
;
; Method 1 - ComObjActive("Shell.Application")
;   Gets the RUNNING (non-elevated) explorer shell via GetActiveObject.
;   ShellExecute on that object spawns the child at explorer's level.
;
; Method 2 - CreateProcessWithTokenW using explorer.exe's token
;   Opens explorer.exe's process token, duplicates it as a primary token,
;   then creates the process with that token directly.
;
; Method 3 - SAFER API (SaferCreateLevel / SaferComputeTokenFromLevel)
;   Reduces the current elevated token to SAFER_LEVELID_NORMALUSER and
;   uses CreateProcessAsUserW.  Official Microsoft API for this use case.
; -----------------------------------------------------------------------
_LaunchNonElevated(exe, args, workDir) {

    ; ---- Method 1: active Shell.Application (non-elevated explorer) ----
    try {
        shell := ComObjActive("Shell.Application")
        shell.ShellExecute(exe, args, workDir, "", 0)
        return true
    }

    ; ---- Method 2: steal explorer.exe token + CreateProcessWithTokenW ----
    hwndShell := DllCall("GetShellWindow", "Ptr")
    if hwndShell {
        DllCall("GetWindowThreadProcessId", "Ptr", hwndShell,
                "UInt*", &_shellPID := 0, "UInt")
        if _shellPID {
            ; PROCESS_QUERY_INFORMATION = 0x0400
            _hProc := DllCall("OpenProcess", "UInt", 0x0400, "Int", 0,
                              "UInt", _shellPID, "Ptr")
            if _hProc {
                ; TOKEN_DUPLICATE = 0x0002
                DllCall("OpenProcessToken", "Ptr", _hProc, "UInt", 0x0002,
                        "Ptr*", &_hTok := 0)
                DllCall("CloseHandle", "Ptr", _hProc)
                if _hTok {
                    _hPri := 0
                    ; DuplicateTokenEx: MAXIMUM_ALLOWED, SecurityImpersonation=2, TokenPrimary=1
                    DllCall("advapi32\DuplicateTokenEx",
                            "Ptr", _hTok, "UInt", 0x02000000,
                            "Ptr", 0, "Int", 2, "Int", 1,
                            "Ptr*", &_hPri := 0)
                    DllCall("CloseHandle", "Ptr", _hTok)
                    if _hPri {
                        ; STARTUPINFOW (64-bit: 104 bytes; dwFlags@60, wShowWindow@64)
                        _si := Buffer(104, 0)
                        NumPut("UInt",   104, _si,  0)  ; cb
                        NumPut("UInt",   0x1, _si, 60)  ; dwFlags = STARTF_USESHOWWINDOW
                        NumPut("UShort", 0,   _si, 64)  ; wShowWindow = SW_HIDE
                        _pi := Buffer(24, 0)             ; PROCESS_INFORMATION (64-bit)
                        _cmd := '"' exe '" ' args
                        _ok := DllCall("advapi32\CreateProcessWithTokenW",
                                       "Ptr",  _hPri,
                                       "UInt", 0,
                                       "Ptr",  0,
                                       "Str",  _cmd,
                                       "UInt", 0x08000000,
                                       "Ptr",  0,
                                       "Str",  workDir,
                                       "Ptr",  _si,
                                       "Ptr",  _pi,
                                       "Int")
                        DllCall("CloseHandle", "Ptr", _hPri)
                        if _ok {
                            DllCall("CloseHandle", "Ptr", NumGet(_pi, 0, "Ptr"))
                            DllCall("CloseHandle", "Ptr", NumGet(_pi, 8, "Ptr"))
                            return true
                        }
                    }
                }
            }
        }
    }

    ; ---- Method 3: SAFER API ------------------------------------------------
    _hLevel := 0
    ; SaferCreateLevel(SAFER_SCOPEID_USER=1, SAFER_LEVELID_NORMALUSER=0x20000, open=0)
    if DllCall("advapi32\SaferCreateLevel",
               "UInt", 1, "UInt", 0x20000, "UInt", 0,
               "Ptr*", &_hLevel := 0, "Ptr", 0, "Int") {
        _hSaferTok := 0
        DllCall("advapi32\SaferComputeTokenFromLevel",
                "Ptr", _hLevel, "Ptr", 0, "Ptr*", &_hSaferTok := 0,
                "UInt", 0, "Ptr", 0, "Int")
        DllCall("advapi32\SaferCloseLevel", "Ptr", _hLevel, "Int")
        if _hSaferTok {
            _si2 := Buffer(104, 0)
            NumPut("UInt",   104, _si2,  0)
            NumPut("UInt",   0x1, _si2, 60)
            NumPut("UShort", 0,   _si2, 64)
            _pi2 := Buffer(24, 0)
            _cmd2 := '"' exe '" ' args
            _ok2 := DllCall("advapi32\CreateProcessAsUserW",
                            "Ptr",  _hSaferTok,
                            "Ptr",  0,
                            "Str",  _cmd2,
                            "Ptr",  0, "Ptr", 0,
                            "Int",  0,
                            "UInt", 0x08000000,
                            "Ptr",  0,
                            "Str",  workDir,
                            "Ptr",  _si2,
                            "Ptr",  _pi2,
                            "Int")
            DllCall("CloseHandle", "Ptr", _hSaferTok)
            if _ok2 {
                DllCall("CloseHandle", "Ptr", NumGet(_pi2, 0, "Ptr"))
                DllCall("CloseHandle", "Ptr", NumGet(_pi2, 8, "Ptr"))
                return true
            }
        }
    }

    return false  ; all methods failed; caller falls back to elevated Run
}

_pyScript := A_ScriptDir "\inventor_camera_server.py"
if FileExist(_pyScript) {
    if !_LaunchNonElevated("pyw.exe", '"' _pyScript '"', A_ScriptDir) {
        ; All non-elevated methods failed -- elevated fallback.
        ; Python will log "ELEVATED" and COM to Inventor won't work.
        Run('pyw.exe "' _pyScript '"', A_ScriptDir, "Hide", &gPythonPID)
    }
    ; Non-elevated launches don't return a PID -- find it via WMI after 2 s.
    SetTimer(FindPyServerPID, -2000)
} else {
    MsgBox(
        "inventor_camera_server.py not found.`n" .
        "Place it in the same folder as this AHK script:`n" .
        A_ScriptDir,
        "3D Mouse: missing file", "Icon!"
    )
}

SetTimer(CheckEnvironment, 1000)

; Discover the PID of the Python camera server 2 s after launch (WMI query).
FindPyServerPID() {
    global gPythonPID
    try {
        wmi := ComObjGet("winmgmts:")
        for proc in wmi.ExecQuery(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pyw.exe'") {
            if (proc.CommandLine && InStr(proc.CommandLine, "inventor_camera_server.py")) {
                gPythonPID := proc.ProcessId
                break
            }
        }
    }
}
; --- 4. ENVIRONMENT & STATE MANAGER ---