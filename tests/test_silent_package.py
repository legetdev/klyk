"""Keep installed protocol evidence bound to the actual candidate artifact, without native imports."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from silent_protocol_smoke import ROOT, main, select_package


class SilentPackageTests(unittest.TestCase):
    """Reject source fallbacks and stale, incomplete, or altered installed Python runtimes."""

    def _package(self, directory):
        """Create a temporary installed-layout copy containing only reviewed Python files."""
        prefix=Path(directory)/'venv'
        root=prefix/'lib/site-packages'
        package=root/'klyk'
        package.mkdir(parents=True)
        for path in (ROOT/'klyk').glob('*.py'):
            shutil.copyfile(path,package/path.name)
        return prefix,root,SimpleNamespace(__file__=str(package/'__init__.py'),__version__='fixture')

    def test_installed_selection_requires_environment_origin_and_exact_runtime(self):
        """Matching bytes outside the environment and any changed installed file remain unqualified."""
        with tempfile.TemporaryDirectory() as directory:
            prefix,root,package=self._package(directory)
            with patch.dict(sys.modules,{'klyk':package}),patch.object(sys,'prefix',str(prefix)),patch.object(sys,'path',list(sys.path)):
                actual,metadata=select_package(True)
                self.assertEqual(actual,root.resolve())
                self.assertTrue(metadata['origin_correct'])
                self.assertTrue(metadata['runtime_matches_reviewed_checkout'])
                for path,contents in ((root/'klyk/unexpected.py','pass\n'),(root/'klyk/__init__.py','modified\n')):
                    original=path.read_bytes() if path.exists() else None
                    path.write_text(contents)
                    self.assertFalse(select_package(True)[1]['runtime_matches_reviewed_checkout'])
                    if original is None:path.unlink()
                    else:path.write_bytes(original)
                (root/'klyk/image_bounds.py').unlink()
                self.assertFalse(select_package(True)[1]['runtime_matches_reviewed_checkout'])
            source=SimpleNamespace(__file__=str(ROOT/'klyk/__init__.py'),__version__='fixture')
            with patch.dict(sys.modules,{'klyk':source}),patch.object(sys,'path',list(sys.path)):
                metadata=select_package(True)[1]
                self.assertFalse(metadata['origin_correct'])
                self.assertTrue(metadata['runtime_matches_reviewed_checkout'])

    def test_startup_diagnostics_survive_the_package_binding_check(self):
        """A successful preliminary artifact check must not hide a later startup failure."""
        class FailedClient:
            """Fail the protocol handshake before any tool request or native child exists."""

            def __init__(self, *args, **kwargs):
                """Supply a bounded, synthetic startup diagnostic without launching a process."""
                self._stderr_tail=b'synthetic startup boundary failure'

            def __enter__(self):
                """Represent handshake failure before the request-bearing context starts."""
                raise TimeoutError('synthetic handshake failure')

            def __exit__(self, *args):
                """Provide the unused context-manager counterpart for the inert adapter."""
                return False

        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json'
            package={'origin_correct':True,'runtime_matches_reviewed_checkout':True}
            with patch('silent_protocol_smoke.select_package',return_value=(ROOT,package)), \
                 patch.dict(sys.modules,{'klyk.client':SimpleNamespace(KlykClient=FailedClient)}), \
                 patch.object(sys,'argv',['silent_protocol_smoke.py','--output',str(report)]),patch('builtins.print'):
                with self.assertRaisesRegex(TimeoutError,'synthetic handshake failure'):
                    main()
            result=json.loads(report.read_text())
            self.assertEqual(len(result['checks']),1)
            self.assertTrue(result['checks'][0]['passed'])
            self.assertFalse(result['completed'])
            self.assertEqual(result['startup_diagnostics'],'synthetic startup boundary failure')
            self.assertEqual(report.stat().st_mode & 0o777,0o600)

    def test_different_installed_artifact_is_refused_before_child_launch(self):
        """An older artifact with the same tool names must not reach a native-operating subprocess."""
        with tempfile.TemporaryDirectory() as directory:
            prefix,root,package=self._package(directory)
            (root/'klyk/__init__.py').write_text('old candidate\n')
            report=Path(directory)/'report.json'
            with patch.dict(sys.modules,{'klyk':package}),patch.object(sys,'prefix',str(prefix)),patch.object(sys,'path',list(sys.path)), \
                 patch.object(subprocess,'Popen') as launch,patch.object(sys,'argv',['silent_protocol_smoke.py','--installed','--output',str(report)]),patch('builtins.print'):
                with self.assertRaisesRegex(AssertionError,'actual package origin'):
                    main()
            launch.assert_not_called()
            evidence=json.loads(report.read_text())
            self.assertFalse(evidence['completed'])
            self.assertFalse(evidence['package']['runtime_matches_reviewed_checkout'])
            self.assertEqual(report.stat().st_mode & 0o777,0o600)


if __name__=='__main__':
    unittest.main()
