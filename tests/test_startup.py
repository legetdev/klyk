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

    def test_input_is_ready_and_worker_started_before_optional_menu(self):
        """Menu construction cannot enter the self-test loop or prevent MCP startup."""
        for menu_fails in (False, True):
            with self.subTest(menu_fails=menu_fails):
                events = []
                thread = MagicMock()
                thread.start.side_effect = lambda: events.append("worker")
                thread.is_alive.return_value = False

                def menu_install():
                    """Assert startup was already permitted even when the indicator fails."""
                    self.assertEqual(events, ["input", "keyboard", "worker"])
                    events.append("menu")
                    if menu_fails:
                        raise RuntimeError("status bar unavailable")

                modules = {
                    "menubar": SimpleNamespace(menubar=SimpleNamespace(install_if_needed=menu_install)),
                    "updates": SimpleNamespace(start_background_check=MagicMock()),
                    "keycodes": SimpleNamespace(warm_keyboard_layout=lambda: events.append("keyboard")),
                    "threading": SimpleNamespace(Thread=MagicMock(return_value=thread)),
                }
                real_import = builtins.__import__

                def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                    """Replace only the bootstrap's native or asynchronous services."""
                    if level and not name:
                        return SimpleNamespace(**{key: modules[key] for key in fromlist})
                    if name in modules:
                        return modules[name]
                    return real_import(name, globals, locals, fromlist, level)

                native_builtins = dict(vars(builtins), __import__=fake_import)
                ui = MagicMock()
                ns = {"__builtins__": native_builtins, "_ui": ui, "log": MagicMock(),
                      "_install_signal_handlers": MagicMock(),
                      "_install_parent_death_watch": MagicMock(), "_refresh_menubar": MagicMock(),
                      "skylight": SimpleNamespace(is_available=lambda: True,
                          self_test=lambda **kw: events.append("input") or True)}
                load_functions("mcp_server.py", {"_run_on_macos"}, ns)

                ns["_run_on_macos"]()

                self.assertEqual(events, ["input", "keyboard", "worker", "menu"])
                ui.run_blocking.assert_called_once()
                thread.join.assert_called_once_with(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
