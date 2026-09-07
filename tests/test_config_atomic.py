"""Protect existing client settings when configuration writes fail."""

import json
import tempfile
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


if __name__ == '__main__':
    unittest.main()
