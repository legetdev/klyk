"""Check grading captures use the target's refreshed window bounds."""

from types import SimpleNamespace
import unittest
from unittest import mock

import klyk
from klyk import grader


class GraderTests(unittest.TestCase):
    """Exercise geometry bookkeeping without capturing the user's desktop."""

    def test_moved_window_uses_current_origin(self):
        """Refresh position as well as dimensions before capturing a moved window."""
        session = SimpleNamespace(
            pid=42, window_id=1, win_x=10, win_y=20,
            width=100, height=100, target="native",
        )
        capture = mock.Mock()
        capture.get_window_for_pid.return_value = {
            "window_id": 2,
            "bounds": {"X": 300, "Y": 400, "Width": 640, "Height": 480},
        }
        capture.take_screenshot.return_value = ("pixels", 640, 480)
        with mock.patch.object(klyk, "capture", capture, create=True):
            result = grader.grade_ui(session)
        capture.take_screenshot.assert_called_once_with(
            window_id=2, logical_width=640, logical_height=480,
            win_x=300, win_y=400,
        )
        self.assertEqual(result["screenshot"], "pixels")


if __name__ == "__main__":
    unittest.main()
