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
        launcher_fake = SimpleNamespace(pid_alive=MagicMock(return_value=pid_alive))
        return capture, launcher_fake

    def test_attach_to_known_pid_does_not_open_or_spawn(self):
        """A known running app is attached by PID without another open request."""
        with patch.object(launcher, "_quick_pid_for_app", return_value=731), patch.object(
            launcher.subprocess, "Popen"
        ) as popen:
            result = launcher.launch_native_app(app_name="Fixture App")

        self.assertEqual(result, (731, True))
        popen.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
