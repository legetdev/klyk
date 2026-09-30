"""Exercise setup safety with temporary files and inert process/network fixtures."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from klyk import cli, clients, doctor, updates


class SetupSecurityTests(unittest.TestCase):
    """Keep every installer, permission prompt and process signal fake."""

    def setUp(self):
        """Capture diagnostic text without opening applications or native prompts."""
        self.output = io.StringIO()
        self.errors = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        errors = contextlib.redirect_stderr(self.errors)
        output.__enter__()
        errors.__enter__()
        self.addCleanup(errors.__exit__, None, None, None)
        self.addCleanup(output.__exit__, None, None, None)

    def _identity(self, pid, argv=None, uid=None):
        """Build a stable synthetic process identity for restart selection."""
        return {"pid": pid, "uid": os.getuid() if uid is None else uid,
                "started": (1234, pid), "executable": sys.executable,
                "argv": argv or [sys.executable, "-P", "-m", "klyk.mcp_server"]}

    def test_help_and_unknown_flags_never_run_side_effect_commands(self):
        """Asking for help or a nonexistent dry-run must never uninstall or update."""
        for verb in ("install", "uninstall", "update", "doctor", "restart"):
            for flag in ("--help", "-h", "--dry-run"):
                with self.subTest(verb=verb, flag=flag), \
                     mock.patch.object(sys, "argv", ["klyk", verb, flag]), \
                     mock.patch.object(cli, "_install") as install, \
                     mock.patch.object(cli, "_uninstall") as uninstall, \
                     mock.patch.object(cli, "_update") as update, \
                     mock.patch.object(cli, "_doctor") as diagnose, \
                     mock.patch.object(cli, "_restart") as restart:
                    if flag == "--dry-run":
                        with self.assertRaises(SystemExit) as exit_status:
                            cli.main()
                        self.assertEqual(exit_status.exception.code, 2)
                    else:
                        cli.main()
                    for command in (install, uninstall, update, diagnose, restart):
                        command.assert_not_called()

    def test_extra_positionals_and_client_all_conflict_fail_before_dispatch(self):
        """Mistyped arguments cannot expand removal scope or choose a different client."""
        for arguments in (("uninstall", "cursor", "--all"), ("uninstall", "cursor", "claude"),
                          ("update", "extra"), ("restart", "extra")):
            with self.subTest(arguments=arguments), \
                 mock.patch.object(sys, "argv", ["klyk", *arguments]), \
                 mock.patch.object(cli, "_uninstall") as remove, \
                 mock.patch.object(cli, "_update") as upgrade, \
                 mock.patch.object(cli, "_restart") as restart:
                with self.assertRaises(SystemExit) as exit_status:
                    cli.main()
                self.assertEqual(exit_status.exception.code, 2)
                remove.assert_not_called()
                upgrade.assert_not_called()
                restart.assert_not_called()

    def test_restart_selection_requires_uid_exact_interpreter_and_module(self):
        """PID ownership alone does not authorize killing another app or virtualenv."""
        variants = (([sys.executable, "-m", "klyk.mcp_server"], None, True),
                    ([sys.executable, "-P", "-m", "klyk.mcp_server"], None, True),
                    ([str(Path(sys.executable).with_name("other-python")), "-m", "klyk.mcp_server"], None, False),
                    ([sys.executable, "-c", "# klyk.mcp_server"], None, False),
                    ([sys.executable, "-m", "klyk.mcp_server", "extra"], None, False),
                    ([sys.executable, "-m", "klyk.mcp_server"], os.getuid() + 1, False))
        for argv, uid, accepted in variants:
            identity = self._identity(123, argv, uid)
            fake = types.ModuleType("klyk.launcher")
            fake.process_identity = mock.Mock(return_value=identity)
            with self.subTest(argv=argv, uid=uid), mock.patch.dict(sys.modules, {"klyk.launcher": fake}):
                self.assertEqual(cli._installation_process(123), identity if accepted else None)
                fake.process_identity.assert_called_once_with(123, include_command=True)

    def test_restart_rejects_invalid_and_reused_owner_without_signalling(self):
        """A stale owner file can never target a reused PID or a special process group."""
        fake = types.ModuleType("klyk.launcher")
        fake.process_identity = mock.Mock(return_value=None)
        fake.terminate_pid = mock.Mock()
        ownership = types.ModuleType("klyk.ownership")
        ownership.current_owner = mock.Mock(return_value=123)
        with mock.patch.dict(sys.modules, {"klyk.launcher": fake, "klyk.ownership": ownership}):
            for pid in (True, -1, 0, 1, 10 ** 200):
                self.assertIsNone(cli._installation_process(pid))
            fake.process_identity.assert_not_called()
            with self.assertRaises(SystemExit) as exit_status:
                cli._restart()
            self.assertEqual(exit_status.exception.code, 1)
            fake.terminate_pid.assert_not_called()

    def test_update_restart_passes_expected_identity_for_only_this_installation(self):
        """Candidate enumeration never broadens the verified termination target."""
        first = self._identity(11)
        third = self._identity(33)
        fake = types.ModuleType("klyk.launcher")
        fake.terminate_pid = mock.Mock(side_effect=(True, False))
        with mock.patch.dict(sys.modules, {"klyk.launcher": fake}), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(stdout="11\n22\n33\n11\n", returncode=0)) as enumerate_processes, \
             mock.patch.object(cli, "_installation_process", side_effect=lambda pid: {11: first, 33: third}.get(pid)):
            cli._restart_after_update()
        self.assertEqual(fake.terminate_pid.call_args_list, [mock.call(11, expected_identity=first), mock.call(33, expected_identity=third)])
        self.assertEqual(enumerate_processes.call_args.args[0][0], "/usr/bin/pgrep")
        self.assertIn("could not be stopped safely", self.output.getvalue())

    def test_opencode_status_requires_exact_server_name_and_connected_state(self):
        """Another server with a similar name cannot make setup report success."""
        outputs = (("✓ klyk  connected", True), ("├ klyk connected", True),
                   ("\x1b[32m✓ klyk  connected\x1b[0m", True),
                   ("✓ klyk-other  connected", False), ("✓ other-klyk connected", False),
                   ("klyk disconnected", False), ("klyk not connected", False),
                   ("Klyk connected", False), ("klyk\nother connected", False))
        for output, connected in outputs:
            with self.subTest(output=output), \
                 mock.patch.object(cli, "_opencode_executable", return_value="/fake/opencode"), \
                 mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(stdout=output, stderr="", returncode=0)) as run:
                self.assertEqual(cli._verify_opencode_connection(), connected)
                self.assertEqual(run.call_args.args[0], ["/fake/opencode", "mcp", "list"])
        with mock.patch.object(cli, "_opencode_executable", return_value="/fake/opencode"), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(stdout="klyk connected", stderr="", returncode=1)):
            self.assertFalse(cli._verify_opencode_connection())

    def test_unknown_opencode_version_has_plain_error_without_recursive_snippet(self):
        """Handling a version/config error does not immediately raise it again."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            client.path.write_text("{}")
            with mock.patch.object(clients, "opencode_major_version", return_value=None), \
                 mock.patch.object(clients, "opencode_executable", return_value="/fake/opencode"):
                self.assertFalse(cli._configure_client(client))
            self.assertEqual(client.path.read_text(), "{}")

    def test_uvx_setup_refuses_persisting_disposable_environment(self):
        """Disposable installs may print the catalog but cannot register fragile commands."""
        with mock.patch.object(updates, "install_method", return_value="uvx"), \
             mock.patch.object(cli, "_require_macos"), \
             mock.patch.object(cli, "_configure_client") as configure, \
             mock.patch.object(cli, "_open_settings") as settings:
            with self.assertRaises(SystemExit):
                cli._install(["cursor"])
            with self.assertRaises(SystemExit):
                cli._doctor_fix()
            cli._install(["--list"])
            configure.assert_not_called()
            settings.assert_not_called()
        self.assertIn("uv tool install klyk", self.errors.getvalue())

    def test_upgrade_failure_and_failed_fresh_import_never_restart(self):
        """The old process stays usable when upgrade verification cannot prove a new package."""
        sequences = ((mock.Mock(returncode=1, stderr="fixture", stdout=""),),
                     (mock.Mock(returncode=0, stderr="", stdout=""), mock.Mock(returncode=1, stdout="", stderr="fixture")))
        for sequence in sequences:
            with self.subTest(sequence=len(sequence)), \
                 mock.patch.object(updates, "install_method", return_value="pip"), \
                 mock.patch.object(cli.subprocess, "run", side_effect=sequence) as run, \
                 mock.patch.object(updates, "check") as check, \
                 mock.patch.object(cli, "_restart_after_update") as restart:
                with self.assertRaises(SystemExit) as exit_status:
                    cli._update([])
                self.assertEqual(exit_status.exception.code, 1)
                restart.assert_not_called()
                check.assert_not_called()
                self.assertEqual(run.call_args_list[0].kwargs["env"]["PYTHONPATH"], "")
                if len(sequence) == 2:
                    self.assertEqual(run.call_args_list[1].args[0][1:3], ["-P", "-c"])

    def test_uvx_upgrade_uses_new_cache_version_without_restarting_old_interpreter(self):
        """uv handles cache replacement; persistent servers never point into the new disposable cache."""
        with mock.patch.object(updates, "install_method", return_value="uvx"), \
             mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="klyk 99.0.0\n", stderr="")) as run, \
             mock.patch.object(updates, "check", return_value={"latest": "99.0.0"}), \
             mock.patch.object(cli, "_restart_after_update") as restart:
            cli._update([])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], updates.upgrade_command("uvx"))
            restart.assert_not_called()
        self.assertIn("Refreshed the disposable uvx environment", self.output.getvalue())

    def test_unchanged_old_install_behind_metadata_is_reported_as_failure(self):
        """A tool upgrade of another installation cannot be mistaken for this one's success."""
        import klyk
        with mock.patch.object(updates, "install_method", return_value="pip"), \
             mock.patch.object(cli.subprocess, "run", side_effect=(mock.Mock(returncode=0, stdout="", stderr=""), mock.Mock(returncode=0, stdout=klyk.__version__, stderr=""))), \
             mock.patch.object(updates, "check", return_value={"latest": "99.0.0"}), \
             mock.patch.object(cli, "_restart_after_update") as restart:
            with self.assertRaises(SystemExit):
                cli._update([])
            restart.assert_not_called()

    def test_full_uninstall_retains_runtime_when_client_cleanup_fails(self):
        """An unreadable config cannot be left pointing at binaries that were just removed."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / ".klyk"
            state.mkdir()
            client = replace(clients.get("cursor"), path=home / "mcp.json")
            client.path.write_text("{")
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(clients, "CLIENTS", {"cursor": client}):
                with self.assertRaises(SystemExit):
                    cli._uninstall([])
            self.assertTrue(state.is_dir())
            self.assertEqual(client.path.read_text(), "{")

    def test_full_uninstall_removes_known_logs_without_following_linked_state(self):
        """Only requested state links and known rotations are removed from the temp home."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            victim = home / "victim"
            victim.mkdir()
            marker = victim / "preserve"
            marker.write_text("keep")
            (home / ".klyk").symlink_to(victim, target_is_directory=True)
            for name in ("klyk.log", *(f"klyk.log.{index}" for index in range(1, 6)), "klyk.log.unknown"):
                (home / name).write_text("fixture")
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(clients, "CLIENTS", {}):
                cli._uninstall([])
            self.assertEqual(marker.read_text(), "keep")
            self.assertFalse((home / ".klyk").is_symlink())
            self.assertTrue((home / "klyk.log.unknown").exists())
            self.assertFalse((home / "klyk.log").exists())
            self.assertFalse((home / "klyk.log.5").exists())

    def test_full_uninstall_reports_failed_deletion_and_does_not_claim_success(self):
        """A denied state deletion leaves an actionable failure instead of a false clean result."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / ".klyk"
            state.mkdir()
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(clients, "CLIENTS", {}), \
                 mock.patch.object(cli.shutil, "rmtree", side_effect=PermissionError("fixture")):
                with self.assertRaises(SystemExit) as exit_status:
                    cli._uninstall([])
                self.assertEqual(exit_status.exception.code, 1)
            self.assertTrue(state.is_dir())
            self.assertNotIn("are gone", self.output.getvalue())

    def test_full_uninstall_finds_native_entry_after_v1_downgrade(self):
        """An inactive V2 entry cannot remain behind and reappear after reinstalling V2."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            client = replace(clients.get("opencode"), path=home / "opencode.json")
            client.path.write_text('{"mcp":{"servers":{"klyk":{"type":"local","command":["old"],"disabled":true},"other":{"type":"local","command":["keep"]}}}}')
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(clients, "CLIENTS", {"opencode": client}), \
                 mock.patch.object(clients, "opencode_major_version", return_value=1):
                self.assertIsNone(clients.current_entry(client))
                cli._uninstall([])
            servers = json.loads(client.path.read_text())["mcp"]["servers"]
            self.assertNotIn("klyk", servers)
            self.assertEqual(servers["other"]["command"], ["keep"])

    def test_doctor_uses_native_preflight_and_flags_missing_import_environment(self):
        """Diagnostics check permission without screenshots and detect incomplete launch hardening."""
        capture = types.ModuleType("klyk.capture")
        capture.check_screen_recording = mock.Mock()
        with mock.patch.dict(sys.modules, {"klyk.capture": capture}), mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(doctor.check_screen_recording_permission().status, "ok")
            capture.check_screen_recording.assert_called_once_with()
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("cursor"), path=Path(directory) / "mcp.json")
            entry = {key: value for key, value in client.entry.items() if key != "env"}
            client.path.write_text(json.dumps({"mcpServers": {"klyk": entry}}))
            with mock.patch.object(clients, "CLIENTS", {"cursor": client}):
                check = doctor.check_mcp_client_entries()
                self.assertEqual(check.status, "fail")
                self.assertIn("Python imports", check.detail)


if __name__ == "__main__":
    unittest.main()
