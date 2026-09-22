"""Portable observation and dispatcher contract regressions.

The server is loaded through ``server_support`` so every native boundary is a
controlled mock and no application or desktop state is touched.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import unittest

from mcp import types

from server_support import load_server, payload


class ObservationContractTests(unittest.IsolatedAsyncioTestCase):
    """Keep observation targeting, retry policy, and repaint timing precise."""

    def setUp(self):
        """Create one inert server and a stable selected-window fixture."""
        self.server = load_server()
        self.session = SimpleNamespace(
            app="Fixture",
            pid=123,
            window_id=77,
            win_x=10,
            win_y=20,
            width=100,
            height=80,
            mode="autonomous",
            windowless=False,
            screenshots_taken=0,
            ax_warmup_attempted=False,
            last_mutation_at=0.0,
        )
        self.server._get_session = AsyncMock(return_value=(self.session, False))
        self.server._refresh_window = AsyncMock()
        self.server.ownership.is_owner.return_value = True
        self.server.computer.ax_snapshot.return_value = []
        self.server.computer.ax_search_focused.return_value = []
        self.server.ocr.is_available.return_value = False

    async def test_display_screenshot_does_not_need_or_open_an_app(self):
        """Display capture validates without app and never resolves a session."""
        schema = self.server._TOOL_VALIDATORS["screenshot"]
        schema.validate({"display": "main"})
        self.server.capture.resolve_display.return_value = {
            "index": 0,
            "display_id": 11,
            "x": 0,
            "y": 0,
            "is_main": True,
        }
        self.server.capture.take_display_screenshot.return_value = ("cG5n", 100, 80)

        result = payload(await self.server.call_tool("screenshot", {"display": "main"}))

        self.assertEqual(result["coord_space"], "screen")
        self.server._get_session.assert_not_awaited()
        self.server.capture.resolve_display.assert_called_once_with("main")

    async def test_selected_window_is_forwarded_to_ax_snapshot_and_label_search(self):
        """Both structural reads and semantic lookup stay on the selected window."""
        await self.server.call_tool("ax_snapshot", {"app": "Fixture", "window_id": 77})
        self.server.computer.ax_snapshot.assert_called_once_with(
            123, max_results=201, window_id=77
        )

        self.server.computer.ax_snapshot.reset_mock()
        await self.server.call_tool(
            "click_element", {"app": "Fixture", "window_id": 77, "label": "Save"}
        )
        self.server.computer.ax_search_focused.assert_called_once_with(
            123, "save", max_results=32, window_id=77
        )

    async def test_nonempty_sparse_inspect_does_not_warmup_sleep(self):
        """A sparse but nonempty AX response is valid and receives no retry delay."""
        self.server.computer.ax_snapshot.return_value = [
            {"role": "AXButton", "label": "Save", "x": 20, "y": 30}
        ]
        sleeper = AsyncMock()
        with patch.object(self.server.asyncio, "sleep", sleeper):
            result = payload(
                await self.server.call_tool(
                    "inspect", {"app": "Fixture", "detail": "slim"}
                )
            )

        self.assertEqual(result["ax_element_count"], 1)
        self.server.computer.ax_snapshot.assert_called_once()
        sleeper.assert_not_awaited()

    async def test_empty_ax_warmup_retries_only_once_per_session(self):
        """Repeated empty inspections perform one warmup retry, then stay cheap."""
        self.server.computer.ax_snapshot.side_effect = [[], [], []]
        sleeper = AsyncMock()
        with patch.object(self.server.asyncio, "sleep", sleeper):
            await self.server.call_tool("inspect", {"app": "Fixture", "detail": "slim"})
            await self.server.call_tool("inspect", {"app": "Fixture", "detail": "slim"})

        self.assertEqual(self.server.computer.ax_snapshot.call_count, 3)
        sleeper.assert_awaited_once_with(0.25)
        self.assertTrue(self.session.ax_warmup_attempted)

    async def test_top_level_calls_serialize_and_nested_run_does_not_deadlock(self):
        """The request lock serializes peers while run's ordered children re-enter safely."""
        original = self.server._execute_tool
        active = 0
        maximum = 0

        async def tracked(name, arguments):
            """Track overlap around the real dispatcher without changing behavior."""
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.005)
                return await original(name, arguments)
            finally:
                active -= 1

        self.server._execute_tool = tracked
        peers = await asyncio.gather(
            self.server.call_tool("wait", {"seconds": 0.01}),
            self.server.call_tool("wait", {"seconds": 0.01}),
        )
        self.assertEqual(len(peers), 2)
        self.assertEqual(maximum, 1)

        nested = payload(
            await self.server.call_tool(
                "run",
                {
                    "app": "Fixture",
                    "actions": [
                        {"tool": "wait", "seconds": 0},
                        {"tool": "wait", "seconds": 0},
                    ],
                },
            )
        )
        self.assertTrue(nested["ok"])

    async def test_repaint_wait_uses_remaining_budget_after_think_time(self):
        """A capture after 120 ms of think time waits only for the remainder."""
        self.session.last_mutation_at = time.monotonic() - 0.12
        self.server.capture.take_screenshot.return_value = ("cG5n", 100, 80)

        await self.server._take_screenshot(self.session, window_id=77)

        settle = self.server.capture.take_screenshot.call_args.kwargs["settle_ms"]
        self.assertGreaterEqual(settle, 0)
        self.assertLessEqual(settle, 60)
        self.assertLess(settle, self.server._POST_ACTION_SETTLE_MS)

    async def test_background_ocr_match_refuses_when_skylight_is_unavailable(self):
        """OCR grounding must preserve background mode's no-cursor contract."""
        self.session.mode = "background"
        self.server.skylight.is_available.return_value = True
        self.server.skylight.delivery_verified.return_value = False
        self.server.capture.take_screenshot.return_value = ("cG5n", 100, 80)
        self.server.ocr.is_available.return_value = True
        self.server.ocr.recognize_all.return_value = [
            {"text": "Save", "x": 30, "y": 40, "width": 40, "height": 20, "confidence": 0.99}
        ]

        result = payload(await self.server.call_tool(
            "click_element", {"app": "Fixture", "window_id": 77, "label": "Save"}
        ))

        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_foreground"])
        self.assertEqual(result["reason"], "skylight_delivery_unavailable")
        self.server.computer.click.assert_not_called()

    async def test_coordinate_fallback_with_failed_focus_sends_no_input(self):
        """Semantic fallback must stop before input when the requested window is not key."""
        self.server.computer.ax_search_focused.return_value = [
            {"label": "Save", "role": "AXButton", "x": 40, "y": 60, "width": 40, "height": 20}
        ]
        self.server.computer.ax_resolve_and_act.return_value = {
            "ok": False, "status": "unsupported"
        }
        self.server.computer.raise_window = AsyncMock(return_value={
            "ok": False, "focused": False, "warning": "different window is key"
        })
        self.server.skylight.is_available.return_value = False

        for mode in ("autonomous", "humanoid"):
            with self.subTest(mode=mode):
                self.session.mode = mode
                self.server.computer.click.reset_mock()
                result = payload(await self.server.call_tool(
                    "click_element", {"app": "Fixture", "window_id": 77, "label": "Save"}
                ))
                self.assertFalse(result.get("ok", True))
                self.assertIn("different window", result["error"])
                self.server.computer.click.assert_not_called()

    async def test_successful_coordinate_fallback_preserves_target_and_delivery_route(self):
        """A successful semantic fallback returns the matched target and actual route."""
        self.session.mode = "humanoid"
        self.server.skylight.is_available.return_value = False
        self.server.computer.ax_search_focused.return_value = [
            {"label": "Save", "role": "AXButton", "x": 40, "y": 60, "width": 40, "height": 20}
        ]
        self.server.computer.ax_resolve_and_act.return_value = {
            "ok": False, "status": "unsupported"
        }
        self.server.computer.raise_window = AsyncMock(return_value={
            "ok": True, "focused": True, "window_id": 77
        })
        self.server.computer.is_frontmost_app.return_value = True
        self.server.computer.click = AsyncMock()

        result = payload(await self.server.call_tool(
            "click_element", {"app": "Fixture", "window_id": 77, "label": "Save"}
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "ax_match+cursor_warp")
        self.assertEqual(result["clicked"], {"label": "Save", "role": "AXButton",
                                               "x": 30, "y": 40, "width": 40, "height": 20})
        self.server.computer.click.assert_awaited_once_with(40, 60, "left", None)

    async def test_keyboard_tools_refuse_selected_session_window_without_selector(self):
        """Keyboard tools inherit the selected window and refuse before native input."""
        self.session.mode = "background"
        refusal = {
            "ok": False,
            "requires_foreground": True,
            "window_id": 77,
            "reason": "background_no_activate",
        }
        self.server._focus_if_needed = AsyncMock(return_value=refusal)

        cases = (
            ("type_text", {"app": "Fixture", "text": "hello", "mode": "keys"}, "type_text_char_by_char"),
            ("press_key", {"app": "Fixture", "key": "a"}, "press_key"),
            ("hold_key", {"app": "Fixture", "key": "a", "duration": 0.05}, "hold_key"),
        )
        for name, arguments, native_method in cases:
            with self.subTest(name=name):
                self.server._focus_if_needed.reset_mock()
                getattr(self.server.computer, native_method).reset_mock()
                result = payload(await self.server.call_tool(name, arguments))
                self.assertTrue(result["requires_foreground"])
                self.server._focus_if_needed.assert_awaited_once_with(self.session, 77)
                getattr(self.server.computer, native_method).assert_not_called()


if __name__ == "__main__":
    unittest.main()
