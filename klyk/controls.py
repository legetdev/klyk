"""A single background menu-bar popover for Klyk's per-environment switches.

The controls use only native AppKit and private settings. They never capture the
screen, inspect other apps, request Accessibility, or start a computer-use session.
The existing activity indicator remains independent from this user control.
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
from pathlib import Path

from . import clients, connection_policy as policy, jsonc
from .private_files import open_private, private_directory

log = logging.getLogger("klyk.controls")
_LABELS = {"codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode",
           "gemini": "Gemini", "antigravity": "Antigravity", "grok": "Grok",
           "other": "Other connections"}
_PRIORITY = ("codex", "claude", "opencode")
_native_types = None


def configured_environments() -> tuple[str, ...]:
    """Show core clients and configured environments without running any client CLI."""
    present = set(_PRIORITY)
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


def start_background() -> bool:
    """Start one detached controller with no window, Dock icon, terminal or sound."""
    if sys.platform != "darwin":
        raise RuntimeError("Klyk controls are available on macOS.")
    if running():
        return True
    private_directory(policy.policy_path().parent)
    safe_names = {"HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    environment = {key: value for key, value in os.environ.items() if key in safe_names}
    environment["HOME"] = str(policy.policy_path().parent.parent)
    environment["PYTHONPATH"] = ""
    reader, writer = os.pipe()
    try:
        with open_private(policy.policy_path().parent / "controls.log", "a") as output:
            child = subprocess.Popen([sys.executable, "-P", "-m", "klyk.controls", "--ready-fd", str(writer)],
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
            raise RuntimeError("Klyk controls could not start. Check the private controls log.")
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
    return True


def _wait_ready(reader: int, child) -> bool:
    """Confirm the new interface was built rather than merely spawning a process."""
    readable, _, _ = select.select([reader], [], [], 3.0)
    return bool(readable and os.read(reader, 1) == b"1" and child.poll() is None)


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

        def quitControls_(self, sender):
            """Quit the interface without changing any saved access preference."""
            from .ui_thread import ui
            if not self.controller.busy:
                ui.shutdown()

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
        self._timer = None

    def build(self) -> None:
        """Create a quiet system-styled popover, leaving it closed at startup."""
        from AppKit import (NSFont, NSColor, NSImage, NSStatusBar, NSVariableStatusItemLength,
                            NSView, NSViewController, NSPopover, NSPopoverBehaviorTransient,
                            NSTextField, NSScrollView, NSSwitch, NSButton)
        from Foundation import NSTimer, NSRunLoop, NSMakeRect
        Actions, Document = _types()
        self._delegate = Actions.alloc().init()
        self._delegate.controller = self
        width, row_height = 320, 56
        body_height = min(448, len(self.keys) * row_height)
        height = body_height + 130
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

        def text(value, frame, size=13, secondary=False):
            """Create a noneditable accessible system label in the existing appearance."""
            field = NSTextField.labelWithString_(value)
            field.setFrame_(NSMakeRect(*frame))
            field.setFont_(NSFont.systemFontOfSize_(size))
            field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
            content.addSubview_(field)
            return field

        title = text("Klyk", (20, height - 36, 280, 24), 18)
        title.setFont_(NSFont.boldSystemFontOfSize_(18))
        text("Computer access", (20, height - 59, 280, 20), 12, True)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 60, width, body_height))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(len(self.keys) * row_height > body_height)
        document = Document.alloc().initWithFrame_(NSMakeRect(0, 0, width, len(self.keys) * row_height))
        previous = None
        for index, key in enumerate(self.keys):
            row = NSView.alloc().initWithFrame_(NSMakeRect(16, index * row_height, width - 32, row_height))
            name = NSTextField.labelWithString_(label(key))
            name.setFont_(NSFont.systemFontOfSize_(13))
            name.setFrame_(NSMakeRect(4, 29, 210, 18))
            row.addSubview_(name)
            status = NSTextField.labelWithString_("Off")
            status.setFont_(NSFont.systemFontOfSize_(11))
            status.setTextColor_(NSColor.secondaryLabelColor())
            status.setFrame_(NSMakeRect(4, 10, 210, 17))
            row.addSubview_(status)
            switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(width - 102, 8, 62, 40))
            switch.setTag_(index)
            switch.setTarget_(self._delegate)
            switch.setAction_("toggleConnection:")
            switch.setAccessibilityLabel_(label(key) + " computer access")
            switch.setAccessibilityIdentifier_("klyk-access-" + key)
            switch.setToolTip_("Off blocks Klyk's screen reads and actions for " + label(key) + ".")
            if previous is not None:
                previous.setNextKeyView_(switch)
            previous = switch
            row.addSubview_(switch)
            document.addSubview_(row)
            self.rows[key] = switch, status
        scroll.setDocumentView_(document)
        content.addSubview_(scroll)
        self._footer = NSTextField.wrappingLabelWithString_("On allows screen reads and actions.")
        self._footer.setFont_(NSFont.systemFontOfSize_(10))
        self._footer.setTextColor_(NSColor.secondaryLabelColor())
        self._footer.setFrame_(NSMakeRect(20, 10, 220, 42))
        content.addSubview_(self._footer)
        self._quit = NSButton.buttonWithTitle_target_action_("Quit", self._delegate, "quitControls:")
        self._quit.setBordered_(False)
        self._quit.setFrame_(NSMakeRect(246, 15, 58, 40))
        self._quit.setAccessibilityLabel_("Quit Klyk controls")
        self._quit.setToolTip_("Closes the controls; saved access stays in effect.")
        content.addSubview_(self._quit)
        view_controller = NSViewController.alloc().init()
        view_controller.setView_(content)
        self._popover = NSPopover.alloc().init()
        self._popover.setBehavior_(NSPopoverBehaviorTransient)
        self._popover.setAnimates_(False)
        self._popover.setContentViewController_(view_controller)
        self._popover.setContentSize_((width, height))
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        button = self._item.button()
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_("switch.2", "Klyk controls")
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
        else:
            button.setTitle_("Klyk")
        button.setToolTip_("Klyk — computer access")
        button.setTarget_(self._delegate)
        button.setAction_("showControls:")
        self._timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, True, lambda timer: self.refresh())
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, "NSEventTrackingRunLoopMode")
        self.refresh()

    def show(self) -> None:
        """Show the user-requested panel without activating any other app or window."""
        from AppKit import NSMinYEdge
        if self._popover.isShown():
            self._popover.performClose_(None)
            return
        self.refresh()
        self._popover.showRelativeToRect_ofView_preferredEdge_(self._item.button().bounds(), self._item.button(), NSMinYEdge)

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
        error = self.error or state.get("error", "")
        marker = (state, error, frozenset(self.busy))
        if marker == self._last_state:
            return
        self._last_state = marker
        for key, (switch, status) in self.rows.items():
            allowed = state["clients"][key]["enabled"]
            switch.setState_(1 if allowed else 0)
            switch.setEnabled_(not state.get("error") and key not in self.busy)
            status.setStringValue_("Saving…" if key in self.busy else ("On" if allowed else "Off"))
        self._quit.setEnabled_(not self.busy)
        self._footer.setStringValue_(error or "On allows screen reads and actions.")
        count = sum(state["clients"][key]["enabled"] for key in self.keys)
        self._item.button().setToolTip_("Klyk — all access off" if count == 0 else f"Klyk — {count} environments allowed")

    def change(self, key: str, value: bool) -> None:
        """Persist user intent off the main thread, keeping switches responsive on I/O errors."""
        from .ui_thread import ui
        if key in self.busy:
            return
        self.busy.add(key)
        self.error = ""
        self.refresh()

        def save():
            """Keep failures plain and restore the actual state after an atomic write."""
            error = ""
            try:
                policy.set_enabled(key, value)
            except (OSError, ValueError, RuntimeError):
                error = "Could not save. Try the switch again."

            self._completed.put((key, error))
            # The periodic refresh also drains completions if the UI queue is full.
            ui.dispatch(self.refresh, guarded=False)

        threading.Thread(target=save, name="klyk-switch", daemon=True).start()


def run(ready_fd: int | None = None) -> None:
    """Hold the singleton lease and run only the native control interface."""
    if sys.platform != "darwin":
        raise RuntimeError("Klyk controls are available on macOS.")
    private_directory(policy.policy_path().parent)
    with open_private(_lock_path(), "a+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            policy.initialize()
        except (OSError, ValueError):
            pass  # Invalid state is shown as a banner and grants no access.
        from .ui_thread import ui
        if not ui.install_on_main_thread():
            raise RuntimeError("Klyk controls could not start the native interface.")
        controller = Controls(configured_environments())
        controller.build()
        if ready_fd is not None:
            os.write(ready_fd, b"1")
            os.close(ready_fd)
        ui.run_blocking()
        fcntl.flock(lease, fcntl.LOCK_UN)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Klyk background access controls")
    parser.add_argument("--ready-fd", type=int, default=None, help=argparse.SUPPRESS)
    run(parser.parse_args().ready_fd)
