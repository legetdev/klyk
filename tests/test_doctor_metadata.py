"""Keep explicit permission diagnostics independent from gated computer-use runtime."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from klyk import connection_policy as policy, doctor


class DoctorMetadataTests(unittest.TestCase):
    """Spy on fake first-party APIs; never probe native permissions or owner files."""

    def setUp(self):
        """Store a real all-Off policy in a private fixture, with an explicit client tag."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "connections.json"
        self.path.write_text(json.dumps(policy._default()))
        self.path.chmod(0o600)
        for patcher in (mock.patch.object(policy, "policy_path", return_value=self.path),
                        mock.patch.dict(os.environ, {"KLYK_CLIENT": "codex"}),
                        mock.patch.object(sys, "platform", "darwin")):
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def _without_runtime(self, *, allow_policy=False):
        """Reject runtime/native imports and initialization, not merely their output."""
        forbidden = {"AppKit", "Quartz", "Foundation", "computer", "capture", "controls",
                     "connection_gate", "ownership", "emergency", "ui_thread", "skylight"}
        if not allow_policy:
            forbidden.add("connection_policy")
        real_import = __import__

        def metadata_import(name, *args, **kwargs):
            """Fail if a permission query reaches any computer-use or controls module."""
            fromlist = kwargs.get("fromlist", args[2] if len(args) > 2 else ()) or ()
            if (name in forbidden or name.removeprefix("klyk.") in forbidden
                    or (name in ("", "klyk") and any(item in forbidden for item in fromlist))):
                raise AssertionError(f"unexpected runtime import: {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=metadata_import), \
             mock.patch.object(doctor, "private_directory", side_effect=AssertionError("no initialization")), \
             mock.patch.object(doctor, "open_private", side_effect=AssertionError("no log writes")), \
             mock.patch.object(doctor.subprocess, "run", side_effect=AssertionError("no external process")):
            yield

    def test_all_off_permission_metadata_uses_only_raw_first_party_queries(self):
        """Off blocks computer tools but does not falsify the owner's permission diagnostics."""
        self.assertFalse(policy.enabled())
        original = self.path.read_bytes()
        ax_query = mock.Mock(return_value=True)
        screen_query = mock.Mock(return_value=True)
        libraries = {
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices":
                types.SimpleNamespace(AXIsProcessTrustedWithOptions=ax_query),
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics":
                types.SimpleNamespace(CGPreflightScreenCaptureAccess=screen_query),
        }
        with self._without_runtime(), mock.patch.object(doctor.ctypes, "CDLL", side_effect=libraries.__getitem__) as load, \
             mock.patch.object(policy, "enabled", side_effect=AssertionError("permission metadata has no access gate")):
            ax_result = doctor.check_accessibility_permission()
            screen_result = doctor.check_screen_recording_permission()
        self.assertEqual(ax_result.status, "ok")
        self.assertEqual(screen_result.status, "ok")
        self.assertEqual(load.call_args_list, [mock.call(path) for path in libraries])
        ax_query.assert_called_once_with(None)
        screen_query.assert_called_once_with()
        self.assertEqual(ax_query.argtypes, [doctor.ctypes.c_void_p])
        self.assertIs(ax_query.restype, doctor.ctypes.c_bool)
        self.assertEqual(screen_query.argtypes, [])
        self.assertIs(screen_query.restype, doctor.ctypes.c_bool)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_permission_denials_keep_plain_actionable_failures(self):
        """A genuine raw API denial still directs the owner to the correct permission."""
        cases = ((doctor.check_accessibility_permission, "AXIsProcessTrustedWithOptions", "Accessibility"),
                 (doctor.check_screen_recording_permission, "CGPreflightScreenCaptureAccess", "Screen Recording"))
        for check, symbol, permission in cases:
            query = mock.Mock(return_value=False)
            with self.subTest(permission=permission), self._without_runtime(), \
                 mock.patch.object(doctor.ctypes, "CDLL", return_value=types.SimpleNamespace(**{symbol: query})):
                result = check()
            self.assertEqual(result.status, "fail")
            self.assertIn(permission, result.name)
            self.assertIn("System Settings", result.detail + result.remedy)
            self.assertNotIn("access is off", result.detail + result.remedy)
            self.assertNotIn("Traceback", result.detail + result.remedy)

    def test_permission_library_errors_remain_nonraising_and_hide_raw_error_text(self):
        """Unavailable library diagnostics expose the error kind, not arbitrary details."""
        for check, status in ((doctor.check_accessibility_permission, "fail"),
                              (doctor.check_screen_recording_permission, "warn")):
            with self.subTest(check=check.__name__), self._without_runtime(), \
                 mock.patch.object(doctor.ctypes, "CDLL", side_effect=OSError("SECRET-FIXTURE")):
                result = check()
            self.assertEqual(result.status, status)
            self.assertIn("OSError", result.detail)
            self.assertNotIn("SECRET-FIXTURE", result.detail + result.remedy)

    def test_missing_screen_preflight_remains_an_actionable_failure(self):
        """A missing documented symbol must not make permission status look granted."""
        with self._without_runtime(), mock.patch.object(doctor.ctypes, "CDLL", return_value=types.SimpleNamespace()):
            result = doctor.check_screen_recording_permission()
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.detail, "Screen Recording permission preflight is unavailable on this macOS version.")

    def test_all_off_delivery_skips_before_any_native_or_runtime_import(self):
        """Explicit diagnostics cannot run an input self-test for an Off environment."""
        original = self.path.read_bytes()
        with self._without_runtime(allow_policy=True), \
             mock.patch.object(doctor.ctypes, "CDLL", side_effect=AssertionError("no native probes")):
            result = doctor.check_skylight_delivery()
        self.assertEqual(result.status, "warn")
        self.assertIn("access is off", result.detail)
        self.assertIn("menu-bar controls", result.remedy)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_on_delivery_preserves_success_retry_failure_and_unavailable_behavior(self):
        """An explicitly enabled fixture still follows the existing bounded delivery check."""
        policy.set_enabled("codex", True)
        cases = ((True, [True], None, "ok"), (True, [False, True], False, "ok"),
                 (True, [False, False], False, "fail"), (True, [False, False], None, "warn"),
                 (False, [], None, "warn"))
        for available, answers, verified, status in cases:
            skylight = types.ModuleType("klyk.skylight")
            skylight.is_available = mock.Mock(return_value=available)
            skylight.self_test = mock.Mock(side_effect=answers)
            skylight.delivery_verified = mock.Mock(return_value=verified)
            with self.subTest(available=available, answers=answers, verified=verified), \
                 mock.patch.dict(sys.modules, {"klyk.skylight": skylight}), \
                 mock.patch.object(sys.modules["klyk"], "skylight", skylight, create=True):
                result = doctor.check_skylight_delivery()
            self.assertEqual(result.status, status)
            self.assertEqual(skylight.self_test.call_args_list, [mock.call(timeout=0.5)] * len(answers))

    def test_unknown_policy_state_never_reaches_delivery_test(self):
        """Policy-query failure remains a warning and cannot start native input probes."""
        with self._without_runtime(allow_policy=True), mock.patch.object(policy, "enabled", side_effect=OSError("fixture denied")):
            result = doctor.check_skylight_delivery()
        self.assertEqual(result.status, "warn")
        self.assertIn("OSError", result.detail)

    def test_non_macos_checks_skip_without_loading_libraries_or_policy(self):
        """Unsupported-platform metadata exits before touching native boundaries."""
        with mock.patch.object(sys, "platform", "linux"), self._without_runtime(), \
             mock.patch.object(doctor.ctypes, "CDLL", side_effect=AssertionError("no native libraries")):
            for check in (doctor.check_accessibility_permission, doctor.check_screen_recording_permission,
                          doctor.check_skylight_delivery):
                self.assertEqual(check().status, "warn")


if __name__ == "__main__":
    unittest.main()
