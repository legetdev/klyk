"""
SkyLight private-framework binding for invisible mouse input.

Wraps `SLEventPostToPid` from `/System/Library/PrivateFrameworks/SkyLight.framework`
so klyk can deliver mouse-down / mouse-up / mouse-dragged / scroll-wheel events
directly to a target PID and window — without warping the global cursor, without
raising the window, and without stealing focus from whatever the user is currently
in.

Public API:

    is_available()                                     -> bool
    post_mouse_click(pid, window_id, x, y,
                     button="left", modifier_flags=0,
                     primer_first=False)               -> bool
    post_double_click(pid, window_id, x, y,
                      modifier_flags=0,
                      primer_first=False)              -> bool
    post_triple_click(pid, window_id, x, y,
                      modifier_flags=0,
                      primer_first=False)              -> bool
    post_drag(pid, window_id, x1, y1, x2, y2,
              steps=20, step_delay=0.010,
              button="left", modifier_flags=0,
              primer_first=False)                      -> bool
    post_scroll(pid, window_id, x, y,
                direction, amount=3,
                modifier_flags=0)                      -> bool

Coordinate convention: window-local, **top-left origin** (matches klyk's
existing screenshot / click coordinate space). The Y axis is not flipped
inside this module; the SkyLight layer interprets `SLEventSetWindowLocation`
input as top-left and the NSView callback's bottom-left reporting is a
separate AppKit-side concern documented in the Phase 2 verification memo.

Modifier flags: caller passes a CGEventFlags bitmask (e.g.
`klyk.keycodes.MODIFIER_FLAGS['cmd'] | MODIFIER_FLAGS['shift']`). We stamp
the flags onto every event in the sequence — down, up, intermediate drag
moves, scroll. macOS shortcut resolution (Cmd+click → open in new tab,
Shift+click → range select) reads the flags off the click events the same
way regardless of whether they came through the HID tap or via PostToPid.

Why this exists: `CGEventPostToPid` (public) drops events silently for
non-foreground targets and the cursor warps if you fall back to
`CGEventPost(kCGHIDEventTap, ev)`. `SLEventPostToPid` is the private path
the public open-source clients (cua-driver, termcanvas, openclicky, yabai)
use to route a CGEvent into the WindowServer for a specific PID + window
without touching the cursor — but only when the event is pre-stamped with
target PID, target window number, and a window-local CGPoint. This module
encodes that full stamping recipe.

Empirically verified on macOS 25.3.0 against an in-process AppKit sink and
two third-party PIDs (Finder, Chrome) — see `PHASE_2_VERIFY_SKYLIGHT.md`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import asyncio
import logging
import struct
import threading
import time
from ctypes import c_double, c_int32, c_int64, c_uint32, c_uint64, c_void_p

from . import connection_gate as _connection_gate

log = logging.getLogger("klyk.skylight")

# ---------------------------------------------------------------------------
# CGPoint — pass-by-value struct that both CG and SkyLight take by value
# ---------------------------------------------------------------------------

class CGPoint(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


# ProcessSerialNumber — Carbon Process Manager handle, needed to route the
# key-window events (make_window_key) to a specific process. Same layout as
# klyk/computer.py's _PSN.
class _PSN(ctypes.Structure):
    _fields_ = [("hi", c_uint32), ("lo", c_uint32)]


# ---------------------------------------------------------------------------
# Framework loading — guarded so the module imports cleanly even if SkyLight
# isn't present (future macOS may rename or remove it; we want a clean
# fallback, not an ImportError on a leaf module).
# ---------------------------------------------------------------------------

_AVAILABLE = False
_cg = None
_cf = None
_sl = None
_as = None

# Separate availability flag for the key-window routing primitive
# (make_window_key). Independent of _AVAILABLE so that if only these extra
# symbols are missing on some future macOS, invisible clicks still work — they
# just deliver as a raw backgrounded click (which interacts with buttons/menus
# but not key-window-dependent controls) instead of a keyed click.
_KEYWIN_AVAILABLE = False

# Tri-state cache for the delivery self-test (see self_test()):
#   None  = not yet run, or inconclusive (couldn't build the test harness)
#   True  = a stamped click was actually delivered on this macOS build
#   False = SkyLight loaded but delivery is broken (the silent-failure mode)
# Callers gate the seamless path on delivery_verified() being non-False.
_DELIVERY_VERIFIED = None

try:
    _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _sl = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")

    # CoreGraphics — event construction + per-field stamping. Restypes /
    # argtypes set explicitly so ctypes doesn't truncate pointers on 64-bit.
    _cg.CGEventCreateMouseEvent.restype = c_void_p
    _cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
    _cg.CGEventCreateScrollWheelEvent.restype = c_void_p
    # ScrollWheelEvent is variadic in C; ctypes calls it with a fixed wheel1
    # arg here (1 wheel, vertical). Horizontal scroll is set via
    # CGEventSetIntegerValueField on the kCGScrollWheelEventDeltaAxis2 slot
    # after creation, matching the recipe in klyk/computer.py.
    _cg.CGEventCreateScrollWheelEvent.argtypes = [c_void_p, c_uint32, c_uint32, c_int32]
    _cg.CGEventSetIntegerValueField.restype = None
    _cg.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int64]
    _cg.CGEventSetDoubleValueField.restype = None
    _cg.CGEventSetDoubleValueField.argtypes = [c_void_p, c_uint32, c_double]
    _cg.CGEventSetFlags.restype = None
    _cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]

    # CoreFoundation — release the CGEvent refs we allocate. CGEvent is a
    # CFType, so CFRelease is the correct cleanup path.
    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [c_void_p]

    # SkyLight (private) — the routing primitive + the window-local point
    # stamper. Argument order verified empirically; the 3-arg
    # (conn, pid, ev) variant some blog posts suggest segfaults.
    _sl.SLEventPostToPid.restype = None
    _sl.SLEventPostToPid.argtypes = [c_int32, c_void_p]
    _sl.SLEventSetWindowLocation.restype = None
    _sl.SLEventSetWindowLocation.argtypes = [c_void_p, CGPoint]

    _AVAILABLE = True
except (OSError, AttributeError) as e:
    # OSError = framework not loadable; AttributeError = symbol missing.
    # In either case the public API degrades to is_available()=False and
    # the post_* functions return False — the caller picks the legacy CG path.
    log.warning("skylight: unavailable (%s)", e)

# Key-window routing primitives — bound separately so a missing symbol on a
# future macOS disables ONLY the keyed-click upgrade, not all invisible input.
try:
    if _AVAILABLE:
        # SLPSPostEventRecordTo(ProcessSerialNumber*, void* event_record) —
        # yabai's make_key_window primitive. Argtypes match the proven call
        # convention (both pointers passed as c_void_p via byref / array decay).
        _sl.SLPSPostEventRecordTo.restype = c_int32
        _sl.SLPSPostEventRecordTo.argtypes = [c_void_p, c_void_p]
        _as = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        _as.GetProcessForPID.restype = c_int32
        _as.GetProcessForPID.argtypes = [c_int32, ctypes.POINTER(_PSN)]
        _KEYWIN_AVAILABLE = True
except (OSError, AttributeError) as e:
    log.warning("skylight: key-window routing unavailable (%s)", e)


# ---------------------------------------------------------------------------
# CGEvent constants — mouse-button event types + button index. Public from
# CGEventTypes.h. Named here so the post path doesn't carry bare integers.
# ---------------------------------------------------------------------------

_kCGEventLeftMouseDown    = 1
_kCGEventLeftMouseUp      = 2
_kCGEventRightMouseDown   = 3
_kCGEventRightMouseUp     = 4
_kCGEventMouseMoved       = 5
_kCGEventLeftMouseDragged = 6
_kCGEventRightMouseDragged = 7
_kCGEventScrollWheel      = 22

_kCGMouseButtonLeft  = 0
_kCGMouseButtonRight = 1

_kCGScrollEventUnitLine = 1


# ---------------------------------------------------------------------------
# CGEventField stamp slots — the minimum set the WindowServer requires to
# accept a posted-to-PID event for delivery to a specific window. Public
# numeric values from CGEventTypes.h; the "TargetWindow" field (51) is
# documented in some Apple headers and used by every working OSS client.
#
# Without this stamping, SLEventPostToPid silently no-ops — the cursor
# doesn't move (good) but no event reaches the target either (bad). Stamping
# every field below is what flips the no-op into a delivered click.
# ---------------------------------------------------------------------------

_FIELD_MOUSE_EVENT_CLICK_STATE  = 1    # kCGMouseEventClickState — 1 / 2 / 3 for single / double / triple click
_FIELD_EVENT_PRESSURE           = 34   # kCGMouseEventPressure — 1.0 for down, 0.0 for up
_FIELD_TARGET_UNIX_PID          = 39   # kCGEventTargetUnixProcessID — route to this PID
_FIELD_SOURCE_UNIX_PID          = 41   # kCGEventSourceUnixProcessID — set same as target
_FIELD_EVENT_TARGET_WINDOW      = 51   # kCGEventTargetWindow — CGWindowID of the target window
_FIELD_WINDOW_UNDER_POINTER     = 91   # kCGMouseEventWindowUnderMousePointer
_FIELD_WINDOW_UNDER_POINTER_OK  = 92   # kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent
_FIELD_SCROLL_DELTA_AXIS_2      = 12   # kCGScrollWheelEventDeltaAxis2 — horizontal scroll delta


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _stamp_routing(ev: int, pid: int, window_id: int) -> None:
    """
    Stamp the PID/window routing fields that SkyLight delivery requires.
    Applied to every event type (mouse-down/up/move/drag, scroll wheel).
    """
    ev_p = c_void_p(ev)
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_TARGET_UNIX_PID, c_int64(pid))
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_SOURCE_UNIX_PID, c_int64(pid))
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_EVENT_TARGET_WINDOW, c_int64(window_id))
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_WINDOW_UNDER_POINTER, c_int64(window_id))
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_WINDOW_UNDER_POINTER_OK, c_int64(window_id))


def _stamp_mouse_event(
    ev: int,
    pid: int,
    window_id: int,
    is_down: bool,
    x: float,
    y: float,
    modifier_flags: int = 0,
    click_state: int = 1,
) -> None:
    """
    Full stamping pass for a mouse event (down / up / dragged / moved).
    Applies routing, pressure, window-local point, modifier flags, and
    click state in one place so every event type leaves the function in
    the same accept-ready state.
    """
    ev_p = c_void_p(ev)
    _stamp_routing(ev, pid, window_id)
    _cg.CGEventSetDoubleValueField(ev_p, _FIELD_EVENT_PRESSURE, c_double(1.0 if is_down else 0.0))
    _cg.CGEventSetIntegerValueField(ev_p, _FIELD_MOUSE_EVENT_CLICK_STATE, c_int64(click_state))
    _cg.CGEventSetFlags(ev_p, c_uint64(modifier_flags))
    # Window-local point via the private SkyLight stamper. Top-left origin —
    # klyk's convention everywhere else. The NSView callback re-reports it
    # in bottom-left coords but the routing layer interprets top-left.
    _sl.SLEventSetWindowLocation(ev_p, CGPoint(float(x), float(y)))


def _button_event_types(button: str) -> tuple[int, int, int, int]:
    """Resolve (down_type, up_type, dragged_type, button_index) for a button name."""
    if button == "right":
        return (
            _kCGEventRightMouseDown,
            _kCGEventRightMouseUp,
            _kCGEventRightMouseDragged,
            _kCGMouseButtonRight,
        )
    # Default to left for any other value — matches klyk/computer.py click().
    return (
        _kCGEventLeftMouseDown,
        _kCGEventLeftMouseUp,
        _kCGEventLeftMouseDragged,
        _kCGMouseButtonLeft,
    )


def _post_event(pid: int, ev: int) -> None:
    """Post a single CGEvent via SkyLight. Caller owns release."""
    _sl.SLEventPostToPid(c_int32(pid), c_void_p(ev))


def _release(ev: int) -> None:
    """Balance the caller's retained native event after every delivery outcome."""
    if ev:
        _cf.CFRelease(c_void_p(ev))


