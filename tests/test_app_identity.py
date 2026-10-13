"""Portable native identity contracts for foreground and app discovery."""

import builtins
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


class AppIdentityTests(unittest.TestCase):
    """Verify identity lookups fail closed without launching or guessing."""

    def test_frontmost_pid_uses_active_process_serial_number(self):
        """The active process API, rather than topmost window order, supplies the PID."""
        appserv = MagicMock()

        def front(output):
            output._obj.high = 12
            output._obj.low = 34
            return 0

        def process_pid(serial, output):
            self.assertEqual((serial._obj.high, serial._obj.low), (12, 34))
            output._obj.value = 321
            return 0

        appserv.GetFrontProcess.side_effect = front
        appserv.GetProcessPID.side_effect = process_pid
        class ProcessSerialNumber(ctypes.Structure):
            """Native process serial stand-in matching capture's field layout."""

            _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

        ns = {"ctypes": ctypes, "_appserv": appserv, "_ProcessSerialNumber": ProcessSerialNumber}
        load_functions("capture.py", {"frontmost_pid"}, ns)

        self.assertEqual(ns["frontmost_pid"](), 321)
        appserv.GetFrontProcess.assert_called_once()
        appserv.GetProcessPID.assert_called_once()

    def test_frontmost_pid_returns_unknown_on_native_failure(self):
        """A failed foreground query returns unknown instead of a false PID."""
        for front_result, pid_result in ((-1, 0), (0, -1)):
            with self.subTest(front_result=front_result, pid_result=pid_result):
                appserv = MagicMock()
                appserv.GetFrontProcess.return_value = front_result
                appserv.GetProcessPID.return_value = pid_result
                class ProcessSerialNumber(ctypes.Structure):
                    """Native process serial stand-in for failure handling."""

                    _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]

                ns = {"ctypes": ctypes, "_appserv": appserv, "_ProcessSerialNumber": ProcessSerialNumber}
                load_functions("capture.py", {"frontmost_pid"}, ns)
                self.assertIsNone(ns["frontmost_pid"]())
                if front_result != 0:
                    appserv.GetProcessPID.assert_not_called()

    @staticmethod
    def _appkit_namespace(apps_by_bundle=None, running_apps=None):
        """Build AppKit stand-ins for bundle and exact-name lookups."""
        running_apps = list(running_apps or [])
        ns_running = SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=MagicMock(
                return_value=list(apps_by_bundle or [])
            )
        )
        workspace = SimpleNamespace(
            sharedWorkspace=lambda: SimpleNamespace(
                runningApplications=lambda: running_apps
            )
        )
        return SimpleNamespace(NSRunningApplication=ns_running, NSWorkspace=workspace)

    @staticmethod
    def _load_quick(appkit):
        """Load the launcher lookup with only a fake AppKit import."""
        native_builtins = dict(vars(builtins))
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            """Return the controlled AppKit module and delegate other imports."""
            if name == "AppKit":
                return appkit
            return real_import(name, globals, locals, fromlist, level)

        native_builtins["__import__"] = fake_import
        ns = {
            "__builtins__": native_builtins,
            "_validate_app_identifier": lambda value, field: None,
            "_check_request_access": lambda: None,
        }
        load_functions("launcher.py", {"_quick_pid_for_app"}, ns)
        return ns["_quick_pid_for_app"]

    @staticmethod
    def _app(pid, name, terminated=False, bundle_name=None):
        """Create a running-application record with observable identity fields."""
        url = None
        if bundle_name is not None:
            url = SimpleNamespace(lastPathComponent=lambda: bundle_name)
        return SimpleNamespace(
            processIdentifier=lambda: pid,
            isTerminated=lambda: terminated,
            localizedName=lambda: name,
            bundleURL=lambda: url,
        )

    def test_bundle_lookup_ignores_terminated_apps(self):
        """A terminated matching process cannot be returned as an existing app."""
        appkit = self._appkit_namespace(apps_by_bundle=[
            self._app(90, "Demo", terminated=True),
            self._app(91, "Demo"),
        ])
        lookup = self._load_quick(appkit)
        self.assertEqual(lookup("com.example.Demo", None), 91)

    def test_name_lookup_requires_exact_name_or_bundle_component(self):
        """Name lookup accepts exact app identity and excludes partial matches."""
        exact = self._app(123, "Demo", bundle_name="Demo.app")
        partial = self._app(456, "Demo Helper", bundle_name="Demo Helper.app")
        appkit = self._appkit_namespace(running_apps=[partial, exact])
        lookup = self._load_quick(appkit)
        self.assertEqual(lookup(None, "demo.app"), 123)

    def test_duplicate_active_name_matches_are_ambiguous(self):
        """Multiple active matches fail rather than selecting an arbitrary PID."""
        apps = [self._app(123, "Demo"), self._app(456, "Demo")]
        lookup = self._load_quick(self._appkit_namespace(running_apps=apps))
        with self.assertRaisesRegex(RuntimeError, "More than one"):
            lookup(None, "Demo")

    def test_native_lookup_error_does_not_run_open(self):
        """A native discovery error must stop launch before any open command."""
        popen = MagicMock()
        ns = {
            "_check_request_access": lambda: None,
            "_quick_pid_for_app": MagicMock(side_effect=RuntimeError("lookup failed")),
            "subprocess": SimpleNamespace(Popen=popen),
            "_find_pid_for_app": MagicMock(),
            "time": SimpleNamespace(sleep=MagicMock()),
            "CHROMIUM_BROWSERS": set(),
        }
        load_functions("launcher.py", {"launch_native_app"}, ns)
        with self.assertRaisesRegex(RuntimeError, "lookup failed"):
            ns["launch_native_app"](app_name="Demo")
        popen.assert_not_called()
        ns["_find_pid_for_app"].assert_not_called()

    def test_registered_cold_launch_has_no_fixed_delay(self):
        """An immediately registered app returns without a launch sleep or another open."""
        clock = SimpleNamespace(monotonic=lambda: 1.0, sleep=MagicMock())
        popen = MagicMock()
        ns = {"_check_request_access": lambda: None,
              "time": clock, "subprocess": SimpleNamespace(Popen=popen),
              "_quick_pid_for_app": MagicMock(side_effect=[None, 321]),
              "CHROMIUM_BROWSERS": {"Chromium"}}
        load_functions("launcher.py", {"launch_native_app", "_find_pid_for_app"}, ns)

        self.assertEqual(ns["launch_native_app"](app_name="Chromium"), (321, False))
        popen.assert_called_once_with(["/usr/bin/open", "-a", "Chromium", "--args", "--force-renderer-accessibility"])
        clock.sleep.assert_not_called()

    def test_registration_timeout_does_not_oversleep_its_budget(self):
        """A missing process uses monotonic time and caps its final polling interval."""
        now = [0.0]
        intervals = []

        def sleep(seconds):
            """Advance a controlled clock without waiting or operating the desktop."""
            intervals.append(seconds)
            now[0] += seconds

        ns = {"_check_request_access": lambda: None,
              "time": SimpleNamespace(monotonic=lambda: now[0], sleep=sleep),
              "_quick_pid_for_app": MagicMock(return_value=None)}
        load_functions("launcher.py", {"_find_pid_for_app"}, ns)

        with self.assertRaisesRegex(RuntimeError, "Could not find PID"):
            ns["_find_pid_for_app"](None, "Demo", timeout=0.15)
        self.assertEqual(len(intervals), 2)
        self.assertAlmostEqual(sum(intervals), 0.15)

    def test_registration_error_stops_polling_without_retry(self):
        """Ambiguous or failed identity lookup is never treated as absent registration."""
        clock = SimpleNamespace(monotonic=lambda: 1.0, sleep=MagicMock())
        ns = {"_check_request_access": lambda: None, "time": clock,
              "_quick_pid_for_app": MagicMock(side_effect=RuntimeError("ambiguous app"))}
        load_functions("launcher.py", {"_find_pid_for_app"}, ns)

        with self.assertRaisesRegex(RuntimeError, "ambiguous app"):
            ns["_find_pid_for_app"](None, "Demo")
        clock.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
