"""Portable contracts for native input ownership and AX scoping."""

import asyncio
import builtins
import ctypes
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


class NativeContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise native boundaries with AST-loaded handlers and no desktop access."""

    async def test_run_input_waits_for_worker_after_cancellation(self):
        """Cancellation must not finish while the executor still owns input."""
        worker = asyncio.get_running_loop().create_future()
        loop = SimpleNamespace(run_in_executor=lambda executor, function: worker)
        proxy = SimpleNamespace(
            CancelledError=asyncio.CancelledError,
            get_running_loop=lambda: loop,
            shield=asyncio.shield,
        )
        ns = {
            "asyncio": proxy,
            "threading": __import__("threading"),
            "_worker_state": SimpleNamespace(),
            "_check_stop": lambda: None,
        }
        load_functions("computer.py", {"run_input"}, ns)
        task = asyncio.create_task(ns["run_input"](lambda: "done"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        worker.set_result("done")
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_run_input_repeated_cancellation_cannot_release_early(self):
        """Every extra cancellation is absorbed until the native worker exits."""
        worker = asyncio.get_running_loop().create_future()
        loop = SimpleNamespace(run_in_executor=lambda executor, function: worker)
        proxy = SimpleNamespace(
            CancelledError=asyncio.CancelledError,
            get_running_loop=lambda: loop,
            shield=asyncio.shield,
        )
        ns = {
            "asyncio": proxy,
            "threading": __import__("threading"),
            "_worker_state": SimpleNamespace(),
            "_check_stop": lambda: None,
        }
        load_functions("computer.py", {"run_input"}, ns)
        task = asyncio.create_task(ns["run_input"](lambda: "done"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        worker.set_result("done")
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_run_input_real_worker_observes_cancel_and_finishes_cleanup(self):
        """The real executor path exposes cancellation to the worker before exit."""
        started = threading.Event()
        cleaned = threading.Event()
        state = threading.local()

        def check_stop():
            """Mirror the production stop checkpoint against the worker-local flag."""
            if getattr(state, "cancelled", None) is not None and state.cancelled.is_set():
                raise RuntimeError("cancelled")

        def work():
            """Stay at a native checkpoint until run_input signals cancellation."""
            started.set()
            try:
                while True:
                    check_stop()
                    time.sleep(0.001)
            finally:
                cleaned.set()

        ns = {
            "asyncio": asyncio,
            "threading": threading,
            "_worker_state": state,
            "_check_stop": check_stop,
        }
        load_functions("computer.py", {"run_input"}, ns)
        task = asyncio.create_task(ns["run_input"](work))
        self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())

    def test_ax_element_at_rejects_foreign_pid_hit(self):
        """A coordinate hit from another process cannot authorize an action."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111

        def copy_at_position(root, x, y, output):
            output._obj.value = 500
            return 0

        api.AXUIElementCopyElementAtPosition.side_effect = copy_at_position
        cf = MagicMock()
        ns = {
            "ctypes": ctypes,
            "time": __import__("time"),
            "_appserv": api,
            "_cf": cf,
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.2,
            "_ax_exact_window": lambda pid, window_id: 900,
            "_ax_matches_pid": lambda element, pid: False,
            "_ax_read_attr_ptr": lambda element, attr: 0,
            "_ax_str_attr": lambda element, attr: None,
            "_ax_cgpoint": lambda element: None,
            "_ax_cgsize": lambda element: None,
        }
        load_functions("computer.py", {"_ax_element_at"}, ns)
        self.assertEqual(ns["_ax_element_at"](10, 20, 42, 77), 0)
        self.assertTrue(any(
            getattr(call.args[0], "value", None) == 500
            for call in cf.CFRelease.call_args_list
        ))

    def test_ax_element_at_does_not_widen_unresolved_window(self):
        """A missing requested window must stop before global hit testing."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111
        ns = {
            "ctypes": ctypes,
            "time": __import__("time"),
            "_appserv": api,
            "_cf": MagicMock(),
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.2,
            "_ax_exact_window": lambda pid, window_id: 0,
            "_ax_matches_pid": lambda element, pid: True,
            "_ax_read_attr_ptr": lambda element, attr: 0,
            "_ax_str_attr": lambda element, attr: None,
        }
        load_functions("computer.py", {"_ax_element_at"}, ns)
        self.assertEqual(ns["_ax_element_at"](10, 20, 42, 77), 0)
        api.AXUIElementCopyElementAtPosition.assert_not_called()

    def test_ax_window_native_mismatch_cannot_use_geometry(self):
        """A known different native window ID cannot qualify by matching bounds."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111

        def identity(window, output):
            output._obj.value = 999
            return 0

        api._AXUIElementGetWindow.side_effect = identity
        cf = MagicMock()
        cf.CFArrayGetCount.return_value = 1
        cf.CFArrayGetValueAtIndex.return_value = 500
        ns = {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": cf,
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.2,
            "_ax_read_attr_ptr": lambda element, attr: 300,
            "time": time,
            "_ax_cgpoint": MagicMock(return_value=(10.0, 20.0)),
            "_ax_cgsize": MagicMock(return_value=(100.0, 200.0)),
            "capture": SimpleNamespace(),
        }
        native_builtins = dict(vars(builtins))
        real_import = builtins.__import__
        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            """Satisfy the unused relative capture import without loading native code."""
            if level == 1:
                return SimpleNamespace(capture=SimpleNamespace())
            return real_import(name, globals, locals, fromlist, level)
        native_builtins["__import__"] = fake_import
        ns["__builtins__"] = native_builtins
        load_functions("computer.py", {"_ax_window_for_cg_id"}, ns)
        self.assertEqual(ns["_ax_window_for_cg_id"](42, 77, 10, 20, 100, 200), 0)
        ns["_ax_cgpoint"].assert_not_called()
        ns["_ax_cgsize"].assert_not_called()

    def test_busy_app_window_lookup_waits_once_then_keeps_walks_fast(self):
        """A browser that misses the 50 ms walk cap still resolves its exact window."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111
        timeouts = []
        api.AXUIElementSetMessagingTimeout.side_effect = lambda element, seconds: timeouts.append(seconds)

        def identity(window, output):
            output._obj.value = 77
            return 0

        api._AXUIElementGetWindow.side_effect = identity
        cf = MagicMock()
        cf.CFArrayGetCount.return_value = 1
        cf.CFArrayGetValueAtIndex.return_value = 500
        cf.CFRetain.return_value = 500

        def busy_read(element, attr):
            """Answer the window list only when the caller waits longer than the walk cap."""
            return 300 if attr == b"AXWindows" and timeouts and timeouts[-1] >= 0.5 else 0

        ns = {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": cf,
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.05,
            "_AX_PATIENT_TIMEOUT_SECONDS": 1.0,
            "_ax_read_attr_ptr": busy_read,
            "time": time,
            "_ax_cgpoint": MagicMock(return_value=(10.0, 20.0)),
            "_ax_cgsize": MagicMock(return_value=(100.0, 200.0)),
        }
        native_builtins = dict(vars(builtins))
        real_import = builtins.__import__
        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            """Satisfy the unused relative capture import without loading native code."""
            if level == 1:
                return SimpleNamespace(capture=SimpleNamespace())
            return real_import(name, globals, locals, fromlist, level)
        native_builtins["__import__"] = fake_import
        ns["__builtins__"] = native_builtins
        load_functions("computer.py", {"_ax_window_for_cg_id"}, ns)
        self.assertEqual(ns["_ax_window_for_cg_id"](42, 77, 10, 20, 100, 200), 500)
        self.assertEqual(timeouts[0], 0.05)
        self.assertEqual(timeouts[-1], 0.05, "the walk cap is restored after the patient read")
        self.assertLessEqual(max(timeouts), 1.0)

    def test_busy_app_focused_summary_still_reports_the_focused_field(self):
        """A verify readback on a busy editor must not come back empty after 50 ms."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111
        timeouts = []
        api.AXUIElementSetMessagingTimeout.side_effect = lambda element, seconds: timeouts.append(seconds)

        def busy_read(element, attr):
            """The app answers its root questions only to a patient caller."""
            if element == 111:
                patient = bool(timeouts) and timeouts[-1] >= 0.5
                return {b"AXFocusedUIElement": 200, b"AXFocusedWindow": 300}.get(attr, 0) if patient else 0
            return 0

        attrs = {(200, b"AXRole"): "AXTextArea", (200, b"AXValue"): "Electron verified",
                 (300, b"AXTitle"): "electron-fixture.txt"}
        ns = {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": MagicMock(),
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.05,
            "_AX_PATIENT_TIMEOUT_SECONDS": 1.0,
            "_ax_read_attr_ptr": busy_read,
            "_ax_str_attr": lambda element, attr: attrs.get((element, attr), ""),
        }
        load_functions("computer.py", {"ax_focused_summary"}, ns)
        summary = ns["ax_focused_summary"](42)
        self.assertEqual(summary["focused"]["role"], "AXTextArea")
        self.assertEqual(summary["focused"]["value"], "Electron verified")
        self.assertEqual(summary["window_title"], "electron-fixture.txt")
        self.assertEqual(timeouts[-1], 0.05)

    def _slow_root_walk_namespace(self):
        """A browser that answers its window lookup after 0.95 s (within the patient bound), then walks quickly."""
        clock = [100.0]

        def slow_root(*args):
            clock[0] += 0.95
            return 500

        def walk(element, results, *args):
            deadline = next(a for a in args if isinstance(a, float) and a > 1.0)
            if clock[0] <= deadline:
                results.append({"role": "AXButton", "label": "Increment browser", "x": 1, "y": 2})

        cf = MagicMock()
        cf.CFArrayGetCount.return_value = 1
        cf.CFArrayGetValueAtIndex.return_value = 600
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111
        return {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": cf,
            "time": SimpleNamespace(monotonic=lambda: clock[0]),
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.05,
            "_AX_SNAPSHOT_CHILDREN_CAP": 20,
            "_ax_exact_window": slow_root,
            "_ax_read_root_attr": slow_root,
            "_ax_read_attr_ptr": lambda element, attr: 0 if attr == b"AXFocusedUIElement" else 700,
            "_ax_collect": walk,
            "_ax_search_by_label": lambda element, query, results, depth, max_depth, per_node, max_results, deadline:
                walk(element, results, deadline),
            "release_refs": lambda entries: None,
        }

    def test_slow_window_lookup_leaves_the_walk_its_full_budget(self):
        """A patient window lookup must not use up the walk's own time (empty inspect)."""
        for name, call in (
            ("ax_snapshot", lambda f: f(42, window_id=77)),
            ("ax_snapshot", lambda f: f(42)),
            ("ax_search_focused", lambda f: f(42, "Increment", window_id=77)),
            ("ax_search_focused", lambda f: f(42, "Increment")),
            ("ax_collect_focused", lambda f: f(42)),
        ):
            with self.subTest(name=name):
                ns = self._slow_root_walk_namespace()
                load_functions("computer.py", {name}, ns)
                self.assertEqual([e["label"] for e in call(ns[name])], ["Increment browser"])

    def test_focused_window_requires_exact_identity_when_window_is_selected(self):
        """Equal geometry is insufficient when the requested AX window differs."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 111
        cf = MagicMock()
        cf.CFEqual.side_effect = lambda left, right: left.value == right.value
        ns = {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": cf,
            "_ax_read_attr_ptr": lambda element, attr: 500,
            "_ax_exact_window": lambda pid, window_id: ns["target"],
            "_ax_cgpoint": lambda element: (10.0, 20.0),
            "_ax_cgsize": lambda element: (100.0, 200.0),
            "target": 600,
        }
        load_functions("computer.py", {"_verify_focused_window"}, ns)
        self.assertFalse(ns["_verify_focused_window"](42, 10, 20, 100, 200, window_id=77))
        ns["target"] = 500
        self.assertTrue(ns["_verify_focused_window"](42, 10, 20, 100, 200, window_id=77))

    def test_ax_set_value_mismatched_readback_is_unverified(self):
        """An accepted AX write with stale readback stays unverified for fallback."""
        cf = MagicMock()
        cf.CFStringCreateWithBytes.return_value = 77
        def read_attr(element, attr):
            """Keep the native role fixture independent of the owned readback pointer."""
            return "AXTextField" if attr == b"AXRole" else "old value"

        ns = {
            "ctypes": ctypes,
            "_check_stop": lambda: None,
            "_ax_element_at": lambda *args: 500,
            "_ax_matches_pid": lambda element, pid: True,
            "_ax_str_attr": read_attr,
            "_ax_read_attr_ptr": lambda element, attr: 88,
            "_cftype_to_str": lambda pointer: "old value",
            "_ax_is_web_backed": lambda element: False,
            "_ax_attr_is_settable": lambda element, attr: True,
            "_ax_set_attr_value": lambda element, attr, value: True,
            "_AX_TEXT_INPUT_ROLES": {"AXTextField"},
            "_cf": cf,
            "kCFStringEncodingUTF8": 0x08000100,
        }
        load_functions("computer.py", {"ax_set_value_at"}, ns)
        result = ns["ax_set_value_at"](10, 20, "new value", 42, 77)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unverified")
        self.assertTrue(result["attempted"])
        self.assertFalse(result["verified"])

    def test_stamped_pair_posts_mouse_up_when_gap_raises(self):
        """A timing failure must still emit the matching mouse-up event."""
        cg = MagicMock()
        cg.CGEventCreateMouseEvent.side_effect = [10, 11]
        sent = []
        ns = {
            "CGPoint": lambda x, y: (x, y),
            "_cg": cg,
            "_button_event_types": lambda button: (1, 2, 6, 0),
            "_stamp_mouse_event": lambda *args: None,
            "_post_event": lambda pid, event: sent.append(event),
            "_release": lambda event: None,
            "time": SimpleNamespace(sleep=lambda delay: (_ for _ in ()).throw(RuntimeError("clock"))),
        }
        load_functions("skylight.py", {"_post_stamped_pair"}, ns)
        with self.assertRaisesRegex(RuntimeError, "clock"):
            ns["_post_stamped_pair"](42, 77, 10, 20, "left")
        self.assertEqual(sent, [10, 11])


if __name__ == "__main__":
    unittest.main()
