"""Exercise access revocation with isolated files and inert native functions only."""

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from klyk import connection_gate as gate, connection_policy as policy
from klyk.ui_thread import UIThread
from klyk import window_capture
from test_input_cleanup import load_functions


class IsolatedPolicy(unittest.TestCase):
    """Use a private temporary switch; never inspect the owner's configuration."""

    def setUp(self):
        """Keep all policy reads/writes and all client identity inside the test."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'connections.json'
        self.path_patch = patch.object(policy, 'policy_path', return_value=self.path)
        self.client_patch = patch.object(policy, 'current_client', return_value='codex')
        self.path_patch.start()
        self.client_patch.start()
        gate._responses.clear()

    def tearDown(self):
        """Release only test resources after every worker has completed."""
        gate._responses.clear()
        self.client_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def enable(self):
        """Return a real captured generation for this isolated environment."""
        policy.set_enabled('codex', True)
        return gate.capture_scope()

    def cycle(self):
        """Leave access On while permanently revoking every preceding generation."""
        policy.set_enabled('codex', False)
        policy.set_enabled('codex', True)


class NativeGateTests(IsolatedPolicy):
    """Native spies prove refusal before any desktop-facing function is invoked."""

    def test_missing_and_malformed_state_never_calls_native(self):
        """Default-Off includes a damaged policy file without exposing its contents."""
        native = MagicMock()
        library = gate.protect_library(SimpleNamespace(read=native))
        for raw in (None, b'broken private policy'):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.path.write_bytes(raw)
                    self.path.chmod(0o600)
                with self.assertRaises(policy.AccessDisabled):
                    library.read()
        native.assert_not_called()

    def test_native_function_metadata_is_forwarded(self):
        """ctypes prototypes reach the underlying function, including while Off."""
        native = MagicMock()
        library = gate.protect_library(SimpleNamespace(read=native, _handle=123))
        library.read.argtypes = ['exact argument']
        library.read.restype = 'exact result'
        self.assertEqual(native.argtypes, ['exact argument'])
        self.assertEqual(native.restype, 'exact result')
        self.assertEqual(library._handle, 123)
        native.assert_not_called()

    def test_old_scope_cannot_resume_after_off_on(self):
        """Re-enabling restores only new requests, not an old callback."""
        old = self.enable()
        native = MagicMock(return_value='fresh')
        library = gate.protect_library(SimpleNamespace(read=native))
        with gate.scope(old):
            self.assertEqual(library.read(), 'fresh')
            self.cycle()
            with self.assertRaises(policy.AccessDisabled):
                library.read()
        with gate.scope(gate.capture_scope()):
            self.assertEqual(library.read(), 'fresh')
        self.assertEqual(native.call_count, 2)

    def test_queued_worker_is_rejected_without_running(self):
        """An executor backlog retains the generation from enqueue time."""
        request = self.enable()
        entered, release = threading.Event(), threading.Event()
        queued = MagicMock()

        def blocker():
            """Hold one inert thread until the second job has been revoked."""
            entered.set()
            release.wait(1)
            return 'late first result'

        with gate.GateExecutor(max_workers=1) as executor:
            with gate.scope(request):
                first = executor.submit(blocker)
                self.assertTrue(entered.wait(1))
                second = executor.submit(queued)
                self.cycle()
            release.set()
            for future in (first, second):
                with self.assertRaises(policy.AccessDisabled):
                    future.result(timeout=1)
        queued.assert_not_called()

    def test_inflight_worker_cannot_begin_next_native_stage(self):
        """A previously started fake OS read may finish, but cannot start another."""
        request = self.enable()
        entered, release = threading.Event(), threading.Event()
        next_read = MagicMock()
        library = gate.protect_library(SimpleNamespace(read=next_read))

        def worker():
            """Model a blocking native result followed by another AX/capture stage."""
            entered.set()
            release.wait(1)
            return library.read()

        with gate.GateExecutor(max_workers=1) as executor:
            with gate.scope(request):
                future = executor.submit(worker)
            self.assertTrue(entered.wait(1))
            self.cycle()
            release.set()
            with self.assertRaises(policy.AccessDisabled):
                future.result(timeout=1)
        next_read.assert_not_called()

    def test_screen_capture_completion_cannot_start_second_capture(self):
        """The real completion helper rejects revocation after shareable-content lookup."""
        request = self.enable()
        second_stage = MagicMock()

        def first_stage(receive):
            """Return a synthetic native object and revoke before completion is consumed."""
            receive(object(), None)
            self.cycle()

        with gate.scope(request), self.assertRaises(policy.AccessDisabled):
            window_capture._complete(first_stage, time.monotonic() + 1)
            window_capture._complete(second_stage, time.monotonic() + 1)
        second_stage.assert_not_called()

    def test_revoked_held_input_cleanup_preserves_new_generation_and_latch(self):
        """Off releases old inputs once without releasing a fresh request or physical pause."""
        old = self.enable()
        events = []
        namespace = {'_connection_gate': gate, '_stop_lock': threading.RLock(),
                     '_stop_engaged': [True], '_check_stop': gate.checkpoint}
        load_functions('computer.py', {'_begin_input', '_finish_input', 'release_held_input'}, namespace)
        with gate.scope(old):
            namespace['_begin_input'](('old',), lambda: events.append('old down'),
                                      lambda: events.append('old up'))
        self.cycle()
        fresh = gate.capture_scope()
        with gate.scope(fresh):
            namespace['_begin_input'](('fresh',), lambda: events.append('fresh down'),
                                      lambda: events.append('fresh up'))
        namespace['release_held_input'](engage_stop=False, revoked_only=True)
        namespace['release_held_input'](engage_stop=False, revoked_only=True)
        self.assertEqual(events, ['old down', 'fresh down', 'old up'])
        self.assertTrue(namespace['_stop_engaged'][0])
        self.assertIn(('fresh',), namespace['_held_inputs'])
        namespace['_finish_input'](('fresh',))
        self.assertEqual(events[-1], 'fresh up')

    def test_cleanup_cannot_start_a_new_input_while_off(self):
        """The cleanup allowance is below the new-input stop checkpoint."""
        request = self.enable()
        policy.set_enabled('codex', False)
        namespace = {'_connection_gate': gate, '_check_stop': gate.checkpoint}
        load_functions('computer.py', {'_begin_input'}, namespace)
        down, up = MagicMock(), MagicMock()
        with gate.scope(request), gate.cleanup(), self.assertRaises(policy.AccessDisabled):
            namespace['_begin_input'](('new',), down, up)
        down.assert_not_called()
        up.assert_not_called()

    def test_capture_process_refuses_revocation_before_spawn_and_after_result(self):
        """The real fallback helper never launches its next conversion after Off."""
        request = self.enable()
        native = MagicMock(side_effect=lambda *args, **kwargs: (self.cycle(), 'pixels')[1])
        namespace = {'_connection_gate': gate, 'subprocess': SimpleNamespace(run=native)}
        load_functions('capture.py', {'_run_process'}, namespace)
        with gate.scope(request), self.assertRaises(policy.AccessDisabled):
            namespace['_run_process']('fake capture')
        with gate.scope(request), self.assertRaises(policy.AccessDisabled):
            namespace['_run_process']('fake conversion')
        self.assertEqual(native.call_count, 1)

    def test_physical_stop_remains_engaged_after_off_on(self):
        """The actual stop checkpoint keeps the user's latch independent of access."""
        self.enable()
        namespace = {'_connection_gate': gate, '_worker_state': threading.local(),
                     '_stop_lock': threading.RLock(), '_stop_engaged': [True],
                     'EmergencyStop': RuntimeError}
        load_functions('computer.py', {'_check_stop'}, namespace)
        self.cycle()
        with gate.scope(gate.capture_scope()), self.assertRaisesRegex(RuntimeError, 'Emergency stop'):
            namespace['_check_stop']()
        self.assertTrue(namespace['_stop_engaged'][0])

    def test_clipboard_snapshot_stops_before_the_next_type_read(self):
        """Revoking one fake pasteboard read prevents reading the following type."""
        request = self.enable()
        item = MagicMock()
        item.types.return_value = ['plain', 'image']
        item.dataForType_.side_effect = lambda value: (self.cycle(), b'private fake value')[1]
        pasteboard = MagicMock()
        pasteboard.changeCount.return_value = 7
        pasteboard.pasteboardItems.return_value = [item]
        appkit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=MagicMock(return_value=pasteboard)),
                                 NSPasteboardItem=MagicMock())
        namespace = {'_connection_gate': gate}
        load_functions('computer.py', {'_snapshot_pasteboard'}, namespace)
        with patch.dict(sys.modules, {'AppKit': appkit}), gate.scope(request):
            with self.assertRaises(policy.AccessDisabled):
                namespace['_snapshot_pasteboard']()
            with self.assertRaises(policy.AccessDisabled):
                namespace['_snapshot_pasteboard']()
        item.dataForType_.assert_called_once_with('plain')
        appkit.NSPasteboard.generalPasteboard.assert_called_once()

    def test_mid_pbcopy_revocation_restores_only_its_borrowed_clipboard(self):
        """A fake pbcopy finishes after Off; its count is learned for cleanup, never paste."""
        request = self.enable()
        for newer_copy in (False, True):
            with self.subTest(newer_copy=newer_copy):
                if newer_copy:
                    request = gate.capture_scope()
                count, restored = [7], []
                pasteboard = MagicMock()
                pasteboard.changeCount.side_effect = lambda: count[0]
                appkit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: pasteboard))

                def pbcopy(*args, **kwargs):
                    """Change only synthetic clipboard state, then revoke its request."""
                    count[0] = 8
                    self.cycle()

                async def sleep(delay):
                    """Model a subsequent user copy without waiting or native operations."""
                    if newer_copy:
                        count[0] = 9

                namespace = {'_connection_gate': gate, 'asyncio': SimpleNamespace(sleep=sleep),
                             '_input_lock': asyncio.Lock(), '_check_stop': gate.checkpoint,
                             '_check_frontmost': lambda pid: gate.checkpoint(),
                             '_snapshot_pasteboard': lambda: (['original'], 7),
                             '_restore_pasteboard': restored.append, '_paste_sync': MagicMock(),
                             'subprocess': SimpleNamespace(run=MagicMock(side_effect=pbcopy)),
                             '_clipboard_snapshot': None, '_clipboard_change_count': None,
                             '_clipboard_scope': None}
                load_functions('computer.py', {'type_text'}, namespace)
                with patch.dict(sys.modules, {'AppKit': appkit}), gate.scope(request):
                    with self.assertRaises(policy.AccessDisabled):
                        asyncio.run(namespace['type_text']('temporary'))
                self.assertEqual(restored, [] if newer_copy else [['original']])
                namespace['_paste_sync'].assert_not_called()
                namespace['subprocess'].run.assert_called_once()
                self.assertIsNone(namespace['_clipboard_snapshot'])
                self.assertIsNone(namespace['_clipboard_change_count'])
                self.assertIsNone(namespace['_clipboard_scope'])

    def listener(self):
        """Compile the actual listener lifecycle with synthetic native handles only."""
        class Pointer:
            """Provide CF pointer constants without loading a native framework."""

            def __init__(self, value):
                """Retain only a synthetic integer address."""
                self.value = value

            @classmethod
            def in_dll(cls, library, name):
                """Expose two synthetic mode constants expected by the real source."""
                return cls(80 if name == 'kCFRunLoopCommonModes' else 81)

        native = MagicMock()
        native.CGEventTapCreate.return_value = 10
        cf = MagicMock()
        cf.CFMachPortCreateRunLoopSource.return_value = 11
        cf.CFRunLoopGetCurrent.return_value = 12
        adapter = SimpleNamespace(Request=gate.Request, policy=policy, scope=gate.scope,
                                  checkpoint=gate.checkpoint, cleanup=gate.cleanup, valid=gate.valid)
        namespace = {'_connection_gate': adapter, '_tap_lock': threading.RLock(),
                     'threading': threading, 'ctypes': SimpleNamespace(c_void_p=Pointer, c_uint64=int),
                     '_cg': native, '_cf': cf, '_stop_thread': [None], '_stop_generation': [None],
                     '_stop_tap': [None], '_stop_loop': [None], '_tap_callback': object(),
                     'kCGSessionEventTap': 1, 'kCGHeadInsertEventTap': 0,
                     'kCGEventTapOptionListenOnly': 1, 'kCGEventKeyDown': 10,
                     'log': logging.getLogger('inert-listener')}
        load_functions('computer.py', {'_start_emergency_stop_tap', '_stop_emergency_stop_tap'}, namespace)
        return namespace

    def peer_can_take_listener_lock(self, lock):
        """Use another Python thread to inspect lifetime serialization, not native state."""
        def attempt():
            """Return whether the listener lock was free during a synthetic native call."""
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            return acquired

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(attempt).result(timeout=1)

    def test_listener_stop_holds_handle_lock_during_native_shutdown(self):
        """A synthetic native handle cannot be freed between shutdown's lookup and use."""
        namespace = self.listener()
        namespace['_stop_tap'][0] = 10
        namespace['_stop_loop'][0] = 12
        ownership = []
        namespace['_cg'].CGEventTapEnable.side_effect = lambda *args: ownership.append(
            self.peer_can_take_listener_lock(namespace['_tap_lock']))
        namespace['_cf'].CFRunLoopStop.side_effect = lambda *args: ownership.append(
            self.peer_can_take_listener_lock(namespace['_tap_lock']))
        namespace['_stop_emergency_stop_tap']()
        self.assertEqual(ownership, [False, False])

    def test_listener_early_stop_cannot_enter_a_permanent_wait(self):
        """Revocation immediately before the first native wait terminates its generation."""
        self.enable()
        namespace = self.listener()
        release_legacy_wait = threading.Event()

        def checkpoint(*args, **kwargs):
            """Reproduce Off after the final check but before entering a native run loop."""
            gate.checkpoint(*args, **kwargs)
            if namespace['_stop_loop'][0] is not None:
                self.cycle()
                namespace['_stop_emergency_stop_tap']()

        namespace['_connection_gate'].checkpoint = checkpoint
        namespace['_cf'].CFRunLoopRun.side_effect = lambda: release_legacy_wait.wait(1)
        namespace['_start_emergency_stop_tap']()
        thread = namespace['_stop_thread'][0]
        try:
            thread.join(timeout=0.3)
            self.assertFalse(thread.is_alive())
            namespace['_cf'].CFRunLoopRun.assert_not_called()
            namespace['_cf'].CFRunLoopRunInMode.assert_not_called()
            self.assertIsNone(namespace['_stop_tap'][0])
            self.assertIsNone(namespace['_stop_loop'][0])
            self.assertEqual(namespace['_cf'].CFRelease.call_count, 2)
        finally:
            release_legacy_wait.set()
            thread.join(timeout=1)

    def test_listener_normal_wait_and_final_release_are_bounded(self):
        """One fake bounded native wait ends after Off and releases its two owned refs."""
        self.enable()
        namespace = self.listener()
        ownership = []
        namespace['_cf'].CFRunLoopRunInMode.side_effect = lambda *args: self.cycle()
        namespace['_cf'].CFRelease.side_effect = lambda *args: ownership.append(
            self.peer_can_take_listener_lock(namespace['_tap_lock']))
        namespace['_start_emergency_stop_tap']()
        namespace['_stop_thread'][0].join(timeout=1)
        self.assertFalse(namespace['_stop_thread'][0].is_alive())
        namespace['_cf'].CFRunLoopRunInMode.assert_called_once_with(81, 0.05, False)
        namespace['_cf'].CFRunLoopRun.assert_not_called()
        self.assertEqual(ownership, [False, False])

    def display_mode(self):
        """Load the real scale getter and exact release alias with fake CG functions."""
        native = SimpleNamespace(CGMainDisplayID=MagicMock(return_value=1),
            CGDisplayCopyDisplayMode=MagicMock(return_value=90),
            CGDisplayModeGetPixelWidth=MagicMock(return_value=2000),
            CGDisplayModeGetWidth=MagicMock(return_value=1000), CGDisplayModeRelease=MagicMock())
        namespace = {'_cg': native, '_HAS_DISPLAY_MODE': True, '_connection_gate': gate,
                     '_run_process': MagicMock(return_value=SimpleNamespace(stdout='retina'))}
        source = Path(__file__).resolve().parents[1] / 'klyk/capture.py'
        assignment = next(node for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == '_release_display_mode' for target in node.targets))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[assignment], type_ignores=[])),
                     str(source), 'exec'), namespace)
        namespace['_cg'] = gate.protect_library(native)
        load_functions('capture.py', {'get_scale_factor'}, namespace)
        return namespace, native

    def test_display_mode_release_runs_after_revocation_without_new_reads(self):
        """Off during a copy or getter releases its owned mode and prevents every next read."""
        for phase in ('copy', 'pixel'):
            with self.subTest(phase=phase):
                request = self.enable()
                namespace, native = self.display_mode()
                selected = native.CGDisplayCopyDisplayMode if phase == 'copy' else native.CGDisplayModeGetPixelWidth
                selected.side_effect = lambda *args: (self.cycle(), 90 if phase == 'copy' else 2000)[1]
                with gate.scope(request):
                    with self.assertRaises(policy.AccessDisabled):
                        namespace['get_scale_factor']()
                    with self.assertRaises(policy.AccessDisabled):
                        namespace['_cg'].CGDisplayModeGetWidth(90)
                    with self.assertRaises(policy.AccessDisabled):
                        namespace['_cg'].CGDisplayModeRelease(90)
                native.CGDisplayModeRelease.assert_called_once_with(90)
                native.CGDisplayModeGetWidth.assert_not_called()
                namespace['_run_process'].assert_not_called()
                self.assertEqual(native.CGDisplayModeGetPixelWidth.call_count, phase == 'pixel')

    def test_display_mode_release_runs_on_getter_failure_and_success(self):
        """A normal getter error still releases exactly once before the inert fallback."""
        request = self.enable()
        for failed in (False, True):
            with self.subTest(failed=failed), gate.scope(request):
                namespace, native = self.display_mode()
                if failed:
                    native.CGDisplayModeGetWidth.side_effect = RuntimeError('synthetic getter failure')
                self.assertEqual(namespace['get_scale_factor'](), 2.0)
                native.CGDisplayModeRelease.assert_called_once_with(90)
                self.assertEqual(namespace['_run_process'].call_count, int(failed))

    def test_off_display_mode_never_acquires_a_reference(self):
        """Off before the first read neither acquires nor releases a synthetic mode."""
        namespace, native = self.display_mode()
        with self.assertRaises(policy.AccessDisabled):
            namespace['get_scale_factor']()
        native.CGMainDisplayID.assert_not_called()
        native.CGDisplayCopyDisplayMode.assert_not_called()
        native.CGDisplayModeRelease.assert_not_called()
        namespace['_run_process'].assert_not_called()


