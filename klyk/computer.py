"""
CoreGraphics-based OS-level input synthesis via ctypes.
All coordinates are in logical points (matches CGEvent coordinate space).
"""

import asyncio
import atexit
import ctypes
import ctypes.util
import logging
import os
import subprocess
import threading
import time
import time

from .keycodes import parse_key_combo, char_to_keycode, MODIFIER_FLAGS

# ---------------------------------------------------------------------------
# Framework loading
# ---------------------------------------------------------------------------

_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
_appserv = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))

log = logging.getLogger("klyk.computer")

# ---------------------------------------------------------------------------
# CGPoint / CGSize structs
# ---------------------------------------------------------------------------

class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


# ---------------------------------------------------------------------------
# Function signatures — CoreGraphics input
# ---------------------------------------------------------------------------

_cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
_cg.CGEventCreateMouseEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32,
]
_cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_cg.CGEventCreateKeyboardEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool,
]
_cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
_cg.CGEventCreateScrollWheelEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
]
_cg.CGEventPost.restype = None
_cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_cg.CGEventPostToPid.restype = None
_cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
_cg.CGEventSetFlags.restype = None
_cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cg.CGEventSetIntegerValueField.restype = None
_cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64]
_cg.CGEventKeyboardSetUnicodeString.restype = None
_cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]

# ---------------------------------------------------------------------------
# Function signatures — CoreFoundation
# ---------------------------------------------------------------------------

_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFRetain.restype = ctypes.c_void_p
_cf.CFRetain.argtypes = [ctypes.c_void_p]
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFCopyDescription.restype = ctypes.c_void_p
_cf.CFCopyDescription.argtypes = [ctypes.c_void_p]
_cf.CFGetTypeID.restype = ctypes.c_ulong
_cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
_cf.CFStringGetTypeID.restype = ctypes.c_ulong
# CFBoolean / CFNumber type checks + value extraction, so AX scalar values are
# returned as clean "true"/"false"/"42" rather than raw "<CFBoolean …>{value=…}"
# debug descriptions (CFCopyDescription).
_cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
_cf.CFBooleanGetTypeID.argtypes = []
_cf.CFBooleanGetValue.restype = ctypes.c_bool
_cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
_cf.CFNumberGetTypeID.restype = ctypes.c_ulong
_cf.CFNumberGetTypeID.argtypes = []
_cf.CFNumberGetValue.restype = ctypes.c_bool
_cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
_kCFNumberDoubleType = 13
# kCFBooleanTrue — for setting boolean AX attributes (e.g. AXFocused) to true.
_kCFBooleanTrue = ctypes.c_void_p.in_dll(_cf, "kCFBooleanTrue")
_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_cf.CFArrayCreate.restype = ctypes.c_void_p
_cf.CFArrayCreate.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p,
]
_cf.CFEqual.restype = ctypes.c_bool
_cf.CFEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

# ---------------------------------------------------------------------------
# Function signatures — ApplicationServices / AX + ProcessManager
# ---------------------------------------------------------------------------

_appserv.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_appserv.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
_appserv.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
_appserv.AXUIElementCreateSystemWide.argtypes = []
_appserv.AXUIElementCopyElementAtPosition.restype = ctypes.c_int32
_appserv.AXUIElementCopyElementAtPosition.argtypes = [
    ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.POINTER(ctypes.c_void_p),
]
_appserv.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
_appserv.AXUIElementCopyAttributeValue.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
# Batched-attr read — the single biggest AX-walk optimisation. One IPC
# per element instead of N. Apple docs: "Returns the values of multiple
# attributes in the array. If options=0, failed reads return an
# AXValueRef of type kAXValueAXErrorType so the caller can still get
# the rest of the values."
_appserv.AXUIElementCopyMultipleAttributeValues.restype = ctypes.c_int32
_appserv.AXUIElementCopyMultipleAttributeValues.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p),
]
# Bound worst-case per-IPC latency — Finder occasionally takes 100s of
# ms to respond to a single AX query under load. Without this, one slow
# element blocks the entire walker.
_appserv.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
_appserv.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
_appserv.AXUIElementCreateApplication.restype = ctypes.c_void_p
_appserv.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
_appserv.AXValueGetType.restype = ctypes.c_uint32
_appserv.AXValueGetType.argtypes = [ctypes.c_void_p]
_appserv.AXValueGetValue.restype = ctypes.c_bool
_appserv.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_appserv.AXValueCreate.restype = ctypes.c_void_p
_appserv.AXValueCreate.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_appserv.AXUIElementSetAttributeValue.restype = ctypes.c_int32
_appserv.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
# Settable check — needed so we don't trigger AXSetValue on read-only fields
# and silently no-op (the perform-set returns 0 in some apps even when the
# attribute isn't writable). Used by ax_set_value_at to decide whether the
# AX-write fast path is safe before falling back to click+paste.
_appserv.AXUIElementIsAttributeSettable.restype = ctypes.c_int32
_appserv.AXUIElementIsAttributeSettable.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool),
]
_appserv.AXUIElementPerformAction.restype = ctypes.c_int32
_appserv.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_appserv.AXUIElementCopyActionNames.restype = ctypes.c_int32
_appserv.AXUIElementCopyActionNames.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

# ProcessSerialNumber struct (Carbon Process Manager)
class _PSN(ctypes.Structure):
    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]

_appserv.GetProcessForPID.restype = ctypes.c_int32
_appserv.GetProcessForPID.argtypes = [ctypes.c_int32, ctypes.POINTER(_PSN)]
_appserv.SetFrontProcessWithOptions.restype = ctypes.c_int32
_appserv.SetFrontProcessWithOptions.argtypes = [ctypes.POINTER(_PSN), ctypes.c_uint32]

# ---------------------------------------------------------------------------
# CGEvent constants
# ---------------------------------------------------------------------------

kCGHIDEventTap = 0
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventKeyDown = 10
kCGEventKeyUp = 11
kCGEventScrollWheel = 22
kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGScrollEventUnitLine = 1
kCGMouseEventClickState = 1
kCGScrollWheelEventDeltaAxis2 = 12
kCFStringEncodingUTF8 = 0x08000100

# ProcessManager constants
_kSetFrontProcessFrontWindowOnly = 2  # bring front window only, not all windows

# AX value type constants
kAXValueCGPointType = 1
kAXValueCGSizeType  = 2
# kAXValueAXErrorType wraps a per-attribute lookup failure inside a
# CopyMultipleAttributeValues result. Distinguished from real values
# via AXValueGetType.
kAXValueAXErrorType = 5

# Event tap constants
kCGSessionEventTap       = 1
kCGHeadInsertEventTap    = 0
kCGEventTapOptionListenOnly = 1
kCGKeyboardEventKeycode  = 9

# ---------------------------------------------------------------------------
# Function signatures — event tap and flags
# ---------------------------------------------------------------------------

_cg.CGEventGetIntegerValueField.restype  = ctypes.c_int64
_cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_int32]
_cg.CGEventGetFlags.restype              = ctypes.c_uint64
_cg.CGEventGetFlags.argtypes             = [ctypes.c_void_p]
_cg.CGEventTapCreate.restype             = ctypes.c_void_p
_cg.CGEventTapCreate.argtypes            = [
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
]
_cf.CFMachPortCreateRunLoopSource.restype  = ctypes.c_void_p
_cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
_cf.CFRunLoopAddSource.restype             = None
_cf.CFRunLoopAddSource.argtypes            = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_cf.CFRunLoopGetCurrent.restype            = ctypes.c_void_p
_cf.CFRunLoopGetCurrent.argtypes           = []
_cf.CFRunLoopRun.restype                   = None
_cf.CFRunLoopRun.argtypes                  = []

# ---------------------------------------------------------------------------
# Global input lock — cursor is a shared OS resource
# ---------------------------------------------------------------------------

_input_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Emergency stop — Cmd+Shift+Escape halts all input synthesis globally
# ---------------------------------------------------------------------------

_EMERGENCY_STOP_KEYCODE = 53
_EMERGENCY_STOP_FLAGS   = 0x120000  # Cmd (0x100000) | Shift (0x020000)

# A latch, not a counter: once the user fires the chord, ALL input stays blocked
# until the user clears it by pressing the chord again. The agent cannot clear it
# (the resume tool only reports status), so a hijacked or prompt-injected agent
# can't un-pause a stop the user just triggered.
_stop_engaged     = [False]
_last_chord_t     = [0.0]
_CHORD_DEBOUNCE_S = 0.6   # ignore key-repeat / panic double-taps within this window
_stop_lock        = threading.Lock()


class EmergencyStop(RuntimeError):
    pass


def _check_stop() -> None:
    with _stop_lock:
        if not _stop_engaged[0]:
            return
    raise EmergencyStop(
        "Emergency stop is ACTIVE — all Klyk input is blocked. It can be cleared "
        "ONLY by the user pressing Cmd+Shift+Escape again; the resume tool cannot "
        "clear it. Tell the user to press the chord to resume."
    )


def emergency_stop_active() -> bool:
    with _stop_lock:
        return _stop_engaged[0]


def reset_emergency_stop() -> None:
    # Internal clear, used only by the physical-chord toggle — never reachable from a
    # tool call. Kept for tests / programmatic reset.
    with _stop_lock:
        _stop_engaged[0] = False
        _last_chord_t[0] = 0.0


def _toggle_stop_from_chord():
    """Toggle the latch from a physical Cmd+Shift+Escape press. Debounced so a
    key-repeat or a panicked double-tap can't immediately flip it back. Returns the
    new engaged state, or None when the press was debounced."""
    now = time.monotonic()
    with _stop_lock:
        if now - _last_chord_t[0] < _CHORD_DEBOUNCE_S:
            return None
        _last_chord_t[0] = now
        _stop_engaged[0] = not _stop_engaged[0]
        return _stop_engaged[0]


_TAP_CB_TYPE = ctypes.CFUNCTYPE(
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_void_p,
)


def _make_stop_callback():
    def _cb(proxy, etype, event, refcon):
        try:
            if etype == kCGEventKeyDown:
                kc = _cg.CGEventGetIntegerValueField(
                    ctypes.c_void_p(event), kCGKeyboardEventKeycode
                )
                fl = _cg.CGEventGetFlags(ctypes.c_void_p(event))
                if kc == _EMERGENCY_STOP_KEYCODE and (fl & _EMERGENCY_STOP_FLAGS) == _EMERGENCY_STOP_FLAGS:
                    engaged = _toggle_stop_from_chord()
                    if engaged is True:
                        log.warning("Emergency stop ENGAGED (Cmd+Shift+Escape) — all input blocked until you press the chord again")
                    elif engaged is False:
                        log.warning("Emergency stop CLEARED (Cmd+Shift+Escape) — input re-enabled")
        except Exception:
            pass
        return event
    return _TAP_CB_TYPE(_cb)


_tap_callback = _make_stop_callback()


def _start_emergency_stop_tap() -> None:
    def _run():
        try:
            common_modes = ctypes.c_void_p.in_dll(_cf, "kCFRunLoopCommonModes").value
            tap = _cg.CGEventTapCreate(
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
                ctypes.c_uint64(1 << kCGEventKeyDown),
                _tap_callback,
                None,
            )
            if not tap:
                log.warning("Emergency stop tap could not be created (Accessibility permission required)")
                return
            src = _cf.CFMachPortCreateRunLoopSource(None, ctypes.c_void_p(tap), 0)
            rl  = _cf.CFRunLoopGetCurrent()
            _cf.CFRunLoopAddSource(ctypes.c_void_p(rl), ctypes.c_void_p(src), common_modes)
            log.info("Emergency stop tap active — Cmd+Shift+Escape will halt all input")
            _cf.CFRunLoopRun()
        except Exception as e:
            log.warning(f"Emergency stop tap error: {e}")

    threading.Thread(target=_run, daemon=True, name="klyk-stop").start()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post(event_ptr: int) -> None:
    _cg.CGEventPost(kCGHIDEventTap, ctypes.c_void_p(event_ptr))
    _cf.CFRelease(ctypes.c_void_p(event_ptr))


def _post_to_pid(pid: int, event_ptr: int) -> None:
    """Post keyboard event directly to a process — no window activation needed."""
    _cg.CGEventPostToPid(ctypes.c_int32(pid), ctypes.c_void_p(event_ptr))
    _cf.CFRelease(ctypes.c_void_p(event_ptr))


def _cfstr_to_py(cf_str: int) -> str:
    if not cf_str:
        return ""
    buf = ctypes.create_string_buffer(2048)
    ok = _cf.CFStringGetCString(ctypes.c_void_p(cf_str), buf, 2048, kCFStringEncodingUTF8)
    return buf.value.decode("utf-8", errors="replace") if ok else ""


def _cftype_to_str(val_ref: int) -> str:
    if not val_ref:
        return ""
    string_tid = _cf.CFStringGetTypeID()
    val_tid = _cf.CFGetTypeID(ctypes.c_void_p(val_ref))
    if val_tid == string_tid:
        return _cfstr_to_py(val_ref)
    # Scalar AX values (e.g. AXValue of a checkbox/toggle = bool, a stepper =
    # number) — return a clean primitive string, not the CFCopyDescription repr.
    if val_tid == _cf.CFBooleanGetTypeID():
        return "true" if _cf.CFBooleanGetValue(ctypes.c_void_p(val_ref)) else "false"
    if val_tid == _cf.CFNumberGetTypeID():
        out = ctypes.c_double(0.0)
        if _cf.CFNumberGetValue(ctypes.c_void_p(val_ref), _kCFNumberDoubleType, ctypes.byref(out)):
            v = out.value
            return str(int(v)) if v == int(v) else repr(v)
        return ""
    desc = _cf.CFCopyDescription(ctypes.c_void_p(val_ref))
    if desc:
        result = _cfstr_to_py(desc)
        _cf.CFRelease(ctypes.c_void_p(desc))
        return result
    return ""


