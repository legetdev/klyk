"""A single background menu-bar popover for Klyk's per-environment switches.

The controls use only native AppKit and private settings. They never capture the
screen, inspect other apps, request Accessibility, or start a computer-use session.
Live status comes from the servers' own presence files (see presence.py).
It is Klyk's only menu-bar item and also shows recent activity and the emergency stop.

MCP servers call ``ensure_running()`` on each tool request: at most one cheap lease
check per 10 s per process, and a quiet background start only when no controller holds
the lease, so a hidden or crashed controller comes back without ever opening the panel.

The controller exits once no AI app has been connected for a while, unless the user keeps
it. Hiding it lasts until a tool request or an explicit open, not merely a connection.
"""

from __future__ import annotations

import fcntl
import logging
import os
import queue
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, clients, connection_policy as policy, jsonc, ownership, presence, updates
from .private_files import open_private, private_directory

log = logging.getLogger("klyk.controls")
_LABELS = {"codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode",
           "gemini": "Gemini", "antigravity": "Antigravity", "grok": "Grok",
           "other": "Other connections"}
_PRIORITY = ("codex", "claude", "opencode")
_native_types = None
_ALL = "*"  # Busy marker while every environment is being switched Off.
_OCCLUSION_VISIBLE = 1 << 1  # NSWindowOcclusionStateVisible, without importing AppKit.
_SYMBOLS = {"off": ("cursorarrow.slash", "Klyk controls, all access off"),
            "on": ("cursorarrow.click.2", "Klyk controls, access on"),
            "busy": ("cursorarrow.motionlines", "Klyk controls, working"),
            "stopped": ("hand.raised.fill", "Klyk controls, emergency stop on")}
_FOOTER = "Changes apply immediately, without restarting the AI app."
_SUBTITLE = "Computer access  ·  ⌘⇧Esc stops everything"
_STOPPED = "Emergency stop is on  ·  ⌘⇧Esc resumes"
_PRESENCE_INTERVAL = 1.0
_IDLE_GRACE = 30.0  # Outlasts a client reconnecting, so the icon never flickers.
_LINGER_GRACE = 600.0  # A user who just installed or opened Klyk gets time to start an AI app.
_ACTIVITY_LINES = 4
_ENSURE_INTERVAL = 10.0
_ensure_lock = threading.Lock()
_ensure_checked: float | None = None
_ensure_thread: threading.Thread | None = None
_images: dict = {}


def configured_environments() -> tuple[str, ...]:
    """Show core, configured and saved-enabled clients without running any client CLI."""
    present = set(_PRIORITY)
    present.update(key for key, row in policy.snapshot()["clients"].items()
                   if row["enabled"] and key in clients.CLIENTS)
    # A client that is connected right now is shown even without a saved entry.
    present.update(key for key, row in presence.summary().items()
                   if row["connected"] and key in clients.CLIENTS)
    for key, client in clients.CLIENTS.items():
        if key in _PRIORITY:
            continue
        try:
            if clients.current_entry(client) is not None:
                present.add(key)
        except (OSError, ValueError, jsonc.ConfigFormatError):
            # An unreadable configured client remains visible and safely Off.
            if client.path.exists():
                present.add(key)
    ordered = (*_PRIORITY, *(key for key in clients.CLIENTS if key not in _PRIORITY and key in present))
    return (*ordered, "other")


def label(key: str) -> str:
    """Use concise familiar client names without introducing provider branding."""
    return _LABELS.get(key, clients.CLIENTS[key].label if key in clients.CLIENTS else "Unknown connection")


def status_text(allowed: bool, live: dict | None, saving: bool = False) -> str:
    """Describe one environment's saved switch and live connection in a few plain words."""
    live = live or {}
    if saving:
        return "Saving…"
    if not allowed:
        return "Off · connected" if live.get("connected") else "Off"
    if live.get("active"):
        return f"On · working in {live['app']}" if live.get("app") else "On · working"
    if live.get("driving"):
        return "On · in control"
    return "On · connected" if live.get("connected") else "On · not connected"