class WriterGateTests(IsolatedPolicy):
    """Only complete fixed refusals may cross the writer after generation revocation."""

    def frame(self, request_id):
        """Build an inert sensitive result; no image or clipboard was actually read."""
        return json.dumps({'jsonrpc': '2.0', 'id': request_id,
                           'result': {'content': [{'type': 'image', 'data': 'PRIVATE_PIXELS'},
                                                  {'type': 'text', 'text': 'PRIVATE_AX_VALUE'}]}}) + '\n'

    def test_revoked_frame_is_replaced_at_actual_writer(self):
        """A handler completed while On; its later writer sees the Off/On transition."""
        request = self.enable()
        gate.bind_response(7, request)
        self.cycle()
        stream = io.StringIO()
        gate.GuardedWriter(stream).write(self.frame(7))
        result = json.loads(stream.getvalue())
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('PRIVATE', stream.getvalue())

    def test_fresh_frame_and_protocol_handshake_remain_available(self):
        """Valid tool replies survive; non-tool protocol messages do not need access."""
        request = self.enable()
        gate.bind_response('fresh', request)
        stream = io.StringIO()
        writer = gate.GuardedWriter(stream)
        writer.write(self.frame('fresh'))
        self.assertIn('PRIVATE_PIXELS', stream.getvalue())
        policy.set_enabled('codex', False)
        stream.seek(0)
        stream.truncate()
        writer.write('{"jsonrpc":"2.0","id":"init","result":{"protocolVersion":"test"}}\n')
        self.assertIn('protocolVersion', stream.getvalue())

    def test_duplicate_ids_never_unpoison_a_later_sensitive_reply(self):
        """Both ambiguous replies are refused even when the first was already written."""
        request = self.enable()
        gate.bind_response(9, request)
        gate.bind_response(9, request)
        stream = io.StringIO()
        writer = gate.GuardedWriter(stream)
        writer.write(self.frame(9))
        writer.write(self.frame(9))
        self.assertNotIn('PRIVATE', stream.getvalue())
        self.assertEqual(len(stream.getvalue().splitlines()), 2)


