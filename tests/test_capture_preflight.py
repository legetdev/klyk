"""Permission checks must never capture or write private desktop content."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


class CapturePreflightTests(unittest.TestCase):
    """Exercise granted, denied, and unsupported permission states."""

    def test_permission_states_do_not_capture_or_write_files(self):
        """Only the platform permission query may run, including failure paths."""
        for allowed in (True, False, None):
            with self.subTest(allowed=allowed):
                preflight = MagicMock(return_value=allowed)
                subprocess = MagicMock()
                tempfile = MagicMock()
                cg = SimpleNamespace() if allowed is None else SimpleNamespace(
                    CGPreflightScreenCaptureAccess=preflight)
                namespace = {'_cg': cg, 'ctypes': ctypes,
                             'subprocess': subprocess, 'tempfile': tempfile}
                load_functions('capture.py', {'check_screen_recording'}, namespace)
                if allowed:
                    self.assertIsNone(namespace['check_screen_recording']())
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Screen Recording permission'):
                        namespace['check_screen_recording']()
                if allowed is not None:
                    preflight.assert_called_once_with()
                self.assertEqual(subprocess.mock_calls, [])
                self.assertEqual(tempfile.mock_calls, [])
