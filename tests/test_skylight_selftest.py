"""Verify delivery-test lifecycle with fake AppKit objects and the real access gate."""

import asyncio
import logging
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

from klyk import connection_gate as gate, connection_policy as policy
from klyk.ui_thread import UIThread
from test_connection_gate import IsolatedPolicy
from test_input_cleanup import load_functions


class Allocatable:
    """Support the small Objective-C allocation surface without loading AppKit."""

    @classmethod
    def alloc(cls):
        """Allocate only a Python instance."""
        return cls()

    def init(self):
        """Return the synthetic initialized object."""
        return self

    def initWithFrame_(self, frame):
        """Keep the inert frame for the fake sink view."""
        self.frame = frame
        return self


class Timer:
    """Retain an exact target callback and expose explicit inert firing."""

    def __init__(self, interval, target, selector):
        """Record the timer without scheduling any operating-system callback."""
        self.interval, self.target, self.selector = interval, target, selector
        self.invalidate = MagicMock()

    def fire(self):
        """Model an AppKit callback with no inherited request context."""
        with gate.scope(None):
            getattr(self.target, self.selector.replace(':', '_'))(self)


class SelfTestHarness:
    """Use the actual UI queue and source helpers with entirely synthetic native objects."""

    def __init__(self, testcase, *, running=True, delivered=True):
        """Build native spies; neither NSApp nor a native window is created."""
        self.testcase, self.delivered = testcase, delivered
        self.windows, self.timers, self.post_scopes = [], [], []
        self.ui = UIThread()
        self.ui._available = True
        self.ui.install_on_main_thread = MagicMock(return_value=True)
        self.app = MagicMock()
        self.app.isRunning.return_value = running

        def window():
            """Allocate a Python window spy with a unique retained-window identity."""
            value = MagicMock()
            value.initWithContentRect_styleMask_backing_defer_.return_value = value
            value.windowNumber.return_value = 40 + len(self.windows)
            value.setContentView_.side_effect = lambda sink: setattr(value, 'sink', sink)
            value.close.side_effect = lambda: testcase.assertIs(threading.current_thread(), threading.main_thread())
            self.windows.append(value)
            return value

        def timer(interval, target, selector, user_info, repeats):
            """Create only a retained Python timer and reject repeating scheduling."""
            testcase.assertFalse(repeats)
            value = Timer(interval, target, selector)
            self.timers.append(value)
            return value

        self.appkit = SimpleNamespace(NSView=Allocatable, NSWindow=SimpleNamespace(alloc=window),
            NSApplication=SimpleNamespace(sharedApplication=lambda: self.app),
            NSBackingStoreBuffered=2, NSMakeRect=lambda *args: args,
            NSEvent=MagicMock(), NSMakePoint=lambda *args: args, NSEventTypeApplicationDefined=15)
        self.foundation = SimpleNamespace(NSObject=Allocatable,
            NSTimer=SimpleNamespace(scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=timer))

        def post(pid, window_id, x, y):
            """Observe a fake delivery only in the retained own-window sink."""
            self.post_scopes.append(gate.current_scope())
            target = next(value for value in self.windows if value.windowNumber.return_value == window_id)
            testcase.assertIs(gate.current_scope(), target.sink._klyk_selftest['request'])
            if self.delivered:
                target.sink.mouseDown_(None)
            return True

        self.namespace = {'__package__': 'klyk', '_connection_gate': gate, '_AVAILABLE': True,
            '_DELIVERY_VERIFIED': None, '_SINK_CLASS': None, '_DRIVER_CLASS': None,
            'threading': threading, 'asyncio': asyncio, 'time': time,
            'log': logging.getLogger('inert-selftest'), 'post_mouse_click': MagicMock(side_effect=post)}
        load_functions('skylight.py', {'self_test', 'self_test_async'}, self.namespace)

    def modules(self):
        """Substitute only this test's fake AppKit/Foundation and Python UI queue."""
        return patch.dict(sys.modules, {'AppKit': self.appkit, 'Foundation': self.foundation,
                                       'klyk.ui_thread': SimpleNamespace(ui=self.ui)})

    def drain(self):
        """Service the actual Python UI queue on the main thread without a native loop."""
        self.testcase.assertIs(threading.current_thread(), threading.main_thread())
        while not self.ui._queue.empty():
            self.ui._queue.get_nowait()()

    async def prepared(self):
        """Wait briefly for the worker to enqueue and the main thread to prepare its sink."""
        deadline = time.monotonic() + 1
        while not self.timers and time.monotonic() < deadline:
            self.drain()
            await asyncio.sleep(0.001)
        self.testcase.assertEqual(len(self.timers), 2)

    async def complete(self, task, *, fire=True):
        """Drive only queued Python callbacks and explicitly selected fake timers."""
        deadline = time.monotonic() + 1.5
        fired = False
        while not task.done() and time.monotonic() < deadline:
            self.drain()
            if fire and self.timers and not fired:
                fired = True
                self.timers[0].fire()
                self.timers[1].fire()
            await asyncio.sleep(0.001)
        self.drain()
        self.testcase.assertTrue(task.done())
        return await task


