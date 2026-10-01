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
    """Represent CoreGraphics logical-point coordinates at the native boundary."""
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    """Represent native window dimensions without guessing coordinate scale."""
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CFRange(ctypes.Structure):
    """Represent an exact CoreFoundation UTF-16 range for complete string reads."""
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


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
_cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
_cf.CFStringCreateWithBytes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_bool]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetLength.restype = ctypes.c_long
_cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
_cf.CFStringGetCharacters.restype = None
_cf.CFStringGetCharacters.argtypes = [ctypes.c_void_p, CFRange, ctypes.POINTER(ctypes.c_uint16)]
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
_appserv.AXUIElementGetPid.restype = ctypes.c_int32
_appserv.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
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
_cf.CFRunLoopRemoveSource.restype          = None
_cf.CFRunLoopRemoveSource.argtypes         = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
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
_stop_lock        = threading.RLock()  # Signal cleanup may interrupt a stop check on this thread.
_worker_state = threading.local()
_held_lock = threading.RLock()
_held_inputs: dict[tuple, object] = {}


class EmergencyStop(RuntimeError):
    """An input request attempted to continue after the user's stop latch engaged."""
    pass


def _begin_input(token: tuple, press, release) -> None:
    """Register release before a down event so exit cleanup cannot miss a held input."""
    with _held_lock:
        _check_stop()
        _held_inputs.setdefault(token, release)
        try:
            press()
        except BaseException:
            _finish_input(token)
            raise


def _finish_input(token: tuple) -> bool:
    """Release one held input exactly once, even when stop or cancellation is active."""
    with _held_lock:
        release = _held_inputs.pop(token, None)
        if release is None:
            return False
        release()
        return True


def release_held_input() -> None:
    """Halt new downs and release every held key/button before a hard process exit."""
    with _stop_lock:
        _stop_engaged[0] = True
    with _held_lock:
        pending = list(_held_inputs.values())
        _held_inputs.clear()
        for release in pending:
            try:
                release()
            except Exception:
                log.warning("A held input could not be released during exit cleanup.")


def _check_stop() -> None:
    """Stop new input after a physical stop or cancellation of its owning request."""
    cancelled = getattr(_worker_state, 'cancelled', None)
    if cancelled is not None and cancelled.is_set():
        raise RuntimeError('Input request was cancelled; no further input will be sent.')
    with _stop_lock:
        if not _stop_engaged[0]:
            return
    raise EmergencyStop(
        "Emergency stop is ACTIVE — all Klyk input is blocked. It can be cleared "
        "ONLY by the user pressing Cmd+Shift+Escape again; the resume tool cannot "
        "clear it. Tell the user to press the chord to resume."
    )


async def run_input(function):
    """Keep ownership until a cancelled native input worker has released its input."""
    cancelled = threading.Event()

    def run():
        """Make cancellation visible at the native worker's existing stop checkpoints."""
        _worker_state.cancelled = cancelled
        try:
            _check_stop()
            return function()
        finally:
            del _worker_state.cancelled

    worker = asyncio.get_running_loop().run_in_executor(None, run)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # Repeated cancellation must not release the request lock ahead of the worker.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # Retrieve a stopped worker's exception without masking cancellation.
        raise


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
    """Ignore key repeats and recover a disabled listener without changing the stop latch."""
    def _cb(proxy, etype, event, refcon):
        """Handle one event-tap notification without consuming the user's physical input."""
        try:
            if etype in (0xFFFFFFFE, 0xFFFFFFFF):
                tap = _stop_tap[0]
                if tap:
                    _cg.CGEventTapEnable(ctypes.c_void_p(tap), True)
                return event
            if etype == kCGEventKeyDown:
                if _cg.CGEventGetIntegerValueField(ctypes.c_void_p(event), 8):
                    return event  # A held chord must never toggle an engaged stop off.
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
_stop_tap = [None]
_cg.CGEventTapEnable.restype = None
_cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]


def _start_emergency_stop_tap() -> None:
    """Install the listener on a daemon run loop and retain its handle for recovery."""
    def _run():
        """Run the native listener without activating any application."""
        tap = src = rl = None
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
            _stop_tap[0] = tap
            src = _cf.CFMachPortCreateRunLoopSource(None, ctypes.c_void_p(tap), 0)
            if not src:
                log.warning("Emergency stop listener could not create its run-loop source.")
                return
            rl  = _cf.CFRunLoopGetCurrent()
            _cf.CFRunLoopAddSource(ctypes.c_void_p(rl), ctypes.c_void_p(src), common_modes)
            log.info("Emergency stop tap active — Cmd+Shift+Escape will halt all input")
            _cf.CFRunLoopRun()
        except Exception as error:
            log.warning("Emergency stop listener failed (%s).", type(error).__name__)
        finally:
            _stop_tap[0] = None
            if src:
                if rl:
                    _cf.CFRunLoopRemoveSource(ctypes.c_void_p(rl), ctypes.c_void_p(src), common_modes)
                _cf.CFRelease(ctypes.c_void_p(src))
            if tap:
                _cf.CFRelease(ctypes.c_void_p(tap))

    threading.Thread(target=_run, daemon=True, name="klyk-stop").start()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post(event_ptr: int) -> None:
    """Post and release one owned event; allocation failures must never reach native APIs."""
    if not event_ptr:
        raise RuntimeError("Input event could not be created; no input was sent.")
    try:
        _cg.CGEventPost(kCGHIDEventTap, ctypes.c_void_p(event_ptr))
    finally:
        _cf.CFRelease(ctypes.c_void_p(event_ptr))


def _post_to_pid(pid: int, event_ptr: int) -> None:
    """Post keyboard event directly to a process — no window activation needed."""
    if not event_ptr:
        raise RuntimeError("Input event could not be created; no input was sent.")
    try:
        _cg.CGEventPostToPid(ctypes.c_int32(pid), ctypes.c_void_p(event_ptr))
    finally:
        _cf.CFRelease(ctypes.c_void_p(event_ptr))


def _cfstr_to_py(cf_str: int) -> str:
    """Decode every bounded UTF-16 code unit; unreadable text must never resemble empty text."""
    if not cf_str:
        return ""
    length = _cf.CFStringGetLength(ctypes.c_void_p(cf_str))
    if not 0 <= length <= 100_000:
        raise ValueError("The complete accessibility value exceeds its safe text limit.")
    if length == 0:
        return ""
    units = (ctypes.c_uint16 * length)()
    _cf.CFStringGetCharacters(ctypes.c_void_p(cf_str), CFRange(0, length), units)
    return bytes(units).decode("utf-16-le", "strict")


def _cftype_to_str(val_ref: int) -> str:
    """Read native strings and scalar values without substituting unreadable values."""
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
        try:
            return _cfstr_to_py(desc)
        finally:
            _cf.CFRelease(ctypes.c_void_p(desc))
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

        try:
            result = _cftype_to_str(val_ref.value)
        finally:
            _cf.CFRelease(val_ref)
        return result if result else None

    except Exception:
        return None


def _press_key_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    """Post an exact modifier state, including zero, so preceding shortcuts cannot leak Shift."""
    _key_down_sync(keycode, flags, pid)
    try:
        time.sleep(0.005)
    finally:
        _key_up_sync(keycode, flags, pid)


def _key_down_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    """Post a single keydown — no matching keyup. Used by hold_key to press
    the key at the start of the hold; the hold loop reposts dragged-style
    keydowns to keep auto-repeat alive in apps that listen for repeats."""
    _begin_input(("key", pid, keycode),
                 lambda: _send_key_event(keycode, flags, True, pid),
                 lambda: _send_key_event(keycode, flags, False, pid))


def _key_up_sync(keycode: int, flags: int, pid: int | None = None) -> None:
    """Post a single keyup — pairs with _key_down_sync at the end of a hold."""
    _finish_input(("key", pid, keycode))


def _send_key_event(keycode: int, flags: int, is_down: bool, pid: int | None) -> None:
    """Construct a keyboard event with explicit flags and release its native reference."""
    event = _cg.CGEventCreateKeyboardEvent(None, keycode, is_down)
    if not event:
        raise RuntimeError("Keyboard event could not be created; input was interrupted.")
    try:
        _cg.CGEventSetFlags(ctypes.c_void_p(event), flags)
    except BaseException:
        _cf.CFRelease(ctypes.c_void_p(event))
        raise
    (_post_to_pid(pid, event) if pid else _post(event))