def _ax_attribute_at(x: float, y: float, attribute_bytes: bytes) -> str | None:
    try:
        sys_elem = _appserv.AXUIElementCreateSystemWide()
        if not sys_elem:
            return None

        elem_ref = ctypes.c_void_p(0)
        err = _appserv.AXUIElementCopyElementAtPosition(
            ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref)
        )
        _cf.CFRelease(ctypes.c_void_p(sys_elem))
        if err != 0 or not elem_ref.value:
            return None

        attr_key = _cf.CFStringCreateWithCString(None, attribute_bytes, kCFStringEncodingUTF8)
        val_ref = ctypes.c_void_p(0)
        err = _appserv.AXUIElementCopyAttributeValue(
            elem_ref, ctypes.c_void_p(attr_key), ctypes.byref(val_ref)
        )
        _cf.CFRelease(ctypes.c_void_p(attr_key))
        _cf.CFRelease(elem_ref)

        if err != 0 or not val_ref.value:
            return None

        result = _cftype_to_str(val_ref.value)
        _cf.CFRelease(val_ref)
        return result if result else None

    except Exception:
        return None


def _press_key_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    post = (lambda ev: _post_to_pid(pid, ev)) if pid else _post
    ev_down = _cg.CGEventCreateKeyboardEvent(None, keycode, True)
    if flags:
        _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), flags)
    post(ev_down)
    time.sleep(0.005)
    ev_up = _cg.CGEventCreateKeyboardEvent(None, keycode, False)
    if flags:
        _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), flags)
    post(ev_up)


def _key_down_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    """Post a single keydown — no matching keyup. Used by hold_key to press
    the key at the start of the hold; the hold loop reposts dragged-style
    keydowns to keep auto-repeat alive in apps that listen for repeats."""
    post = (lambda ev: _post_to_pid(pid, ev)) if pid else _post
    ev_down = _cg.CGEventCreateKeyboardEvent(None, keycode, True)
    if flags:
        _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), flags)
    post(ev_down)


def _key_up_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    """Post a single keyup — pairs with _key_down_sync at the end of a hold."""
    post = (lambda ev: _post_to_pid(pid, ev)) if pid else _post
    ev_up = _cg.CGEventCreateKeyboardEvent(None, keycode, False)
    if flags:
        _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), flags)
    post(ev_up)


def _paste_sync(pid: int | None = None) -> None:
    post = (lambda ev: _post_to_pid(pid, ev)) if pid else _post
    # Resolve V against the active layout so Cmd+V works regardless of
    # whether the V key sits at the US-QWERTY position (kc 9).
    v_keycode, _ = char_to_keycode("v")
    if v_keycode is None:
        v_keycode = 9  # last-ditch fallback
    cmd_flag = MODIFIER_FLAGS["cmd"]
    ev_down = _cg.CGEventCreateKeyboardEvent(None, v_keycode, True)
    _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), cmd_flag)
    post(ev_down)
    time.sleep(0.005)
    ev_up = _cg.CGEventCreateKeyboardEvent(None, v_keycode, False)
    _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), cmd_flag)
    post(ev_up)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def check_accessibility() -> None:
    trusted = _appserv.AXIsProcessTrustedWithOptions(None)
    if not trusted:
        raise RuntimeError(
            "klyk needs Accessibility permission to read the AX tree and post "
            "keyboard events. Grant it:\n"
            "  System Settings → Privacy & Security → Accessibility\n"
            "  Add your terminal app (Ghostty, Terminal, iTerm2, etc.), toggle ON.\n"
            "Then `klyk doctor` to verify, and restart your MCP client."
        )


# ---------------------------------------------------------------------------
# App activation — native ProcessManager (~5ms vs ~450ms for osascript)
# ---------------------------------------------------------------------------

async def activate_app(pid: int) -> None:
    """Bring app window to front using native ProcessManager API. Falls back to osascript if unavailable."""
    try:
        psn = _PSN()
        err = _appserv.GetProcessForPID(pid, ctypes.byref(psn))
        if err == 0:
            _appserv.SetFrontProcessWithOptions(ctypes.byref(psn), _kSetFrontProcessFrontWindowOnly)
            await asyncio.sleep(0.005)
            return
    except Exception:
        pass
    # Fallback: shortened osascript (no AXRaise, just frontmost)
    script = f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
    )
    await asyncio.sleep(0.05)


def is_frontmost_app(pid: int) -> bool:
    """
    True if `pid` owns the topmost user-level on-screen window — the
    WindowServer's notion of the active app. Used by the seamless-mode
    click handlers to decide whether SkyLight delivery can succeed.

    Implementation reads CGWindowList live each call rather than
    NSWorkspace.frontmostApplication(). NSWorkspace updates via
    distributed notifications that require a pumped AppKit main run
    loop; klyk's MCP server pumps asyncio, not AppKit, so the cached
    value goes stale within seconds of a Cmd-Tab. CGWindowList is
    WindowServer-live and matches every other window primitive in klyk.
    Returns False on any error so callers treat "unknown" as "not active".
    """
    try:
        from . import capture
        front = capture.frontmost_pid()
        return front is not None and int(front) == int(pid)
    except Exception as e:
        import logging
        logging.getLogger("klyk.computer").warning(
            f"is_frontmost_app: lookup failed ({type(e).__name__}: {e})"
        )
        return False


# ---------------------------------------------------------------------------
# AX readback — value and label
# ---------------------------------------------------------------------------

def ax_value_at(x: float, y: float, max_retries: int = 4, settle_ms: int = 150) -> str | None:
    """
    Read the AXValue at (x, y). Retries up to max_retries with `settle_ms` between
    attempts to handle SwiftUI @State lag. Returns None when no value is reachable;
    callers that need to distinguish "element has no AXValue" from "AX call kept
    failing" should use ax_value_at_detailed.
    """
    value, _ = ax_value_at_detailed(x, y, max_retries=max_retries, settle_ms=settle_ms)
    return value


def ax_value_at_detailed(x: float, y: float, max_retries: int = 4, settle_ms: int = 150) -> tuple[str | None, str]:
    """
    Same as ax_value_at but returns (value, status). Status is one of:
        "ok"         — value was read successfully
        "no_value"   — AX call succeeded but the element exposes no AXValue
        "no_element" — no AX element resolved at the coordinate
    Lets handlers tell agents whether to retry (transient AX failure) or stop
    asking (the element fundamentally doesn't expose a value).
    """
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(settle_ms / 1000)
        try:
            sys_elem = _appserv.AXUIElementCreateSystemWide()
            if not sys_elem:
                continue
            elem_ref = ctypes.c_void_p(0)
            err = _appserv.AXUIElementCopyElementAtPosition(
                ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref)
            )
            _cf.CFRelease(ctypes.c_void_p(sys_elem))
            if err != 0 or not elem_ref.value:
                continue
            try:
                attr_key = _cf.CFStringCreateWithCString(None, b"AXValue", kCFStringEncodingUTF8)
                val_ref = ctypes.c_void_p(0)
                err = _appserv.AXUIElementCopyAttributeValue(
                    elem_ref, ctypes.c_void_p(attr_key), ctypes.byref(val_ref)
                )
                _cf.CFRelease(ctypes.c_void_p(attr_key))
                if err != 0:
                    continue
                if not val_ref.value:
                    # Element resolved, attribute call ok, but no value present.
                    # That's a stable signal — element just doesn't expose AXValue.
                    return (None, "no_value")
                value = _cftype_to_str(val_ref.value)
                _cf.CFRelease(val_ref)
                if value:
                    return (value, "ok")
            finally:
                _cf.CFRelease(elem_ref)
        except Exception:
            continue
    return (None, "no_element")


def ax_perform_action_at(x: float, y: float, action: str) -> dict:
    """
    Resolve the AX element at (x, y) and invoke AXUIElementPerformAction with
    the named action (e.g. AXPress, AXShowMenu, AXIncrement).

    Returns a dict:
      {ok: True,  role, action, status: "performed"}
        on success.
      {ok: False, role, action, available_actions, status: "unsupported" | ...}
        on failure — the available_actions list lets the agent pick a
        supported one without another round-trip.
      {ok: False, action, status: "no_element"}
        when no AX element resolves at the coordinate.

    More reliable than a mouse click for activating controls because it skips
    the click pipeline entirely — useful for elements whose hit area is small,
    elements partially covered by other windows, or accessibility-focused apps
    that respond cleanly to AX but oddly to synthetic clicks.
    """
    action_bytes = action.encode("utf-8")

    sys_elem = _appserv.AXUIElementCreateSystemWide()
    if not sys_elem:
        return {"ok": False, "action": action, "status": "no_element",
                "error": "AXUIElementCreateSystemWide returned NULL"}
    elem_ref = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyElementAtPosition(
        ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref)
    )
    _cf.CFRelease(ctypes.c_void_p(sys_elem))
    if err != 0 or not elem_ref.value:
        return {"ok": False, "action": action, "status": "no_element"}

    try:
        role = _ax_str_attr(int(elem_ref.value), b"AXRole") or "AXUnknownRole"

        # Enumerate supported actions so we can either confirm the requested
        # one is valid (and give a precise unsupported error) or list what's
        # available on failure. AXUIElementCopyActionNames returns a CFArray
        # of CFStringRefs.
        names_ref = ctypes.c_void_p(0)
        _appserv.AXUIElementCopyActionNames(elem_ref, ctypes.byref(names_ref))
        available: list[str] = []
        if names_ref.value:
            count = int(_cf.CFArrayGetCount(names_ref))
            for i in range(count):
                s_ptr = _cf.CFArrayGetValueAtIndex(names_ref, i)
                if s_ptr:
                    s = _cftype_to_str(s_ptr)
                    if s:
                        available.append(s)
            _cf.CFRelease(names_ref)

        if available and action not in available:
            return {
                "ok": False, "role": role, "action": action,
                "status": "unsupported",
                "available_actions": available,
            }

        ok = _ax_perform_action(int(elem_ref.value), action_bytes)
        if not ok:
            return {
                "ok": False, "role": role, "action": action,
                "status": "perform_failed",
                "available_actions": available,
            }
        return {"ok": True, "role": role, "action": action, "status": "performed"}
    finally:
        _cf.CFRelease(elem_ref)


def ax_resolve_and_act(
    x: float,
    y: float,
    action_chain: tuple[str, ...] = ("AXPress", "AXOpen"),
    max_levels_up: int = 2,
) -> dict:
    """
    Resolve the AX element at (x, y) and try each action in action_chain in
    order; if none are supported there, walk up the AX parent chain (up to
    max_levels_up levels) and retry. First action that performs successfully
    wins.

    Why a chain + parent walk: many real elements are decorative wrappers
    around the actionable target. Finder sidebar rows expose AXOpen on the
    AXRow, not the inner AXStaticText; toolbar buttons sometimes expose
    AXPress on the AXButton, sometimes on a wrapping AXGroup. Trying just
    AXPress on the matched element bails on those cases unnecessarily.
    Cap at 2 levels so we don't activate the wrong target by reaching too
    far up (e.g. an enclosing AXOutline's AXPress would open an arbitrary
    row, not the one matched).

    Returns:
      {ok: True, role, action, level, status: "performed"}
        on success. `level` is one of "element", "parent_1", "parent_2" so
        the caller can surface where the action ended up landing.
      {ok: False, role, action: None, available_actions: {element: [...],
       parents: [...]}, status: "unsupported"} when no chain entry matched
        at any walked level.
      {ok: False, action: None, status: "no_element"} when no AX element
        resolves at the coordinate.
    """
    sys_elem = _appserv.AXUIElementCreateSystemWide()
    if not sys_elem:
        return {"ok": False, "action": None, "status": "no_element",
                "error": "AXUIElementCreateSystemWide returned NULL"}
    elem_ref = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyElementAtPosition(
        ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref)
    )
    _cf.CFRelease(ctypes.c_void_p(sys_elem))
    if err != 0 or not elem_ref.value:
        return {"ok": False, "action": None, "status": "no_element"}

    def _available(eptr: int) -> list[str]:
        names_ref = ctypes.c_void_p(0)
        _appserv.AXUIElementCopyActionNames(ctypes.c_void_p(eptr), ctypes.byref(names_ref))
        out: list[str] = []
        if names_ref.value:
            try:
                count = int(_cf.CFArrayGetCount(names_ref))
                for i in range(count):
                    s_ptr = _cf.CFArrayGetValueAtIndex(names_ref, i)
                    if s_ptr:
                        s = _cftype_to_str(s_ptr)
                        if s:
                            out.append(s)
            finally:
                _cf.CFRelease(names_ref)
        return out

    def _try_chain(eptr: int) -> tuple[str | None, list[str]]:
        avail = _available(eptr)
        for action in action_chain:
            if action not in avail:
                continue
            if _ax_perform_action(eptr, action.encode("utf-8")):
                return action, avail
            # supported but perform_failed — keep trying the rest of the chain
        return None, avail

    # Track parent refs we own so we can release them in `finally`.
    parent_chain: list[int] = []
    try:
        role_elem = _ax_str_attr(int(elem_ref.value), b"AXRole") or "AXUnknownRole"
        action, avail_elem = _try_chain(int(elem_ref.value))
        if action:
            return {"ok": True, "role": role_elem, "action": action,
                    "level": "element", "status": "performed"}

        parents_avail: list[dict] = []
        current_ptr = int(elem_ref.value)
        for level_idx in range(1, max_levels_up + 1):
            parent_ptr = _ax_read_attr_ptr(current_ptr, b"AXParent")
            if not parent_ptr:
                break
            parent_chain.append(parent_ptr)
            role_parent = _ax_str_attr(parent_ptr, b"AXRole") or "AXUnknownRole"
            action, avail_parent = _try_chain(parent_ptr)
            parents_avail.append({"level": f"parent_{level_idx}",
                                  "role": role_parent,
                                  "available_actions": avail_parent})
            if action:
                return {"ok": True, "role": role_parent, "action": action,
                        "level": f"parent_{level_idx}",
                        "matched_role": role_elem,
                        "status": "performed"}
            current_ptr = parent_ptr

        return {"ok": False, "role": role_elem, "action": None,
                "available_actions": {"element": avail_elem,
                                      "parents": parents_avail},
                "status": "unsupported"}
    finally:
        for p in parent_chain:
            _cf.CFRelease(ctypes.c_void_p(p))
        _cf.CFRelease(elem_ref)


