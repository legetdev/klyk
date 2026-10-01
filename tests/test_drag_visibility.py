"""Cross-app visible drag ownership and cancellation with all native boundaries inert."""

import asyncio
import ctypes
import threading
import time
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions
from test_input_security import core_namespace


class VisibleHitTests(unittest.TestCase):
    """A covered app-scoped endpoint cannot authorize system-wide mouse input."""

    def _namespace(self):
        """Provide retained fake AX references with explicit ownership and exact-window equality."""
        namespace={'ctypes':ctypes,'_cf':MagicMock(),'_check_stop':MagicMock(),
                   '_ax_element_at':MagicMock(return_value=20),
                   '_ax_matches_pid':MagicMock(return_value=True),
                   '_ax_exact_window':MagicMock(return_value=40),
                   '_ax_str_attr':MagicMock(return_value='AXButton'),
                   '_ax_read_attr_ptr':MagicMock(return_value=30)}
        namespace['_cf'].CFEqual.return_value=True
        load_functions('computer.py',{'ax_visible_target_at'},namespace)
        return namespace

    def test_actual_global_hit_and_exact_window_are_required_and_released(self):
        """Never pass an expected PID to the hit lookup that can see behind another window."""
        for role,releases in (('AXButton',[30,40,20]),('AXWindow',[40,20])):
            with self.subTest(role=role):
                ns=self._namespace();ns['_ax_str_attr'].return_value=role
                self.assertTrue(ns['ax_visible_target_at'](12,34,123,7))
                ns['_ax_element_at'].assert_called_once_with(12,34)
                ns['_ax_exact_window'].assert_called_once_with(123,7)
                self.assertEqual([call.args[0].value for call in ns['_cf'].CFRelease.call_args_list],releases)
                if role=='AXWindow':ns['_ax_read_attr_ptr'].assert_not_called()

    def test_unknown_foreign_and_sibling_hits_refuse_without_leaking(self):
        """PID ownership alone cannot approve another selected window of the same application."""
        for failure in ('no_hit','foreign_hit','missing_window','no_owner','foreign_owner','sibling'):
            with self.subTest(failure=failure):
                ns=self._namespace()
                if failure=='no_hit':ns['_ax_element_at'].return_value=0
                elif failure=='foreign_hit':ns['_ax_matches_pid'].return_value=False
                elif failure=='missing_window':ns['_ax_exact_window'].return_value=0
                elif failure=='no_owner':ns['_ax_read_attr_ptr'].return_value=0
                elif failure=='foreign_owner':ns['_ax_matches_pid'].side_effect=[True,False]
                else:ns['_cf'].CFEqual.return_value=False
                self.assertFalse(ns['ax_visible_target_at'](12,34,123,7))
                released=[call.args[0].value for call in ns['_cf'].CFRelease.call_args_list]
                self.assertEqual(released,[] if failure=='no_hit' else [20] if failure in
                                 ('foreign_hit','missing_window') else [40,20] if failure=='no_owner' else [30,40,20])

    def test_windowless_hit_still_needs_known_positive_pid(self):
        """Dock-like targets need actual visible ownership and never an unconstrained PID match."""
        ns=self._namespace()
        self.assertTrue(ns['ax_visible_target_at'](12,34,123))
        ns['_ax_exact_window'].assert_not_called();ns['_ax_read_attr_ptr'].assert_not_called()
        for pid,window in ((None,None),(True,None),(0,None),(-1,None),(123,0),(123,True)):
            with self.subTest(pid=pid,window=window):
                ns=self._namespace()
                self.assertFalse(ns['ax_visible_target_at'](12,34,pid,window))
                ns['_ax_element_at'].assert_not_called()

    def test_native_failure_releases_references_and_stop_prevents_lookup(self):
        """Unknown native state cannot leave retained references or bypass an existing stop."""
        ns=self._namespace();ns['_cf'].CFEqual.side_effect=RuntimeError('inert native failure')
        with self.assertRaisesRegex(RuntimeError,'inert native failure'):
            ns['ax_visible_target_at'](12,34,123,7)
        self.assertEqual([call.args[0].value for call in ns['_cf'].CFRelease.call_args_list],[30,40,20])
        ns=self._namespace();ns['_check_stop'].side_effect=RuntimeError('inert stop')
        with self.assertRaisesRegex(RuntimeError,'inert stop'):ns['ax_visible_target_at'](12,34,123,7)
        ns['_ax_element_at'].assert_not_called()