def _paste_sync(pid: int | None = None) -> None:
    """Deliver Cmd+V with the same balanced-key cleanup as every other shortcut."""
    # Resolve V against the active layout so Cmd+V works regardless of
    # whether the V key sits at the US-QWERTY position (kc 9).
    v_keycode, _ = char_to_keycode("v")
    if v_keycode is None:
        v_keycode = 9  # last-ditch fallback
    _press_key_sync(v_keycode, MODIFIER_FLAGS["cmd"], pid)


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
    _check_stop()
    try:
        psn = _PSN()
        err = _appserv.GetProcessForPID(pid, ctypes.byref(psn))
        if err == 0:
            _check_stop()
            if _appserv.SetFrontProcessWithOptions(ctypes.byref(psn), _kSetFrontProcessFrontWindowOnly) == 0:
                await asyncio.sleep(0.005)
                return
    except RuntimeError:
        raise
    except Exception:
        pass
    # Fallback: shortened osascript (no AXRaise, just frontmost)
    script = f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'
    await run_input(
        lambda: subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=3)
    )
    await asyncio.sleep(0.05)


def is_frontmost_app(pid: int) -> bool:
    """Check the actual active process, not the owner of the highest visible window.

    Process Manager reads avoid stale AppKit caches and correctly handle active
    apps with no normal windows. Unknown foreground state fails closed.
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


def ax_value_at_detailed(x: float, y: float, max_retries: int = 4, settle_ms: int = 150, expected_pid: int | None = None, window_id: int | None = None) -> tuple[str | None, str]:
    """
    Same as ax_value_at but returns (value, status). Status is one of:
        "ok"         — value was read successfully
        "no_value"   — AX call succeeded but the element exposes no AXValue
        "no_element" — no AX element resolved at the coordinate
        "unreadable_value" — the full value cannot be safely decoded
    Lets handlers tell agents whether to retry (transient AX failure) or stop
    asking (the element fundamentally doesn't expose a value).
    """
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(settle_ms / 1000)
        try:
            elem_ref = ctypes.c_void_p(_ax_element_at(x, y, expected_pid, window_id))
            if not elem_ref.value:
                continue
            try:
                if not _ax_matches_pid(int(elem_ref.value), expected_pid):
                    return (None, "wrong_app")
                attr_key = _cf.CFStringCreateWithCString(None, b"AXValue", kCFStringEncodingUTF8)
                val_ref = ctypes.c_void_p(0)
                err = _appserv.AXUIElementCopyAttributeValue(
                    elem_ref, ctypes.c_void_p(attr_key), ctypes.byref(val_ref)
                )
                _cf.CFRelease(ctypes.c_void_p(attr_key))
                if err != 0:
                    if val_ref.value:
                        _cf.CFRelease(val_ref)
                    if err in (-25205, -25212):  # Unsupported attribute or no value is stable.
                        return (None, "no_value")
                    continue
                if not val_ref.value:
                    # Element resolved, attribute call ok, but no value present.
                    # That's a stable signal — element just doesn't expose AXValue.
                    return (None, "no_value")
                try:
                    value = _cftype_to_str(val_ref.value)
                except ValueError:
                    return (None, "unreadable_value")
                finally:
                    _cf.CFRelease(val_ref)
                return (value, "ok")
            finally:
                _cf.CFRelease(elem_ref)
        except Exception:
            continue
    return (None, "no_element")


def _ax_matches_pid(element: int, expected_pid: int | None) -> bool:
    """Reject a global AX hit belonging to an occluding application."""
    if expected_pid is None:
        return True
    pid = ctypes.c_int32()
    return (_appserv.AXUIElementGetPid(ctypes.c_void_p(element), ctypes.byref(pid)) == 0
            and pid.value == expected_pid)


def ax_perform_action_at(x: float, y: float, action: str, expected_pid: int | None = None, window_id: int | None = None, expected_label: str | None = None) -> dict:
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
    _check_stop()
    action_bytes = action.encode("utf-8")

    elem_ref = ctypes.c_void_p(_ax_element_at(x, y, expected_pid, window_id, expected_label))
    if not elem_ref.value:
        return {"ok": False, "action": action, "status": "no_element"}

    try:
        if not _ax_matches_pid(int(elem_ref.value), expected_pid):
            return {"ok": False, "status": "wrong_app", "error": "The AX target belongs to another app; no action was sent."}
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
    expected_pid: int | None = None,
    window_id: int | None = None,
    expected_label: str | None = None,
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
    _check_stop()
    elem_ref = ctypes.c_void_p(_ax_element_at(x, y, expected_pid, window_id, expected_label))
    if not elem_ref.value:
        return {"ok": False, "action": None, "status": "stale_target" if expected_label else "no_element"}

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
            raise RuntimeError("The accessibility action failed; its effect is unknown. Observe before retrying.")
        return None, avail

    # Track parent refs we own so we can release them in `finally`.
    parent_chain: list[int] = []
    try:
        if not _ax_matches_pid(int(elem_ref.value), expected_pid):
            return {"ok": False, "status": "wrong_app", "error": "The AX target belongs to another app; no action was sent."}
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


def ax_grid_text(pid: int, window_id: int, points: list[tuple[float, float]]) -> list[str | None]:
    """Read grid cells from one bounded window tree; pixels remain the color authority."""
    elements = ax_snapshot(pid, window_id=window_id, max_results=600, max_children_per_node=80)
    values = []
    for x, y in points:
        candidates = [e for e in elements if (e.get('value') or e.get('label'))
                      and e.get('width', 0) > 0 and e.get('height', 0) > 0
                      and abs(e['x'] - x) <= e['width'] / 2
                      and abs(e['y'] - y) <= e['height'] / 2
                      and e.get('role') not in ('AXWindow', 'AXGroup', 'AXScrollArea')]
        target = min(candidates, key=lambda e: e['width'] * e['height'], default=None)
        value = (target.get('value') or target.get('label')) if target else None
        values.append(str(value)[:200] if value is not None else None)
    return values


# ---------------------------------------------------------------------------
# AX tree snapshot — full UI element inspection
# ---------------------------------------------------------------------------

def _ax_read_attr_ptr(elem: int, attr: bytes) -> int:
    """Return an owned AX attribute only when the native read succeeded."""
    attr_key = _cf.CFStringCreateWithCString(None, attr, kCFStringEncodingUTF8)
    if not attr_key:
        return 0
    val_ref = ctypes.c_void_p(0)
    try:
        err = _appserv.AXUIElementCopyAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(attr_key), ctypes.byref(val_ref)
        )
    finally:
        _cf.CFRelease(ctypes.c_void_p(attr_key))
    if err != 0:
        if val_ref.value:
            _cf.CFRelease(val_ref)
        return 0
    return val_ref.value or 0


def _ax_str_attr(elem: int, attr: bytes) -> str:
    """Decode one owned attribute while balancing references on failed or oversized reads."""
    ptr = _ax_read_attr_ptr(elem, attr)
    if not ptr:
        return ""
    try:
        return _cftype_to_str(ptr)
    finally:
        _cf.CFRelease(ctypes.c_void_p(ptr))


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
    window_id: int | None = None,
) -> list[dict]:
    """
    Return labeled/interactive elements from the selected window, or all app windows if omitted.
    Each element: {role, label?, value?, x, y, width, height} in screen
    coords. Single-IPC-per-element via batched attribute reads.

    Three budget knobs:
      - `max_children_per_node` (default 20) caps each parent's child
        walk so huge homogeneous collections (Finder Desktop icons,
        browser DOM list rows) don't dominate latency.
      - `deadline_seconds` (default 0.9 s) is a wall-clock cut-off — the
        walker returns whatever it has when it crosses, plus any in-flight
        native call's bounded messaging timeout.
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
            if window_id is not None:
                root = _ax_exact_window(pid, window_id)
                if not root:
                    return []
                try:
                    _ax_collect(root, results, 0, max_depth, max_children_per_node,
                                deadline_ts, max_results, focused_ref)
                    return results
                finally:
                    _cf.CFRelease(ctypes.c_void_p(root))
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
    """Build an exact UTF-8 string without silently truncating embedded NUL characters."""
    encoded = s.encode("utf-8")
    return _cf.CFStringCreateWithBytes(None, encoded, len(encoded), kCFStringEncodingUTF8, False)


def _ax_str(elem: int, attr: bytes) -> str:
    p = _ax_read_attr_ptr(elem, attr)
    if not p:
        return ""
    try:
        return _cftype_to_str(p)
    finally:
        _cf.CFRelease(ctypes.c_void_p(p))


def _ax_set_value(elem: int, value: str) -> bool:
    """Set a string attribute only while the emergency latch permits input."""
    _check_stop()
    a = _cfstr("AXValue")
    v = _cfstr(value)
    try:
        if not a or not v:
            return False
        return _appserv.AXUIElementSetAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(a), ctypes.c_void_p(v)
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))
        if v:
            _cf.CFRelease(ctypes.c_void_p(v))


