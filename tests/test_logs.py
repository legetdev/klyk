"""Regression coverage for structured diagnostic credential redaction."""

import json
import contextlib
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from klyk import logs
from klyk.logs import LogBuffer, LogRecordBuffer, NativeLogCapture, StderrReader


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

    def test_oauth_aws_and_cookie_records_are_scrubbed(self):
        """Common compound credential names and cookie headers must be covered."""
        for value in ("access_token=synthetic-private", "refresh_token:synthetic-private",
                      "client_secret=synthetic-private", "AWS_SECRET_ACCESS_KEY=synthetic-private",
                      "private_key='synthetic-private'", "Cookie: a=synthetic-private; b=synthetic-other",
                      "Set-Cookie: a=synthetic-private; HttpOnly"):
            with self.subTest(value=value):
                self.assertNotIn("synthetic", logs._scrub(value))

    def test_split_and_unterminated_credentials_are_scrubbed(self):
        """Byte chunk boundaries and an incomplete quoted value preserve privacy."""
        records = LogRecordBuffer()
        self.assertEqual(records.feed(b"access_to"), [])
        self.assertEqual(records.feed(b"ken=synthetic-private\n"), ["access_token=***"])
        for value in (b'password="synthetic-private', b'{"refresh_token":"synthetic-private\\'):
            records = LogRecordBuffer()
            records.feed(value)
            self.assertNotIn("synthetic", records.partial())

    def test_oversized_unicode_record_is_dropped_as_a_whole(self):
        """The record cap measures bytes, including multi-byte Unicode values."""
        capture = NativeLogCapture(1)
        capture.append_stderr("\U0001f600" * 3000)
        self.assertEqual(list(capture.buffer.app_errors), ["[oversized log line omitted]"])


@contextlib.contextmanager
def temporary_logging(path):
    """Configure a private diagnostic logger and restore the caller's handlers."""
    logger = logging.getLogger("klyk")
    previous = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    try:
        logs.configure_logging(str(path))
        yield logger
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers, logger.level, logger.propagate = previous


class PersistentDiagnosticTests(unittest.TestCase):
    """Exercise real private files and safe handling of logging failures."""

    def test_file_handler_bounds_and_scrubs_records_without_tracebacks(self):
        """Neither oversized records nor exception payloads may persist."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostic.log"
            with temporary_logging(path) as logger:
                logger.warning("access_token=synthetic-private-token")
                logger.warning("operation failed: %s", ValueError("synthetic-private-exception"))
                logger.warning("x" * 20000)
                try:
                    raise ValueError("synthetic-private-request")
                except ValueError:
                    logger.error("operation failed", exc_info=True)
                for handler in logger.handlers:
                    handler.flush()
                contents = path.read_text()
            self.assertNotIn("synthetic-private", contents)
            self.assertIn("access_token=***", contents)
            self.assertIn("operation failed: ValueError", contents)
            self.assertIn("oversized diagnostic record omitted", contents)
            self.assertNotIn("Traceback", contents)
            self.assertLess(path.stat().st_size, logs._LOG_LINE_CAP)

    def test_forged_record_control_characters_are_escaped(self):
        """Injected newlines and terminal escapes must remain literal in one record."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostic.log"
            with temporary_logging(path) as logger:
                logger.warning("source\nforged\r\x1b[31m\u202eevil")
                logger.handlers[0].flush()
                contents = path.read_text()
            self.assertEqual(len(contents.splitlines()), 1)
            self.assertIn(r"\u000aforged\u000d\u001b", contents)
            self.assertIn(r"\u202e", contents)

    def test_failed_rotation_does_not_replay_raw_record_to_stderr(self):
        """I/O errors must not trigger logging's default Message/Arguments dump."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostic.log"
            with temporary_logging(path) as logger:
                logger.info("warmup")
                handler = logger.handlers[0]
                handler.maxBytes = 1
                stderr = io.StringIO()
                with patch.object(handler, "_open", side_effect=OSError("unavailable")), contextlib.redirect_stderr(stderr):
                    logger.warning("synthetic-private-request")
                self.assertEqual(stderr.getvalue(), "")

    def test_reconfiguration_replaces_and_closes_owned_handler(self):
        """Repeated setup must not duplicate diagnostic writes or retain old FDs."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostic.log"
            with temporary_logging(path) as logger:
                original = logger.handlers[0]
                logs.configure_logging(str(path))
                self.assertIsNone(original.stream)
                self.assertEqual(len(logger.handlers), 1)
                logger.info("single diagnostic")
                logger.handlers[0].flush()
                self.assertEqual(path.read_text().count("single diagnostic"), 1)


class StderrReaderLifecycleTests(unittest.TestCase):
    """Verify pipe shutdown independently of the writer's process lifetime."""

    def test_stop_closes_reader_while_writer_remains_open(self):
        """A quiet live pipe cannot block session cleanup or leak the reader thread."""
        read_fd, write_fd = os.pipe()
        pipe = os.fdopen(read_fd, "rb")
        reader = StderrReader(pipe, LogBuffer())
        try:
            started = time.monotonic()
            reader.stop()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(reader._thread.is_alive())
            self.assertTrue(pipe.closed)
            os.fstat(write_fd)
        finally:
            os.close(write_fd)

    def test_parent_exit_with_inherited_writer_does_not_delay_stop(self):
        """A descendant retaining stderr cannot prolong cleanup after its parent exits."""
        source = '''import subprocess,sys,time
subprocess.Popen([sys.executable,"-c","import time;time.sleep(0.8)"],stdout=subprocess.DEVNULL)
print("ready",flush=True)
time.sleep(30)
'''
        proc = subprocess.Popen([sys.executable, "-u", "-c", source], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline(), b"ready\n")
            reader = StderrReader(proc.stderr, LogBuffer())
            proc.terminate()
            proc.wait(timeout=2)
            started = time.monotonic()
            reader.stop()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(reader._thread.is_alive())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
