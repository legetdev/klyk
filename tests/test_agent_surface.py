"""Agent-facing surface: model-ready frames, familiar arguments, element numbers and routing."""
import json
import math
import os
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from mcp import types

from klyk import elements, frames, keycodes, surface
from server_support import load_server, payload


def tiles(width, height):
    """Provider tile count for an image."""
    return math.ceil(width / 28) * math.ceil(height / 28)


class FrameTests(unittest.TestCase):
    """Images fit every mainstream model unchanged, so coordinates round-trip."""

    def test_small_windows_are_unchanged(self):
        for size in ((400, 600), (1024, 768), (1280, 720)):
            with self.subTest(size=size):
                self.assertEqual(frames.fit(*size), size)
                self.assertTrue(frames.Frame.for_window(*size).identity)

    def test_large_windows_fit_the_standard_tier_with_their_aspect(self):
        for width, height in ((1920, 1080), (1470, 956), (2560, 1440), (3456, 2234), (1080, 1920), (5000, 300)):
            with self.subTest(size=(width, height)):
                fitted = frames.fit(width, height)
                self.assertLessEqual(max(fitted), 1568)
                self.assertLessEqual(tiles(*fitted), 1568)
                self.assertAlmostEqual(fitted[0] / fitted[1], width / height, delta=0.02 * width / height)
                # The fit is tight: one more pixel on the long edge would not fit.
                bigger = (fitted[0] + 2, round((fitted[0] + 2) * height / width)) if width >= height else \
                    (round((fitted[1] + 2) * width / height), fitted[1] + 2)
                self.assertFalse(max(bigger) <= 1568 and tiles(*bigger) <= 1568 and bigger[0] <= width)

    def test_high_tier_is_opt_in(self):
        with mock.patch.dict(os.environ, {"KLYK_IMAGE_TIER": "high"}):
            self.assertEqual(frames.fit(1920, 1080), (1920, 1080))
            self.assertLessEqual(tiles(*frames.fit(3456, 2234)), 4784)
        with mock.patch.dict(os.environ, {"KLYK_IMAGE_TIER": "nonsense"}):
            self.assertLess(frames.fit(1920, 1080)[0], 1920)

    def test_invalid_sizes_are_refused(self):
        for size in ((0, 10), (10, -1), (True, 10)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                frames.fit(*size)

    def test_inputs_convert_to_logical_points(self):
        frame = frames.Frame.for_window(1920, 1080)
        args = {"app": "A", "x": frame.image_width / 2, "y": frame.image_height / 2}
        logical = frames.args_to_logical("click", args, frame)
        self.assertAlmostEqual(logical["x"], 960, delta=1)
        self.assertAlmostEqual(logical["y"], 540, delta=1)
        self.assertEqual(args["x"], frame.image_width / 2, "the caller's arguments are not mutated")
        drag = frames.args_to_logical("drag", {"x1": 0, "y1": 0, "x2": frame.image_width, "y2": frame.image_height}, frame)
        self.assertEqual((round(drag["x2"]), round(drag["y2"])), (1920, 1080))
        pixels = frames.args_to_logical("get_pixels", {"points": [{"x": 10, "y": 10}],
                                                       "regions": [{"x": 0, "y": 0, "width": 100, "height": 50}]}, frame)
        self.assertGreater(pixels["points"][0]["x"], 10)
        self.assertGreater(pixels["regions"][0]["width"], 100)
        zoom = frames.args_to_logical("zoom", {"region": [0, 0, frame.image_width, frame.image_height]}, frame)
        self.assertEqual(zoom["region"], [0, 0, 1920, 1080])
        template = frames.args_to_logical("get_template", {"x1": 1, "y1": 1, "x2": 50, "y2": 50}, frame)
        self.assertTrue(all(isinstance(template[k], int) for k in ("x1", "y1", "x2", "y2")))

    def test_identity_and_screen_tools_pass_through(self):
        frame = frames.Frame.for_window(1920, 1080)
        args = {"x": 5, "y": 5}
        self.assertIs(frames.args_to_logical("click", args, frames.Frame.for_window(400, 300)), args)
        self.assertIs(frames.args_to_logical("set_window_bounds", args, frame), args)
        self.assertIs(frames.args_to_logical("click", args, None), args)

    def test_results_convert_window_geometry_only(self):
        frame = frames.Frame.for_window(1920, 1080)
        result = {
            "width": 1920, "height": 1080, "win_x": 100, "win_y": 50,
            "ax_elements": [{"element": 3, "x": 960, "y": 540, "width": 100, "height": 20}],
            "focus_warning": {"window_id": 7, "x": 999},
            "_meta": {"duration_ms": 12},
            "box": [0, 0, 1920, 1080], "size": [1920, 1080],
        }
        converted = frames.result_to_image("inspect", result, frame)
        self.assertEqual((converted["width"], converted["height"]), (frame.image_width, frame.image_height))
        self.assertEqual((converted["win_x"], converted["win_y"]), (100, 50))
        self.assertEqual(converted["ax_elements"][0]["element"], 3)
        self.assertEqual(converted["ax_elements"][0]["x"], frame.image_width // 2)
        self.assertEqual(converted["focus_warning"], result["focus_warning"])
        self.assertEqual(converted["_meta"], result["_meta"])
        self.assertEqual(converted["box"], [0, 0, frame.image_width, frame.image_height])
        self.assertEqual(converted["size"], [frame.image_width, frame.image_height])
        self.assertIs(frames.result_to_image("list_windows", result, frame), result)

    def test_round_trip_lands_on_the_same_logical_point(self):
        frame = frames.Frame.for_window(2560, 1440)
        for logical_x, logical_y in ((0, 0), (1279, 719), (2559, 1439), (37, 1001)):
            image_x, image_y = frame.to_image(logical_x, "x"), frame.to_image(logical_y, "y")
            back = frames.args_to_logical("click", {"x": image_x, "y": image_y}, frame)
            self.assertLessEqual(abs(back["x"] - logical_x), 1 / frame.scale)
            self.assertLessEqual(abs(back["y"] - logical_y), 1 / frame.scale)


class SurfaceTests(unittest.TestCase):
    """Familiar spellings map onto canonical arguments; canonical ones always win."""

    def test_coordinates_and_element_aliases(self):
        self.assertEqual(surface.normalize("click", {"coordinate": [3, 4]}), {"x": 3, "y": 4})
        self.assertEqual(surface.normalize("click", {"coordinate": [3, 4], "x": 9, "y": 9})["x"], 9)
        for alias in ("ref_12", "12", "e12", "[12]", 12, 12.0):
            with self.subTest(alias=alias):
                self.assertEqual(surface.normalize("click", {"ref": alias}), {"element": 12})
        self.assertEqual(surface.normalize("click", {"element_index": 5})["element"], 5)
        self.assertEqual(surface.normalize("click", {"element": "button"})["element"], "button")
        with self.assertRaises(ValueError):
            surface.parse_element(0)

    def test_click_modifiers_and_counts(self):
        self.assertEqual(surface.normalize("click", {"text": "ctrl+shift"})["modifiers"], ["ctrl", "shift"])
        self.assertEqual(surface.normalize("click", {"keys": ["SHIFT"]})["modifiers"], ["shift"])
        self.assertEqual(surface.normalize("click", {"modifiers": ["super", "Option"]})["modifiers"], ["cmd", "alt"])
        self.assertEqual(surface.normalize("click", {"mouse_button": "right", "clicks": 2}),
                         {"button": "right", "click_count": 2})
        self.assertEqual(surface.modifiers_from("Control_L+Super_R"), ["ctrl", "cmd"])
        with self.assertRaises(ValueError):
            surface.modifiers_from(["hyper"])

    def test_drag_scroll_wait_and_keys(self):
        self.assertEqual(surface.normalize("drag", {"start_coordinate": [1, 2], "coordinate": [3, 4]}),
                         {"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        self.assertEqual(surface.normalize("drag", {"path": [{"x": 1, "y": 2}, {"x": 5, "y": 6}]}),
                         {"x1": 1, "y1": 2, "x2": 5, "y2": 6})
        self.assertIn("path", surface.normalize("drag", {"path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}]}))
        self.assertEqual(surface.normalize("drag", {"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}),
                         {"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        self.assertEqual(surface.normalize("scroll", {"scroll_direction": "down", "scroll_amount": 5}),
                         {"direction": "down", "amount": 5})
        self.assertEqual(surface.normalize("scroll", {"scroll_y": -300}), {"direction": "up", "amount": 6})
        self.assertEqual(surface.normalize("scroll", {"scroll_x": 120, "scroll_y": 10})["direction"], "right")
        self.assertEqual(surface.normalize("scroll", {"direction": "down", "pages": 1.5})["amount"], 15)
        self.assertEqual(surface.normalize("wait", {"duration": 2}), {"seconds": 2})
        self.assertEqual(surface.normalize("press_key", {"text": "ctrl+s"}), {"key": "ctrl+s"})
        self.assertEqual(surface.normalize("press_key", {"keys": ["CMD", "SHIFT", "T"]}), {"key": "cmd+shift+T"})
        self.assertEqual(surface.normalize("press_key", {"keys": ["Tab", "Return"]}), {"keys": ["Tab", "Return"]})
        self.assertEqual(surface.normalize("hold_key", {"text": "w", "seconds": 2}), {"key": "w", "duration": 2})
        self.assertEqual(surface.normalize("screenshot", {"zoom": [0, 0, 5, 5]}), {"region": [0, 0, 5, 5]})

    def test_run_dialects(self):
        self.assertEqual(surface.translate_step({"type": "click", "x": 1, "y": 2, "button": "left"}),
                         {"tool": "click", "x": 1, "y": 2, "button": "left"})
        self.assertEqual(surface.translate_step({"type": "type", "text": "hi"}), {"tool": "type_text", "text": "hi"})
        self.assertEqual(surface.translate_step({"type": "keypress", "keys": ["CTRL", "A"]}),
                         {"tool": "press_key", "keys": ["CTRL", "A"]})
        self.assertEqual(surface.translate_step({"tool": "left_click", "coordinate": [1, 2]}),
                         {"tool": "click", "coordinate": [1, 2], "button": "left"})
        self.assertEqual(surface.translate_step({"tool": "triple_click", "x": 1, "y": 1})["click_count"], 3)
        self.assertEqual(surface.translate_step({"tool": "left_click_drag"})["tool"], "drag")
        self.assertEqual(surface.translate_step({"tool": "zoom", "region": [0, 0, 1, 1]})["tool"], "screenshot")
        self.assertEqual(surface.translate_step({"type": "wait"}), {"tool": "wait", "seconds": 1})
        self.assertEqual(surface.translate_step({"tool": "inspect"}), {"tool": "inspect"})
        for name in ("cursor_position", "left_mouse_down", "middle_click"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                surface.translate_step({"tool": name})


class KeyNameTests(unittest.TestCase):
    """xdotool, uppercase and macOS key names resolve to the same keys."""

    def test_named_keys(self):
        self.assertEqual(keycodes.parse_key_combo("Page_Down"), keycodes.parse_key_combo("PageDown"))
        self.assertEqual(keycodes.parse_key_combo("KP_Enter")[0], 76)
        self.assertEqual(keycodes.parse_key_combo("ENTER"), keycodes.parse_key_combo("Return"))
        self.assertEqual(keycodes.parse_key_combo("ARROWUP"), keycodes.parse_key_combo("Up"))
        self.assertEqual(keycodes.parse_key_combo("BackSpace"), keycodes.parse_key_combo("Backspace"))

    def test_modifier_aliases(self):
        cmd = keycodes.MODIFIER_FLAGS["cmd"]
        self.assertEqual(keycodes.parse_key_combo("super+c"), keycodes.parse_key_combo("cmd+c"))
        self.assertEqual(keycodes.parse_key_combo("Super_L+c")[1] & cmd, cmd)
        self.assertEqual(keycodes.parse_key_combo("meta+shift+t"), keycodes.parse_key_combo("cmd+shift+t"))
        self.assertEqual(keycodes.parse_key_combo("ctrl+minus"), keycodes.parse_key_combo("ctrl+-"))
        self.assertEqual(keycodes.parse_key_combo("minus"), keycodes.parse_key_combo("-"))
        with self.assertRaises(ValueError):
            keycodes.parse_key_combo("Super_L")


class ElementTableTests(unittest.TestCase):
    """Numbers are stable for the same native element, bounded, and never leak references."""

    def table(self, capacity=elements.CAPACITY):
        self.released = []
        return elements.ElementTable(self.released.append, lambda a, b: a % 1000 == b % 1000,
                                     lambda ref: ref % 1000, capacity=capacity)

    def test_same_element_keeps_its_number(self):
        table = self.table()
        first = [{"role": "AXButton", "label": "Save", "_ref": 1001}, {"role": "AXButton", "label": "Open", "_ref": 1002}]
        table.adopt(5, 7, first)
        self.assertEqual([e["element"] for e in first], [1, 2])
        self.assertTrue(all("_ref" not in e for e in first))
        again = [{"role": "AXButton", "label": "Open", "_ref": 2002}, {"role": "AXTextField", "_ref": 2003}]
        table.adopt(5, 7, again)
        self.assertEqual([e["element"] for e in again], [2, 3])
        self.assertEqual(self.released, [2002], "the duplicate reference is released, the original kept")
        self.assertEqual(table.get(2).ref, 1002)

    def test_other_processes_never_share_numbers(self):
        table = self.table()
        a, b = [{"_ref": 1001}], [{"_ref": 2001}]
        table.adopt(5, 7, a)
        table.adopt(6, 8, b)
        self.assertNotEqual(a[0]["element"], b[0]["element"])

    def test_capacity_evicts_oldest_and_releases(self):
        table = self.table(capacity=2)
        items = [{"_ref": 1}, {"_ref": 2}, {"_ref": 3}]
        table.adopt(5, 7, items)
        self.assertIsNone(table.get(1))
        self.assertEqual(self.released, [1])
        self.assertEqual(len(table), 2)
        self.assertIn("no longer tracked", elements.missing_message(1, "Notes", table.latest_number()))
        self.assertIn("never listed", elements.missing_message(99, "Notes", table.latest_number()))
        table.clear()
        self.assertEqual(sorted(self.released), [1, 2, 3])
        self.assertEqual(len(table), 0)

    def test_entries_without_references_are_left_unnumbered(self):
        table = self.table()
        items = [{"role": "AXGroup"}]
        table.adopt(5, 7, items)
        self.assertNotIn("element", items[0])


def text(data):
    """One JSON text content block."""
    return [types.TextContent(type="text", text=json.dumps(data))]


class PublicRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Each public call reaches exactly one handler with logical coordinates."""

    def setUp(self):
        self.s = load_server()
        self.session = SimpleNamespace(app="Fixture", pid=123, window_id=7, width=1920, height=1080,
                                       win_x=0, win_y=0, mode="autonomous", windowless=False, frames={},
                                       elements=None)
        self.s.registry.get_by_app.return_value = self.session
        self.calls = []

        async def execute(name, args):
            self.calls.append((name, dict(args)))
            return text({"ok": True, "via": "test"})

        self.s._execute_tool = execute
        self.frame = frames.Frame.for_window(1920, 1080)

    async def call(self, name, args):
        return payload(await self.s._execute_public(name, args))

    async def test_public_surface_is_mece_and_routable(self):
        names = [tool.name for tool in self.s.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 38)
        for name in names:
            self.assertTrue(name in self.s._PUBLIC_ROUTES or name in self.s._TOOL_SCHEMAS, name)
        hidden = ("click_element", "ax_snapshot", "wait_for", "get_pixel ", "get_pixel,", "drag_to_element",
                  "double_click", "triple_click", "close_apps", "list_sessions", "get_escalation_log")
        for tool in self.s.TOOLS:
            for word in hidden:
                self.assertNotIn(word, tool.description, f"{tool.name} mentions hidden {word.strip()}")

    def test_runtime_messages_name_only_public_tools(self):
        """Hints and errors returned to agents never point at tools agents cannot see."""
        import ast
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "klyk/mcp_server.py").read_text()
        tree = ast.parse(source)
        hidden = ("click_element", "ax_snapshot", "get_escalation_log", "list_sessions", "wait_for_visual",
                  "drag_to_element", "close_apps", "resume tool", "get_pixel(")
        internal_end = next(node.end_lineno for node in tree.body if isinstance(node, ast.Assign)
                            and any(getattr(t, "id", "") == "_INTERNAL_TOOLS" for t in node.targets))
        skip = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))  # Docstrings are for maintainers.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and getattr(node.func.value, "id", "") == "log":
                skip.update(id(arg) for arg in node.args)  # Private diagnostics, never returned.
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.lineno > internal_end \
                    and id(node) not in skip and " " in node.value and any(word in node.value for word in hidden):
                offenders.append((node.lineno, node.value[:80]))
        self.assertEqual(offenders, [])

    async def test_click_targets_route_to_one_handler(self):
        self.session.width, self.session.height = 800, 600
        await self.call("click", {"app": "Fixture", "label": "Save"})
        await self.call("click", {"app": "Fixture", "x": 10, "y": 20, "click_count": 2})
        await self.call("click", {"app": "Fixture", "x": 10, "y": 20, "click_count": 3, "modifiers": ["cmd"]})
        await self.call("click", {"app": "Fixture", "coordinate": [10, 20], "button": "right"})
        self.assertEqual([name for name, _ in self.calls], ["click_element", "double_click", "triple_click", "click"])
        self.assertEqual(self.calls[1][1]["x"], 10)
        self.assertEqual(self.calls[2][1]["modifiers"], ["cmd"])
        self.assertEqual(self.calls[3][1]["button"], "right")
        self.assertNotIn("button", self.calls[1][1])

    async def test_click_refuses_ambiguous_or_missing_targets(self):
        for args in ({"app": "Fixture"}, {"app": "Fixture", "label": "A", "x": 1, "y": 1},
                     {"app": "Fixture", "element": 1, "label": "A"}, {"app": "Fixture", "x": 1}):
            with self.subTest(args=args):
                result = await self.call("click", args)
                self.assertFalse(result["ok"])
                self.assertIn("exactly one target", result["error"])
        result = await self.call("click", {"app": "Fixture", "label": "A", "button": "right"})
        self.assertIn("element number", result["error"])
        self.assertEqual(self.calls, [])

    async def test_large_window_coordinates_are_converted(self):
        x, y = self.frame.image_width / 2, self.frame.image_height / 2
        await self.call("click", {"app": "Fixture", "x": x, "y": y})
        name, args = self.calls[0]
        self.assertAlmostEqual(args["x"], 960, delta=1)
        self.assertAlmostEqual(args["y"], 540, delta=1)

    async def test_results_are_reported_in_image_pixels(self):
        async def execute(name, args):
            return text({"width": 1920, "height": 1080, "ax_elements": [{"x": 960, "y": 540}]})
        self.s._execute_tool = execute
        result = await self.call("inspect", {"app": "Fixture"})
        self.assertEqual(result["width"], self.frame.image_width)
        self.assertEqual(result["ax_elements"][0]["x"], self.frame.image_width // 2)
        self.assertEqual(result["scaled_from"], [1920, 1080])

    async def test_element_targets_use_live_centers(self):
        self.session.width, self.session.height = 800, 600
        self.s._element_target = AsyncMock(return_value={"x": 100.0, "y": 50.0, "window_id": 7})
        await self.call("click", {"app": "Fixture", "element": 4, "click_count": 2})
        await self.call("scroll", {"app": "Fixture", "element": 4, "direction": "down"})
        await self.call("click", {"app": "Fixture", "ref": "ref_4"})
        self.assertEqual(self.calls[0][0], "double_click")
        self.assertEqual((self.calls[0][1]["x"], self.calls[0][1]["window_id"]), (100.0, 7))
        self.assertEqual(self.calls[1][0], "scroll")
        self.assertEqual(self.calls[2], ("click_element", {"app": "Fixture", "element": 4}))

    async def test_element_coordinates_are_never_rescaled(self):
        self.s._element_target = AsyncMock(return_value={"x": 960.0, "y": 540.0, "window_id": 7})
        await self.call("long_press", {"app": "Fixture", "element": 9})
        self.assertEqual((self.calls[0][1]["x"], self.calls[0][1]["y"]), (960.0, 540.0))

    async def test_wait_close_and_pixels_route_by_argument(self):
        await self.call("wait", {"seconds": 1})
        await self.call("wait", {"app": "Fixture", "text": "Done", "timeout": 2})
        await self.call("wait", {"app": "Fixture", "template_id": "tpl_1", "present": False})
        await self.call("close_app", {"app": "Fixture"})
        await self.call("close_app", {"apps": ["A", "B"], "app": "Fixture"})
        await self.call("get_pixels", {"app": "Fixture", "x": 3, "y": 4})
        self.assertEqual([name for name, _ in self.calls],
                         ["wait", "wait_for", "wait_for_visual", "close_app", "close_apps", "get_pixels"])
        self.assertEqual(self.calls[4][1], {"apps": ["A", "B"]})
        result = await self.call("wait", {"seconds": 1, "text": "x", "app": "Fixture"})
        self.assertIn("exactly one", result["error"])

    async def test_inspect_detail_all_and_zoom(self):
        await self.call("inspect", {"app": "Fixture", "detail": "all"})
        await self.call("screenshot", {"app": "Fixture", "region": [0, 0, 100, 100]})
        await self.call("screenshot", {"display": "main"})
        self.assertEqual([name for name, _ in self.calls], ["ax_snapshot", "zoom", "screenshot"])
        self.assertGreater(self.calls[1][1]["region"][2], 100, "zoom regions convert to logical points")

    async def test_earlier_tool_names_still_work(self):
        await self.call("click_element", {"app": "Fixture", "label": "Save"})
        await self.call("wait_for", {"app": "Fixture", "text": "x"})
        self.assertEqual([name for name, _ in self.calls], ["click_element", "wait_for"])

    async def test_first_call_of_a_fresh_server_converts_once_the_window_is_known(self):
        """No session yet: coordinates convert as the handler attaches the window, never unconverted."""
        async def execute(name, args):
            await self.s._get_session(args, name)
            self.calls.append((name, dict(args)))
            return text({"ok": True})
        self.s._execute_tool = execute
        self.s.registry.get_by_app.return_value = None
        self.s.get_or_create_session = AsyncMock(return_value=(self.session, True))
        await self.call("click", {"app": "Fixture", "x": self.frame.image_width / 2,
                                  "y": self.frame.image_height / 2})
        name, args = self.calls[0]
        self.assertAlmostEqual(args["x"], 960, delta=1)
        self.assertAlmostEqual(args["y"], 540, delta=1)
        self.assertIsNone(self.s._deferred_frame.get())

    async def test_plain_screenshots_report_their_image_size(self):
        async def execute(name, args):
            return text({"width": 1920, "height": 1080, "window_id": 7})
        self.s._execute_tool = execute
        result = await self.call("screenshot", {"app": "Fixture"})
        self.assertEqual((result["width"], result["height"]), (self.frame.image_width, self.frame.image_height))
        self.assertEqual(result["scaled_from"], [1920, 1080])
        self.assertIn(7, self.session.frames)

    async def test_drag_label_evidence_stays_in_screen_points(self):
        async def execute(name, args):
            return text({"ok": True, "source": {"x": 1500, "y": 900}, "target": {"x": 1700, "y": 950}})
        self.s._execute_tool = execute
        result = await self.call("drag", {"app": "Fixture", "source_label": "A", "target_label": "B"})
        self.assertEqual(result["source"], {"x": 1500, "y": 900})

    async def test_element_routes_ignore_window_arguments(self):
        self.s._element_target = AsyncMock(return_value={"x": 10.0, "y": 10.0, "window_id": 9, "ref": None})
        await self.call("click", {"app": "Fixture", "element": 3, "window_id": 7})
        await self.call("read_element", {"app": "Fixture", "element": 3, "window": "B"})
        self.assertEqual(self.calls[0], ("click_element", {"app": "Fixture", "element": 3}))
        self.assertEqual(self.calls[1], ("read_element", {"app": "Fixture", "element": 3}))

    def test_windowless_sessions_are_not_numbered(self):
        released = []
        self.s.computer.release_refs = lambda entries: released.extend(entries)
        windowless = SimpleNamespace(pid=5, window_id=0, windowless=True, elements=None)
        kept = [{"role": "AXDockItem", "_ref": 1}]
        result = self.s._adopt_elements(windowless, kept, [])
        self.assertNotIn("element", result[0])
        self.assertIsNone(windowless.elements)
        self.assertEqual(len(released), 1)

    async def test_validation_errors_are_short(self):
        result = await self.call("scroll", {"app": "Fixture", "direction": "sideways"})
        self.assertFalse(result["ok"])
        self.assertLess(len(result["error"]), 300)
        result = await self.call("click", {"app": "Fixture", "x": float("nan"), "y": 1})
        self.assertIn("finite", result["error"])


if __name__ == "__main__":
    unittest.main()


class ElementActionTests(unittest.IsolatedAsyncioTestCase):
    """Numbered elements act on the exact control and refuse rather than guess."""

    def setUp(self):
        self.s = load_server()
        self.session = SimpleNamespace(app="Fixture", pid=123, window_id=7, width=400, height=300, win_x=100,
                                       win_y=50, mode="autonomous", windowless=False, frames={}, elements=None,
                                       escalation_log=[], screenshots_taken=0, ax_warmup_attempted=False,
                                       ax_disabled_warned_on_inspect=False)
        self.s.registry.get_by_app.return_value = self.session
        self.s._get_session = AsyncMock(return_value=(self.session, False))
        self.s._refresh_window = AsyncMock()
        self.s.ownership.is_owner.return_value = True
        self.s.computer._check_stop.return_value = None
        native = self.s.computer
        native.refs_equal = lambda a, b: a == b
        native.ref_hash = lambda ref: ref
        self.retained = []
        native.retain_ref = lambda ref: self.retained.append(ref) or ref
        native.release_ref = MagicMock()
        native.ax_ref_state.return_value = {"role": "AXButton", "label": "Save", "value": "draft",
                                            "value_status": "ok", "x": 200.0, "y": 100.0,
                                            "width": 80.0, "height": 20.0}
        native.ax_ref_at_point.return_value = True
        self.s.capture.get_window_by_id.return_value = {"pid": 123, "x": 100, "y": 50, "width": 400,
                                                        "height": 300, "window_id": 7}
        self.s._is_chromium_based = lambda session: False
        self.s._element_table(self.session).adopt(123, 7, [{"role": "AXButton", "label": "Save", "_ref": 555}])

    def assertBalanced(self):
        """Every reference retained for a call is released by the end of it."""
        released = [call.args[0] for call in self.s.computer.release_ref.call_args_list]
        self.assertEqual(sorted(released), sorted(self.retained))

    async def test_plain_click_uses_the_elements_own_action(self):
        self.s.computer.ax_ref_perform.return_value = {"ok": True, "action": "AXPress", "status": "performed"}
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 1}))
        self.assertEqual(result["via"], "ax_action")
        self.assertEqual((result["clicked"]["x"], result["clicked"]["y"]), (140.0, 60.0))
        self.assertEqual(self.s.computer.ax_ref_perform.call_args.args[0], 555)
        self.assertBalanced()

    async def test_unsupported_action_falls_back_to_its_live_center(self):
        self.s.computer.ax_ref_perform.return_value = {"ok": False, "status": "unsupported"}
        self.s.call_tool = AsyncMock(return_value=text({"ok": True, "via": "skylight+keyed"}))
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 1}))
        self.assertEqual(result["via"], "element+skylight+keyed")
        sent = self.s.call_tool.call_args.args[1]
        self.assertEqual((sent["x"], sent["y"], sent["window_id"]), (140.0, 60.0, 7))
        self.assertBalanced()

    async def test_covered_or_vanished_elements_send_nothing(self):
        self.s.computer.ax_ref_perform.return_value = {"ok": False, "status": "unsupported"}
        self.s.computer.ax_ref_at_point.return_value = False
        self.s.call_tool = AsyncMock()
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 1}))
        self.assertFalse(result["ok"])
        self.assertIn("covered or scrolled", result["error"])
        self.s.call_tool.assert_not_awaited()
        self.s.computer.ax_ref_state.return_value = None
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 1}))
        self.assertIn("no longer exists", result["error"])
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 99}))
        self.assertIn("never listed", result["error"])
        self.assertBalanced()

    async def test_offscreen_element_is_refused(self):
        self.s.computer.ax_ref_state.return_value = {**self.s.computer.ax_ref_state.return_value, "x": 900.0}
        self.s.computer.ax_ref_perform.return_value = {"ok": False, "status": "unsupported"}
        result = payload(await self.s._execute_tool("click_element", {"app": "Fixture", "element": 1}))
        self.assertIn("outside its window", result["error"])

    async def test_read_and_actions_use_the_reference(self):
        result = payload(await self.s._execute_tool("read_element", {"app": "Fixture", "element": 1}))
        self.assertEqual((result["value"], result["status"], result["element"]), ("draft", "ok", 1))
        self.s.computer.ax_ref_perform.return_value = {"ok": False, "status": "unsupported",
                                                       "available_actions": ["AXPress"]}
        result = payload(await self.s._execute_tool("ax_action", {"app": "Fixture", "element": 1,
                                                                   "action": "AXIncrement"}))
        self.assertEqual(result["available_actions"], ["AXPress"])
        self.assertEqual(result["action"], "AXIncrement")
        self.assertBalanced()

    async def test_inspect_numbers_shown_elements_and_releases_the_rest(self):
        raw = [{"role": "AXButton", "label": f"B{i}", "x": 150 + i, "y": 60, "width": 10, "height": 10,
                "_ref": 1000 + i} for i in range(60)]
        self.s.computer.ax_snapshot.return_value = raw
        self.s._take_screenshot = AsyncMock(return_value=("", 400, 300, None))
        self.session.elements = None
        result = payload(await self.s._execute_tool("inspect", {"app": "Fixture"}))
        numbers = [element["element"] for element in result["ax_elements"]]
        self.assertEqual(numbers, list(range(1, 51)))
        self.assertEqual(list(result["ax_elements"][0])[0], "element")
        dropped = self.s.computer.release_refs.call_args.args[0]
        self.assertEqual(len(dropped), 10)
        self.s.computer.ax_snapshot.return_value = [{"role": "AXButton", "label": "B0", "x": 150, "y": 60,
                                                     "width": 10, "height": 10, "_ref": 1000}]
        result = payload(await self.s._execute_tool("inspect", {"app": "Fixture"}))
        self.assertEqual(result["ax_elements"][0]["element"], 1, "the same element keeps its number")


class SettingsNameTests(unittest.TestCase):
    """Setup messages name the pane the running macOS actually shows."""

    def test_pane_names_follow_the_macos_version(self):
        from klyk import settings_names
        for version, accessibility, screen in (("27.0.1", "Device Control and Data Access", "Screen & System Audio Recording"),
                                               ("15.4", "Accessibility", "Screen & System Audio Recording"),
                                               ("14.6", "Accessibility", "Screen Recording"),
                                               ("", "Accessibility", "Screen Recording")):
            with self.subTest(version=version), mock.patch("platform.mac_ver", return_value=(version, ("", "", ""), "")):
                self.assertEqual(settings_names.accessibility_pane(), accessibility)
                self.assertEqual(settings_names.screen_recording_pane(), screen)


class PasteHoldTests(unittest.IsolatedAsyncioTestCase):
    """The borrowed clipboard is kept until a slow app has actually pasted."""

    def load(self, seen):
        import asyncio as real_asyncio
        import time as real_time
        from test_input_cleanup import load_functions
        calls = []

        def has(pid, probe):
            calls.append((pid, probe))
            return seen(len(calls))
        ns = {"asyncio": real_asyncio, "time": real_time, "_focused_value_has": has,
              "_PASTE_CONFIRM_SECONDS": 0.3}
        load_functions("computer.py", {"_await_paste_landed"}, ns)
        return ns["_await_paste_landed"], calls

    async def test_returns_once_the_field_shows_the_text(self):
        wait, calls = self.load(lambda count: count >= 2)
        started = __import__("time").monotonic()
        await wait(42, "  \nHello world\nsecond line")
        self.assertLess(__import__("time").monotonic() - started, 0.25)
        self.assertEqual(calls[0], (42, "Hello world"))

    async def test_unconfirmed_paste_waits_the_bound(self):
        wait, calls = self.load(lambda count: False)
        started = __import__("time").monotonic()
        await wait(42, "text")
        self.assertGreaterEqual(__import__("time").monotonic() - started, 0.28)

    async def test_no_target_or_blank_text_returns_immediately(self):
        wait, calls = self.load(lambda count: False)
        await wait(None, "text")
        await wait(42, "   ")
        self.assertEqual(calls, [])


class ZoomSizingTests(unittest.TestCase):
    """Zoom never asks for a capture beyond the image limits and fits the model budget."""

    def test_zoom_plans_stay_within_limits(self):
        from klyk import image_bounds
        for size, region in (((1919, 1081), (10, 10, 60, 40)), ((1920, 1080), (0, 0, 1920, 1080)),
                             ((3000, 2000), (100, 100, 400, 300)), ((800, 600), (5, 5, 6, 6)),
                             ((5120, 2880), (0, 0, 5120, 2880)), ((400, 300), (390, 290, 900, 900))):
            with self.subTest(size=size, region=region):
                magnification, full, crop = frames.zoom_plan(*size, region, (1568, 1568))
                image_bounds.validate_image_dimensions(*full)
                self.assertLessEqual(max(crop[2], crop[3]), 1568)
                self.assertLessEqual(math.ceil(crop[2] / 28) * math.ceil(crop[3] / 28), 1568)
                self.assertLessEqual(crop[0] + crop[2], full[0])
                self.assertLessEqual(crop[1] + crop[3], full[1])
                self.assertLessEqual(magnification, 4.0)
        with self.assertRaises(ValueError):
            frames.zoom_plan(400, 300, (500, 500, 600, 600), (1568, 1568))