def _check_stop() -> None:
    """Share the input worker's cancellation and physical-stop checkpoints."""
    from .computer import _check_stop as check
    check()


def _begin_input(token, press, release) -> None:
    """Register invisible button release in the same exit cleanup registry as visible input."""
    from .computer import _begin_input as begin
    begin(token, press, release)


def _finish_input(token) -> None:
    """Release an invisible button exactly once, including after worker cancellation."""
    from .computer import _finish_input as finish
    finish(token)


def _post_stamped_pair(
    pid: int,
    window_id: int,
    x: float,
    y: float,
    button: str,
    modifier_flags: int = 0,
    click_state: int = 1,
) -> None:
    """
    Build a mouse-down + mouse-up pair, fully field-stamped for SkyLight
    delivery, post them with a 5 ms inter-event gap, release the events.
    """
    down_type, up_type, _drag_type, btn_index = _button_event_types(button)
    placeholder = CGPoint(0.0, 0.0)
    ev_down = _cg.CGEventCreateMouseEvent(None, down_type, placeholder, btn_index)
    ev_up   = _cg.CGEventCreateMouseEvent(None, up_type,   placeholder, btn_index)
    try:
        if not ev_down or not ev_up:
            raise RuntimeError("Invisible mouse events could not be created; no click was sent.")
        _stamp_mouse_event(ev_down, pid, window_id, True,  x, y, modifier_flags, click_state)
        _stamp_mouse_event(ev_up,   pid, window_id, False, x, y, modifier_flags, click_state)
        token = ("skylight", pid, window_id, btn_index)
        _begin_input(token, lambda: _post_event(pid, ev_down), lambda: _post_event(pid, ev_up))
        try:
            time.sleep(0.005)
        finally:
            _finish_input(token)
    finally:
        _release(ev_down)
        _release(ev_up)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """
    Returns True if SkyLight.framework loaded and the required private
    symbols resolved at module-import time. Safe to call from anywhere;
    never raises.
    """
    return _AVAILABLE


