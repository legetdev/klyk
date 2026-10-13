"""Adversarial input safety tests with native input, apps and clipboard fully replaced."""

import asyncio
import ctypes
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from klyk import launcher, ownership
from test_input_cleanup import load_functions


def core_namespace():
    """Provide event constructors that only append to an in-memory trace."""
    events = []
    native_events = {}
    cg = MagicMock()

    def construct(source, kind, point, button):
        """Allocate a fake event ID retaining the requested point and button."""
        identifier = len(native_events) + 1
        native_events[identifier] = (kind, (point.x, point.y), button)
        return identifier

    cg.CGEventCreateMouseEvent.side_effect = construct
    cg.CGEventCreateKeyboardEvent.side_effect = lambda source, code, down: 10 if down else 11
    namespace = {
        "asyncio": asyncio, "ctypes": ctypes, "threading": threading, "time": time,
        "_cg": cg, "_cf": MagicMock(), "log": MagicMock(),
        "_input_lock": asyncio.Lock(), "_held_lock": threading.RLock(), "_held_inputs": {},
        "_worker_state": threading.local(), "_stop_lock": threading.RLock(), "_stop_engaged": [False],
        "_post": lambda event: events.append(native_events.get(event, event)),
        "_post_to_pid": lambda pid, event: events.append((pid, event)),
        "CGPoint": lambda *args, **kwargs: SimpleNamespace(x=kwargs.get("x", args[0] if args else 0), y=kwargs.get("y", args[1] if len(args)>1 else 0)),
        "kCGEventLeftMouseDown": 1, "kCGEventLeftMouseUp": 2,
        "kCGEventRightMouseDown": 3, "kCGEventRightMouseUp": 4,
        "kCGEventLeftMouseDragged": 6, "kCGEventRightMouseDragged": 7,
        "kCGMouseButtonLeft": 0, "kCGMouseButtonRight": 1, "kCGMouseEventClickState": 1,
        "MODIFIER_FLAGS": {"cmd": 0x100000, "shift": 0x20000},
        "parse_key_combo": lambda key: (42, 0), "is_frontmost_app": lambda pid: True,
        "EmergencyStop": type("EmergencyStop", (RuntimeError,), {}),
    }
    load_functions("computer.py", {"_check_stop", "_begin_input", "_finish_input", "release_held_input"}, namespace)
    return namespace, events


