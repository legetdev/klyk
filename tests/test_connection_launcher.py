"""Revoked tool requests cannot begin later app, log or process-signal stages."""

import signal
import unittest
from unittest.mock import patch
from klyk import launcher, connection_gate as gate, connection_policy as policy
from test_connection_gate import IsolatedPolicy


class LauncherAccessTests(IsolatedPolicy):
    """Use real generation checks and inert process boundaries throughout."""

    def test_revoked_request_cannot_signal_or_start_log(self):
        """A late close-app or diagnostic stage stops before its first OS mutation."""
        request = self.enable()
        self.cycle()
        with gate.scope(request), patch.object(launcher, "_process_status", return_value="same"), \
                patch.object(launcher.os, "kill") as send, patch.object(launcher.subprocess, "Popen") as spawn:
            with self.assertRaises(policy.AccessDisabled):
                launcher.terminate_pid(12345, expected_identity={"started": (1, 0)})
            with self.assertRaises(policy.AccessDisabled):
                launcher.start_native_log_stream(12345)
            send.assert_not_called()
            spawn.assert_not_called()

    def test_revocation_after_term_prevents_kill_escalation(self):
        """A submitted TERM is not undone, but a later KILL must check the old generation."""
        request = self.enable()
        count = 0
        def inspect(*args):
            """Revoke immediately before the second identity-verified signal stage."""
            nonlocal count
            count += 1
            if count == 2:
                self.cycle()
            return "same"
        with gate.scope(request), patch.object(launcher, "_process_status", side_effect=inspect), \
                patch.object(launcher.os, "kill") as send:
            with self.assertRaises(policy.AccessDisabled):
                launcher.terminate_pid(12345, term_timeout=0, expected_identity={"started": (1, 0)})
            send.assert_called_once_with(12345, signal.SIGTERM)

    def test_cli_without_request_keeps_its_identity_checked_termination(self):
        """Independent CLI cleanup is not a computer tool and does not require an On switch."""
        with patch.object(launcher, "_process_status", side_effect=["same", "same", "gone"]), \
                patch.object(launcher.os, "kill") as send, patch.object(launcher.time, "sleep"), \
                patch.object(gate, "checkpoint") as checkpoint:
            self.assertTrue(launcher.terminate_pid(12345, term_timeout=0, expected_identity={"started": (1, 0)}))
            self.assertEqual(send.call_count, 2)
            checkpoint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