def keywin_available() -> bool:
    """
    True if the key-window routing primitive (make_window_key) resolved its
    private symbols. When False, invisible clicks still work — they deliver as
    a raw backgrounded click instead of a keyed one — so callers treat this as
    an optional upgrade, never a hard requirement. Never raises.
    """
    return _KEYWIN_AVAILABLE


def make_window_key(pid: int, window_id: int) -> bool:
    """
    Make the target window the KEY window for input routing — WITHOUT raising
    it, changing its z-order, switching Spaces, or changing the OS-active app
    (no focus theft). This is yabai's `make_key_window` pattern: two
    `SLPSPostEventRecordTo` events carrying a reverse-engineered event record.

    Why klyk needs it: a raw SkyLight click delivered to a backgrounded native
    window fires simple controls (buttons, menu items) but does NOT drive
    key-window-dependent ones — text-field caret placement, list/table/sidebar
    row selection — because AppKit routes those only inside the key window.
    Calling this immediately before the click makes those controls interact
    while the user's foreground app, window stack, and Space stay exactly as
    they were. Verified empirically (2026-07-06): a button AND a text field in a
    backgrounded window both interact after this call, with zero change to the
    active app and zero window raise (6/6 reproducible), whereas the fuller
    `_SLPSSetFrontProcessWithOptions` variant steals keyboard focus and is
    deliberately NOT used here.

    Coordinates / raising are untouched — this only flips key-window state.

    Returns True if the events were posted, False if the primitive isn't
    available on this macOS (caller then delivers a raw click, which still
    works for simple controls). Never raises.
    """
    if not _KEYWIN_AVAILABLE:
        return False
    _check_stop()
    try:
        psn = _PSN()
        if _as.GetProcessForPID(int(pid), ctypes.byref(psn)) != 0:
            return False
        # Reverse-engineered 0xf8-byte event record (yabai window_manager.c →
        # window_manager_make_key_window). Field offsets are load-bearing.
        b = (ctypes.c_uint8 * 0xf8)()
        b[0x04] = 0xf8
        b[0x3a] = 0x10
        struct.pack_into("<I", b, 0x3c, int(window_id) & 0xffffffff)
        for i in range(0x20, 0x30):
            b[i] = 0xff
        b[0x08] = 0x01
        _check_stop()
        _sl.SLPSPostEventRecordTo(ctypes.byref(psn), b)
        b[0x08] = 0x02
        _sl.SLPSPostEventRecordTo(ctypes.byref(psn), b)
        return True
    except Exception as e:  # never let a routing hiccup break the click path
        log.warning("skylight.make_window_key: %s: %s", type(e).__name__, e)
        return False


