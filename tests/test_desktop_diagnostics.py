"""Check Electron readiness and failure evidence with only temporary files and inert native reads."""

import ast
from collections import deque
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

    def electron_sequence(self, observations, *, omit_completion_check=False, saved_override=None):
        """Run the actual fixture's Electron statements and polling with an inert owned editor."""
        tree=ast.parse(Path(__file__).with_name('desktop_smoke.py').read_text())
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        check=next(node for node in main.body if isinstance(node,ast.FunctionDef) and node.name=='check')

        def call_statement(node, name):
            """Find named fixture steps without importing or running its native main entry point."""
            return (isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                    and isinstance(node.value.func,ast.Name) and node.value.func.id==name)

        body=next(node.body for node in ast.walk(main) if isinstance(getattr(node,'body',None),list)
                  and any(call_statement(item,'call') and any(
                      keyword.arg=='key' and isinstance(keyword.value,ast.Constant) and keyword.value.value=='cmd+1'
                      for keyword in item.value.keywords) for item in node.body))
        start=next(index for index,node in enumerate(body) if call_statement(node,'call') and any(
            keyword.arg=='key' and isinstance(keyword.value,ast.Constant) and keyword.value.value=='cmd+1'
            for keyword in node.value.keywords))
        end=next(index for index,node in enumerate(body) if call_statement(node,'check')
                 and isinstance(node.value.args[0],ast.Constant)
                 and node.value.args[0].value=='Electron editor saved exact Unicode text')
        statements=body[start:end+1]
        if omit_completion_check:
            statements=[node for node in statements if not (call_statement(node,'check')
                        and node.value.args[0].value=='Electron exact Unicode text observed before saving')]
        module=ast.Module(body=[check,*statements],type_ignores=[])
        clock=[0.0];pending=deque(observations)
        state={'events':[],'typed':False,'value':'Electron baseline','role':'AXTextArea',
               'file':'Electron baseline','report':{'checks':[]}}

        def read(pid):
            """Advance synthetic receiver evidence only through bounded reads of the owned host."""
            self.assertEqual(pid,123)
            if state['typed'] and pending:
                state['role'],state['value']=pending.popleft()
            state['events'].append(('read',state['role'],state['value']))
            return {'focused':{'role':state['role'],'value':state['value']}}

        def call(client, tool, **arguments):
            """Record inputs without native adapters, leaving posted text unacknowledged until read."""
            self.assertEqual(arguments.get('app'),'Klyk Electron Fixture')
            self.assertEqual(arguments.get('window_id'),77)
            state['events'].append(('input',tool,arguments.get('key')))
            if tool=='type_text':
                self.assertEqual(arguments.get('text'),'Electron verified 🧭')
                self.assertEqual(arguments.get('mode'),'keys')
                state['typed']=True
            if arguments.get('key')=='cmd+s':
                state['file']=state['value'] if saved_override is None else saved_override
            return {'ok':True}

        def sleep(duration):
            """Advance the real polling loop's deadline without waiting or sending further input."""
            clock[0]+=duration

        document=SimpleNamespace(read_text=Mock(side_effect=lambda:state['file']))
        namespace={'electron_editor_ready':electron_editor_ready,'computer':SimpleNamespace(ax_focused_summary=read),
                   'editor':SimpleNamespace(pid=123),'editor_wid':77,'c':object(),'document':document,
                   'report':state['report'],'call':call,
                   'time':SimpleNamespace(monotonic=lambda:clock[0],sleep=sleep)}

        def run():
            """Execute only the selected original source statements under inert fixture bindings."""
            exec(compile(ast.fix_missing_locations(module),'inert_electron_sequence','exec'),namespace)

        state['document']=document
        return run,state

    def test_old_sequence_saves_before_posted_text_is_observed(self):
        """The former fixture advanced on posting success while its owned editor still held baseline."""
        run,state=self.electron_sequence((('AXTextArea','Electron baseline'),
                                         ('AXTextArea','Electron ve'),('AXTextArea','Electron verified 🧭')),
                                        omit_completion_check=True)
        with self.assertRaisesRegex(AssertionError,'Electron editor saved exact Unicode text'):
            run()
        self.assertEqual(state['file'],'Electron baseline')
        self.assertEqual(sum(event==('input','press_key','cmd+s') for event in state['events']),1)
        self.assertFalse(any(event==('read','AXTextArea','Electron verified 🧭') for event in state['events']))

    def test_delayed_exact_text_is_observed_before_one_save(self):
        """Receiver lag causes reads until exact content appears, with no repeated typing or shortcut."""
        run,state=self.electron_sequence((('AXTextArea','Electron baseline'),
                                         ('AXTextArea','Electron ve'),('AXTextArea','Electron verified 🧭')))
        run()
        ready=state['events'].index(('read','AXTextArea','Electron verified 🧭'))
        save=state['events'].index(('input','press_key','cmd+s'))
        self.assertLess(ready,save)
        self.assertEqual(sum(event[0]=='input' and event[1]=='type_text' for event in state['events']),1)
        self.assertEqual(sum(event==('input','press_key','cmd+s') for event in state['events']),1)
        self.assertEqual(state['report']['electron_after_typing']['focused']['value'],'Electron baseline')
        self.assertTrue(all(check['passed'] for check in state['report']['checks']))

    def test_persistent_permutation_never_sends_save(self):
        """A complete but reordered value is a failure, even though type_text returned success."""
        run,state=self.electron_sequence((('AXTextArea','Electron baseline'),
                                         ('AXTextArea','Electron vfied 🧭eri')))
        with self.assertRaisesRegex(AssertionError,'Electron exact Unicode text observed before saving'):
            run()
        self.assertFalse(any(event==('input','press_key','cmd+s') for event in state['events']))
        self.assertEqual(sum(event[0]=='input' and event[1]=='type_text' for event in state['events']),1)
        state['document'].read_text.assert_not_called()
        self.assertEqual(state['report']['checks'][-1],
                         {'name':'Electron exact Unicode text observed before saving','passed':False})

    def test_matching_text_in_another_role_never_sends_save(self):
        """A label or group with the expected string cannot prove completed owned editor input."""
        run,state=self.electron_sequence((('AXGroup','Electron verified 🧭'),))
        with self.assertRaisesRegex(AssertionError,'Electron exact Unicode text observed before saving'):
            run()
        self.assertFalse(any(event==('input','press_key','cmd+s') for event in state['events']))

    def test_exact_editor_observation_still_requires_independent_file_readback(self):
        """The added readiness observation does not replace the existing exact saved-file proof."""
        run,state=self.electron_sequence((('AXTextArea','Electron verified 🧭'),),saved_override='Electron baseline')
        with self.assertRaisesRegex(AssertionError,'Electron editor saved exact Unicode text'):
            run()
        self.assertEqual(sum(event==('input','press_key','cmd+s') for event in state['events']),1)
        self.assertFalse(state['report']['checks'][-1]['passed'])


if __name__=='__main__':unittest.main()