def ax_label_at(x: float, y: float) -> str | None:
    for attr in (b"AXTitle", b"AXDescription", b"AXRoleDescription"):
        label = _ax_attribute_at(x, y, attr)
        if label:
            return label
    return None


_AX_CELL_ATTRS = (b"AXValue", b"AXTitle", b"AXDescription")


def ax_cell_text_at(x: float, y: float) -> str | None:
    """
    Resolve the AX element at screen (x, y) and return the first non-empty
    of (AXValue, AXTitle, AXDescription). Optimised for grid-cell reads:
    one element-resolve IPC plus one batched multi-attribute read, instead
    of the 1+N pattern of ax_value_at / ax_label_at chains. Returns None
    when nothing resolves or every candidate attribute is empty/an AX
    error wrapper.
    """
    sys_elem = _appserv.AXUIElementCreateSystemWide()
    if not sys_elem:
        return None
    elem_ref = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyElementAtPosition(
        ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref),
    )
    _cf.CFRelease(ctypes.c_void_p(sys_elem))
    if err != 0 or not elem_ref.value:
        return None
    try:
        raw = _ax_read_multi(int(elem_ref.value), _AX_CELL_ATTRS)
        if raw is None:
            return None
        try:
            for ptr in raw:
                if not ptr:
                    continue
                s = _cftype_to_str(ptr)
                if s and s.strip():
                    return s.strip()
            return None
        finally:
            for p in raw:
                if p:
                    _cf.CFRelease(ctypes.c_void_p(p))
    finally:
        _cf.CFRelease(elem_ref)


# ---------------------------------------------------------------------------
# AX tree snapshot — full UI element inspection
# ---------------------------------------------------------------------------

def _ax_read_attr_ptr(elem: int, attr: bytes) -> int:
    attr_key = _cf.CFStringCreateWithCString(None, attr, kCFStringEncodingUTF8)
    if not attr_key:
        return 0
    val_ref = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyAttributeValue(
        ctypes.c_void_p(elem), ctypes.c_void_p(attr_key), ctypes.byref(val_ref)
    )
    _cf.CFRelease(ctypes.c_void_p(attr_key))
    return val_ref.value or 0


def _ax_str_attr(elem: int, attr: bytes) -> str:
    ptr = _ax_read_attr_ptr(elem, attr)
    if not ptr:
        return ""
    result = _cftype_to_str(ptr)
    _cf.CFRelease(ctypes.c_void_p(ptr))
    return result


def _ax_cgpoint(elem: int) -> tuple[float, float] | None:
    ptr = _ax_read_attr_ptr(elem, b"AXPosition")
    if not ptr:
        return None
    if _appserv.AXValueGetType(ctypes.c_void_p(ptr)) == kAXValueCGPointType:
        pt = CGPoint(x=0.0, y=0.0)
        _appserv.AXValueGetValue(ctypes.c_void_p(ptr), kAXValueCGPointType, ctypes.byref(pt))
        _cf.CFRelease(ctypes.c_void_p(ptr))
        return pt.x, pt.y
    _cf.CFRelease(ctypes.c_void_p(ptr))
    return None


def _ax_cgsize(elem: int) -> tuple[float, float] | None:
    ptr = _ax_read_attr_ptr(elem, b"AXSize")
    if not ptr:
        return None
    if _appserv.AXValueGetType(ctypes.c_void_p(ptr)) == kAXValueCGSizeType:
        sz = CGSize(width=0.0, height=0.0)
        _appserv.AXValueGetValue(ctypes.c_void_p(ptr), kAXValueCGSizeType, ctypes.byref(sz))
        _cf.CFRelease(ctypes.c_void_p(ptr))
        return sz.width, sz.height
    _cf.CFRelease(ctypes.c_void_p(ptr))
    return None


from .ax_roles import INTERACTIVE_ROLES as _AX_INCLUDE_ROLES


# ---------------------------------------------------------------------------
# AX batched-attribute reads — single IPC per element
# ---------------------------------------------------------------------------
#
# The naive walker did one CopyAttributeValue per attribute: AXRole +
# AXTitle + AXDescription + AXPlaceholderValue + AXValue + AXChildren +
# (on match) AXPosition + AXSize = up to 8 IPC round-trips per element.
# At ~3 ms per round-trip and ~1800 elements in Finder, that's ~45 s.
#
# CopyMultipleAttributeValues delivers all of them in one round-trip and
# brings the per-element cost down to ~3-5 ms. Combined with the
# per-node child cap in _ax_collect below, a Finder click_element walk
# now completes in well under a second.

# CFArray of CFStrings cached per attribute set. Built lazily; lifetime
# of the array AND its CFString members extends to module shutdown.
# The underlying CFArray is built with NULL callbacks (no retain on
# insert), so we keep the CFString refs alive via _ax_attrs_keep below.
_ax_attrs_cache: dict[tuple[bytes, ...], int] = {}
_ax_attrs_keep: list = []


def _get_attrs_array(attrs: tuple[bytes, ...]) -> int:
    cached = _ax_attrs_cache.get(attrs)
    if cached is not None:
        return cached
    cf_strs = [
        _cf.CFStringCreateWithCString(None, a, kCFStringEncodingUTF8)
        for a in attrs
    ]
    ptrs = (ctypes.c_void_p * len(cf_strs))(*cf_strs)
    arr = _cf.CFArrayCreate(None, ptrs, len(cf_strs), None)
    # Hold references so neither the CFStrings nor the C-array of pointers
    # are freed for the rest of the process lifetime.
    _ax_attrs_keep.append(cf_strs)
    _ax_attrs_keep.append(ptrs)
    _ax_attrs_keep.append(arr)
    _ax_attrs_cache[attrs] = arr
    return arr


# The two attribute sets the walker uses on every element. Pulled to
# module level so the CFArray for each is built exactly once.
# AXPlaceholderValue is intentionally absent — it's almost always None
# on labeled UI elements and the extra attr per IPC isn't worth the
# 10-15% latency cost on a 300-element walk. Empty input fields whose
# placeholder is the agent's target are handled by OCR.
_AX_WALK_ATTRS = (
    b"AXRole",
    b"AXTitle",
    b"AXDescription",
    b"AXValue",
    b"AXChildren",
)
_AX_POS_SIZE_ATTRS = (b"AXPosition", b"AXSize")


def _ax_read_multi(elem: int, attrs: tuple[bytes, ...]) -> list | None:
    """
    Read all `attrs` from `elem` in a single AX IPC round-trip. Returns
    a parallel list of CFType pointers (caller owns refs, must CFRelease
    each non-None entry) or None on IPC failure. Per-attribute failures
    are returned as None entries (kAXValueAXErrorType wrappers are
    unwrapped to None for the caller's convenience).
    """
    attrs_arr = _get_attrs_array(attrs)
    values = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyMultipleAttributeValues(
        ctypes.c_void_p(elem), ctypes.c_void_p(attrs_arr), 0, ctypes.byref(values),
    )
    if err != 0 or not values.value:
        return None
    try:
        count = int(_cf.CFArrayGetCount(values))
        result: list = []
        for i in range(count):
            v = _cf.CFArrayGetValueAtIndex(values, i)
            if not v:
                result.append(None)
                continue
            # AXValueGetType returns kAXValueAXErrorType when the slot
            # is an unwrapped per-attr error rather than a real value.
            try:
                t = _appserv.AXValueGetType(ctypes.c_void_p(v))
            except Exception:
                t = 0
            if t == kAXValueAXErrorType:
                result.append(None)
                continue
            _cf.CFRetain(ctypes.c_void_p(v))
            result.append(v)
        return result
    finally:
        _cf.CFRelease(values)


def _decode_pos_size(pos_ptr: int, size_ptr: int) -> tuple[float, float, float, float] | None:
    """Decode an (AXPosition, AXSize) pair already fetched via _ax_read_multi."""
    if not pos_ptr or not size_ptr:
        return None
    if _appserv.AXValueGetType(ctypes.c_void_p(pos_ptr)) != kAXValueCGPointType:
        return None
    if _appserv.AXValueGetType(ctypes.c_void_p(size_ptr)) != kAXValueCGSizeType:
        return None
    pt = CGPoint(x=0.0, y=0.0)
    _appserv.AXValueGetValue(ctypes.c_void_p(pos_ptr), kAXValueCGPointType, ctypes.byref(pt))
    sz = CGSize(width=0.0, height=0.0)
    _appserv.AXValueGetValue(ctypes.c_void_p(size_ptr), kAXValueCGSizeType, ctypes.byref(sz))
    if sz.width <= 0 or sz.height <= 0:
        return None
    return (pt.x, pt.y, sz.width, sz.height)


def _ax_collect(
    elem: int,
    results: list,
    depth: int,
    max_depth: int,
    max_children_per_node: int = 200,
    deadline_ts: float = 0.0,
    max_results: int = 0,
    focused_ref: int = 0,
) -> None:
    """
    Walk `elem` recursively, collecting interactive/labeled descendants.
    Single IPC per element via _ax_read_multi (plus a second for pos/size
    on elements that qualify). A per-node child cap skips huge homogeneous
    collections (Finder Desktop icons, list views); a deadline_ts of >0
    aborts the walk when the wall clock crosses it so the caller never
    overruns its budget.

    `max_results` (default 0 = unlimited) stops the walk when `results`
    reaches that length. Cheap safety net for pathologically heavy trees
    — the agent-facing inspect only surfaces 50 elements anyway, so
    walking 10× that is wasted IPC. Inactive at default; callers opt in.
    """
    if depth > max_depth or (deadline_ts > 0.0 and time.monotonic() > deadline_ts):
        return
    if max_results > 0 and len(results) >= max_results:
        return
    try:
        raw = _ax_read_multi(elem, _AX_WALK_ATTRS)
        if raw is None:
            return
        role_ptr, title_ptr, desc_ptr, value_ptr, children_ptr = raw
        try:
            role = _cftype_to_str(role_ptr) if role_ptr else ""
            if not role:
                return
            label = (
                (_cftype_to_str(title_ptr) if title_ptr else "")
                or (_cftype_to_str(desc_ptr) if desc_ptr else "")
            )
            value = _cftype_to_str(value_ptr) if value_ptr else ""

            if role in _AX_INCLUDE_ROLES or label:
                # Only spend the second IPC for elements that might be
                # collected — saves ~40% IPC on a typical walk where most
                # leaves are non-interactive scaffolding.
                ps_raw = _ax_read_multi(elem, _AX_POS_SIZE_ATTRS)
                if ps_raw is not None:
                    try:
                        decoded = _decode_pos_size(ps_raw[0], ps_raw[1])
                        if decoded:
                            x, y, w, h = decoded
                            entry: dict = {
                                "role": role,
                                "x": int(x + w / 2),
                                "y": int(y + h / 2),
                                "width": int(w),
                                "height": int(h),
                            }
                            if label:
                                entry["label"] = label
                            if value:
                                entry["value"] = value
                            # Mark the element that currently holds keyboard
                            # focus. Fetched once at walk start; compared via
                            # CFEqual which is documented to return true for
                            # the same underlying AX element across queries
                            # even when the pointers differ.
                            if focused_ref and _cf.CFEqual(
                                ctypes.c_void_p(elem), ctypes.c_void_p(focused_ref)
                            ):
                                entry["focused"] = True
                            results.append(entry)
                    finally:
                        for p in ps_raw:
                            if p:
                                _cf.CFRelease(ctypes.c_void_p(p))

            if children_ptr:
                count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                limit = min(count, max_children_per_node)
                for i in range(limit):
                    if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                        break
                    if max_results > 0 and len(results) >= max_results:
                        break
                    child = _cf.CFArrayGetValueAtIndex(
                        ctypes.c_void_p(children_ptr), i,
                    )
                    if child:
                        _ax_collect(
                            child, results, depth + 1, max_depth,
                            max_children_per_node, deadline_ts, max_results,
                            focused_ref,
                        )
        finally:
            for p in raw:
                if p:
                    _cf.CFRelease(ctypes.c_void_p(p))
    except Exception:
        pass


# Per-IPC timeout cap (seconds). Bounds worst-case single-element latency
# so one slow app can't block a full walk. Set on the app-level element
# at the start of every snapshot — AX cascades this to the children
# AXUIElementRefs used inside the same walk.
_AX_MESSAGING_TIMEOUT_SECONDS = 0.05  # 50 ms

