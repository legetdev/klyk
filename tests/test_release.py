"""Portable release-name checks for private plans, secrets, and evidence."""

import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

from release_check import check_names, main


class ReleaseNameTests(unittest.TestCase):
    """Ensure publication checks reject private or generated path entries."""

    def test_targeted_evidence_requires_fresh_passing_minor_checks(self):
        """Targeted releases cannot substitute empty, stale, failed, or major evidence."""
        valid = {'fingerprint': 'candidate', 'completed': True, 'scope': 'minor',
                 'rationale': 'Installer-only change; migration and native discovery checked.',
                 'checks': [{'name': 'migration', 'passed': True}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'targeted.json'
            with mock.patch('release_check.fingerprint', return_value='candidate'), \
                 mock.patch('release_check.subprocess.check_output', return_value='README.md'), \
                 mock.patch('sys.argv', ['release_check.py', '--targeted', str(path)]), \
                 mock.patch('builtins.print'):
                path.write_text(json.dumps(valid))
                main()
                for changes in ({'fingerprint': 'old'}, {'completed': False}, {'scope': 'major'},
                                {'rationale': ''}, {'checks': []}, {'error': 'failed'},
                                {'checks': [{'name': 'migration', 'passed': False}]}):
                    with self.subTest(changes=changes):
                        path.write_text(json.dumps({**valid, **changes}))
                        with self.assertRaises(ValueError):
                            main()

    def test_full_evidence_remains_supported_and_cannot_mix_with_targeted(self):
        """Keep the full release route while rejecting ambiguous verification modes."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.json'
            path.write_text(json.dumps({'fingerprint': 'candidate', 'completed': True,
                'checks': [{'name': 'native', 'passed': True}],
                'tools': ['inspect'], 'calls': [{'tool': 'inspect'}]}))
            with mock.patch('release_check.fingerprint', return_value='candidate'), \
                 mock.patch('release_check.subprocess.check_output', return_value='README.md'), \
                 mock.patch('builtins.print'):
                with mock.patch('sys.argv', ['release_check.py', '--live', str(path)]):
                    main()
                with mock.patch('sys.argv', ['release_check.py', '--live', str(path), '--targeted', str(path)]):
                    with self.assertRaises(ValueError):
                        main()

    def test_private_and_generated_entries_are_rejected(self):
        """Reject private plans, environment files, and verification artifacts anywhere in a path."""
        rejected = (
            "ROADMAP.md",
            "archive/POLARSTAR.md",
            ".env",
            ".verification/live.json",
        )
        for name in rejected:
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_names([name])

    def test_public_source_entries_are_allowed(self):
        """Allow ordinary source, documentation, and test entries used in a release."""
        check_names(
            [
                "README.md",
                "SECURITY.md",
                "klyk/clients.py",
                "tests/test_release.py",
                "dist/klyk-0.5.0-py3-none-any.whl",
            ]
        )


if __name__ == "__main__":
    unittest.main()
