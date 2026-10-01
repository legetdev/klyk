"""Check compact fixture geometry and process environments without constructing native windows."""

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from live_smoke import compact_layout_matches, fixture_environment, fixture_input_unchanged, held_key_released


class FixtureLayoutTests(unittest.TestCase):
    """Preserve all native controls and separate drag surfaces on the smaller runner desktop."""

    def _state(self, *, receiver=False, moved=False, y=20, height=628, visible_frame=None):
        """Represent independent native bounds and an explicit synthetic usable screen area."""
        second=120 if receiver else 560 if moved else 540
        visible=visible_frame or {'x':0,'y':0,'width':1024,'height':700}
        return {'layout':'compact','requested_y':20,'windows':[
            {'id':index+1,'title':title,'visible':True,'x':x,'y':y,'width':400,'height':height,
             'content_width':400,'content_height':600,'screen_visible_frame':dict(visible)}
            for index,(title,x) in enumerate((('Klyk Fixture A',120),('Klyk Fixture B',second)))]}

    def test_compact_layout_keeps_controls_and_separate_cross_app_targets(self):
        """The moved source and overlapping receiver fit 1024 by 700 without sharing pixels."""
        primary=self._state(moved=True)
        receiver=self._state(receiver=True)
        self.assertTrue(compact_layout_matches(self._state()))
        self.assertTrue(compact_layout_matches(primary,moved=True))
        self.assertTrue(compact_layout_matches(receiver,receiver=True))
        source=primary['windows'][1]
        destination=receiver['windows'][1]
        self.assertGreater(source['x'],destination['x']+destination['width'])
        for window in primary['windows']+receiver['windows']:
            self.assertLessEqual(window['x']+window['width'],1024)
            self.assertLessEqual(window['y']+window['height'],700)

    def test_wrong_window_position_or_shrunken_content_fails(self):
        """Offscreen, moved, missing, duplicate, or shrunk native geometry cannot qualify."""
        for changes in ({'x':1420},{'y':160},{'content_height':500},{'content_width':300},{'height':800}):
            state=self._state()
            state['windows'][1].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(compact_layout_matches(state))
        state=self._state();state['windows'][1]['title']='Klyk Fixture A'
        self.assertFalse(compact_layout_matches(state))
        self.assertFalse(compact_layout_matches({'layout':'compact','windows':[]}))

    def test_actual_native_y_adjustment_is_bounded_and_aligned(self):
        """Recorded y83/frame632 can qualify with a real usable area, without guessing a clamp inset."""
        visible={'x':0,'y':80,'width':1024,'height':664}
        initial=self._state(y=83,height=632,visible_frame=visible)
        moved=self._state(moved=True,y=83,height=632,visible_frame=visible)
        receiver=self._state(receiver=True,y=83,height=632,visible_frame=visible)
        self.assertTrue(compact_layout_matches(initial))
        self.assertTrue(compact_layout_matches(moved,moved=True,aligned_y=83))
        self.assertTrue(compact_layout_matches(receiver,receiver=True,aligned_y=83))
        self.assertFalse(compact_layout_matches(initial,aligned_y=20))
        # Cocoa may add an inset; containment and observed alignment decide qualification.
        self.assertTrue(compact_layout_matches(self._state(y=85,height=632,visible_frame=visible)))
        receiver['windows'][0]['y']=receiver['windows'][1]['y']=84
        self.assertFalse(compact_layout_matches(receiver,receiver=True,aligned_y=83))

    def test_unsafe_native_visible_area_identity_or_content_never_qualifies(self):
        """Refuse clipped frames, arbitrary surfaces, duplicate IDs, missing area, and unequal origins."""
        visible={'x':0,'y':83,'width':1024,'height':661}
        for changes in ({'y':80},{'y':115},{'content_width':399},{'content_height':599},
                        {'visible':False},{'id':1},{'x':float('nan')},{'height':float('inf')}):
            state=self._state(y=83,height=632,visible_frame=visible)
            state['windows'][1].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(compact_layout_matches(state))
        state=self._state(y=83,height=632,visible_frame=visible)
        state['windows'][1]['y']=84
        self.assertFalse(compact_layout_matches(state))
        for changes in ({'height':610},{'width':900},{'height':float('inf')},{'x':float('nan')}):
            state=self._state(y=83,height=632,visible_frame=visible)
            for window in state['windows']:window['screen_visible_frame'].update(changes)
            with self.subTest(visible_area=changes):
                self.assertFalse(compact_layout_matches(state))
        state=self._state(y=83,height=632,visible_frame=visible)
        state['windows'][1].pop('screen_visible_frame')
        self.assertFalse(compact_layout_matches(state))
        state=self._state(y=83,height=632,visible_frame=visible)
        state['windows'][1]['screen_visible_frame']['y']=84
        self.assertFalse(compact_layout_matches(state))
        state=self._state();state['requested_y']=160
        self.assertFalse(compact_layout_matches(state))

    def test_receiver_check_uses_its_own_current_screen_frame(self):
        """A changed usable screen between launches cannot impose the primary's historical y on the receiver."""
        tree=ast.parse(Path(__file__).with_name('live_smoke.py').read_text())
        check=next(node for node in ast.walk(tree) if isinstance(node,ast.Call)
                   and isinstance(node.func,ast.Name) and node.func.id=='check'
                   and node.args and isinstance(node.args[0],ast.Constant)
                   and node.args[0].value=='compact receiver native bounds stay on left')
        primary=self._state(moved=True,y=83,height=632,
                            visible_frame={'x':0,'y':63,'width':1024,'height':674})
        receiver=self._state(receiver=True,y=80,height=632,
                             visible_frame={'x':0,'y':60,'width':1024,'height':677})
        self.assertTrue(compact_layout_matches(primary,moved=True,aligned_y=83))
        self.assertFalse(compact_layout_matches(receiver,receiver=True,aligned_y=83))
        expression=compile(ast.Expression(check.args[1]),'inert_receiver_layout','eval')
        bindings={'compact_layout_matches':compact_layout_matches,'json':json,
                  'receiver_state':SimpleNamespace(read_text=lambda:json.dumps(receiver)),
                  'report':{'fixture_layout':{'primary_after_move':primary['windows']}}}
        self.assertTrue(eval(expression,bindings))
        for changes in ({'y':79},{'y':59},{'x':540},{'content_height':599},{'id':1}):
            with self.subTest(changes=changes):
                original=dict(receiver['windows'][1])
                receiver['windows'][1].update(changes)
                self.assertFalse(eval(expression,bindings))
                receiver['windows'][1]=original

    def test_file_panel_shift_restores_both_source_windows_before_receiver(self):
        """Execute the actual inert placement block so activation cannot cover the later receiver."""
        tree=ast.parse(Path(__file__).with_name('live_smoke.py').read_text())
        block=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
                   and any(isinstance(child,ast.Constant)
                           and child.value=='compact source windows stay on right after file panels'
                           for child in ast.walk(node)))
        state=self._state(moved=True,y=83,height=632,
                          visible_frame={'x':0,'y':60,'width':1024,'height':677})
        state['windows'][1]['x']=384
        self.assertFalse(compact_layout_matches(state,source_on_right=True))
        observed=[{'window_id':2,'x':384,'y':53,'width':400,'height':632},
                  {'window_id':1,'x':120,'y':53,'width':400,'height':632}]
        calls=[];report={'fixture_layout':{}}

        def call(_client,tool,**arguments):
            """Apply only inert window geometry while recording exact selected-window ordering."""
            calls.append((tool,arguments))
            if tool=='list_windows':return {'windows':observed}
            self.assertEqual(tool,'set_window_bounds')
            self.assertEqual({key:arguments[key] for key in ('x','y','width','height')},
                             {'x':560,'y':53,'width':400,'height':632})
            state['windows'][arguments['window_id']-1]['x']=arguments['x']

        bindings={'compact':True,'call':call,'client':None,'target':{'window_id':2},
                  'report':report,'current':lambda:state,'compact_layout_matches':compact_layout_matches,
                  'check':lambda _name,condition:self.assertTrue(condition)}
        exec(compile(ast.Module(body=[block],type_ignores=[]),'inert_source_placement','exec'),bindings)
        self.assertEqual([arguments['window_id'] for tool,arguments in calls if tool=='set_window_bounds'],[1,2])
        self.assertEqual(report['fixture_layout']['primary_before_receiver'],state['windows'])
        self.assertTrue(compact_layout_matches(state,source_on_right=True))
        for window in state['windows']:self.assertGreater(window['x'],120+400)
        for changes in ({'x':120},{'x':384},{'id':1},{'content_height':599},{'y':59}):
            with self.subTest(changes=changes):
                original=dict(state['windows'][1]);state['windows'][1].update(changes)
                self.assertFalse(compact_layout_matches(state,source_on_right=True))
                state['windows'][1]=original
        self.assertFalse(compact_layout_matches(state,receiver=True,source_on_right=True))

    def test_compact_children_receive_exact_isolated_geometry_flags(self):
        """Inherited overlap/offset cannot displace the primary or receiver in compact mode."""
        with patch.dict(os.environ,{'KLYK_FIXTURE_COMPACT':'1','KLYK_FIXTURE_OFFSET':'999','KLYK_FIXTURE_OVERLAP':'1'}):
            primary=fixture_environment('primary.json')
            receiver=fixture_environment('receiver.json',receiver=True)
        self.assertEqual((primary['KLYK_FIXTURE_OFFSET'],primary['KLYK_FIXTURE_OVERLAP']),('0','0'))
        self.assertEqual((receiver['KLYK_FIXTURE_OFFSET'],receiver['KLYK_FIXTURE_OVERLAP']),('0','1'))
        self.assertEqual(receiver['KLYK_FIXTURE_STATE'],'receiver.json')

    def test_held_key_requires_independent_matching_up_event(self):
        """Later successful text cannot qualify a hold with missing, mismatched, or duplicated up."""
        before={'input_events':{'key_down:124':3,'key_up:124':2}}
        after={'input_events':{'key_down:124':5,'key_up:124':3}}
        self.assertTrue(held_key_released(before,after))
        for events in ({'key_down:124':5,'key_up:124':2},
                       {'key_down:124':3,'key_up:124':3},
                       {'key_down:124':5,'key_up:124':4},
                       {'key_down:124':5,'key_up:123':3}):
            with self.subTest(events=events):
                self.assertFalse(held_key_released(before,{'input_events':events,'fields':['valuea']}))
        self.assertFalse(held_key_released({},{}))
        self.assertFalse(held_key_released(before,{'input_events':None}))

    def test_absent_dialog_refusal_requires_no_independent_input_or_effect(self):
        """A failure response cannot qualify when native input or fixture state changed."""
        before={'input_events':{'key_down:0':1},'fields':['baseline'],'clicks':0,'selections':0,
                'selection':['none'],'scroll':[0],'drops':[],'opened':''}
        self.assertTrue(fixture_input_unchanged(before,dict(before)))
        changes={'input_events':{'key_down:0':2},'fields':['altered'],'clicks':1,'selections':1,
                 'selection':['{0,1}'],'scroll':[1],'drops':['payload'],'opened':'unexpected'}
        for field,value in changes.items():
            after={**before,field:value}
            with self.subTest(field=field):
                self.assertFalse(fixture_input_unchanged(before,after))
        after=dict(before);after.pop('input_events')
        self.assertFalse(fixture_input_unchanged(before,after))

    def test_default_receiver_keeps_existing_full_layout(self):
        """Without compact opt-in, the existing receiver offset remains 880 points."""
        with patch.dict(os.environ,{'KLYK_FIXTURE_COMPACT':'0'}):
            self.assertEqual(fixture_environment('receiver.json',receiver=True)['KLYK_FIXTURE_OFFSET'],'880')


if __name__=='__main__':
    unittest.main()
