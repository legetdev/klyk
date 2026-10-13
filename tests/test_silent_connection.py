"""Keep permission-denied connection evidence distinct from working native access."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from silent_connection_smoke import budget_error, permission_denied
from silent_protocol_bootstrap import inhibit_stop_tap

ROOT = Path(__file__).resolve().parents[1]


class SilentTapAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Exercise repeated actual lazy initialization without native listener bookkeeping."""

    async def test_repeated_native_initialization_never_starts_the_inhibited_tap(self):
        """A granted initialization path must survive both startup calls and later requests."""
        native_start = Mock(side_effect=AssertionError('native tap must remain inhibited'))
        computer = SimpleNamespace(_start_emergency_stop_tap=native_start, _stop_thread=[None])
        audit = {'substitutions': []}
        inhibit_stop_tap(computer, audit)
        tree = ast.parse((ROOT / 'klyk/mcp_server.py').read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_ensure_native_runtime')
        namespace = {'asyncio': asyncio, 'computer': computer, '_native_initialized': False,
                     '_native_init_lock': asyncio.Lock(),
                     'connection_gate': SimpleNamespace(checkpoint=lambda: None)}

        def finish():
            """Complete only the inert finish phase; native delivery remains outside this test."""
            computer._start_emergency_stop_tap()
            namespace['_native_initialized'] = True

        dispatch = Mock(side_effect=lambda callback, timeout: callback())
        native_verify = Mock(side_effect=AssertionError('native verification must remain inhibited'))
        namespace.update(_initialize_native_runtime=Mock(),
                         _finish_native_initialization=Mock(side_effect=finish),
                         _ui=SimpleNamespace(dispatch_sync=dispatch),
                         skylight=SimpleNamespace(is_available=lambda: False, self_test_async=native_verify))
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'actual-lazy-entry', 'exec'), namespace)
        await namespace['_ensure_native_runtime']()
        await namespace['_ensure_native_runtime']()
        self.assertEqual(namespace['_initialize_native_runtime'].call_count, 1)
        self.assertEqual(namespace['_finish_native_initialization'].call_count, 1)
        self.assertEqual(dispatch.call_count, 2)
        native_verify.assert_not_called()
        native_start.assert_not_called()
        self.assertEqual(computer._stop_thread, [None])
        self.assertIn('direct global emergency-stop event tap startup', audit['substitutions'])

    async def test_unexpected_native_listener_thread_fails_before_start(self):
        """The remaining thread guard rejects an adapter miss rather than silently accepting it."""
        tree = ast.parse((ROOT / 'tests/silent_protocol_bootstrap.py').read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == 'guarded_start')
        original_start = Mock()

        def forbidden(name):
            """Fail before any thread target can run, retaining the prohibited boundary name."""
            def reject():
                """Represent the bootstrap's existing fail-fast native prohibition."""
                raise RuntimeError(name)
            return reject

        namespace = {'original_start': original_start, 'forbidden': forbidden}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'actual-start-guard', 'exec'), namespace)
        with self.assertRaisesRegex(RuntimeError, 'unexpected native emergency-stop thread startup'):
            namespace['guarded_start'](SimpleNamespace(name='klyk-stop'))
        original_start.assert_not_called()
        worker = SimpleNamespace(name='klyk')
        namespace['guarded_start'](worker)
        original_start.assert_called_once_with(worker)


class SilentConnectionEvidenceTests(unittest.TestCase):
    """Reject generic failures, substituted grants and unexpected captured content."""

    def test_permission_refusal_requires_the_actual_matching_query(self):
        """A plain tool error qualifies only alongside its independently recorded false preflight."""
        for api, error in (
                ('AXIsProcessTrustedWithOptions', 'klyk needs Accessibility permission to read the AX tree'),
                ('CGPreflightScreenCaptureAccess', 'klyk needs Screen Recording permission to capture window contents')):
            data = {'ok': False, 'error': error, '_meta': {'duration_ms': 1}}
            self.assertTrue(permission_denied(data, {'permission_queries': [{'api': api, 'allowed': False}]}))
            for allowed in (True, 0, None):
                self.assertFalse(permission_denied(data, {'permission_queries': [{'api': api, 'allowed': allowed}]}))

    def test_arbitrary_failure_or_private_content_cannot_qualify_denial(self):
        """The fallback must not turn a product exception or captured pixels into accepted evidence."""
        audit = {'permission_queries': [{'api': 'AXIsProcessTrustedWithOptions', 'allowed': False}]}
        valid = {'ok': False, 'error': 'klyk needs Accessibility permission to read the AX tree'}
        for data in ({'ok': False, 'error': 'unexpected product failure'},
                     {**valid, 'main': {'width': 100}}, {**valid, 'pixels': 'private'},
                     {**valid, 'blocked': 'access_off'}, {**valid, 'ok': True}):
            self.assertFalse(permission_denied(data, audit))
        self.assertFalse(permission_denied(valid, {'permission_queries': []}))
        self.assertFalse(permission_denied(valid, {'permission_queries': [
            {'api': 'CGPreflightScreenCaptureAccess', 'allowed': False}]}))

    def test_fresh_on_handler_proof_requires_the_exact_bounded_batch_error(self):
        """Only a handler that passed the access gate can establish resumed protocol access."""
        def result(data):
            """Construct a content frame without invoking any server or native module."""
            return {'content': [{'type': 'text', 'text': json.dumps(data)}]}

        self.assertTrue(budget_error(result({'ok': False,
            'error': 'run supports at most 8 nested levels; split the sequence and observe between batches.'})))
        for data in ({'ok': False, 'blocked': 'access_off'}, {'ok': False, 'error': 'permission denied'},
                     {'ok': True, 'error': 'run supports at most 8 nested levels;'}):
            self.assertFalse(budget_error(result(data)))


if __name__ == '__main__':
    unittest.main()
