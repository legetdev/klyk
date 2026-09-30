"""Keep final keyboard readiness after window focus using inert foreground adapters."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from server_support import load_server, payload


class KeyDeliveryOrderTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual focus/readiness helpers without desktop input or activation."""

    def setUp(self):
        """Model a window raise that invalidates the preceding foreground observation."""
        self.server = load_server()
        self.session = SimpleNamespace(
            app="Fixture", pid=42, window_id=77, mode="autonomous", escalation_log=[],
        )
        self.server._get_session = AsyncMock(return_value=(self.session, False))
        self.server.ownership.is_owner.return_value = True
        self.active = True
        self.chromium = False
        self.race_before_input = False
        self.events = []
        self.server._is_chromium_based = lambda session: self.chromium
        # Only this extracted module's settles are inert; asyncio itself and
        # the real request serialization remain unchanged.
        self.server.asyncio = SimpleNamespace(
            current_task=asyncio.current_task, get_event_loop=asyncio.get_event_loop,
            sleep=AsyncMock(),
        )

        async def raise_window(pid, window_id):
            """Expose the observed activation invalidation while still making the window key."""
            self.events.append(("focus", pid, window_id))
            self.active = False
            return {"ok": True, "focused": True, "window_id": window_id}

        def is_frontmost(pid):
            """Return the current foreground state rather than a cached successful check."""
            self.events.append(("foreground_check", pid, self.active))
            return self.active

        async def await_frontmost(session):
            """Stand in for activation/settling without calling any live application API."""
            self.events.append(("ready", session.pid))
            self.active = True
            return True

        async def deliver(keys, pid, *, expected_frontmost_pid=None):
            """Model the last-instant native guard before recording any key event."""
            self.events.append(("native_guard", expected_frontmost_pid))
            if self.race_before_input:
                self.active = False
            if expected_frontmost_pid is not None and not self.active:
                raise RuntimeError("Target changed before key input; no event was posted")
            self.events.append(("input", keys, pid))

        async def hold(key, duration, pid, *, expected_frontmost_pid=None):
            """Apply the same inert native guard without holding any real key."""
            self.assertEqual(duration, 0.05)
            await deliver(key, pid, expected_frontmost_pid=expected_frontmost_pid)

        self.server.computer.raise_window = AsyncMock(side_effect=raise_window)
        self.server.computer.is_frontmost_app.side_effect = is_frontmost
        self.server._await_frontmost = AsyncMock(side_effect=await_frontmost)
        self.server.computer.press_key = AsyncMock(side_effect=deliver)
        self.server.computer.press_keys = AsyncMock(side_effect=deliver)
        self.server.computer.hold_key = AsyncMock(side_effect=hold)

    async def press(self, *, tool_name="press_key", **arguments):
        """Use the real MCP dispatcher with one trusted inert target session."""
        if tool_name == "hold_key":
            arguments["duration"] = 0.05
        return payload(await self.server.call_tool(tool_name, {"app": "Fixture", **arguments}))

    def assert_no_input(self):
        """Refusals cannot enter either native keyboard boundary or activation fallback."""
        self.server.computer.press_key.assert_not_awaited()
        self.server.computer.press_keys.assert_not_awaited()
        self.server.computer.hold_key.assert_not_awaited()
        self.server._await_frontmost.assert_not_awaited()
        self.assertFalse(any(event[0] == "input" for event in self.events))

    def prepare_visible_input(self):
        """Use inert geometry, resolved endpoints and mouse delivery for visible fallback branches."""
        self.session.win_x, self.session.win_y = 10, 20
        self.session.width, self.session.height = 100, 80
        self.server._refresh_window = AsyncMock()
        self.server._check_click_safety = AsyncMock(return_value=(True, None))
        self.server._check_semantic_drag_endpoint = AsyncMock(return_value=(True, None))
        self.server._nearby_ax_hint = AsyncMock(return_value=None)
        self.server.skylight.is_available.return_value = False

        async def endpoint(session, query, *selectors, **options):
            """Provide two observed in-window AX targets without any native traversal."""
            return {"ok": True, "via": "ax", "elem": {
                "role": "AXButton", "label": query, "x": 30 if query == "Source" else 50,
                "y": 40, "width": 10, "height": 10,
            }}

        async def mouse_input(*coordinates, **options):
            """Record mouse delivery only after a fresh readiness check restores the target."""
            self.assertTrue(self.active, "Window focus invalidated the earlier delivery gate")
            self.events.append(("mouse_input",))

        self.server._resolve_label_in_window = AsyncMock(side_effect=endpoint)
        for name in ("click", "double_click", "triple_click", "long_press", "drag"):
            setattr(self.server.computer, name, AsyncMock(side_effect=mouse_input))
        self.server.computer.type_text_char_by_char = AsyncMock(
            side_effect=self.server.computer.press_key.side_effect,
        )
        self.server.computer.ax_value_at_detailed.return_value = ("Second", "ok")

    def visible_cases(self):
        """Return the finite visible fallbacks whose last focus operation precedes delivery."""
        return (
            ("click", {"x": 20, "y": 30}),
            ("double_click", {"x": 20, "y": 30}),
            ("triple_click", {"x": 20, "y": 30}),
            ("long_press", {"x": 20, "y": 30, "duration": 0.1}),
            ("drag", {"x1": 20, "y1": 30, "x2": 40, "y2": 40}),
            ("drag_to_element", {"source_label": "Source", "target_label": "Target"}),
        )

    async def test_window_focus_precedes_final_readiness_for_single_sequence_and_repeat(self):
        """A focus side effect must be repaired by a fresh readiness check before input."""
        cases = (
            ("press_key", {"key": "Cmd+S"}, False),
            ("press_key", {"keys": ["Cmd+S", "a"]}, False),
            ("press_key", {"key": "Cmd+S", "repeat": 2}, False),
            ("press_key", {"key": "Return"}, True),
            ("hold_key", {"key": "Cmd+S"}, False),
            ("hold_key", {"key": "Return"}, True),
        )
        for tool_name, arguments, chromium in cases:
            with self.subTest(tool=tool_name, arguments=arguments, chromium=chromium):
                self.events.clear()
                self.active = True
                self.chromium = chromium
                self.server._await_frontmost.reset_mock()
                result = await self.press(tool_name=tool_name, **arguments)
                self.assertTrue(result["ok"])
                self.assertEqual([event[0] for event in self.events], [
                    "focus", "foreground_check", "ready", "native_guard", "input",
                ])
                self.assertEqual(self.events[1], ("foreground_check", 42, False))
                self.assertEqual(self.events[3], ("native_guard", 42))
                self.server._await_frontmost.assert_awaited_once_with(self.session)

    async def test_explicit_selected_window_is_focused_before_delivery_gate(self):
        """An explicit window selector keeps its target while receiving the final readiness check."""
        result = await self.press(key="Cmd+S", window_id=91)
        self.assertTrue(result["ok"])
        self.server.computer.raise_window.assert_awaited_once_with(42, 91)
        self.assertEqual(self.events[0], ("focus", 42, 91))
        self.assertEqual(self.events[1], ("foreground_check", 42, False))
        self.server.computer.press_key.assert_awaited_once_with(
            "Cmd+S", 42, expected_frontmost_pid=42,
        )

    async def test_background_non_key_window_refuses_before_readiness_or_input(self):
        """Background focus refusal must not activate, check away its refusal or post keys."""
        self.session.mode = "background"
        self.server.computer.is_window_key.return_value = False
        self.server._ensure_key_delivery = AsyncMock(wraps=self.server._ensure_key_delivery)
        for tool_name in ("press_key", "hold_key"):
            with self.subTest(tool=tool_name):
                result = await self.press(tool_name=tool_name, key="Cmd+S")
                self.assertTrue(result["requires_foreground"])
                self.assertEqual(result["via"], "background_no_activate")
                self.server.computer.is_window_key.assert_called_with(42, 77)
        self.server.computer.raise_window.assert_not_awaited()
        self.server._ensure_key_delivery.assert_not_awaited()
        self.server.computer.is_frontmost_app.assert_not_called()
        self.assert_no_input()

    async def test_background_key_window_still_requires_frontmost_command_delivery(self):
        """A key window is not permission to route a command through another app's menu bar."""
        self.session.mode = "background"
        self.active = False
        self.server.computer.is_window_key.return_value = True
        for tool_name in ("press_key", "hold_key"):
            with self.subTest(tool=tool_name):
                result = await self.press(tool_name=tool_name, key="Cmd+S")
                self.assertTrue(result["requires_foreground"])
                self.assertEqual(result["reason"], "command_shortcut_needs_frontmost")
                self.server.computer.is_window_key.assert_called_with(42, 77)
        self.server.computer.raise_window.assert_not_awaited()
        self.assertEqual(self.server.computer.is_frontmost_app.call_count, 2)
        self.server.computer.is_frontmost_app.assert_called_with(42)
        self.assert_no_input()

    async def test_last_instant_foreground_change_refuses_without_retry(self):
        """Ordering repairs readiness but never suppresses a later native guard or retries input."""
        self.race_before_input = True
        for tool_name in ("press_key", "hold_key"):
            with self.subTest(tool=tool_name):
                self.events.clear()
                self.active = True
                self.server._await_frontmost.reset_mock()
                result = await self.press(tool_name=tool_name, key="Cmd+S")
                self.assertFalse(result["ok"])
                self.assertIn("no event was posted", result["error"])
                self.assertEqual([event[0] for event in self.events], [
                    "focus", "foreground_check", "ready", "native_guard",
                ])
                self.server._await_frontmost.assert_awaited_once_with(self.session)
        self.server.computer.press_key.assert_awaited_once_with(
            "Cmd+S", 42, expected_frontmost_pid=42,
        )
        self.server.computer.press_keys.assert_not_awaited()
        self.server.computer.hold_key.assert_awaited_once_with(
            "Cmd+S", 0.05, 42, expected_frontmost_pid=42,
        )

    async def test_failed_window_focus_stops_before_readiness_or_input(self):
        """An autonomous focus failure cannot be masked by a successful earlier foreground check."""
        self.server.computer.raise_window.return_value = {"ok": False, "focused": False}
        self.server.computer.raise_window.side_effect = None
        self.server._ensure_key_delivery = AsyncMock(wraps=self.server._ensure_key_delivery)
        for tool_name in ("press_key", "hold_key"):
            with self.subTest(tool=tool_name):
                result = await self.press(tool_name=tool_name, key="Cmd+S")
                self.assertFalse(result["ok"])
        self.server._ensure_key_delivery.assert_not_awaited()
        self.server.computer.is_frontmost_app.assert_not_called()
        self.assert_no_input()

    async def test_plain_native_key_without_window_retains_invisible_delivery(self):
        """Ordinary PID-targeted native keys still require no foreground activation."""
        self.session.window_id = 0
        self.active = False
        result = await self.press(key="Return")
        self.assertTrue(result["ok"])
        self.server.computer.raise_window.assert_not_awaited()
        self.server.computer.is_frontmost_app.assert_not_called()
        self.server._await_frontmost.assert_not_awaited()
        self.server.computer.press_key.assert_awaited_once_with(
            "Return", 42, expected_frontmost_pid=None,
        )

    async def test_visible_mouse_fallbacks_recheck_readiness_after_last_focus(self):
        """Both visible modes retain focus call counts and refresh readiness before mouse input."""
        self.prepare_visible_input()
        for mode in ("autonomous", "humanoid"):
            self.session.mode = mode
            for tool_name, arguments in self.visible_cases():
                with self.subTest(mode=mode, tool=tool_name):
                    self.events.clear()
                    self.active = True
                    self.server._await_frontmost.reset_mock()
                    self.server.computer.raise_window.reset_mock()
                    result = payload(await self.server.call_tool(tool_name, {
                        "app": "Fixture", **arguments,
                    }))
                    self.assertTrue(result["ok"])
                    self.assertEqual([event[0] for event in self.events], [
                        "focus", "foreground_check", "ready", "mouse_input",
                    ])
                    self.server.computer.raise_window.assert_awaited_once_with(42, 77)
                    self.server._await_frontmost.assert_awaited_once_with(self.session)

        # Explicit click already performed two raises; keep both, then check
        # readiness after the last one instead of inserting another raise.
        self.events.clear()
        self.active = True
        self.server.computer.raise_window.reset_mock()
        result = payload(await self.server.call_tool("click", {
            "app": "Fixture", "window_id": 77, "x": 20, "y": 30,
        }))
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.computer.raise_window.await_count, 2)
        self.assertEqual([event[0] for event in self.events], [
            "focus", "focus", "foreground_check", "ready", "mouse_input",
        ])

    async def test_native_option_rechecks_readiness_before_click_and_guarded_selection(self):
        """Native option input keeps its exact readback and final PID guards after window focus."""
        self.prepare_visible_input()
        result = payload(await self.server.call_tool("select_option", {
            "app": "Fixture", "x": 20, "y": 30, "option": "Second",
        }))
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual([event[0] for event in self.events], [
            "focus", "foreground_check", "ready", "mouse_input",
            "native_guard", "input", "native_guard", "input",
        ])
        self.server.computer.raise_window.assert_awaited_once_with(42, 77)
        self.server._await_frontmost.assert_awaited_once_with(self.session)
        self.server.computer.type_text_char_by_char.assert_awaited_once_with(
            "Second", 42, expected_frontmost_pid=42,
        )
        self.server.computer.press_key.assert_awaited_once_with(
            "Return", 42, expected_frontmost_pid=42,
        )
        self.assertEqual(self.server._refresh_window.await_count, 2)
        self.assertEqual(self.server._check_click_safety.await_count, 2)

    async def test_visible_background_refusals_precede_focus_gate_and_input(self):
        """Every visible fallback keeps background refusal ahead of activation and event delivery."""
        self.prepare_visible_input()
        self.session.mode = "background"
        self.server._ensure_key_delivery = AsyncMock(wraps=self.server._ensure_key_delivery)
        cases = self.visible_cases() + (("select_option", {"x": 20, "y": 30, "option": "Second"}),)
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                result = payload(await self.server.call_tool(tool_name, {"app": "Fixture", **arguments}))
                self.assertTrue(result["requires_foreground"])
        self.server._ensure_key_delivery.assert_not_awaited()
        self.server.computer.raise_window.assert_not_awaited()
        self.server.computer.type_text_char_by_char.assert_not_awaited()
        for name in ("click", "double_click", "triple_click", "long_press", "drag"):
            getattr(self.server.computer, name).assert_not_awaited()
        self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