# Default per-node child cap for the public ax_snapshot. Skips huge
# homogeneous collections (Finder Desktop full of file icons, browser
# list views with hundreds of rows). Agents who need full coverage can
# pass max_children_per_node larger.
_AX_SNAPSHOT_CHILDREN_CAP = 20


def ax_snapshot(
    pid: int,
    max_depth: int = 30,
    max_children_per_node: int = _AX_SNAPSHOT_CHILDREN_CAP,
    deadline_seconds: float = 0.9,
    max_results: int = 0,
) -> list[dict]:
    """
    Return labeled/interactive UI elements across all windows of the app.
    Each element: {role, label?, value?, x, y, width, height} in screen
    coords. Single-IPC-per-element via batched attribute reads.

    Three budget knobs:
      - `max_children_per_node` (default 20) caps each parent's child
        walk so huge homogeneous collections (Finder Desktop icons,
        browser DOM list rows) don't dominate latency.
      - `deadline_seconds` (default 0.9 s) is a wall-clock cut-off — the
        walker returns whatever it has when it crosses, never overrunning
        the 1 s tool budget even on pathologically slow trees.
      - `max_results` (default 0 = unlimited) — early-terminate the walk
        when the raw collection reaches this count. inspect surfaces only
        50 elements to the agent, so walking many more is wasted IPC on
        heavy trees. Cheap safety net; inert on lean apps.

    Pass `max_children_per_node=500, deadline_seconds=5.0, max_results=0`
    when the agent genuinely needs exhaustive inspection.
    """
    deadline_ts = (
        time.monotonic() + deadline_seconds if deadline_seconds > 0 else 0.0
    )
    try:
        app_ptr = _appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return []
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        results: list[dict] = []
        seen: set[tuple] = set()
        # One extra IPC to fetch the app's currently-focused UI element so
        # _ax_collect can mark it as `focused: True`. AX returns a stable
        # AXUIElementRef even though pointer identity isn't guaranteed —
        # CFEqual handles the cross-query comparison. None on apps with no
        # focused element (rare, e.g. just after launch).
        focused_ref = _ax_read_attr_ptr(app_ptr, b"AXFocusedUIElement") or 0
        try:
            wins_ptr = _ax_read_attr_ptr(app_ptr, b"AXWindows")
            had_windows = False
            if wins_ptr:
                try:
                    count = _cf.CFArrayGetCount(ctypes.c_void_p(wins_ptr))
                    if count > 0:
                        had_windows = True
                    for i in range(count):
                        if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                            break
                        if max_results > 0 and len(results) >= max_results:
                            break
                        win = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(wins_ptr), i)
                        if win:
                            _ax_collect(
                                win, results, 0, max_depth,
                                max_children_per_node, deadline_ts, max_results,
                                focused_ref,
                            )
                finally:
                    _cf.CFRelease(ctypes.c_void_p(wins_ptr))
            # Fallback for windowless apps (Dock, SystemUIServer, etc.): walk
            # AXChildren of the app element directly. Dock items live here,
            # not under AXWindows. Only fires when AXWindows is empty so
            # normal app walks aren't disturbed.
            if not had_windows:
                children_ptr = _ax_read_attr_ptr(app_ptr, b"AXChildren")
                if children_ptr:
                    try:
                        count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                        for i in range(count):
                            if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                                break
                            if max_results > 0 and len(results) >= max_results:
                                break
                            child = _cf.CFArrayGetValueAtIndex(
                                ctypes.c_void_p(children_ptr), i,
                            )
                            if child:
                                _ax_collect(
                                    child, results, 0, max_depth,
                                    max_children_per_node, deadline_ts, max_results,
                                    focused_ref,
                                )
                    finally:
                        _cf.CFRelease(ctypes.c_void_p(children_ptr))
        finally:
            if focused_ref:
                _cf.CFRelease(ctypes.c_void_p(focused_ref))
            _cf.CFRelease(ctypes.c_void_p(app_ptr))

        unique = []
        for e in results:
            key = (e.get("role"), e.get("x"), e.get("y"))
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique
    except Exception:
        return []


_AX_MENU_ITEM_ROLES = {"AXMenuItem"}


def _ax_collect_menu_items(
    elem: int,
    results: list,
    depth: int,
    max_depth: int,
    deadline_ts: float,
) -> None:
    """Recursively collect AXMenuItem entries (label + screen coords) under an
    open AXMenu. Same IPC discipline as _ax_collect but scoped to menu roles."""
    if depth > max_depth or (deadline_ts > 0.0 and time.monotonic() > deadline_ts):
        return
    try:
        raw = _ax_read_multi(elem, _AX_WALK_ATTRS)
        if raw is None:
            return
        role_ptr, title_ptr, desc_ptr, value_ptr, children_ptr = raw
        try:
            role = _cftype_to_str(role_ptr) if role_ptr else ""
            label = (
                (_cftype_to_str(title_ptr) if title_ptr else "")
                or (_cftype_to_str(desc_ptr) if desc_ptr else "")
            )
            if role in _AX_MENU_ITEM_ROLES and label:
                ps_raw = _ax_read_multi(elem, _AX_POS_SIZE_ATTRS)
                if ps_raw is not None:
                    try:
                        decoded = _decode_pos_size(ps_raw[0], ps_raw[1])
                        if decoded:
                            x, y, w, h = decoded
                            results.append({
                                "role": role,
                                "x": int(x + w / 2),
                                "y": int(y + h / 2),
                                "width": int(w),
                                "height": int(h),
                                "label": label,
                            })
                    finally:
                        for p in ps_raw:
                            if p:
                                _cf.CFRelease(ctypes.c_void_p(p))
            if children_ptr:
                count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                for i in range(min(count, 300)):
                    if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                        break
                    child = _cf.CFArrayGetValueAtIndex(
                        ctypes.c_void_p(children_ptr), i,
                    )
                    if child:
                        _ax_collect_menu_items(
                            child, results, depth + 1, max_depth, deadline_ts,
                        )
        finally:
            for p in raw:
                if p:
                    _cf.CFRelease(ctypes.c_void_p(p))
    except Exception:
        pass


def ax_read_open_menu(pid: int, max_depth: int = 8, deadline_seconds: float = 0.5) -> list[dict]:
    """
    Return the items of an OPEN popup menu (e.g. a right-click contextual menu)
    as [{role, label, x, y, width, height}] in screen coords.

    A contextual menu is an `AXMenu` hosted as a direct child of the
    `AXApplication` element — NOT under `AXFocusedWindow` — so the regular
    `ax_snapshot` (which walks `AXWindows`) never sees it. This reader walks the
    app element's `AXChildren`, skips the menu bar (`AXMenuBar`), and collects
    `AXMenuItem`s from any open `AXMenu`. Returns [] when no menu is open.
    """
    deadline_ts = (
        time.monotonic() + deadline_seconds if deadline_seconds > 0 else 0.0
    )
    try:
        app_ptr = _appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return []
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        results: list[dict] = []
        try:
            children_ptr = _ax_read_attr_ptr(app_ptr, b"AXChildren")
            if children_ptr:
                try:
                    count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                    for i in range(count):
                        if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                            break
                        child = _cf.CFArrayGetValueAtIndex(
                            ctypes.c_void_p(children_ptr), i,
                        )
                        if not child:
                            continue
                        # Skip the menu bar so only a genuinely-open popup menu's
                        # items surface (menu-bar submenus are closed/empty here).
                        role_ptr = _ax_read_attr_ptr(child, b"AXRole")
                        role = _cftype_to_str(role_ptr) if role_ptr else ""
                        if role_ptr:
                            _cf.CFRelease(ctypes.c_void_p(role_ptr))
                        if role == "AXMenuBar":
                            continue
                        _ax_collect_menu_items(
                            child, results, 0, max_depth, deadline_ts,
                        )
                finally:
                    _cf.CFRelease(ctypes.c_void_p(children_ptr))
        finally:
            _cf.CFRelease(ctypes.c_void_p(app_ptr))
        seen: set[tuple] = set()
        unique = []
        for e in results:
            key = (e.get("label"), e.get("x"), e.get("y"))
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Save / open panel driving via AX
#
# NSSavePanel / NSOpenPanel keystroke automation is fragile: the panel is a
# modal sheet whose key-event routing depends on the host app being OS-frontmost
# and uncontended, so synthetic keystrokes leak when another app holds focus.
# The accessibility API bypasses the event/focus system entirely — setting the
# filename field's value and pressing the Save button via AX is deterministic
# regardless of focus. (Directory navigation still needs Go-To-Folder, the one
# step with no AX affordance — handled by the caller.)
# ---------------------------------------------------------------------------

def _cfstr(s: str) -> int:
    return _cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)


def _ax_str(elem: int, attr: bytes) -> str:
    p = _ax_read_attr_ptr(elem, attr)
    if not p:
        return ""
    try:
        return _cftype_to_str(p)
    finally:
        _cf.CFRelease(ctypes.c_void_p(p))


def _ax_set_value(elem: int, value: str) -> bool:
    a = _cfstr("AXValue")
    v = _cfstr(value)
    try:
        return _appserv.AXUIElementSetAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(a), ctypes.c_void_p(v)
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))
        if v:
            _cf.CFRelease(ctypes.c_void_p(v))


def _ax_press(elem: int, action: str = "AXPress") -> bool:
    a = _cfstr(action)
    try:
        return _appserv.AXUIElementPerformAction(
            ctypes.c_void_p(elem), ctypes.c_void_p(a)
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))


def ax_set_save_filename(pid: int, filename: str) -> bool:
    """Set the 'Save As:' filename field of an open save panel via AX (no
    keystrokes — focus-independent). The panel is an AXSheet in the app's tree;
    the filename field is the AXTextField immediately following the 'Save As:'
    label (the deep AXTextFields are file-list rows, and one is the 'tag editor'
    — both excluded). Returns True if the value was set."""
    try:
        app = _appserv.AXUIElementCreateApplication(pid)
        if not app:
            return False
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS
            )
        except Exception:
            pass
        state = {"saw_label": False, "done": False}

        def walk(e: int, depth: int, in_sheet: bool) -> None:
            if depth > 16 or state["done"]:
                return
            role = _ax_str(e, b"AXRole")
            in_sheet = in_sheet or role == "AXSheet"
            if in_sheet:
                if role == "AXStaticText":
                    v = _ax_str(e, b"AXValue").strip().rstrip(":").lower()
                    if v == "save as":
                        state["saw_label"] = True
                elif role == "AXTextField":
                    if state["saw_label"] and _ax_str(e, b"AXTitle") != "tag editor":
                        if _ax_set_value(e, filename):
                            state["done"] = True
                        return
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                    for i in range(min(n, 80)):
                        if state["done"]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, depth + 1, in_sheet)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, 0, False)
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
        return state["done"]
    except Exception:
        return False


def _ax_set_focused(elem: int) -> bool:
    """Set AXFocused=true on an element (gives it keyboard focus)."""
    a = _cfstr("AXFocused")
    try:
        return _appserv.AXUIElementSetAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(a), _kCFBooleanTrue
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))


def ax_focus_save_field(pid: int) -> bool:
    """Give keyboard focus to a save/open panel's 'Save As:' field via AX, so the
    panel SHEET (not the document behind it) becomes the key window before we
    send Go-To-Folder keystrokes — without this, those keys leak into the
    document. Same AXSheet walk as ax_set_save_filename; sets AXFocused on the
    field. Returns True if the field was found and focused."""
    try:
        app = _appserv.AXUIElementCreateApplication(pid)
        if not app:
            return False
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS
            )
        except Exception:
            pass
        state = {"saw_label": False, "done": False}

        def walk(e: int, depth: int, in_sheet: bool) -> None:
            if depth > 16 or state["done"]:
                return
            role = _ax_str(e, b"AXRole")
            in_sheet = in_sheet or role == "AXSheet"
            if in_sheet:
                if role == "AXStaticText":
                    v = _ax_str(e, b"AXValue").strip().rstrip(":").lower()
                    if v == "save as":
                        state["saw_label"] = True
                elif role == "AXTextField":
                    if state["saw_label"] and _ax_str(e, b"AXTitle") != "tag editor":
                        _ax_set_focused(e)
                        state["done"] = True
                        return
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                    for i in range(min(n, 80)):
                        if state["done"]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, depth + 1, in_sheet)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, 0, False)
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
        return state["done"]
    except Exception:
        return False