def _ax_press(elem: int, action: str = "AXPress") -> bool:
    """Perform a panel action only while the emergency latch permits input."""
    _check_stop()
    a = _cfstr(action)
    try:
        if not a:
            return False
        return _appserv.AXUIElementPerformAction(
            ctypes.c_void_p(elem), ctypes.c_void_p(a)
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))


def _ax_save_field(pid: int) -> int | None:
    """Retain one unambiguous Save As field within a bounded, read-only panel walk."""
    _check_stop()
    app = _appserv.AXUIElementCreateApplication(pid)
    if not app:
        return None
    fields = []
    state = {"label": False, "nodes": 0, "complete": True}
    deadline = time.monotonic() + 1.5
    try:
        _appserv.AXUIElementSetMessagingTimeout(ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS)

        def walk(element: int, depth: int, in_panel: bool) -> None:
            """Collect candidates before any write so duplicates never produce partial input."""
            _check_stop()
            state["nodes"] += 1
            if depth > 16 or state["nodes"] > 600 or time.monotonic() >= deadline:
                state["complete"] = False
                return
            role = _ax_str(element, b"AXRole")
            if role in ("AXSheet", "AXDialog"):
                in_panel = True
                state["label"] = False
            if in_panel:
                if role == "AXStaticText" and _ax_str(element, b"AXValue").strip().rstrip(":").casefold() == "save as":
                    state["label"] = True
                elif role == "AXTextField" and state["label"] and _ax_str(element, b"AXTitle").casefold() != "tag editor":
                    fields.append(_cf.CFRetain(ctypes.c_void_p(element)))
                    state["label"] = False
            children = _ax_read_attr_ptr(element, b"AXChildren")
            if children:
                try:
                    count = _cf.CFArrayGetCount(ctypes.c_void_p(children))
                    if count > 80:
                        state["complete"] = False
                    for index in range(min(count, 80)):
                        if not state["complete"]:
                            break
                        child = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(children), index)
                        if child:
                            walk(child, depth + 1, in_panel)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(children))

        walk(app, 0, False)
        if not state["complete"] or len(fields) != 1:
            return None
        field = fields[0]
        if _ax_str(field, b"AXRole") != "AXTextField":
            return None
        return fields.pop()
    finally:
        for field in fields:
            _cf.CFRelease(ctypes.c_void_p(field))
        _cf.CFRelease(ctypes.c_void_p(app))


def ax_set_save_filename(pid: int, filename: str) -> bool:
    """Write one verified Save As field once; a failed AX write never targets a second field."""
    try:
        field = _ax_save_field(pid)
        if not field:
            return False
        try:
            return _ax_set_value(field, filename)
        finally:
            _cf.CFRelease(ctypes.c_void_p(field))
    except RuntimeError:
        raise
    except Exception:
        return False


def _ax_set_focused(elem: int) -> bool:
    """Set AXFocused=true on an element (gives it keyboard focus)."""
    _check_stop()
    a = _cfstr("AXFocused")
    try:
        if not a:
            return False
        return _appserv.AXUIElementSetAttributeValue(
            ctypes.c_void_p(elem), ctypes.c_void_p(a), _kCFBooleanTrue
        ) == 0
    finally:
        if a:
            _cf.CFRelease(ctypes.c_void_p(a))


def ax_focus_save_field(pid: int) -> bool:
    """Focus one verified Save As field; never redirect navigation keys into another field."""
    try:
        field = _ax_save_field(pid)
        if not field:
            return False
        try:
            return _ax_set_focused(field)
        finally:
            _cf.CFRelease(ctypes.c_void_p(field))
    except RuntimeError:
        raise
    except Exception:
        return False


def _ax_open_path_context(pid: int, expected: tuple[int, int, int, int, int] | None = None) -> tuple[int, int, int, int, int] | None:
    """Prove initial chooser scope, or revalidate an already retained field and its exact ancestry."""
    _check_stop()
    app = _appserv.AXUIElementCreateApplication(pid)
    if not app:
        return None
    owned = []
    buttons = []
    fields = []
    try:
        _appserv.AXUIElementSetMessagingTimeout(ctypes.c_void_p(app), _AX_MESSAGING_TIMEOUT_SECONDS)
        field = _ax_read_attr_ptr(app, b"AXFocusedUIElement")
        if not field:
            return None
        owned.append(field)
        if (_ax_str(field, b"AXRole") != "AXTextField"
                or _ax_str(field, b"AXSubrole") == "AXSecureTextField"
                or _ax_str(field, b"AXFocused").casefold() != "true"
                or not _ax_matches_pid(field, pid)
                or not _ax_attr_is_settable(field, b"AXValue")):
            return None

        # Native evidence on both supported SDKs exposes this exact parent
        # chain; a document field or a search field in the outer panel fails it.
        current = field
        for role in ("AXSheet", "AXSheet", "AXWindow", "AXApplication"):
            _check_stop()
            current = _ax_read_attr_ptr(current, b"AXParent")
            if not current:
                return None
            owned.append(current)
            if _ax_str(current, b"AXRole") != role or not _ax_matches_pid(current, pid):
                return None
        chooser, panel, window = owned[1:4]
        if (not _cf.CFEqual(ctypes.c_void_p(current), ctypes.c_void_p(app))
                or (_ax_str(panel, b"AXDescription") or _ax_str(panel, b"AXTitle")).strip().casefold() not in ("open", "choose")):
            return None

        if expected:
            if not all(_cf.CFEqual(ctypes.c_void_p(old), ctypes.c_void_p(new))
                       for old, new in zip((expected[0], expected[1], expected[2], expected[4]), (field, chooser, panel, window))):
                return None
            # The observed Go to Folder prompt disappears or echoes the path
            # after writing. Re-prove retained focus and ancestry,
            # never infer a replacement field from mutable suggestion content.
            if not expected[3]:
                result = tuple(_cf.CFRetain(ctypes.c_void_p(pointer)) for pointer in (field, chooser, panel, window))
                return (*result[:3], 0, result[3])

        state = {"complete": True, "marker": 0}
        seen = set()
        deadline = time.monotonic() + 0.8

        def walk(element: int, depth: int = 0) -> None:
            """Find initial path controls or re-prove the selected Go button's current attachment."""
            _check_stop()
            if element in seen:
                return
            if (depth > 10 or len(seen) >= 160 or time.monotonic() >= deadline
                    or not _ax_matches_pid(element, pid)):
                state["complete"] = False
                return
            seen.add(element)
            # Completed sibling subtrees release their child arrays. Keep each
            # seen reference alive so a later element cannot reuse its address.
            owned.append(_cf.CFRetain(ctypes.c_void_p(element)))
            raw = _ax_read_multi(element, _AX_WALK_ATTRS)
            if raw is None or len(raw) != 5:
                for pointer in raw or ():
                    if pointer:
                        _cf.CFRelease(ctypes.c_void_p(pointer))
                state["complete"] = False
                return
            try:
                role, title, description, value = [(_cftype_to_str(p) if p else "") for p in raw[:4]]
                if not role or role == "AXWebArea" or (depth and role in ("AXSheet", "AXDialog")):
                    state["complete"] = False
                    return
                if not expected and role == "AXStaticText" and value.strip().rstrip(":").casefold() in ("go to folder", "go to the folder"):
                    state["marker"] += 1
                if not expected and role in ("AXTextField", "AXComboBox", "AXTextArea", "AXSearchField") and _ax_attr_is_settable(element, b"AXValue"):
                    fields.append(_cf.CFRetain(ctypes.c_void_p(element)))
                if role == "AXButton" and (title or description).strip().casefold() in ("go", "go to folder"):
                    buttons.append(_cf.CFRetain(ctypes.c_void_p(element)))
                # These are path suggestions, not the editable path or its
                # confirmation control. Their size cannot manufacture uniqueness.
                if role in ("AXTable", "AXOutline", "AXBrowser", "AXList"):
                    return
                children = raw[4]
                if children:
                    count = _cf.CFArrayGetCount(ctypes.c_void_p(children))
                    if count > 80:
                        state["complete"] = False
                        return
                    for index in range(count):
                        if not state["complete"]:
                            break
                        child = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(children), index)
                        if child:
                            walk(child, depth + 1)
            finally:
                for pointer in raw:
                    if pointer:
                        _cf.CFRelease(ctypes.c_void_p(pointer))

        walk(chooser)
        if not state["complete"] or len(buttons) > 1:
            return None
        if expected:
            if len(buttons) != 1 or not _cf.CFEqual(ctypes.c_void_p(buttons[0]), ctypes.c_void_p(expected[3])):
                return None
            current = buttons[0]
            for _ in range(11):
                _check_stop()
                if time.monotonic() >= deadline:
                    return None
                current = _ax_read_attr_ptr(current, b"AXParent")
                if not current:
                    return None
                owned.append(current)
                if not _ax_matches_pid(current, pid):
                    return None
                if _cf.CFEqual(ctypes.c_void_p(current), ctypes.c_void_p(chooser)):
                    break
                if _ax_str(current, b"AXRole") in ("AXSheet", "AXDialog", "AXWindow", "AXApplication"):
                    return None
            else:
                return None
        elif (state["marker"] != 1 or len(fields) != 1
                or not _cf.CFEqual(ctypes.c_void_p(fields[0]), ctypes.c_void_p(field))):
            return None
        result = tuple(_cf.CFRetain(ctypes.c_void_p(pointer)) for pointer in (field, chooser, panel, window))
        return (*result[:3], _cf.CFRetain(ctypes.c_void_p(buttons[0])) if buttons else 0, result[3])
    except RuntimeError:
        raise
    except Exception:
        return None
    finally:
        for pointer in (*owned, *fields, *buttons, app):
            _cf.CFRelease(ctypes.c_void_p(pointer))


