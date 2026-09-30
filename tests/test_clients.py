"""Regression tests for client configuration adapters and the JSONC editor."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from klyk import clients, jsonc


class JsoncEditorTests(unittest.TestCase):
    """Exercise surgical JSONC edits without touching a user configuration."""

    def test_reads_comments_trailing_commas_unicode_and_crlf(self) -> None:
        """Parse supported JSONC syntax while retaining its Python values."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            text = (
                '{\r\n'
                '\t"label": "café", // inline comment\r\n'
                '\t"mcp": {\r\n'
                '\t\t"other": {"description": "保持"},\r\n'
                '\t},\r\n'
                '}\r\n'
            )
            present, value = jsonc.top_level_property(text, path, "mcp")

        self.assertTrue(present)
        self.assertEqual(value, {"other": {"description": "保持"}})

    def test_set_preserves_unrelated_bytes_and_crlf(self) -> None:
        """Insert klyk without rewriting comments, Unicode, or other settings."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            text = (
                '{\r\n'
                '\t"label": "café", // keep this comment\r\n'
                '\t"mcp": {\r\n'
                '\t\t"other": {"description": "保持"},\r\n'
                '\t},\r\n'
                '}\r\n'
            )
            entry = {
                "type": "local",
                "command": ["/usr/bin/python", "-m", "klyk.mcp_server"],
            }
            updated = jsonc.set_mcp_entry(text, path, "klyk", entry)
            present, mcp = jsonc.top_level_property(updated, path, "mcp")

        self.assertTrue(present)
        self.assertEqual(mcp["klyk"], entry)
        self.assertIn('"label": "café", // keep this comment', updated)
        self.assertIn('"description": "保持"', updated)
        self.assertEqual(updated.count("\r\n"), updated.count("\n"))

    def test_set_existing_entry_is_byte_idempotent(self) -> None:
        """Setting an already matching entry must not churn the document."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            text = '{\n  "mcp": {\n    "klyk": {"type": "local",},\n  },\n}\n'
            entry = {"type": "local"}
            once = jsonc.set_mcp_entry(text, path, "klyk", entry)
            twice = jsonc.set_mcp_entry(once, path, "klyk", entry)

        self.assertEqual(twice, once)

    def test_duplicate_owned_entries_are_collapsed(self) -> None:
        """Refresh removes duplicate klyk keys before leaving one entry."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            text = (
                '{\n'
                '  "mcp": {\n'
                '    "klyk": {"type": "local", "old": 1},\n'
                '    "klyk": {"type": "local", "old": 2}\n'
                '  }\n'
                '}\n'
            )
            updated = jsonc.set_mcp_entry(text, path, "klyk", {"type": "local"})
            present, mcp = jsonc.top_level_property(updated, path, "mcp")

        self.assertTrue(present)
        self.assertEqual(mcp, {"klyk": {"type": "local"}})

    def test_malformed_documents_fail_closed_for_all_mutations(self) -> None:
        """Malformed JSONC raises the project error and is never rewritten."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jsonc"
            malformed = '{\n  "mcp": {\n    "klyk": [1,\n  }\n}'
            for operation in (
                lambda: jsonc.parse_object(malformed, path),
                lambda: jsonc.top_level_property(malformed, path, "mcp"),
                lambda: jsonc.set_mcp_entry(malformed, path, "klyk", {}),
                lambda: jsonc.remove_mcp_entry(malformed, path, "klyk"),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(jsonc.ConfigFormatError) as raised:
                        operation()
                    self.assertIn(str(path), str(raised.exception))

    def test_remove_is_surgical_and_idempotent(self) -> None:
        """Remove only klyk and report no change when it is already absent."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            text = '{\n  // preserve\n  "name": "café",\n  "mcp": {\n    "klyk": {},\n    "other": {"ok": true}\n  }\n}\n'
            updated, changed = jsonc.remove_mcp_entry(text, path, "klyk")
            again, changed_again = jsonc.remove_mcp_entry(updated, path, "klyk")

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(again, updated)
        self.assertIn('// preserve', updated)
        self.assertIn('"name": "café"', updated)
        self.assertIn('"other": {"ok": true}', updated)
        self.assertNotIn('"klyk"', updated)


class ClientAdapterTests(unittest.TestCase):
    """Verify client adapters against disposable config paths."""

    def setUp(self) -> None:
        """Keep version detection entirely fake even when OpenCode is installed."""
        self._version_function = clients.opencode_major_version
        version = mock.patch.object(clients, "opencode_major_version", return_value=1)
        version.start()
        self.addCleanup(version.stop)

    def test_antigravity_migration_preserves_legacy_and_custom_settings(self) -> None:
        """Repair old-path setup without touching older clients or unrelated settings."""
        from klyk import doctor
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            client = replace(clients.get("agy"), path=base / "config" / "mcp_config.json")
            legacy = base / "antigravity-cli" / "mcp_config.json"
            legacy.parent.mkdir()
            custom = {"command": "/old/python", "args": ["-m", "klyk.mcp_server"],
                      "env": {"KLYK_UPDATE_CHECK": "0"}, "enabledTools": ["inspect"]}
            legacy.write_text(json.dumps({"mcpServers": {"klyk": custom, "other": {"keep": True}}}))
            original = legacy.read_bytes()
            with mock.patch.object(clients, "CLIENTS", {"antigravity": client}):
                result = doctor.check_mcp_client_entries()
                self.assertEqual(result.status, "fail")
                self.assertIn("legacy", result.detail)
            self.assertTrue(clients.is_present(client))
            self.assertEqual(clients.write_entry(client), "added")
            self.assertEqual(legacy.read_bytes(), original)
            migrated = clients.current_entry(client)
            self.assertEqual(migrated["command"], client.entry["command"])
            self.assertEqual(migrated["env"], {"PYTHONPATH": "", **custom["env"]})
            self.assertEqual(migrated["enabledTools"], custom["enabledTools"])
            self.assertEqual(clients.write_entry(client), "unchanged")
            with mock.patch.object(clients, "CLIENTS", {"antigravity": client}):
                self.assertEqual(doctor.check_mcp_client_entries().status, "ok")
            self.assertTrue(clients.remove_entry(client))
            self.assertEqual(legacy.read_bytes(), original)

    def test_antigravity_current_config_takes_precedence_and_preserves_mode(self) -> None:
        """Current custom fields win over legacy setup and a repeat install is inert."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            client = replace(clients.get("agy"), path=base / "config" / "mcp_config.json")
            client.path.parent.mkdir()
            data = {"setting": "保持", "mcpServers": {"other": {"keep": True},
                    "klyk": {"command": "/old", "args": [], "env": {"CUSTOM": "keep"}, "disabled": True}}}
            client.path.write_text(json.dumps(data))
            client.path.chmod(0o640)
            self.assertEqual(clients.write_entry(client), "updated")
            updated = json.loads(client.path.read_text())
            self.assertEqual(updated["setting"], data["setting"])
            self.assertEqual(updated["mcpServers"]["other"], data["mcpServers"]["other"])
            self.assertTrue(updated["mcpServers"]["klyk"]["disabled"])
            self.assertEqual(updated["mcpServers"]["klyk"]["env"], {"PYTHONPATH": "", "CUSTOM": "keep"})
            self.assertEqual(client.path.stat().st_mode & 0o777, 0o640)
            original = client.path.read_bytes()
            with mock.patch.object(clients, "legacy_antigravity_entry", side_effect=AssertionError("must not read legacy")):
                self.assertEqual(clients.write_entry(client), "unchanged")
            self.assertEqual(client.path.read_bytes(), original)

    def test_antigravity_invalid_config_and_failed_write_preserve_original(self) -> None:
        """Refuse malformed input and leave the destination intact on write failure."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("agy"), path=Path(directory) / "mcp_config.json")
            for text in ('{', '[]', '{"mcpServers": []}', '{"mcpServers": {"klyk": []}}'):
                client.path.write_text(text)
                with self.assertRaises((ValueError, clients.ConfigFormatError)):
                    clients.write_entry(client)
                self.assertEqual(client.path.read_text(), text)
            client.path.write_text('{"keep": true}')
            with mock.patch.object(clients.jsonc.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    clients.write_entry(client)
            self.assertEqual(client.path.read_text(), '{"keep": true}')

    def test_doctor_fix_migrates_only_previously_configured_antigravity(self) -> None:
        """Repair the old-path installation without enrolling an unconfigured client."""
        from klyk import cli, doctor
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            client = replace(clients.get("agy"), path=base / "config" / "mcp_config.json")
            with mock.patch.object(clients, "CLIENTS", {"antigravity": client}), \
                 mock.patch.object(Path, "home", return_value=base), \
                 mock.patch.object(doctor, "run_all_checks", return_value=[doctor.CheckResult("fixture", "ok", "ready")]), \
                 mock.patch("builtins.print"):
                cli._doctor_fix()
                self.assertFalse(client.path.exists())
                legacy = base / "antigravity-cli" / "mcp_config.json"
                legacy.parent.mkdir()
                legacy.write_text(json.dumps({"mcpServers": {"klyk": client.entry}}))
                cli._doctor_fix()
                self.assertEqual(clients.current_entry(client), client.entry)

    def test_all_other_client_config_paths_remain_unchanged(self) -> None:
        """Keep every existing non-agy client registry contract stable."""
        paths = {"claude": ".claude.json", "cursor": ".cursor/mcp.json",
                 "windsurf": ".codeium/windsurf/mcp_config.json", "continue": ".continue/config.json",
                 "cline": "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
                 "codex": ".codex/config.toml", "opencode": ".config/opencode/opencode.json",
                 "gemini": ".gemini/settings.json", "grok": ".grok/config.toml"}
        for key, path in paths.items():
            self.assertEqual(clients.get(key).path, Path.home() / path)
            self.assertIsNone(clients.legacy_antigravity_entry(clients.get(key)))
        self.assertEqual(clients.get("agy").path, Path.home() / ".gemini/config/mcp_config.json")

    def test_opencode_write_preserves_jsonc_and_is_idempotent(self) -> None:
        """Install into OpenCode's selected config without losing user content."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = clients.get("opencode")
            self.assertIsNotNone(original)
            client = replace(original, path=base / "config" / "opencode.json")
            client.path.parent.mkdir(parents=True, exist_ok=True)
            client.path.write_text(
                '{\n  "theme": "café",\n  "mcp": {"other": {"type": "local"}}\n}\n',
                encoding="utf-8",
            )

            self.assertEqual(clients.write_entry(client), "added")
            first = client.path.read_text(encoding="utf-8")
            # A matching entry must remain byte-identical after the first write.
            self.assertEqual(clients.write_entry(client), "unchanged")
            second = client.path.read_text(encoding="utf-8")
            present, mcp = jsonc.top_level_property(first, client.path, "mcp")

        self.assertTrue(present)
        self.assertEqual(mcp["other"], {"type": "local"})
        self.assertEqual(second, first)
        self.assertIn('"theme": "café"', first)

    def test_opencode_precedence_selects_highest_priority_existing_file(self) -> None:
        """Select opencode.jsonc over lower-priority global config files."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = clients.get("opencode")
            self.assertIsNotNone(original)
            client = replace(original, path=base / "opencode.json")
            config = client.path.parent
            (config / "config.json").write_text("{}", encoding="utf-8")
            (config / "opencode.json").write_text("{}", encoding="utf-8")
            (config / "opencode.jsonc").write_text("{}", encoding="utf-8")

            selected = clients.config_path(client)

            self.assertEqual(selected, config / "opencode.jsonc")
            self.assertEqual(
                clients.config_files(client),
                (config / "config.json", config / "opencode.json", config / "opencode.jsonc"),
            )

    def test_context_block_round_trip_is_surgical(self) -> None:
        """Context opt-in preserves surrounding content and supports removal."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = clients.get("gemini")
            self.assertIsNotNone(original)
            context = base / "GEMINI.md"
            client = replace(original, path=base / "settings.json", context_file=context)
            context.write_text("# User notes\n\nKeep this.\n", encoding="utf-8")

            self.assertEqual(clients.write_context_block(client), "added")
            first = context.read_text(encoding="utf-8")
            self.assertEqual(clients.write_context_block(client), "unchanged")
            self.assertTrue(clients.remove_context_block(client))
            final = context.read_text(encoding="utf-8")

        self.assertIn("# User notes", first)
        self.assertIn("Keep this.", first)
        self.assertEqual(final, "# User notes\n\nKeep this.\n")

    def test_json_adapter_writes_expected_entry_without_real_home(self) -> None:
        """Write a normal mcpServers config entirely inside the temp tree."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = clients.get("cursor")
            self.assertIsNotNone(original)
            client = replace(original, path=base / "cursor" / "mcp.json")
            client.path.parent.mkdir(parents=True, exist_ok=True)
            client.path.write_text(json.dumps({"settings": {"keep": True}}), encoding="utf-8")

            self.assertEqual(clients.write_entry(client), "added")
            data = json.loads(client.path.read_text(encoding="utf-8"))
            removed = clients.remove_entry(client)

        self.assertTrue(removed)
        self.assertTrue(data["settings"]["keep"])
        self.assertEqual(data["mcpServers"][clients.SERVER_KEY], client.entry)

    def test_opencode_malformed_config_is_not_overwritten(self) -> None:
        """A malformed selected config raises before any atomic write occurs."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = clients.get("opencode")
            self.assertIsNotNone(original)
            client = replace(original, path=base / "opencode.json")
            client.path.parent.mkdir(parents=True, exist_ok=True)
            malformed = '{"mcp": [}'
            client.path.write_text(malformed, encoding="utf-8")

            with self.assertRaises(jsonc.ConfigFormatError):
                clients.write_entry(client)

            self.assertEqual(client.path.read_text(encoding="utf-8"), malformed)

    def test_opencode_v2_refresh_preserves_native_settings_and_other_bytes(self) -> None:
        """Edit native V2, retaining environment, disabled state and server options."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=2):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            selected = client.path.with_suffix(".jsonc")
            native = {"type": "local", "command": ["/old/python"], "disabled": True,
                      "environment": {"KLYK_UPDATE_CHECK": "0", "CUSTOM": "keep"},
                      "cwd": ".", "codemode": False,
                      "timeout": {"startup": 45000}, "protocol": "legacy"}
            text = '{\r\n\t// Keep café\r\n\t"theme": "保持",\r\n\t"mcp": {"timeout": {"catalog": 60000}, "servers": ' + json.dumps({"klyk": native, "type": {"type": "local", "command": ["keep"]}}) + '}\r\n}\r\n'
            selected.write_bytes(text.encode())
            selected.chmod(0o640)
            self.assertEqual(clients.current_entry(client), native)
            self.assertEqual(clients.write_entry(client), "updated")
            written = selected.read_bytes()
            updated = clients.current_entry(client)
            self.assertEqual(updated["command"], client.entry["command"])
            self.assertEqual(updated["environment"], {"PYTHONPATH": "", **native["environment"]})
            for key in ("disabled", "cwd", "codemode", "timeout", "protocol"):
                self.assertEqual(updated[key], native[key])
            self.assertIn(b"// Keep caf\xc3\xa9\r\n", written)
            self.assertIn('"theme": "保持"'.encode(), written)
            self.assertEqual(selected.stat().st_mode & 0o777, 0o640)
            self.assertEqual(clients.write_entry(client), "unchanged")
            self.assertEqual(selected.read_bytes(), written)
            self.assertTrue(clients.remove_entry(client))
            self.assertIsNone(clients.current_entry(client))
            self.assertIn('"type": {"type": "local", "command": ["keep"]}', selected.read_text())

    def test_opencode_v2_migrates_legacy_enable_and_timeout_without_duplicates(self) -> None:
        """Normalize the documented V1 controls when creating a V2 server entry."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=2):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            legacy = {"type": "local", "command": ["old"], "enabled": False,
                      "timeout": 30000, "environment": {"KLYK_UPDATE_CHECK": "0"}}
            client.path.write_text(json.dumps({"mcp": {"klyk": legacy, "other": {"type": "local", "command": ["keep"]}}}))
            self.assertEqual(clients.current_entry(client), legacy)
            self.assertEqual(clients.write_entry(client), "updated")
            mcp = json.loads(client.path.read_text())["mcp"]
            self.assertNotIn("klyk", mcp)
            self.assertEqual(mcp["other"]["command"], ["keep"])
            entry = mcp["servers"]["klyk"]
            self.assertTrue(entry["disabled"])
            self.assertNotIn("enabled", entry)
            self.assertEqual(entry["timeout"], {"catalog": 30000, "execution": 30000})
            self.assertEqual(entry["environment"]["KLYK_UPDATE_CHECK"], "0")

    def test_opencode_v2_mixed_entries_use_native_precedence_per_server(self) -> None:
        """Supported flat members coexist, with native values winning equal names."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=2):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            old = {"type": "local", "command": ["old"], "enabled": False}
            native = {"type": "local", "command": ["native"], "disabled": True}
            client.path.write_text(json.dumps({"mcp": {"klyk": old, "servers": {"other": native}}}))
            self.assertEqual(clients.current_entry(client), old)
            client.path.write_text(json.dumps({"mcp": {"klyk": old, "servers": {"klyk": native}}}))
            self.assertEqual(clients.current_entry(client), native)
            self.assertEqual(clients.write_entry(client), "updated")
            self.assertEqual(clients.current_entry(client)["disabled"], True)
            self.assertNotIn("klyk", json.loads(client.path.read_text())["mcp"])

    def test_opencode_v2_ignores_old_filename_but_can_migrate_its_owned_entry(self) -> None:
        """Migrate only klyk from V1 config.json, preserving unrelated legacy data."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=2):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            legacy = client.path.parent / "config.json"
            legacy.write_text(json.dumps({"theme": "old", "mcp": {"klyk": {"type": "local", "command": ["old"], "enabled": False}}}))
            original = legacy.read_bytes()
            self.assertIsNone(clients.current_entry(client))
            self.assertEqual(clients.config_files(client), ())
            self.assertEqual(clients.write_entry(client), "updated")
            self.assertTrue(clients.current_entry(client)["disabled"])
            self.assertEqual(legacy.read_bytes(), original)
            self.assertNotIn("theme", json.loads(client.path.read_text()))
            self.assertTrue(clients.remove_entry(client))
            self.assertNotIn("klyk", json.loads(legacy.read_text())["mcp"])

    def test_opencode_unknown_or_unsupported_major_refuses_mutation(self) -> None:
        """A version lookup failure must not silently generate a V1 config."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            client.path.write_text('{"theme":"keep"}')
            for major in (None, 3):
                with self.subTest(major=major), \
                     mock.patch.object(clients, "opencode_major_version", return_value=major), \
                     mock.patch.object(clients, "opencode_executable", return_value="/fake/opencode"):
                    with self.assertRaises(clients.ConfigFormatError):
                        clients.write_entry(client)
                    self.assertEqual(client.path.read_text(), '{"theme":"keep"}')

    def test_opencode_version_parses_only_successful_version_output(self) -> None:
        """Use fake CLI output without running any installed executable."""
        cases = (("2.0.4\n", 0, 2), ("opencode v1.9.1-beta.2", 0, 1),
                 ("2.0.4", 1, None), ("warning\n2.0.4", 0, None))
        real_version = self._version_function
        for output, code, expected in cases:
            with self.subTest(output=output), \
                 mock.patch.object(clients, "opencode_executable", return_value="/fake/opencode"), \
                 mock.patch.object(clients.subprocess, "run", return_value=mock.Mock(stdout=output, returncode=code)) as run:
                self.assertEqual(real_version(), expected)
                self.assertEqual(run.call_args.args[0], ["/fake/opencode", "--version"])

    def test_opencode_ambiguous_containers_and_reserved_legacy_name_are_retained(self) -> None:
        """Never drop a duplicate container or reuse a V1 server called servers."""
        texts = ('{"mcp":{"servers":{},"servers":{}}}',
                 '{"mcp":{"servers":{"type":"local","command":["keep"]}}}')
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=2):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            for text in texts:
                with self.subTest(text=text):
                    client.path.write_text(text)
                    with self.assertRaises(clients.ConfigFormatError):
                        clients.write_entry(client)
                    self.assertEqual(client.path.read_text(), text)

    def test_json_refresh_preserves_deliberate_environment_and_restrictions(self) -> None:
        """Refresh an old interpreter without enabling disabled servers or tools."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("cursor"), path=Path(directory) / "mcp.json")
            old = {"command": "/old/python", "args": [], "disabled": True,
                   "env": {"PYTHONPATH": "/deliberately/trusted", "KLYK_UPDATE_CHECK": "0"},
                   "disabledTools": ["click"], "timeout": 5000}
            client.path.write_text(json.dumps({"mcpServers": {"klyk": old, "other": {"keep": True}}}))
            self.assertEqual(clients.write_entry(client), "updated")
            entry = clients.current_entry(client)
            for key in ("disabled", "env", "disabledTools", "timeout"):
                self.assertEqual(entry[key], old[key])
            self.assertEqual(entry["args"], ["-P", "-m", "klyk.mcp_server"])
            self.assertEqual(client.entry["env"], {"PYTHONPATH": ""})

    def test_strict_json_rejects_ambiguous_and_nonfinite_settings(self) -> None:
        """Ambiguous values fail without modifying existing credential-bearing files."""
        texts = ('{"mcpServers":{},"mcpServers":{}}', '{"x":NaN}',
                 '{"x":1e10000}', '{"mcpServers":{"klyk":{"env":[]}}}',
                 '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}')
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("cursor"), path=Path(directory) / "mcp.json")
            for text in texts:
                with self.subTest(text=text[:40]):
                    client.path.write_text(text)
                    with self.assertRaises(clients.ConfigFormatError):
                        clients.write_entry(client)
                    self.assertEqual(client.path.read_text(), text)

    def test_new_toml_launch_includes_safe_import_environment(self) -> None:
        """The TOML setup offers the same import boundary as JSON clients."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("codex"), path=Path(directory) / "config.toml")
            self.assertEqual(clients.write_entry(client), "added")
            self.assertEqual(clients.current_entry(client), client.entry)

    def test_opencode_uninstall_rolls_back_earlier_file_on_replace_failure(self) -> None:
        """Multiple managed configs do not end half-removed after a write error."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            first = client.path.parent / "config.json"
            for path in (first, client.path):
                path.write_text('{"mcp":{"klyk":{},"other":{"keep":true}}}')
            before = {path: path.read_bytes() for path in (first, client.path)}
            real_write = jsonc.atomic_write

            def fail_selected(path, text, **kwargs):
                """Fail the second destination while permitting the rollback."""
                if path == client.path:
                    raise OSError("fixture write failure")
                return real_write(path, text, **kwargs)

            with mock.patch.object(jsonc, "atomic_write", side_effect=fail_selected):
                with self.assertRaises(OSError):
                    clients.remove_entry(client)
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_opencode_refuses_stale_lower_precedence_customization(self) -> None:
        """The selected file cannot override settings changed in a source during refresh."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            lower = client.path.parent / "config.json"
            lower.write_text('{"mcp":{"klyk":{"type":"local","command":["old"],"environment":{"CUSTOM":"before"}}}}')
            client.path.write_text('{"theme":"keep"}')
            real_set = jsonc.set_mcp_entry

            def edit_source(*args, **kwargs):
                """Model an external editor finishing after the original snapshots."""
                lower.write_text('{"mcp":{"klyk":{"type":"local","command":["old"],"environment":{"CUSTOM":"external"}}}}')
                return real_set(*args, **kwargs)

            with mock.patch.object(jsonc, "set_mcp_entry", side_effect=edit_source):
                with self.assertRaises(clients.ConfigFormatError):
                    clients.write_entry(client)
            self.assertEqual(client.path.read_text(), '{"theme":"keep"}')
            self.assertEqual(json.loads(lower.read_text())["mcp"]["klyk"]["environment"]["CUSTOM"], "external")

    def test_opencode_duplicate_owned_policy_fields_are_not_silently_chosen(self) -> None:
        """An ambiguous disable flag or environment must be resolved before refresh."""
        texts = ('{"mcp":{"klyk":{"disabled":true,"disabled":false}}}',
                 '{"mcp":{"servers":{"klyk":{"environment":{"MODE":"safe","MODE":"other"}}}}}')
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            for major, text in zip((1, 2), texts):
                with self.subTest(major=major), mock.patch.object(clients, "opencode_major_version", return_value=major):
                    client.path.write_text(text)
                    with self.assertRaises(clients.ConfigFormatError):
                        clients.write_entry(client)
                    self.assertEqual(client.path.read_text(), text)

    def test_explicit_native_shape_is_usable_when_cli_version_is_unavailable(self) -> None:
        """Read native server maps without mistaking a server named type for V1."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(clients, "opencode_major_version", return_value=None), \
             mock.patch.object(clients, "opencode_executable", return_value="/fake/opencode"):
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            client.path.write_text(json.dumps({"mcp": {"servers": {"type": {"type": "local", "command": ["keep"]}, "klyk": client.entry}}}))
            self.assertEqual(clients.current_entry(client), client.entry)
            self.assertEqual(clients.write_entry(client), "unchanged")

    def test_v1_downgrade_cannot_enable_or_duplicate_an_existing_native_server(self) -> None:
        """Fail closed when V1 cannot retain a native V2 server's policy semantics."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("opencode"), path=Path(directory) / "opencode.json")
            text = '{"mcp":{"servers":{"klyk":{"type":"local","command":["old"],"disabled":true}}}}'
            client.path.write_text(text)
            with self.assertRaises(clients.ConfigFormatError):
                clients.write_entry(client)
            self.assertEqual(client.path.read_text(), text)

    def test_falsey_wrong_toml_container_cannot_be_poisoned_by_append(self) -> None:
        """An empty scalar or array is invalid schema, not an absent server table."""
        with tempfile.TemporaryDirectory() as directory:
            client = replace(clients.get("codex"), path=Path(directory) / "config.toml")
            for text in ('mcp_servers = ""\n', 'mcp_servers = []\n', 'mcp_servers = 0\n', '[mcp_servers]\nklyk = []\n'):
                with self.subTest(text=text):
                    client.path.write_text(text)
                    with self.assertRaises(clients.ConfigFormatError):
                        clients.write_entry(client)
                    self.assertEqual(client.path.read_text(), text)

    def test_antigravity_refuses_stale_legacy_customization(self) -> None:
        """Migration retains a legacy editor's later privacy setting instead of importing stale state."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            client = replace(clients.get("agy"), path=base / "config" / "mcp_config.json")
            legacy = base / "antigravity-cli" / "mcp_config.json"
            legacy.parent.mkdir()
            legacy.write_text('{"mcpServers":{"klyk":{"command":"old","args":[],"env":{"CUSTOM":"before"}}}}')
            real_refresh = clients._refreshed_entry

            def edit_legacy(*args, **kwargs):
                """Simulate a newer editor save after the migration snapshot was read."""
                legacy.write_text('{"mcpServers":{"klyk":{"command":"old","args":[],"env":{"KLYK_UPDATE_CHECK":"0"}}}}')
                return real_refresh(*args, **kwargs)

            with mock.patch.object(clients, "_refreshed_entry", side_effect=edit_legacy):
                with self.assertRaises(clients.ConfigFormatError):
                    clients.write_entry(client)
            self.assertFalse(client.path.exists())
            self.assertEqual(json.loads(legacy.read_text())["mcpServers"]["klyk"]["env"], {"KLYK_UPDATE_CHECK": "0"})

    def test_toml_snippet_preserves_unicode_paths_and_environment_values(self) -> None:
        """Non-BMP paths use valid TOML Unicode rather than JSON surrogate escapes."""
        with tempfile.TemporaryDirectory() as directory:
            entry = {"command": "/fixture/🔒/python", "args": ["-P", "-m", "klyk.mcp_server"],
                     "env": {"PYTHONPATH": "", "LABEL": "café 🔒"}}
            client = replace(clients.get("codex"), path=Path(directory) / "config.toml", entry=entry)
            self.assertEqual(clients.write_entry(client), "added")
            self.assertEqual(clients.current_entry(client), entry)


if __name__ == "__main__":
    unittest.main()
