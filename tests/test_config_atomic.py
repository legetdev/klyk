"""Protect existing client settings when configuration writes fail."""

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from klyk import clients, jsonc


class AtomicConfigTests(unittest.TestCase):
    """Verify all config-writing routes preserve the original on replace failure."""

    def test_failed_writes_preserve_original_bytes(self):
        """Install, uninstall, TOML append, and context updates never truncate files."""
        cases = (
            ('json', clients.write_entry, '{"mcpServers":{"other":{"command":"keep"}}}'),
            ('json', clients.remove_entry, '{"mcpServers":{"klyk":{},"other":{}}}'),
            ('toml', clients.write_entry, '# keep\n[features]\nenabled = true\n'),
            ('context', clients.write_context_block, 'User instructions\n'),
            ('context', clients.write_context_block, 'User instructions\n<!-- klyk:start -->old<!-- klyk:end -->\n'),
            ('context', clients.remove_context_block, 'User instructions\n' + clients.context_block()),
        )
        for fmt, operation, original in cases:
            with self.subTest(fmt=fmt, operation=operation.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config'
                path.write_text(original)
                client = clients.Client('fixture', 'Fixture', path, fmt, context_file=path)
                with patch.object(jsonc.os, 'replace', side_effect=OSError('write failed')):
                    with self.assertRaises(OSError):
                        operation(client)
                self.assertEqual(path.read_text(), original)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_json_write_preserves_symlink_permissions_and_unrelated_settings(self):
        """Successful updates retain the user's path and surrounding configuration."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target.json'
            target.write_text('{"mcpServers":{"other":{"command":"keep"}},"theme":"dark"}')
            target.chmod(0o640)
            link = Path(directory) / 'config.json'
            link.symlink_to(target)
            client = replace(clients.CLIENTS['cursor'], path=link)
            self.assertEqual(clients.write_entry(client), 'added')
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            data = json.loads(target.read_text())
            self.assertEqual(data['theme'], 'dark')
            self.assertEqual(data['mcpServers']['other'], {'command': 'keep'})
            self.assertEqual(clients.write_entry(client), 'unchanged')

    def test_regular_file_checks_refuse_fifo_and_oversized_config(self):
        """Unusual config paths fail promptly instead of blocking or allocating without bounds."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fifo'
            os.mkfifo(path)
            started = time.monotonic()
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.read_exact(path)
            self.assertLess(time.monotonic() - started, 0.5)
            large = Path(directory) / 'large'
            with large.open('wb') as handle:
                handle.truncate(jsonc._MAX_CONFIG_BYTES + 1)
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.read_exact(large)

    def test_snapshot_rejects_external_edit_or_symlink_retarget(self):
        """A stale caller cannot overwrite an editor's change or a newly selected target."""
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / 'first'
            second = Path(directory) / 'second'
            first.write_text('old')
            second.write_text('preserve')
            link = Path(directory) / 'config'
            link.symlink_to(first)
            original = jsonc.read_snapshot(link)
            first.write_text('external edit')
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.atomic_write(link, 'stale replacement', expected=original)
            self.assertEqual(first.read_text(), 'external edit')
            original = jsonc.read_snapshot(link)
            link.unlink()
            link.symlink_to(second)
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.atomic_write(link, 'stale replacement', expected=original)
            self.assertEqual(first.read_text(), 'external edit')
            self.assertEqual(second.read_text(), 'preserve')

    def test_snapshot_rechecks_after_tempfile_flush(self):
        """An edit during the actual write is detected before atomic replacement."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            path.write_text('original')
            original = jsonc.read_snapshot(path)
            with patch.object(jsonc.os, 'fsync', side_effect=lambda fd: path.write_text('external edit')):
                with self.assertRaises(jsonc.ConfigFormatError):
                    jsonc.atomic_write(path, 'stale replacement', expected=original)
            self.assertEqual(path.read_text(), 'external edit')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_replacement_with_preserved_mtime_is_still_a_conflict(self):
        """File identity catches an atomic replacement even if timestamps are copied."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            path.write_text('original')
            original = jsonc.read_snapshot(path)
            old = path.stat()
            external = path.with_name('external')
            external.write_text('external')
            os.utime(external, ns=(old.st_atime_ns, old.st_mtime_ns))
            os.replace(external, path)
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.atomic_write(path, 'stale replacement', expected=original)
            self.assertEqual(path.read_text(), 'external')

    def test_new_configs_are_private_and_hardlink_targets_are_preserved(self):
        """Atomic replacement creates private files and does not truncate linked originals."""
        with tempfile.TemporaryDirectory() as directory:
            victim = Path(directory) / 'victim'
            victim.write_text('preserve')
            requested = Path(directory) / 'config'
            os.link(victim, requested)
            jsonc.atomic_write(requested, 'replacement')
            self.assertEqual(victim.read_text(), 'preserve')
            self.assertEqual(requested.read_text(), 'replacement')
            created = Path(directory) / 'new'
            jsonc.atomic_write(created, '{}')
            self.assertEqual(created.stat().st_mode & 0o777, 0o600)

    def test_secret_tokens_and_excessive_jsonc_complexity_are_not_echoed(self):
        """Malformed credentials and extreme syntax fail with bounded plain errors."""
        path = Path('/fixture/config.jsonc')
        secret = 'synthetic_private_value_never_echo'
        values = ('{"token":' + secret + '}', '{"x":1e10000}',
                  '{"x":' + '[' * (jsonc._MAX_CONFIG_DEPTH + 1) + '0' + ']' * (jsonc._MAX_CONFIG_DEPTH + 1) + '}')
        for text in values:
            with self.subTest(text=text[:30]), self.assertRaises(jsonc.ConfigFormatError) as raised:
                jsonc.parse_object(text, path)
            self.assertNotIn(secret, str(raised.exception))
        with patch.object(jsonc, '_MAX_CONFIG_TOKENS', 10):
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.parse_object('{"x":[0,0,0,0,0,0,0]}', path)

    def test_unowned_and_broken_link_configs_are_not_changed(self):
        """Refuse ambiguous destinations rather than repairing another user's files."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            path.write_text('preserve')
            with patch.object(jsonc.os, 'getuid', return_value=-1):
                with self.assertRaises(jsonc.ConfigFormatError):
                    jsonc.atomic_write(path, 'replacement')
            self.assertEqual(path.read_text(), 'preserve')
            link = Path(directory) / 'broken'
            link.symlink_to(Path(directory) / 'missing')
            with self.assertRaises(jsonc.ConfigFormatError):
                jsonc.atomic_write(link, 'replacement')
            self.assertTrue(link.is_symlink())


if __name__ == '__main__':
    unittest.main()