def _ax_panel_actions(element: int) -> set[str] | None:
    """Read bounded action names before choosing a native panel confirmation path."""
    _check_stop()
    names = ctypes.c_void_p()
    try:
        status = _appserv.AXUIElementCopyActionNames(ctypes.c_void_p(element), ctypes.byref(names))
        if status != 0 or not names.value:
            return None
        count = _cf.CFArrayGetCount(names)
        if not 0 <= count <= 32:
            return None
        return {_cftype_to_str(_cf.CFArrayGetValueAtIndex(names, index)) for index in range(count)}
    finally:
        if names.value:
            _cf.CFRelease(names)


def ax_navigate_open_panel(pid: int, path: str) -> str | None:
    """Submit one retained Go to Folder path; unknown effects never permit another input attempt."""
    _check_stop()
    if not path or "\0" in path or len(path) > 16_384:
        return None
    context = _ax_open_path_context(pid)
    if not context:
        return None
    field, chooser, panel, button, window = context
    attempted = False
    try:
        field_actions = _ax_panel_actions(field)
        chooser_actions = _ax_panel_actions(chooser)
        target, action = (chooser, b"AXConfirm") if "AXConfirm" in (chooser_actions or ()) else (0, b"")
        if not target and button and "AXPress" in (_ax_panel_actions(button) or ()):
            target, action = button, b"AXPress"
        keyboard = not target
        if (keyboard and not button and chooser_actions == {"AXRaise"}
                and field_actions is not None and "AXConfirm" in field_actions
                and field_actions <= {"AXConfirm", "AXShowMenu"}):
            # The standard field's AXConfirm accepts editing without closing
            # its chooser. Choose one primary foreground Return before the path
            # write: host-PID delivery reaches the app without submitting this sheet.
            target, action = field, b"AXConfirm"
        if not target:
            raise RuntimeError("The Go to Folder chooser has no accessible confirmation action; no path was typed.")

        _check_stop()
        # Retained identity does not prove current ownership after intervening
        # native reads; check every selected control before the first mutation.
        if not all(_ax_matches_pid(element, pid) for element in (chooser, panel, window, button, field) if element):
            return None
        expected = (field, chooser, panel, button if action == b"AXPress" else 0, window)
        before_write = _ax_open_path_context(pid, expected)
        try:
            if not before_write:
                return None
        finally:
            for pointer in before_write or ():
                if pointer:
                    _cf.CFRelease(ctypes.c_void_p(pointer))
        _check_stop()
        if not all(_ax_matches_pid(element, pid) for element in (field, chooser, panel, window, button, target) if element):
            return None
        attempted = True
        if not _ax_set_value(field, path) or _ax_str(field, b"AXValue") != path:
            raise RuntimeError("The Go to Folder path write could not be verified; inspect the dialog before continuing.")
        if keyboard:
            # Revalidate the retained target after the foreground lookup;
            # that native read can observe an intervening focus or path change.
            _check_frontmost(pid)
        current_actions = _ax_panel_actions(target)
        if (action.decode() not in (current_actions or ())
                or (keyboard and (not current_actions <= {"AXConfirm", "AXShowMenu"}
                    or _ax_panel_actions(chooser) != {"AXRaise"}))):
            raise RuntimeError("The Go to Folder target changed after the path write; inspect the dialog before continuing.")
        _check_stop()
        if not all(_ax_matches_pid(element, pid) for element in (field, chooser, panel, window, button, target) if element):
            raise RuntimeError("The Go to Folder target changed owner after the path write; inspect the dialog before continuing.")
        refreshed = _ax_open_path_context(pid, expected)
        try:
            if (not refreshed or not all(_cf.CFEqual(ctypes.c_void_p(context[index]), ctypes.c_void_p(refreshed[index]))
                                        for index in (0, 1, 2, 4))
                    or (action == b"AXPress" and (not refreshed[3]
                        or not _cf.CFEqual(ctypes.c_void_p(button), ctypes.c_void_p(refreshed[3]))
                        or _ax_str(button, b"AXRole") != "AXButton"
                        or (_ax_str(button, b"AXTitle") or _ax_str(button, b"AXDescription")).strip().casefold() not in ("go", "go to folder")))
                    or _ax_str(field, b"AXValue") != path):
                raise RuntimeError("The Go to Folder target changed after the path write; inspect the dialog before continuing.")
        finally:
            for pointer in refreshed or ():
                if pointer:
                    _cf.CFRelease(ctypes.c_void_p(pointer))
        _check_stop()
        if not all(_ax_matches_pid(element, pid) for element in (field, chooser, panel, window, button, target) if element):
            raise RuntimeError("The Go to Folder target changed owner after the path write; inspect the dialog before continuing.")
        if keyboard:
            _check_stop()
            _press_key_sync(36, 0)
        elif not _ax_perform_action(target, action):
            raise RuntimeError("The Go to Folder confirmation could not be verified; inspect the dialog before continuing.")

        # Prove closure from the retained outer panel's direct children. A
        # partial generic screenshot/tree is never evidence of sheet absence.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            _check_stop()
            children = _ax_read_attr_ptr(panel, b"AXChildren")
            if children:
                try:
                    count = _cf.CFArrayGetCount(ctypes.c_void_p(children))
                    closed = (0 <= count <= 200 and _ax_matches_pid(panel, pid)
                              and _ax_str(panel, b"AXRole") == "AXSheet"
                              and (_ax_str(panel, b"AXDescription") or _ax_str(panel, b"AXTitle")).strip().casefold() in ("open", "choose"))
                    for index in range(count if closed else 0):
                        _check_stop()
                        if time.monotonic() >= deadline:
                            closed = False
                            break
                        child = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(children), index)
                        if not child or not _ax_matches_pid(child, pid):
                            closed = False
                            break
                        role = _ax_str(child, b"AXRole") if child else ""
                        if not role or role in ("AXSheet", "AXDialog"):
                            closed = False
                    if closed and time.monotonic() < deadline:
                        return path
                finally:
                    _cf.CFRelease(ctypes.c_void_p(children))
            time.sleep(0.05)
        raise RuntimeError("The Go to Folder chooser did not close; inspect the dialog before continuing.")
    except RuntimeError:
        raise
    except Exception:
        if attempted:
            raise RuntimeError("The Go to Folder navigation could not be verified; inspect the dialog before continuing.") from None
        return None
    finally:
        for pointer in context:
            if pointer:
                _cf.CFRelease(ctypes.c_void_p(pointer))


