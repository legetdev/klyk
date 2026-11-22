"""Keys typed into a busy browser or Electron field must land in order, once each.

Recorded hosted-VM failures showed VS Code receiving "Electron vfied 🧭eri" and
"Eletron verified 🧭c" for "Electron verified 🧭": keys queued faster than the
renderer applied them, and the editor's text-area sync put later keys first.
These tests model that renderer without any desktop access.
"""

import asyncio
import ctypes
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from server_support import load_server, payload
from test_input_cleanup import load_functions


class BusyRenderer:
    """Applies one queued key per observation; two queued keys land swapped, like a stale editor sync."""

    def __init__(self, value="", reflects=True):
        self.value = value
        self.queue = []
        self.max_backlog = 0
        self.reflects = reflects
        self.keys = []

    def key(self, char):
        self.keys.append(char)
        self.queue.append(char)
        self.max_backlog = max(self.max_backlog, len(self.queue))

    def observe(self, pid=None):
        if len(self.queue) >= 2:
            first, second = self.queue[0], self.queue[1]
            self.value += second + first
            del self.queue[:2]
        elif self.queue:
            self.value += self.queue.pop(0)
        return self.value if self.reflects else None

    def drain(self):
        while self.queue:
            self.observe()
        return self.value


def typing_namespace(renderer, check_stop=lambda: None):
    """Load the real typing loop with key delivery and field readback bound to the model."""
    def unicode_event(units_pointer, units, is_down, pid):
        if is_down:
            renderer.key(bytes(units_pointer).decode("utf-16-le"))

    ns = {
        "asyncio": asyncio,
        "ctypes": ctypes,
        "time": SimpleNamespace(sleep=lambda _: None, monotonic=time.monotonic),
        "_input_lock": asyncio.Lock(),
        "_check_stop": check_stop,
        "is_frontmost_app": lambda pid: True,
        "char_to_keycode": lambda char: (ord(char), 0) if char.isascii() else (None, 0),
        "_press_key_sync": lambda keycode, flags, pid: renderer.key(chr(keycode)),
        "_begin_input": lambda token, down, up: down(),
        "_finish_input": lambda token: None,
        "_send_unicode_event": unicode_event,
        "_focused_field_state": renderer.observe,
        "_TYPING_FIRST_CONFIRM_SECONDS": 0.05,
        "_TYPING_CONFIRM_SECONDS": 0.2,
    }
    load_functions("computer.py", {"type_text_char_by_char"}, ns)
    return ns


class WebTypingPaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_renderer_receives_each_key_only_after_the_previous_one_landed(self):
        renderer = BusyRenderer()
        ns = typing_namespace(renderer)
        landed = await ns["type_text_char_by_char"]("Electron verified 🧭", 42, confirm_pid=42)
        self.assertEqual(renderer.drain(), "Electron verified 🧭")
        self.assertEqual(renderer.max_backlog, 1)
        self.assertTrue(landed)

    async def test_unpaced_keys_reproduce_the_recorded_reordering(self):
        """Control: without confirmation the same renderer garbles the text, as on hosted VMs."""
        renderer = BusyRenderer()
        ns = typing_namespace(renderer)
        landed = await ns["type_text_char_by_char"]("verified", 42)
        self.assertNotEqual(renderer.drain(), "verified")
        self.assertFalse(landed)

    async def test_field_that_never_reflects_typing_costs_one_short_wait_and_sends_each_key_once(self):
        renderer = BusyRenderer(reflects=False)
        ns = typing_namespace(renderer)
        started = time.monotonic()
        landed = await ns["type_text_char_by_char"]("abcdef", 42, confirm_pid=42)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(renderer.keys, list("abcdef"))
        self.assertFalse(landed)

    async def test_field_that_stops_reflecting_midway_falls_back_without_resending(self):
        renderer = BusyRenderer()
        original = renderer.observe
        def observe(pid=None):
            value = original(pid)
            return value if len(renderer.keys) <= 3 else value[:3]
        renderer.observe = observe
        ns = typing_namespace(renderer)
        ns["_focused_field_state"] = observe
        landed = await ns["type_text_char_by_char"]("abcdef", 42, confirm_pid=42)
        self.assertEqual(renderer.keys, list("abcdef"))
        self.assertFalse(landed)

    async def test_emergency_stop_still_halts_paced_typing_before_the_next_key(self):
        renderer = BusyRenderer()
        check = MagicMock(side_effect=[None, None, RuntimeError("stopped")])
        ns = typing_namespace(renderer, check_stop=check)
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await ns["type_text_char_by_char"]("abc", 42, confirm_pid=42)
        self.assertEqual(renderer.keys, ["a"])


class CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


class AutocompleteField:
    """An address bar: the shown value stays the completed URL, only the caret moves."""

    def __init__(self):
        self.keys = []
        self.applied = 0

    def key(self, char):
        self.keys.append(char)

    def observe(self, pid=None):
        self.applied = len(self.keys)
        return f"google.com\0{self.applied},{10 - self.applied}"


class FieldStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_autocompletion_is_paced_by_the_caret_without_stalling(self):
        field = AutocompleteField()
        ns = typing_namespace(field)
        ns["_TYPING_FIRST_CONFIRM_SECONDS"] = 5.0
        ns["_TYPING_CONFIRM_SECONDS"] = 5.0
        started = time.monotonic()
        landed = await ns["type_text_char_by_char"]("goog", 42, confirm_pid=42)
        self.assertTrue(landed)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(field.keys, list("goog"))

    def _state_namespace(self, attributes):
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 11

        def read_attr(element, attr):
            return {b"AXFocusedUIElement": 21, b"AXSelectedTextRange": 31}.get(attr, 0) if attr in attributes else 0

        def range_value(pointer, kind, output):
            output._obj.location, output._obj.length = 3, 0
            return True

        api.AXValueGetValue.side_effect = range_value
        ns = {
            "ctypes": ctypes,
            "_appserv": api,
            "_cf": MagicMock(),
            "_ax_read_attr_ptr": read_attr,
            "_ax_str_attr": lambda element, attr: attributes.get(attr, ""),
            "_FIELD_TEXT_ROLES": frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}),
            "_kAXValueCFRangeType": 4,
            "CFRange": CFRange,
        }
        load_functions("computer.py", {"_focused_field_state"}, ns)
        return ns

    def test_field_state_includes_value_and_caret(self):
        ns = self._state_namespace({b"AXFocusedUIElement": 1, b"AXSelectedTextRange": 1,
                                    b"AXRole": "AXTextField", b"AXValue": "abc"})
        self.assertEqual(ns["_focused_field_state"](42), "abc\x003,0")

    def test_secure_and_non_text_focus_is_never_read(self):
        for attributes in ({b"AXFocusedUIElement": 1, b"AXRole": "AXTextField", b"AXSubrole": "AXSecureTextField",
                            b"AXValue": "secret"},
                           {b"AXFocusedUIElement": 1, b"AXRole": "AXButton", b"AXValue": "x"},
                           {}):
            with self.subTest(attributes=attributes):
                self.assertIsNone(self._state_namespace(attributes)["_focused_field_state"](42))


class WebTypingDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = load_server()
        self.session = SimpleNamespace(
            app="Klyk Electron Fixture", pid=123, window_id=77, win_x=0, win_y=0,
            width=100, height=80, mode="autonomous", windowless=False,
            screenshots_taken=0, ax_warmup_attempted=False, last_mutation_at=0.0,
        )
        self.server._get_session = AsyncMock(return_value=(self.session, False))
        self.server._focus_if_needed = AsyncMock(return_value=None)
        self.server._ensure_key_delivery = AsyncMock(return_value=None)
        self.server.ownership.is_owner.return_value = True
        self.server.computer.type_text_char_by_char = AsyncMock(return_value=True)

    async def test_chromium_keys_typing_is_paced_by_the_field(self):
        self.server._is_chromium_based = lambda session: True
        result = payload(await self.server.call_tool(
            "type_text", {"app": "Klyk Electron Fixture", "text": "hi", "mode": "keys"}))
        self.assertTrue(result["ok"])
        self.server.computer.type_text_char_by_char.assert_awaited_once_with(
            "hi", 123, expected_frontmost_pid=123, confirm_pid=123)

    async def test_native_keys_typing_stays_unpaced(self):
        self.server._is_chromium_based = lambda session: False
        result = payload(await self.server.call_tool(
            "type_text", {"app": "Klyk Electron Fixture", "text": "hi", "mode": "keys"}))
        self.assertTrue(result["ok"])
        self.server.computer.type_text_char_by_char.assert_awaited_once_with(
            "hi", 123, expected_frontmost_pid=None, confirm_pid=None)


if __name__ == "__main__":
    unittest.main()
