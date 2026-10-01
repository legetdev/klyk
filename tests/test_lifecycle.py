"""Portable lifecycle regressions with all native boundaries replaced by fakes."""

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import klyk
from klyk import launcher
from klyk import session as session_module


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Keep app attachment and session capacity safe without launching apps."""

    def setUp(self):
        """Isolate the process-wide session registry for each lifecycle test."""
        self._original_registry = session_module.registry
        session_module.registry = session_module.SessionRegistry()
        self.addCleanup(self._restore_registry)

    def _restore_registry(self):
        """Restore the shared registry after a test cannot affect later tests."""
        session_module.registry = self._original_registry

    def _fake_native_modules(self, *, window=None, pid_alive=True):
        """Provide native-facing modules that cannot access a real desktop."""
        capture = SimpleNamespace(get_window_by_id=MagicMock(return_value=window))
        launcher_fake = SimpleNamespace(
            pid_alive=MagicMock(return_value=pid_alive),
            process_identity=MagicMock(return_value={"pid": 731, "uid": 501, "started": (1, 0)}),
        )
        return capture, launcher_fake

    def test_attach_to_known_pid_does_not_open_or_spawn(self):
        """A known running app is attached by PID without another open request."""
        with patch.object(launcher, "_quick_pid_for_app", return_value=731), patch.object(
            launcher.subprocess, "Popen"
        ) as popen:
            result = launcher.launch_native_app(app_name="Fixture App")

        self.assertEqual(result, (731, True))
        popen.assert_not_called()

    def test_permission_error_does_not_claim_process_terminated(self):
        """An inaccessible process remains unverified at both termination polls."""
        with patch.object(launcher.os, "kill", side_effect=PermissionError):
            self.assertFalse(launcher.terminate_pid(731, term_timeout=1))
        with patch.object(launcher.os, "kill", side_effect=[None, None, PermissionError]), \
             patch.object(launcher.time, "sleep"):
            self.assertFalse(launcher.terminate_pid(731, term_timeout=0))

    async def test_missing_live_window_does_not_close_or_terminate_app(self):
        """A vanished selected window fails closed while preserving the live app."""
        existing = session_module.Session(
            session_id="fixture-session",
            app="Fixture App",
            target="native",
            pid=731,
            window_id=42,
            width=800,
            height=600,
            scale=1.0,
            process_identity={"pid": 731, "uid": 501, "started": (1, 0)},
        )
        session_module.registry.register(existing, app_key="Fixture App")
        capture, launcher_fake = self._fake_native_modules(window=None)
        close = MagicMock()
        terminate = MagicMock()
        with patch.dict(
            sys.modules,
            {"klyk.capture": capture, "klyk.launcher": launcher_fake},
        ), patch.object(klyk, "capture", capture, create=True), patch.object(klyk, "launcher", launcher_fake), patch.object(session_module, "_close_session", close), patch.object(
            launcher_fake, "terminate_pid", terminate, create=True
        ):
            with self.assertRaisesRegex(RuntimeError, "No app was closed"):
                await session_module.get_or_create_session("Fixture App")

        close.assert_not_called()
        terminate.assert_not_called()

    async def test_sixty_four_sessions_block_new_launch(self):
        """The bounded registry rejects a sixty-fifth app before create_session."""
        for index in range(64):
            session_module.registry.register(
                session_module.Session(
                    session_id=f"fixture-{index}",
                    app=f"Fixture {index}",
                    target="native",
                    pid=index + 1,
                    window_id=index + 1,
                    width=1,
                    height=1,
                    scale=1.0,
                ),
                app_key=f"Fixture {index}",
            )
        capture, launcher_fake = self._fake_native_modules(window=None)
        create = AsyncMock()
        with patch.dict(
            sys.modules,
            {"klyk.capture": capture, "klyk.launcher": launcher_fake},
        ), patch.object(klyk, "capture", capture, create=True), patch.object(klyk, "launcher", launcher_fake), patch.object(session_module, "create_session", create):
            with self.assertRaisesRegex(RuntimeError, "64-app session limit"):
                await session_module.get_or_create_session("Fixture 64")

        create.assert_not_awaited()

    async def test_windowless_session_reuses_stable_process_without_window_zero(self):
        """Dock-like sessions remain reusable without looking up a nonexistent CG window 0."""
        identity = {"pid": 731, "uid": 501, "started": (1, 0)}
        existing = session_module.Session("fixture", "Dock", "native", 731, 0, 0, 0, 1.0,
                                          windowless=True, process_identity=identity)
        session_module.registry.register(existing, app_key="Dock")
        capture, launcher_fake = self._fake_native_modules()
        with patch.dict(sys.modules, {"klyk.capture": capture, "klyk.launcher": launcher_fake}), \
             patch.object(klyk, "capture", capture, create=True), patch.object(klyk, "launcher", launcher_fake):
            session, is_new = await session_module.get_or_create_session("Dock", allow_launch=False)
        self.assertIs(session, existing)
        self.assertFalse(is_new)
        capture.get_window_by_id.assert_not_called()

    async def test_changed_process_identity_never_reuses_or_closes_live_app(self):
        """A recycled PID cannot be acted on even if an old selected window number still exists."""
        identity = {"pid": 731, "uid": 501, "started": (1, 0)}
        existing = session_module.Session("fixture", "Fixture App", "native", 731, 42, 800, 600, 1.0,
                                          process_identity=identity)
        session_module.registry.register(existing, app_key="Fixture App")
        capture, launcher_fake = self._fake_native_modules(window={"pid": 731})
        launcher_fake.process_identity.return_value = {**identity, "started": (2, 0)}
        with patch.dict(sys.modules, {"klyk.capture": capture, "klyk.launcher": launcher_fake}), \
             patch.object(klyk, "capture", capture, create=True), patch.object(klyk, "launcher", launcher_fake), \
             patch.object(session_module, "_close_session") as close:
            with self.assertRaisesRegex(RuntimeError, "process identity changed"):
                await session_module.get_or_create_session("Fixture App", allow_launch=False)
        close.assert_not_called()
        capture.get_window_by_id.assert_not_called()

    def test_close_passes_original_identity_to_termination(self):
        """Session cleanup passes its captured start token rather than identifying a replacement."""
        identity = {"pid": 731, "uid": 501, "started": (1, 0)}
        existing = session_module.Session("fixture", "Fixture App", "native", 731, 42, 800, 600, 1.0,
                                          process_identity=identity)
        launcher_fake = SimpleNamespace(terminate_pid=MagicMock(return_value=True), pid_alive=MagicMock(return_value=True))
        with patch.dict(sys.modules, {"klyk.launcher": launcher_fake}), patch.object(klyk, "launcher", launcher_fake):
            session_module._close_session(existing)
        launcher_fake.terminate_pid.assert_called_once_with(731, expected_identity=identity)

    async def test_passive_cold_attach_refuses_launch_before_effects(self):
        """Background/non-owner observations cannot cold-launch an absent native app."""
        capture, launcher_fake = self._fake_native_modules()
        launcher_fake._quick_pid_for_app = MagicMock(return_value=None)
        launcher_fake.launch_native_app = MagicMock()
        computer_fake = SimpleNamespace(run_input=AsyncMock(), activate_app=AsyncMock())
        with patch.dict(sys.modules, {"klyk.capture": capture, "klyk.launcher": launcher_fake, "klyk.computer": computer_fake}), \
             patch.object(klyk, "capture", capture, create=True), patch.object(klyk, "launcher", launcher_fake), \
             patch.object(klyk, "computer", computer_fake, create=True):
            with self.assertRaisesRegex(RuntimeError, "cannot launch"):
                await session_module.create_session("native", app_name="Fixture App", allow_launch=False)
        launcher_fake.launch_native_app.assert_not_called()
        computer_fake.run_input.assert_not_awaited()
        computer_fake.activate_app.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