def ax_navigate_save_panel(pid: int, target_dir: str) -> str | None:
    """Select a sidebar row only when its local URL proves the exact requested directory.
    Return None before writing if path evidence is absent; an attempted but
    unverified selection raises so callers cannot assume another navigation is safe.
    """
    from urllib.parse import unquote, urlparse
    target_path = os.path.realpath(os.path.abspath(os.path.expanduser(target_dir)))
    want = os.path.basename(target_path.rstrip("/"))
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
        deadline = time.monotonic() + 1.5
        visited = [0]

        def within_budget():
            """Bound repeated accessibility traversal and stop cancelled requests before writes."""
            _check_stop()
            visited[0] += 1
            return visited[0] <= 600 and time.monotonic() < deadline

        def name_of(e, d=0):
            """Read a sidebar row's bounded visible name without guessing a path from it."""
            if d > 5 or not within_budget():
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

        def where_value(e, d=0, in_panel=False):
            """Read an explicit current-location label; an empty value is no evidence."""
            if d > 18 or not within_budget():
                return None
            role = _ax_str(e, b"AXRole")
            in_panel = in_panel or role in ("AXSheet", "AXDialog")
            if in_panel and role == "AXPopUpButton":
                lbl = (_ax_str(e, b"AXDescription") or _ax_str(e, b"AXTitle") or "")
                if "where" in lbl.lower():
                    return _ax_str(e, b"AXValue")
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    for i in range(min(_cf.CFArrayGetCount(ctypes.c_void_p(ch)), 130)):
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            r = where_value(k, d + 1, in_panel)
                            if r:
                                return r
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))
            return None

        found = {"row": None}  # holds a CFRetain'd row ref (survives array release)

        def row_path(element):
            """Read a real local directory URL; a visible favourite name is never a path."""
            value = _ax_str(element, b"AXURL") or _ax_str(element, b"AXDocument")
            if not value:
                return None
            parsed = urlparse(value)
            if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
                value = unquote(parsed.path)
            elif parsed.scheme or not value.startswith("/"):
                return None
            return os.path.realpath(value)

        def walk(e, in_sidebar, depth, in_panel=False):
            """Select only a bounded sidebar match for later full-path validation."""
            if depth > 16 or found["row"] or not within_budget():
                return
            role = _ax_str(e, b"AXRole")
            if role in ("AXSheet", "AXDialog"):
                in_panel = True
                in_sidebar = False
            if in_panel and role == "AXOutline":
                in_sidebar = True
            if in_sidebar and role == "AXRow" and row_path(e) == target_path:
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
                            walk(k, in_sidebar, depth + 1, in_panel)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, False, 0)
            row = found["row"]
            if not row:
                return None
            try:
                if row_path(row) != target_path:
                    return None
                row_label = name_of(row) or want
                _check_stop()
                a = _cfstr("AXSelected")
                if not a:
                    return None
                try:
                    status = _appserv.AXUIElementSetAttributeValue(
                        ctypes.c_void_p(row), ctypes.c_void_p(a), _kCFBooleanTrue
                    )
                    if status != 0:
                        raise RuntimeError("The save-panel location change failed; inspect the panel before continuing.")
                finally:
                    _cf.CFRelease(ctypes.c_void_p(a))
                time.sleep(0.2)  # let the panel commit the location change
                wv = where_value(app) or ""
                selected = _ax_str(row, b"AXSelected").casefold() == "true"
                if selected and row_path(row) == target_path and wv and wv.casefold() in (want.casefold(), row_label.casefold()):
                    return wv
                raise RuntimeError("The save-panel location change could not be verified; inspect the panel before continuing.")
            finally:
                _cf.CFRelease(ctypes.c_void_p(row))
            return None
        finally:
            _cf.CFRelease(ctypes.c_void_p(app))
    except RuntimeError:
        raise
    except Exception:
        return None


def ax_press_panel_button(pid: int, titles: tuple, substring: bool = False) -> str | None:
    """Press a button inside an open sheet (save/open panel or alert) by title,
    via AX (focus-independent). `titles` is tried in order; `substring=True`
    matches when a wanted string is contained in a button title (for variable
    labels like 'Use ".txt"'). Returns the matched button's title, else None."""
    _check_stop()
    wanted = [t.lower() for t in titles]
    candidates = []
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
        state = {"nodes": 0, "complete": True}
        deadline = time.monotonic() + 1.5

        def matches(title: str) -> bool:
            """Check the caller's specific title policy without triggering any action."""
            t = title.lower()
            if substring:
                return any(w in t for w in wanted)
            return t in wanted

        def walk(e: int, depth: int, in_sheet: bool) -> None:
            """Collect all bounded candidates so duplicate titles are rejected before a press."""
            _check_stop()
            state["nodes"] += 1
            if depth > 16 or state["nodes"] > 600 or time.monotonic() >= deadline:
                state["complete"] = False
                return
            role = _ax_str(e, b"AXRole")
            in_sheet = in_sheet or role in ("AXSheet", "AXDialog")
            if in_sheet and role == "AXButton":
                title = _ax_str(e, b"AXTitle")
                if title and matches(title):
                    candidates.append((_cf.CFRetain(ctypes.c_void_p(e)), title))
            ch = _ax_read_attr_ptr(e, b"AXChildren")
            if ch:
                try:
                    n = _cf.CFArrayGetCount(ctypes.c_void_p(ch))
                    if n > 80:
                        state["complete"] = False
                    for i in range(min(n, 80)):
                        if not state["complete"]:
                            break
                        k = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(ch), i)
                        if k:
                            walk(k, depth + 1, in_sheet)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(ch))

        try:
            walk(app, 0, False)
            if not state["complete"] or len(candidates) != 1:
                return None
            element, title = candidates[0]
            _check_stop()
            if _ax_str(element, b"AXRole") != "AXButton" or _ax_str(element, b"AXTitle") != title:
                return None
            return title if _ax_press(element) else None
        finally:
            for element, title in candidates:
                _cf.CFRelease(ctypes.c_void_p(element))
            _cf.CFRelease(ctypes.c_void_p(app))
    except RuntimeError:
        raise
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


def ax_read_alert(pid: int, include_save_panel: bool = False) -> dict | None:
    """Read an alert sheet's message + buttons, if one is open — e.g. the error
    macOS raises when a save is refused ("you don't have permission", "the
    volume is read-only") or a confirmation ("you used the extension .txt …").
    Returns {'text': str, 'buttons': [str]} for the alert, or None. Excludes the
    save/open panel itself (which carries a 'Save As:' field), so the caller can
    tell a real alert apart from the panel and surface — not silently dismiss —
    the reason a save failed. Set include_save_panel=True when verifying that
    a save has completed: an open Save As panel then returns save_panel=True."""
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
                if is_alert or (include_save_panel and has_save_as):
                    found[0] = {"text": msg[:500], "buttons": buttons}
                    if has_save_as:
                        found[0]["save_panel"] = True
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
    window_id: int | None = None,
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
            focused_ptr = (_ax_exact_window(pid, window_id) if window_id is not None
                           else _ax_read_attr_ptr(app_ptr, b"AXFocusedWindow"))
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
    """Replace the clipboard only while the current input request remains permitted."""
    _check_stop()
    # timeout so a contended/stuck pasteboard server (e.g. behind a modal sheet)
    # fails fast instead of hanging the tool for minutes (was unbounded).
    subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)


def set_clipboard_image(image_path: str) -> str:
    """Load a PNG file into the system clipboard. Returns the resolved absolute path."""
    path = os.path.abspath(os.path.expanduser(image_path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"image not found: {path}")
    _check_stop()
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = f'set the clipboard to (read (POSIX file "{escaped}") as «class PNGf»)'
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"clipboard image write failed: {err}")
    return path


def get_clipboard() -> str:
    """Read clipboard text with a bounded subprocess timeout."""
    # timeout so a contended/stuck pasteboard server fails fast (was unbounded).
    result = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, check=True, timeout=5)
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
    _check_stop()
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
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        # AppleScript "Can't get menu item …" → the path was wrong.
        raise RuntimeError(f"click_menu {' > '.join(path)} failed: {err}")


