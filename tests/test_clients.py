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


if __name__ == "__main__":
    unittest.main()
