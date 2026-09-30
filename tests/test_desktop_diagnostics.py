"""Check Electron readiness and failure evidence with only temporary files and inert native reads."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from desktop_smoke import electron_editor_ready, electron_fixture_diagnostic


class DesktopDiagnosticTests(unittest.TestCase):
    """Prevent cold editor focus, unbounded reads, or diagnostic errors from concealing failure."""

    def test_only_owned_focused_text_with_exact_baseline_is_ready(self):
        """A filename or chat field cannot qualify the disposable document for replacement."""
        computer=SimpleNamespace(ax_focused_summary=Mock())
        cases=(({'focused':{'role':'AXTextArea','value':'Electron baseline'}},True),
               ({'focused':{'role':'AXTextField','value':'Electron baseline'}},True),
               ({'window_title':'electron-fixture.txt'},False),
               ({'focused':{'role':'AXTextArea','value':''}},False),
               ({'focused':{'role':'AXTextArea','value':'Electron baselin'}},False),
               ({'focused':{'role':'AXGroup','value':'Electron baseline'}},False),
               ({'focused':{'role':'AXTextArea','value':'Electron baseline extra'}},False))
        for observation,expected in cases:
            with self.subTest(observation=observation):
                computer.ax_focused_summary.return_value=observation
                self.assertIs(electron_editor_ready(computer,123,'Electron baseline'),expected)
                computer.ax_focused_summary.assert_called_with(123)

    def test_failure_reads_generated_unicode_and_exact_owned_host_only(self):
        """The report keeps real Unicode readback without another editor input action."""
        computer=object()
        with tempfile.TemporaryDirectory() as directory:
            document=Path(directory)/'electron-fixture.txt';document.write_text('Electron verified 🧭')
            with patch('desktop_smoke.fixture_panel_diagnostic',return_value={'host_pid':123}) as probe:
                result=electron_fixture_diagnostic(computer,123,document)
        probe.assert_called_once_with(computer,123)
        self.assertEqual(result['saved_text'],'Electron verified 🧭')
        self.assertFalse(result['saved_text_truncated'])
        self.assertEqual(result['native'],{'host_pid':123})

    def test_file_evidence_has_a_fixed_byte_bound(self):
        """A large generated file cannot become an unbounded failure attachment."""
        stream=Mock();stream.read.return_value=b'x'*8193
        document=Mock();document.open.return_value.__enter__=Mock(return_value=stream)
        document.open.return_value.__exit__=Mock(return_value=False)
        with patch('desktop_smoke.fixture_panel_diagnostic',return_value={}):
            result=electron_fixture_diagnostic(object(),123,document)
        stream.read.assert_called_once_with(8193)
        self.assertEqual(len(result['saved_text']),8192)
        self.assertTrue(result['saved_text_truncated'])

    def test_diagnostic_failure_retains_only_categories(self):
        """Missing files or native failures stay inconclusive without replaying private error text."""
        document=Mock();document.open.side_effect=OSError('private-fixture-sentinel')
        with patch('desktop_smoke.fixture_panel_diagnostic',side_effect=ValueError('private-fixture-sentinel')):
            result=electron_fixture_diagnostic(object(),123,document)
        self.assertEqual(result['file_read_error'],'OSError')
        self.assertEqual(result['native_probe_error'],'ValueError')
        self.assertNotIn('private-fixture-sentinel',str(result))


if __name__=='__main__':unittest.main()