def set_window_bounds(pid: int, x: int, y: int, width: int | None = None, height: int | None = None) -> None:
    """Move (and optionally resize) the frontmost window of a process via System Events."""
    _check_stop()
    cmd = ["/usr/bin/osascript", "-e",
           f'tell application "System Events" to tell (first process whose unix id is {pid}) '
           f"to set position of window 1 to {{{int(x)}, {int(y)}}}"]
    if width is not None or height is not None:
        width_expr = str(int(width)) if width is not None else "item 1 of (size of window 1)"
        height_expr = str(int(height)) if height is not None else "item 2 of (size of window 1)"
        cmd.extend(["-e",
                    f'tell application "System Events" to tell (first process whose unix id is {pid}) '
                    f"to set size of window 1 to {{{width_expr}, {height_expr}}}"])
    result = subprocess.run(cmd, capture_output=True, timeout=5)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"set_window_bounds failed: {err}")


# ---------------------------------------------------------------------------
# Multi-window: bridge CG window IDs to AX AXWindow refs
# ---------------------------------------------------------------------------
# Prefer Apple's native identity bridge. Older surfaces without it may use a
# unique geometry match; overlapping equal-sized windows must never be guessed.

def _ax_exact_window(pid: int, window_id: int) -> int:
    """Resolve a live window owned by this PID; return a retained AX reference."""
    from . import capture
    win = capture.get_window_by_id(window_id)
    if not win or win['pid'] != pid:
        return 0
    return _ax_window_for_cg_id(pid, window_id, win['x'], win['y'], win['width'], win['height'])


def _ax_element_at(x: float, y: float, expected_pid: int | None = None,
                   window_id: int | None = None, expected_label: str | None = None) -> int:
    """Resolve within the requested window, including when another window covers it.

    App-scoped hit testing is the fast path. A bounded subtree search handles
    overlapping windows of the same app. The caller owns the returned reference.
    """
    root = (_appserv.AXUIElementCreateApplication(expected_pid) if expected_pid is not None
            else _appserv.AXUIElementCreateSystemWide())
    if not root:
        return 0
    window = 0

    def matches_label(element):
        """Require the observed label to remain attached to the selected control."""
        return expected_label is None or any(
            _ax_str_attr(element, attr) == expected_label
            for attr in (b'AXTitle', b'AXDescription', b'AXValue')
        )

    try:
        window = _ax_exact_window(expected_pid, window_id) if window_id is not None and expected_pid is not None else 0
        if window_id is not None and not window:
            return 0
        _appserv.AXUIElementSetMessagingTimeout(ctypes.c_void_p(root), _AX_MESSAGING_TIMEOUT_SECONDS)
        found = ctypes.c_void_p()
        err = _appserv.AXUIElementCopyElementAtPosition(
            ctypes.c_void_p(root), float(x), float(y), ctypes.byref(found))
        if found.value:
            valid = err == 0 and _ax_matches_pid(found.value, expected_pid)
            if valid and window:
                owner = _ax_read_attr_ptr(found.value, b'AXWindow')
                try:
                    valid = bool(owner and _cf.CFEqual(ctypes.c_void_p(owner), ctypes.c_void_p(window)))
                finally:
                    if owner:
                        _cf.CFRelease(ctypes.c_void_p(owner))
            if valid and matches_label(found.value):
                return found.value
            _cf.CFRelease(found)
        if not window:
            return 0
        deadline = time.monotonic() + 0.6
        best, best_depth, visited = 0, -1, 0

        def visit(element, depth):
            """Find the deepest containing control without traversing unrelated windows."""
            nonlocal best, best_depth, visited
            if depth > 30 or visited >= 400 or time.monotonic() >= deadline:
                return
            visited += 1
            pos, size = _ax_cgpoint(element), _ax_cgsize(element)
            contains = bool(pos and size and pos[0] <= x < pos[0] + size[0]
                            and pos[1] <= y < pos[1] + size[1])
            if pos and size and size[0] > 0 and size[1] > 0 and not contains:
                return
            if contains and depth > best_depth and matches_label(element):
                if best:
                    _cf.CFRelease(ctypes.c_void_p(best))
                best = _cf.CFRetain(ctypes.c_void_p(element)) or 0
                best_depth = depth
            children = _ax_read_attr_ptr(element, b'AXChildren')
            if children:
                try:
                    for index in range(min(80, _cf.CFArrayGetCount(ctypes.c_void_p(children)))):
                        if visited >= 400 or time.monotonic() >= deadline:
                            break
                        child = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(children), index)
                        if child:
                            visit(child, depth + 1)
                finally:
                    _cf.CFRelease(ctypes.c_void_p(children))
        try:
            visit(window, 0)
            return best
        except BaseException:
            if best:
                _cf.CFRelease(ctypes.c_void_p(best))
            raise
    finally:
        if window:
            _cf.CFRelease(ctypes.c_void_p(window))
        _cf.CFRelease(ctypes.c_void_p(root))


def _ax_window_for_cg_id(pid: int, target_window_id: int, target_x: float, target_y: float, target_w: float, target_h: float, tolerance: float = 4.0) -> int:
    """
    Find the AXWindow ref corresponding to a CG window_id. Strategy, in order:

    Prefer the native AX-to-CG identity bridge. If it is unavailable, require
    one unique position-and-size match. Similar size or z-order is not identity.

    Caller must CFRelease the returned ref. Returns 0 if no match found.
    """
    from . import capture
    app_ptr = _appserv.AXUIElementCreateApplication(pid)
    if not app_ptr:
        return 0
    try:
        _appserv.AXUIElementSetMessagingTimeout(ctypes.c_void_p(app_ptr), _AX_MESSAGING_TIMEOUT_SECONDS)
        wins_ptr = _ax_read_attr_ptr(app_ptr, b"AXWindows")
        if not wins_ptr:
            return 0
        try:
            count = _cf.CFArrayGetCount(ctypes.c_void_p(wins_ptr))

            try:
                identity = _appserv._AXUIElementGetWindow
                identity.restype = ctypes.c_int32
                identity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            except AttributeError:
                identity = None
            ax_entries = []
            deadline = time.monotonic() + 0.6
            for i in range(count):
                if time.monotonic() >= deadline:
                    return 0  # An incomplete search cannot establish unique geometry.
                win = _cf.CFArrayGetValueAtIndex(ctypes.c_void_p(wins_ptr), i)
                if not win:
                    continue
                if identity is not None:
                    native_id = ctypes.c_uint32()
                    if identity(ctypes.c_void_p(win), ctypes.byref(native_id)) == 0:
                        if native_id.value == target_window_id:
                            return _cf.CFRetain(ctypes.c_void_p(win)) or 0
                        continue  # A known different ID cannot qualify by geometry.
                pos = _ax_cgpoint(win)
                size = _ax_cgsize(win)
                ax_entries.append((win, pos, size))

            def _pos_ok(pos) -> bool:
                return bool(pos) and abs(pos[0] - target_x) <= tolerance and abs(pos[1] - target_y) <= tolerance

            def _size_ok(size) -> bool:
                return bool(size) and abs(size[0] - target_w) <= tolerance and abs(size[1] - target_h) <= tolerance

            # Compatibility for surfaces without native window identity.
            pos_size_matches = [idx for idx, (_, pos, size) in enumerate(ax_entries) if _pos_ok(pos) and _size_ok(size)]
            if len(pos_size_matches) == 1:
                return _cf.CFRetain(ctypes.c_void_p(ax_entries[pos_size_matches[0]][0])) or 0

        finally:
            _cf.CFRelease(ctypes.c_void_p(wins_ptr))
    finally:
        _cf.CFRelease(ctypes.c_void_p(app_ptr))
    return 0


def _ax_set_attr_value(elem: int, attr: bytes, ax_value_ref: int) -> bool:
    """Set an AX attribute (caller owns ax_value_ref). Returns True on success."""
    _check_stop()
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
    """Perform one AX action only while the emergency latch permits input."""
    _check_stop()
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