class SelfTestLifecycleTests(IsolatedPolicy, unittest.IsolatedAsyncioTestCase):
    """A running AppKit loop stays alive through verification, timeout and revocation."""

    async def asyncSetUp(self):
        """Propagate real request generations through the actual gate executor."""
        asyncio.get_running_loop().set_default_executor(gate.GateExecutor(max_workers=2))

    async def test_existing_loop_verifies_delivery_without_run_stop_or_wake_event(self):
        """Main-thread preparation plus the retained timer request preserves the outer loop."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            self.assertTrue(await harness.complete(task))
        self.assertTrue(harness.namespace['_DELIVERY_VERIFIED'])
        self.assertEqual(harness.post_scopes, [request])
        harness.app.run.assert_not_called()
        harness.app.stop_.assert_not_called()
        harness.app.postEvent_atStart_.assert_not_called()
        harness.windows[0].close.assert_called_once()
        for timer in harness.timers:
            timer.invalidate.assert_called_once()

    async def test_retained_window_disables_automatic_release_before_it_is_ordered(self):
        """A Python-retained NSWindow must not also release itself on close."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            self.assertTrue(await harness.complete(task))
        window = harness.windows[0]
        window.setReleasedWhenClosed_.assert_called_once_with(False)
        names = [call[0] for call in window.method_calls]
        self.assertLess(names.index('setReleasedWhenClosed_'), names.index('orderFrontRegardless'))
        self.assertLess(names.index('setReleasedWhenClosed_'), names.index('close'))
        window.close.assert_called_once()
        harness.app.stop_.assert_not_called()

    async def test_completed_no_hit_records_false_but_incomplete_timeout_stays_unknown(self):
        """An observed finish distinguishes dropped delivery from an unprocessed timer."""
        for fire, expected in ((True, False), (False, None)):
            with self.subTest(fire=fire):
                request, harness = self.enable(), SelfTestHarness(self, delivered=False)
                with harness.modules(), gate.scope(request):
                    task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
                    self.assertFalse(await harness.complete(task, fire=fire))
                self.assertIs(harness.namespace['_DELIVERY_VERIFIED'], expected)
                harness.windows[0].close.assert_called_once()
                harness.app.run.assert_not_called()
                harness.app.stop_.assert_not_called()

    async def test_cancelled_gate_executor_request_closes_only_its_main_thread_sink(self):
        """Cleanup bypasses only its own queue item, never a revoked executor job."""
        request, harness = self.enable(), SelfTestHarness(self)
        harness.namespace['_DELIVERY_VERIFIED'] = True
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            await harness.prepared()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await harness.complete(task, fire=False)
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.namespace['post_mouse_click'].assert_not_called()
        harness.windows[0].close.assert_called_once()
        harness.app.stop_.assert_not_called()

    async def test_cancel_before_main_thread_preparation_prevents_a_late_sink(self):
        """A cancelled executor await cannot leave an old UI callback creating a window."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            deadline = time.monotonic() + 1
            while harness.ui._queue.empty() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertFalse(harness.ui._queue.empty())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            harness.drain()  # The worker's real queued callback runs only after cancellation.
            await asyncio.sleep(0.01)
        self.assertEqual(harness.windows, [])
        self.assertEqual(harness.timers, [])
        harness.namespace['post_mouse_click'].assert_not_called()
        harness.app.run.assert_not_called()
        harness.app.stop_.assert_not_called()
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])

    async def test_cancel_during_main_thread_preparation_closes_the_late_owned_window(self):
        """A separate asyncio worker can cancel while the actual main-thread callback is busy."""
        request, harness = self.enable(), SelfTestHarness(self)
        ready, observed = threading.Event(), threading.Event()
        bridge, errors = {}, []
        allocate = harness.appkit.NSWindow.alloc

        def interrupted_allocation():
            """Pause only an inert allocation until the worker's cancelled finally has run."""
            window = allocate()
            bridge['loop'].call_soon_threadsafe(bridge['task'].cancel)
            self.assertTrue(observed.wait(1))
            return window

        async def verify():
            """Run the production async path through its real generation-aware executor."""
            asyncio.get_running_loop().set_default_executor(gate.GateExecutor(max_workers=1))
            with gate.scope(request):
                bridge['loop'] = asyncio.get_running_loop()
                bridge['task'] = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
                ready.set()
                try:
                    await bridge['task']
                    errors.append('Cancellation was lost.')
                except asyncio.CancelledError:
                    observed.set()

        def worker():
            """Keep the real asyncio/executor lifecycle isolated from the fake UI main thread."""
            try:
                asyncio.run(verify())
            except BaseException as error:
                errors.append(error)

        harness.appkit.NSWindow.alloc = interrupted_allocation
        thread = threading.Thread(target=worker, name='inert-selftest-worker', daemon=True)
        with harness.modules():
            thread.start()
            deadline = time.monotonic() + 1.5
            while (not ready.is_set() or harness.ui._queue.empty()) and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertTrue(ready.is_set())
            self.assertFalse(harness.ui._queue.empty())
            harness.drain()
            while thread.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            thread.join(timeout=0.1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(observed.is_set())
        self.assertEqual(len(harness.windows), 1)
        harness.windows[0].setReleasedWhenClosed_.assert_called_once_with(False)
        harness.windows[0].close.assert_called_once()
        harness.windows[0].orderFrontRegardless.assert_not_called()
        self.assertEqual(harness.timers, [])
        harness.namespace['post_mouse_click'].assert_not_called()
        harness.app.run.assert_not_called()
        harness.app.stop_.assert_not_called()
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])

    async def test_off_during_allocation_configures_safe_owned_cleanup_before_refusal(self):
        """A just-returned native allocation still needs safe lifetime configuration when revoked."""
        request, harness = self.enable(), SelfTestHarness(self)
        allocate = harness.appkit.NSWindow.alloc

        def revoke_during_allocation():
            """Model a native allocation completing after its generation was invalidated."""
            window = allocate()
            self.cycle()
            return window

        harness.appkit.NSWindow.alloc = revoke_during_allocation
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            with self.assertRaises(policy.AccessDisabled):
                await harness.complete(task, fire=False)
        self.assertEqual(len(harness.windows), 1)
        harness.windows[0].setReleasedWhenClosed_.assert_called_once_with(False)
        harness.windows[0].close.assert_called_once()
        harness.windows[0].orderFrontRegardless.assert_not_called()
        self.assertEqual(harness.timers, [])
        harness.namespace['post_mouse_click'].assert_not_called()
        harness.app.stop_.assert_not_called()
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])

    async def test_repeated_cancel_during_successful_cleanup_preserves_cancellation(self):
        """Cancellation while closing an observed hit cannot turn into successful completion."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            await harness.prepared()
            harness.timers[0].fire()
            deadline = time.monotonic() + 1
            while harness.ui._queue.empty() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertFalse(harness.ui._queue.empty())
            self.assertTrue(harness.namespace['_DELIVERY_VERIFIED'])
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            harness.drain()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.windows[0].close.assert_called_once()
        harness.app.stop_.assert_not_called()
        for timer in harness.timers:
            timer.invalidate.assert_called_once()

    async def test_off_on_revokes_callbacks_and_still_cleans_up_under_gate_executor(self):
        """Old timers/sinks cannot post, claim a hit or stop a fresh verification."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            old_task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            await harness.prepared()
            old_sink, old_driver = harness.windows[0].sink, harness.timers[0].target
            self.cycle()
            with self.assertRaises(policy.AccessDisabled):
                await harness.complete(old_task, fire=False)
            self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        old_driver.post_(None)
        old_sink.mouseDown_(None)
        harness.namespace['post_mouse_click'].assert_not_called()
        old_driver.finish_(None)
        harness.app.stop_.assert_not_called()
        harness.windows[0].close.assert_called_once()

        harness.timers.clear()
        with harness.modules(), gate.scope(gate.capture_scope()):
            fresh = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            await harness.prepared()
            old_driver.post_(None)
            old_sink.mouseDown_(None)
            old_driver.finish_(None)
            self.assertFalse(harness.windows[1].sink._klyk_selftest['hit'])
            self.assertTrue(await harness.complete(fresh))
        self.assertTrue(harness.namespace['_DELIVERY_VERIFIED'])
        harness.windows[1].close.assert_called_once()
        harness.app.stop_.assert_not_called()

    def test_standalone_refuses_a_running_loop_before_creating_any_sink(self):
        """The synchronous doctor path never nests an already running application loop."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            self.assertFalse(harness.namespace['self_test'](timeout=0.2))
        self.assertEqual(harness.windows, [])
        self.assertEqual(harness.timers, [])
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.app.run.assert_not_called()
        harness.app.stop_.assert_not_called()

    def test_standalone_keeps_its_owned_loop_delivery_contract(self):
        """Without an active loop the doctor still receives a real-mode completion verdict."""
        request, harness = self.enable(), SelfTestHarness(self, running=False)
        harness.app.run.side_effect = lambda: [timer.fire() for timer in list(harness.timers)]
        with harness.modules(), gate.scope(request):
            self.assertTrue(harness.namespace['self_test'](timeout=0.2))
        harness.app.run.assert_called_once()
        harness.app.stop_.assert_called_once()
        harness.windows[0].close.assert_called_once()

    async def test_no_owned_window_identity_does_not_search_or_post_elsewhere(self):
        """A zero native window number produces no fake input or global fallback."""
        request, harness = self.enable(), SelfTestHarness(self)
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            await harness.prepared()
            harness.windows[0].windowNumber.return_value = 0
            self.assertFalse(await harness.complete(task))
        harness.namespace['post_mouse_click'].assert_not_called()
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.windows[0].close.assert_called_once()

    async def test_refused_post_is_inconclusive_and_cannot_overwrite_a_physical_stop(self):
        """A fake safety refusal before delivery is not evidence that SkyLight dropped a click."""
        request, harness = self.enable(), SelfTestHarness(self)
        harness.namespace['post_mouse_click'].side_effect = RuntimeError('inert physical stop')
        with harness.modules(), gate.scope(request):
            task = asyncio.create_task(harness.namespace['self_test_async'](timeout=0.2))
            self.assertFalse(await harness.complete(task))
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.windows[0].close.assert_called_once()
        harness.app.stop_.assert_not_called()

    def test_off_does_not_import_native_harness_or_prepare_ui(self):
        """The request gate precedes native setup even on the standalone entry point."""
        harness = SelfTestHarness(self)
        with harness.modules(), self.assertRaises(policy.AccessDisabled):
            harness.namespace['self_test']()
        harness.ui.install_on_main_thread.assert_not_called()
        self.assertEqual(harness.windows, [])

    async def test_async_off_clears_a_stale_verdict_before_any_preparation(self):
        """Initial revocation cannot leave a prior hit reported as this run's outcome."""
        harness = SelfTestHarness(self)
        harness.namespace['_DELIVERY_VERIFIED'] = True
        with harness.modules(), self.assertRaises(policy.AccessDisabled):
            await harness.namespace['self_test_async']()
        self.assertIsNone(harness.namespace['_DELIVERY_VERIFIED'])
        harness.ui.install_on_main_thread.assert_not_called()
        self.assertEqual(harness.windows, [])

    async def test_runtime_main_thread_finish_follows_awaited_verification(self):
        """The actual ensure function finishes ownership/listening only after delivery completes."""
        request, harness = self.enable(), SelfTestHarness(self)
        events = []
        namespace = {'asyncio': asyncio, 'connection_gate': gate, '_ui': harness.ui,
                     '_native_init_lock': asyncio.Lock(), '_native_initialized': False,
                     'computer': SimpleNamespace(_start_emergency_stop_tap=MagicMock()),
                     'log': MagicMock()}

        def initialize():
            """Record only an inert main-thread preflight."""
            self.assertIs(threading.current_thread(), threading.main_thread())
            events.append('load')

        def finish():
            """Readiness follows the completed async verification on the main thread."""
            self.assertIs(threading.current_thread(), threading.main_thread())
            self.assertEqual(events, ['load', 'verify'])
            events.append('finish')
            namespace['_native_initialized'] = True

        async def verify(timeout):
            """Model completed delivery without entering or stopping an AppKit loop."""
            self.assertEqual(events, ['load'])
            await asyncio.sleep(0)
            events.append('verify')
            return True

        namespace.update(_initialize_native_runtime=initialize, _finish_native_initialization=finish,
                         skylight=SimpleNamespace(is_available=lambda: True, self_test_async=verify,
                                                 delivery_verified=lambda: True))
        load_functions('mcp_server.py', {'_ensure_native_runtime'}, namespace)
        with gate.scope(request):
            task = asyncio.create_task(namespace['_ensure_native_runtime']())
            await harness.complete(task, fire=False)
            await namespace['_ensure_native_runtime']()
        self.assertEqual(events, ['load', 'verify', 'finish'])
        self.assertTrue(namespace['_native_initialized'])
        self.assertEqual(namespace['computer']._start_emergency_stop_tap.call_count, 2)

    async def test_runtime_revocation_during_verification_prevents_main_thread_finish(self):
        """A stale self-test result cannot claim ownership or start a listener after Off/On."""
        request, harness = self.enable(), SelfTestHarness(self)
        namespace = {'asyncio': asyncio, 'connection_gate': gate, '_ui': harness.ui,
                     '_native_init_lock': asyncio.Lock(), '_native_initialized': False,
                     'computer': SimpleNamespace(_start_emergency_stop_tap=MagicMock()),
                     'log': MagicMock(), '_initialize_native_runtime': MagicMock(),
                     '_finish_native_initialization': MagicMock()}

        async def verify(timeout):
            """Return an inert late hit after revoking its original generation."""
            self.cycle()
            return True

        namespace['skylight'] = SimpleNamespace(is_available=lambda: True, self_test_async=verify,
                                               delivery_verified=lambda: True)
        load_functions('mcp_server.py', {'_ensure_native_runtime'}, namespace)
        with gate.scope(request):
            task = asyncio.create_task(namespace['_ensure_native_runtime']())
            with self.assertRaises(policy.AccessDisabled):
                await harness.complete(task, fire=False)
        namespace['_initialize_native_runtime'].assert_called_once()
        namespace['_finish_native_initialization'].assert_not_called()
        namespace['computer']._start_emergency_stop_tap.assert_not_called()
        self.assertFalse(namespace['_native_initialized'])


if __name__ == '__main__':
    unittest.main()