def delivery_verified():
    """
    Result of the most recent delivery self_test(), as a tri-state:
      True  — a stamped click was confirmed delivered on this macOS build
      False — SkyLight loaded but delivery is broken (silent-failure mode)
      None  — self_test() hasn't run, or couldn't build its test harness
    Callers should treat only an explicit False as "skip the SkyLight path";
    None means "unknown, proceed as normal" (fail open). Never raises.
    """
    return _DELIVERY_VERIFIED


# Self-test harness state. The AppKit sink/driver subclasses are defined lazily
# (once per process) by _build_selftest_classes() so importing skylight stays a
# pure-ctypes, AppKit-free operation until the self-test actually runs.
_SINK_CLASS = None
_DRIVER_CLASS = None


def _build_selftest_classes() -> None:
    """Define the sink NSView + driver NSObject subclasses exactly once."""
    global _SINK_CLASS, _DRIVER_CLASS
    if _SINK_CLASS is not None:
        return
    from AppKit import NSView
    from Foundation import NSObject

    class _SLSelfTestSink(NSView):
        """Observe only delivery into this test's own retained offscreen window."""

        def acceptsFirstMouse_(self, _event):
            """Allow the stamped click without activating or making the window key."""
            return True

        def mouseDown_(self, _event):
            """A stale sink callback must not verify a newer test generation."""
            state = getattr(self, "_klyk_selftest", None)
            if (state and state["active"] and not state["cancelled"].is_set()
                    and _connection_gate.valid(state["request"])):
                state["hit"] = True

    class _SLSelfTestDriver(NSObject):
        """Retain the original request through native timer callbacks and cleanup."""

        def post_(self, _timer):
            """Post only to the retained own window while its request is still valid."""
            state = getattr(self, "_klyk_selftest", None)
            if not state or not state["active"] or state["cancelled"].is_set():
                return
            try:
                with _connection_gate.scope(state["request"]):
                    _connection_gate.checkpoint()
                    win = state["win"]
                    wid = int(win.windowNumber()) if win is not None else 0
                    _connection_gate.checkpoint()
                    if wid > 0 and not state["cancelled"].is_set():
                        state["sent"] = bool(post_mouse_click(state["pid"], wid, state["x"], state["y"]))
            except _connection_gate.policy.AccessDisabled:
                pass
            except Exception as error:
                log.warning("skylight.self_test post failed (%s)", type(error).__name__)

        def finish_(self, _timer):
            """Close this sink; only a standalone test may stop its own application loop."""
            state = getattr(self, "_klyk_selftest", None)
            if state is None:
                return
            if not state["own_loop"] or not state["active"]:
                _close_self_test(state)
                return
            state["done"].set()
            app = state["app"]
            # -[NSApplication stop:] only takes effect when run() next pulls an
            # event from the queue. Post a no-op application-defined event so the
            # loop wakes immediately and returns — without this, run() hangs
            # until some other event happens to arrive.
            try:
                app.stop_(None)
                from AppKit import NSEvent, NSMakePoint
                try:
                    from AppKit import NSEventTypeApplicationDefined as _APPDEF
                except Exception:
                    _APPDEF = 15  # NSApplicationDefined
                ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    _APPDEF, NSMakePoint(0, 0), 0, 0.0, 0, None, 0, 0, 0
                )
                app.postEvent_atStart_(ev, True)
            except Exception as error:
                log.warning("skylight.self_test finish failed (%s)", type(error).__name__)

    _SINK_CLASS = _SLSelfTestSink
    _DRIVER_CLASS = _SLSelfTestDriver