class VisibleDragTests(unittest.IsolatedAsyncioTestCase):
    """Preflight stays inside the input lock and precedes every mouse-down."""

    async def test_covered_source_or_destination_sends_no_event(self):
        """A refused endpoint cannot become a partial drag or move the actual cursor."""
        for source_visible in (False,True):
            with self.subTest(source_visible=source_visible):
                ns,events=core_namespace()
                ns['ax_visible_target_at']=MagicMock(side_effect=[source_visible,False])
                load_functions('computer.py',{'drag'},ns)
                with self.assertRaisesRegex(RuntimeError,'No drag was sent'):
                    await ns['drag'](10,20,30,40,visible_targets=((123,7),(345,8)))
                self.assertEqual(events,[]);self.assertEqual(ns['_held_inputs'],{})
                ns['_cg'].CGEventCreateMouseEvent.assert_not_called()
                self.assertEqual(ns['ax_visible_target_at'].call_count,2 if source_visible else 1)

    async def test_visible_pair_keeps_modified_drag_balanced(self):
        """Successful checks use both exact identities and preserve button/modifier delivery."""
        ns,events=core_namespace();ns['ax_visible_target_at']=MagicMock(return_value=True)
        load_functions('computer.py',{'drag'},ns)
        await ns['drag'](10,20,30,40,steps=1,step_delay=0,button='right',modifiers=['shift'],
                         visible_targets=((123,7),(345,None)))
        self.assertEqual([call.args for call in ns['ax_visible_target_at'].call_args_list],
                         [(10.0,20.0,123,7),(30.0,40.0,345,None)])
        self.assertEqual(events,[(3,(10.0,20.0),1),(7,(30.0,40.0),1),(4,(30.0,40.0),1)])
        self.assertEqual([call.args[1] for call in ns['_cg'].CGEventSetFlags.call_args_list],[0x20000]*3)

    async def test_window_changed_while_waiting_for_input_lock_is_not_approved(self):
        """Do not cache a successful visibility check before a preceding input finishes."""
        ns,events=core_namespace();ns['ax_visible_target_at']=MagicMock(return_value=False)
        load_functions('computer.py',{'drag'},ns)
        await ns['_input_lock'].acquire()
        task=asyncio.create_task(ns['drag'](10,20,30,40,visible_targets=((123,7),(345,8))))
        await asyncio.sleep(0)
        ns['ax_visible_target_at'].assert_not_called()
        ns['_input_lock'].release()
        with self.assertRaisesRegex(RuntimeError,'No drag was sent'):await task
        self.assertEqual(events,[])

    async def test_cancelled_or_stopped_preflight_never_begins_drag(self):
        """Cancellation drains the native read and a stop after it still blocks the first down."""
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled):
                ns,events=core_namespace();entered=threading.Event()

                def hit(*_arguments):
                    """Model a bounded metadata read that observes worker cancellation or a stop."""
                    entered.set()
                    if cancelled:
                        while not ns['_worker_state'].cancelled.is_set():time.sleep(.001)
                        ns['_check_stop']()
                    else:ns['_stop_engaged'][0]=True
                    return True

                ns['ax_visible_target_at']=hit;load_functions('computer.py',{'drag'},ns)
                task=asyncio.create_task(ns['drag'](10,20,30,40,visible_targets=((123,7),(345,8))))
                for _ in range(1000):
                    if entered.is_set():break
                    await asyncio.sleep(.001)
                self.assertTrue(entered.is_set())
                if cancelled:task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancelled else RuntimeError):await task
                self.assertEqual(events,[]);self.assertEqual(ns['_held_inputs'],{})

    async def test_incomplete_visibility_pair_cannot_approve_one_endpoint(self):
        """Internal callers cannot accidentally omit the destination from a guarded drag."""
        ns,events=core_namespace();ns['ax_visible_target_at']=MagicMock(return_value=True)
        load_functions('computer.py',{'drag'},ns)
        with self.assertRaisesRegex(ValueError,'both endpoint identities'):
            await ns['drag'](10,20,30,40,visible_targets=((123,7),))
        ns['ax_visible_target_at'].assert_not_called();self.assertEqual(events,[])


if __name__=='__main__':
    unittest.main()