def ax_set_value_at(x: float, y: float, text: str, expected_pid: int | None = None,
                    window_id: int | None = None) -> dict:
    """
    Try to set the AXValue of the element at (x, y) to `text` — the fully
    invisible alternative to click+Cmd+A+paste for native text inputs.

    Cascade: resolve element → reject if web-area-rooted → reject if role
    isn't a known text input → reject if AXValue isn't settable → set → read
    back. Rejections before writing permit a fallback. An uncertain write raises
    or returns attempted=True, verified=False; the caller must not repeat input.

    Returns:
      {ok: True,  role, status: "set", via: "ax_set_value"}            on success
      {ok: False, status: "no_element"}                                no AX element at coord
      {ok: False, role, status: "web_backed"}                          inside AXWebArea — JS won't see set
      {ok: False, role, status: "not_text_input"}                      role isn't in _AX_TEXT_INPUT_ROLES
      {ok: False, role, status: "not_settable"}                        AXValue is read-only on this element
      {ok: False, role, status: "unverified", attempted: True}        accepted write differs on readback
    """
    _check_stop()
    elem_ref = ctypes.c_void_p(_ax_element_at(x, y, expected_pid, window_id))
    if not elem_ref.value:
        return {"ok": False, "status": "no_element"}

    try:
        if not _ax_matches_pid(int(elem_ref.value), expected_pid):
            return {"ok": False, "status": "wrong_app", "error": "The AX target belongs to another app; no action was sent."}
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

        cfstr = _cfstr(text)
        if not cfstr:
            return {"ok": False, "role": role, "status": "set_failed", "err": "cfstring_alloc"}
        try:
            ok = _ax_set_attr_value(int(elem_ref.value), b"AXValue", cfstr)
        finally:
            _cf.CFRelease(ctypes.c_void_p(cfstr))
        if not ok:
            raise RuntimeError('The accessibility write failed; its effect is unknown. Observe before retrying.')
        # Acceptance alone is not evidence that the native control changed.
        value_ref = _ax_read_attr_ptr(int(elem_ref.value), b'AXValue')
        verified = False
        if value_ref:
            try:
                verified = _cftype_to_str(value_ref) == text
            except ValueError as error:
                raise RuntimeError("The accessibility write was accepted, but its complete value could not be verified. Observe before retrying.") from error
            finally:
                _cf.CFRelease(ctypes.c_void_p(value_ref))
        return {"ok": verified, "role": role, "status": "set" if verified else "unverified",
                "attempted": True, "verified": verified, "via": "ax_set_value"}
    finally:
        _cf.CFRelease(elem_ref)


def _verify_focused_window(pid: int, target_x: float, target_y: float, target_w: float, target_h: float, tolerance: float = 4.0, window_id: int | None = None) -> bool:
    """
    Read the app's AXFocusedWindow and compare exact identity when available,
    otherwise geometry. This is the post-condition for raise_window: even if AXRaise
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
            if window_id is not None:
                target = _ax_exact_window(pid, window_id)
                try:
                    return bool(target and _cf.CFEqual(ctypes.c_void_p(focused_ptr), ctypes.c_void_p(target)))
                finally:
                    if target:
                        _cf.CFRelease(ctypes.c_void_p(target))
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
            float(win["width"]), float(win["height"]), window_id=window_id,
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
        focused   — True iff AXFocusedWindow matches the exact target window.
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
        focused = _verify_focused_window(pid, tx, ty, tw, th, window_id=window_id)
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
        if _verify_focused_window(pid, tx, ty, tw, th, window_id=window_id):
            return {"ok": True, "window_id": window_id, "via": "ax", "focused": True}

        # Post-condition failed: AXRaise returned ok=True (or False) but the
        # focused window is still something else. Retry once after a longer settle.
        _ax_perform_action(ax_win, b"AXRaise")
        await asyncio.sleep(0.08)
        if _verify_focused_window(pid, tx, ty, tw, th, window_id=window_id):
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
    if not win or win.get("pid") != pid:
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

        if width is not None or height is not None:
            width = width if width is not None else int(win["width"])
            height = height if height is not None else int(win["height"])
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

def _send_mouse_event(kind: int, point: CGPoint, button: int, flags: int = 0, click_state: int = 1) -> None:
    """Build an owned mouse event with explicit flags before posting it once."""
    event = _cg.CGEventCreateMouseEvent(None, kind, point, button)
    if not event:
        raise RuntimeError("Mouse event could not be created; input was interrupted.")
    try:
        _cg.CGEventSetFlags(ctypes.c_void_p(event), flags)
        _cg.CGEventSetIntegerValueField(ctypes.c_void_p(event), kCGMouseEventClickState, click_state)
    except BaseException:
        _cf.CFRelease(ctypes.c_void_p(event))
        raise
    _post(event)

async def move_cursor(x: int, y: int) -> None:
    """Move the global cursor only after input ownership's serialized delivery permits it."""
    _check_stop()
    async with _input_lock:
        _check_stop()
        pt = CGPoint(x=float(x), y=float(y))
        ev = _cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, pt, kCGMouseButtonLeft)
        _post(ev)


def _modifier_flags(modifiers: list[str] | None) -> int:
    """Combine supported named modifier flags for explicit per-event stamping."""
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
    """Deliver one visible click while retaining its release through stop, cancel, and exit."""
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        if button == "right":
            down_t, up_t, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        else:
            down_t, up_t, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        flags = _modifier_flags(modifiers)
        token = ("mouse", None, btn)
        _begin_input(token, lambda: _send_mouse_event(down_t, pt, btn, flags),
                     lambda: _send_mouse_event(up_t, pt, btn, flags))
        try:
            await asyncio.sleep(0.005)
        finally:
            # Cancellation must release the button before relinquishing input.
            _finish_input(token)


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
        token = ("mouse", None, btn)
        _begin_input(token, lambda: _send_mouse_event(down_t, pt, btn),
                     lambda: _send_mouse_event(up_t, pt, btn))
        try:
            # Hold. Caller-provided duration; use small interval so an emergency
            # stop can interrupt during a long hold without waiting for the full
            # sleep.
            elapsed = 0.0
            step = 0.05
            while elapsed < duration:
                _check_stop()
                await asyncio.sleep(min(step, duration - elapsed))
                elapsed += step
        finally:
            _finish_input(token)


async def double_click(x: int, y: int, modifiers: list[str] | None = None) -> None:
    """Deliver a double click while rechecking stop before each separate press."""
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        flags = _modifier_flags(modifiers)
        for click_state in (1, 2):
            token = ("mouse", None, kCGMouseButtonLeft)
            _begin_input(token, lambda: _send_mouse_event(kCGEventLeftMouseDown, pt, kCGMouseButtonLeft, flags, click_state),
                         lambda: _send_mouse_event(kCGEventLeftMouseUp, pt, kCGMouseButtonLeft, flags, click_state))
            try:
                await asyncio.sleep(0.02)
            finally:
                # Preserve click-state and modifiers even when cancelled mid-pair.
                _finish_input(token)
            await asyncio.sleep(0.02)


async def triple_click(x: int, y: int, modifiers: list[str] | None = None) -> None:
    """Three down/up pairs with click_state 1/2/3 — apps see a real triple click
    (paragraph select in text views, full-content select in single-line fields)."""
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        flags = _modifier_flags(modifiers)
        for click_state in (1, 2, 3):
            token = ("mouse", None, kCGMouseButtonLeft)
            _begin_input(token, lambda: _send_mouse_event(kCGEventLeftMouseDown, pt, kCGMouseButtonLeft, flags, click_state),
                         lambda: _send_mouse_event(kCGEventLeftMouseUp, pt, kCGMouseButtonLeft, flags, click_state))
            try:
                await asyncio.sleep(0.02)
            finally:
                # Preserve click-state and modifiers even when cancelled mid-pair.
                _finish_input(token)
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Keyboard input
# ---------------------------------------------------------------------------

def _check_frontmost(expected_pid: int | None) -> None:
    """Refuse global typing after the foreground app changes or cannot be verified."""
    _check_stop()
    if expected_pid is not None and not is_frontmost_app(expected_pid):
        raise RuntimeError("The foreground app changed; no further keys were sent. Inspect the dialog before continuing.")


async def press_key(key_string: str, pid: int | None = None, *, expected_frontmost_pid: int | None = None) -> None:
    """Deliver one parsed key and recheck stop after any renderer settling delay."""
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
        _check_frontmost(expected_frontmost_pid)
        _press_key_sync(keycode, flags, pid)


