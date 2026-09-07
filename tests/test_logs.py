"""Regression coverage for structured diagnostic credential redaction."""

import json
import unittest

from klyk.logs import NativeLogCapture


class StructuredLogTests(unittest.TestCase):
    """Ensure credentials are scrubbed before structured stderr is retained."""

    def test_json_credentials_are_redacted_including_spaces_and_escapes(self):
        """Quoted keys and escaped value quotes must not bypass redaction."""
        capture = NativeLogCapture(1)
        credentials = {
            "password": 'my secret "password"',
            "TOKEN": "two words",
            "api_key": "key\\with\\slashes",
            "Authorization": "Bearer abc123",
            "event": "connection failed",
        }
        capture.append_stderr(json.dumps(credentials))
        retained = json.loads(capture.buffer.app_errors[0])
        for key in credentials:
            self.assertEqual(retained[key], credentials[key] if key == "event" else "***")
        self.assertNotIn("abc123", capture.buffer.app_errors[0])


if __name__ == "__main__":
    unittest.main()