def activity_lines(found: list[dict], now: float) -> list[str]:
    """Newest actions across every connected environment, in plain words."""
    actions = sorted((action for data in found for action in data.get("recent", [])),
                     key=lambda action: action[0], reverse=True)
    lines = []
    for at, app, tool, mode in actions[:_ACTIVITY_LINES]:
        age = max(0, int(now - at))
        when = ("just now" if age < 2 else f"{age}s ago" if age < 60 else f"{age // 60}m ago" if age < 3600
                else f"{age // 3600}h ago")
        unusual = f" ({mode})" if mode in ("background", "humanoid") else ""
        lines.append(f"{tool.replace('_', ' ').capitalize()} in {app}{unusual} · {when}")
    return lines


def update_line() -> str:
    """Name an available release from the local cache; never touches the network."""
    try:
        state = updates.status()
    except Exception:
        return ""
    return f"Version {state['latest']} is available. Run klyk update." if state["update_available"] else ""


def menu_summary(state: dict, live: dict, stopped: bool = False) -> tuple[str, str]:
    """Pick the menu-bar symbol and tooltip: stopped, all off, some on, or someone working."""
    if stopped:
        return "stopped", "Klyk — emergency stop on, Cmd+Shift+Esc resumes"
    allowed = [key for key, row in state["clients"].items() if row["enabled"]]
    if not allowed:
        return "off", "Klyk — all access off"
    working = [key for key in allowed if live.get(key, {}).get("active")]
    if working:
        phrases = [label(key) + (f" working in {live[key]['app']}" if live[key].get("app") else " working")
                   for key in working]
        text = ", ".join(phrases[:2]) + (f" and {len(phrases) - 2} more" if len(phrases) > 2 else "")
        return "busy", "Klyk — " + text
    noun = "environment" if len(allowed) == 1 else "environments"
    return "on", f"Klyk — {len(allowed)} {noun} allowed"


def _symbol(kind: str):
    """Load one template SF Symbol once; None when this macOS lacks it."""
    if kind not in _images:
        from AppKit import NSImage
        name, description = _SYMBOLS[kind]
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, description)
        if image is not None:
            image.setTemplate_(True)
        _images[kind] = image
    return _images[kind]


def _marker(name: str) -> Path:
    """Two user choices outlive the controller: an empty file beside the settings means yes."""
    return policy.policy_path().parent / f"controls.{name}"


def chosen(name: str) -> bool:
    """Whether the user chose ``hidden`` or ``keep``; lexists never follows a planted link."""
    return os.path.lexists(_marker(name))


