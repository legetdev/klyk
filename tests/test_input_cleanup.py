"""Exercise native input cleanup with fake event delivery, never the real desktop."""
import ast
import asyncio
import ctypes
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


def load_functions(filename,names,namespace):
    """Compile unchanged functions with event bindings supplied by each test."""
    path=Path(__file__).resolve().parents[1]/'klyk'/filename
    tree=ast.parse(path.read_text());nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    tree=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree),str(path),'exec'),namespace)
    return namespace


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """Stopping or cancelling input must still release held mouse buttons."""

    def test_unmodified_keys_clear_inherited_flags(self):
        """A slash or shortcut must not shift later plain letters and punctuation."""
        cg=MagicMock();cg.CGEventCreateKeyboardEvent.side_effect=[1,2]
        ns={'ctypes':ctypes,'_cg':cg,'_post':lambda event:None,'time':SimpleNamespace(sleep=lambda _:None)}
        load_functions('computer.py',{'_press_key_sync'},ns)
        ns['_press_key_sync'](0,0)
        self.assertEqual([call.args[1] for call in cg.CGEventSetFlags.call_args_list],[0,0])

    async def test_visible_mouse_release_on_stop(self):
        """Long presses and drags both emit mouse-up when the latch interrupts them."""
        for name,args in [('long_press',(10,20,.2)),('drag',(10,20,30,40))]:
            with self.subTest(name=name):
                sent=[];cg=MagicMock();cg.CGEventCreateMouseEvent.side_effect=lambda source,kind,point,button:kind
                check=MagicMock(side_effect=[None,RuntimeError('stopped')])
                ns={'asyncio':asyncio,'time':__import__('time'),'_input_lock':asyncio.Lock(),'_check_stop':check,'_cg':cg,'_post':sent.append,
                    'CGPoint':lambda *a,**kw:SimpleNamespace(**kw),'kCGEventLeftMouseDown':1,'kCGEventLeftMouseUp':2,'kCGEventLeftMouseDragged':6,
                    'kCGEventRightMouseDown':3,'kCGEventRightMouseUp':4,'kCGMouseButtonLeft':0,'kCGMouseButtonRight':1}
                load_functions('computer.py',{name},ns)
                with self.assertRaisesRegex(RuntimeError,'stopped'): await ns[name](*args)
                self.assertEqual(sent,[1,2])

    async def test_visible_mouse_release_on_cancellation(self):
        """Cancelling an in-flight hold releases the button before the coroutine exits."""
        sent=[];cg=MagicMock();cg.CGEventCreateMouseEvent.side_effect=lambda source,kind,point,button:kind
        ns={'asyncio':asyncio,'_input_lock':asyncio.Lock(),'_check_stop':lambda:None,'_cg':cg,'_post':sent.append,
            'CGPoint':lambda *a,**kw:SimpleNamespace(**kw),'kCGEventLeftMouseDown':1,'kCGEventLeftMouseUp':2,'kCGMouseButtonLeft':0}
        load_functions('computer.py',{'long_press'},ns)
        task=asyncio.create_task(ns['long_press'](10,20,5));await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(sent,[1,2])

    def test_invisible_drag_release_on_stop(self):
        """The SkyLight drag loop checks the latch and still sends its final release."""
        sent=[];cg=MagicMock();cg.CGEventCreateMouseEvent.side_effect=lambda source,kind,point,button:kind
        ns={'_AVAILABLE':True,'_cg':cg,'CGPoint':lambda *a:None,'_button_event_types':lambda button:(1,2,6,0),
            '_stamp_mouse_event':lambda *a:None,'_post_event':lambda pid,event:sent.append(event),
            '_release':lambda event:None,'time':SimpleNamespace(sleep=lambda t:None)}
        load_functions('skylight.py',{'post_drag'},ns)
        check=MagicMock(side_effect=[None,RuntimeError('stopped')])
        with self.assertRaisesRegex(RuntimeError,'stopped'):ns['post_drag'](123,7,0,0,10,10,check_stop=check)
        self.assertEqual(sent,[1,2])

    def test_ax_target_rejects_other_process(self):
        """Global AX hit tests cannot authorize an action in an occluding app."""
        api=MagicMock()
        def pid(element,output):
            """Return the independently observed owner of the hit-tested element."""
            ctypes.cast(output,ctypes.POINTER(ctypes.c_int32))[0]=456
            return 0
        api.AXUIElementGetPid.side_effect=pid
        ns={'ctypes':ctypes,'_appserv':api}
        load_functions('computer.py',{'_ax_matches_pid'},ns)
        self.assertFalse(ns['_ax_matches_pid'](12,123))
        self.assertTrue(ns['_ax_matches_pid'](12,456))

    async def test_type_text_char_by_char_rechecks_stop_between_characters(self):
        """Stop a character sequence before posting the character after the latch fires."""
        posted=[]
        pressed=[]
        check=MagicMock(side_effect=[None,None,RuntimeError('stopped')])
        ns={
            'asyncio':asyncio,
            'ctypes':ctypes,
            'time':SimpleNamespace(sleep=lambda _:None),
            '_input_lock':asyncio.Lock(),
            '_check_stop':check,
            'char_to_keycode':lambda char:(ord(char),0),
            '_press_key_sync':lambda keycode,flags,pid:pressed.append((keycode,flags,pid)),
            '_post':posted.append,
            '_post_to_pid':posted.append,
            '_cg':MagicMock(),
        }
        load_functions('computer.py',{'type_text_char_by_char'},ns)

        with self.assertRaisesRegex(RuntimeError,'stopped'):
            await ns['type_text_char_by_char']('ab')

        self.assertEqual(pressed,[(ord('a'),0,None)])
        self.assertEqual(check.call_count,3)

    async def test_type_text_char_by_char_emits_full_utf16_surrogate_pair(self):
        """Send both UTF-16 code units for a non-BMP character on key down and up."""
        events=[]
        unicode_calls=[]
        cg=MagicMock()
        cg.CGEventCreateKeyboardEvent.side_effect=[101,102]

        def capture_unicode(event,count,pointer):
            """Capture the UTF-16 units passed to the CoreGraphics event."""
            units=(ctypes.c_uint16 * count).from_address(pointer.value)
            unicode_calls.append((event.value if hasattr(event,'value') else event,count,list(units)))

        cg.CGEventKeyboardSetUnicodeString.side_effect=capture_unicode
        ns={
            'asyncio':asyncio,
            'ctypes':ctypes,
            'time':SimpleNamespace(sleep=lambda _:None),
            '_input_lock':asyncio.Lock(),
            '_check_stop':lambda:None,
            'char_to_keycode':lambda char:(None,0),
            '_press_key_sync':MagicMock(),
            '_post':events.append,
            '_post_to_pid':events.append,
            '_cg':cg,
        }
        load_functions('computer.py',{'type_text_char_by_char'},ns)

        await ns['type_text_char_by_char']('😀')

        expected=[0xD83D,0xDE00]
        self.assertEqual(events,[101,102])
        self.assertEqual(unicode_calls,[(101,2,expected),(102,2,expected)])

    async def test_hold_key_releases_after_stop(self):
        """Release a held key when the emergency stop interrupts its hold loop."""
        events=[]
        check=MagicMock(side_effect=[None,RuntimeError('stopped')])
        ns={
            'asyncio':asyncio,
            '_input_lock':asyncio.Lock(),
            '_check_stop':check,
            'parse_key_combo':lambda key:(42,0),
            '_key_down_sync':lambda keycode,flags,pid:events.append(('down',keycode,flags,pid)),
            '_key_up_sync':lambda keycode,flags,pid:events.append(('up',keycode,flags,pid)),
        }
        load_functions('computer.py',{'hold_key'},ns)

        with self.assertRaisesRegex(RuntimeError,'stopped'):
            await ns['hold_key']('a',1.0)

        self.assertEqual(events,[('down',42,0,None),('up',42,0,None)])

    async def test_type_text_restores_clipboard_after_cancellation(self):
        """Restore the original clipboard when a paste coroutine is cancelled."""
        snapshot=['original clipboard']
        restored=[]
        started=asyncio.Event()

        async def block_sleep(delay):
            """Hold the paste at its first await so the test can cancel it."""
            started.set()
            await asyncio.Future()

        class Pasteboard:
            """Minimal AppKit pasteboard provider for the synchronous restore path."""

            @staticmethod
            def generalPasteboard():
                """Return the controlled pasteboard instance."""
                return pasteboard

        pasteboard=MagicMock()
        pasteboard.changeCount.return_value=7
        appkit=SimpleNamespace(NSPasteboard=Pasteboard)
        ns={
            'asyncio':SimpleNamespace(sleep=block_sleep),
            '_input_lock':asyncio.Lock(),
            '_check_stop':lambda:None,
            '_snapshot_pasteboard':lambda:snapshot,
            '_restore_pasteboard':lambda value:restored.append(value),
            '_paste_sync':lambda pid:None,
            'subprocess':SimpleNamespace(run=MagicMock()),
            '_clipboard_snapshot':None,
        }
        load_functions('computer.py',{'type_text'},ns)

        with patch.dict(sys.modules,{'AppKit':appkit}):
            task=asyncio.create_task(ns['type_text']('temporary'))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(restored,[snapshot])

    async def test_type_text_restores_clipboard_after_paste_failure(self):
        """Restore the original clipboard when paste delivery raises."""
        snapshot=['original clipboard']
        restored=[]

        async def no_sleep(delay):
            """Skip timing delays while exercising the failure cleanup path."""

        class Pasteboard:
            """Minimal AppKit pasteboard provider for a failed paste."""

            @staticmethod
            def generalPasteboard():
                """Return the controlled pasteboard instance."""
                return pasteboard

        pasteboard=MagicMock()
        pasteboard.changeCount.return_value=7
        appkit=SimpleNamespace(NSPasteboard=Pasteboard)
        ns={
            'asyncio':SimpleNamespace(sleep=no_sleep),
            '_input_lock':asyncio.Lock(),
            '_check_stop':lambda:None,
            '_snapshot_pasteboard':lambda:snapshot,
            '_restore_pasteboard':lambda value:restored.append(value),
            '_paste_sync':MagicMock(side_effect=RuntimeError('paste failed')),
            'subprocess':SimpleNamespace(run=MagicMock()),
            '_clipboard_snapshot':None,
        }
        load_functions('computer.py',{'type_text'},ns)

        with patch.dict(sys.modules,{'AppKit':appkit}):
            with self.assertRaisesRegex(RuntimeError,'paste failed'):
                await ns['type_text']('temporary')

        self.assertEqual(restored,[snapshot])

    async def test_type_text_does_not_overwrite_newer_clipboard(self):
        """Leave a clipboard change made during paste cleanup untouched."""
        snapshot=['original clipboard']
        restored=[]

        async def no_sleep(delay):
            """Skip timing delays while exercising change-count preservation."""

        class Pasteboard:
            """Minimal AppKit pasteboard provider with a changed clipboard count."""

            @staticmethod
            def generalPasteboard():
                """Return the controlled pasteboard instance."""
                return pasteboard

        pasteboard=MagicMock()
        pasteboard.changeCount.side_effect=[7,8,9]
        appkit=SimpleNamespace(NSPasteboard=Pasteboard)
        ns={
            'asyncio':SimpleNamespace(sleep=no_sleep),
            '_input_lock':asyncio.Lock(),
            '_check_stop':lambda:None,
            '_snapshot_pasteboard':lambda:snapshot,
            '_restore_pasteboard':lambda value:restored.append(value),
            '_paste_sync':lambda pid:None,
            'subprocess':SimpleNamespace(run=MagicMock()),
            '_clipboard_snapshot':None,
        }
        load_functions('computer.py',{'type_text'},ns)

        with patch.dict(sys.modules,{'AppKit':appkit}):
            await ns['type_text']('temporary')

        self.assertEqual(restored,[])
