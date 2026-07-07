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
import logging
import struct
import time
from ctypes import c_double, c_int32, c_int64, c_uint32, c_uint64, c_void_p

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
    if click_state != 1:
        # Default click_state on a freshly-created event is 1; only stamp
        # when the caller wants 2 (double) or higher.
        _cg.CGEventSetIntegerValueField(ev_p, _FIELD_MOUSE_EVENT_CLICK_STATE, c_int64(click_state))
    if modifier_flags:
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
    if ev:
        _cf.CFRelease(c_void_p(ev))


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
        _stamp_mouse_event(ev_down, pid, window_id, True,  x, y, modifier_flags, click_state)
        _stamp_mouse_event(ev_up,   pid, window_id, False, x, y, modifier_flags, click_state)
        _post_event(pid, ev_down)
        time.sleep(0.005)
        _post_event(pid, ev_up)
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
_SELFTEST = {"hit": False, "pid": 0, "x": 0, "y": 0, "win": None, "w": 0, "app": None}
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
        # acceptsFirstMouse → a click lands even when the window isn't key.
        def acceptsFirstMouse_(self, _event):
            return True

        def mouseDown_(self, _event):
            _SELFTEST["hit"] = True

    class _SLSelfTestDriver(NSObject):
        # Fired by a timer once the run loop is live: resolve the window id
        # (valid only once the window is actually on-screen under app.run())
        # and post the stamped click through the real delivery path.
        def post_(self, _timer):
            try:
                win = _SELFTEST.get("win")
                wid = int(win.windowNumber()) if win is not None else 0
                if wid <= 0:
                    from . import capture
                    for w in capture._list_all_windows():
                        if w["pid"] == _SELFTEST["pid"] and abs(
                            w["bounds"]["Width"] - _SELFTEST["w"]
                        ) < 2:
                            wid = w["window_id"]
                            break
                if wid > 0:
                    post_mouse_click(
                        _SELFTEST["pid"], wid, _SELFTEST["x"], _SELFTEST["y"]
                    )
            except Exception as e:  # never let a timer callback escape
                log.warning("skylight.self_test post: %s: %s", type(e).__name__, e)

        def finish_(self, _timer):
            app = _SELFTEST.get("app")
            if app is None:
                return
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
            except Exception as e:
                log.warning("skylight.self_test finish: %s: %s", type(e).__name__, e)

    _SINK_CLASS = _SLSelfTestSink
    _DRIVER_CLASS = _SLSelfTestDriver


