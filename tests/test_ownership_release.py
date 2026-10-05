"""Exercise revocation handoff using only private temporary owner/input files."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from klyk import ownership


class OwnershipReleaseTests(unittest.TestCase):
    """Never signal or initialize an app, and never use the owner's real token."""

    def setUp(self):
        """Give every ownership operation its own disposable private path."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.owner = Path(directory.name) / "owner"
        owner_path = mock.patch.object(ownership, "OWNER_PATH", self.owner)
        owner_path.start()
        self.addCleanup(owner_path.stop)

    def test_release_ours_writes_zero_without_replacing_file_or_signalling(self):
        """Release is an atomic token update; the inode and separate lease stay intact."""
        self.owner.write_text(f"{ownership._MY_PID}\n")
        inode = self.owner.stat().st_ino
        with mock.patch.object(ownership.os, "kill", side_effect=AssertionError("no signals")), \
             mock.patch.object(ownership, "_alive", side_effect=AssertionError("no process queries")), \
             mock.patch.object(Path, "unlink", side_effect=AssertionError("no unlinks")):
            self.assertTrue(ownership.release_ownership_if_owned())
            self.assertTrue(ownership.release_ownership_if_owned())
        self.assertEqual(self.owner.read_text(), "0\n")
        self.assertEqual(self.owner.stat().st_ino, inode)
        self.assertFalse(self.owner.with_name("owner.input").exists())

    def test_other_live_owner_and_non_owner_tokens_remain_exactly_unchanged(self):
        """No liveness check or rewrite is needed when the recorded token is not ours."""
        for content in (f"{os.getppid()}\n", "0\n", "", "malformed\n", "99999999999999999999999999\n"):
            with self.subTest(content=content):
                self.owner.write_text(content)
                with mock.patch.object(ownership.os, "kill", side_effect=AssertionError("no signals")), \
                     mock.patch.object(ownership, "_write_pid", side_effect=AssertionError("do not rewrite other tokens")):
                    self.assertTrue(ownership.release_ownership_if_owned())
                self.assertEqual(self.owner.read_text(), content)

    def test_busy_owner_file_fails_promptly_and_can_be_retried(self):
        """A contended owner lock cannot block the independent revocation watcher."""
        self.owner.write_text(f"{ownership._MY_PID}\n")
        original = self.owner.read_bytes()
        with self.owner.open("a+") as peer:
            fcntl.flock(peer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            start = time.monotonic()
            self.assertFalse(ownership.release_ownership_if_owned())
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(self.owner.read_bytes(), original)
            fcntl.flock(peer.fileno(), fcntl.LOCK_UN)
        self.assertTrue(ownership.release_ownership_if_owned())
        self.assertEqual(self.owner.read_text(), "0\n")

    def test_directory_fifo_and_linked_owner_files_fail_without_touching_targets(self):
        """Special or redirected token files cannot delay or redirect release writes."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            victim = base / "victim"
            victim.write_text(f"{ownership._MY_PID}\n")
            victim.chmod(0o640)
            linked = base / "linked"
            linked.symlink_to(victim)
            hardlinked = base / "hardlinked"
            os.link(victim, hardlinked)
            folder = base / "folder"
            folder.mkdir()
            fifo = base / "fifo"
            os.mkfifo(fifo, 0o600)
            for path in (linked, hardlinked, folder, fifo):
                with self.subTest(path=path.name), mock.patch.object(ownership, "OWNER_PATH", path):
                    start = time.monotonic()
                    self.assertFalse(ownership.release_ownership_if_owned())
                    self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(victim.read_text(), f"{ownership._MY_PID}\n")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o640)

    def test_unavailable_read_and_write_failures_return_false_and_leave_retry_possible(self):
        """Local failures do not raise into runtime revocation or retain the owner lock."""
        self.owner.write_text(f"{ownership._MY_PID}\n")
        with mock.patch.object(ownership, "_open", return_value=None):
            self.assertFalse(ownership.release_ownership_if_owned())
        with mock.patch.object(ownership, "_read_pid", side_effect=OSError("fixture read denied")):
            self.assertFalse(ownership.release_ownership_if_owned())
        with mock.patch.object(ownership, "_write_pid", side_effect=OSError("fixture write denied")):
            self.assertFalse(ownership.release_ownership_if_owned())
        self.assertEqual(self.owner.read_text(), f"{ownership._MY_PID}\n")
        self.assertTrue(ownership.release_ownership_if_owned())

    def test_clearing_owner_cannot_bypass_the_retained_input_cleanup_lease(self):
        """Another real Python process remains blocked until the original request exits."""
        ownership.claim_ownership()
        env = {**os.environ, "KLYK_OWNER_FILE": str(self.owner), "KLYK_UPDATE_CHECK": "0",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        code = (
            "from klyk import ownership\n"
            "try:\n"
            " with ownership.control_request():\n"
            "  print('controlled')\n"
            "except RuntimeError:\n"
            " print('busy')\n"
        )
        with ownership.control_request():
            input_path = self.owner.with_name("owner.input")
            input_inode = input_path.stat().st_ino
            self.assertTrue(ownership.release_ownership_if_owned())
            self.assertEqual(self.owner.read_text(), "0\n")
            self.assertEqual(input_path.stat().st_ino, input_inode)
            blocked = subprocess.run([sys.executable, "-P", "-B", "-c", code], env=env,
                                     capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(blocked.stdout.strip(), "busy")
            self.assertEqual(blocked.stderr, "")
            self.assertEqual(self.owner.read_text(), "0\n")
        resumed = subprocess.run([sys.executable, "-P", "-B", "-c", code], env=env,
                                 capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(resumed.stdout.strip(), "controlled")
        self.assertEqual(resumed.stderr, "")


if __name__ == "__main__":
    unittest.main()
