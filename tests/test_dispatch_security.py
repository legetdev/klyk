"""Adversarial dispatcher regressions with no desktop, input or clipboard access."""

import asyncio
from contextlib import contextmanager
import io
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp import types
from klyk import ownership as real_ownership
from server_support import load_server, payload


class DispatchSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Exercise unchanged dispatcher code while replacing only native boundaries."""

    def setUp(self):
        """Create an isolated driver, selected window and in-memory diagnostics."""
        self.s = load_server()
        self.session = SimpleNamespace(
            app="Fixture", pid=123, window_id=7, width=100, height=80,
            win_x=0, win_y=0, mode="autonomous", windowless=False,
            last_mutation_at=0, screenshots_taken=0, ax_warmup_attempted=True,
            process_identity="fixture-start-token", template_cache={},
        )
        self.sessions = {"Fixture": self.session}
        self.s.registry.get_by_app.side_effect = self.sessions.get
        self.get_session = self.s._get_session
        self.focus_if_needed = self.s._focus_if_needed
        self.s._get_session = AsyncMock(return_value=(self.session, False))
        self.s._refresh_window = AsyncMock()
        self.s._ensure_key_delivery = AsyncMock(return_value=None)
        self.s._focus_if_needed = AsyncMock(return_value={"ok": True, "focused": True})
        self.s._is_chromium_based = lambda session: False
        self.s.ownership.is_owner.return_value = True
        self.s.computer.emergency_stop_active.return_value = False
        self.s.computer._check_stop.return_value = None
        self.s.computer.ax_perform_action_at.return_value = {"ok": True}
        self.s.computer.ax_snapshot.return_value = []
        self.s.computer.ax_search_focused.return_value = []
        self.s.computer.press_key = AsyncMock()
        self.s.computer.press_keys = AsyncMock()
        self.s.computer.click = AsyncMock()
        self.s.computer.drag = AsyncMock()
        self.s.computer.type_text = AsyncMock()
        self.s.computer.type_text_char_by_char = AsyncMock()
        self.s.ocr.is_available.return_value = True
        self.log_output = io.StringIO()
        self.s.log = logging.Logger("isolated-dispatch", level=logging.INFO)
        self.s.log.addHandler(logging.StreamHandler(self.log_output))

    async def test_malformed_app_types_never_escape_cleanup_or_touch_native_input(self):
        """Truthful validation failures cannot become unhashable registry exceptions."""
        for app in (["bad"], {"bad": "value"}, True, 12, ""):
            with self.subTest(app=app):
                result = payload(await self.s.call_tool("ax_action", {
                    "app": app, "x": 10, "y": 10, "action": "AXPress",
                }))
                self.assertFalse(result["ok"])
                self.assertEqual(self.s._call_depth, 0)
                self.assertIn("_meta", result)
        self.s.registry.get_by_app.assert_not_called()
        self.s._get_session.assert_not_awaited()
        self.s.computer._check_stop.assert_not_called()

    async def test_falsy_nonobject_arguments_do_not_become_empty_valid_requests(self):
        """Invalid transports must not claim control merely because their body is falsy."""
        for arguments in (False, 0, [], "", [1], "body"):
            with self.subTest(arguments=arguments):
                result = payload(await self.s.call_tool("take_control", arguments))
                self.assertFalse(result["ok"])
        self.s.ownership.claim_ownership.assert_not_called()

    async def test_real_ownership_refusal_keeps_structured_marker_and_zero_input(self):
        """Production file locks and lease checks must preserve the public non-owner contract."""
        self.s.ownership = real_ownership
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(real_ownership, "OWNER_PATH", Path(folder) / "owner"), \
                patch.object(real_ownership, "_MY_PID", 10101), \
                patch.object(real_ownership, "_alive", return_value=True):
            real_ownership.OWNER_PATH.write_text("20202\n")
            result = payload(await self.s.call_tool("ax_action", {
                "app": "Fixture", "x": 10, "y": 10, "action": "AXPress",
            }))
            self.assertFalse(result["ok"])
            self.assertEqual(result["blocked"], "not_active_session")
            self.assertEqual(real_ownership.OWNER_PATH.read_text(), "20202\n")
            self.s.computer.ax_perform_action_at.assert_not_called()
            self.s._get_session.assert_not_awaited()

    async def test_real_owned_request_holds_lease_during_inert_native_delivery(self):
        """Fixing the refusal must not let a successful action bypass the production lease."""
        self.s.ownership = real_ownership
        leased = []

        def action(*args, **kwargs):
            """Observe the real lease context at an inert action boundary."""
            leased.append(real_ownership._control_held.get())
            return {"ok": True}

        self.s.computer.ax_perform_action_at.side_effect = action
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(real_ownership, "OWNER_PATH", Path(folder) / "owner"), \
                patch.object(real_ownership, "_MY_PID", 10101), \
                patch.object(real_ownership, "_alive", return_value=True):
            real_ownership.OWNER_PATH.write_text("10101\n")
            result = payload(await self.s.call_tool("ax_action", {
                "app": "Fixture", "x": 10, "y": 10, "action": "AXPress",
            }))
            self.assertTrue(result["ok"])
            self.assertEqual(len(leased), 1)
            self.assertIsNotNone(leased[0])
            self.assertIsNone(real_ownership._control_held.get())

    async def test_handoff_during_real_lease_entry_remains_a_structured_no_input_refusal(self):
        """Ownership can change after the fast check; the real lease must reject that race."""
        self.s.ownership = real_ownership
        production_lease = real_ownership.control_request

        @contextmanager
        def transfer_before_entry():
            """Change only the isolated owner token before entering the unchanged native lease."""
            real_ownership.OWNER_PATH.write_text("20202\n")
            with production_lease():
                yield

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(real_ownership, "OWNER_PATH", Path(folder) / "owner"), \
                patch.object(real_ownership, "_MY_PID", 10101), \
                patch.object(real_ownership, "_alive", return_value=True), \
                patch.object(real_ownership, "control_request", transfer_before_entry):
            real_ownership.OWNER_PATH.write_text("10101\n")
            result = payload(await self.s.call_tool("ax_action", {
                "app": "Fixture", "x": 10, "y": 10, "action": "AXPress",
            }))
            self.assertEqual(result["blocked"], "not_active_session")
            self.s.computer.ax_perform_action_at.assert_not_called()
            self.s._get_session.assert_not_awaited()

    async def test_persistent_diagnostics_omit_unknown_names_and_argument_keys(self):
        """Request-controlled metadata must be private even before schema rejection."""
        secret_name = "private-name-sentinel"
        secret_key = "private-key-sentinel"
        await self.s.call_tool(secret_name, {secret_key: "unused"})
        await self.s.call_tool("ax_action", {
            "app": "Fixture", "x": 10, "y": 10, "action": "AXPress", secret_key: "unused",
        })
        diagnostics = self.log_output.getvalue()
        self.assertNotIn(secret_name, diagnostics)
        self.assertNotIn(secret_key, diagnostics)
        self.assertIn("tool: unknown", diagnostics)
        self.assertIn("tool: ax_action", diagnostics)

    async def test_focus_exception_bodies_remain_in_caller_error_only(self):
        """A native exception may contain document text and must not persist."""
        self.s.computer.raise_window = AsyncMock(side_effect=RuntimeError("private-native-sentinel"))
        with self.assertRaises(RuntimeError):
            await self.focus_if_needed(self.session, 7)
        self.assertNotIn("private-native-sentinel", self.log_output.getvalue())
        self.assertIn("RuntimeError", self.log_output.getvalue())

    async def test_click_hint_label_is_not_written_to_diagnostics(self):
        """The caller can receive a nearby AX hint without copying its label into logs."""
        self.session.mode = "humanoid"
        self.s._nearby_ax_hint = AsyncMock(return_value={"label": "private-screen-sentinel"})
        result = payload(await self.s.call_tool("click", {"app": "Fixture", "x": 10, "y": 10}))
        self.assertEqual(result["nearby_ax_hint"]["label"], "private-screen-sentinel")
        self.assertNotIn("private-screen-sentinel", self.log_output.getvalue())

    async def test_visual_poll_sleep_cannot_exceed_remaining_deadline(self):
        """A valid long polling interval cannot lock the server beyond a short timeout."""
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        self.s.matcher.find.return_value = None
        waits = []

        async def capture_wait(seconds):
            """Stop after recording the requested wait without wasting wall-clock time."""
            waits.append(seconds)
            raise asyncio.CancelledError()

        with patch.object(self.s.asyncio, "sleep", side_effect=capture_wait):
            with self.assertRaises(asyncio.CancelledError):
                await self.s.call_tool("wait_for_visual", {
                    "app": "Fixture", "template_b64": "fixture", "timeout": 0.05, "poll_interval": 30,
                })
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)
        self.assertLessEqual(waits[0], 0.05)
        self.assertEqual(self.s._call_depth, 0)

    async def test_passive_observations_never_activate_or_raise_a_humanoid_target(self):
        """Read privileges stay available after ownership loss or stop without focus changes."""
        self.session.mode = "humanoid"
        self.s.ownership.is_owner.return_value = False
        self.s.computer.emergency_stop_active.return_value = True
        self.s.computer._check_stop.side_effect = RuntimeError("stop active")
        self.s.computer.activate_app = AsyncMock()
        self.s.computer.raise_window = AsyncMock()
        self.s.capture.take_screenshot.return_value = ("pixels", 100, 80)
        for name, detail in (("screenshot", {}), ("inspect", {}), ("inspect", {"detail": "slim"})):
            for selector in ({}, {"window_id": 7}):
                with self.subTest(name=name, detail=detail, selector=selector):
                    await self.s.call_tool(name, {"app": "Fixture", **detail, **selector})
        self.s.computer.activate_app.assert_not_awaited()
        self.s.computer.raise_window.assert_not_awaited()
        self.s.computer._check_stop.assert_not_called()

    async def test_cold_observation_and_background_configuration_refuse_launch(self):
        """A cold read requires control and no stop; background configuration never launches."""
        self.s._get_session = self.get_session
        self.s.registry.get_by_app.side_effect = None
        self.s.registry.get_by_app.return_value = None
        self.s.get_or_create_session = AsyncMock(return_value=(self.session, True))
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        for name, owner, stopped, extra in (
                ("screenshot", False, False, {}), ("screenshot", True, True, {}),
                ("set_mode", True, False, {"mode": "background"})):
            with self.subTest(name=name, owner=owner, stopped=stopped):
                self.s.ownership.is_owner.return_value = owner
                self.s.computer.emergency_stop_active.return_value = stopped
                await self.s.call_tool(name, {"app": "Fixture", **extra})
                self.assertFalse(self.s.get_or_create_session.await_args.kwargs["allow_launch"])

    async def test_authorized_cold_observation_retains_control_lease_during_creation(self):
        """Observation-triggered launch must hold the same cross-process lease as input."""
        self.s._get_session = self.get_session
        self.s.registry.get_by_app.side_effect = None
        self.s.registry.get_by_app.return_value = None
        phases = []

        @contextmanager
        def control_lease():
            """Model the action lock lifetime without a filesystem ownership record."""
            phases.append("enter")
            yield
            phases.append("exit")

        async def create(app, **options):
            """Record that session creation occurs inside the permitted launch lease."""
            self.assertTrue(options["allow_launch"])
            phases.append("create")
            return self.session, True

        self.s.ownership.control_request = control_lease
        self.s.get_or_create_session = AsyncMock(side_effect=create)
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        await self.s.call_tool("screenshot", {"app": "Fixture"})
        self.assertEqual(phases, ["enter", "create", "exit"])

    async def test_background_focus_window_never_raises_a_nonkey_window(self):
        """The strict background mode also covers an explicit focus request."""
        self.s._focus_if_needed = self.focus_if_needed
        self.session.mode = "background"
        self.s.computer.is_window_key.return_value = False
        self.s.computer.raise_window = AsyncMock()
        result = payload(await self.s.call_tool("focus_window", {"app": "Fixture", "window_id": 7}))
        self.assertTrue(result["requires_foreground"])
        self.s.computer.raise_window.assert_not_awaited()

    async def test_invalid_visual_template_fails_without_a_timeout_loop(self):
        """A fixed decoder error is actionable immediately and never proves absence."""
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        self.s.matcher.find.side_effect = ValueError("invalid PNG")
        result = payload(await self.s.call_tool("wait_for_visual", {
            "app": "Fixture", "template_b64": "broken", "present": False, "timeout": 30,
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["polls"], 1)
        self.s._take_screenshot.assert_awaited_once()

    async def test_run_app_override_does_not_inherit_another_apps_window(self):
        """A documented per-step app override must reach that app without the outer window."""
        original = self.s.call_tool
        seen = []

        async def record(name, arguments):
            """Capture nested targeting at the real recursive dispatch boundary."""
            seen.append(dict(arguments))
            return [types.TextContent(type="text", text='{"ok":true}')]

        self.s.call_tool = record
        result = payload(await original("run", {"app": "Fixture", "window_id": 7, "actions": [
            {"tool": "type_text", "app": "Other", "text": "first"},
            {"tool": "press_key", "key": "a"},
        ]}))
        self.assertTrue(result["ok"])
        self.assertEqual(seen[0]["app"], "Other")
        self.assertNotIn("window_id", seen[0])
        self.assertEqual((seen[1]["app"], seen[1]["window_id"]), ("Fixture", 7))

    async def test_run_keeps_semantic_cross_app_drag_evidence(self):
        """Targets, route and cross-app evidence cannot collapse into a bare acknowledgement."""
        original = self.s.call_tool
        evidence = {"ok": True, "source": {"label": "A"}, "target": {"label": "B"},
                    "cross_app": True, "target_app": "Other", "via": "cursor_warp"}
        self.s.call_tool = AsyncMock(return_value=[types.TextContent(type="text", text=json.dumps(evidence))])
        result = payload(await original("run", {"app": "Fixture", "actions": [
            {"tool": "drag_to_element", "source_label": "A", "target_label": "B", "target_app": "Other"},
        ]}))
        self.assertEqual(result["results"][0]["result"], evidence)
        self.assertNotIn("count", result["results"][0])

    async def test_run_compaction_preserves_route_and_distinct_window_labels(self):
        """Different target labels or delivery paths are distinct actions in the batch report."""
        original = self.s.call_tool
        self.s.call_tool = AsyncMock(side_effect=[
            [types.TextContent(type="text", text='{"ok":true,"via":"skylight"}')],
            [types.TextContent(type="text", text='{"ok":true,"via":"cursor_warp"}')],
            [types.TextContent(type="text", text='{"ok":true,"via":"cursor_warp"}')],
        ])
        result = payload(await original("run", {"app": "Fixture", "actions": [
            {"tool": "click", "window": "A", "x": 10, "y": 10},
            {"tool": "click", "window": "A", "x": 20, "y": 10},
            {"tool": "click", "window": "B", "x": 20, "y": 10},
        ]}))
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual([r["result"]["via"] for r in result["results"]], ["skylight", "cursor_warp", "cursor_warp"])
        self.assertEqual([r["window"] for r in result["results"]], ["A", "A", "B"])

    async def test_excessive_nested_run_is_rejected_before_any_step(self):
        """Nested batches share an aggregate budget and cannot recurse unboundedly."""
        for excessive_depth in (False, True):
            with self.subTest(excessive_depth=excessive_depth):
                actions = [{"tool": "wait", "seconds": 0}] * (1 if excessive_depth else 500)
                if excessive_depth:
                    for _ in range(8):
                        actions = [{"tool": "run", "actions": actions}]
                else:
                    actions = [{"tool": "run", "actions": actions}, {"tool": "run", "actions": actions}]
                original = self.s.call_tool
                nested = AsyncMock()
                self.s.call_tool = nested
                result = payload(await original("run", {"app": "Fixture", "actions": actions}))
                self.assertFalse(result["ok"])
                nested.assert_not_awaited()
                self.s.call_tool = original

    async def test_copied_child_context_cannot_bypass_request_lock(self):
        """A child task inherits context, but only the actual owning task may re-enter."""
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        children = []

        async def resolve(arguments, tool):
            """Launch a peer from inside a request to reproduce ContextVar inheritance."""
            if arguments["x"] == 10:
                children.append(asyncio.create_task(self.s.call_tool("ax_action", {
                    "app": "Fixture", "x": 20, "y": 20, "action": "AXPress",
                })))
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
            return self.session, False

        self.s._get_session.side_effect = resolve
        first = asyncio.create_task(self.s.call_tool("ax_action", {
            "app": "Fixture", "x": 10, "y": 10, "action": "AXPress",
        }))
        await first_started.wait()
        await asyncio.sleep(0)
        self.assertFalse(second_started.is_set())
        release_first.set()
        results = await asyncio.gather(first, children[0])
        self.assertTrue(second_started.is_set())
        self.assertTrue(all("_meta" in payload(r) for r in results))
        self.assertEqual(self.s._call_depth, 0)

    async def test_partial_resize_preserves_omitted_dimension(self):
        """Width-only and height-only requests resize instead of silently becoming a move."""
        for dimension, requested, expected in (("width", 55, (55, 80)), ("height", 44, (100, 44))):
            with self.subTest(dimension=dimension):
                self.s.computer.set_window_bounds_by_id.reset_mock()
                result = payload(await self.s.call_tool("set_window_bounds", {
                    "app": "Fixture", "window_id": 7, "x": 0, "y": 0, dimension: requested,
                }))
                self.assertTrue(result["ok"])
                self.s.computer.set_window_bounds_by_id.assert_called_once_with(123, 7, 0, 0, *expected)

    async def test_native_drag_rechecks_bounds_after_its_final_refresh(self):
        """A window shrinking between grounding and native delivery cannot redirect a drag."""
        refreshes = 0

        async def shrink(*args, **kwargs):
            """Change bounds at the exact native-path refresh after the initial safety check."""
            nonlocal refreshes
            refreshes += 1
            if refreshes == 2:
                self.session.width = 5

        self.s._refresh_window.side_effect = shrink
        self.s._seamless_drag = AsyncMock(return_value={"ok": True})
        result = payload(await self.s.call_tool("drag", {"app": "Fixture", "x1": 1, "y1": 1, "x2": 10, "y2": 10}))
        self.assertFalse(result["ok"])
        self.s._seamless_drag.assert_not_awaited()
        self.s.computer.drag.assert_not_awaited()

    async def test_read_grid_short_results_leave_unknown_samples_null(self):
        """Missing color/text samples must not invent black or raise a short-list exception."""
        self.s.capture.get_pixels_in_rects.return_value = [(10, 20, 30)]
        self.s.computer.ax_grid_text.return_value = ["A"]
        result = payload(await self.s.call_tool("read_grid", {
            "app": "Fixture", "x": 0, "y": 0, "rows": 2, "cols": 2,
            "cell_width": 20, "cell_height": 20,
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(result["cells"][0][0]["hex"], "#0a141e")
        self.assertIsNone(result["cells"][1][1]["hex"])
        self.assertIsNone(result["cells"][1][1]["text"])
        self.assertEqual(result["text_status"], "unavailable")

    async def test_read_grid_ax_failure_preserves_complete_color_evidence(self):
        """AX failure cannot discard independent, complete pixel samples."""
        self.s.capture.get_pixels_in_rects.return_value = [(10, 20, 30)] * 4
        self.s.computer.ax_grid_text.side_effect = RuntimeError("unavailable")
        result = payload(await self.s.call_tool("read_grid", {
            "app": "Fixture", "x": 0, "y": 0, "rows": 2, "cols": 2,
            "cell_width": 20, "cell_height": 20,
        }))
        self.assertTrue(result["ok"])
        self.assertEqual(result["cells"][1][1]["hex"], "#0a141e")
        self.assertIsNone(result["cells"][1][1]["text"])

    async def test_template_crop_reports_actual_clipped_bounds_and_caches(self):
        """Returned crop metadata must describe stored pixels, including edge clipping."""
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        self.s.matcher.crop.return_value = "cropped fixture"
        result = payload(await self.s.call_tool("get_template", {
            "app": "Fixture", "x1": -10, "y1": -4, "x2": 120, "y2": 90,
        }))
        self.assertEqual(result["region"], [0, 0, 100, 80])
        self.assertEqual(result["size"], [100, 80])
        self.assertEqual(result["requested_region"], [-10, -4, 120, 90])
        self.s.matcher.cache_template.assert_called_once_with(self.session.template_cache, result["template_id"], "cropped fixture")
        self.assertNotIn("template_b64", result)

    async def test_empty_template_crop_does_not_mutate_the_cache(self):
        """A crop outside the image must fail before crop or cache eviction."""
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        result = payload(await self.s.call_tool("get_template", {
            "app": "Fixture", "x1": 150, "y1": 0, "x2": 200, "y2": 10,
        }))
        self.assertFalse(result["ok"])
        self.s.matcher.crop.assert_not_called()
        self.s.matcher.cache_template.assert_not_called()

    async def test_read_text_payload_is_bounded_without_changing_raw_action_text(self):
        """Only the returned OCR observation is shortened; the native result stays intact."""
        self.s._take_screenshot = AsyncMock(return_value=("pixels", 100, 80, None))
        observations = [{"text": "x" * 250, "x": 5, "y": i, "width": 10,
                         "height": 10, "confidence": 1.0} for i in range(205)]
        self.s.ocr.recognize_all.return_value = observations
        result = payload(await self.s.call_tool("read_text", {"app": "Fixture"}))
        self.assertEqual((result["count"], result["observation_count"]), (200, 205))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["full_text"]), 12000)
        self.assertTrue(all(len(o["text"]) == 200 for o in result["observations"]))
        self.assertEqual(len(observations[0]["text"]), 250)

    async def test_partial_ocr_region_is_rejected_before_capture(self):
        """A partial region must never silently broaden recognition to the entire window."""
        self.s._take_screenshot = AsyncMock()
        result = payload(await self.s.call_tool("read_text", {"app": "Fixture", "x": 0, "y": 0}))
        self.assertFalse(result["ok"])
        self.s._take_screenshot.assert_not_awaited()
        self.s._get_session.assert_not_awaited()

    async def test_read_element_bounds_oversized_value_with_an_explicit_flag(self):
        """A shortened field prefix cannot silently claim to be the complete AX value."""
        self.s.computer.ax_value_at_detailed.return_value = ("x" * 12001, "ok")
        result = payload(await self.s.call_tool("read_element", {"app": "Fixture", "x": 10, "y": 10}))
        self.assertEqual(len(result["value"]), 12000)
        self.assertTrue(result["truncated"])

    async def test_unreadable_ax_value_is_inconclusive_instead_of_empty(self):
        """An unsupported complete value cannot verify a blank field."""
        self.s.computer.ax_value_at_detailed.return_value = (None, "unreadable_value")
        result = payload(await self.s.call_tool("read_element", {"app": "Fixture", "x": 10, "y": 10}))
        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "unreadable_value")
        self.assertIsNone(result["value"])

    def test_evidence_bounds_text_without_mutating_native_match(self):
        """Long app-controlled labels stay exact for delivery but cannot flood responses."""
        original = {"label": "x" * 1000, "value": "y" * 1000, "role": "AXButton", "x": 20, "y": 20}
        bounded = self.s._element_evidence(original)
        self.assertEqual(len(bounded["label"]), 201)
        self.assertTrue(bounded["text_truncated"])
        self.assertEqual(len(original["label"]), 1000)

    async def test_windowless_cross_app_drag_revalidates_rect_and_preserves_modifiers(self):
        """An app-scoped Dock target remains supported without raw placeholder coordinates."""
        target = SimpleNamespace(app="Dock", pid=345, window_id=0, width=0, height=0,
                                 win_x=0, win_y=0, windowless=True, process_identity="dock-start-token")
        self.s.get_or_create_session = AsyncMock(return_value=(target, False))
        source_elem = {"label": "A", "x": 10, "y": 10, "width": 20, "height": 20}
        target_elem = {"label": "Trash", "x": 110, "y": 120, "width": 30, "height": 30}
        self.s._resolve_label_in_window = AsyncMock(side_effect=[
            {"ok": True, "via": "ax", "elem": source_elem},
            {"ok": True, "via": "ax", "elem": target_elem},
            {"ok": True, "via": "ax", "elem": dict(target_elem)},
        ])
        result = payload(await self.s.call_tool("drag_to_element", {
            "app": "Fixture", "source_label": "A", "target_label": "Trash", "target_app": "Dock",
            "button": "right", "modifiers": ["alt"],
        }))
        self.assertTrue(result["ok"])
        self.assertTrue(result["cross_app"])
        self.s.computer.drag.assert_awaited_once_with(10, 10, 110, 120, hover_target_seconds=0.0,
                                                    button="right", modifiers=["alt"],
                                                    visible_targets=((123, 7), (345, None)))
        self.assertEqual([c.kwargs for c in self.s.get_or_create_session.await_args_list],
                         [{}, {"allow_launch": False}, {"allow_launch": False}])

    async def test_regular_cross_app_dispatch_reaches_visible_owner_guard_before_down(self):
        """Exercise the actual native drag coroutine after source focus with a covered destination."""
        from test_input_cleanup import load_functions
        from test_input_security import core_namespace
        target=SimpleNamespace(app='Receiver',pid=345,window_id=8,width=100,height=80,
                               win_x=100,win_y=0,windowless=False,process_identity='receiver-token')
        self.s.get_or_create_session=AsyncMock(return_value=(target,False))
        self.s._resolve_label_in_window=AsyncMock(side_effect=[
            {'ok':True,'via':'ax','elem':{'label':'A','x':10,'y':10,'width':20,'height':20}},
            {'ok':True,'via':'ax','elem':{'label':'B','x':110,'y':10,'width':20,'height':20}},
        ])
        ns,events=core_namespace();hits=[]

        def visible_hit(x,y,pid,window_id):
            """A source activation cannot turn app-scoped destination evidence into visible ownership."""
            self.s._focus_if_needed.assert_awaited_once()
            self.s._ensure_key_delivery.assert_awaited_once()
            hits.append((x,y,pid,window_id))
            return pid==123

        ns['ax_visible_target_at']=visible_hit
        load_functions('computer.py',{'drag'},ns)
        self.s.computer.drag=ns['drag']
        result=payload(await self.s.call_tool('drag_to_element',{
            'app':'Fixture','source_label':'A','target_label':'B','target_app':'Receiver',
        }))
        self.assertFalse(result['ok']);self.assertIn('No drag was sent',result['error'])
        self.assertEqual(hits,[(10.0,10.0,123,7),(110.0,10.0,345,8)])
        self.assertEqual(events,[]);self.assertEqual(ns['_held_inputs'],{})

    async def test_windowless_unknown_empty_changed_rect_and_process_refuse(self):
        """Windowless support never turns missing evidence into permission for a desktop drag."""
        for failure in ("unknown", "empty", "changed", "process"):
            with self.subTest(failure=failure):
                target = SimpleNamespace(app="Dock", pid=345, windowless=True,
                                         process_identity=None if failure == "unknown" else "dock-token")
                elem = {"label": "Trash", "x": 110, "y": 120, "width": 30, "height": 30}
                if failure == "empty":
                    elem["width"] = 0
                current = SimpleNamespace(app="Dock", pid=345, windowless=True,
                                          process_identity="different-token") if failure == "process" else target
                self.s.get_or_create_session = AsyncMock(return_value=(current, False))
                fresh = dict(elem, x=111) if failure == "changed" else dict(elem)
                self.s._resolve_label_in_window = AsyncMock(return_value={"ok": True, "via": "ax", "elem": fresh})
                safe, reason = await self.s._check_semantic_drag_endpoint(target, elem, "ax", "trash", 0, revalidate=True)
                self.assertFalse(safe)
                self.assertTrue(reason)
        self.s.computer.drag.assert_not_awaited()

    async def test_semantic_drag_default_refuses_equally_ranked_endpoints(self):
        """A default index cannot silently pick one of several identical controls."""
        self.s.computer.ax_search_focused.return_value = [
            {"label": "A", "x": 10, "y": 10}, {"label": "A", "x": 20, "y": 10},
        ]
        result = await self.s._resolve_label_in_window(self.session, "a", 0, None, None, index_explicit=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["ambiguous"])
        selected = await self.s._resolve_label_in_window(self.session, "a", 1, None, None, index_explicit=True)
        self.assertEqual(selected["elem"]["x"], 20)

    async def test_context_menu_fallback_does_not_press_an_ordinary_button(self):
        """Coincidental window buttons are not evidence that a popup menu opened."""
        self.s.skylight.post_mouse_click.return_value = True
        self.s.computer.ax_read_open_menu.return_value = []
        self.s.computer.ax_snapshot.return_value = [{"role": "AXButton", "label": "Delete", "x": 10, "y": 10}]
        result = payload(await self.s.call_tool("context_menu_select", {
            "app": "Fixture", "x": 10, "y": 10, "item_label": "Delete", "timeout": 0.2,
        }))
        self.assertFalse(result["ok"])
        self.s.computer.ax_perform_action_at.assert_not_called()
        self.s.computer.press_keys.assert_awaited_once_with(["Escape"], 123)

    async def test_context_menu_repeated_items_require_an_explicit_index(self):
        """Ambiguous menus dismiss; an explicit choice rechecks its actual label at delivery."""
        self.s.skylight.post_mouse_click.return_value = True
        self.s.computer.ax_read_open_menu.return_value = [
            {"role": "AXMenuItem", "label": "Delete", "x": 10, "y": 10},
            {"role": "AXMenuItem", "label": "Delete", "x": 20, "y": 10},
        ]
        arguments = {"app": "Fixture", "x": 10, "y": 10, "item_label": "Delete", "timeout": 0.2}
        refused = payload(await self.s.call_tool("context_menu_select", arguments))
        self.assertTrue(refused["ambiguous"])
        self.s.computer.ax_perform_action_at.assert_not_called()
        selected = payload(await self.s.call_tool("context_menu_select", dict(arguments, item_index=1)))
        self.assertTrue(selected["ok"])
        self.s.computer.ax_perform_action_at.assert_called_once_with(20, 10, "AXPress", expected_pid=123, expected_label="Delete")


if __name__ == "__main__":
    unittest.main()
