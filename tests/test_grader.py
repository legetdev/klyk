"""Check grading captures use the target's refreshed window bounds."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

import klyk
from klyk import grader, reporter
from klyk.logs import LogBuffer


class GraderTests(unittest.TestCase):
    """Exercise geometry bookkeeping without capturing the user's desktop."""

    def test_moved_window_uses_current_origin(self):
        """Refresh position as well as dimensions before capturing a moved window."""
        session = SimpleNamespace(
            pid=42, window_id=1, win_x=10, win_y=20,
            width=100, height=100, target="native",
        )
        capture = mock.Mock()
        capture.get_window_by_id.return_value = {
            "window_id": 1, "pid": 42,
            "x": 300, "y": 400, "width": 640, "height": 480,
        }
        capture.take_screenshot.return_value = ("pixels", 640, 480)
        with mock.patch.object(klyk, "capture", capture, create=True):
            result = grader.grade_ui(session)
        capture.take_screenshot.assert_called_once_with(
            window_id=1, logical_width=640, logical_height=480,
            win_x=300, win_y=400,
        )
        self.assertEqual(result["screenshot"], "pixels")
        capture.get_window_for_pid.assert_not_called()

    def test_grading_and_verdict_refuse_missing_or_reassigned_windows(self):
        """A vanished selected window cannot reveal a sibling or another process."""
        session = SimpleNamespace(pid=42, window_id=1)
        for window in (None, {"pid": 99, "window_id": 1}):
            for operation in (grader.grade_ui, lambda s: reporter.generate_verdict(s, "test")):
                capture = mock.Mock()
                capture.get_window_by_id.return_value = window
                with self.subTest(window=window), mock.patch.object(klyk, "capture", capture, create=True):
                    with self.assertRaisesRegex(RuntimeError, "selected window"):
                        operation(session)
                capture.take_screenshot.assert_not_called()

    def test_verdict_preserves_explicit_selection(self):
        """A different larger sibling must never replace the selected document."""
        session = SimpleNamespace(pid=42, window_id=1, screenshots_taken=0, log_buffer=LogBuffer())
        capture = mock.Mock()
        capture.get_window_by_id.return_value = {"pid": 42, "window_id": 1,
            "x": 10, "y": 20, "width": 300, "height": 200}
        capture.take_screenshot.return_value = ("selected pixels", 300, 200)
        with mock.patch.object(klyk, "capture", capture, create=True):
            result = reporter.generate_verdict(session, "test")
        self.assertEqual(result["screenshot"], "selected pixels")
        capture.get_window_by_id.assert_called_once_with(1)
        capture.get_window_for_pid.assert_not_called()
        self.assertEqual(capture.take_screenshot.call_args.kwargs['window_id'], 1)

    def test_valid_threshold_keeps_owner_setting(self):
        """Finite settings retain the documented inclusive 0–10 grading range."""
        for value, expected in (("0", 0.0), ("7.5", 7.5), ("10", 10.0)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"KLYK_UI_PASS_THRESHOLD": value}):
                self.assertEqual(grader.ui_pass_threshold(), (expected, None))

    def test_invalid_threshold_degrades_without_exposing_value(self):
        """Bad configuration cannot produce NaN JSON or suppress unrelated evidence."""
        session = SimpleNamespace(pid=42, window_id=1, target="native", screenshots_taken=0,
                                  log_buffer=LogBuffer())
        capture = mock.Mock()
        capture.get_window_by_id.return_value = {"pid": 42, "window_id": 1,
            "x": 10, "y": 20, "width": 300, "height": 200}
        capture.take_screenshot.return_value = ("selected pixels", 300, 200)
        for value in ("NaN", "inf", "-inf", "-1", "11", "owner-secret-sentinel", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"KLYK_UI_PASS_THRESHOLD": value}), \
                    mock.patch.object(klyk, "capture", capture, create=True):
                for result in (grader.grade_ui(session), reporter.generate_verdict(session, "test")):
                    self.assertEqual(result["pass_threshold"], 7.0)
                    self.assertEqual(result["configuration_warning"],
                                     "UI pass threshold must be a finite number from 0 to 10; using the default 7.0.")
                    encoded = json.dumps(result, allow_nan=False)
                    self.assertNotIn("owner-secret-sentinel", encoded)
                    self.assertEqual(result["screenshot"], "selected pixels")


if __name__ == "__main__":
    unittest.main()
