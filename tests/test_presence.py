"""Real-file tests for live server presence, using only a private temporary Klyk folder."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from klyk import connection_policy as policy, presence


def dead_pid() -> int:
    """Return the PID of a process that has already exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class PresenceTests(unittest.TestCase):
    """Writers and readers share one isolated folder; no real server or controller is touched."""

    def setUp(self):
        """Point Klyk's state at a temporary home and reset this process's presence."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / ".klyk"
        self.state.mkdir(mode=0o700)
        self.folder = self.state / "presence"
        self.own = self.folder / f"{os.getpid()}.json"
        location = patch.object(policy, "policy_path", return_value=self.state / "connections.json")
        location.start()
        self.addCleanup(location.stop)
        # The test process must not leave an exit hook behind.
        hook = patch.object(presence.atexit, "register")
        self.register = hook.start()
        self.addCleanup(hook.stop)
        for name, value in (("_state", None), ("_written", (None, None, float("-inf"))), ("_registered", False)):
            reset = patch.object(presence, name, value)
            reset.start()
            self.addCleanup(reset.stop)

    def put(self, pid, client="claude", *, acted=None, app=None, started=None, raw=None):
        """Plant one presence file as another server would have written it."""
        self.folder.mkdir(mode=0o700, exist_ok=True)
        path = self.folder / f"{pid}.json"
        if raw is None:
            raw = json.dumps({"pid": pid, "client": client, "version": "0.7.0",
                              "started_at": presence.time.time() if started is None else started,
                              "last_action_at": acted, "last_app": app, "last_tool": "click" if app else None})
        path.write_text(raw)
        path.chmod(0o600)
        return path

    def test_connected_writes_an_owner_only_file_with_exact_fields(self):
        """The announcement is private, atomic and holds no screen content."""
        presence.connected("claude")
        data = json.loads(self.own.read_text())
        self.assertEqual(set(data), {"pid", "client", "version", "started_at", "last_action_at", "last_app", "last_tool"})
        self.assertEqual((data["pid"], data["client"], data["version"]), (os.getpid(), "claude", presence.__version__))
        self.assertIsNone(data["last_action_at"])
        self.assertEqual(stat.S_IMODE(self.own.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual([path.name for path in self.folder.iterdir()], [self.own.name])  # No temporary left.
        self.register.assert_called_once_with(presence.clear)

    def test_unknown_client_is_unattributed(self):
        """An invalid identity never appears as a real environment."""
        presence.connected("not-a-client")
        self.assertIsNone(json.loads(self.own.read_text())["client"])
        self.assertFalse(any(row["connected"] for row in presence.summary().values()))

    def test_record_rate_limits_identical_writes_but_not_changes(self):
        """Busy servers rewrite at most twice a second, yet a new app or tool shows at once."""
        clock = [1000.0]
        with patch.object(presence.time, "monotonic", side_effect=lambda: clock[0]):
            presence.connected("codex")
            self.assert_rate_limited(clock)
        data = json.loads(self.own.read_text())
        self.assertEqual((data["client"], data["last_app"], data["last_tool"]), ("codex", "Safari", "type_text"))
        self.assertIsNotNone(data["last_action_at"])

    def assert_rate_limited(self, clock):
        """Count real writes while the monotonic clock is advanced by hand."""
        with patch.object(presence, "_write_locked", wraps=presence._write_locked) as write:
            presence.record("codex", "Notes", "click")
            presence.record("codex", "Notes", "click")
            clock[0] += 0.2
            presence.record("codex", "Notes", "click")
            self.assertEqual(write.call_count, 1)
            presence.record("codex", "Notes", "type_text")
            presence.record("codex", "Safari", "type_text")
            self.assertEqual(write.call_count, 3)
            clock[0] += 0.5
            presence.record("codex", "Safari", "type_text")
            self.assertEqual(write.call_count, 4)

    def test_record_sanitizes_display_text(self):
        """App names are single-line and bounded before they reach the panel."""
        presence.connected("claude")
        presence.record("claude", "Evil\nApp\x1b[31m" + "x" * 200, "tool\r")
        data = json.loads(self.own.read_text())
        self.assertEqual(len(data["last_app"]), 80)
        self.assertTrue(data["last_app"].isprintable())
        self.assertEqual(data["last_tool"], "tool")

    def test_record_and_clear_never_raise(self):
        """A full disk or broken folder can never fail a tool request or shutdown."""
        presence.connected("claude")
        with patch.object(presence, "_write_locked", side_effect=OSError("disk full")):
            self.assertIsNone(presence.record("claude", "Notes", "click"))
        with patch.object(presence.os, "unlink", side_effect=PermissionError("fixture")):
            self.assertIsNone(presence.clear())
        with patch.object(policy, "policy_path", side_effect=KeyError("no account")):
            self.assertIsNone(presence.connected("claude"))
            self.assertIsNone(presence.record("claude", "Notes", "click"))
            self.assertIsNone(presence.clear())
            self.assertEqual(presence.entries(), [])

    def test_record_never_recreates_state_removed_by_uninstall(self):
        """A lingering server cannot bring back a deleted Klyk folder."""
        presence.connected("claude")
        self.own.unlink()
        self.folder.rmdir()
        self.state.rmdir()
        presence.record("claude", "Notes", "click")
        self.assertFalse(self.state.exists())

    def test_record_before_announcement_and_after_clear_is_silent(self):
        """Only an announced, live server publishes; clear removes exactly its own file."""
        presence.record("claude", "Notes", "click")
        self.assertFalse(self.folder.exists())
        other = self.put(os.getppid(), "codex")
        presence.connected("claude")
        presence.clear()
        self.assertFalse(self.own.exists())
        presence.record("claude", "Notes", "click")
        self.assertFalse(self.own.exists())
        self.assertTrue(other.exists())

    def test_summary_reports_connected_active_and_app(self):
        """Each environment shows whether it is connected and where it is working right now."""
        now = presence.time.time()
        self.put(os.getpid(), "claude", acted=now - 1, app="Safari")
        self.put(os.getppid(), "codex", acted=now - 6, app="Notes")
        result = presence.summary(now=now)
        self.assertEqual(result["claude"], {"connected": True, "active": True, "app": "Safari"})
        self.assertEqual(result["codex"], {"connected": True, "active": False, "app": None})
        self.assertEqual(result["opencode"], {"connected": False, "active": False, "app": None})
        self.assertEqual(set(result), set(policy.CLIENT_KEYS))
        self.assertFalse(presence.summary(now=now + 10)["claude"]["active"])

    def test_two_servers_of_one_environment_show_the_most_recent_app(self):
        """Separate sessions of the same client merge into one row without flicker."""
        now = presence.time.time()
        self.put(os.getpid(), "claude", acted=now - 3, app="Notes")
        self.put(os.getppid(), "claude", acted=now - 1, app="Mail")
        self.assertEqual(presence.summary(now=now)["claude"]["app"], "Mail")

    def test_readers_remove_dead_malformed_oversized_and_mismatched_files(self):
        """Stale or broken entries disappear instead of showing a false connection."""
        gone = dead_pid()
        stale = self.put(gone, "codex")
        broken = self.put(os.getppid(), raw="{")
        large = self.folder / f"{os.getpid()}.json"
        large.write_text(json.dumps({"pid": os.getpid(), "padding": "x" * 4096}))
        large.chmod(0o600)
        wrong = self.folder / "123456789.json"
        wrong.write_text(json.dumps({"pid": os.getppid()}))
        wrong.chmod(0o600)
        self.assertFalse(any(row["connected"] for row in presence.summary().values()))
        for path in (stale, broken, large, wrong):
            self.assertFalse(path.exists(), path.name)

    def test_one_impossible_pid_never_hides_the_other_servers(self):
        """A name no process can have is dropped while real servers stay listed."""
        bogus = self.put(9999999999, "codex")
        self.put(os.getpid(), "claude")
        result = presence.summary()
        self.assertTrue(result["claude"]["connected"])
        self.assertFalse(result["codex"]["connected"])
        self.assertFalse(bogus.exists())

    def test_readers_remove_symlinks_without_touching_their_target(self):
        """A planted link is dropped; the file it points at is never read or changed."""
        self.folder.mkdir(mode=0o700)
        target = self.state / "precious"
        target.write_text(json.dumps({"pid": os.getpid()}))
        link = self.folder / f"{os.getpid()}.json"
        link.symlink_to(target)
        self.assertFalse(presence.summary()["claude"]["connected"])
        self.assertFalse(link.is_symlink())
        self.assertEqual(json.loads(target.read_text()), {"pid": os.getpid()})

    def test_readers_remove_files_owned_by_someone_else(self):
        """Only this user's servers can claim a connection."""
        path = self.put(os.getpid(), "claude")
        real = os.getuid()
        with patch.object(presence.os, "getuid", return_value=real + 1):
            entries = presence.entries()
        self.assertEqual(entries, [])
        self.assertFalse(path.exists())

    def test_reused_pid_is_not_a_connection(self):
        """A process that started after the file was written is not the server that wrote it."""
        path = self.put(os.getppid(), "codex", started=1000.0)
        from klyk import launcher
        with patch.object(launcher, "process_identity", return_value={"started": (5000, 0)}):
            self.assertFalse(presence.summary()["codex"]["connected"])
        self.assertFalse(path.exists())
        kept = self.put(os.getppid(), "codex", started=6000.0)
        with patch.object(launcher, "process_identity", return_value={"started": (5000, 0)}):
            self.assertTrue(presence.summary()["codex"]["connected"])
        self.assertTrue(kept.exists())

    def test_reads_at_most_64_files_per_pass(self):
        """A flooded folder costs bounded work on the panel's main thread."""
        self.folder.mkdir(mode=0o700)
        for index in range(80):
            path = self.folder / f"{1000000 + index}.json"
            path.write_text("{")
        with patch.object(presence, "_load", wraps=presence._load) as load:
            presence.summary()
        self.assertEqual(load.call_count, 64)
        self.assertEqual(len(list(self.folder.iterdir())), 16)

    def test_leftover_temporary_of_a_dead_writer_is_removed(self):
        """A crash mid-write leaves nothing behind, while a live writer's temporary is kept."""
        self.folder.mkdir(mode=0o700)
        dead = self.folder / f".{dead_pid()}.0123abcd.tmp"
        live = self.folder / f".{os.getpid()}.0123abcd.tmp"
        dead.write_text("x")
        live.write_text("x")
        presence.summary()
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_write_replaces_a_planted_link_instead_of_following_it(self):
        """Publishing can never overwrite a file outside the presence folder."""
        self.folder.mkdir(mode=0o700)
        target = self.state / "precious"
        target.write_text("keep")
        self.own.symlink_to(target)
        presence.connected("claude")
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(self.own.is_symlink())
        self.assertEqual(json.loads(self.own.read_text())["client"], "claude")

    def test_linked_presence_folder_is_refused(self):
        """A presence folder that is a link to elsewhere receives nothing."""
        elsewhere = self.state / "elsewhere"
        elsewhere.mkdir()
        self.folder.symlink_to(elsewhere, target_is_directory=True)
        presence.connected("claude")
        presence.record("claude", "Notes", "click")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_clear_during_a_write_leaves_no_file(self):
        """An exit racing the final write cannot leave this server listed as connected."""
        presence.connected("claude")
        real = os.replace

        def replace_then_clear(source, target):
            """Simulate the exit hook running between the rename and the write's completion."""
            real(source, target)
            presence._state = None

        with patch.object(presence.os, "replace", side_effect=replace_then_clear):
            presence.record("claude", "Notes", "click")
        self.assertFalse(self.own.exists())


if __name__ == "__main__":
    unittest.main()
