"""Keep permission-denied connection evidence distinct from working native access."""

import json
import unittest

from silent_connection_smoke import budget_error, permission_denied


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
