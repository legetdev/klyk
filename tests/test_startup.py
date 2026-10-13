"""Keep optional menu UI outside input initialization and the MCP startup barrier."""

import builtins
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_input_cleanup import load_functions


class StartupTests(unittest.TestCase):
    """Exercise the real bootstrap with inert main-loop and worker boundaries."""

    def test_appkit_finishes_launch_while_activation_is_prohibited(self):
        """Initializing or reusing the UI must not implicitly activate the process."""
        from klyk import ui_thread
        app = MagicMock()
        events = []
        app.setActivationPolicy_.side_effect = lambda policy: events.append(policy) or True
        app.finishLaunching.side_effect = lambda: events.append("finish")
        appkit = SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=lambda: app),
            NSApplicationActivationPolicyProhibited=2, NSApplicationActivationPolicyAccessory=1)
        foundation = SimpleNamespace(NSTimer=MagicMock(), NSRunLoop=MagicMock())
        coordinator = ui_thread.UIThread()
        with patch.object(ui_thread.sys, "platform", "darwin"), patch.dict(
                sys.modules, {"AppKit": appkit, "Foundation": foundation}):
            self.assertTrue(coordinator.install_on_main_thread())
            self.assertTrue(coordinator.install_on_main_thread())

        self.assertEqual(events, [2, "finish", 1])
        foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.assert_called_once()

    def test_protocol_starts_before_any_native_access_is_initialized(self):
        """An Off connection can serve stdio without probing or loading computer facilities."""
        events = []
        thread = MagicMock()
        thread.start.side_effect = lambda: events.append("worker")
        thread.is_alive.return_value = False
        thread_factory = MagicMock(return_value=thread)
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            """Fail on native imports while supplying only an inert worker thread."""
            if level:
                self.fail(f"Startup imported a computer facility: {name or fromlist}")
            if name == "threading":
                return SimpleNamespace(Thread=thread_factory)
            return real_import(name, globals, locals, fromlist, level)

        ui = MagicMock()
        ui.install_on_main_thread.side_effect = lambda: events.append("ui")
        ui.run_blocking.side_effect = lambda: events.append("loop")
        forbid = MagicMock(side_effect=AssertionError("Native access before an enabled request"))
        ns = {"__builtins__": dict(vars(builtins), __import__=fake_import),
              "_ui": ui, "log": MagicMock(), "main": forbid,
              "_install_signal_handlers": lambda: events.append("signals"),
              "_install_parent_death_watch": lambda: events.append("parent-watch"),
              "_initialize_native_runtime": forbid, "_cleanup_input_on_exit": forbid,
              "skylight": SimpleNamespace(is_available=forbid, self_test=forbid),
              "computer": SimpleNamespace(_start_emergency_stop_tap=forbid),
              "ownership": SimpleNamespace(claim_ownership_if_unowned=forbid)}
        load_functions("mcp_server.py", {"_run_on_macos"}, ns)

        ns["_run_on_macos"]()

        self.assertEqual(events, ["ui", "signals", "parent-watch", "worker", "loop"])
        self.assertEqual(thread_factory.call_args.kwargs["name"], "klyk")
        self.assertFalse(thread_factory.call_args.kwargs["daemon"])
        self.assertTrue(callable(thread_factory.call_args.kwargs["target"]))
        thread.join.assert_called_once_with(timeout=2.0)
        forbid.assert_not_called()


if __name__ == "__main__":
    unittest.main()