class UIQueueGateTests(IsolatedPolicy):
    """Use the real Python UI queue without installing NSApp or drawing anything."""

    def ui(self):
        """Expose only the queue to the test; no native event loop is installed."""
        value = UIThread()
        value._available = True
        return value

    def test_queued_callback_does_not_run_after_off_on(self):
        """A main-thread callback retains the generation from dispatch time."""
        self.enable()
        ui, action = self.ui(), MagicMock()
        self.assertTrue(ui.dispatch(action))
        self.cycle()
        with self.assertRaises(policy.AccessDisabled):
            ui._queue.get_nowait()()
        action.assert_not_called()

    def test_independent_controls_callback_runs_while_everything_off(self):
        """An explicit UI-only exemption preserves the separate controls surface."""
        ui, status_refresh = self.ui(), MagicMock()
        self.assertTrue(ui.dispatch(status_refresh, guarded=False))
        ui._queue.get_nowait()()
        status_refresh.assert_called_once()

    def test_sync_timeout_cancels_the_queued_callback(self):
        """A native initializer/action cannot unexpectedly run after its timeout."""
        self.enable()
        ui, action = self.ui(), MagicMock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ui.dispatch_sync, action, 0.02)
            with self.assertRaises(TimeoutError):
                future.result(timeout=1)
        ui._queue.get_nowait()()
        action.assert_not_called()

    def test_sync_revocation_does_not_wait_for_long_native_timeout(self):
        """Queued work observes Off within its short wait poll, then stays cancelled."""
        self.enable()
        ui, action = self.ui(), MagicMock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ui.dispatch_sync, action, 10)
            deadline = time.monotonic() + 1
            while ui._queue.empty() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertFalse(ui._queue.empty())
            policy.set_enabled('codex', False)
            with self.assertRaises(policy.AccessDisabled):
                future.result(timeout=0.3)
        ui._queue.get_nowait()()
        action.assert_not_called()

    def test_ui_queue_has_a_finite_capacity(self):
        """A client cannot retain an unlimited queue of generation-bound closures."""
        self.enable()
        ui = self.ui()
        for _ in range(256):
            self.assertTrue(ui.dispatch(lambda: None))
        self.assertFalse(ui.dispatch(lambda: None))
        self.assertEqual(ui._queue.qsize(), 256)

    def test_direct_main_thread_result_is_bound_to_its_original_generation(self):
        """The main-thread fast path cannot return a result from an Off/On transition."""
        self.enable()
        ui = self.ui()
        with self.assertRaises(policy.AccessDisabled):
            ui.dispatch_sync(lambda: (self.cycle(), 'private old result')[1])


if __name__ == '__main__':
    unittest.main()
