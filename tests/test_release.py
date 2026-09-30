"""Portable release-name checks for private plans, secrets, and evidence."""

import unittest
import json
import tempfile
import subprocess
from pathlib import Path
from unittest import mock

from release_check import check_names, main, expected_tools, NATIVE_CHECKS, DESKTOP_CHECKS


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
                 mock.patch('release_check.subprocess.check_output', return_value=b'README.md\0'), \
                 mock.patch('sys.argv', ['release_check.py', '--targeted', str(path)]), \
                 mock.patch('builtins.print'):
                path.write_text(json.dumps(valid))
                main()
                for changes in ({'fingerprint': 'old'}, {'completed': False}, {'scope': 'major'},
                                {'rationale': ''}, {'checks': []}, {'error': 'failed'},
                                {'cleanup_errors': ['KeyboardInterrupt']}, {'report_error': 'OSError'},
                                {'checks': [{'name': 'migration', 'passed': False}]}):
                    with self.subTest(changes=changes):
                        path.write_text(json.dumps({**valid, **changes}))
                        with self.assertRaises(ValueError):
                            main()

    def _native(self, sdk):
        """Build complete synthetic report data without claiming a native test ran."""
        tools = sorted(expected_tools())
        return {'fingerprint': 'candidate', 'completed': True,
                'environment': {'mcp': sdk},
                'checks': [{'name': name, 'passed': True} for name in sorted(NATIVE_CHECKS)],
                'tools': tools, 'calls': [{'tool': name} for name in tools]}

    def _desktop(self):
        """Represent the distinct Chrome/Electron observations required by the gate."""
        return {'fingerprint': 'candidate', 'completed': True,
                'environment': {'mcp': '2.2.0'},
                'checks': [{'name': name, 'passed': True} for name in sorted(DESKTOP_CHECKS)],
                'calls': [{'tool': 'inspect'}, {'tool': 'click'}, {'tool': 'type_text'}]}

    def _run_reports(self, reports, kinds=('live', 'live', 'desktop')):
        """Exercise the real gate against disposable evidence files and an isolated index."""
        with tempfile.TemporaryDirectory() as directory:
            arguments = ['release_check.py']
            for index, (kind, report) in enumerate(zip(kinds, reports)):
                path = Path(directory) / f'{index}.json'
                path.write_text(json.dumps(report))
                arguments.extend([f'--{kind}', str(path)])
            with mock.patch('release_check.fingerprint', return_value='candidate'), \
                 mock.patch('release_check.subprocess.check_output', return_value=b'README.md\0'), \
                 mock.patch('builtins.print'), mock.patch('sys.argv', arguments):
                main()

    def test_full_evidence_needs_both_sdks_and_desktop(self):
        """A single SDK, duplicate SDK, or missing application suite cannot qualify a major release."""
        first, second, desktop = self._native('1.30.0'), self._native('2.2.0'), self._desktop()
        self._run_reports([first, second, desktop])
        for reports, kinds in (([first, desktop], ('live', 'desktop')),
                               ([first, first, desktop], ('live', 'live', 'desktop')),
                               ([first, second], ('live', 'live')),
                               ([desktop], ('desktop',))):
            with self.subTest(kinds=kinds), self.assertRaises(ValueError):
                self._run_reports(reports, kinds)

    def test_full_evidence_requires_actual_declared_tool_coverage(self):
        """Empty, partial, duplicated, or invented tool lists must not pass by agreeing with calls."""
        first, second, desktop = self._native('1.30.0'), self._native('2.2.0'), self._desktop()
        self.assertEqual(len(first['tools']), 48)
        for changes in ({'tools': [], 'calls': []},
                        {'tools': ['inspect'], 'calls': [{'tool': 'inspect'}]},
                        {'tools': ['invented'], 'calls': [{'tool': 'invented'}]},
                        {'tools': first['tools'] + ['inspect']},
                        {'calls': first['calls'][:-1]}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self._run_reports([{**first, **changes}, second, desktop])

    def test_each_independent_native_and_desktop_check_is_required(self):
        """Calling every tool cannot substitute for independently observed native/browser effects."""
        reports = [self._native('1.30.0'), self._native('2.2.0'), self._desktop()]
        for index in (0, 2):
            for omitted in reports[index]['checks']:
                partial = {**reports[index], 'checks': [check for check in reports[index]['checks'] if check != omitted]}
                changed = list(reports)
                changed[index] = partial
                with self.subTest(kind=index, omitted=omitted['name']), self.assertRaises(ValueError):
                    self._run_reports(changed)

    def test_malformed_report_fields_never_become_acceptance(self):
        """Truthy strings, nameless assertions, malformed calls, and missing SDK versions fail closed."""
        first, second, desktop = self._native('1.30.0'), self._native('2.2.0'), self._desktop()
        for changes in ({'completed': 1}, {'completed': 'true'}, {'checks': {}},
                        {'checks': [{'name': '', 'passed': True}]},
                        {'checks': [{'name': 'sentinel', 'passed': 'true'}]},
                        {'checks': [None]}, {'calls': [None]}, {'tools': {}},
                        {'environment': {}}, {'environment': {'mcp': '3.0.0'}},
                        {'environment': {'mcp': 2}}, {'environment': {'mcp': '2'}},
                        {'error': 'native failure'}, {'cleanup_errors': ['ValueError']},
                        {'cleanup_errors': False}, {'cleanup_errors': {}}, {'report_error': 'OSError'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._run_reports([{**first, **changes}, second, desktop])
        for malformed in ([], None, True):
            with self.subTest(report=malformed), self.assertRaises(ValueError):
                self._run_reports([malformed, second, desktop])

    def test_full_and_targeted_evidence_cannot_be_mixed(self):
        """Minor reports cannot weaken or make the major gate ambiguous."""
        with self.assertRaises(ValueError):
            self._run_reports([self._native('1.30.0'), self._desktop(), {}], ('live', 'desktop', 'targeted'))

    def test_private_and_generated_entries_are_rejected(self):
        """Reject private plans, environment files, and verification artifacts anywhere in a path."""
        rejected = (
            "ROADMAP.md",
            "archive/POLARSTAR.md",
            ".env",
            "klyk/.env.production",
            ".env.local",
            ".verification/live.json",
            "klyk/.VERIFICATION/credential.json",
            "klyk/.CLAUDE/settings.json",
            "klyk/.CoDeX/config.toml",
            "klyk/__PYCACHE__/code.pyc",
            "klyk/secret.LOG",
            "klyk/generated.PYC",
            "klyk\\.ENV.PRODUCTION",
            "/absolute/runtime.py",
            "C:\\private\\runtime.py",
            "klyk/../private/runtime.py",
        )
        for name in rejected:
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_names([name])

    def test_git_quoted_unicode_filename_cannot_hide_private_directory(self):
        """Use a real disposable index to catch Git display escaping of a private path."""
        with tempfile.TemporaryDirectory(prefix='klyk-release-index-') as directory:
            root=Path(directory)
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','config','--local','user.email','78686489+legetdev@users.noreply.github.com'],cwd=root,check=True)
            subprocess.run(['git','config','--local','user.name','Bent'],cwd=root,check=True)
            private=root/'.verification';private.mkdir()
            name='.verification/ü.png'
            (root/name).write_bytes(b'synthetic fixture; no screenshot')
            subprocess.run(['git','add','-f','--',name],cwd=root,check=True)
            quoted=subprocess.check_output(['git','-c','core.quotePath=true','ls-files'],cwd=root,text=True)
            self.assertTrue(quoted.startswith('".verification/'))
            with mock.patch('release_check.ROOT',root),mock.patch('sys.argv',['release_check.py']),mock.patch('builtins.print'):
                with self.assertRaisesRegex(ValueError,'Private or generated content'):
                    main()

    def test_public_source_entries_are_allowed(self):
        """Allow ordinary source, documentation, and test entries used in a release."""
        check_names(
            [
                "README.md",
                ".env.example",
                "SECURITY.md",
                "klyk/clients.py",
                "tests/test_release.py",
                "dist/klyk-0.5.0-py3-none-any.whl",
            ]
        )


if __name__ == "__main__":
    unittest.main()
