"""Keep native Open navigation scoped while all desktop/input boundaries are inert."""

import asyncio
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from server_support import load_server, payload


class OpenDialogTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real dispatcher with phase-correct native chooser responses."""

    def setUp(self):
        """Create one observed Open panel without any live application or keyboard."""
        self.server = load_server()
        self.session = SimpleNamespace(app="Fixture", pid=42, mode="autonomous")
        self.path = "/tmp/generated-fixture.txt"
        self.server._get_session = AsyncMock(return_value=(self.session, False))
        self.server._await_frontmost = AsyncMock(return_value=True)
        self.server.ownership.is_owner.return_value = True
        self.server.computer.ax_snapshot.return_value = [
            {"role": "AXSheet", "label": "Open", "x": 500, "y": 400,
             "width": 500, "height": 300},
            {"role": "AXButton", "label": "Open"},
        ]
        self.server.computer.ax_navigate_open_panel.return_value = self.path
        self.server.computer.ax_press_panel_button.return_value = "Open"
        self.server.computer.press_key = AsyncMock()
        self.server.computer.type_text = AsyncMock()
        self.server.computer.type_text_char_by_char = AsyncMock()
        # Advance only this source module's clock/sleeps so refusal deadlines
        # remain meaningful without blocking a test or patching asyncio globally.
        ticks = itertools.count()
        self.server.time = SimpleNamespace(monotonic=lambda: next(ticks) * 0.05)
        self.server.asyncio = SimpleNamespace(
            current_task=asyncio.current_task, get_event_loop=asyncio.get_event_loop,
            sleep=AsyncMock(),
        )

    async def open(self, *, with_path=True):
        """Call the real Open handler and return its structured result."""
        arguments = {"app": "Fixture", "action": "open"}
        if with_path:
            arguments["path"] = self.path
        return payload(await self.server.call_tool("handle_system_dialog", arguments))

    def assert_no_global_path_input(self):
        """Only the guarded chooser shortcut may use keyboard delivery."""
        self.server.computer.type_text.assert_not_awaited()
        self.server.computer.type_text_char_by_char.assert_not_awaited()
        for call in self.server.computer.press_key.await_args_list:
            self.assertEqual(call.args[0], "Cmd+Shift+G")
            self.assertEqual(call.kwargs["expected_frontmost_pid"], 42)

    async def test_exact_scoped_navigation_uses_native_helper_without_global_path_input(self):
        """A verified native path/closure permits only the observed Open-button action."""
        result = await self.open()
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], self.path)
        self.server.computer.ax_navigate_open_panel.assert_called_once_with(42, self.path)
        self.server.computer.ax_press_panel_button.assert_called_once_with(42, ("Open", "Choose"))
        self.server.computer.press_key.assert_awaited_once_with("Cmd+Shift+G", expected_frontmost_pid=42)
        self.assert_no_global_path_input()

    async def test_focused_document_field_cannot_become_a_path_input_fallback(self):
        """A slash-prefixed document value outside the panel is irrelevant to chooser proof."""
        self.server.computer.ax_snapshot.return_value.append({
            "role": "AXTextField", "label": "Document content", "focused": True,
            "value": "/existing document text", "x": 50, "y": 50,
            "width": 100, "height": 30,
        })
        self.server.computer.ax_navigate_open_panel.return_value = None
        result = await self.open()
        self.assertFalse(result["ok"])
        self.assertIn("no path was written", result["error"])
        self.server.computer.ax_press_panel_button.assert_not_called()
        self.assert_no_global_path_input()

    async def test_missing_or_multiple_panels_send_no_shortcut_or_path(self):
        """A missing/ambiguous initial panel must stop before any navigation mutation."""
        for panels in ([], [{"role": "AXSheet"}, {"role": "AXDialog"}]):
            with self.subTest(panels=panels):
                self.server.computer.ax_snapshot.return_value = panels
                result = await self.open()
                self.assertFalse(result["ok"])
        self.server.computer.press_key.assert_not_awaited()
        self.server.computer.ax_navigate_open_panel.assert_not_called()
        self.server.computer.ax_press_panel_button.assert_not_called()

    async def test_missing_open_button_sends_no_shortcut_or_path(self):
        """An unrelated single sheet is not proof of an Open/Choose panel."""
        self.server.computer.ax_snapshot.return_value = [{"role": "AXSheet"}]
        result = await self.open()
        self.assertFalse(result["ok"])
        self.server.computer.press_key.assert_not_awaited()
        self.server.computer.ax_navigate_open_panel.assert_not_called()

    async def test_unready_chooser_retries_only_until_deadline_before_open(self):
        """A no-write observation may retry; its sleep never exceeds remaining readiness time."""
        self.server.computer.ax_navigate_open_panel.return_value = None
        result = await self.open()
        self.assertFalse(result["ok"])
        self.assertGreater(self.server.computer.ax_navigate_open_panel.call_count, 1)
        self.server.computer.ax_press_panel_button.assert_not_called()
        self.assertTrue(all(0 <= call.args[0] <= 0.1 for call in self.server.asyncio.sleep.await_args_list))
        self.assert_no_global_path_input()

    async def test_ready_after_no_write_observation_keeps_supported_navigation(self):
        """Only the helper's no-write state is eligible for a readiness retry."""
        self.server.computer.ax_navigate_open_panel.side_effect = [None, self.path]
        result = await self.open()
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.computer.ax_navigate_open_panel.call_count, 2)
        self.server.computer.ax_press_panel_button.assert_called_once()
        self.assert_no_global_path_input()

    async def test_unknown_navigation_effect_never_retries_or_presses_open(self):
        """An attempted write/commit failure must not trigger a second navigation or Return."""
        self.server.computer.ax_navigate_open_panel.side_effect = RuntimeError("Chooser effect is unknown; inspect first")
        result = await self.open()
        self.assertFalse(result["ok"])
        self.server.computer.ax_navigate_open_panel.assert_called_once_with(42, self.path)
        self.server.computer.ax_press_panel_button.assert_not_called()
        self.assert_no_global_path_input()

    async def test_unsupported_native_commit_fails_without_open_or_keyboard_fallback(self):
        """A provider without a scoped commit action must not be guessed with global Return."""
        self.server.computer.ax_navigate_open_panel.side_effect = RuntimeError(
            "The chooser has no accessible confirmation action; no path was written"
        )
        result = await self.open()
        self.assertFalse(result["ok"])
        self.server.computer.ax_navigate_open_panel.assert_called_once()
        self.server.computer.ax_press_panel_button.assert_not_called()
        self.assert_no_global_path_input()

    async def test_mismatched_native_readback_never_presses_open(self):
        """Even a truthy helper response cannot substitute a different requested path."""
        self.server.computer.ax_navigate_open_panel.return_value = "/tmp/different-fixture.txt"
        result = await self.open()
        self.assertFalse(result["ok"])
        self.server.computer.ax_press_panel_button.assert_not_called()
        self.assert_no_global_path_input()

    async def test_open_without_path_uses_only_the_observed_panel_button(self):
        """Existing selected-file navigation remains supported without a GoTo operation."""
        result = await self.open(with_path=False)
        self.assertTrue(result["ok"])
        self.server.computer.press_key.assert_not_awaited()
        self.server.computer.ax_navigate_open_panel.assert_not_called()
        self.server.computer.ax_press_panel_button.assert_called_once()

    async def test_background_and_failed_host_focus_send_no_dialog_input(self):
        """Neither a background request nor failed activation can enter the chooser."""
        self.session.mode = "background"
        background = await self.open()
        self.assertTrue(background["requires_foreground"])
        self.server._await_frontmost.assert_not_awaited()
        self.session.mode = "autonomous"
        self.server._await_frontmost.return_value = False
        failed_focus = await self.open()
        self.assertFalse(failed_focus["ok"])
        self.server.computer.press_key.assert_not_awaited()
        self.server.computer.ax_navigate_open_panel.assert_not_called()
        self.server.computer.ax_press_panel_button.assert_not_called()


if __name__ == "__main__":
    unittest.main()