def ax_navigate_save_panel(pid: int, target_dir: str) -> str | None:
    """Navigate an OPEN save/open panel to target_dir via accessibility — fully
    invisible (no cursor, no keystrokes). Selects the sidebar location whose name
    matches the target directory's basename: covers the home folder, Desktop,
    Downloads, iCloud Drive, and any user-added Favourite. Returns the location
    name it landed on (verified against the panel's 'Where' value), or None when
    there is no sidebar match — the caller then reports honestly. Subfolders that
    aren't sidebar entries return None by design: the panel's file browser is a
    virtualized NSBrowser that doesn't expose its cells to AX reliably.

    Why this works where keystrokes don't: the panel is a separate (sandboxed)
    process, so a global Cmd+Shift+G is misrouted to the host app. AX, by
    contrast, bridges into the panel — the same channel that already sets the
    filename and presses Save. Setting AXSelected on a sidebar row navigates it.
    """
    want = os.path.basename(os.path.abspath(os.path.expanduser(target_dir)).rstrip("/"))
    if not want:
        return None
    try:
        app = _appserv.AXUIElementCreateApplication(pid)
        if not app:
            return None
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS
            )
        except Exception:
            pass

        def name_of(e, d=0):
            if d > 5:
                return None
            if _ax_str(e, b"AXRole") == "AXStaticText":
                v = _ax_str(e, b"AXValue")
                if v:
                    return v
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    for i in range(min(_cf.CFArrayGetCount(ctypes.c_void_p(ch)), 20)):
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            r = name_of(k, d + 1)
                            if r:
                                return r
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))
            return None

        def where_value(e, d=0):
            if d > 18:
                return None
            if _ax_str(e, b"AXRole") == "AXPopUpButton":
                lbl = (_ax_str(e, b"AXDescription") or _ax_str(e, b"AXTitle") or "")
                if "where" in lbl.lower():
                    return _ax_str(e, b"AXValue")
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    for i in range(min(_cf.CFArrayGetCount(ctypes.c_void_p(ch)), 130)):
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            r = where_value(k, d + 1)
                            if r:
                                return r
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))
            return None

        found = {"row": None}  # holds a CFRetain'd row ref (survives array release)

        def walk(e, in_sidebar, depth):
            if depth > 16 or found["row"]:
                return
            role = _ax_str(e, b"AXRole")
            if role == "AXOutline":
                in_sidebar = True
            if in_sidebar and role == "AXRow" and name_of(e) == want:
                # Retain — the row is borrowed from a CFArray we release below.
                found["row"] = _cf.CFRetain(ctypes.c_void_p(e))
                return
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    for i in range(min(_cf.CFArrayGetCount(ctypes.c_void_p(ch)), 130)):
                        if found["row"]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, in_sidebar, depth + 1)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, False, 0)
            row = found["row"]
            if not row:
                return None
            a = _cfstr("AXSelected")
            try:
                _appserv.AXUIElementSetAttributeValue(
                    ctypes.c_void_p(row), ctypes.c_void_p(a), _kCFBooleanTrue
                )
            finally:
                if a:
                    _cf.CFRelease(ctypes.c_void_p(a))
            _cf.CFRelease(ctypes.c_void_p(row))
            time.sleep(0.2)  # let the panel commit the location change
            wv = where_value(app) or ""
            if want and (want in wv or wv in want):
                return wv or want
            return None
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
    except Exception:
        return None


def ax_press_panel_button(pid: int, titles: tuple, substring: bool = False) -> str | None:
    """Press a button inside an open sheet (save/open panel or alert) by title,
    via AX (focus-independent). `titles` is tried in order; `substring=True`
    matches when a wanted string is contained in a button title (for variable
    labels like 'Use ".txt"'). Returns the matched button's title, else None."""
    wanted = [t.lower() for t in titles]
    try:
        app = _appserv.AXUIElementCreateApplication(pid)
        if not app:
            return None
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS
            )
        except Exception:
            pass
        state = {"pressed": None}

        def matches(title: str) -> bool:
            t = title.lower()
            if substring:
                return any(w in t for w in wanted)
            return t in wanted

        def walk(e: int, depth: int, in_sheet: bool) -> None:
            if depth > 16 or state["pressed"]:
                return
            role = _ax_str(e, b"AXRole")
            in_sheet = in_sheet or role == "AXSheet"
            if in_sheet and role == "AXButton":
                title = _ax_str(e, b"AXTitle")
                if title and matches(title) and _ax_press(e):
                    state["pressed"] = title
                    return
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                    for i in range(min(n, 80)):
                        if state["pressed"]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, depth + 1, in_sheet)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, 0, False)
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
        return state["pressed"]
    except Exception:
        return None


def _collect_sheet_contents(elem: int) -> tuple:
    """Walk a sheet subtree and return (static_texts, button_titles,
    has_save_as_field)."""
    texts: list[str] = []
    buttons: list[str] = []
    has_save_as = [False]

    def w(x: int, d: int) -> None:
        if d > 12:
            return
        role = _ax_str(x, b"AXRole")
        if role == "AXStaticText":
            v = _ax_str(x, b"AXValue")
            if v:
                texts.append(v)
                if v.strip().rstrip(":").lower() == "save as":
                    has_save_as[0] = True
        elif role == "AXButton":
            t = _ax_str(x, b"AXTitle")
            if t:
                buttons.append(t)
        ch = _ax_read_attr_ptr(x, b"AXChildren")
        if ch:
            try:
                n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                for i in range(min(n, 60)):
                    k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                    if k:
                        w(k, d + 1)
            finally:
                _cf.CFRelease(ctypes.c_void_p(ch))

    w(elem, 0)
    return texts, buttons, has_save_as[0]


def ax_read_alert(pid: int) -> dict | None:
    """Read an alert sheet's message + buttons, if one is open — e.g. the error
    macOS raises when a save is refused ("you don't have permission", "the
    volume is read-only") or a confirmation ("you used the extension .txt …").
    Returns {'text': str, 'buttons': [str]} for the alert, or None. Excludes the
    save/open panel itself (which carries a 'Save As:' field), so the caller can
    tell a real alert apart from the panel and surface — not silently dismiss —
    the reason a save failed."""
    try:
        app = _appserv.AXUIElementCreateApplication(pid)
        if not app:
            return None
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS
            )
        except Exception:
            pass
        found = [None]

        def walk(e: int, d: int) -> None:
            if d > 16 or found[0]:
                return
            role = _ax_str(e, b"AXRole")
            if role == "AXSheet":
                texts, buttons, has_save_as = _collect_sheet_contents(e)
                desc = _ax_str(e, b"AXDescription").lower()
                # An alert: has buttons + a real message, and is NOT the save
                # panel (no 'Save As:' field). 'alert' description is a strong tip.
                msg = " ".join(t for t in texts if len(t) > 2)
                is_alert = bool(buttons) and not has_save_as and (
                    desc == "alert" or any(len(t) > 12 for t in texts)
                )
                if is_alert:
                    found[0] = {"text": msg[:500], "buttons": buttons}
                    return
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                    for i in range(min(n, 80)):
                        if found[0]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, d + 1)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, 0)
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
        return found[0]
    except Exception:
        return None


def _ax_search_by_label(
    elem: int,
    query: str,
    results: list,
    depth: int,
    max_depth: int,
    max_children_per_node: int,
    max_results: int,
    deadline_ts: float,
) -> None:
    """
    Walk `elem` looking for label/value matches against `query` (already
    lowercased). One IPC per visited element; a second IPC per element
    only when the label matches and we need pos/size. Recursion stops
    when results hits max_results, when depth exceeds max_depth, or when
    the wall clock crosses `deadline_ts` — whichever comes first. The
    deadline keeps the worst case bounded even for apps with pathologically
    large or slow AX trees; the caller treats deadline-cut results the
    same as zero matches and falls through to OCR.
    """
    if (
        depth > max_depth
        or len(results) >= max_results
        or time.monotonic() > deadline_ts
    ):
        return
    try:
        raw = _ax_read_multi(elem, _AX_WALK_ATTRS)
        if raw is None:
            return
        role_ptr, title_ptr, desc_ptr, value_ptr, children_ptr = raw
        try:
            role = _cftype_to_str(role_ptr) if role_ptr else ""
            if not role:
                return
            label = (
                (_cftype_to_str(title_ptr) if title_ptr else "")
                or (_cftype_to_str(desc_ptr) if desc_ptr else "")
            )
            value = _cftype_to_str(value_ptr) if value_ptr else ""

            matched = (
                (label and query in label.lower())
                or (value and query in value.lower())
            )
            if matched:
                ps_raw = _ax_read_multi(elem, _AX_POS_SIZE_ATTRS)
                if ps_raw is not None:
                    try:
                        decoded = _decode_pos_size(ps_raw[0], ps_raw[1])
                        if decoded:
                            x, y, w, h = decoded
                            entry: dict = {
                                "role": role,
                                "x": int(x + w / 2),
                                "y": int(y + h / 2),
                                "width": int(w),
                                "height": int(h),
                            }
                            if label:
                                entry["label"] = label
                            if value:
                                entry["value"] = value
                            results.append(entry)
                            if len(results) >= max_results:
                                return
                    finally:
                        for p in ps_raw:
                            if p:
                                _cf.CFRelease(ctypes.c_void_p(p))

            if children_ptr and len(results) < max_results:
                count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                limit = min(count, max_children_per_node)
                for i in range(limit):
                    if (
                        len(results) >= max_results
                        or time.monotonic() > deadline_ts
                    ):
                        break
                    child = _cf.CFArrayGetValueAtIndex(
                        ctypes.c_void_p(children_ptr), i,
                    )
                    if child:
                        _ax_search_by_label(
                            child, query, results, depth + 1, max_depth,
                            max_children_per_node, max_results, deadline_ts,
                        )
        finally:
            for p in raw:
                if p:
                    _cf.CFRelease(ctypes.c_void_p(p))
    except Exception:
        pass


def ax_search_focused(
    pid: int,
    query: str,
    max_depth: int = 30,
    max_children_per_node: int = 20,
    max_results: int = 20,
    deadline_seconds: float = 0.8,
) -> list[dict]:
    """
    Find UI elements in AXFocusedWindow whose label or value contains
    `query` (case-insensitive substring). Returns up to `max_results`
    matches with {role, label, value, x, y, width, height}.

    Designed for click_element's hot path:
      - one batched IPC per visited element via _ax_search_by_label
      - second batched IPC only on match
      - 30-child-per-node cap (sidebars/toolbars/menus comfortable,
        content collections skipped)
      - hard wall-clock deadline (default 0.6 s) — the walker returns
        early with whatever it has rather than blow the 1 s tool budget

    Anything that didn't surface here is OCR's responsibility: visible
    content text in lists, browser viewports, and canvas surfaces is
    where OCR shines and AX walks struggle.
    """
    q = (query or "").lower().strip()
    if not q:
        return []
    deadline_ts = time.monotonic() + deadline_seconds
    try:
        app_ptr = _appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return []
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        results: list[dict] = []
        try:
            focused_ptr = _ax_read_attr_ptr(app_ptr, b"AXFocusedWindow")
            if focused_ptr:
                try:
                    children_ptr = _ax_read_attr_ptr(focused_ptr, b"AXChildren")
                    if children_ptr:
                        try:
                            count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                            limit = min(count, max_children_per_node)
                            for i in range(limit):
                                if (
                                    len(results) >= max_results
                                    or time.monotonic() > deadline_ts
                                ):
                                    break
                                child = _cf.CFArrayGetValueAtIndex(
                                    ctypes.c_void_p(children_ptr), i,
                                )
                                if child:
                                    _ax_search_by_label(
                                        child, q, results, 0, max_depth,
                                        max_children_per_node, max_results,
                                        deadline_ts,
                                    )
                        finally:
                            _cf.CFRelease(ctypes.c_void_p(children_ptr))
                finally:
                    _cf.CFRelease(ctypes.c_void_p(focused_ptr))
        finally:
            _cf.CFRelease(ctypes.c_void_p(app_ptr))
        # Dedup by (role, x, y) — same key the generic walker uses.
        unique: list[dict] = []
        seen: set[tuple] = set()
        for e in results:
            key = (e.get("role"), e.get("x"), e.get("y"))
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique
    except Exception:
        return []


