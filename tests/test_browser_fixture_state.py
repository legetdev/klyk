"""Verify independent browser event evidence without a browser or native APIs."""
import threading
import unittest
from desktop_smoke import update_browser_state


class BrowserFixtureStateTests(unittest.TestCase):
    """Prevent delayed HTTP and malformed input from changing accepted browser evidence."""

    def setUp(self):
        """Give concurrent fixture events one shared standard-library lock."""
        self.lock=threading.Lock()

    def update(self,state,payload):
        """Exercise the same locked state function used by the live HTTP handler."""
        return update_browser_state(state,payload,self.lock)

    def event(self,sequence,text,paste_text=None):
        """Create a bounded disposable event with an independent sequence number."""
        return dict(sequence=sequence,count=1,text=text,paste_events=1 if paste_text is not None else 0,paste_text=paste_text)

    def test_out_of_order_events_keep_latest_real_browser_state(self):
        """A late pre-paste request cannot replace the final pasted document value."""
        state={}
        for event in (self.event(1,'Browser input'),self.event(3,'Browser paste','Browser paste'),self.event(2,'','Browser paste')):
            self.assertTrue(self.update(state,event))
        self.assertEqual(state['text'],'Browser paste')
        self.assertEqual(state['paste_text'],'Browser paste')
        self.assertEqual(state['sequence'],3)

    def test_duplicate_event_cannot_change_previous_evidence(self):
        """Conflicting duplicate event numbers retain the first accepted observation."""
        state={};self.assertTrue(self.update(state,self.event(2,'Browser paste','Browser paste')))
        before=dict(state)
        self.assertTrue(self.update(state,self.event(2,'unexpected')))
        self.assertEqual(state,before)

    def test_malformed_events_do_not_change_state(self):
        """Numeric coercion, oversized fields and wrong data types fail closed."""
        state={};self.assertTrue(self.update(state,self.event(1,'baseline')))
        before=dict(state)
        invalid=[None,[],{},self.event(True,'text'),self.event(-1,'text'),self.event(1_000_001,'text'),self.event(2,'x'*1025),self.event(2,'text','x'*1025)]
        for key,value in (('count',float('nan')),('count',True),('paste_events',float('inf')),('paste_text',[]),('text',1)):
            event=self.event(2,'text');event[key]=value;invalid.append(event)
        for payload in invalid:
            with self.subTest(payload=type(payload).__name__):
                self.assertFalse(self.update(state,payload))
                self.assertEqual(state,before)


    def test_concurrent_handler_events_keep_newest_state(self):
        """A delayed update cannot overtake a newer concurrent browser event."""
        paused=threading.Event();resume=threading.Event();newer_arrived=threading.Event();failures=[]

        class PausedState(dict):
            """Pause the older event at its mutation boundary while a newer event arrives."""

            def update(inner,**values):
                """Hold the older event long enough to expose an unlocked compare/update race."""
                if values['sequence']==2:
                    paused.set()
                    if not resume.wait(2):
                        raise TimeoutError('Owned concurrent fixture test did not resume')
                super().update(**values)

        class ObservedLock:
            """Expose a newer event waiting on the real mutex without sleeps or scheduler assumptions."""

            def __init__(inner):
                """Create the same standard lock used by the actual handler."""
                inner.mutex=threading.Lock()

            def __enter__(inner):
                """Signal a newer lock attempt before blocking behind the older event."""
                if threading.current_thread() is newer:newer_arrived.set()
                return inner.mutex.__enter__()

            def __exit__(inner,*args):
                """Release the standard lock even if an owned event fails."""
                return inner.mutex.__exit__(*args)

        self.lock=ObservedLock()
        state=PausedState(sequence=1,count=1,text='baseline',paste_events=0,paste_text=None)

        def deliver(sequence,text):
            """Record exceptions from bounded test-owned handler threads."""
            try:
                self.update(state,self.event(sequence,text))
                if sequence==3:newer_arrived.set()
            except BaseException as error:
                failures.append(error)

        older=threading.Thread(target=deliver,args=(2,'older'),daemon=True)
        newer=threading.Thread(target=deliver,args=(3,'newer'),daemon=True)
        older.start()
        try:
            self.assertTrue(paused.wait(2))
            newer.start();self.assertTrue(newer_arrived.wait(2))
        finally:
            resume.set();older.join(2)
            if newer.ident is not None:newer.join(2)
        self.assertFalse(older.is_alive());self.assertFalse(newer.is_alive())
        self.assertEqual(failures,[])
        self.assertEqual((state['sequence'],state['text']),(3,'newer'))


if __name__=='__main__':unittest.main()
