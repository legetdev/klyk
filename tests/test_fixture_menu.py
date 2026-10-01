"""Exercise passive file-panel handoffs with every native observation intercepted."""
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from live_smoke import fixture_file_menu_ready, wait_for_fixture_file_menu


class FixtureMenuReadinessTests(unittest.TestCase):
    """Require independent sheet closure and exact enabled menu evidence before input."""

    def _state(self):
        """Represent two independently reported windows belonging to the generated fixture."""
        return {'pid':123,'active':True,'windows':[
            {'id':1,'title':'Klyk Fixture A','visible':True,'attached_sheet':False},
            {'id':2,'title':'Klyk Fixture B','visible':True,'attached_sheet':False}]}

    def test_sheet_unknown_owner_or_inactive_state_does_not_probe_menu(self):
        """A saved file cannot substitute for missing, active or unowned sheet evidence."""
        states=[None,{}, {'pid':123,'active':True,'windows':[]}, self._state(),self._state(),self._state()]
        states[3]['windows'][1]['attached_sheet']=True
        states[4]['windows'][0].pop('attached_sheet')
        states[5]['active']=False
        states.extend([{'pid':124,**{k:v for k,v in self._state().items() if k!='pid'}},
                       {'pid':123,'active':True,'windows':[None,None]}])
        for changes in ({'id':1},{'id':True},{'title':'Unrelated'},{'visible':None},{'attached_sheet':0}):
            state=self._state();state['windows'][1].update(changes);states.append(state)
        for state in states:
            with self.subTest(state=state),patch('live_smoke.subprocess.run') as run:
                self.assertFalse(fixture_file_menu_ready(state,123,'Open Test File'))
                run.assert_not_called()

    def test_only_whitelisted_command_and_integer_fixture_pid_can_be_probed(self):
        """Malformed commands or process IDs cannot create injected or unrelated native queries."""
        for pid,command in ((True,'Open Test File'),(0,'Open Test File'),('123','Open Test File'),
                            (123,'Quit'),(123,'Open" & do shell script "id'),(123,None)):
            with self.subTest(pid=pid,command=command),patch('live_smoke.subprocess.run') as run:
                self.assertFalse(fixture_file_menu_ready(self._state(),pid,command))
                run.assert_not_called()

    def test_exact_enabled_native_menu_is_read_only_and_bounded(self):
        """The one probe checks foreground, existence and enabled status without any input."""
        for command in ('Open Test File','Save Test File'):
            with self.subTest(command=command),patch('live_smoke.subprocess.run',return_value=
                    SimpleNamespace(returncode=0,stdout='true\n')) as run:
                self.assertTrue(fixture_file_menu_ready(self._state(),123,command))
                argv=run.call_args.args[0];script=argv[2]
                self.assertEqual(argv[:2],['/usr/bin/osascript','-e'])
                self.assertIn('unix id is 123',script)
                self.assertIn('if not frontmost then return false',script)
                self.assertIn(f'exists menu item "{command}" of menu "File" of menu bar 1',script)
                self.assertIn(f'return enabled of menu item "{command}"',script)
                self.assertFalse(any(word in script for word in ('click ','activate','keystroke','set ')))
                self.assertEqual(run.call_args.kwargs,{'capture_output':True,'text':True,'timeout':1})

    def test_unavailable_disabled_invalid_and_timed_out_native_menu_stays_unready(self):
        """A failed read never becomes a menu action or an optimistic success."""
        results=[SimpleNamespace(returncode=0,stdout=value) for value in ('false','TRUE','','true\nfalse',None)]
        results.append(SimpleNamespace(returncode=1,stdout='true'))
        results.extend((OSError('unavailable'),subprocess.TimeoutExpired('/usr/bin/osascript',1)))
        for result in results:
            with self.subTest(result=result),patch('live_smoke.subprocess.run',**(
                    {'side_effect':result} if isinstance(result,Exception) else {'return_value':result})) as run:
                self.assertFalse(fixture_file_menu_ready(self._state(),123,'Open Test File'))
                self.assertEqual(run.call_count,1)

    def test_persistent_unknown_readiness_reaches_deadline_without_input(self):
        """Passive polling is bounded and never acquires a menu action to replay."""
        clock=[0.0];timeouts=[]
        def probe(state,pid,command,*,timeout):
            """Use the entire supplied passive-query budget without any native action."""
            timeouts.append(timeout);clock[0]+=timeout;return False
        with patch('live_smoke.time.monotonic',side_effect=lambda:clock[0]), \
             patch('live_smoke.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)), \
             patch('live_smoke.fixture_file_menu_ready',side_effect=probe):
            evidence=wait_for_fixture_file_menu(self._state,123,'Open Test File')
        self.assertFalse(evidence['ready'])
        self.assertEqual(evidence['observations'],3)
        self.assertEqual(evidence['elapsed_ms'],3000)
        self.assertEqual(timeouts[:2],[1,1])
        self.assertAlmostEqual(timeouts[2],.9)

    def test_transient_readiness_and_slow_snapshot_do_not_extend_budget(self):
        """A ready observation returns once; a snapshot using the deadline starts no native query."""
        clock=[0.0]
        with patch('live_smoke.time.monotonic',side_effect=lambda:clock[0]), \
             patch('live_smoke.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)), \
             patch('live_smoke.fixture_file_menu_ready',side_effect=[False,True]) as probe:
            evidence=wait_for_fixture_file_menu(self._state,123,'Open Test File')
        self.assertTrue(evidence['ready']);self.assertEqual(evidence['observations'],2)
        self.assertEqual(probe.call_count,2)
        def delayed_state():
            """Represent a state read that consumes the remaining whole observation deadline."""
            clock[0]+=3;return self._state()
        with patch('live_smoke.time.monotonic',side_effect=lambda:clock[0]), \
             patch('live_smoke.fixture_file_menu_ready') as probe:
            evidence=wait_for_fixture_file_menu(delayed_state,123,'Open Test File')
        self.assertFalse(evidence['ready']);self.assertEqual(evidence['observations'],0)
        probe.assert_not_called()
