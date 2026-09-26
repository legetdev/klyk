"""Check grading captures use the target's refreshed window bounds."""

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


if __name__ == "__main__":
    unittest.main()
