"""Regression tests for ownership, activity retention, and log hygiene."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from klyk import activity, logs, ownership


class OwnershipTests(unittest.TestCase):
    """Exercise ownership with a temporary token and mocked process liveness."""

    def setUp(self) -> None:
        """Redirect the ownership token to a disposable file for each test."""
        self._temporary = tempfile.TemporaryDirectory()
        self.owner_path = Path(self._temporary.name) / "owner"
        self.owner_patch = mock.patch.object(ownership, "OWNER_PATH", self.owner_path)
        self.owner_patch.start()

    def tearDown(self) -> None:
        """Restore ownership globals and remove the disposable token."""
        self.owner_patch.stop()
        self._temporary.cleanup()

    def _write_owner(self, pid: int) -> None:
        """Write a controlled owner pid into the isolated token file."""
        self.owner_path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_path.write_text(f"{pid}\n", encoding="utf-8")

    def test_current_process_claims_free_or_dead_owner(self) -> None:
        """A free token and a dead owner both become this process's token."""
        self.assertEqual(ownership.claim_ownership_if_unowned(), ownership._MY_PID)
        self.assertTrue(ownership.is_owner())

        self._write_owner(42424242)
        with mock.patch.object(ownership, "_alive", return_value=False):
            self.assertEqual(ownership.claim_ownership_if_unowned(), ownership._MY_PID)
            self.assertTrue(ownership.is_owner())
        self.assertEqual(ownership.current_owner(), ownership._MY_PID)

    def test_live_owner_is_preserved_until_forceful_transfer(self) -> None:
        """Startup defers to a live owner while explicit claim transfers control."""
        previous = 4242
        self._write_owner(previous)
        with mock.patch.object(ownership, "_alive", return_value=True):
            self.assertEqual(ownership.claim_ownership_if_unowned(), previous)
            self.assertFalse(ownership.is_owner())
            self.assertEqual(ownership.current_owner(), previous)
            self.assertEqual(ownership.claim_ownership(), previous)
        self.assertEqual(ownership.current_owner(), ownership._MY_PID)

    def test_unavailable_owner_storage_fails_closed(self) -> None:
        """Storage failure must never grant unverified control to a caller."""
        with mock.patch.object(ownership, "_open", return_value=None):
            self.assertFalse(ownership.is_owner())
            self.assertEqual(ownership.claim_ownership_if_unowned(), 0)
            with self.assertRaisesRegex(RuntimeError, "owner|control|storage"):
                ownership.claim_ownership()


