"""Portable regressions for template bounds and cancellation-safe click pairs."""

import asyncio
import ctypes
import base64
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from klyk import ownership

from test_input_cleanup import load_functions


class MatcherBoundsTests(unittest.TestCase):
    """Search rectangles must never wrap around NumPy's negative indices."""

    def setUp(self):
        """Load the real matcher with in-memory images at the codec boundary."""
        self.haystack = np.random.default_rng(7).uniform(0, 255, (8, 9, 3))
        self.needle = self.haystack[2:4, 3:5].copy()
        images = {"screen": self.haystack, "template": self.needle}
        self.namespace = {"np": np, "capture": SimpleNamespace(decode_png_to_rgb_array=images.__getitem__)}
        load_functions("matcher.py", {"find", "_match_ncc", "_window_sums", "_xcorr_valid", "_next_fast_len"}, self.namespace)

    def test_empty_or_reversed_regions_are_rejected(self):
        """Off-image negative endpoints cannot turn into a valid unrelated crop."""
        regions = [(-5, 0, -1, 8), (0, -5, 9, -1), (4, 0, 2, 8), (0, 4, 9, 2), (10, 0, 12, 8)]
        for region in regions:
            with self.subTest(region=region), self.assertRaisesRegex(ValueError, "Invalid search region"):
                self.namespace["find"]("screen", "template", search_region=region)

    def test_partial_overlap_keeps_window_relative_coordinates(self):
        """Clipping a valid overlapping region preserves exact match coordinates."""
        result = self.namespace["find"]("screen", "template", search_region=(-2, 1, 7, 20))
        self.assertEqual(result["box"], [3, 2, 5, 4])
        self.assertEqual(result["confidence"], 1.0)


class ClickCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Every posted mouse-down must have a matching release on cancellation."""

    async def test_cancel_each_click_pair_releases_same_button_and_flags(self):
        """Cancel inside every pair, checking release metadata and no later clicks."""
        for name, pairs in (("click", 1), ("double_click", 2), ("triple_click", 3)):
            for cancel_pair in range(1, pairs + 1):
                with self.subTest(name=name, pair=cancel_pair):
                    sent = []
                    cg = MagicMock()
                    cg.CGEventCreateMouseEvent.side_effect = lambda source, kind, point, button: kind
                    pauses = 0

                    async def pause(_delay):
                        """Inject cancellation at the chosen down/up gap deterministically."""
                        nonlocal pauses
                        pauses += 1
                        if pauses == 2 * cancel_pair - 1:
                            raise asyncio.CancelledError()

                    namespace = {
                        "asyncio": SimpleNamespace(sleep=pause), "ctypes": ctypes,
                        "_input_lock": asyncio.Lock(), "_check_stop": lambda: None,
                        "_modifier_flags": lambda modifiers: 8, "_cg": cg, "_post": sent.append,
                        "CGPoint": lambda **kwargs: SimpleNamespace(**kwargs),
                        "kCGEventLeftMouseDown": 1, "kCGEventLeftMouseUp": 2,
                        "kCGMouseButtonLeft": 0, "kCGMouseEventClickState": 1,
                        "kCGEventRightMouseDown": 3, "kCGEventRightMouseUp": 4,
                        "kCGMouseButtonRight": 1,
                    }
                    load_functions("computer.py", {name}, namespace)
                    kwargs = {"button": "right"} if name == "click" else {}
                    with self.assertRaises(asyncio.CancelledError):
                        await namespace[name](10, 20, modifiers=["shift"], **kwargs)
                    self.assertEqual(sent, ([3, 4] if name == "click" else [1, 2]) * cancel_pair)
                    self.assertEqual([call.args[1] for call in cg.CGEventSetFlags.call_args_list], [8] * (cancel_pair * 2))
                    if name != "click":
                        states = [call.args[2] for call in cg.CGEventSetIntegerValueField.call_args_list]
                        self.assertEqual(states, [state for state in range(1, cancel_pair + 1) for _ in range(2)])
                    self.assertFalse(namespace["_input_lock"].locked())


class CaptureFailureTests(unittest.TestCase):
    """Failed scoped captures must not expose the desktop or invalid image bytes."""

    def setUp(self):
        """Load the fallback pipeline with subprocess and native capture isolated."""
        self.namespace = {
            "_HAS_IMAGEIO": False, "os": os, "tempfile": tempfile,
            "subprocess": subprocess, "base64": base64,
            "time": SimpleNamespace(sleep=lambda _: None), "get_scale_factor": lambda: 1.0,
        }
        load_functions("capture.py", {"take_screenshot", "_parse_sips_dimensions"}, self.namespace)

    def test_scoped_failure_never_retries_full_desktop(self):
        """Both window-id and rectangle requests stop when their capture fails."""
        for kwargs in ({"window_id": 42}, {"win_x": 1, "win_y": 2, "logical_width": 20, "logical_height": 30}):
            with self.subTest(kwargs=kwargs), patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError, "requested area"):
                    self.namespace["take_screenshot"](**kwargs)
                self.assertEqual(run.call_count, 1)
                self.assertFalse(Path(run.call_args.args[0][-1]).exists())

    def test_conversion_failure_never_returns_empty_png(self):
        """A failed sips conversion propagates and cleans every temporary output."""
        paths = []

        def run(command, **kwargs):
            """Produce a valid capture and fail conversion as subprocess.run would."""
            paths.append(command[-1])
            if command[0] == "screencapture":
                Path(command[-1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 120)
                return SimpleNamespace(returncode=0)
            self.assertTrue(kwargs.get("check"))
            raise subprocess.CalledProcessError(1, command)

        with patch.object(subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                self.namespace["take_screenshot"](window_id=42, logical_width=20, logical_height=30)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_explicit_full_screen_capture_remains_supported(self):
        """An unscoped request still returns its image and measured dimensions."""
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 120

        def run(command, **kwargs):
            """Return a full-screen image followed by its measured dimensions."""
            if command[0] == "screencapture":
                self.assertNotIn("-R", command)
                self.assertNotIn("-l", command)
                Path(command[-1]).write_bytes(png)
                return SimpleNamespace(returncode=0)
            self.assertTrue(kwargs.get("check"))
            return SimpleNamespace(returncode=0, stdout="pixelWidth: 40\npixelHeight: 30\n")

        with patch.object(subprocess, "run", side_effect=run):
            encoded, width, height = self.namespace["take_screenshot"]()
        self.assertEqual(base64.b64decode(encoded), png)
        self.assertEqual((width, height), (40, 30))

    def test_missing_dimensions_are_not_replaced_with_guesses(self):
        """A missing or zero dimension cannot describe a usable screenshot."""
        for output in ("", "pixelWidth: 20", "pixelWidth: 0\npixelHeight: 30"):
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, "dimensions"):
                self.namespace["_parse_sips_dimensions"](output)


class AlertObservationTests(unittest.TestCase):
    """Completion checks can detect a save panel without changing alert defaults."""

    def test_save_panel_requires_opt_in_and_real_alerts_stay_visible(self):
        """Preserve alert reads while exposing a lingering Save As sheet on request."""
        api = MagicMock()
        api.AXUIElementCreateApplication.return_value = 1
        contents = MagicMock(return_value=(["Save As:", "Document"], ["Save", "Cancel"], True))
        namespace = {
            "ctypes": ctypes, "_appserv": api, "_cf": MagicMock(),
            "_AX_MESSAGING_TIMEOUT_SECONDS": 0.5,
            "_ax_str": lambda element, attr: "AXSheet" if attr == b"AXRole" else "",
            "_collect_sheet_contents": contents, "_ax_read_attr_ptr": lambda *args: 0,
        }
        load_functions("computer.py", {"ax_read_alert"}, namespace)
        self.assertIsNone(namespace["ax_read_alert"](42))
        panel = namespace["ax_read_alert"](42, include_save_panel=True)
        self.assertTrue(panel["save_panel"])
        self.assertEqual(panel["buttons"], ["Save", "Cancel"])
        contents.return_value = (["You do not have permission to save this file"], ["OK"], False)
        self.assertEqual(namespace["ax_read_alert"](42)["buttons"], ["OK"])


class CorruptOwnershipTests(unittest.TestCase):
    """A corrupt oversized token cannot prevent recovery of a free owner slot."""

    def test_unrepresentable_pid_is_reclaimed_without_signalling(self):
        """The OS rejects the overflowing PID before any signal can be issued."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner"
            path.write_text(str(1 << 100), encoding="utf-8")
            with patch.object(ownership, "OWNER_PATH", path):
                self.assertEqual(ownership.claim_ownership_if_unowned(), ownership._MY_PID)
                self.assertTrue(ownership.is_owner())


if __name__ == "__main__":
    unittest.main()