def _prepare_self_test(timeout: float, *, own_loop: bool, cancelled=None):
    """Build one main-thread sink and timers without running or stopping AppKit."""
    cancelled = cancelled if cancelled is not None else threading.Event()
    if cancelled.is_set():
        return None
    request = _connection_gate.capture_scope()

    def checkpoint():
        """Reject task cancellation even before its permission generation is revoked."""
        if cancelled.is_set():
            raise RuntimeError("Delivery verification was cancelled.")
        _connection_gate.checkpoint(request)

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Delivery verification must initialize on the main thread.")
    if not _AVAILABLE:
        return None
    import os
    from AppKit import NSApplication, NSWindow, NSBackingStoreBuffered, NSMakeRect
    from Foundation import NSTimer
    from .ui_thread import ui
    if not ui.install_on_main_thread():
        raise RuntimeError("Background AppKit initialization is unavailable")
    app = NSApplication.sharedApplication()
    if bool(app.isRunning()) == own_loop:
        raise RuntimeError("Use asynchronous delivery verification with the running AppKit loop.")
    _build_selftest_classes()
    w, h = 200, 160
    state = dict(hit=False, sent=False, request=request, cancelled=cancelled,
                 active=True, closed=False, own_loop=own_loop,
                 done=threading.Event(), pid=os.getpid(), x=w // 2, y=h // 2,
                 win=None, app=app, driver=None, timers=[], duration=0.2 + max(0.2, timeout))
    try:
        checkpoint()
        # The retained sink stays far outside every display and is never activated.
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(-30000.0, -30000.0, w, h), 0, NSBackingStoreBuffered, False
        )
        state["win"] = win
        win.setReleasedWhenClosed_(False)  # Python retains the sink until exact main-thread cleanup.
        checkpoint()
        win.setTitle_("__klyk_sl_selftest__")
        sink = _SINK_CLASS.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        sink._klyk_selftest = state
        win.setContentView_(sink)
        checkpoint()
        win.orderFrontRegardless()
        driver = _DRIVER_CLASS.alloc().init()
        driver._klyk_selftest = state
        state["driver"] = driver
        for interval, selector in ((0.2, "post:"), (state["duration"], "finish:")):
            checkpoint()
            state["timers"].append(NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, driver, selector, None, False))
        checkpoint()
        return state
    except BaseException:
        _close_self_test(state)
        raise


