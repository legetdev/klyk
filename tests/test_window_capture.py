"""Portable regressions for scoped window capture and capture-plan caching.

These tests exercise the capture boundaries with inert native adapters. They do
not import the MCP entry point or open a macOS window.
"""

import base64
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import subprocess
import struct
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_input_cleanup import enabled_gate, load_functions
from klyk.image_bounds import png_dimensions, validate_image_dimensions


class CaptureScopeTests(unittest.TestCase):
    """A requested window must remain the capture scope through every fallback."""

    def setUp(self):
        """Load only the production screenshot function with native calls isolated."""
        self.namespace = {
            "_HAS_IMAGEIO": False,
            "os": os,
            "tempfile": __import__("tempfile"),
            "subprocess": subprocess,
            "base64": base64,
            "time": SimpleNamespace(sleep=lambda _seconds: None),
            "window_capture": SimpleNamespace(),
            "_validate_image_dimensions": validate_image_dimensions, "png_dimensions": png_dimensions,
        }
        load_functions("capture.py", {"take_screenshot"}, self.namespace)

    @staticmethod
    def _png():
        """Return a minimally valid PNG payload large enough for the size guard."""
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 20, 30) + b"\0" * 120

    def test_window_id_wins_over_rectangle_in_cli_fallback(self):
        """Supplying both forms must use screencapture's window selector only."""
        commands = []

        def run(command, **kwargs):
            """Create controlled capture output and conversion dimensions."""
            commands.append(command)
            if command[0] == "/usr/sbin/screencapture":
                Path(command[-1]).write_bytes(self._png())
                return SimpleNamespace(returncode=0)
            Path(command[-1]).write_bytes(self._png())
            return SimpleNamespace(returncode=0, stdout="pixelWidth: 20\npixelHeight: 30\n")

        with patch.object(subprocess, "run", side_effect=run):
            encoded, width, height = self.namespace["take_screenshot"](
                window_id=42,
                logical_width=20,
                logical_height=30,
                win_x=100,
                win_y=200,
            )
        self.assertEqual((width, height), (20, 30))
        self.assertEqual(base64.b64decode(encoded), self._png())
        self.assertIn("-l", commands[0])
        self.assertIn("42", commands[0])
        self.assertNotIn("-R", commands[0])

    def test_window_only_native_failure_keeps_window_scope(self):
        """A failed native window capture may fall back to CLI, never to a region."""
        self.namespace["_HAS_IMAGEIO"] = True
        native = MagicMock(side_effect=RuntimeError("native failure"))
        self.namespace["_take_screenshot_cg"] = native
        commands = []

        def run(command, **kwargs):
            """Return a failed scoped CLI capture without permitting a retry."""
            commands.append(command)
            return SimpleNamespace(returncode=1)

        with patch.object(subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "requested area"):
                self.namespace["take_screenshot"](
                    window_id=42,
                    logical_width=20,
                    logical_height=30,
                    win_x=100,
                    win_y=200,
                )
        native.assert_called_once_with(100, 200, 20, 30, window_id=42)
        self.assertEqual(len(commands), 1)
        self.assertIn("-l", commands[0])
        self.assertNotIn("-R", commands[0])
        self.assertNotIn("-t", commands[0][commands[0].index("-l") + 1 :])

    def test_native_window_capture_does_not_fallback_to_full_display(self):
        """A null scoped native image must fail without invoking display capture."""
        self.namespace["_HAS_IMAGEIO"] = True
        cg = MagicMock()
        cg.CGWindowListCreateImage.return_value = None
        cg.CGDisplayCreateImageForRect = MagicMock(side_effect=AssertionError("desktop fallback"))
        self.namespace.update({
            "_cg": cg,
            "_cf": MagicMock(),
            "CGRect": lambda **values: SimpleNamespace(**values),
            "kCGWindowListOptionIncludingWindow": 1,
            "kCGWindowListOptionOnScreenOnly": 2,
            "kCGWindowImageBoundsIgnoreFraming": 3,
            "kCGWindowImageDefault": 4,
            "kCGNullWindowID": 0,
            "_validate_image_dimensions": validate_image_dimensions,
        })
        load_functions("capture.py", {"_take_screenshot_cg"}, self.namespace)
        with self.assertRaisesRegex(RuntimeError, "returned NULL"):
            self.namespace["_take_screenshot_cg"](100, 200, 20, 30, window_id=42)
        call = cg.CGWindowListCreateImage.call_args
        self.assertEqual(call.args[1], 1)
        self.assertEqual(call.args[2], 42)
        cg.CGDisplayCreateImageForRect.assert_not_called()


