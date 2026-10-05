"""Verify client grouping and safe configuration migration with inert fixtures."""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
import tomllib
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from klyk import clients, doctor, jsonc


class ProcessClientTests(unittest.TestCase):
    """Use fake kernel metadata; never query or activate another running app."""

    CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"

    def _identity(self, executable, argv=None, **changes):
        """Build a same-user, stable synthetic direct-parent record."""
        return {"pid": 1234, "uid": 501, "started": (123, 456),
                "executable": executable, "argv": [executable] if argv is None else argv, **changes}

    def _inferred(self, identity, parents=(1234, 1234), failure=None, resolved=None):
        """Replace every process query and path resolution with deterministic metadata."""
        launcher = types.ModuleType("klyk.launcher")
        launcher.process_identity = mock.Mock(return_value=identity, side_effect=failure)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {"klyk.launcher": launcher}), \
             mock.patch.object(os, "getppid", side_effect=parents), \
             mock.patch.object(os, "getuid", return_value=501), \
             mock.patch.object(pwd, "getpwuid", return_value=types.SimpleNamespace(pw_dir="/verified/home")), \
             mock.patch.object(Path, "resolve", autospec=True,
                               side_effect=lambda path: Path((resolved or {}).get(str(path), str(path)))), \
             mock.patch.object(clients.subprocess, "run", side_effect=AssertionError("no subprocesses")):
            result = clients.current_process_client()
        launcher.process_identity.assert_called_once_with(1234, include_command=True)
        return result

    def test_explicit_registry_tags_win_without_any_process_query(self):
        """Normal configured launches take the cheap explicit-identity path."""
        for key in clients.CLIENTS:
            with self.subTest(key=key), mock.patch.dict(os.environ, {"KLYK_CLIENT": key}, clear=True), \
                 mock.patch.object(os, "getppid", side_effect=AssertionError("explicit tags need no inference")):
                self.assertEqual(clients.current_process_client(), key)

    def test_invalid_explicit_tags_fail_closed_without_inference(self):
        """A broken tag must not gain access through an enabled Other preference."""
        for tag in ("", "other", "CODEX", " codex", "codex\n", "agy", "unknown", "codex/claude"):
            with self.subTest(tag=tag), mock.patch.dict(os.environ, {"KLYK_CLIENT": tag}, clear=True), \
                 mock.patch.object(os, "getppid", side_effect=AssertionError("invalid tags cannot infer")):
                self.assertIsNone(clients.current_process_client())

    def test_verified_native_parent_locations_are_recognized_exactly(self):
        """Only known native launchers and their exact argv[0] identify legacy launches."""
        cases = (("codex", self.CODEX), ("codex", "/opt/homebrew/bin/codex"),
                 ("claude", "/verified/home/.local/bin/claude"),
                 ("claude", "/usr/local/bin/claude"),
                 ("opencode", "/verified/home/.opencode/bin/opencode"))
        for key, executable in cases:
            for argv0 in (executable, key):
                with self.subTest(key=key, argv0=argv0):
                    self.assertEqual(self._inferred(self._identity(executable, [argv0, "--fixture"])), key)

    def test_unrelated_names_shared_runtimes_and_spoofed_argv_stay_other(self):
        """Substrings, arguments and executable basenames cannot select an enabled client."""
        cases = (("/tmp/codex", ["codex"]), ("/tmp/claude", ["claude"]),
                 ("/tmp" + self.CODEX, [self.CODEX]),
                 (self.CODEX + "-other", [self.CODEX]),
                 (self.CODEX, ["/tmp/codex"]),
                 ("/opt/homebrew/bin/node", ["node", "/verified/home/.local/bin/claude"]),
                 ("/bin/zsh", ["zsh", "-c", self.CODEX]),
                 ("/usr/bin/python3", ["python3", "-m", "codex"]),
                 ("codex", ["codex"]))
        for executable, argv in cases:
            with self.subTest(executable=executable, argv=argv):
                self.assertEqual(self._inferred(self._identity(executable, argv)), "other")

    def test_missing_malformed_other_user_and_reparented_records_stay_other(self):
        """Unverified or changed parent identity cannot be assigned to a named environment."""
        records = (None, {}, self._identity(self.CODEX, uid=502),
                   self._identity(self.CODEX, pid=4321),
                   self._identity(self.CODEX, argv=[]),
                   self._identity(self.CODEX, argv="codex"),
                   {**self._identity(self.CODEX), "executable": None})
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(self._inferred(record), "other")
        self.assertEqual(self._inferred(self._identity(self.CODEX), parents=(1234, 4321)), "other")
        self.assertEqual(self._inferred(None, failure=OSError("metadata denied")), "other")

    def test_native_symlink_resolution_uses_exact_target_without_trusting_home_env(self):
        """A known native launcher may resolve to its versioned binary; HOME is irrelevant."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".local/share/claude/versions/9.9.9"
            target.parent.mkdir(parents=True)
            target.write_text("inert")
            launcher_path = home / ".local/bin/claude"
            launcher_path.parent.mkdir(parents=True)
            launcher_path.symlink_to(target)
            launcher = types.ModuleType("klyk.launcher")
            launcher.process_identity = mock.Mock(return_value=self._identity(str(target.resolve()), [str(launcher_path)]))
            with mock.patch.dict(os.environ, {"HOME": "/untrusted/home"}, clear=True), \
                 mock.patch.dict(sys.modules, {"klyk.launcher": launcher}), \
                 mock.patch.object(os, "getppid", return_value=1234), \
                 mock.patch.object(os, "getuid", return_value=501), \
                 mock.patch.object(pwd, "getpwuid", return_value=types.SimpleNamespace(pw_dir=str(home))):
                self.assertEqual(clients.current_process_client(), "claude")

    def test_custom_launcher_links_to_shared_runtimes_or_arbitrary_versions_stay_other(self):
        """Owner-customized shell/interpreter links are not verified native client binaries."""
        for target in ("/bin/zsh", "/opt/homebrew/bin/node", "/tmp/9.9.9", "/tmp/claude-other"):
            with self.subTest(target=target):
                self.assertEqual(self._inferred(self._identity(target), resolved={
                    "/verified/home/.local/bin/claude": target,
                    "/opt/homebrew/bin/opencode": target,
                    "/usr/local/bin/codex": target,
                }), "other")


class ClientTagMigrationTests(unittest.TestCase):
    """Exercise all registered config shapes without editing owner configuration."""

    def test_registry_and_copied_client_environments_have_distinct_canonical_tags(self):
        """Nested environment dictionaries cannot leak one client's tag into another."""
        for key, client in clients.CLIENTS.items():
            env_key = "environment" if client.fmt == "opencode" else "env"
            self.assertEqual(client.entry[env_key]["KLYK_CLIENT"], key)
            copied = replace(client)
            self.assertIsNot(copied.entry[env_key], client.entry[env_key])
            copied.entry[env_key]["CUSTOM"] = "fixture"
            self.assertNotIn("CUSTOM", client.entry[env_key])
        self.assertEqual(clients.LAUNCH_ENTRY["env"], {"PYTHONPATH": ""})

    def test_all_json_clients_refresh_only_owned_fields_and_correct_reserved_tag(self):
        """Existing privacy preferences, tool restrictions and other servers survive tagging."""
        for key, registered in clients.CLIENTS.items():
            if registered.fmt != "json":
                continue
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                client = replace(registered, path=Path(directory) / "mcp.json")
                old = {"command": clients.LAUNCH_ENTRY["command"], "args": ["-m", "klyk.mcp_server"],
                       "env": {"PYTHONPATH": "/owner/trusted", "KLYK_UPDATE_CHECK": "0",
                               "CUSTOM": "保持", "KLYK_CLIENT": "wrong"},
                       "disabled": True, "disabledTools": ["click"], "timeout": 5000}
                original = {"owner_setting": {"keep": True}, "mcpServers": {"klyk": old, "other": {"keep": True}}}
                client.path.write_text(json.dumps(original))
                self.assertEqual(clients.write_entry(client), "updated")
                expected = {**original, "mcpServers": {**original["mcpServers"], "klyk": {
                    **old, "args": clients.LAUNCH_ENTRY["args"],
                    **({"type": "stdio"} if key == "claude" else {}),
                    "env": {**old["env"], "KLYK_CLIENT": key}}}}
                self.assertEqual(json.loads(client.path.read_text()), expected)
                self.assertEqual(clients.write_entry(client), "unchanged")

    def test_opencode_v1_and_v2_preserve_comment_and_custom_environment(self):
        """Both native schemas receive environment tags without losing server restrictions."""
        for major in (1, 2):
            with self.subTest(major=major), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.object(clients, "opencode_major_version", return_value=major):
                client = replace(clients.get("opencode"), path=Path(directory) / "opencode.jsonc")
                old = {"type": "local", "command": [clients.LAUNCH_ENTRY["command"], "-P", "-m", "klyk.mcp_server"],
                       "environment": {"PYTHONPATH": "/owner/trusted", "KLYK_UPDATE_CHECK": "0", "KLYK_CLIENT": "wrong"},
                       "disabled": True, "cwd": "/fixture", "timeout": {"startup": 45000}}
                servers = {"klyk": old, "other": {"type": "local", "command": ["keep"]}}
                original = {"theme": "保持", "mcp": {"servers": servers} if major == 2 else servers}
                text = json.dumps(original).replace('{', '{\n// Keep this comment\n', 1)
                client.path.write_text(text)
                self.assertEqual(clients.write_entry(client), "updated")
                expected_entry = {**old, "environment": {**old["environment"], "KLYK_CLIENT": "opencode"}}
                self.assertEqual(clients.current_entry(client), expected_entry)
                self.assertIn("// Keep this comment", client.path.read_text())
                expected_servers = {**servers, "klyk": expected_entry}
                expected = {**original, "mcp": {"servers": expected_servers} if major == 2 else expected_servers}
                self.assertEqual(jsonc.parse_object(client.path.read_text(), client.path).to_python(), expected)
                self.assertEqual(clients.write_entry(client), "unchanged")

    def test_already_safe_toml_gains_tag_with_full_semantics_and_comments_preserved(self):
        """Current v0.6 launches must actually receive the new tag, not report a false update."""
        for key in ("codex", "grok"):
            for tag_line in ("", 'KLYK_CLIENT = "wrong" # retain tag comment\n',
                             "'KLYK_CLIENT' = 'wrong' # retain tag comment\n"):
                with self.subTest(key=key, tag_line=tag_line), tempfile.TemporaryDirectory() as directory:
                    client = replace(clients.get(key), path=Path(directory) / "config.toml")
                    fake = 'notes = \'\'\'\n[mcp_servers.klyk.env]\nKLYK_CLIENT = "wrong"\n\'\'\'\n'
                    source = fake + ('[mcp_servers.klyk]\n'
                        f'command = {json.dumps(clients.LAUNCH_ENTRY["command"])}\n'
                        'args = ["-P", "-m", "klyk.mcp_server"] # unchanged args\n'
                        'disabled = true\nenabled_tools = ["inspect"]\nstartup_timeout_sec = 90\n'
                        '[mcp_servers.klyk.env] # retain env comment\n'
                        'PYTHONPATH = "" # retain import preference\n'
                        'KLYK_UPDATE_CHECK = "0"\nCUSTOM = "保持"\n' + tag_line)
                    client.path.write_text(source)
                    expected = tomllib.loads(source)
                    expected["mcp_servers"]["klyk"]["env"]["KLYK_CLIENT"] = key
                    self.assertEqual(clients.write_entry(client), "updated")
                    result = client.path.read_text()
                    self.assertEqual(tomllib.loads(result), expected)
                    self.assertNotEqual(result, source)
                    self.assertTrue(result.startswith(fake))
                    self.assertIn('# unchanged args\n', result)
                    self.assertIn('# retain import preference\n', result)
                    self.assertIn('# retain env comment\n', result)
                    if tag_line:
                        self.assertIn('# retain tag comment\n', result)
                    self.assertEqual(clients.write_entry(client), "unchanged")

    def test_inline_and_dotted_wrong_toml_tags_are_corrected_without_losing_owner_env(self):
        """Existing reserved tags can be safely corrected in supported standard layouts."""
        environments = ('env = {KLYK_CLIENT = "wrong", PYTHONPATH = "/owner/trusted", CUSTOM = "keep"} # inline\n',
                        'env.KLYK_CLIENT = "wrong" # dotted\nenv.PYTHONPATH = "/owner/trusted"\nenv.CUSTOM = "keep"\n')
        for environment in environments:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                client = replace(clients.get("codex"), path=Path(directory) / "config.toml")
                source = ('[mcp_servers.klyk]\n'
                    f'command = {json.dumps(clients.LAUNCH_ENTRY["command"])}\n'
                    'args = ["-m", "klyk.mcp_server"]\n' + environment)
                client.path.write_text(source)
                self.assertEqual(clients.write_entry(client), "updated")
                expected_text = source.replace('args = [', 'args = ["-P", ', 1).replace('KLYK_CLIENT = "wrong"', 'KLYK_CLIENT = "codex"', 1)
                self.assertEqual(client.path.read_text(), expected_text)

    def test_toml_refuses_unexpected_semantic_changes_and_concurrent_editor_writes(self):
        """No incomplete migration or stale snapshot may overwrite an owner's settings."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("grok"), path=Path(directory) / "config.toml")
            source = ('[mcp_servers.klyk]\n'
                f'command = {json.dumps(clients.LAUNCH_ENTRY["command"])}\n'
                'args = ["-P", "-m", "klyk.mcp_server"]\n'
                '[mcp_servers.klyk.env]\nPYTHONPATH = ""\n')
            client.path.write_text(source)
            unexpected = replace(client, entry={**client.entry, "extra_owned_field": "uneditable"})
            with mock.patch.object(jsonc, "atomic_write") as write:
                with self.assertRaises(clients.ManualEditRequired):
                    clients.write_entry(unexpected)
                write.assert_not_called()
            self.assertEqual(client.path.read_text(), source)
            owner_text = '# Owner saved newer settings\nowner = true\n'
            with mock.patch.object(jsonc.os, "fsync", side_effect=lambda fd: client.path.write_text(owner_text)):
                with self.assertRaises(jsonc.ConfigFormatError):
                    clients.write_entry(client)
            self.assertEqual(client.path.read_text(), owner_text)


class ClientTagDoctorTests(unittest.TestCase):
    """Check client identity diagnostics without policy, native helpers or config writes."""

    def _check(self, client, major=1):
        """Confine discovery to one inert fixture and reject every initialization path."""
        with mock.patch.object(clients, "CLIENTS", {client.key: client}), \
             mock.patch.object(clients, "opencode_major_version", return_value=major), \
             mock.patch.object(clients.subprocess, "run", side_effect=AssertionError("no installed CLI calls")), \
             mock.patch.object(clients, "current_process_client", side_effect=AssertionError("no runtime identity query")), \
             mock.patch.object(jsonc, "atomic_write", side_effect=AssertionError("read-only diagnostics")), \
             mock.patch.object(doctor, "private_directory", side_effect=AssertionError("no state initialization")), \
             mock.patch.object(doctor, "open_private", side_effect=AssertionError("no log initialization")), \
             mock.patch.object(doctor.os, "kill", side_effect=AssertionError("no process signal")), \
             mock.patch.object(doctor.shutil, "which", side_effect=lambda command: command):
            return doctor.check_mcp_client_entries()

    def _fixture(self, directory, registered, tag, major=1, legacy=False):
        """Create only a disposable supported config with custom restrictions and comments."""
        extension = ".toml" if registered.fmt == "toml" else ".jsonc" if registered.fmt == "opencode" else ".json"
        client = replace(registered, path=Path(directory) / (registered.key + extension))
        env_key = "environment" if client.fmt == "opencode" else "env"
        environment = {"PYTHONPATH": "/owner/trusted", "KLYK_UPDATE_CHECK": "0", "CUSTOM": "保持"}
        if tag is not None:
            environment["KLYK_CLIENT"] = tag
        entry = {**client.entry, env_key: environment, "disabled": True}
        if legacy:
            if client.fmt == "opencode":
                entry["command"] = [client.entry["command"][0], "-m", "klyk.mcp_server"]
            else:
                entry["args"] = ["-m", "klyk.mcp_server"]
        if client.fmt == "toml":
            text = (f'# Preserve owner comments\n[mcp_servers.klyk]\n'
                    f'command = {json.dumps(entry["command"])}\n'
                    f'args = {json.dumps(entry["args"])}\n'
                    'disabled = true\nenabled_tools = ["inspect"]\n'
                    '[mcp_servers.klyk.env]\n' + ''.join(
                        f'{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}\n'
                        for key, value in environment.items()))
        else:
            entry["disabledTools"] = ["click"]
            servers = {"klyk": entry, "other": {"keep": True}}
            data = {"owner_setting": "保持"}
            if client.fmt == "opencode":
                data["mcp"] = {"servers": servers} if major == 2 else servers
                text = json.dumps(data).replace('{', '{\n// Preserve owner comments\n', 1)
            else:
                data["mcpServers"] = servers
                text = json.dumps(data, indent=2)
        client.path.write_text(text)
        return client, client.path.read_bytes()

    def test_all_supported_current_configs_flag_missing_and_wrong_tags_without_writes(self):
        """An otherwise safe launcher must name its own exact canonical environment."""
        for registered in clients.CLIENTS.values():
            majors = (1, 2) if registered.fmt == "opencode" else (1,)
            wrong_client = "claude" if registered.key != "claude" else "codex"
            for major in majors:
                for tag in (None, "", "other", wrong_client, "SECRET-FIXTURE-DO-NOT-ECHO", 123):
                    with self.subTest(client=registered.key, major=major, tag=tag), tempfile.TemporaryDirectory() as directory:
                        client, original = self._fixture(directory, registered, tag, major)
                        check = self._check(client, major)
                        self.assertEqual(check.status, "fail")
                        self.assertIn(f"KLYK_CLIENT={client.key}", check.detail)
                        self.assertIn(f"klyk install {client.key}", check.remedy)
                        self.assertNotIn("SECRET-FIXTURE", check.detail + check.remedy)
                        self.assertEqual(client.path.read_bytes(), original)

    def test_legacy_launches_also_report_missing_identity(self):
        """Older standard arguments do not hide the independently missing identity tag."""
        for key in ("cursor", "codex", "grok", "opencode"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                client, original = self._fixture(directory, clients.get(key), None, legacy=True)
                check = self._check(client)
                self.assertEqual(check.status, "fail")
                self.assertIn(f"KLYK_CLIENT={key}", check.detail)
                self.assertEqual(client.path.read_bytes(), original)

    def test_correct_tags_pass_with_deliberate_owner_settings_untouched(self):
        """Tag validation does not demand default privacy/import values or enabled tools."""
        for registered in clients.CLIENTS.values():
            majors = (1, 2) if registered.fmt == "opencode" else (1,)
            for major in majors:
                with self.subTest(client=registered.key, major=major), tempfile.TemporaryDirectory() as directory:
                    client, original = self._fixture(directory, registered, registered.key, major)
                    check = self._check(client, major)
                    self.assertEqual(check.status, "ok")
                    self.assertIn("configuration only", check.detail)
                    self.assertEqual(client.path.read_bytes(), original)

    def test_read_only_config_check_does_not_import_native_or_policy_runtime(self):
        """This metadata check must remain independent of ownership, controls and capture."""
        forbidden = {"AppKit", "Quartz", "Foundation", "klyk.connection_policy", "klyk.connection_gate",
                     "klyk.controls", "klyk.computer", "klyk.mcp_server", "klyk.capture", "klyk.ownership"}
        real_import = __import__

        def metadata_import(name, *args, **kwargs):
            """Refuse every native/runtime import while allowing stdlib config parsers."""
            fromlist = kwargs.get("fromlist", args[2] if len(args) > 2 else ()) or ()
            if name in forbidden or (name in ("", "klyk") and any(f"klyk.{item}" in forbidden for item in fromlist)):
                raise AssertionError(f"unexpected runtime import: {name}")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            client, original = self._fixture(directory, clients.get("codex"), None)
            with mock.patch("builtins.__import__", side_effect=metadata_import):
                self.assertEqual(self._check(client).status, "fail")
            self.assertEqual(client.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