def _close_self_test(state) -> None:
    """Invalidate and close only the owned sink; cleanup never stops the outer loop."""
    if state["closed"]:
        return
    state["active"] = False
    state["closed"] = True
    for timer in state["timers"]:
        try:
            timer.invalidate()
        except Exception:
            pass
    state["timers"].clear()
    win, state["win"] = state["win"], None
    state["driver"] = state["app"] = None
    if win is not None:
        try:
            win.close()
        except Exception:
            pass
    state["done"].set()


def self_test(timeout: float = 0.6) -> bool:
    """Verify own-sink delivery on the main thread only when no AppKit loop is running."""
    global _DELIVERY_VERIFIED
    state = None
    try:
        state = _prepare_self_test(timeout, own_loop=True)
        if state is None:
            _DELIVERY_VERIFIED = False
            return False
        state["app"].run()
        _connection_gate.checkpoint(state["request"])
        _DELIVERY_VERIFIED = True if state["hit"] else (False if state["sent"] and state["done"].is_set() else None)
        return _DELIVERY_VERIFIED is True
    except _connection_gate.policy.AccessDisabled:
        _DELIVERY_VERIFIED = None
        raise
    except Exception as error:
        log.warning("skylight.self_test unavailable (%s)", type(error).__name__)
        _DELIVERY_VERIFIED = None
        return False
    finally:
        if state is not None:
            _close_self_test(state)


async def self_test_async(timeout: float = 0.6) -> bool:
    """Await own-sink delivery using the existing AppKit loop without running or stopping it."""
    global _DELIVERY_VERIFIED
    from .ui_thread import ui
    _DELIVERY_VERIFIED = None
    request = _connection_gate.capture_scope()
    prepared = []
    cancelled = threading.Event()

    def prepare():
        """Retain cleanup state even if revocation rejects the callback's late result."""
        state = _prepare_self_test(timeout, own_loop=False, cancelled=cancelled)
        if state is not None:
            prepared.append(state)
            if cancelled.is_set():
                _close_self_test(state)
        return state

    try:
        state = await asyncio.get_running_loop().run_in_executor(
            None, lambda: ui.dispatch_sync(prepare, timeout=2.0))
        if state is None:
            _DELIVERY_VERIFIED = False
            return False
        deadline = time.monotonic() + state["duration"] + 0.1
        while not state["done"].is_set() and not state["hit"] and time.monotonic() < deadline:
            _connection_gate.checkpoint(request)
            await asyncio.sleep(0.01)
        _connection_gate.checkpoint(request)
        _DELIVERY_VERIFIED = True if state["hit"] else (False if state["sent"] and state["done"].is_set() else None)
        return _DELIVERY_VERIFIED is True
    except _connection_gate.policy.AccessDisabled:
        _DELIVERY_VERIFIED = None
        raise
    except asyncio.CancelledError:
        _DELIVERY_VERIFIED = None
        raise
    except Exception as error:
        log.warning("skylight.self_test unavailable (%s)", type(error).__name__)
        _DELIVERY_VERIFIED = None
        return False
    finally:
        # A cancelled await does not stop a running executor/UI callback. Close
        # late preparation on that callback's main thread, and forbid its post.
        cancelled.set()
        for state in prepared:
            state["active"] = False
            if not state["closed"]:
                # Only this retained sink/timer cleanup is independent of access.
                # Its finish timer also closes it if the bounded UI queue is full.
                ui.dispatch(lambda state=state: _close_self_test(state), guarded=False)
        cleanup_deadline = time.monotonic() + 0.2
        cleanup_cancelled = False
        while any(not state["closed"] for state in prepared) and time.monotonic() < cleanup_deadline:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cleanup_cancelled = True
                continue  # The already-queued own-sink cleanup must survive repeated cancellation.
        if not _connection_gate.valid(request):
            _DELIVERY_VERIFIED = None
            _connection_gate.checkpoint(request)
        if cleanup_cancelled:
            _DELIVERY_VERIFIED = None
            raise asyncio.CancelledError