class CapturePreflightTests(unittest.TestCase):
    """Permission checks must stay metadata-only and never capture the desktop."""

    def _load(self, cg, subprocess_mock, tempfile_mock):
        """Load the preflight helper with observable forbidden I/O boundaries."""
        namespace = {
            "_cg": cg,
            "ctypes": ctypes,
            "subprocess": subprocess_mock,
            "tempfile": tempfile_mock,
        }
        load_functions("capture.py", {"check_screen_recording"}, namespace)
        return namespace["check_screen_recording"]

    def test_granted_preflight_has_no_capture_side_effects(self):
        """A granted preflight returns without subprocesses or temporary files."""
        preflight = MagicMock(return_value=True)
        subprocess_mock = MagicMock()
        tempfile_mock = MagicMock()
        check = self._load(SimpleNamespace(CGPreflightScreenCaptureAccess=preflight), subprocess_mock, tempfile_mock)

        self.assertIsNone(check())
        preflight.assert_called_once_with()
        subprocess_mock.assert_not_called()
        tempfile_mock.assert_not_called()

    def test_denied_preflight_has_no_capture_side_effects(self):
        """A denied permission reports a clear error without attempting capture."""
        preflight = MagicMock(return_value=False)
        subprocess_mock = MagicMock()
        tempfile_mock = MagicMock()
        check = self._load(SimpleNamespace(CGPreflightScreenCaptureAccess=preflight), subprocess_mock, tempfile_mock)

        with self.assertRaisesRegex(RuntimeError, "Screen Recording permission"):
            check()
        subprocess_mock.assert_not_called()
        tempfile_mock.assert_not_called()

    def test_unavailable_preflight_reports_unsupported_api_without_capture(self):
        """Older macOS without the preflight symbol fails before any fallback I/O."""
        subprocess_mock = MagicMock()
        tempfile_mock = MagicMock()
        check = self._load(SimpleNamespace(), subprocess_mock, tempfile_mock)

        with self.assertRaisesRegex(RuntimeError, "preflight is unavailable"):
            check()
        subprocess_mock.assert_not_called()
        tempfile_mock.assert_not_called()


