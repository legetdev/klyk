"""Keep malformed cache files and failed writes from breaking update diagnostics."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from klyk import updates


class UpdateCacheTests(unittest.TestCase):
    """Exercise the shared cache without network or user-state access."""

    def test_invalid_cache_recovers_with_one_fetch(self):
        """Invalid field types and timestamps are treated as a cache miss."""
        for data in ([1], {}, {'checked_at': 'today'}, {'checked_at': True},
                     {'checked_at': float('nan')}, {'checked_at': float('inf')},
                     {'checked_at': -1}, {'checked_at': 1, 'latest': [1]}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'cache.json'
                path.write_text(json.dumps(data))
                with patch.object(updates, '_CACHE_PATH', path), \
                     patch.object(updates, '_memo_mtime', None), \
                     patch.object(updates, 'enabled', return_value=True), \
                     patch.object(updates, '_fetch_latest', return_value='1.2.3') as fetch:
                    self.assertIsNone(updates.status()['latest'])
                    self.assertEqual(updates.check()['latest'], '1.2.3')
                    updates.check()
                    fetch.assert_called_once()

    def test_future_timestamp_does_not_suppress_refresh(self):
        """A clock correction must not suppress checks until a future date."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.json'
            path.write_text(json.dumps({'checked_at': 200, 'latest': '0.1.0'}))
            with patch.object(updates, '_CACHE_PATH', path), \
                 patch.object(updates, '_memo_mtime', None), \
                 patch.object(updates, 'enabled', return_value=True), \
                 patch.object(updates.time, 'time', return_value=100), \
                 patch.object(updates, '_fetch_latest', return_value='1.2.3') as fetch:
                self.assertEqual(updates.check()['latest'], '1.2.3')
                fetch.assert_called_once()

    def test_failed_replace_preserves_previous_cache_and_cleans_temp(self):
        """A failed atomic write neither truncates the cache nor leaves debris."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.json'
            original = '{"checked_at": 1, "latest": "0.1.0"}'
            path.write_text(original)
            with patch.object(updates, '_CACHE_PATH', path), \
                 patch.object(updates.os, 'replace', side_effect=OSError('write failed')):
                updates._write_cache('1.2.3')
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
