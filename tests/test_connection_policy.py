"""Real-file tests for revocation, client isolation, unsafe state and responsive controls."""

import json
import os
import pwd
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from klyk import connection_policy as policy, controls


class ConnectionPolicyTests(unittest.TestCase):
    """Exercise the persisted trust boundary without importing any desktop APIs."""

    def setUp(self):
        """Use a separate owner-private directory for every policy test."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "connections.json"
        self.location = patch.object(policy, "policy_path", return_value=self.path)
        self.location.start()
        self.addCleanup(self.location.stop)
        self.addCleanup(self.temp.cleanup)

    def write(self, value):
        """Write malformed or external state as an independently supplied file."""
        self.path.write_text(value if isinstance(value, str) else json.dumps(value))
        self.path.chmod(0o600)

    def test_missing_policy_and_invalid_identity_deny_access(self):
        """A fresh or unrecognized client must not gain desktop access implicitly."""
        self.assertFalse(policy.enabled("codex"))
        self.assertFalse(policy.enabled("not-a-client"))
        with patch.object(policy, "current_client", return_value=None):
            self.assertFalse(policy.enabled())
            with self.assertRaises(policy.AccessDisabled):
                policy.token()

    def test_client_home_cannot_redirect_the_owner_policy(self):
        """A per-client HOME override must not create a separate permissive policy."""
        self.location.stop()
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".klyk" / "connections.json"
        with patch.dict(os.environ, {"HOME": self.temp.name, "KLYK_POLICY_PATH": str(self.path)}):
            self.assertEqual(policy.policy_path(), expected)

    def test_off_on_never_revalidates_old_request(self):
        """Turning access back on must not revive work queued before revocation."""
        policy.set_enabled("codex", True)
        old = policy.token("codex")
        self.assertTrue(policy.allows(old))
        policy.set_enabled("codex", False)
        self.assertFalse(policy.allows(old))
        policy.set_enabled("codex", True)
        self.assertFalse(policy.allows(old))
        self.assertTrue(policy.allows(policy.token("codex")))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_client_revocation_is_independent(self):
        """Disabling Codex must leave Claude's exact active generation untouched."""
        policy.set_enabled("codex", True)
        policy.set_enabled("claude", True)
        claude = policy.token("claude")
        policy.set_enabled("codex", False)
        self.assertTrue(policy.allows(claude))
        self.assertFalse(policy.enabled("codex"))
        self.assertTrue(policy.enabled("claude"))

    def test_initialize_preserves_saved_off_preference(self):
        """Installing or opening the controls must never override an explicit Off."""
        policy.initialize("codex")
        self.assertTrue(policy.enabled("codex"))
        policy.set_enabled("codex", False)
        original = self.path.read_bytes()
        policy.initialize("codex")
        policy.initialize()
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse(policy.enabled("codex"))

    def test_bad_types_duplicates_and_unknown_schema_fail_closed(self):
        """External JSON cannot smuggle truthy switches or ambiguous policy values."""
        baseline = policy.initialize()
        cases = ["{\"version\":1,\"version\":1}", "[true]", "not json", "{}"]
        for field, bad in [("enabled", 1), ("enabled", "true"), ("generation", True), ("generation", -1)]:
            value = json.loads(json.dumps(baseline))
            value["clients"]["codex"][field] = bad
            cases.append(value)
        for case in cases:
            with self.subTest(case=case):
                self.write(case)
                self.assertFalse(policy.enabled("codex"))
                self.assertIn("error", policy.snapshot())
                with self.assertRaises((ValueError, OSError)):
                    policy.set_enabled("codex", True)

    def test_symlink_hardlink_fifo_oversize_and_public_permissions_deny(self):
        """Unsafe files must neither grant access nor block the switch reader."""
        real = Path(self.temp.name) / "real.json"
        policy.initialize("codex")
        raw = self.path.read_bytes()
        real.write_bytes(raw)
        real.chmod(0o600)
        self.path.unlink()
        self.path.symlink_to(real)
        self.assertFalse(policy.enabled("codex"))
        with self.assertRaises((ValueError, OSError)):
            policy.set_enabled("codex", True)
        self.assertEqual(real.read_bytes(), raw)
        self.path.unlink()
        os.link(real, self.path)
        self.assertFalse(policy.enabled("codex"))
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        start = time.monotonic()
        self.assertFalse(policy.enabled("codex"))
        self.assertLess(time.monotonic() - start, 0.5)
        self.path.unlink()
        self.write(" " * 16_385)
        self.assertFalse(policy.enabled("codex"))
        self.write(raw.decode())
        self.path.chmod(0o644)
        self.assertFalse(policy.enabled("codex"))

    def test_concurrent_switches_do_not_lose_other_clients(self):
        """Separate writers must serialize the real file and preserve each other's intent."""
        policy.initialize()
        barrier = threading.Barrier(3)
        errors = []

        def write_client(key):
            """Race independent client changes against the same private lock."""
            barrier.wait()
            try:
                policy.set_enabled(key, True)
            except Exception as error:
                errors.append(type(error).__name__)

        workers = [threading.Thread(target=write_client, args=(key,)) for key in ("codex", "claude")]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(policy.enabled("codex"))
        self.assertTrue(policy.enabled("claude"))

    def test_malformed_tokens_are_not_accepted(self):
        """Only an exact canonical client and integer generation can authorize late work."""
        policy.set_enabled("codex", True)
        for value in (None, [], ([], 1), ("codex", True), ("codex", -1), ("codex", 1, 2)):
            with self.subTest(value=value):
                self.assertFalse(policy.allows(value))

    def test_controller_starts_without_secret_environment_or_native_imports(self):
        """A background controller must not inherit credentials or execute desktop probes."""
        with patch.object(controls.sys, "platform", "darwin"), patch.object(controls, "running", return_value=False), \
                patch.object(controls.subprocess, "Popen") as spawn, \
                patch.object(controls, "_wait_ready", return_value=True), \
                patch.dict(os.environ, {"SECRET_TEST_TOKEN": "not-to-be-inherited", "PYTHONPATH": "unsafe"}):
            self.assertTrue(controls.start_background())
        args, kwargs = spawn.call_args
        self.assertEqual(args[0][1:5], ["-P", "-m", "klyk.controls", "--ready-fd"])
        self.assertNotIn("SECRET_TEST_TOKEN", kwargs["env"])
        self.assertEqual(kwargs["env"]["PYTHONPATH"], "")
        self.assertEqual(kwargs["stdin"], controls.subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertIsNone(controls._native_types)


if __name__ == "__main__":
    unittest.main()
