"""Check compact fixture geometry and process environments without constructing native windows."""

import os
import unittest
from unittest.mock import patch

from live_smoke import compact_layout_matches, fixture_environment, fixture_input_unchanged, held_key_released


class FixtureLayoutTests(unittest.TestCase):
    """Preserve all native controls and separate drag surfaces on the smaller runner desktop."""

    def _state(self, *, receiver=False, moved=False):
        """Represent measured frame and content bounds rather than driver response success."""
        second=120 if receiver else 560 if moved else 540
        return {'layout':'compact','windows':[
            {'title':title,'x':x,'y':20,'width':400,'height':628,'content_width':400,'content_height':600}
            for title,x in (('Klyk Fixture A',120),('Klyk Fixture B',second))]}

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
