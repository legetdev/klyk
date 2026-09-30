"""Exercise setup safety with temporary files and inert process/network fixtures."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import tomllib
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from klyk import cli, clients, doctor, jsonc, updates


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


class TomlMigrationTests(unittest.TestCase):
    """Verify generated-launch repairs using only disposable Codex/Grok files."""

    def _fixture(self, directory, key="codex", args=None, extras=""):
        """Create a real temporary TOML document without launching any client."""
        client = replace(clients.get(key), path=Path(directory) / f"{key}.toml")
        source = (
            '# Preserve the owner settings.\n'
            '[features]\nactive = true\n\n'
            '[mcp_servers.other]\ncommand = "keep"\n'
            'args = ["-m", "klyk.mcp_server"] # another server is unrelated\n\n'
            '[mcp_servers.klyk] # generated launch\n'
            f'command = {json.dumps(clients.LAUNCH_ENTRY["command"])}\n'
            f'args = {json.dumps(args or ["-m", "klyk.mcp_server"])} # preserve this comment\n'
            + extras
        )
        client.path.write_bytes(source.encode())
        return client, source

    def _assert_migrated(self, client, original):
        """Compare every parsed setting and verify an identical repeat is inert."""
        expected = tomllib.loads(original)
        entry = expected["mcp_servers"]["klyk"]
        entry["args"] = ["-P", "-m", "klyk.mcp_server"]
        entry["env"] = {"PYTHONPATH": "", **entry.get("env", {})}
        self.assertEqual(clients.write_entry(client), "updated")
        result = client.path.read_bytes()
        self.assertEqual(tomllib.loads(result.decode()), expected)
        with mock.patch.object(jsonc, "atomic_write") as write:
            self.assertEqual(clients.write_entry(client), "unchanged")
            write.assert_not_called()
        self.assertEqual(client.path.read_bytes(), result)
        return result.decode()

    def test_generated_codex_and_grok_upgrade_preserves_symlink_mode_and_crlf(self):
        """Both legacy args and partially hardened entries gain a safe import environment."""
        for key in ("codex", "grok"):
            for args in (["-m", "klyk.mcp_server"], ["-P", "-m", "klyk.mcp_server"]):
                with self.subTest(key=key, args=args), tempfile.TemporaryDirectory() as directory:
                    client, source = self._fixture(directory, key, args)
                    source = source.replace("\n", "\r\n")
                    client.path.write_bytes(source.encode())
                    client.path.chmod(0o640)
                    target = client.path
                    link = target.with_name("linked.toml")
                    link.symlink_to(target)
                    client = replace(client, path=link)
                    result = self._assert_migrated(client, source)
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                    self.assertEqual(result.count("\n"), result.count("\r\n"))
                    self.assertIn('# another server is unrelated\r\n', result)
                    self.assertIn('# preserve this comment\r\n', result)

    def test_multiline_fake_headers_comments_and_custom_restrictions_survive(self):
        """Native whole-file comparison rejects header lookalikes inside a string."""
        with tempfile.TemporaryDirectory() as directory:
            client, _ = self._fixture(directory)
            fake = (
                'notes = \'\'\'\n[mcp_servers.klyk]\n'
                'args = ["-m", "klyk.mcp_server"]\n'
                '[mcp_servers.klyk.env]\nKEEP = "fake"\n\'\'\'\n\n'
            )
            source = fake + (
                '[mcp_servers.klyk] # real header\n'
                f'command = {json.dumps(clients.LAUNCH_ENTRY["command"])}\n'
                'args = [\n # module comment\n "-m", # flag comment\n'
                ' "klyk.mcp_server", # server comment\n] # array comment\n'
                'disabled = true\nstartup_timeout_sec = 90\ntool_timeout_sec = 120\n'
                'enabled_tools = ["inspect", "click"]\n'
                '[mcp_servers.klyk.env] # real env\n'
                'KLYK_UPDATE_CHECK = "0" # explicit opt-out\nCUSTOM = "保持"\n'
                '[mcp_servers.other]\ncommand = "keep"\n'
            )
            client.path.write_bytes(source.encode())
            result = self._assert_migrated(client, source)
            expected_text = source.replace('args = [\n', 'args = ["-P", \n', 1)
            expected_text = expected_text.replace(
                '[mcp_servers.klyk.env] # real env\n',
                '[mcp_servers.klyk.env] # real env\n"PYTHONPATH" = ""\n', 1)
            self.assertEqual(result, expected_text)

    def test_inline_and_dotted_environment_settings_are_preserved(self):
        """Known generated commands can retain inline, dotted and empty env tables."""
        environments = (
            'env = { KLYK_UPDATE_CHECK = "0", CUSTOM = "keep" } # inline note\n',
            'env.KLYK_UPDATE_CHECK = "0" # dotted note\nenv.CUSTOM = "keep"\n',
            'env = {} # empty inline note\n',
        )
        for environment in environments:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                client, source = self._fixture(directory, "grok", extras=environment + 'disabled = true\n')
                result = self._assert_migrated(client, source)
                for line in environment.splitlines():
                    if "#" in line:
                        self.assertIn(line.split("#", 1)[1], result)
                self.assertTrue(clients.current_entry(client)["disabled"])

    def test_explicit_owner_pythonpath_and_update_optout_remain_unchanged(self):
        """Deliberate import overrides and privacy preferences survive a launch repair."""
        for override in ("", "/owner/trusted"):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as directory:
                env = ('[mcp_servers.klyk.env]\n'
                       f'PYTHONPATH = {json.dumps(override)} # owner override\n'
                       'KLYK_UPDATE_CHECK = "0"\n')
                client, source = self._fixture(directory, extras=env)
                result = self._assert_migrated(client, source)
                self.assertTrue(result.endswith(env))
                self.assertEqual(clients.current_entry(client)["env"]["PYTHONPATH"], override)

    def test_differing_commands_or_custom_arguments_are_not_replaced(self):
        """Only this exact interpreter and the two owned standard argument lists qualify."""
        launches = (("python3", ["-m", "klyk.mcp_server"]),
                    ("/different/venv/bin/python", ["-m", "klyk.mcp_server"]),
                    (clients.LAUNCH_ENTRY["command"], ["-I", "-m", "klyk.mcp_server"]),
                    (clients.LAUNCH_ENTRY["command"], ["-m", "klyk.mcp_server", "custom"]))
        for command, args in launches:
            with self.subTest(command=command, args=args), tempfile.TemporaryDirectory() as directory:
                client, source = self._fixture(directory, args=args)
                source = source.replace(json.dumps(clients.LAUNCH_ENTRY["command"]), json.dumps(command))
                client.path.write_bytes(source.encode())
                with mock.patch.object(jsonc, "atomic_write") as write:
                    with self.assertRaises(clients.ManualEditRequired):
                        clients.write_entry(client)
                    write.assert_not_called()
                self.assertEqual(client.path.read_bytes(), source.encode())

    def test_malformed_or_unsupported_tables_are_not_clobbered(self):
        """Malformed syntax and frozen inline tables require repair without a write."""
        command = json.dumps(clients.LAUNCH_ENTRY["command"])
        sources = (
            '[mcp_servers.klyk]\ncommand = "first"\ncommand = "duplicate"\n',
            f'[mcp_servers.klyk]\ncommand = {command}\nargs = ["-m", "klyk.mcp_server"]\nenv = 42\n',
            f'[mcp_servers]\nklyk = {{command = {command}, args = ["-m", "klyk.mcp_server"]}}\n',
            f'[mcp_servers.klyk]\ncommand = {command}\nargs = ["-m", "klyk.mcp_server"]\n[mcp_servers."klyk"."env"]\nCUSTOM = "keep"\n',
            'mcp_servers = {other = {command = "keep"}}\n',
        )
        for source in sources:
            with self.subTest(source=source[:40]), tempfile.TemporaryDirectory() as directory:
                client = replace(clients.get("codex"), path=Path(directory) / "config.toml")
                client.path.write_bytes(source.encode())
                with mock.patch.object(jsonc, "atomic_write") as write:
                    with self.assertRaises((tomllib.TOMLDecodeError, jsonc.ConfigFormatError, clients.ManualEditRequired)):
                        clients.write_entry(client)
                    write.assert_not_called()
                self.assertEqual(client.path.read_bytes(), source.encode())

    def test_migration_rejects_concurrent_editor_change_before_replace(self):
        """A real file changed during tempfile flush remains the editor's version."""
        with tempfile.TemporaryDirectory() as directory:
            client, _ = self._fixture(directory)
            external = '# changed by the owner\n[features]\nactive = false\n'
            with mock.patch.object(jsonc.os, "fsync", side_effect=lambda fd: client.path.write_text(external)):
                with self.assertRaises(jsonc.ConfigFormatError):
                    clients.write_entry(client)
            self.assertEqual(client.path.read_text(), external)
            self.assertEqual(list(Path(directory).iterdir()), [client.path])

    def test_failed_migration_replace_keeps_original_bytes(self):
        """A disk replacement failure never leaves a partially updated launch."""
        with tempfile.TemporaryDirectory() as directory:
            client, source = self._fixture(directory, "grok")
            with mock.patch.object(jsonc.os, "replace", side_effect=OSError("fixture replacement denied")):
                with self.assertRaises(OSError):
                    clients.write_entry(client)
            self.assertEqual(client.path.read_bytes(), source.encode())
            self.assertEqual(list(Path(directory).iterdir()), [client.path])

    def test_many_string_lookalikes_stop_without_a_write(self):
        """Bound repeated native parses when a string contains many fake argument keys."""
        with tempfile.TemporaryDirectory() as directory:
            client, source = self._fixture(directory)
            source = 'notes = \'\'\'\n' + 'args = ["-m", "klyk.mcp_server"]\n' * 64 + "'''\n" + source
            client.path.write_bytes(source.encode())
            with mock.patch.object(jsonc, "atomic_write") as write:
                with self.assertRaises(clients.ManualEditRequired):
                    clients.write_entry(client)
                write.assert_not_called()
            self.assertEqual(client.path.read_bytes(), source.encode())

    def test_existing_doctor_fix_repairs_generated_toml_in_a_fresh_invocation(self):
        """The established repair command updates both adapters with native checks mocked."""
        with tempfile.TemporaryDirectory() as directory:
            codex, _ = self._fixture(directory, "codex")
            grok, _ = self._fixture(directory, "grok")
            with mock.patch.object(Path, "home", return_value=Path(directory)), \
                 mock.patch.object(clients, "CLIENTS", {"codex": codex, "grok": grok}), \
                 mock.patch.object(cli, "_persistent_install_required", return_value=True), \
                 mock.patch.object(doctor, "run_all_checks", return_value=[doctor.CheckResult("fixture", "ok", "ready")]), \
                 mock.patch.object(cli, "_open_settings") as settings, mock.patch("builtins.print"):
                cli._doctor_fix()
                self.assertEqual(doctor.check_mcp_client_entries().status, "ok")
                settings.assert_not_called()
            for client in (codex, grok):
                self.assertEqual(clients.current_entry(client), client.entry)


if __name__ == "__main__":
    unittest.main()