class ActivityRecorderTests(unittest.TestCase):
    """Verify activity state is bounded, observable, and isolated per app."""

    def test_record_coerces_coordinates_and_notifies_observers(self) -> None:
        """Record a useful action snapshot and notify subscribers once."""
        recorder = activity.ActivityRecorder()
        observed: list[dict] = []

        def observer(entry: dict) -> None:
            """Collect one callback payload for assertion."""
            observed.append(entry)

        recorder.subscribe(observer)
        entry = recorder.record(
            "TextEdit", "click", x=10.9, y=20.1, session_mode="autonomous", pid=123
        )

        self.assertEqual(entry["x"], 10)
        self.assertEqual(entry["y"], 20)
        self.assertEqual(observed, [entry])
        summary = recorder.get_summary()[0]
        self.assertEqual(summary["last_action"], "click")
        self.assertEqual(summary["mode"], "autonomous")
        self.assertEqual(summary["pid"], 123)

    def test_per_app_history_is_bounded_and_oldest_entries_drop(self) -> None:
        """Retain at most the documented 200 recent entries per app."""
        recorder = activity.ActivityRecorder()
        for index in range(205):
            recorder.record("TextEdit", f"action-{index}")

        self.assertEqual(len(recorder._per_app["TextEdit"]), activity._MAX_PER_APP)
        self.assertEqual(recorder.get_recent("TextEdit", 1)[0]["tool"], "action-204")
        self.assertEqual(recorder.get_recent("TextEdit", 200)[0]["tool"], "action-5")

    def test_app_history_uses_lru_eviction_at_outer_cap(self) -> None:
        """Evict the oldest app while retaining the newest app histories."""
        recorder = activity.ActivityRecorder()
        with mock.patch.object(activity, "_MAX_APPS", 2), mock.patch.object(
            activity.time, "time", side_effect=[1.0, 2.0, 3.0]
        ):
            recorder.record("A", "one")
            recorder.record("B", "two")
            recorder.record("C", "three")

        self.assertNotIn("A", recorder._per_app)
        self.assertEqual(set(recorder._per_app), {"B", "C"})

    def test_dispatch_filter_and_via_stamp(self) -> None:
        """Record only user-visible tools and stamp their delivery path."""
        original = activity.recorder
        activity.recorder = activity.ActivityRecorder()
        try:
            class SessionStub:
                """Minimal session shape consumed by record_from_args."""

                app = "TextEdit"
                mode = "background"
                escalation_log = [{"reason": "test"}]
                pid = 55
                win_x = 4
                win_y = 5

            activity.record_from_args(SessionStub(), "type_text", {"text": "hello"})
            activity.record_from_args(SessionStub(), "list_sessions", {})
            activity.record_via("TextEdit", "type_text", "skylight")
            entry = activity.recorder.get_last("TextEdit")
        finally:
            activity.recorder = original

        self.assertIsNotNone(entry)
        self.assertEqual(entry["detail"], "5 chars")
        self.assertEqual(entry["via"], "skylight")
        self.assertEqual(entry["win_x"], 4)

    def test_remove_app_clears_all_retained_state(self) -> None:
        """Closing an app removes history, summary state, and last action."""
        recorder = activity.ActivityRecorder()
        recorder.record("TextEdit", "click", session_mode="humanoid")
        recorder.remove_app("TextEdit")

        self.assertEqual(recorder.get_recent("TextEdit"), [])
        self.assertIsNone(recorder.get_last("TextEdit"))
        self.assertEqual(recorder.get_summary(), [])


class LogHygieneTests(unittest.TestCase):
    """Verify bounded stderr capture and credential scrubbing."""

    def test_scrubs_common_credentials_before_retention(self) -> None:
        """Hide password, bearer, AWS key, and JWT values in captured lines."""
        capture = logs.NativeLogCapture(123)
        capture.append_stderr("password=swordfish token:abc123 secret=hidden")
        capture.append_stderr("Authorization: Bearer abcdefghijklmnop")
        capture.append_stderr("Authorization: Bearer x")
        capture.append_stderr("Bearer tok:en/with,punct")
        capture.append_stderr("AWS AKIAIOSFODNN7EXAMPLE")
        capture.append_stderr("jwt=eyJhbGciOiJIUzI1NiJ9.payloadsegment.signaturesegment")
        lines = capture.buffer.to_dict()["app_errors"]
        joined = "\n".join(lines)

        for secret in ("swordfish", "abc123", "hidden", "abcdefghijklmnop", "tok:en/with,punct", "AKIAIOSFODNN7EXAMPLE", "payloadsegment"):
            self.assertNotIn(secret, joined)
        self.assertIn("password=***", joined)
        self.assertIn("Bearer ***", joined)
        self.assertIn("JWT", joined)

    def test_capture_is_bounded_and_response_budget_drops_oldest(self) -> None:
        """Keep 500 lines and trim oldest lines when a payload budget is set."""
        capture = logs.NativeLogCapture(123)
        for index in range(501):
            capture.append_stderr(f"line-{index}")

        channel = capture.buffer.to_dict()["app_errors"]
        self.assertEqual(len(channel), logs._LOG_CHANNEL_CAP)
        self.assertEqual(channel[0], "line-1")
        bounded = capture.buffer.to_dict(max_chars=30)
        self.assertTrue(bounded["_truncated"])
        self.assertNotIn("line-1", bounded["app_errors"])
        self.assertIn("line-500", bounded["app_errors"])

    def test_scrubbing_is_idempotent_for_existing_redactions(self) -> None:
        """Already-redacted values remain redacted on another capture pass."""
        self.assertEqual(logs._scrub("password=*** token=***"), "password=*** token=***")


if __name__ == "__main__":
    unittest.main()