async def hold_key(
    key_string: str,
    duration: float,
    pid: int | None = None,
    *,
    expected_frontmost_pid: int | None = None,
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
        _check_frontmost(expected_frontmost_pid)
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
                    _check_frontmost(expected_frontmost_pid)
                    _key_down_sync(keycode, flags, pid)
        finally:
            # Always release, even on _check_stop interrupt — leaving a key
            # stuck down would be a much worse failure mode than a partial
            # hold.
            _key_up_sync(keycode, flags, pid)


async def press_keys(keys: list[str], pid: int | None = None, *, expected_frontmost_pid: int | None = None) -> None:
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
            _check_frontmost(expected_frontmost_pid)
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
        def emit(is_down):
            """Construct the matching system event; releasing bypasses the engaged stop."""
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
        token = ("system", code)
        _begin_input(token, lambda: emit(True), lambda: emit(False))
        try:
            await asyncio.sleep(0.01)
        finally:
            _finish_input(token)


# The user's pasteboard contents captured before a paste, pending restore.
# Held at module scope (not a local) so the atexit safety net can flush it if
# the process exits inside the post-paste restore window. None = nothing pending.
_clipboard_snapshot: list | None = None
_clipboard_change_count: int | None = None


def _snapshot_pasteboard() -> tuple[list, int] | None:
    """Capture the general pasteboard's full typed contents so a paste can be
    undone byte-for-byte — preserving images, files, RTF, or an empty
    clipboard, not just plain text (pbpaste silently flattens all of those to
    ''). Returns (NSPasteboardItem copies, change count), or None when a stable
    snapshot could not be captured; the caller then leaves the clipboard intact."""
    try:
        from AppKit import NSPasteboard, NSPasteboardItem  # lazy, like NSEvent
        pb = NSPasteboard.generalPasteboard()
        change_count = pb.changeCount()
        snapshot = []
        for item in (pb.pasteboardItems() or []):
            copy = NSPasteboardItem.alloc().init()
            for t in (item.types() or []):
                data = item.dataForType_(t)
                if data is not None:
                    copy.setData_forType_(data, t)
            snapshot.append(copy)
        return (snapshot, change_count) if pb.changeCount() == change_count else None
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
    exits while a paste is awaiting clipboard restoration."""
    global _clipboard_snapshot, _clipboard_change_count
    snapshot, change_count = _clipboard_snapshot, _clipboard_change_count
    # Detach pending state first so signal cleanup and atexit cannot restore twice.
    _clipboard_snapshot = _clipboard_change_count = None
    if snapshot is None or change_count is None:
        return
    try:
        from AppKit import NSPasteboard
        if NSPasteboard.generalPasteboard().changeCount() == change_count:
            _restore_pasteboard(snapshot)
    except Exception:
        pass


atexit.register(_flush_clipboard_restore)


async def type_text(text: str, pid: int | None = None, *, expected_frontmost_pid: int | None = None) -> None:
    """Paste while preserving every clipboard type, including on failure or cancellation."""
    from AppKit import NSPasteboard
    global _clipboard_snapshot, _clipboard_change_count
    _check_stop()
    async with _input_lock:
        _check_stop()
        preserved = _snapshot_pasteboard()
        if preserved is None:
            raise RuntimeError("Could not preserve the clipboard; use mode='keys' or try again.")
        snapshot, captured_count = preserved
        pb = NSPasteboard.generalPasteboard()
        change_count = pb.changeCount()
        if change_count != captured_count:
            raise RuntimeError("The clipboard changed before the paste; nothing was written. Try again or use mode='keys'.")
        _clipboard_snapshot = snapshot
        _clipboard_change_count = None  # An in-flight pbcopy has no verified generation yet.
        try:
            _check_frontmost(expected_frontmost_pid)
            subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
            change_count = pb.changeCount()
            _clipboard_change_count = change_count
            await asyncio.sleep(0.005)
            _check_frontmost(expected_frontmost_pid)
            _paste_sync(pid)
            # Finish restoration before the next tool can intentionally replace
            # the clipboard. This also bounds cleanup after cancelled pastes.
            await asyncio.sleep(0.15)
        finally:
            # A real copy made by the user during the paste takes precedence.
            if pb.changeCount() == change_count:
                _restore_pasteboard(snapshot)
            _clipboard_snapshot = None
            _clipboard_change_count = None


async def type_text_char_by_char(text: str, pid: int | None = None, *, expected_frontmost_pid: int | None = None) -> None:
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
            _check_frontmost(expected_frontmost_pid)
            keycode, flags = char_to_keycode(char)
            if keycode is not None:
                _press_key_sync(keycode, flags, pid)
            else:
                encoded = char.encode("utf-16-le")
                units = len(encoded) // 2
                uni = (ctypes.c_uint16 * units).from_buffer_copy(encoded)
                token = ("unicode", pid)
                _begin_input(token, lambda: _send_unicode_event(uni, units, True, pid),
                             lambda: _send_unicode_event(uni, units, False, pid))
                try:
                    time.sleep(0.005)
                finally:
                    _finish_input(token)
            await asyncio.sleep(0.015)


# ---------------------------------------------------------------------------
# Drag and drop
# ---------------------------------------------------------------------------

def _send_unicode_event(units_pointer, units: int, is_down: bool, pid: int | None) -> None:
    """Deliver every UTF-16 code unit with balanced keyboard flags and native ownership."""
    event = _cg.CGEventCreateKeyboardEvent(None, 0, is_down)
    if not event:
        raise RuntimeError("Unicode keyboard event could not be created; input was interrupted.")
    try:
        _cg.CGEventSetFlags(ctypes.c_void_p(event), 0)
        _cg.CGEventKeyboardSetUnicodeString(ctypes.c_void_p(event), units, ctypes.cast(units_pointer, ctypes.c_void_p))
    except BaseException:
        _cf.CFRelease(ctypes.c_void_p(event))
        raise
    (_post_to_pid(pid, event) if pid else _post(event))

async def drag(
    x1: int, y1: int,
    x2: int, y2: int,
    steps: int = 20,
    step_delay: float = 0.010,
    hover_target_seconds: float = 0.0,
    *,
    button: str = "left",
    modifiers: list[str] | None = None,
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
        if button == "right":
            down, up, dragged, btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventRightMouseDragged, kCGMouseButtonRight
        elif button == "left":
            down, up, dragged, btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged, kCGMouseButtonLeft
        else:
            raise ValueError("Drag button must be left or right; no input was sent.")
        flags = _modifier_flags(modifiers)

        last_point = [src]
        token = ("mouse", None, btn)
        _begin_input(token, lambda: _send_mouse_event(down, src, btn, flags),
                     lambda: _send_mouse_event(up, last_point[0], btn, flags))
        try:
            await asyncio.sleep(0.05)

            for i in range(1, steps + 1):
                _check_stop()
                t = i / steps
                pt = CGPoint(
                    x=x1 + (x2 - x1) * t,
                    y=y1 + (y2 - y1) * t,
                )
                _check_stop()
                _send_mouse_event(dragged, pt, btn, flags)
                last_point[0] = pt
                await asyncio.sleep(step_delay)

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
                    _check_stop()
                    # Re-emit a dragged event at the target to keep the OS-side
                    # hover state alive — some apps drop the spring trigger if no
                    # events arrive for too long.
                    _send_mouse_event(dragged, dst, btn, flags)
                    last_point[0] = dst
            else:
                await asyncio.sleep(0.02)
        finally:
            _finish_input(token)


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

async def scroll(x: int, y: int, direction: str, amount: int, modifiers: list[str] | None = None) -> None:
    """Deliver visible scroll with explicit modifiers after the caller verifies targeting."""
    _check_stop()
    async with _input_lock:
        pt = CGPoint(x=float(x), y=float(y))
        ev_move = _cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, pt, kCGMouseButtonLeft)
        _post(ev_move)
        await asyncio.sleep(0.01)
        _check_stop()

        if direction in ("up", "down"):
            delta = amount if direction == "up" else -amount
            ev = _cg.CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, delta)
        else:
            delta = amount if direction == "right" else -amount
            ev = _cg.CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, 0)

        if not ev:
            raise RuntimeError("Scroll event could not be created; no scroll was sent.")
        try:
            if direction not in ("up", "down"):
                _cg.CGEventSetIntegerValueField(ctypes.c_void_p(ev), kCGScrollWheelEventDeltaAxis2, delta)
            _cg.CGEventSetFlags(ctypes.c_void_p(ev), modifier_flags_from_list(modifiers))
        except BaseException:
            _cf.CFRelease(ctypes.c_void_p(ev))
            raise
        _post(ev)


# Start global emergency stop listener on import
_start_emergency_stop_tap()