def self_test(timeout: float = 0.6) -> bool:
    """
    Verify that SkyLight actually DELIVERS a stamped click — not merely that the
    framework loaded. Creates an in-process AppKit sink window, drives a bounded
    `NSApp.run()` loop (the only context in which the WindowServer routes posted
    events — manual run-loop pumping does not register the window), posts a
    stamped click to the sink through the same SLEventPostToPid path real clicks
    use, and returns True only if the sink's view actually received the click.

    Why this exists: is_available() only confirms the private symbols loaded. If
    a macOS update keeps the symbols but changes delivery semantics or the
    required field-stamping, SLEventPostToPid silently drops the event — no
    exception, the post reports success, nothing lands. This self-test catches
    that exact case on the user's real macOS build (see ARCHITECTURE.md →
    "Known Limitations & Risks" → silent-delivery-failure gap).

    Must be called ON THE MAIN THREAD with no other `NSApp.run()` already active
    (it runs its own bounded loop and stops it). The sink is never activated, so
    the test does not steal focus or move the cursor — empirically an in-process
    sink receives the stamped click while klyk stays a background accessory app.
    Never raises; returns False on failure and records the verdict in
    delivery_verified() for callers to gate the seamless path on.
    """
    global _DELIVERY_VERIFIED
    if not _AVAILABLE:
        _DELIVERY_VERIFIED = False
        return False
    try:
        import os as _os
        from AppKit import (
            NSApplication, NSWindow, NSBackingStoreBuffered,
            NSApplicationActivationPolicyAccessory, NSMakeRect,
        )
        from Foundation import NSTimer
        _build_selftest_classes()
    except Exception as e:
        # No AppKit / harness can't be built → verdict unknown (fail open)
        # rather than downgrading the seamless path on a missing import.
        log.warning("skylight.self_test: harness unavailable (%s)", e)
        _DELIVERY_VERIFIED = None
        return False

    w, h = 200, 160
    win = None
    try:
        app = NSApplication.sharedApplication()
        try:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        # Parked far off any display so it is never visible, yet still gets a
        # real CG window number under app.run() and receives the stamped click
        # (both verified empirically). No flash, no focus change at startup.
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(-30000.0, -30000.0, w, h), 0, NSBackingStoreBuffered, False
        )
        win.setTitle_("__klyk_sl_selftest__")
        win.setContentView_(_SINK_CLASS.alloc().initWithFrame_(NSMakeRect(0, 0, w, h)))
        win.orderFrontRegardless()

        _SELFTEST.update(
            hit=False, pid=_os.getpid(), x=w // 2, y=h // 2, win=win, w=w, app=app,
        )
        driver = _DRIVER_CLASS.alloc().init()
        # Post shortly after the loop goes live; stop the loop after `timeout`.
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, driver, "post:", None, False
        )
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2 + max(0.2, timeout), driver, "finish:", None, False
        )
        app.run()  # returns when finish_ calls app.stop_()
        _DELIVERY_VERIFIED = bool(_SELFTEST["hit"])
        return _SELFTEST["hit"]
    except Exception as e:
        log.warning("skylight.self_test: %s: %s", type(e).__name__, e)
        _DELIVERY_VERIFIED = None
        return False
    finally:
        _SELFTEST["win"] = None
        _SELFTEST["app"] = None
        if win is not None:
            try:
                win.close()
            except Exception:
                pass


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
    if not _AVAILABLE:
        return False
    if primer_first:
        _post_stamped_pair(pid, window_id, -1.0, -1.0, button, modifier_flags)
        time.sleep(0.05)

    down_type, up_type, drag_type, btn_index = _button_event_types(button)
    placeholder = CGPoint(0.0, 0.0)

    # Mouse-down at start.
    ev_down = _cg.CGEventCreateMouseEvent(None, down_type, placeholder, btn_index)
    try:
        _stamp_mouse_event(ev_down, pid, window_id, True, float(x1), float(y1), modifier_flags)
        _post_event(pid, ev_down)
    finally:
        _release(ev_down)
    time.sleep(0.05)

    # Interpolated drag events.
    for i in range(1, steps + 1):
        t = i / steps
        px = x1 + (x2 - x1) * t
        py = y1 + (y2 - y1) * t
        ev_drag = _cg.CGEventCreateMouseEvent(None, drag_type, placeholder, btn_index)
        try:
            # Pressure stays 1.0 throughout the drag — release is on the
            # final mouse-up event, not the last dragged.
            _stamp_mouse_event(ev_drag, pid, window_id, True, float(px), float(py), modifier_flags)
            _post_event(pid, ev_drag)
        finally:
            _release(ev_drag)
        time.sleep(step_delay)

    time.sleep(0.02)

    # Mouse-up at destination.
    ev_up = _cg.CGEventCreateMouseEvent(None, up_type, placeholder, btn_index)
    try:
        _stamp_mouse_event(ev_up, pid, window_id, False, float(x2), float(y2), modifier_flags)
        _post_event(pid, ev_up)
    finally:
        _release(ev_up)
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

    if direction in ("up", "down"):
        wheel1 = int(amount) if direction == "up" else -int(amount)
        ev = _cg.CGEventCreateScrollWheelEvent(None, _kCGScrollEventUnitLine, 1, wheel1)
        if not ev:
            return False
        try:
            _stamp_routing(ev, pid, window_id)
            if modifier_flags:
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
            if modifier_flags:
                _cg.CGEventSetFlags(c_void_p(ev), c_uint64(modifier_flags))
            _sl.SLEventSetWindowLocation(c_void_p(ev), CGPoint(float(x_window), float(y_window)))
            _post_event(pid, ev)
        finally:
            _release(ev)
        return True

    raise ValueError(f"post_scroll: unknown direction {direction!r}, expected up/down/left/right")