def choose(name: str, value: bool) -> None:
    """Save one choice without ever recreating state that an uninstall removed."""
    if value:
        try:
            os.close(os.open(_marker(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        except FileExistsError:
            pass
    else:
        _marker(name).unlink(missing_ok=True)


def start_for_connection() -> bool:
    """Show the controls for a new connection, unless the user hid them until Klyk is used."""
    return True if chosen("hidden") else start_background()


def ensure_running() -> bool:
    """Bring back a hidden or crashed controller from a server's tool request; never raises.

    Costs one lease check per 10 s per process. When no controller holds the lease it
    starts one in the background with the panel closed, so the request never waits.
    Returns True only when this call scheduled a start.
    """
    global _ensure_checked, _ensure_thread
    try:
        now = time.monotonic()
        with _ensure_lock:
            if _ensure_checked is not None and now - _ensure_checked < _ENSURE_INTERVAL:
                return False
            _ensure_checked = now
            if sys.platform != "darwin" or (_ensure_thread is not None and _ensure_thread.is_alive()):
                return False
        if running():
            return False

        def start() -> None:
            """Start quietly; a failure is retried on a later request."""
            try:
                start_background(show=False)
            except Exception as error:
                log.warning("Klyk controls could not restart (%s)", type(error).__name__)

        thread = threading.Thread(target=start, name="klyk-controls-ensure", daemon=True)
        with _ensure_lock:
            _ensure_thread = thread
        thread.start()
        return True
    except Exception:
        return False


def _lock_path() -> Path:
    """Keep the singleton UI lease beside the owner-private connection settings."""
    return policy.policy_path().parent / "controls.lock"


def running() -> bool:
    """Check the controller lease without process-name guesses or screen access."""
    path = _lock_path()
    if not path.exists():
        return False
    try:
        with open_private(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except (OSError, ValueError):
        return False
    return False


def start_background(show: bool = False, linger: bool = False) -> bool:
    """Start one detached controller with no Dock icon, terminal or sound.

    With show, True means this controller confirmed its panel is open on screen.
    """
    if sys.platform != "darwin":
        raise RuntimeError("Klyk controls are available on macOS.")
    if running():
        return not show
    private_directory(policy.policy_path().parent)
    safe_names = {"HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING",
                  "KLYK_UPDATE_CHECK"}
    environment = {key: value for key, value in os.environ.items() if key in safe_names}
    environment["HOME"] = str(policy.policy_path().parent.parent)
    environment["PYTHONPATH"] = ""
    reader, writer = os.pipe()
    try:
        with open_private(policy.policy_path().parent / "controls.log", "a") as output:
            command = [sys.executable, "-P", "-m", "klyk.controls", "--ready-fd", str(writer)]
            if linger:
                command.append("--linger")
            child = subprocess.Popen([*command, "--show"] if show else command,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     env=environment, close_fds=True, pass_fds=(writer,), start_new_session=True)
        os.close(writer)
        writer = None
        if not _wait_ready(reader, child):
            # This process owns no desktop input or target application state.
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            if running():
                return not show  # A concurrent start won the singleton lease; the controls are up.
            raise RuntimeError("Klyk controls could not start. Check the private controls log.")
        return not show or _wait_shown(reader)
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
    return True


def open_panel() -> bool:
    """Open the panel from any state: replacing the controller also recovers a lost or frozen icon.

    Saved switches live on disk, so a replacement never changes anyone's access.
    """
    for _ in range(2):  # A client connecting mid-replacement can win the lease once.
        if stop_running(timeout=1.0) and start_background(show=True):
            return True
    return False


def stop_running(timeout: float = 3.0) -> bool:
    """Stop the controller holding the lease, forcing a frozen one; True once none remains."""
    import signal
    import time
    path = _lock_path()
    if not path.exists():
        return True
    try:
        with open_private(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return True  # No live holder.
            except BlockingIOError:
                pass
            handle.seek(0)
            pid = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False
    for signal_number, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 1.0)):
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not running():
                return True
            time.sleep(0.05)
    return not running()


def _wait_ready(reader: int, child) -> bool:
    """Confirm the new interface was built rather than merely spawning a process."""
    readable, _, _ = select.select([reader], [], [], 3.0)
    return bool(readable and os.read(reader, 1) == b"1" and child.poll() is None)


def _wait_shown(reader: int) -> bool:
    """Confirm the requested panel is on screen, not merely that the controller started."""
    readable, _, _ = select.select([reader], [], [], 3.0)
    return bool(readable and os.read(reader, 1) == b"2")


def _types():
    """Define Objective-C callbacks lazily, keeping imports and tests free of GUI work."""
    global _native_types
    if _native_types is not None:
        return _native_types
    from AppKit import NSView
    from Foundation import NSObject

    class KlykControlsActions(NSObject):
        """Forward native user events to the retained Python controller."""

        def showControls_(self, sender):
            """Open or close the popover only after the user's menu-bar click."""
            self.controller.show()

        def toggleConnection_(self, sender):
            """Save a switch asynchronously and confirm its actual persisted state."""
            self.controller.change(self.controller.keys[sender.tag()], sender.state() == 1)

        def turnAllOff_(self, sender):
            """Switch every environment Off in one save."""
            self.controller.turn_all_off()

        def hideControls_(self, sender):
            """Hide until Klyk is used, without changing any saved access preference."""
            self.controller.hide()

        def toggleKeep_(self, sender):
            """Keep the icon even while no AI app is connected."""
            self.controller.set_keep(sender.state() == 1)

        def popoverDidClose_(self, notification):
            """Stop watching outside clicks once the popover is gone, however it closed."""
            self.controller.stop_outside_watch()
            self.controller.release_anchor()

    class KlykControlsDocument(NSView):
        """Keep the first environment at the top of a scrollable native list."""

        def isFlipped(self):
            """Use top-down row coordinates for stable scrolling and keyboard order."""
            return True

    _native_types = KlykControlsActions, KlykControlsDocument
    return _native_types


class Controls:
    """Hold native controls; all view mutation runs on the AppKit main thread."""

    def __init__(self, keys: tuple[str, ...]):
        self.keys = keys
        self.busy: set[str] = set()
        self._completed = queue.SimpleQueue()
        self.rows: dict[str, tuple] = {}
        self.error = ""
        self._last_state = None
        self._delegate = None
        self._item = None
        self._popover = None
        self._footer = None
        self._quit = None
        self._keep = None
        self._subtitle = None
        self._activity: list = []
        self._all_off = None
        self._timer = None
        self._outside = None
        self._anchor = None
        self._icon = None
        self._live: dict = {}
        self._live_at: float | None = None
        self._found: list[dict] = []
        self._grace = _IDLE_GRACE
        self._idle_since: float | None = None
        self.idle_exit = False
        self._update = ""

    def build(self) -> None:
        """Create a quiet system-styled popover, closed until the user asks for it."""
        from AppKit import (NSPopover, NSPopoverBehaviorTransient, NSSquareStatusItemLength, NSStatusBar,
                            NSVariableStatusItemLength)
        from Foundation import NSTimer, NSRunLoop
        Actions, _ = _types()
        self._delegate = Actions.alloc().init()
        self._delegate.controller = self
        self._popover = NSPopover.alloc().init()
        self._popover.setBehavior_(NSPopoverBehaviorTransient)
        self._popover.setAnimates_(False)
        self._popover.setDelegate_(self._delegate)
        self._build_content()
        # A fixed square keeps neighbouring menu-bar items still while the symbol changes.
        length = NSSquareStatusItemLength if _symbol("on") is not None else NSVariableStatusItemLength
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(length)
        # Python apps share one preferences domain; a private name keeps another item's hidden state away.
        self._item.setAutosaveName_("klyk.controls")
        button = self._item.button()
        button.setToolTip_("Klyk — computer access")
        button.setTarget_(self._delegate)
        button.setAction_("showControls:")
        self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, True, lambda timer: self.refresh())
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, "NSEventTrackingRunLoopMode")
        self.refresh()


    def _build_content(self) -> None:
        """Create one switch row per current environment inside the existing popover."""
        from AppKit import (NSButton, NSColor, NSControlSizeSmall, NSFont, NSLineBreakByTruncatingTail,
                            NSScrollView, NSSwitch, NSTextField, NSView, NSViewController)
        from Foundation import NSMakeRect
        _, Document = _types()
        self.rows = {}
        width, row_height = 320, 56
        body_height = min(364, len(self.keys) * row_height)
        activity_height = 34 + _ACTIVITY_LINES * 17
        bottom = 74 + activity_height
        height = body_height + bottom + 70
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

        def text(value, frame, size=13, secondary=False):
            """Create a noneditable accessible system label in the existing appearance."""
            field = NSTextField.labelWithString_(value)
            field.setFrame_(NSMakeRect(*frame))
            field.setFont_(NSFont.systemFontOfSize_(size))
            field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
            content.addSubview_(field)
            return field

        title = text("Klyk", (20, height - 36, 180, 24), 18)
        title.setFont_(NSFont.boldSystemFontOfSize_(18))
        title.setToolTip_("Klyk " + __version__)
        self._subtitle = text(_SUBTITLE, (20, height - 59, width - 40, 20), 12, True)
        self._all_off = NSButton.buttonWithTitle_target_action_("Turn All Off", self._delegate, "turnAllOff:")
        self._all_off.setControlSize_(NSControlSizeSmall)
        self._all_off.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self._all_off.sizeToFit()
        size = self._all_off.frame().size
        visible = self._all_off.alignmentRectForFrame_(self._all_off.frame())
        # Align the visible bezel with the switches' right edge and the header text's centre.
        self._all_off.setFrame_(NSMakeRect(width - 22 - visible.origin.x - visible.size.width,
                                           height - 24 - visible.origin.y - visible.size.height / 2,
                                           size.width, size.height))
        self._all_off.setAccessibilityLabel_("Turn off computer access for every environment")
        self._all_off.setToolTip_("Blocks Klyk for every AI app at once. Turn each back on when you need it.")
        content.addSubview_(self._all_off)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, bottom, width, body_height))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(len(self.keys) * row_height > body_height)
        document = Document.alloc().initWithFrame_(NSMakeRect(0, 0, width, len(self.keys) * row_height))
        previous = self._all_off
        for index, key in enumerate(self.keys):
            row = NSView.alloc().initWithFrame_(NSMakeRect(16, index * row_height, width - 32, row_height))
            name = NSTextField.labelWithString_(label(key))
            name.setFont_(NSFont.systemFontOfSize_(13))
            name.setFrame_(NSMakeRect(4, 29, 210, 18))
            name.setLineBreakMode_(NSLineBreakByTruncatingTail)
            row.addSubview_(name)
            status = NSTextField.labelWithString_("Off")
            status.setFont_(NSFont.systemFontOfSize_(11))
            status.setTextColor_(NSColor.secondaryLabelColor())
            status.setFrame_(NSMakeRect(4, 10, 210, 17))
            status.setLineBreakMode_(NSLineBreakByTruncatingTail)  # Long app names end in an ellipsis.
            row.addSubview_(status)
            switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(width - 88, 8, 62, 40))
            switch.setTag_(index)
            switch.setTarget_(self._delegate)
            switch.setAction_("toggleConnection:")
            switch.setAccessibilityLabel_(label(key) + " computer access")
            switch.setAccessibilityIdentifier_("klyk-access-" + key)
            switch.setToolTip_("Off blocks Klyk's screen reads and actions for " + label(key) + ".")
            previous.setNextKeyView_(switch)
            previous = switch
            row.addSubview_(switch)
            document.addSubview_(row)
            self.rows[key] = switch, status
        scroll.setDocumentView_(document)
        content.addSubview_(scroll)
        self._footer = NSTextField.wrappingLabelWithString_(_FOOTER)
        self._footer.setFont_(NSFont.systemFontOfSize_(10))
        self._footer.setTextColor_(NSColor.secondaryLabelColor())
        self._footer.setFrame_(NSMakeRect(20, 42, width - 40, 28))
        content.addSubview_(self._footer)
        heading = text("Recent activity", (20, bottom - 26, width - 40, 17), 11)
        heading.setFont_(NSFont.boldSystemFontOfSize_(11))
        self._activity = []
        for index in range(_ACTIVITY_LINES):
            line = text("", (20, bottom - 46 - index * 17, width - 40, 16), 11, True)
            line.setLineBreakMode_(NSLineBreakByTruncatingTail)
            self._activity.append(line)
        self._keep = NSButton.checkboxWithTitle_target_action_("Keep in menu bar", self._delegate, "toggleKeep:")
        self._keep.setControlSize_(NSControlSizeSmall)
        self._keep.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self._keep.sizeToFit()
        self._keep.setFrameOrigin_((21, 16))
        self._keep.setState_(1 if chosen("keep") else 0)
        self._keep.setToolTip_("Keeps this icon when no AI app is connected. "
                               "Otherwise it hides itself and returns when one connects.")
        content.addSubview_(self._keep)
        self._quit = NSButton.buttonWithTitle_target_action_("Hide Until Klyk Is Used", self._delegate, "hideControls:")
        self._quit.setControlSize_(NSControlSizeSmall)
        self._quit.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self._quit.sizeToFit()
        size = self._quit.frame().size
        visible = self._quit.alignmentRectForFrame_(self._quit.frame())
        self._quit.setFrameOrigin_((width - 20 - visible.origin.x - visible.size.width, 9))
        self._quit.setToolTip_("Hides this icon until an AI app uses Klyk. Saved access stays in effect "
                               "and Klyk keeps running.")
        previous.setNextKeyView_(self._keep)
        self._keep.setNextKeyView_(self._quit)
        content.addSubview_(self._quit)
        view_controller = NSViewController.alloc().init()
        view_controller.setView_(content)
        self._popover.setContentViewController_(view_controller)
        self._popover.setContentSize_((width, height))
        self._last_state = None

    def show(self) -> None:
        """Show the user-requested panel without activating any other app or window."""
        from AppKit import NSMinYEdge
        if self._popover.isShown():
            self._popover.performClose_(None)
            return
        current = configured_environments()
        if current != self.keys and not self.busy:
            # A client installed since startup gets its switch without restarting the controls.
            self.keys = current
            self._build_content()
        self._live_at = None  # Read who is connected now rather than up to a second ago.
        self.refresh()
        anchor = self._item.button() if self.in_menu_bar() else self._screen_anchor()
        self._popover.showRelativeToRect_ofView_preferredEdge_(anchor.bounds(), anchor, NSMinYEdge)
        self._watch_outside_clicks()
        self._show_activity()

    def in_menu_bar(self) -> bool:
        """Report whether the icon can really be seen, since macOS can hide it without saying so."""
        window = self._item.button().window()
        return bool(window is not None and window.isVisible() and window.screen() is not None
                    and window.occlusionState() & _OCCLUSION_VISIBLE)

    def open_when_placed(self, ready_fd: int | None) -> None:
        """Open once macOS has placed the icon, or below the menu bar when it never appears."""
        from Foundation import NSTimer
        self._item.setVisible_(True)  # An explicit open undoes a Cmd-drag out of the menu bar.
        remaining = [20]

        def attempt(timer) -> None:
            """Confirm the outcome to the waiting command only once the panel is really open."""
            remaining[0] -= 1
            if remaining[0] > 0 and not self.in_menu_bar():
                return
            timer.invalidate()
            self.show()
            if ready_fd is not None:
                try:
                    if self._popover.isShown():
                        os.write(ready_fd, b"2")
                    os.close(ready_fd)
                except OSError:
                    pass  # The command stopped waiting; the panel is still open.

        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.05, True, attempt)

    def _screen_anchor(self):
        """Give the panel an invisible anchor below the menu bar of the screen with the pointer."""
        from AppKit import (NSBackingStoreBuffered, NSColor, NSEvent, NSScreen, NSStatusWindowLevel, NSWindow,
                            NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowStyleMaskBorderless)
        from Foundation import NSMakeRect, NSPointInRect
        pointer = NSEvent.mouseLocation()
        screen = next((value for value in NSScreen.screens() if NSPointInRect(pointer, value.frame())),
                      NSScreen.mainScreen())
        area = screen.visibleFrame()
        self.release_anchor()
        self._anchor = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(area.origin.x + area.size.width - 190, area.origin.y + area.size.height - 1, 1, 1),
            NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        self._anchor.setReleasedWhenClosed_(False)
        self._anchor.setOpaque_(False)
        self._anchor.setBackgroundColor_(NSColor.clearColor())
        self._anchor.setIgnoresMouseEvents_(True)
        self._anchor.setLevel_(NSStatusWindowLevel)
        self._anchor.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self._anchor.orderFrontRegardless()  # Never activates Klyk or takes focus from the user's app.
        return self._anchor.contentView()

    def release_anchor(self) -> None:
        """Remove the invisible anchor once its panel has closed."""
        if self._anchor is not None:
            self._anchor.orderOut_(None)
            self._anchor = None

    def _watch_outside_clicks(self) -> None:
        """Close on a click in any other app; Klyk never activates, so transient alone never fires."""
        from AppKit import (NSEvent, NSEventMaskLeftMouseDown, NSEventMaskRightMouseDown,
                            NSEventMaskOtherMouseDown)
        self.stop_outside_watch()
        mask = NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown | NSEventMaskOtherMouseDown
        # Global monitors only observe other apps' events; clicks inside the popover are unaffected.
        self._outside = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, lambda event: self._popover.performClose_(None))

    def stop_outside_watch(self) -> None:
        """Remove the outside-click monitor so no observer outlives the open popover."""
        if self._outside is not None:
            from AppKit import NSEvent
            NSEvent.removeMonitor_(self._outside)
            self._outside = None

    def refresh(self) -> None:
        """Display confirmed file state rather than a switch's optimistic position."""
        while True:
            try:
                key, error = self._completed.get_nowait()
            except queue.Empty:
                break
            self.busy.discard(key)
            self.error = error
        state = policy.snapshot()
        now = time.monotonic()
        if self._live_at is None or now - self._live_at >= _PRESENCE_INTERVAL:
            self._found = presence.entries()
            self._live = self._with_driver(presence.summary(found=self._found))
            self._live_at = now
            self._check_idle(now)
            self._show_activity()
        error = self.error or state.get("error", "")
        live = tuple((key, row["connected"], row["active"], row["app"], row.get("driving"))
                     for key, row in self._live.items())
        stopped = any(data.get("stopped") for data in self._found)
        notice = error or self._update
        marker = (state, notice, frozenset(self.busy), live, stopped)
        if marker == self._last_state:
            return
        self._last_state = marker
        clearing = _ALL in self.busy
        for key, (switch, status) in self.rows.items():
            allowed = state["clients"][key]["enabled"]
            switch.setState_(1 if allowed else 0)
            switch.setEnabled_(not state.get("error") and key not in self.busy and not clearing)
            status.setStringValue_(status_text(allowed, self._live.get(key),
                                               key in self.busy or (clearing and allowed)))
        any_on = any(row["enabled"] for row in state["clients"].values())
        self._all_off.setEnabled_(any_on and not self.busy and not state.get("error"))
        self._quit.setEnabled_(not self.busy)
        self._footer.setStringValue_(notice or _FOOTER)
        if self._subtitle is not None:
            from AppKit import NSColor
            self._subtitle.setStringValue_(_STOPPED if stopped else _SUBTITLE)
            self._subtitle.setTextColor_(NSColor.systemRedColor() if stopped else NSColor.secondaryLabelColor())
        icon, tooltip = menu_summary(state, self._live, stopped)
        self._show_icon(icon)
        self._item.button().setToolTip_(tooltip)

    def _panel_open(self) -> bool:
        """Whether the user is looking at the panel right now."""
        return bool(self._popover is not None and self._popover.isShown())

    def _with_driver(self, live: dict) -> dict:
        """Mark the environment whose server holds the control token."""
        if not self._found:
            return live
        try:
            owner = ownership.current_owner() if ownership.OWNER_PATH.exists() else 0
        except Exception:
            owner = 0
        key = next((data["client"] for data in self._found if data["pid"] == owner), None)
        return {**live, key: {**live[key], "driving": True}} if key in live else live

    def _show_activity(self) -> None:
        """Rewrite the activity section only while someone can see it."""
        if not self._activity or not self._panel_open():
            return
        self._update = update_line()
        lines = activity_lines(self._found, time.time()) or ["Nothing yet"]
        for index, field in enumerate(self._activity):
            value = lines[index] if index < len(lines) else ""
            if field.stringValue() != value:
                field.setStringValue_(value)

    def _check_idle(self, now: float) -> None:
        """Leave the menu bar once nothing has been connected for the grace period."""
        if self._found:
            self._grace = _IDLE_GRACE  # The time allowed for a first connection is used up.
        if self._found or self.busy or self._panel_open() or chosen("keep"):
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= self._grace and not self.idle_exit:
            from .ui_thread import ui
            self.idle_exit = True
            ui.shutdown()

    def hide(self) -> None:
        """Leave the menu bar until Klyk is used; a switch being saved finishes first."""
        from .ui_thread import ui
        if self.busy:
            return
        try:
            choose("hidden", True)
        except OSError:
            pass  # The icon still goes; it merely returns at the next connection.
        ui.shutdown()

    def set_keep(self, value: bool) -> None:
        """Save the choice and show what was really saved."""
        try:
            choose("keep", value)
        except OSError:
            self.error = "Could not save. Try again."
        self._keep.setState_(1 if chosen("keep") else 0)

    def _show_icon(self, kind: str) -> None:
        """Swap the template symbol only when the state changes, with a text fallback."""
        if kind == self._icon:
            return
        self._icon = kind
        button = self._item.button()
        image = _symbol(kind) or _symbol("on")
        if image is not None:
            button.setImage_(image)
        else:
            button.setTitle_("Klyk")  # Older macOS without these symbols still shows the item.

    def change(self, key: str, value: bool) -> None:
        """Persist user intent off the main thread, keeping switches responsive on I/O errors."""
        if key in self.busy or _ALL in self.busy:
            self._last_state = None
            self.refresh()  # Snap a refused switch back to its saved position.
            return
        self._save(key, lambda: policy.set_enabled(key, value), "Could not save. Try the switch again.")

    def turn_all_off(self) -> None:
        """Switch every environment Off in one atomic save, including ones not listed here."""
        if self.busy:
            return  # A pending single switch finishes first, so it cannot land after this.
        self._save(_ALL, policy.disable_all, "Could not turn access off. Try again.")

    def _save(self, key: str, write, failure: str) -> None:
        """Run one settings write on a worker and report its confirmed result to the UI."""
        from .ui_thread import ui
        self.busy.add(key)
        self.error = ""
        self.refresh()

        def save():
            """Keep failures plain and restore the actual state after an atomic write."""
            error = ""
            try:
                write()
            except (OSError, ValueError, RuntimeError):
                error = failure

            self._completed.put((key, error))
            # The periodic refresh also drains completions if the UI queue is full.
            ui.dispatch(self.refresh, guarded=False)

        threading.Thread(target=save, name="klyk-switch", daemon=True).start()