def post_mouse_click(
    pid: int,
    window_id: int,
    x_window: float,
    y_window: float,
    button: str = "left",
    modifier_flags: int = 0,
    primer_first: bool = False,
) -> bool:
    """
    Fire a mouse-down + mouse-up at the given window-local point inside the
    target window of the target PID. Uses SkyLight's private
    `SLEventPostToPid` path so the global cursor does not move, focus does
    not change, and the target window is not raised.

    Coordinates: window-local, top-left origin (matches klyk's screenshot
    coord space and existing click() signatures).

    `modifier_flags`: CGEventFlags bitmask (Cmd=0x100000, Shift=0x20000,
    Option=0x80000, Control=0x40000). Stamped onto both the down and the up
    event. Pass 0 (default) for a plain click.

    `primer_first=True` posts an additional mouse-down/up pair at
    window-local (-1, -1) and waits 50 ms before the real click. This is
    required for Chromium-renderer apps (Google Chrome and the rest of
    CHROMIUM_BROWSERS) — their "trusted event" filter discards a click
    that lands without a recent prior event in the same window's queue.

    Returns True on success, False only if SkyLight wasn't available at
    import time. Any real failure (bad PID, bad window ID, ctypes-level
    error) propagates so the caller can diagnose.

    NOTE: SkyLight delivery to a Chromium renderer ALSO requires the
    target app to be the frontmost app at the OS level. This module
    cannot enforce that — the caller arranges activation (or refuses).
    """
    if not _AVAILABLE:
        return False
    _check_stop()
    if primer_first:
        _post_stamped_pair(pid, window_id, -1.0, -1.0, button, modifier_flags)
        time.sleep(0.05)
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window), button, modifier_flags)
    return True


def post_double_click(
    pid: int,
    window_id: int,
    x_window: float,
    y_window: float,
    modifier_flags: int = 0,
    primer_first: bool = False,
) -> bool:
    """
    Fire a double-click (two mouse-down/up pairs) at the given window-local
    point. The second pair carries click_state=2 in the
    kCGMouseEventClickState field, which is how AppKit and most apps
    distinguish a real double-click from two fast single clicks.

    Inter-pair gap is 20 ms — well below the macOS default double-click
    threshold (~500 ms) and matching the CGEvent path in
    klyk/computer.py.

    Returns True on success, False if SkyLight wasn't available.
    """
    if not _AVAILABLE:
        return False
    _check_stop()
    if primer_first:
        _post_stamped_pair(pid, window_id, -1.0, -1.0, "left", modifier_flags)
        time.sleep(0.05)
    # First click — click_state=1 (single).
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window),
                       "left", modifier_flags, click_state=1)
    time.sleep(0.02)
    # Second click — click_state=2 (this is the bit that makes it a
    # double-click rather than two singles).
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window),
                       "left", modifier_flags, click_state=2)
    return True


def post_triple_click(
    pid: int,
    window_id: int,
    x_window: float,
    y_window: float,
    modifier_flags: int = 0,
    primer_first: bool = False,
) -> bool:
    """
    Fire a triple-click (three mouse-down/up pairs) at the given window-local
    point. Click-state is stamped 1 / 2 / 3 on successive pairs so AppKit and
    most apps recognise it as a real triple-click — paragraph selection in
    text views, full-contents selection in single-line fields (URL bar,
    address bar), full-line selection in code editors.

    Inter-pair gap matches double-click (20 ms) so the OS's click-aggregation
    window (default ~500 ms) keeps the three together.

    Returns True on success, False if SkyLight wasn't available.
    """
    if not _AVAILABLE:
        return False
    _check_stop()
    if primer_first:
        _post_stamped_pair(pid, window_id, -1.0, -1.0, "left", modifier_flags)
        time.sleep(0.05)
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window),
                       "left", modifier_flags, click_state=1)
    time.sleep(0.02)
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window),
                       "left", modifier_flags, click_state=2)
    time.sleep(0.02)
    _post_stamped_pair(pid, window_id, float(x_window), float(y_window),
                       "left", modifier_flags, click_state=3)
    return True