class StopAndReleaseTests(unittest.IsolatedAsyncioTestCase):
    """Stops and cancellation release held inputs before another request can continue."""

    def test_repeated_chord_does_not_clear_and_disabled_tap_recovers(self):
        """Holding a chord ignores repeats; a disabled tap is enabled without resetting stop."""
        now = [1.0]
        fields = {9: 53, 8: 0}
        cg = MagicMock()
        cg.CGEventGetIntegerValueField.side_effect = lambda event, field: fields.get(field, 0)
        cg.CGEventGetFlags.return_value = 0x120000
        ns = {"ctypes": ctypes, "_cg": cg, "_TAP_CB_TYPE": lambda callback: callback,
              "_stop_tap": [81], "kCGEventKeyDown": 10, "kCGKeyboardEventKeycode": 9,
              "_EMERGENCY_STOP_KEYCODE": 53, "_EMERGENCY_STOP_FLAGS": 0x120000,
              "_stop_engaged": [False], "_last_chord_t": [0.0], "_CHORD_DEBOUNCE_S": .6,
              "_stop_lock": threading.Lock(), "time": SimpleNamespace(monotonic=lambda: now[0]), "log": MagicMock()}
        load_functions("computer.py", {"_make_stop_callback", "_toggle_stop_from_chord"}, ns)
        callback = ns["_make_stop_callback"]()
        callback(None, 10, 123, None)
        self.assertTrue(ns["_stop_engaged"][0])
        now[0] = 2.0
        fields[8] = 1
        callback(None, 10, 123, None)
        self.assertTrue(ns["_stop_engaged"][0])
        callback(None, 0xFFFFFFFE, None, None)
        self.assertTrue(ns["_stop_engaged"][0])
        cg.CGEventTapEnable.assert_called_once()
        fields[8] = 0
        callback(None, 10, 123, None)
        self.assertFalse(ns["_stop_engaged"][0])

    async def test_double_and_triple_click_stop_before_next_pair(self):
        """A stop between click pairs releases the first button and prevents the next down."""
        for name in ("double_click", "triple_click"):
            with self.subTest(name=name):
                ns, events = core_namespace()

                async def sleep(delay):
                    """Engage the fake latch at the first post-down delay."""
                    ns["_stop_engaged"][0] = True

                ns["asyncio"] = SimpleNamespace(sleep=sleep)
                load_functions("computer.py", {name}, ns)
                with self.assertRaisesRegex(RuntimeError, "Emergency stop"):
                    await ns[name](10, 20)
                self.assertEqual([event[0] for event in events], [1, 2])
                self.assertEqual(ns["_held_inputs"], {})

    async def test_keyboard_settle_rechecks_stop(self):
        """A stopped request cannot send a key after its renderer settle delay."""
        ns, events = core_namespace()

        async def sleep(delay):
            """Activate the latch during the controllable settle interval."""
            ns["_stop_engaged"][0] = True

        ns["asyncio"] = SimpleNamespace(sleep=sleep)
        load_functions("computer.py", {"press_key"}, ns)
        with self.assertRaisesRegex(RuntimeError, "Emergency stop"):
            await ns["press_key"]("a", pid=123)
        self.assertEqual(events, [])

    async def test_global_typing_stops_when_foreground_changes(self):
        """Only the first character reaches the originally observed foreground app."""
        ns, events = core_namespace()
        ns["char_to_keycode"] = lambda char: (ord(char), 0)
        front = [True]
        ns["is_frontmost_app"] = lambda pid: front[0]

        async def sleep(delay):
            """Change foreground only after the first character was posted."""
            if events:
                front[0] = False

        ns["asyncio"] = SimpleNamespace(sleep=sleep)
        load_functions("computer.py", {"type_text_char_by_char"}, ns)
        with self.assertRaisesRegex(RuntimeError, "foreground app changed"):
            await ns["type_text_char_by_char"]("ab", expected_frontmost_pid=123)
        self.assertEqual(events, [10, 11])

    async def test_system_key_cancellation_always_sends_release(self):
        """Cancel after a fake media down; cleanup still posts the matching media up."""
        ns, events = core_namespace()
        appkit = SimpleNamespace(NSEvent=SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=lambda *args: SimpleNamespace(CGEvent=lambda: args[-2])))
        quartz = SimpleNamespace(kCGHIDEventTap=0, CGEventPost=lambda tap, event: events.append(event))
        ns.update(SYSTEM_KEY_CODES={"volume_up": 0}, SYSTEM_KEY_NAMES=["volume_up"])
        load_functions("computer.py", {"press_system_key"}, ns)
        with patch.dict(sys.modules, {"AppKit": appkit, "Quartz": quartz}):
            task = asyncio.create_task(ns["press_system_key"]("volume_up"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(events, [0xA00, 0xB00])

    def test_exit_cleanup_releases_once_and_prevents_repress(self):
        """Hard-exit cleanup flushes retained downs and prevents workers re-pressing afterward."""
        ns, events = core_namespace()
        load_functions("computer.py", {"_key_down_sync", "_key_up_sync"}, ns)
        ns["_key_down_sync"](42, 0)
        ns["release_held_input"]()
        ns["release_held_input"]()
        ns["_key_up_sync"](42, 0)
        with self.assertRaisesRegex(RuntimeError, "Emergency stop"):
            ns["_key_down_sync"](42, 0)
        self.assertEqual(events, [10, 11])
        self.assertEqual(ns["_held_inputs"], {})

    def test_exit_signal_can_interrupt_held_stop_lock(self):
        """A real signal to our inert child cannot deadlock when cleanup re-enters the stop lock."""
        source = Path(__file__).resolve().parents[1] / "klyk" / "computer.py"
        script = f'''import ast,os,signal,sys,threading
from pathlib import Path
sys.path.insert(0,{str(source.parents[1] / "tests")!r})
from test_input_cleanup import load_functions
tree=ast.parse(Path({str(source)!r}).read_text())
nodes=[node for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=="_stop_lock" for target in node.targets)]
releases=[]
ns={{"threading":threading,"_stop_engaged":[False],"_held_lock":threading.RLock(),"_held_inputs":{{("inert",1):lambda:releases.append("up")}}}}
exec(compile(ast.Module(body=nodes,type_ignores=[]),"<inert input cleanup>","exec"),ns)
load_functions("computer.py",{{"release_held_input"}},ns)
signal.signal(signal.SIGUSR1,lambda sig,frame:ns["release_held_input"]())
with ns["_stop_lock"]:
 os.kill(os.getpid(),signal.SIGUSR1)
ns["release_held_input"]()
assert ns["_stop_engaged"][0] and releases==["up"]
print("released once")
'''
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=3)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "released once")

    async def test_cancelled_drag_releases_at_last_delivered_point(self):
        """Cancellation before the first dragged event cannot complete a drop at destination."""
        ns, events = core_namespace()
        load_functions("computer.py", {"drag"}, ns)
        task = asyncio.create_task(ns["drag"](10, 20, 90, 80))
        await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(events, [(1, (10.0, 20.0), 0), (2, (10.0, 20.0), 0)])

    async def test_visible_drag_preserves_button_and_modifiers(self):
        """Right-button modified drags never silently become unmodified left-button input."""
        ns, events = core_namespace()
        load_functions("computer.py", {"drag"}, ns)
        await ns["drag"](10, 20, 30, 40, steps=1, step_delay=0, button="right", modifiers=["shift"])
        self.assertEqual(events, [(3, (10.0, 20.0), 1), (7, (30.0, 40.0), 1), (4, (30.0, 40.0), 1)])
        self.assertEqual([call.args[1] for call in ns["_cg"].CGEventSetFlags.call_args_list], [0x20000] * 3)

    async def test_cancelled_worker_drains_before_request_returns(self):
        """A native worker observes cancellation and completes release before cancellation escapes."""
        ns, events = core_namespace()
        load_functions("computer.py", {"run_input"}, ns)
        entered = threading.Event()
        cleaned = threading.Event()

        def worker():
            """Hold an inert native boundary until cancellation reaches its stop checkpoint."""
            entered.set()
            try:
                while True:
                    ns["_check_stop"]()
                    time.sleep(.002)
            finally:
                cleaned.set()

        task = asyncio.create_task(ns["run_input"](worker))
        while not entered.is_set():
            await asyncio.sleep(.002)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())