def run(ready_fd: int | None = None, show: bool = False, linger: bool = False) -> None:
    """Hold the singleton lease and run only the native control interface."""
    if sys.platform != "darwin":
        raise RuntimeError("Klyk controls are available on macOS.")
    private_directory(policy.policy_path().parent)
    with open_private(_lock_path(), "a+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Record the lease holder so uninstall can stop exactly this controller.
        lease.truncate(0)
        lease.write(f"{os.getpid()}\n")
        lease.flush()
        try:
            choose("hidden", False)  # Whoever started it wanted it seen.
        except OSError:
            pass
        try:
            policy.initialize()
        except (OSError, ValueError):
            pass  # Invalid state is shown as a banner and grants no access.
        from .ui_thread import ui
        if not ui.install_on_main_thread():
            raise RuntimeError("Klyk controls could not start the native interface.")
        controller = Controls(configured_environments())
        if show or linger:
            controller._grace = _LINGER_GRACE
        controller.build()
        if ready_fd is not None:
            os.write(ready_fd, b"1")
            if not show:
                os.close(ready_fd)
        if show:
            controller.open_when_placed(ready_fd)
        ui.run_blocking()
        fcntl.flock(lease, fcntl.LOCK_UN)
    # A server announces itself before it checks the lease. One that connected while this
    # controller was leaving saw the lease held, so it is picked up here instead.
    if controller.idle_exit and presence.entries():
        start_for_connection()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Klyk background access controls")
    parser.add_argument("--ready-fd", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--show", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--linger", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    run(arguments.ready_fd, arguments.show, arguments.linger)