def post_drag(
    pid: int,
    window_id: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    steps: int = 20,
    step_delay: float = 0.010,
    button: str = "left",
    modifier_flags: int = 0,
    primer_first: bool = False,
    check_stop=None,
) -> bool:
    """
    Fire a drag sequence: mouse-down at (x1, y1), `steps` interpolated
    mouse-dragged events along the line to (x2, y2), then mouse-up at the
    destination. Each intermediate event is fully stamped — apps that
    inspect the dragged events (e.g. Finder for icon drag, web pages with
    HTML5 drag-and-drop) see a continuous sequence.

    `modifier_flags` is held across the entire drag — Cmd-drag (duplicate
    in Finder), Option-drag (snap-to-axis in many apps), Shift-drag (range
    select) all work because the flags are stamped on every event.

    Returns True on success, False if SkyLight wasn't available.
    """
    check_stop = check_stop or _check_stop
    check_stop()
    if not _AVAILABLE:
        return False
    if primer_first:
        _post_stamped_pair(pid, window_id, -1.0, -1.0, button, modifier_flags)
        time.sleep(0.05)

    down_type, up_type, drag_type, btn_index = _button_event_types(button)
    placeholder = CGPoint(0.0, 0.0)

    # Release where the last event actually landed; cancelled drags must not
    # jump to their intended destination and complete an unintended drop.
    last_point = [float(x1), float(y1)]

    def release_button():
        """Construct a final up event using the last delivered point and original flags."""
        ev_up = _cg.CGEventCreateMouseEvent(None, up_type, placeholder, btn_index)
        try:
            if not ev_up:
                raise RuntimeError("Invisible mouse release could not be created.")
            _stamp_mouse_event(ev_up, pid, window_id, False, *last_point, modifier_flags)
            _post_event(pid, ev_up)
        finally:
            _release(ev_up)

    token = ("skylight", pid, window_id, btn_index)
    # Mouse-down at start.
    ev_down = _cg.CGEventCreateMouseEvent(None, down_type, placeholder, btn_index)
    try:
        if not ev_down:
            raise RuntimeError("Invisible mouse event could not be created; no drag was sent.")
        _stamp_mouse_event(ev_down, pid, window_id, True, float(x1), float(y1), modifier_flags)
        _begin_input(token, lambda: _post_event(pid, ev_down), release_button)
    finally:
        _release(ev_down)
    try:
        time.sleep(0.05)

        # Interpolated drag events.
        for i in range(1, steps + 1):
            check_stop()
            t = i / steps
            px = x1 + (x2 - x1) * t
            py = y1 + (y2 - y1) * t
            ev_drag = _cg.CGEventCreateMouseEvent(None, drag_type, placeholder, btn_index)
            try:
                if not ev_drag:
                    raise RuntimeError("Invisible drag event could not be created; drag was interrupted.")
                # Pressure stays 1.0 throughout the drag — release is on the
                # final mouse-up event, not the last dragged.
                _stamp_mouse_event(ev_drag, pid, window_id, True, float(px), float(py), modifier_flags)
                _post_event(pid, ev_drag)
                last_point[:] = [float(px), float(py)]
            finally:
                _release(ev_drag)
            time.sleep(step_delay)

        time.sleep(0.02)

    finally:
        _finish_input(token)
    return True


def post_scroll(
    pid: int,
    window_id: int,
    x_window: float,
    y_window: float,
    direction: str,
    amount: int = 3,
    modifier_flags: int = 0,
) -> bool:
    """
    Fire a scroll-wheel event over the given window-local point. `direction`
    is one of {"up", "down", "left", "right"}. `amount` is the line count
    (matches the kCGScrollEventUnitLine convention klyk uses everywhere).

    Stamped for SkyLight delivery so the cursor doesn't move and the target
    window doesn't have to be the key window — scrolling a background app
    behind the user's foreground work is the canonical seamless use case.

    `modifier_flags`: Cmd+scroll for zoom (most apps), Shift+scroll for
    horizontal (some apps). Stamped on the wheel event the same way as on
    a mouse-down.

    Returns True on success, False if SkyLight wasn't available.
    """
    if not _AVAILABLE:
        return False
    _check_stop()

    if direction in ("up", "down"):
        wheel1 = int(amount) if direction == "up" else -int(amount)
        ev = _cg.CGEventCreateScrollWheelEvent(None, _kCGScrollEventUnitLine, 1, wheel1)
        if not ev:
            return False
        try:
            _stamp_routing(ev, pid, window_id)
            _cg.CGEventSetFlags(c_void_p(ev), c_uint64(modifier_flags))
            _sl.SLEventSetWindowLocation(c_void_p(ev), CGPoint(float(x_window), float(y_window)))
            _post_event(pid, ev)
        finally:
            _release(ev)
        return True

    if direction in ("left", "right"):
        delta = int(amount) if direction == "right" else -int(amount)
        # Build with wheel1=0, then stamp DeltaAxis2 for horizontal — same
        # recipe as klyk/computer.py's scroll().
        ev = _cg.CGEventCreateScrollWheelEvent(None, _kCGScrollEventUnitLine, 1, 0)
        if not ev:
            return False
        try:
            _cg.CGEventSetIntegerValueField(c_void_p(ev), _FIELD_SCROLL_DELTA_AXIS_2, c_int64(delta))
            _stamp_routing(ev, pid, window_id)
            _cg.CGEventSetFlags(c_void_p(ev), c_uint64(modifier_flags))
            _sl.SLEventSetWindowLocation(c_void_p(ev), CGPoint(float(x_window), float(y_window)))
            _post_event(pid, ev)
        finally:
            _release(ev)
        return True

    raise ValueError(f"post_scroll: unknown direction {direction!r}, expected up/down/left/right")