class OwnershipLeaseTests(unittest.IsolatedAsyncioTestCase):
    """Cross-process lease handoffs cannot overlap outstanding input cleanup."""

    def setUp(self):
        """Redirect all ownership operations to an isolated temporary directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.owner = Path(self.temporary.name) / "owner"
        self.patch = patch.object(ownership, "OWNER_PATH", self.owner)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def test_peer_handoff_waits_until_request_releases_lease(self):
        """An inert second Python server receives busy while our request is unfinished."""
        ownership.claim_ownership()
        code = "from klyk import ownership\ntry:\n ownership.claim_ownership()\nexcept RuntimeError:\n print('busy')\nelse:\n print('claimed')"
        env = dict(os.environ, KLYK_OWNER_FILE=str(self.owner), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        with ownership.control_request():
            result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.stdout.strip(), "busy")
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.stdout.strip(), "claimed")

    async def test_inherited_context_cannot_borrow_parent_lease(self):
        """A new asyncio task cannot bypass the lock by inheriting a ContextVar value."""
        ownership.claim_ownership()

        async def other_request():
            """Try a distinct request created while the original control context is active."""
            with ownership.control_request():
                self.fail("A different task borrowed its parent's control lease")

        with ownership.control_request():
            with self.assertRaisesRegex(RuntimeError, "busy"):
                await asyncio.create_task(other_request())
            with ownership.control_request():
                self.assertEqual(ownership.claim_ownership(), 0)

    def test_corrupt_utf8_and_contended_owner_file_do_not_wedge_startup(self):
        """Malformed owner bytes are recoverable and an externally held flock fails promptly."""
        self.owner.write_bytes(b"\xff\xff\n")
        self.assertEqual(ownership.claim_ownership_if_unowned(), ownership._MY_PID)
        with self.owner.open("a+") as peer:
            fcntl.flock(peer.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertEqual(ownership.claim_ownership_if_unowned(), 0)
            self.assertEqual(ownership.current_owner(), 0)
            self.assertFalse(ownership.is_owner())
            with self.assertRaisesRegex(RuntimeError, "busy"):
                ownership.claim_ownership()
            self.assertLess(time.monotonic()-started, .3)


class ProcessIdentityTests(unittest.TestCase):
    """Termination never signals a replacement PID or reports unknown identity as exited."""

    def test_read_only_identity_applies_to_our_python_process(self):
        """The public platform process query reads our own start time without app APIs."""
        if sys.platform != "darwin":
            self.skipTest("Public libproc process identity is macOS only")
        identity = launcher.process_identity(os.getpid(), include_command=True)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["pid"], os.getpid())
        self.assertEqual(identity["uid"], os.getuid())
        self.assertGreater(identity["started"][0], 0)
        self.assertTrue(identity["argv"])
        self.assertEqual(ctypes.sizeof(launcher._ProcessInfo), 136)

    def test_recycled_process_is_never_signalled(self):
        """A known replacement means the original exited and receives no TERM or KILL."""
        original = {"pid": 123, "uid": 501, "started": (1, 1)}
        replacement = {"pid": 123, "uid": 501, "started": (2, 1)}
        with patch.object(launcher, "process_identity", return_value=replacement), patch.object(launcher.os, "kill") as kill:
            self.assertTrue(launcher.terminate_pid(123, expected_identity=original))
        kill.assert_not_called()

    def test_recycle_before_escalation_prevents_kill(self):
        """The escalation phase rechecks identity after sending TERM to the original."""
        original = {"pid": 123, "uid": 501, "started": (1, 1)}
        replacement = {"pid": 123, "uid": 501, "started": (2, 1)}
        with patch.object(launcher, "process_identity", side_effect=[original, replacement]), patch.object(launcher.os, "kill") as kill:
            self.assertTrue(launcher.terminate_pid(123, term_timeout=0, expected_identity=original))
        kill.assert_called_once_with(123, launcher.signal.SIGTERM)

    def test_unknown_identity_is_not_successful_termination(self):
        """An unreadable live process remains unverified and cannot receive a signal."""
        original = {"pid": 123, "uid": 501, "started": (1, 1)}
        with patch.object(launcher, "process_identity", return_value=None), patch.object(launcher, "pid_alive", return_value=True), patch.object(launcher.os, "kill") as kill:
            self.assertFalse(launcher.terminate_pid(123, expected_identity=original))
        kill.assert_not_called()


class ClipboardExitTests(unittest.IsolatedAsyncioTestCase):
    """Exit cleanup must preserve a newer human copy and restore pending state at most once."""

    def test_exit_restore_respects_generation_and_is_idempotent(self):
        """A newer clipboard generation wins over the snapshot retained by a pending paste."""
        for current, expected in ((7, ["old"]), (8, [])):
            with self.subTest(current=current):
                restored = []
                board = SimpleNamespace(changeCount=lambda: current)
                appkit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board))
                ns = {"_clipboard_snapshot": "old", "_clipboard_change_count": 7,
                      "_restore_pasteboard": restored.append}
                load_functions("computer.py", {"_flush_clipboard_restore"}, ns)
                with patch.dict(sys.modules, {"AppKit": appkit}):
                    ns["_flush_clipboard_restore"]()
                    ns["_flush_clipboard_restore"]()
                self.assertEqual(restored, expected)
                self.assertIsNone(ns["_clipboard_snapshot"])

    async def test_changed_clipboard_before_paste_receives_no_write(self):
        """A copy made after the preservation snapshot blocks the proposed clipboard replacement."""
        ns, events = core_namespace()
        board = SimpleNamespace(changeCount=lambda: 8)
        appkit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board))
        subprocess_fake = SimpleNamespace(run=MagicMock())
        ns.update(_snapshot_pasteboard=lambda: (["old"], 7), _restore_pasteboard=MagicMock(),
                  _clipboard_snapshot=None, _clipboard_change_count=None, subprocess=subprocess_fake)
        load_functions("computer.py", {"type_text"}, ns)
        with patch.dict(sys.modules, {"AppKit": appkit}):
            with self.assertRaisesRegex(RuntimeError, "clipboard changed before"):
                await ns["type_text"]("temporary")
        subprocess_fake.run.assert_not_called()
        ns["_restore_pasteboard"].assert_not_called()

    async def test_stop_while_waiting_for_input_lock_prevents_clipboard_write(self):
        """Queued typing cannot replace the clipboard after its initial stop check became stale."""
        ns, events = core_namespace()
        appkit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=MagicMock()))
        subprocess_fake = SimpleNamespace(run=MagicMock())
        ns.update(_snapshot_pasteboard=MagicMock(return_value=(["old"], 7)), _restore_pasteboard=MagicMock(),
                  _clipboard_snapshot=None, _clipboard_change_count=None, subprocess=subprocess_fake)
        load_functions("computer.py", {"type_text"}, ns)
        await ns["_input_lock"].acquire()
        with patch.dict(sys.modules, {"AppKit": appkit}):
            task = asyncio.create_task(ns["type_text"]("temporary"))
            await asyncio.sleep(0)
            ns["_stop_engaged"][0] = True
            ns["_input_lock"].release()
            with self.assertRaisesRegex(RuntimeError, "Emergency stop"):
                await task
        subprocess_fake.run.assert_not_called()
        ns["_snapshot_pasteboard"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