class WindowCaptureCacheTests(unittest.TestCase):
    """The ScreenCaptureKit helper caches bounded plans, never rendered images."""

    def setUp(self):
        """Install inert framework modules and reset the helper's process cache."""
        import klyk.window_capture as window_capture

        self.module = window_capture
        # These legacy cache tests replace every native provider; policy is tested separately.
        gate = patch.object(window_capture, "_connection_gate", enabled_gate(), create=True)
        gate.start()
        self.addCleanup(gate.stop)
        self._original_load = self.module._load
        self._original_capacity = self.module._CAPACITY
        self._original_ttl = self.module._TTL
        self._original_plans = dict(self.module._plans)
        self.addCleanup(self._restore_module_state)
        self.module._plans.clear()
        self.module._CAPACITY = 2
        self.module._TTL = 2.0

        class Data(bytearray):
            """Small mutable data object accepted by the PNG encoder boundary."""

            @classmethod
            def data(cls):
                return cls()

        class Pool:
            """No-op autorelease-pool context for the mocked native bridge."""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        objc = ModuleType("objc")
        objc.autorelease_pool = lambda: Pool()
        foundation = ModuleType("Foundation")
        foundation.NSMutableData = Data
        quartz = ModuleType("Quartz")
        quartz.CGPreflightScreenCaptureAccess = lambda: True
        quartz.CGImageGetWidth = lambda _image: 20
        quartz.CGImageGetHeight = lambda _image: 30
        quartz.CGImageDestinationCreateWithData = lambda data, *_args: data
        quartz.CGImageDestinationAddImage = lambda data, *_args: data.extend(CaptureScopeTests._png())
        quartz.CGImageDestinationFinalize = lambda *_args: True
        self.modules = {"objc": objc, "Foundation": foundation, "Quartz": quartz}
        self.module._load = MagicMock(return_value=self._classes())

    def _restore_module_state(self):
        """Restore process globals so this file cannot affect later test cases."""
        self.module._load = self._original_load
        self.module._CAPACITY = self._original_capacity
        self.module._TTL = self._original_ttl
        self.module._plans.clear()
        self.module._plans.update(self._original_plans)

    def test_invalid_window_plan_rejected_before_native_import_or_lookup(self):
        """Malformed direct requests cannot become a different integer window plan."""
        for values in ((True, 20, 30), (1.5, 20, 30), (2 ** 32 + 1, 20, 30),
                       (1, "20", 30), (1, 20.5, 30), (1, 8193, 1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.module.take(*values)
        self.module._load.assert_not_called()
        self.assertEqual(len(self.module._plans), 0)

    def _classes(self):
        """Build fake ScreenCaptureKit classes whose selectors are observable."""
        content_class = MagicMock()
        filter_class = MagicMock()
        config_class = MagicMock()
        manager = MagicMock()
        return content_class, filter_class, config_class, manager

    def _content(self, window_id):
        """Return shareable content containing exactly the requested window."""
        window = SimpleNamespace(windowID=lambda: window_id)
        return SimpleNamespace(windows=lambda: [window])

    def _take(self, window_id, complete):
        """Call the helper with the mocked framework modules installed."""
        with patch.dict(sys.modules, self.modules):
            with patch.object(self.module, "_complete", side_effect=complete):
                return self.module.take(window_id, 20, 30)

    def test_cache_is_bounded_and_stores_plan_objects(self):
        """Old plans are evicted at capacity and no image is retained in a plan."""
        def complete(start, _deadline):
            """Return content for discovery and an opaque image for capture."""
            holder = []
            start(lambda value, error: holder.extend((value, error)))
            return self._content(1) if not holder else holder[0]

        # The helper's callbacks are easier to model by returning based on call order.
        results = iter([self._content(1), object(), self._content(2), object(), self._content(3), object()])
        self._take(1, lambda _start, _deadline: next(results))
        self._take(2, lambda _start, _deadline: next(results))
        self._take(3, lambda _start, _deadline: next(results))
        self.assertLessEqual(len(self.module._plans), 2)
        for created, window_filter, config in self.module._plans.values():
            self.assertIsInstance(created, float)
            self.assertIsNot(window_filter, None)
            self.assertIsNot(config, None)
            self.assertNotIsInstance(config, (bytes, bytearray))

    def test_expired_plan_is_rebuilt(self):
        """A plan older than the TTL is discarded before the next capture."""
        results = iter([self._content(1), object(), self._content(1), object()])
        self._take(1, lambda _start, _deadline: next(results))
        key = (1, 20, 30)
        _, window_filter, config = self.module._plans[key]
        self.module._plans[key] = (time.monotonic() - 3.0, window_filter, config)
        self._take(1, lambda _start, _deadline: next(results))
        filter_class = self.module._load.return_value[1]
        self.assertEqual(filter_class.alloc.call_count, 2)

    def test_failed_capture_evicts_cached_plan(self):
        """A failed image callback removes the plan so the next call rebuilds it."""
        self._take(1, lambda _start, _deadline: self._content(1))
        self.assertIn((1, 20, 30), self.module._plans)
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            self._take(1, lambda _start, _deadline: (_ for _ in ()).throw(RuntimeError("capture failed")))
        self.assertNotIn((1, 20, 30), self.module._plans)

    def test_callback_timeout_is_bounded(self):
        """A native callback that never completes returns within its deadline."""
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            self.module._complete(lambda _done: None, time.monotonic() + 0.02)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_native_resize_failure_is_reported_and_releases_image(self):
        """A failed logical-size conversion must not return a mismatched image."""
        cg = MagicMock()
        cg.CGWindowListCreateImage.return_value = 99
        cg.CGImageGetWidth.return_value = 40
        cg.CGImageGetHeight.return_value = 60
        cf = MagicMock()
        namespace = {
            "_cg": cg,
            "_cf": cf,
            "ctypes": ctypes,
            "CGRect": lambda **values: SimpleNamespace(**values),
            "kCGWindowListOptionIncludingWindow": 1,
            "kCGWindowListOptionOnScreenOnly": 2,
            "kCGWindowImageBoundsIgnoreFraming": 3,
            "kCGWindowImageDefault": 4,
            "kCGNullWindowID": 0,
            "_resize_cgimage": MagicMock(side_effect=RuntimeError("resize failed")),
            "_cgimage_to_png_bytes": MagicMock(),
            "base64": base64,
            "_validate_image_dimensions": validate_image_dimensions,
        }
        load_functions("capture.py", {"_take_screenshot_cg"}, namespace)
        with self.assertRaisesRegex(RuntimeError, "resize failed"):
            namespace["_take_screenshot_cg"](100, 200, 20, 30, window_id=42)
        namespace["_resize_cgimage"].assert_called_once_with(99, 20, 30)
        namespace["_cgimage_to_png_bytes"].assert_not_called()
        cf.CFRelease.assert_called_once()
        self.assertEqual(cf.CFRelease.call_args.args[0].value, 99)


if __name__ == "__main__":
    unittest.main()
