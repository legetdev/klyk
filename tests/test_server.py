"""Regression tests for targeting, failure reporting, validation, and observation contracts."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
from mcp import types
from server_support import load_server, payload


class ServerTests(unittest.IsolatedAsyncioTestCase):
    """Execute production dispatcher functions with controlled native outcomes."""

    def setUp(self):
        """Create a clean server and a disposable 100 by 100 window for each test."""
        self.s = load_server()
        self.session = SimpleNamespace(app='Fixture',pid=123,window_id=7,width=100,height=100,
                                       win_x=0,win_y=0,mode='autonomous',windowless=False)
        self.s._get_session = AsyncMock(return_value=(self.session,False))
        self.s._refresh_window = AsyncMock()
        self.s.computer._check_stop.return_value = None
        self.s.computer.ax_perform_action_at.return_value = {"ok":True}
        self.s.ownership.is_owner.return_value = True

    async def test_bounds_exclude_right_and_bottom_edges(self):
        """Pixels at the exclusive right/bottom edge must not reach another window."""
        for x,y in [(100,50),(50,100),(-1,0),(0,-1)]:
            with self.subTest(x=x,y=y): self.assertFalse((await self.s._check_click_safety(self.session,x,y))[0])
        self.assertTrue((await self.s._check_click_safety(self.session,99,99))[0])

    async def test_missing_bounds_fail_closed(self):
        """Unresolved window geometry is not permission to click arbitrary screen pixels."""
        for width,height in [(0,0),(-1,100),(100,0)]:
            self.session.width,self.session.height=width,height
            self.assertFalse((await self.s._check_click_safety(self.session,1,1))[0])

    async def test_click_refreshes_bounds_before_validation(self):
        """A resized window must be checked using current rather than cached geometry."""
        async def resized(*args,**kwargs):
            """Model the target shrinking after its previous observation."""
            self.session.width=50
        self.s._refresh_window.side_effect=resized
        self.s._seamless_click=AsyncMock(return_value={'ok':True,'via':'skylight'})
        result=payload(await self.s.call_tool('click',{'app':'Fixture','x':75,'y':20}))
        self.assertFalse(result['ok'])
        self.s._seamless_click.assert_not_awaited()

    def test_failed_observations_are_not_success(self):
        """Warnings, string-valued blocks, and unknown outcomes cannot verify an action."""
        for data in [{'ok':False},{'blocked':'not_active_session'},{'ok':True,'focus_warning':{}},
                     {'requires_foreground':True},{'error':'failed'}]:
            with self.subTest(data=data):
                self.assertFalse(self.s._response_indicates_ok([types.TextContent(type='text',text=json.dumps(data))]))

    async def test_batch_failure_stops_following_input(self):
        """An ambiguous failed action must stop a dependent batch and report failure."""
        original=self.s.call_tool
        delivered=[]
        async def step(name,args):
            """Return ambiguity for the first step and track any unsafe later delivery."""
            delivered.append(name)
            data={'ok':False,'ambiguous':True} if name=='click_element' else {'ok':True}
            return [types.TextContent(type='text',text=json.dumps(data))]
        self.s.call_tool=step
        result=payload(await original('run',{'app':'Fixture','actions':[
            {'tool':'click_element','label':'Duplicate'},{'tool':'type_text','text':'should not type'}]}))
        self.assertFalse(result['ok'])
        self.assertEqual(delivered,['click_element'])
        self.assertEqual(result['skipped_steps'],1)

    async def test_batch_retains_explicit_window_each_step(self):
        """A successful prior step cannot silently retarget the next step to the largest window."""
        original=self.s.call_tool
        seen=[]
        async def step(name,args):
            """Record the window identity passed to each real nested dispatch boundary."""
            seen.append(args.get('window_id'))
            return [types.TextContent(type='text',text='{"ok":true}')]
        self.s.call_tool=step
        result=payload(await original('run',{'app':'Fixture','window_id':7,'actions':[
            {'tool':'press_key','key':'a'},{'tool':'press_key','key':'b'}]}))
        self.assertTrue(result['ok'])
        self.assertEqual(seen,[7,7])

    async def test_invalid_batch_step_stops_before_next_action(self):
        """Schema errors must not be followed by input predicated on a missing action."""
        original=self.s.call_tool
        self.s.call_tool=AsyncMock(return_value=[types.TextContent(type='text',text='{"ok":true}')])
        result=payload(await original('run',{'app':'Fixture','actions':[
            {'tool':'click'},{'tool':'type_text','text':'unsafe continuation'}]}))
        self.assertFalse(result['ok'])
        self.s.call_tool.assert_not_awaited()

    async def test_emergency_stop_blocks_ax_actions(self):
        """The dispatcher latch must also block AX paths that bypass synthesized input."""
        self.s.computer._check_stop.side_effect=RuntimeError('Emergency stop is ACTIVE')
        result=payload(await self.s.call_tool('ax_action',{'app':'Fixture','x':10,'y':10,'action':'AXPress'}))
        self.assertFalse(result['ok'])
        self.s._get_session.assert_not_awaited()

    async def test_verify_unavailable_is_explicit(self):
        """Missing evidence has a readable unavailable outcome rather than silently disappearing."""
        self.s.registry.get_by_app.return_value=None
        result=await self.s._post_action_verify('Fixture')
        self.assertIsInstance(result,dict)
        self.assertEqual(result['status'],'unavailable')

    def test_observation_after_action_has_no_batching_nudge(self):
        """Looking after an action is a valid verification step, not an anti-pattern."""
        self.s._call_history.append('click')
        self.assertIsNone(self.s._detect_hint('inspect',{'app':'Fixture'}))

    async def test_template_crop_allows_exclusive_region_edge(self):
        """A capture rectangle may end at width/height without becoming a click."""
        self.session.template_cache={}
        self.s._take_screenshot=AsyncMock(return_value=('pixels',100,100,None))
        self.s.matcher.crop.return_value='cropped pixels'
        result=payload(await self.s.call_tool('get_template',{'app':'Fixture','x1':0,'y1':0,'x2':100,'y2':100}))
        self.assertIn('template_id',result)
        self.s.matcher.crop.assert_called_once_with('pixels',0,0,100,100)

    async def test_background_does_not_fall_through_failed_delivery(self):
        """A broken private backend cannot turn a background action into visible input."""
        self.session.mode='background'
        self.s.skylight.is_available.return_value=False
        self.s.computer.click=AsyncMock()
        result=payload(await self.s.call_tool('click',{'app':'Fixture','x':10,'y':10}))
        self.assertTrue(result.get('requires_foreground'))
        self.s.computer.click.assert_not_awaited()

    async def test_failed_activation_stops_keyboard_delivery(self):
        """Do not send keys when the target cannot become the active application."""
        self.s._is_chromium_based=lambda session:True
        self.s.computer.is_frontmost_app.return_value=False
        self.s._await_frontmost=AsyncMock(return_value=False)
        with self.assertRaisesRegex(RuntimeError,'no keys were sent'):
            await self.s._ensure_key_delivery(self.session,'type_text',True)

    async def test_background_scroll_refuses_visible_fallback(self):
        """A missing private backend cannot scroll a different foreground window."""
        self.session.mode='background'
        self.s.skylight.is_available.return_value=False
        self.s.computer.scroll=AsyncMock()
        result=payload(await self.s.call_tool('scroll',{'app':'Fixture','x':20,'y':20,'direction':'down'}))
        self.assertTrue(result['requires_foreground'])
        self.s.computer.scroll.assert_not_awaited()

    async def test_fill_fallback_keeps_requested_window(self):
        """Activation cannot reset a fill to the app's default window."""
        self.s.computer.ax_set_value_at.return_value={'ok':False}
        self.s._await_frontmost=AsyncMock(return_value=True)
        self.s._focus_if_needed=AsyncMock(return_value=None)
        self.s.computer.click=AsyncMock();self.s.computer.press_key=AsyncMock();self.s.computer.type_text=AsyncMock()
        result=payload(await self.s.call_tool('fill_field',{'app':'Fixture','window_id':7,'x':20,'y':20,'text':'value'}))
        self.assertTrue(result['ok'])
        self.assertTrue(all(c.kwargs.get('window_id')==7 for c in self.s._refresh_window.call_args_list))

    async def test_batch_missing_tool_fails(self):
        """Malformed steps must not become successful no-ops."""
        result=payload(await self.s.call_tool('run',{'app':'Fixture','actions':[{'tool':''}]}))
        self.assertFalse(result['ok'])

    def test_all_tool_schemas_are_valid(self):
        """The complete specialist surface retains distinct names and valid schemas."""
        self.assertEqual(len(self.s.TOOLS),48)
        self.assertEqual(len(self.s._TOOL_SCHEMAS),48)
        for validator in self.s._TOOL_VALIDATORS.values(): validator.check_schema(validator.schema)
