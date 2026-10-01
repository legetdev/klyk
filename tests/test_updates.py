"""Keep malformed cache files and failed writes from breaking update diagnostics."""

import json
import io
import os
import sys
import tempfile
import time
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
                     {'checked_at': -1}, {'checked_at': 10 ** 400},
                     {'checked_at': 1, 'latest': [1]}):
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

    def test_cache_rejects_fifo_large_symlink_and_deep_metadata(self):
        """Every malformed cache is a prompt cache miss without following targets."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fifo = base / 'fifo'
            os.mkfifo(fifo)
            large = base / 'large'
            with large.open('wb') as handle:
                handle.truncate(updates._MAX_CACHE_BYTES + 1)
            real = base / 'real'
            real.write_text('{"checked_at":1,"latest":"0.5.4"}')
            link = base / 'link'
            link.symlink_to(real)
            deep = base / 'deep'
            deep.write_text('[' * 1100 + '0' + ']' * 1100)
            for path in (fifo, large, link, deep):
                with self.subTest(path=path.name), patch.object(updates, '_CACHE_PATH', path), \
                     patch.object(updates, '_memo_mtime', None):
                    started = time.monotonic()
                    self.assertIsNone(updates._read_cache())
                    self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(real.read_text(), '{"checked_at":1,"latest":"0.5.4"}')

    def test_cache_memo_not_stale_after_replacement_with_same_mtime(self):
        """Separate processes can atomically update a cache without defeating memoization."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache'
            path.write_text('{"checked_at":1,"latest":"0.5.4"}')
            with patch.object(updates, '_CACHE_PATH', path), patch.object(updates, '_memo_mtime', None):
                self.assertEqual(updates._read_cache()['latest'], '0.5.4')
                previous = path.stat()
                replacement = path.with_name('replacement')
                replacement.write_text('{"checked_at":1,"latest":"0.5.5"}')
                os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
                os.replace(replacement, path)
                self.assertEqual(updates._read_cache()['latest'], '0.5.5')

    def test_disabled_check_neither_reads_cache_nor_fetches(self):
        """The privacy opt-out stops every automatic update lookup."""
        with patch.dict(os.environ, {'KLYK_UPDATE_CHECK': '0'}), \
             patch.object(updates, '_read_cache', side_effect=AssertionError('cache must not be read')), \
             patch.object(updates, '_fetch_latest', side_effect=AssertionError('network must not be used')):
            status = updates.check(force=True)
            self.assertFalse(status['enabled'])
            self.assertIsNone(status['latest'])

    def test_cache_write_is_private_and_does_not_follow_existing_symlink(self):
        """Replacement does not truncate a linked file and uses private metadata modes."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            victim = base / 'victim'
            victim.write_text('preserve')
            path = base / 'cache'
            path.symlink_to(victim)
            with patch.object(updates, '_CACHE_PATH', path):
                updates._write_cache('0.5.5')
            self.assertEqual(victim.read_text(), 'preserve')
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class UpdateFetchTests(unittest.TestCase):
    """Use in-memory HTTP fixtures to validate metadata trust and resource limits."""

    def _fetch(self, body):
        """Supply an inert response without any real socket or application calls."""
        response = io.BytesIO(body)
        from unittest.mock import Mock
        opener = Mock()
        opener.open.return_value = response
        with patch.object(updates.urllib.request, 'build_opener', return_value=opener) as build:
            result = updates._fetch_latest()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://pypi.org/pypi/klyk/json')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], updates._FETCH_TIMEOUT_S)
        self.assertIsInstance(build.call_args.args[0], updates._NoRedirect)
        return result

    def test_metadata_accepts_only_bounded_canonical_versions(self):
        """Unexpected structures and oversized metadata cannot become update commands."""
        self.assertEqual(self._fetch(b'{"info":{"version":"1.2.3"}}'), '1.2.3')
        for body in (b'[]', b'{"info":[]}', b'{"info":{"version":"1.2.3;bad"}}',
                     b'{"info":{"version":"1.2.3"}}' + b' ' * updates._MAX_METADATA_BYTES):
            with self.subTest(body=body[:40]):
                self.assertIsNone(self._fetch(body))

    def test_redirects_and_read_deadline_fail_without_following_new_endpoints(self):
        """Reject endpoint changes and expire slow metadata under a finite budget."""
        redirect = updates._NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, '', {}, 'http://other.example/'))
        from unittest.mock import Mock
        response = io.BytesIO(b'{"info":{"version":"1.2.3"}}')
        opener = Mock()
        opener.open.return_value = response
        with patch.object(updates.urllib.request, 'build_opener', return_value=opener), \
             patch.object(updates.time, 'monotonic', side_effect=(0, updates._FETCH_TIMEOUT_S + 1)):
            self.assertIsNone(updates._fetch_latest())


class InstallMethodTests(unittest.TestCase):
    """Verify each upgrade command without invoking installers or changing environments."""

    def test_method_classifier_distinguishes_disposable_uvx_cache(self):
        """An ephemeral uvx interpreter never receives an ordinary pip upgrade."""
        cases = (('/home/u/.local/pipx/venvs/klyk', 'pipx'),
                 ('/home/u/.local/share/uv/tools/klyk', 'uv'),
                 ('/home/u/.cache/uv/archive-v0/abc', 'uvx'),
                 ('/home/u/venv', 'pip'))
        for prefix, expected in cases:
            with self.subTest(prefix=prefix):
                self.assertEqual(updates._detect_method(prefix, prefix + '/lib/python/site-packages/klyk/__init__.py'), expected)
        self.assertEqual(updates._detect_method('/venv', '/repo/klyk/__init__.py'), 'editable')
        self.assertEqual(updates.upgrade_command('uvx'), ['uv', 'tool', 'run', '--upgrade', '--from', 'klyk', 'klyk', 'version'])
        self.assertEqual(updates.upgrade_command('pip')[1:3], ['-P', '-m'])

    def test_custom_tool_directories_use_native_receipts_and_cache_location(self):
        """First-party receipt metadata supports managed locations outside defaults."""
        import klyk
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'custom-tool'
            prefix.mkdir()
            with patch.object(sys, 'prefix', str(prefix)), \
                 patch.object(klyk, '__file__', str(prefix / 'site-packages/klyk/__init__.py')):
                receipt = prefix / 'uv-receipt.toml'
                receipt.write_text('')
                self.assertEqual(updates.install_method(), 'uv')
                receipt.unlink()
                receipt = prefix / 'pipx_metadata.json'
                receipt.write_text('{}')
                self.assertEqual(updates.install_method(), 'pipx')
                receipt.unlink()
                with patch.dict(os.environ, {'UV_CACHE_DIR': directory}):
                    self.assertEqual(updates.install_method(), 'uvx')


if __name__ == '__main__':
    unittest.main()