def ax_collect_focused(
    pid: int,
    max_depth: int = 30,
    max_children_per_node: int = 20,
    deadline_seconds: float = 0.8,
) -> list[dict]:
    """
    Element shape matches ax_snapshot; walks only the children of
    AXFocusedWindow with a tight per-node child cap and a wall-clock
    deadline. The click_element label search uses ax_search_focused
    directly — this function exists for callers that genuinely want
    every label/role in the focused window's reachable AX tree (e.g.
    debugging tools) while still respecting the 1 s tool budget.
    """
    deadline_ts = (
        time.monotonic() + deadline_seconds if deadline_seconds > 0 else 0.0
    )
    try:
        app_ptr = _appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return []
        try:
            _appserv.AXUIElementSetMessagingTimeout(
                ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        results: list[dict] = []
        try:
            focused_ptr = _ax_read_attr_ptr(app_ptr, b"AXFocusedWindow")
            if focused_ptr:
                try:
                    children_ptr = _ax_read_attr_ptr(focused_ptr, b"AXChildren")
                    if children_ptr:
                        try:
                            count = _cf.CFArrayGetCount(ctypes.c_void_p(children_ptr))
                            limit = min(count, max_children_per_node)
                            for i in range(limit):
                                if deadline_ts > 0.0 and time.monotonic() > deadline_ts:
                                    break
                                child = _cf.CFArrayGetValueAtIndex(
                                    ctypes.c_void_p(children_ptr), i,
                                )
                                if child:
                                    _ax_collect(
                                        child, results, 0, max_depth,
                                        max_children_per_node, deadline_ts,
                                    )
                        finally:
                            _cf.CFRelease(ctypes.c_void_p(children_ptr))
                finally:
                    _cf.CFRelease(ctypes.c_void_p(focused_ptr))
        finally:
            _cf.CFRelease(ctypes.c_void_p(app_ptr))
        unique = []
        seen: set[tuple] = set()
        for e in results:
            key = (e.get("role"), e.get("x"), e.get("y"))
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique
    except Exception:
        return []


def ax_focused_summary(pid: int) -> dict:
    """
    Cheap post-action snapshot of an app's focused state. Returns
        {"focused": {"label","role","value"} | absent, "window_title": str | absent}
    with empty dict on any failure — caller treats absence as inconclusive.

    Used by the action tools' opt-in `verify=true` flag. Per-app (not
    system-wide) is the right granularity for klyk's default autonomous
    mode: actions don't take OS focus from the user, so system-wide AX
    would report whatever the user is reading instead of what the action
    did. ~9 IPCs total, ~5-15 ms on typical apps.
    """
    out: dict = {}
    try:
        app_ptr = _appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return out
        try:
            try:
                _appserv.AXUIElementSetMessagingTimeout(
                    ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            focused_ptr = _ax_read_attr_ptr(app_ptr, b"AXFocusedUIElement")
            if focused_ptr:
                try:
                    label = (
                        _ax_str_attr(focused_ptr, b"AXTitle")
                        or _ax_str_attr(focused_ptr, b"AXDescription")
                        or _ax_str_attr(focused_ptr, b"AXPlaceholderValue")
                    )
                    role = _ax_str_attr(focused_ptr, b"AXRole")
                    value = _ax_str_attr(focused_ptr, b"AXValue")
                    if label or role or value:
                        out["focused"] = {
                            "label": label[:80],
                            "role": role,
                            "value": value[:120],
                        }
                finally:
                    _cf.CFRelease(ctypes.c_void_p(focused_ptr))
            win_ptr = _ax_read_attr_ptr(app_ptr, b"AXFocusedWindow")
            if win_ptr:
                try:
                    title = _ax_str_attr(win_ptr, b"AXTitle")
                    if title:
                        out["window_title"] = title[:120]
                finally:
                    _cf.CFRelease(ctypes.c_void_p(win_ptr))
        finally:
            _cf.CFRelease(ctypes.c_void_p(app_ptr))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def set_clipboard(text: str) -> None:
    # timeout so a contended/stuck pasteboard server (e.g. behind a modal sheet)
    # fails fast instead of hanging the tool for minutes (was unbounded).
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)


def set_clipboard_image(image_path: str) -> str:
    """Load a PNG file into the system clipboard. Returns the resolved absolute path."""
    path = os.path.abspath(os.path.expanduser(image_path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"image not found: {path}")
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = f'set the clipboard to (read (POSIX file "{escaped}") as «class PNGf»)'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"clipboard image write failed: {err}")
    return path


def get_clipboard() -> str:
    # timeout so a contended/stuck pasteboard server fails fast (was unbounded).
    result = subprocess.run(["pbpaste"], capture_output=True, check=True, timeout=5)
    return result.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Menu bar + window arrangement
# ---------------------------------------------------------------------------

def _ascript_str(s: str) -> str:
    """Escape a string for safe inclusion inside AppleScript double-quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def click_menu(pid: int, path: list[str]) -> None:
    """Click a menu-bar item by path, e.g. ["Tools", "Annotate", "Arrow"]. Min length 2."""
    if len(path) < 2:
        raise ValueError("menu path needs at least the top menu and one item")
    leaf, parent = _ascript_str(path[-1]), _ascript_str(path[-2])
    target = f'menu item "{leaf}" of menu "{parent}"'
    for k in range(len(path) - 2, 0, -1):
        item, in_menu = _ascript_str(path[k]), _ascript_str(path[k - 1])
        target = f'{target} of menu item "{item}" of menu "{in_menu}"'
    target = f"{target} of menu bar 1"
    script = (
        f'tell application "System Events" to tell (first process whose unix id is {pid}) '
        f"to click {target}"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        # AppleScript "Can't get menu item …" → the path was wrong.
        raise RuntimeError(f"click_menu {' > '.join(path)} failed: {err}")


def set_window_bounds(pid: int, x: int, y: int, width: int | None = None, height: int | None = None) -> None:
    """Move (and optionally resize) the frontmost window of a process via System Events."""
    cmd = ["osascript", "-e",
           f'tell application "System Events" to tell (first process whose unix id is {pid}) '
           f"to set position of window 1 to {{{int(x)}, {int(y)}}}"]
    if width is not None and height is not None:
        cmd.extend(["-e",
                    f'tell application "System Events" to tell (first process whose unix id is {pid}) '
                    f"to set size of window 1 to {{{int(width)}, {int(height)}}}"])
    result = subprocess.run(cmd, capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"set_window_bounds failed: {err}")


# ---------------------------------------------------------------------------
# Multi-window: bridge CG window IDs to AX AXWindow refs
# ---------------------------------------------------------------------------
# CG window IDs (CGWindowID) don't appear in the AX API directly. To target a
# specific window for raise/move/resize, we enumerate the app's AXWindows and
# match by position (AXPosition vs CG bounds X/Y). Position is reliable because
# two real windows of the same app cannot share an exact origin in macOS's
# window server. Falls back to size match if position matching fails (e.g.
# during a window animation).

def _ax_window_for_cg_id(pid: int, target_window_id: int, target_x: float, target_y: float, target_w: float, target_h: float, tolerance: float = 4.0) -> int:
    """
    Find the AXWindow ref corresponding to a CG window_id. Strategy, in order:

    1. Position AND size match unique → use it. This disambiguates a fullscreen
       overlay window that shares an origin with a smaller quadrant window
       (e.g. fullscreen at (0,30) 1920x1050 vs. quadrant at (0,30) 960x540).
    2. Position-only match unique → use it (size unavailable / mid-animation).
    3. Size-only match unique → use it (window animating to new position).
    4. Ambiguous → pair by z-order index between the app's CG and AX window
       lists. Both APIs return front-to-back ordering.

    Caller must CFRelease the returned ref. Returns 0 if no match found.
    """
    from . import capture
    app_ptr = _appserv.AXUIElementCreateApplication(pid)
    if not app_ptr:
        return 0
    try:
        wins_ptr = _ax_read_attr_ptr(app_ptr, b"AXWindows")
        if not wins_ptr:
            return 0
        try:
            count = _cf.CFArrayGetCount(ctypes.c_void_p(wins_ptr))

            ax_entries = []
            for i in range(count):
                win = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(wins_ptr), i)
                if not win:
                    continue
                pos = _ax_cgpoint(win)
                size = _ax_cgsize(win)
                ax_entries.append((win, pos, size))

            def _pos_ok(pos) -> bool:
                return bool(pos) and abs(pos[0] - target_x) <= tolerance and abs(pos[1] - target_y) <= tolerance

            def _size_ok(size) -> bool:
                return bool(size) and abs(size[0] - target_w) <= tolerance and abs(size[1] - target_h) <= tolerance

            # Pass 1: position + size both match — strongest signal.
            pos_size_matches = [idx for idx, (_, pos, size) in enumerate(ax_entries) if _pos_ok(pos) and _size_ok(size)]
            if len(pos_size_matches) == 1:
                return _cf.CFRetain(ctypes.c_void_p(ax_entries[pos_size_matches[0]][0])) or 0

            # Pass 2: position only.
            pos_matches = [idx for idx, (_, pos, _) in enumerate(ax_entries) if _pos_ok(pos)]
            if len(pos_matches) == 1:
                return _cf.CFRetain(ctypes.c_void_p(ax_entries[pos_matches[0]][0])) or 0

            # Pass 3: size only — useful if the window is mid-animation to a new origin.
            size_matches = [idx for idx, (_, _, size) in enumerate(ax_entries) if _size_ok(size)]
            if len(size_matches) == 1:
                return _cf.CFRetain(ctypes.c_void_p(ax_entries[size_matches[0]][0])) or 0

            # Pass 4: ambiguous — pair by z-order index (front-to-back) with the
            # CG list. The CG and AX window lists can be filtered/ordered
            # differently (CG drops sub-50px windows; AX doesn't), so a blind
            # index pair can land on the WRONG window. Only trust it if the
            # paired window's geometry actually lines up with the target —
            # otherwise return no-match (0) and let the caller handle it, rather
            # than silently raising/moving the wrong window.
            cg_windows = capture.list_windows_for_pid(pid)
            cg_index = next((i for i, w in enumerate(cg_windows) if w["window_id"] == target_window_id), None)
            if cg_index is not None and cg_index < len(ax_entries):
                _, pos, size = ax_entries[cg_index]
                if _pos_ok(pos) or _size_ok(size):
                    return _cf.CFRetain(ctypes.c_void_p(ax_entries[cg_index][0])) or 0
        finally:
            _cf.CFRelease(ctypes.c_void_p(wins_ptr))
    finally:
        _cf.CFRelease(ctypes.c_void_p(app_ptr))
    return 0


def _ax_set_attr_value(elem: int, attr: bytes, ax_value_ref: int) -> bool:
    """Set an AX attribute (caller owns ax_value_ref). Returns True on success."""
    attr_key = _cf.CFStringCreateWithCString(None, attr, kCFStringEncodingUTF8)
    if not attr_key:
        return False
    try:
        err = _appserv.AXUIElementSetAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(attr_key), ctypes.c_void_p(ax_value_ref)
        )
        return err == 0
    finally:
        _cf.CFRelease(ctypes.c_void_p(attr_key))


def _ax_perform_action(elem: int, action: bytes) -> bool:
    action_key = _cf.CFStringCreateWithCString(None, action, kCFStringEncodingUTF8)
    if not action_key:
        return False
    try:
        err = _appserv.AXUIElementPerformAction(
            ctypes.c_void_p(elem), ctypes.c_void_p(action_key)
        )
        return err == 0
    finally:
        _cf.CFRelease(ctypes.c_void_p(action_key))


def _ax_attr_is_settable(elem: int, attr: bytes) -> bool:
    """Return True iff `attr` on `elem` is writable via AXUIElementSetAttributeValue."""
    attr_key = _cf.CFStringCreateWithCString(None, attr, kCFStringEncodingUTF8)
    if not attr_key:
        return False
    try:
        settable = ctypes.c_bool(False)
        err = _appserv.AXUIElementIsAttributeSettable(
            ctypes.c_void_p(elem), ctypes.c_void_p(attr_key), ctypes.byref(settable)
        )
        return err == 0 and bool(settable.value)
    finally:
        _cf.CFRelease(ctypes.c_void_p(attr_key))


# Native macOS text-input roles whose AXValue we can set directly. Anything
# rooted in an AXWebArea (Chrome / Safari / Electron web view) is excluded
# regardless of role — JS-backed inputs ignore AXSetValue's underlying
# write because the AX attribute is a one-way snapshot, not bound to the
# DOM input's value. For web pages we still use the click+paste path.
_AX_TEXT_INPUT_ROLES = frozenset({
    "AXTextField",
    "AXTextArea",
    "AXSearchField",
    "AXComboBox",
    # SecureTextField intentionally excluded — fall back to paste so
    # the user's clipboard restore logic engages (set_value_at would
    # store the password in the AXValue cache where some screen readers
    # log it).
})


def _ax_is_web_backed(elem: int, max_hops: int = 12) -> bool:
    """
    Walk parent chain looking for AXWebArea. Returns True if any ancestor
    within `max_hops` is a web area. Bounded so a malformed parent loop
    can't hang us — 12 is far more than any realistic native form depth.

    CFRetain/CFRelease are balanced so we don't leak refs on the walk.
    """
    cur_ref = ctypes.c_void_p(elem)
    _cf.CFRetain(cur_ref)  # we will release in the loop / on exit
    try:
        for _ in range(max_hops):
            role = _ax_str_attr(cur_ref.value, b"AXRole") or ""
            if role == "AXWebArea":
                return True
            parent_ptr = _ax_read_attr_ptr(cur_ref.value, b"AXParent")
            _cf.CFRelease(cur_ref)
            if not parent_ptr:
                # Bridge owner of the loop-end ref so the finally is a no-op.
                cur_ref = ctypes.c_void_p(None)
                return False
            cur_ref = ctypes.c_void_p(parent_ptr)
        return False
    finally:
        if cur_ref.value:
            _cf.CFRelease(cur_ref)


def ax_set_value_at(x: float, y: float, text: str) -> dict:
    """
    Try to set the AXValue of the element at (x, y) to `text` — the fully
    invisible alternative to click+Cmd+A+paste for native text inputs.

    Cascade: resolve element → reject if web-area-rooted → reject if role
    isn't a known text input → reject if AXValue isn't settable → set →
    return success. Any rejection returns ok=False with a `status` field
    naming the bail reason so the caller falls back to click+paste with
    enough context to log the choice.

    Returns:
      {ok: True,  role, status: "set", via: "ax_set_value"}            on success
      {ok: False, status: "no_element"}                                no AX element at coord
      {ok: False, role, status: "web_backed"}                          inside AXWebArea — JS won't see set
      {ok: False, role, status: "not_text_input"}                      role isn't in _AX_TEXT_INPUT_ROLES
      {ok: False, role, status: "not_settable"}                        AXValue is read-only on this element
      {ok: False, role, status: "set_failed", err}                     AXSetAttributeValue itself returned non-zero
    """
    sys_elem = _appserv.AXUIElementCreateSystemWide()
    if not sys_elem:
        return {"ok": False, "status": "no_element"}
    elem_ref = ctypes.c_void_p(0)
    err = _appserv.AXUIElementCopyElementAtPosition(
        ctypes.c_void_p(sys_elem), float(x), float(y), ctypes.byref(elem_ref)
    )
    _cf.CFRelease(ctypes.c_void_p(sys_elem))
    if err != 0 or not elem_ref.value:
        return {"ok": False, "status": "no_element"}

    try:
        role = _ax_str_attr(int(elem_ref.value), b"AXRole") or "AXUnknownRole"

        if role not in _AX_TEXT_INPUT_ROLES:
            return {"ok": False, "role": role, "status": "not_text_input"}

        if _ax_is_web_backed(int(elem_ref.value)):
            # AXSetValue on a web-backed input "succeeds" at the AX layer
            # but doesn't propagate to the DOM input.value — submit handlers
            # see the old empty value. Bail early.
            return {"ok": False, "role": role, "status": "web_backed"}

        if not _ax_attr_is_settable(int(elem_ref.value), b"AXValue"):
            return {"ok": False, "role": role, "status": "not_settable"}

        cfstr = _cf.CFStringCreateWithCString(
            None, text.encode("utf-8"), kCFStringEncodingUTF8,
        )
        if not cfstr:
            return {"ok": False, "role": role, "status": "set_failed", "err": "cfstring_alloc"}
        try:
            ok = _ax_set_attr_value(int(elem_ref.value), b"AXValue", cfstr)
        finally:
            _cf.CFRelease(ctypes.c_void_p(cfstr))
        if not ok:
            return {"ok": False, "role": role, "status": "set_failed", "err": "ax_set_returned_nonzero"}
        return {"ok": True, "role": role, "status": "set", "via": "ax_set_value"}
    finally:
        _cf.CFRelease(elem_ref)


def _verify_focused_window(pid: int, target_x: float, target_y: float, target_w: float, target_h: float, tolerance: float = 4.0) -> bool:
    """
    Read the app's AXFocusedWindow and check whether its position+size matches
    the target. This is the post-condition for raise_window: even if AXRaise
    returned ok, the actual key window for keystrokes may differ when multiple
    windows overlap. Returns True iff the focused window matches the target.
    """
    app_ptr = _appserv.AXUIElementCreateApplication(pid)
    if not app_ptr:
        return False
    try:
        focused_ptr = _ax_read_attr_ptr(app_ptr, b"AXFocusedWindow")
        if not focused_ptr:
            return False
        try:
            pos = _ax_cgpoint(focused_ptr)
            size = _ax_cgsize(focused_ptr)
            if pos is None:
                return False
            if abs(pos[0] - target_x) > tolerance or abs(pos[1] - target_y) > tolerance:
                return False
            # Size is a stronger signal when present (disambiguates fullscreen overlay).
            if size is not None and (abs(size[0] - target_w) > tolerance or abs(size[1] - target_h) > tolerance):
                return False
            return True
        finally:
            _cf.CFRelease(ctypes.c_void_p(focused_ptr))
    finally:
        _cf.CFRelease(ctypes.c_void_p(app_ptr))


def is_window_key(pid: int, window_id: int) -> bool:
    """True if the given CG window is already this app's key/focused window —
    determined WITHOUT activating the app (no focus theft). Lets background mode
    decide whether keystrokes will land in the right window before choosing to
    bail. Returns False on any error (treat unknown as not-key)."""
    try:
        from . import capture
        win = capture.get_window_by_id(window_id)
        if not win or win.get("pid") != pid:
            return False
        return _verify_focused_window(
            pid, float(win["x"]), float(win["y"]),
            float(win["width"]), float(win["height"]),
        )
    except Exception:
        return False


async def raise_window(pid: int, window_id: int) -> dict:
    """
    Bring a specific CG window (by ID) to front and make it the key window.

    Returns a status dict so callers (and downstream tool responses) can
    distinguish silent failures from success:

        ok        — True when the target is the focused/key window after the call.
        via       — 'ax' (AXRaise worked), 'ax_retry' (worked after retry),
                    'ax_no_match' (couldn't find AXWindow ref for this CG id),
                    'ax_raise_failed' (AXRaise + retries didn't make target key).
        focused   — True iff AXFocusedWindow matches target position+size.
        window_id — echoed for convenience.
        warning   — present iff ok=False; human-readable hint for the agent.

    Raises RuntimeError only when the CG window is missing entirely (closed /
    minimized / wrong Space / pid mismatch) — that's an unrecoverable input.
    """
    from . import capture
    win = capture.get_window_by_id(window_id)
    if not win:
        raise RuntimeError(
            f"Window {window_id} not found on screen. It may have been closed, "
            "minimized, or moved to another Space. Call list_windows to refresh."
        )
    if win["pid"] != pid:
        raise RuntimeError(
            f"Window {window_id} belongs to pid {win['pid']}, not {pid}. "
            "Window ID likely went stale across an app relaunch — call list_windows again."
        )

    tx, ty, tw, th = float(win["x"]), float(win["y"]), float(win["width"]), float(win["height"])

    # Activate app first so AXRaise actually brings the app forward, not just
    # the window within an already-background app.
    await activate_app(pid)

    ax_win = _ax_window_for_cg_id(pid, window_id, tx, ty, tw, th)
    if not ax_win:
        # No AX match — app is active but we can't raise the specific window.
        # Verify whether it happens to already be the focused window anyway
        # (single-window app, or it was already on top).
        focused = _verify_focused_window(pid, tx, ty, tw, th)
        return {
            "ok": focused,
            "window_id": window_id,
            "via": "ax_no_match",
            "focused": focused,
            "warning": None if focused else (
                "Could not resolve target window via AX. Keys/clicks will go to "
                "whichever window of this app is currently key, which may not be "
                "the requested one. Call list_windows to refresh, or click into "
                "the target window once to make it key."
            ),
        }

    try:
        ok = _ax_perform_action(ax_win, b"AXRaise")
        await asyncio.sleep(0.03)
        if _verify_focused_window(pid, tx, ty, tw, th):
            return {"ok": True, "window_id": window_id, "via": "ax", "focused": True}

        # Post-condition failed: AXRaise returned ok=True (or False) but the
        # focused window is still something else. Retry once after a longer settle.
        _ax_perform_action(ax_win, b"AXRaise")
        await asyncio.sleep(0.08)
        if _verify_focused_window(pid, tx, ty, tw, th):
            return {"ok": True, "window_id": window_id, "via": "ax_retry", "focused": True}

        return {
            "ok": False,
            "window_id": window_id,
            "via": "ax_raise_failed",
            "focused": False,
            "warning": (
                "AXRaise on the target window returned but the app's focused "
                f"window is still elsewhere (AXRaise returncode ok={ok}). Keys/clicks "
                "WILL route to whichever window is key — likely the wrong one. "
                "Most common cause: a modal dialog or another window of this app "
                "is grabbing focus. Dismiss it (Escape) or click into the target."
            ),
        }
    finally:
        _cf.CFRelease(ctypes.c_void_p(ax_win))


def set_window_bounds_by_id(pid: int, window_id: int, x: int, y: int, width: int | None = None, height: int | None = None) -> dict:
    """
    Position and optionally resize a specific window (by CG window ID).
    Uses AX directly (AXPosition / AXSize) — faster and more reliable than
    osascript, and crucially doesn't require the window to be frontmost.
    Returns {ok, window_id, x, y, width?, height?}.
    """
    from . import capture
    win = capture.get_window_by_id(window_id)
    if not win:
        raise RuntimeError(
            f"Window {window_id} not found on screen (pid {pid}). "
            "Call list_windows to get a current window ID."
        )

    ax_win = _ax_window_for_cg_id(pid, window_id, win["x"], win["y"], win["width"], win["height"])
    if not ax_win:
        raise RuntimeError(
            f"Could not match window {window_id} via AX. "
            "App may have non-standard window implementation, or AX permission missing."
        )
    try:
        # Position
        pt = CGPoint(x=float(x), y=float(y))
        pos_val = _appserv.AXValueCreate(kAXValueCGPointType, ctypes.byref(pt))
        if not pos_val:
            raise RuntimeError("AXValueCreate(CGPoint) failed")
        try:
            ok_pos = _ax_set_attr_value(ax_win, b"AXPosition", pos_val)
        finally:
            _cf.CFRelease(ctypes.c_void_p(pos_val))
        if not ok_pos:
            raise RuntimeError(f"AXPosition set failed for window {window_id}")

        result = {"ok": True, "window_id": window_id, "x": x, "y": y}

        if width is not None and height is not None:
            sz = CGSize(width=float(width), height=float(height))
            size_val = _appserv.AXValueCreate(kAXValueCGSizeType, ctypes.byref(sz))
            if not size_val:
                raise RuntimeError("AXValueCreate(CGSize) failed")
            try:
                ok_sz = _ax_set_attr_value(ax_win, b"AXSize", size_val)
            finally:
                _cf.CFRelease(ctypes.c_void_p(size_val))
            if not ok_sz:
                raise RuntimeError(f"AXSize set failed for window {window_id}")
            result["width"] = width
            result["height"] = height
        return result
    finally:
        _cf.CFRelease(ctypes.c_void_p(ax_win))


# ---------------------------------------------------------------------------
# Mouse input
# ---------------------------------------------------------------------------

async def move_cursor(x: int, y: int) -> None:
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        ev = _cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, pt, kCGMouseButtonLeft)
        _post(ev)


def _modifier_flags(modifiers: list[str] | None) -> int:
    if not modifiers:
        return 0
    flags = 0
    for mod in modifiers:
        flags |= MODIFIER_FLAGS.get(mod.lower(), 0)
    return flags


def modifier_flags_from_list(modifiers: list[str] | None) -> int:
    """Public alias of _modifier_flags so mcp_server / external callers can
    convert a ["cmd", "shift"] list to a CGEventFlags bitmask without
    importing the private name."""
    return _modifier_flags(modifiers)


async def click(x: int, y: int, button: str = "left", modifiers: list[str] | None = None) -> None:
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        if button == "right":
            down_t, up_t, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        else:
            down_t, up_t, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        flags = _modifier_flags(modifiers)
        ev_down = _cg.CGEventCreateMouseEvent(None, down_t, pt, btn)
        if flags:
            _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), flags)
        _post(ev_down)
        await asyncio.sleep(0.005)
        ev_up = _cg.CGEventCreateMouseEvent(None, up_t, pt, btn)
        if flags:
            _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), flags)
        _post(ev_up)


async def long_press(x: int, y: int, duration: float = 1.0, button: str = "left") -> None:
    """
    Press and hold the mouse button at (x, y) for `duration` seconds, then
    release. Use for context menus that appear on hold, drag-initiation in
    some UIs, and any control whose behavior changes with a long vs. short
    press. Default duration is 1 s — adjust per target.
    """
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        if button == "right":
            down_t, up_t, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        else:
            down_t, up_t, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        ev_down = _cg.CGEventCreateMouseEvent(None, down_t, pt, btn)
        _post(ev_down)
        # Hold. Caller-provided duration; use small interval so an emergency
        # stop can interrupt during a long hold without waiting for the full
        # sleep.
        elapsed = 0.0
        step = 0.05
        while elapsed < duration:
            _check_stop()
            await asyncio.sleep(min(step, duration - elapsed))
            elapsed += step
        ev_up = _cg.CGEventCreateMouseEvent(None, up_t, pt, btn)
        _post(ev_up)


async def double_click(x: int, y: int, modifiers: list[str] | None = None) -> None:
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        flags = _modifier_flags(modifiers)
        for click_state in (1, 2):
            ev_down = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft)
            _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev_down), kCGMouseEventClickState, click_state)
            if flags:
                _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), flags)
            _post(ev_down)
            await asyncio.sleep(0.02)
            ev_up = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft)
            _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev_up), kCGMouseEventClickState, click_state)
            if flags:
                _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), flags)
            _post(ev_up)
            await asyncio.sleep(0.02)


async def triple_click(x: int, y: int, modifiers: list[str] | None = None) -> None:
    """Three down/up pairs with click_state 1/2/3 — apps see a real triple click
    (paragraph select in text views, full-content select in single-line fields)."""
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        flags = _modifier_flags(modifiers)
        for click_state in (1, 2, 3):
            ev_down = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft)
            _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev_down), kCGMouseEventClickState, click_state)
            if flags:
                _cg.CGEventSetFlags(ctypes.c_void_p(ev_down), flags)
            _post(ev_down)
            await asyncio.sleep(0.02)
            ev_up = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft)
            _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev_up), kCGMouseEventClickState, click_state)
            if flags:
                _cg.CGEventSetFlags(ctypes.c_void_p(ev_up), flags)
            _post(ev_up)
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Keyboard input
# ---------------------------------------------------------------------------

async def press_key(key_string: str, pid: int | None = None) -> None:
    _check_stop()
    async with _input_lock:
        keycode, flags = parse_key_combo(key_string)
        # Pre-settle so the first key isn't lost to a post-focus/post-activation
        # renderer dead-zone (~30-80 ms) — the same first-keystroke drop that
        # type_text_char_by_char guards against (observed dropping the leading
        # char on Chromium, e.g. REIST → EIST). PostToPid path only; the HID-tap
        # path (pid=None, humanoid) doesn't hit the renderer dead-zone.
        if pid is not None:
            await asyncio.sleep(0.060)
        _press_key_sync(keycode, flags, pid)


async def hold_key(
    key_string: str,
    duration: float,
    pid: int | None = None,
) -> None:
    """
    Press `key_string` and hold it down for `duration` seconds, then release.
    Auto-repeats the keydown every 50 ms during the hold so apps that drive
    behaviour off key-repeat (game movement, scroll-spy, etc.) keep firing.

    Routes via CGEventPostToPid when a pid is given — invisible, no cursor
    movement, no focus change. The emergency-stop chord (Cmd+Shift+Escape)
    is checked every 50 ms so a long hold doesn't block escape.

    Modifier-only holds (e.g. just 'Shift', just 'Cmd') raise — those go
    through the `modifiers` list on click/type/scroll instead, which already
    holds the modifier across the action invisibly. This is for non-modifier
    keys (Space, W, Down, Return, 'a', …).
    """
    keycode, flags = parse_key_combo(key_string)
    _check_stop()
    async with _input_lock:
        # Initial press.
        _key_down_sync(keycode, flags, pid)
        # Hold loop — reposts keydown every 50 ms so apps that expect a
        # repeat stream see one. macOS itself emits ~33 ms repeats for held
        # physical keys; 50 ms is close enough and emergency-stop responsive.
        elapsed = 0.0
        step = 0.05
        try:
            while elapsed < duration:
                _check_stop()
                await asyncio.sleep(min(step, duration - elapsed))
                elapsed += step
                if elapsed < duration:
                    # Re-post keydown for auto-repeat. Skip the final repost
                    # — the keyup is fired below.
                    _key_down_sync(keycode, flags, pid)
        finally:
            # Always release, even on _check_stop interrupt — leaving a key
            # stuck down would be a much worse failure mode than a partial
            # hold.
            _key_up_sync(keycode, flags, pid)


async def press_keys(keys: list[str], pid: int | None = None) -> None:
    """
    Press a sequence of keys back-to-back under a single input-lock acquisition.
    Parses every entry up front so a bad key string fails the whole batch before
    any event is posted (atomic-fail, no partial side effects). Re-checks the
    emergency-stop signal between every keystroke so Cmd+Shift+Esc mid-batch
    actually halts the remaining presses.

    Inter-press delay of 18 ms. Without it, fast-repeated identical keys
    (e.g. Backspace × 6) are silently coalesced at the renderer's keyboard
    event queue: only a fraction of the presses actually take effect.
    Empirically Backspace × 6 with no delay landed as ~3 effective presses
    on Chrome / 6mal5; 18 ms between is enough to keep them distinct
    without noticeably slowing pure-key sequences (a 200-press batch
    still finishes in ~4 s, well inside the tool budget).
    """
    if not keys:
        return
    parsed = [parse_key_combo(k) for k in keys]
    _check_stop()
    async with _input_lock:
        # Pre-settle once so the FIRST key isn't lost to the post-focus renderer
        # dead-zone (see press_key). One 60 ms cost per batch, regardless of
        # length; PostToPid path only.
        if pid is not None:
            await asyncio.sleep(0.060)
        first = True
        for keycode, flags in parsed:
            _check_stop()
            if not first:
                await asyncio.sleep(0.018)
            _press_key_sync(keycode, flags, pid)
            first = False


# System / media keys. These live outside the regular CGEvent keyboard table —
# the OS routes volume, brightness, and media transport through NX_SYSDEFINED
# (NSEventTypeSystemDefined, subtype 8 = aux-key). Codes are the IOKit
# NX_KEYTYPE_* constants from <IOKit/hidsystem/ev_keymap.h>.
SYSTEM_KEY_CODES: dict[str, int] = {
    "volume_up": 0,
    "volume_down": 1,
    "brightness_up": 2,
    "brightness_down": 3,
    "mute": 7,
    "eject": 14,
    "play_pause": 16,
    "next_track": 17,
    "previous_track": 18,
    "fast_forward": 19,
    "rewind": 20,
    "keyboard_brightness_up": 23,
    "keyboard_brightness_down": 24,
    "keyboard_brightness_toggle": 25,
}

SYSTEM_KEY_NAMES: list[str] = sorted(SYSTEM_KEY_CODES)


async def press_system_key(name: str) -> None:
    """
    Fire a system / media key (volume, brightness, play/pause, track skip,
    keyboard backlight, eject) by posting an NSSystemDefined event with
    subtype 8 (aux-key) and a packed data1 field.

    These keys are global — they affect the whole OS, not the foreground app.
    Volume up here behaves identically to pressing F12 on an Apple keyboard.
    """
    key = name.lower().replace("-", "_").replace(" ", "_")
    code = SYSTEM_KEY_CODES.get(key)
    if code is None:
        raise ValueError(
            f"Unknown system key {name!r}. Supported: {SYSTEM_KEY_NAMES}"
        )
    _check_stop()
    # Lazy-import: only loaded when this tool is actually called. AppKit is a
    # transitive of pyobjc-framework-Quartz but declared explicitly in
    # requirements.txt so a future PyObjC re-shuffle can't silently break us.
    from AppKit import NSEvent  # type: ignore
    import Quartz as _Q  # type: ignore

    NSEventTypeSystemDefined = 14
    NSSystemDefinedEventSubtypeAuxKey = 8
    NX_KEYDOWN = 0xA
    NX_KEYUP = 0xB

    async with _input_lock:
        for is_down in (True, False):
            _check_stop()
            phase = NX_KEYDOWN if is_down else NX_KEYUP
            # data1 packs: high 16 = key code, low 16 = (phase << 8) | flags.
            # flags = 0 for a single press. Setting bit 0 of the low byte
            # would indicate a "repeat" key — leave clear for one-shot.
            data1 = (code << 16) | (phase << 8)
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                NSEventTypeSystemDefined,
                (0.0, 0.0),
                0xA00,                      # NX_KEYDOWNMASK in modifier flags
                0,
                0,
                None,
                NSSystemDefinedEventSubtypeAuxKey,
                data1,
                -1,
            )
            cg = ev.CGEvent()
            _Q.CGEventPost(_Q.kCGHIDEventTap, cg)
            if is_down:
                # Tiny gap between down and up so the OS treats it as a real
                # one-shot press, not a too-fast phantom.
                await asyncio.sleep(0.01)


_clipboard_restore_task: asyncio.Task | None = None
# The user's pasteboard contents captured before a paste, pending restore.
# Held at module scope (not a local) so the atexit safety net can flush it if
# the process exits inside the post-paste restore window. None = nothing pending.
_clipboard_snapshot: list | None = None


def _snapshot_pasteboard() -> list | None:
    """Capture the general pasteboard's full typed contents so a paste can be
    undone byte-for-byte — preserving images, files, RTF, or an empty
    clipboard, not just plain text (pbpaste silently flattens all of those to
    ''). Returns NSPasteboardItem copies, or None if AppKit is unavailable so
    the caller skips the restore rather than clobbering with empty text."""
    try:
        from AppKit import NSPasteboard, NSPasteboardItem  # lazy, like NSEvent
        pb = NSPasteboard.generalPasteboard()
        snapshot = []
        for item in (pb.pasteboardItems() or []):
            copy = NSPasteboardItem.alloc().init()
            for t in (item.types() or []):
                data = item.dataForType_(t)
                if data is not None:
                    copy.setData_forType_(data, t)
            snapshot.append(copy)
        return snapshot
    except Exception:
        return None


def _restore_pasteboard(snapshot: list | None) -> None:
    """Restore a snapshot from _snapshot_pasteboard, replacing klyk's pasted
    text with the user's original contents — or an empty clipboard if that's
    what they had. No-op when AppKit was unavailable at snapshot time (None)."""
    if snapshot is None:
        return
    try:
        from AppKit import NSPasteboard
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        if snapshot:
            pb.writeObjects_(snapshot)
    except Exception:
        pass


def _flush_clipboard_restore() -> None:
    """Best-effort synchronous restore for the narrow window where the process
    exits after a paste but before the deferred restore task has run."""
    if _clipboard_snapshot is not None:
        _restore_pasteboard(_clipboard_snapshot)


atexit.register(_flush_clipboard_restore)


async def type_text(text: str, pid: int | None = None) -> None:
    """Type text using clipboard paste, snapshotting and restoring the user's
    FULL clipboard (text, image, files, or empty) around the paste — never just
    the plain-text view that pbpaste exposes, which would corrupt an image or
    file clipboard."""
    global _clipboard_restore_task, _clipboard_snapshot
    _check_stop()
    async with _input_lock:
        # Cancel a pending restore but KEEP its snapshot — the user's original
        # clipboard still has to be restored after THIS paste too. Re-snapshotting
        # now would capture the prior paste's text, so only snapshot when nothing
        # is already pending (the start of a fresh burst).
        if _clipboard_restore_task and not _clipboard_restore_task.done():
            _clipboard_restore_task.cancel()
        if _clipboard_snapshot is None:
            _clipboard_snapshot = _snapshot_pasteboard()
        # timeout so a contended pasteboard (e.g. behind a modal sheet) fails
        # fast instead of hanging type_text for minutes (was unbounded).
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
        await asyncio.sleep(0.005)
        _paste_sync(pid)

    async def _restore():
        # Deferred so the target app's Cmd+V consumes our text before we put the
        # user's data back. Reads the snapshot under the lock at run time; a
        # cancelled restore (rapid successive type_text) never reaches these
        # lines, so the snapshot survives until the final paste's restore runs.
        global _clipboard_snapshot
        await asyncio.sleep(0.15)
        async with _input_lock:
            _restore_pasteboard(_clipboard_snapshot)
            _clipboard_snapshot = None
    _clipboard_restore_task = asyncio.create_task(_restore())


async def type_text_char_by_char(text: str, pid: int | None = None) -> None:
    """
    Per-character keydown/keyup sequence. Used by type_text(mode='keys')
    for keypress-driven contexts (web games, canvas editors).

    Settle delay (60 ms) before the FIRST character. Empirically, when a
    prior tool call activated the target app (autonomous-mode click into
    Chromium triggers an activation), the renderer's keyboard handler is
    not ready for incoming keys for ~30-80 ms after activation. The first
    keystroke that arrives in that window is silently dropped — observed
    on 6mal5 (Chromium) where the leading H of "HEBEL" and the leading Ü
    of "HÜGEL" both vanished. The 60 ms pre-settle absorbs that window
    so the first char lands reliably. Subsequent chars use the existing
    15 ms inter-char delay.
    """
    _check_stop()
    async with _input_lock:
        post = (lambda ev: _post_to_pid(pid, ev)) if pid else _post
        # Pre-settle so the first key doesn't fall into a post-activation
        # renderer dead-zone. Cost: 60 ms once per call, regardless of length.
        await asyncio.sleep(0.060)
        for char in text:
            keycode, flags = char_to_keycode(char)
            if keycode is not None:
                _press_key_sync(keycode, flags, pid)
            else:
                uni = (ctypes.c_uint16 * 1)(ord(char))
                ev_down = _cg.CGEventCreateKeyboardEvent(None, 0, True)
                _cg.CGEventKeyboardSetUnicodeString(
                    ctypes.c_void_p(ev_down), 1, ctypes.cast(uni, ctypes.c_void_p)
                )
                post(ev_down)
                time.sleep(0.005)
                ev_up = _cg.CGEventCreateKeyboardEvent(None, 0, False)
                _cg.CGEventKeyboardSetUnicodeString(
                    ctypes.c_void_p(ev_up), 1, ctypes.cast(uni, ctypes.c_void_p)
                )
                post(ev_up)
            await asyncio.sleep(0.015)


# ---------------------------------------------------------------------------
# Drag and drop
# ---------------------------------------------------------------------------

async def drag(
    x1: int, y1: int,
    x2: int, y2: int,
    steps: int = 20,
    step_delay: float = 0.010,
    hover_target_seconds: float = 0.0,
) -> None:
    """
    Drag from (x1, y1) to (x2, y2) with smooth intermediate events.
    Interpolates through `steps` points so the OS and app register a real drag.

    `hover_target_seconds` holds the mouse at the destination — still pressed —
    before releasing. Use for spring-loaded drops (Finder folders, dock items
    that expand on hover) where the target needs to recognize the hover before
    accepting the drop. The hold is checked against the emergency-stop chord
    every 50 ms so it doesn't block escape.
    """
    _check_stop()
    async with _input_lock:
        src = CGPoint(x=float(x1), y=float(y1))
        dst = CGPoint(x=float(x2), y=float(y2))

        ev_down = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, src, kCGMouseButtonLeft)
        _post(ev_down)
        await asyncio.sleep(0.05)

        for i in range(1, steps + 1):
            _check_stop()
            t = i / steps
            pt = CGPoint(
                x=x1 + (x2 - x1) * t,
                y=y1 + (y2 - y1) * t,
            )
            ev_drag = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, pt, kCGMouseButtonLeft)
            _post(ev_drag)
            time.sleep(step_delay)

        if hover_target_seconds > 0:
            # Spring-loaded hold at the destination. Slice into 50 ms chunks so
            # the emergency-stop chord stays responsive — same pattern as
            # long_press.
            remaining = float(hover_target_seconds)
            while remaining > 0:
                _check_stop()
                slice_s = min(0.05, remaining)
                await asyncio.sleep(slice_s)
                remaining -= slice_s
                # Re-emit a dragged event at the target to keep the OS-side
                # hover state alive — some apps drop the spring trigger if no
                # events arrive for too long.
                ev_idle = _cg.CGEventCreateMouseEvent(
                    None, kCGEventLeftMouseDragged, dst, kCGMouseButtonLeft,
                )
                _post(ev_idle)
        else:
            await asyncio.sleep(0.02)
        ev_up = _cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, dst, kCGMouseButtonLeft)
        _post(ev_up)


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

async def scroll(x: int, y: int, direction: str, amount: int) -> None:
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        ev_move = _cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, pt, kCGMouseButtonLeft)
        _post(ev_move)
        await asyncio.sleep(0.01)

        if direction in ("up", "down"):
            delta = amount if direction == "up" else -amount
            ev = _cg.CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, delta)
        else:
            delta = amount if direction == "right" else -amount
            ev = _cg.CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, 0)
            _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev), kCGScrollWheelEventDeltaAxis2, delta)

        _post(ev)


# Start global emergency stop listener on import
_start_emergency_stop_tap()
