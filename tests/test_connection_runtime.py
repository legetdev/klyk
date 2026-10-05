"""Run the real dispatcher under isolated access policy and inert desktop adapters."""

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from klyk import connection_gate as gate, connection_policy as policy
from server_support import load_server, payload
from test_connection_gate import IsolatedPolicy


class ConnectionRuntimeTests(IsolatedPolicy, unittest.IsolatedAsyncioTestCase):
    """A persistent dispatcher must reject old work and accept only new On requests."""

    async def asyncSetUp(self):
        """Compile real handlers with spies; no native module is imported or invoked."""
        self.server = load_server()
        self.server.connection_gate = gate
        self.server.server = SimpleNamespace(request_context=SimpleNamespace(request_id=None))
        asyncio.get_running_loop().set_default_executor(gate.GateExecutor(max_workers=2))

    async def test_every_tool_refuses_off_without_native_startup(self):
        """The Off gate precedes validation, ownership and every tool body."""
        for tool in self.server.TOOLS:
            with self.subTest(tool=tool.name):
                result = payload(await self.server.call_tool(tool.name, {}))
                self.assertEqual(result['blocked'], 'access_off')
                self.assertFalse(result['ok'])
        self.server._ensure_native_runtime.assert_not_awaited()
        self.assertEqual(self.server.computer.method_calls, [])
        self.assertEqual(self.server.capture.method_calls, [])
        self.assertEqual(self.server.ownership.method_calls, [])

    async def test_same_dispatcher_resumes_without_reconnect(self):
        """One live module serves Off, On, Off and a fresh On generation."""
        self.assertEqual(payload(await self.server.call_tool('wait', {'seconds': 0}))['blocked'], 'access_off')
        self.enable()
        self.assertTrue(payload(await self.server.call_tool('wait', {'seconds': 0}))['ok'])
        policy.set_enabled('codex', False)
        self.assertEqual(payload(await self.server.call_tool('wait', {'seconds': 0}))['blocked'], 'access_off')
        policy.set_enabled('codex', True)
        self.assertTrue(payload(await self.server.call_tool('wait', {'seconds': 0}))['ok'])
        self.assertEqual(self.server._ensure_native_runtime.await_count, 2)

    async def test_bad_on_arguments_do_not_initialize_native(self):
        """Permission probes and the input self-test follow successful validation."""
        self.enable()
        result = payload(await self.server.call_tool('press_key', {
            'app': 'Fixture', 'key': 'a', 'repeat': 0,
        }))
        self.assertFalse(result['ok'])
        self.server._ensure_native_runtime.assert_not_awaited()

    async def test_request_queued_before_cycle_never_enters_tool_body(self):
        """The generation is captured before the call lock rather than after it."""
        self.enable()
        await self.server._call_lock.acquire()
        queued = asyncio.create_task(self.server.call_tool('wait', {'seconds': 0}))
        try:
            await asyncio.sleep(0.01)
            self.cycle()
        finally:
            self.server._call_lock.release()
        self.assertEqual(payload(await queued)['blocked'], 'access_off')
        self.server._ensure_native_runtime.assert_not_awaited()
        self.assertTrue(payload(await self.server.call_tool('wait', {'seconds': 0}))['ok'])

    async def test_inherited_child_cannot_capture_a_fresh_generation(self):
        """A child of an old request cannot rearm itself after Off/On."""
        request = self.enable()
        with gate.scope(request):
            self.cycle()
            child = asyncio.create_task(self.server.call_tool('wait', {'seconds': 0}))
            self.assertEqual(payload(await child)['blocked'], 'access_off')
        self.server._ensure_native_runtime.assert_not_awaited()

    async def test_off_watch_cancels_waiting_call_without_waiting_for_call_lock(self):
        """A queued call finishes within the polling bound while a lock remains held."""
        self.enable()
        callback = MagicMock()
        stop = gate.start_watch(callback)
        await self.server._call_lock.acquire()
        queued = asyncio.create_task(self.server.call_tool('wait', {'seconds': 0}))
        try:
            await asyncio.sleep(0.01)
            policy.set_enabled('codex', False)
            result = await asyncio.wait_for(queued, timeout=0.5)
            self.assertEqual(payload(result)['blocked'], 'access_off')
            self.assertTrue(self.server._call_lock.locked())
            callback.assert_called()
        finally:
            self.server._call_lock.release()
            stop()

    async def test_watch_cancels_long_wait_and_skips_remaining_batch(self):
        """Revocation cancels sleeping work; a batch cannot continue under a new token."""
        self.enable()
        original = self.server._execute_tool
        steps = []
        self.server._get_session = AsyncMock(return_value=(SimpleNamespace(), False))

        async def execute(name, arguments):
            """Cycle the switch just after the first real nested step completed."""
            result = await original(name, arguments)
            if name == 'wait':
                self.assertTrue(payload(result)['ok'])
                steps.append(name)
                self.cycle()
            return result

        self.server._execute_tool = execute
        result = await self.server.call_tool('run', {'app': 'Fixture', 'actions': [
            {'tool': 'wait', 'seconds': 0}, {'tool': 'wait', 'seconds': 0},
        ]})
        self.assertEqual(payload(result)['blocked'], 'access_off')
        self.assertEqual(steps, ['wait'])
        self.server._execute_tool = original
        stop = gate.start_watch(MagicMock())
        waiting = asyncio.create_task(self.server.call_tool('wait', {'seconds': 30}))
        try:
            await asyncio.sleep(0.01)
            policy.set_enabled('codex', False)
            self.assertEqual(payload(await asyncio.wait_for(waiting, timeout=0.5))['blocked'], 'access_off')
        finally:
            stop()

    async def test_late_screenshot_and_save_are_discarded_after_revocation(self):
        """A fake capture started while On cannot return pixels or write save_path later."""
        self.enable()
        started, release = threading.Event(), threading.Event()
        session = SimpleNamespace(app='Fixture', pid=45, window_id=7, win_x=0, win_y=0,
                                  width=400, height=300, mode='background', last_mutation_at=0,
                                  screenshots_taken=0, template_cache={})
        self.server._get_session = AsyncMock(return_value=(session, False))
        self.server._refresh_window = AsyncMock(return_value=None)
        pixels = base64.b64encode(b'PRIVATE_CAPTURE').decode()

        def capture(**kwargs):
            """Hold only a synthetic string, not native image or screenshot APIs."""
            started.set()
            release.wait(1)
            return pixels, 400, 300

        self.server.capture.take_screenshot.side_effect = capture
        destination = self.path.parent / 'must-not-be-created.png'
        call = asyncio.create_task(self.server.call_tool('screenshot', {
            'app': 'Fixture', 'save_path': str(destination),
        }))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            self.cycle()
        finally:
            release.set()
        result = await call
        self.assertEqual(payload(result)['blocked'], 'access_off')
        self.assertTrue(all(item.type == 'text' for item in result))
        self.assertNotIn(pixels, json.dumps([item.model_dump() for item in result]))
        self.assertFalse(destination.exists())

    async def test_result_revoked_during_post_action_verify_is_dropped(self):
        """Optional focused-state evidence cannot outlive the action's access generation."""
        self.enable()
        session = SimpleNamespace(app='Fixture', pid=45, window_id=7, win_x=0, win_y=0,
                                  mode='humanoid', last_mutation_at=0)
        self.server._get_session = AsyncMock(return_value=(session, False))
        self.server._refresh_window = AsyncMock(return_value=None)
        self.server._focus_if_needed = AsyncMock(return_value=None)
        self.server._ensure_key_delivery = AsyncMock(return_value=None)
        self.server.computer.press_key = AsyncMock(return_value=None)

        async def verify(app):
            """Model a native value completing after its access was revoked."""
            self.cycle()
            return {'value': 'PRIVATE_FOCUSED_VALUE'}

        self.server._post_action_verify = verify
        result = await self.server.call_tool('press_key', {
            'app': 'Fixture', 'key': 'a', 'verify': True,
        })
        self.assertEqual(payload(result)['blocked'], 'access_off')
        self.assertNotIn('PRIVATE_FOCUSED_VALUE', json.dumps([item.model_dump() for item in result]))

    async def test_revocation_releases_ownership_even_if_input_cleanup_fails(self):
        """A failed best-effort up must not leave an Off process owning control."""
        self.enable()
        policy.set_enabled('codex', False)
        self.server.computer.revoke_access.side_effect = RuntimeError('inert cleanup failure')
        self.server.ownership.release_ownership_if_owned.return_value = False
        self.assertFalse(self.server._on_access_revoked())
        self.server.ownership.release_ownership_if_owned.assert_called_once()
        self.server.ownership.release_ownership_if_owned.return_value = True
        self.assertTrue(self.server._on_access_revoked())
        self.assertEqual(self.server.ownership.release_ownership_if_owned.call_count, 2)
        self.server._refresh_menubar.assert_called()

    async def test_old_generation_cleanup_preserves_reenabled_ownership(self):
        """Selective cleanup may release old held inputs without clearing fresh control."""
        self.enable()
        self.cycle()
        self.assertTrue(self.server._on_access_revoked())
        self.server.computer.revoke_access.assert_called_once()
        self.server.ownership.release_ownership_if_owned.assert_not_called()

    async def test_watch_retries_busy_ownership_after_requests_have_drained(self):
        """The watcher retries a nonblocking release even with no active request left."""
        self.enable()
        first_poll = threading.Event()
        completed = threading.Event()
        calls = []
        original = policy.snapshot

        def snapshot():
            """Signal only the watcher's initial isolated policy observation."""
            value = original()
            first_poll.set()
            return value

        def revoke():
            """Model one busy owner-file lock followed by a successful release."""
            calls.append('attempt')
            if len(calls) > 1:
                completed.set()
                return True
            return False

        from unittest.mock import patch
        with patch.object(policy, 'snapshot', side_effect=snapshot):
            stop = gate.start_watch(revoke)
            try:
                self.assertTrue(await asyncio.to_thread(first_poll.wait, 0.5))
                policy.set_enabled('codex', False)
                self.assertTrue(await asyncio.to_thread(completed.wait, 0.5))
                self.assertEqual(calls, ['attempt', 'attempt'])
            finally:
                stop()

    async def test_controls_startup_is_independent_of_off_protocol_and_native_access(self):
        """A blocked fake icon launcher runs in a daemon without delaying the entry point."""
        import sys
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        threads = []

        def start_background():
            """Block only a synthetic launcher, never an AppKit or subprocess operation."""
            threads.append(threading.current_thread())
            entered.set()
            release.wait(1)
            return True

        self.server.sys = SimpleNamespace(platform='darwin')
        self.server.__package__ = 'klyk'
        self.server._run_on_macos = MagicMock()
        with patch.dict(sys.modules, {'klyk.controls': SimpleNamespace(start_background=start_background)}):
            try:
                self.server._main_entry()
                self.server._run_on_macos.assert_called_once()
                self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))
                self.assertTrue(threads[0].daemon)
                self.assertEqual(self.server.computer.method_calls, [])
                self.assertEqual(self.server.ownership.method_calls, [])
            finally:
                release.set()
                if threads:
                    await asyncio.to_thread(threads[0].join, 1)


if __name__ == '__main__':
    unittest.main()
