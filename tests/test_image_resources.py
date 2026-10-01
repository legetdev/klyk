"""Image resource and pixel-provider regressions with no desktop activity."""

import ast
import asyncio
import base64
from collections import OrderedDict
import ctypes
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zlib

import numpy as np

from klyk.image_bounds import png_dimensions, validate_image_dimensions, validated_png_bytes
from test_input_cleanup import load_functions


def image(width=2, height=2, color=(7, 31, 89)):
    """Build a tiny real RGB PNG using only standard-library chunk encoding."""
    def chunk(kind, data):
        """Encode one PNG chunk with its CRC, including the terminal IEND."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    # Giant-header tests never need a giant pixel buffer or native decoder.
    pixels = (b"\0" + bytes(color) * width) * height if width * height <= 1000 else b"\0"
    raw += chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    return base64.b64encode(raw).decode()


class MatcherResourceTests(unittest.TestCase):
    """Stop memory amplification before decode, maps, or NumPy FFT allocation."""

    def setUp(self):
        """Compile production math with an observable inert PNG decoder."""
        self.capture = SimpleNamespace(decode_png_to_rgb_array=MagicMock(), encode_rgb_array_to_png_b64=MagicMock())
        self.ns = {"np": np, "math": math, "capture": self.capture,
                   "png_dimensions": png_dimensions, "validated_png_bytes": validated_png_bytes}
        load_functions("matcher.py", {"find", "crop", "_match_ncc", "_window_sums", "_xcorr_valid",
                                      "_next_fast_len", "_validate_match_budget", "_clip_region"}, self.ns)

    def test_large_pair_fails_before_decoder_or_fft(self):
        """A tiny 16MP header pair must never produce multi-gigabyte work arrays."""
        value = image(4000, 4000)
        with patch.object(np.fft, "rfft2") as fft, patch.object(np, "zeros") as allocate:
            with self.assertRaisesRegex(ValueError, "512 MiB.*search_region"):
                self.ns["find"](value, value)
        self.capture.decode_png_to_rgb_array.assert_not_called()
        fft.assert_not_called()
        allocate.assert_not_called()

    def test_direct_array_math_is_bounded_before_maps(self):
        """Zero-stride views reproduce large shapes without allocating their pixels."""
        array = np.broadcast_to(np.array(1.0), (4000, 4000, 3))
        with patch.object(np.fft, "rfft2") as fft, patch.object(np, "zeros") as allocate:
            with self.assertRaisesRegex(ValueError, "512 MiB"):
                self.ns["_match_ncc"](array, array)
        fft.assert_not_called()
        allocate.assert_not_called()

    def test_common_logical_windows_fit_the_work_budget(self):
        """Normal icon searches retain support without a heavyweight live benchmark."""
        for height, width in ((982, 1512), (1169, 1800), (1080, 1920)):
            with self.subTest(width=width, height=height):
                self.ns["_validate_match_budget"](height, width, 32, 32, encoded_bytes=1024 * 1024)
        with self.assertRaisesRegex(ValueError, "512 MiB"):
            self.ns["_validate_match_budget"](2160, 3840, 32, 32)

    def test_search_region_keeps_full_decode_memory_in_budget(self):
        """A safe smaller region preserves coordinates and charges full-image decoding."""
        screen, template = image(3840, 2160), image(16, 16)
        pixels = np.broadcast_to(np.array(1.0), (2160, 3840, 3))
        needle = np.zeros((16, 16, 3))
        self.capture.decode_png_to_rgb_array.side_effect = (pixels, needle)
        compute = MagicMock(return_value=np.array([[0.99]]))
        self.ns["_match_ncc"] = compute
        result = self.ns["find"](screen, template, search_region=(100, 150, 740, 630))
        self.assertEqual(compute.call_args.args[0].shape, (480, 640, 3))
        self.assertEqual(result["box"], [100, 150, 116, 166])
        self.assertEqual((result["x"], result["y"]), (108, 158))

    def test_small_search_region_still_rejects_oversize_decode_work(self):
        """A small matching rectangle cannot excuse a large 16-bit source decode."""
        with self.assertRaisesRegex(ValueError, "512 MiB.*smaller screenshot"):
            self.ns["find"](image(4000, 4000), image(16, 16), search_region=(0, 0, 640, 480))
        self.capture.decode_png_to_rgb_array.assert_not_called()

    def test_large_crop_rejects_before_decode_or_pixel_conversion(self):
        """Extracting a full giant template cannot bypass matching's memory budget."""
        with patch.object(np, "ascontiguousarray") as allocate:
            with self.assertRaisesRegex(ValueError, "512 MiB.*smaller screenshot or crop"):
                self.ns["crop"](image(4000, 4000), 0, 0, 4000, 4000)
        self.capture.decode_png_to_rgb_array.assert_not_called()
        self.capture.encode_rgb_array_to_png_b64.assert_not_called()
        allocate.assert_not_called()

    def test_small_crop_of_4k_source_keeps_supported_pixel_geometry(self):
        """A safe region still charges its full source decode and encodes exact pixels."""
        self.capture.decode_png_to_rgb_array.return_value = np.broadcast_to(
            np.array([10.0, 20.0, 30.0]), (2160, 3840, 3))
        expected = image(5, 5)
        self.capture.encode_rgb_array_to_png_b64.return_value = expected
        self.assertEqual(self.ns["crop"](image(3840, 2160), 20, 30, 25, 35), expected)
        pixels = self.capture.encode_rgb_array_to_png_b64.call_args.args[0]
        self.assertEqual(pixels.shape, (5, 5, 3))
        self.assertEqual(pixels.dtype, np.uint8)
        np.testing.assert_array_equal(pixels[0, 0], [10, 20, 30])

    def test_invalid_threshold_or_region_fails_before_decode(self):
        """Bad comparisons cannot select an arbitrary point or waste native work."""
        for threshold in (-1, 2, float("nan"), float("inf"), True, "0.8"):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.ns["find"](image(), image(), threshold=threshold)
        for region in ((1, 2, 3), (0.5, 0, 2, 2), (True, 0, 2, 2), 7):
            with self.subTest(region=region), self.assertRaises(ValueError):
                self.ns["find"](image(), image(), search_region=region)
        self.capture.decode_png_to_rgb_array.assert_not_called()

    def test_small_matches_agree_with_direct_ground_truth(self):
        """Cross-correlation remains correct for color images and non-square templates."""
        rng = np.random.default_rng(43)
        for height, width, th, tw in ((7, 9, 3, 2), (4, 5, 4, 5), (5, 6, 1, 3)):
            haystack = rng.uniform(0, 255, (height, width, 3))
            template = rng.uniform(0, 255, (th, tw, 3))
            actual = self.ns["_match_ncc"](haystack, template)
            expected = np.zeros_like(actual)
            centered_template = template - template.mean(axis=(0, 1), keepdims=True)
            for y in range(height - th + 1):
                for x in range(width - tw + 1):
                    patch_pixels = haystack[y:y + th, x:x + tw]
                    centered = patch_pixels - patch_pixels.mean(axis=(0, 1), keepdims=True)
                    denominator = math.sqrt(float((centered ** 2).sum() * (centered_template ** 2).sum()))
                    expected[y, x] = float((centered * centered_template).sum()) / denominator if denominator else 0
            np.testing.assert_allclose(actual, expected, rtol=1e-11, atol=1e-11)


class TemplateCacheTests(unittest.TestCase):
    """LRU eviction must enforce bytes and preserve prior templates on failed insertions."""

    def setUp(self):
        """Use small genuine PNGs and a small-scale quota to avoid large allocations."""
        self.ns = {"validated_png_bytes": validated_png_bytes}
        source = Path(__file__).resolve().parents[1] / "klyk" / "matcher.py"
        tree = ast.parse(source.read_text())
        limits = [node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id in
                          {"_TEMPLATE_CACHE_BYTES", "_TEMPLATE_CACHE_ENTRIES"} for target in node.targets)]
        exec(compile(ast.Module(body=limits, type_ignores=[]), str(source), "exec"), self.ns)
        load_functions("matcher.py", {"cache_template"}, self.ns)

    def test_byte_budget_evicts_lru_for_dict_and_ordered_dict(self):
        """Three distinct images fit only after the least-recently-used item is removed."""
        value = image()
        self.ns["_TEMPLATE_CACHE_BYTES"] = len(value) * 2
        for factory in (dict, OrderedDict):
            cache = factory()
            for key in ("a", "b", "a", "c"):
                self.ns["cache_template"](cache, key, value)
            self.assertEqual(list(cache), ["a", "c"])
            self.assertLessEqual(sum(map(len, cache.values())), len(value) * 2)

    def test_invalid_or_oversize_entry_does_not_evict(self):
        """A bad replacement cannot erase previously usable handles."""
        value = image()
        cache = {"keep": value}
        self.ns["_TEMPLATE_CACHE_BYTES"] = len(value) - 1
        for new_value in (value, "not base64", image(8193, 1), None):
            with self.subTest(new_value_type=type(new_value).__name__), self.assertRaises(ValueError):
                self.ns["cache_template"](cache, "new", new_value)
            self.assertEqual(cache, {"keep": value})

    def test_entry_limit_and_aggregate_ceiling_are_finite(self):
        """The existing 64-session ceiling retains at most 512MiB of templates."""
        self.assertEqual(self.ns["_TEMPLATE_CACHE_BYTES"] * 64, 512 * 1024 * 1024)
        cache = {}
        for index in range(51):
            self.ns["cache_template"](cache, str(index), image())
        self.assertEqual(len(cache), 50)
        self.assertNotIn("0", cache)


class NativeImageBoundaryTests(unittest.TestCase):
    """Invalid image metadata never reaches decoder, encoder, or capture allocation."""

    def test_rgb_encode_rejects_huge_shapes_before_contiguous_copy(self):
        """A zero-stride array cannot trigger a giant uint8/native conversion."""
        scope = {"_HAS_IMAGEIO": True, "_validate_image_dimensions": validate_image_dimensions}
        load_functions("capture.py", {"encode_rgb_array_to_png_b64"}, scope)
        value = np.broadcast_to(np.array([1, 2, 3], dtype=np.uint8), (4001, 4001, 3))
        with patch.object(np, "ascontiguousarray") as allocate:
            with self.assertRaises(ValueError):
                scope["encode_rgb_array_to_png_b64"](value)
        allocate.assert_not_called()

    def test_ocr_rejects_bombs_and_nonfinite_regions_before_native_calls(self):
        """OCR shares PNG protection and validates ROI numbers before Vision setup."""
        native = MagicMock()
        scope = {"_require": lambda: None, "math": math,
                 "validated_png_bytes": validated_png_bytes, "validate_image_dimensions": validate_image_dimensions,
                 "NSData": native, "CGImageSourceCreateWithData": native,
                 "CGImageSourceCreateImageAtIndex": native}
        load_functions("ocr.py", {"recognize_all"}, scope)
        for value in (image(8193, 1), image(8192, 8192), "not base64"):
            with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
                scope["recognize_all"](value)
        for region in ((0, 0, float("nan"), 1), (0, 0, 1, float("inf")), (0, 1, 2), None):
            kwargs = {"region": region} if region is not None else {"level": 9}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                scope["recognize_all"](image(), **kwargs)
        native.assert_not_called()

    def test_bad_capture_identity_or_dimensions_stop_before_io(self):
        """An invalid explicit window cannot become an implicit full-desktop request."""
        native = MagicMock()
        scope = {"_validate_image_dimensions": validate_image_dimensions, "_HAS_IMAGEIO": True,
                 "_take_screenshot_cg": native, "subprocess": native, "tempfile": native,
                 "time": SimpleNamespace(sleep=native)}
        load_functions("capture.py", {"take_screenshot"}, scope)
        for kwargs in ({"window_id": 0}, {"window_id": -1}, {"window_id": 2 ** 32},
                       {"window_id": True}, {"logical_width": 8193, "logical_height": 1},
                       {"logical_width": 20}, {"logical_width": 1.5, "logical_height": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                scope["take_screenshot"](**kwargs)
        native.assert_not_called()

    def test_pixel_capture_rejects_bounds_before_native_allocation(self):
        """Scoped samplers cannot truncate identities or allocate oversized native images."""
        native = MagicMock()
        scope = {"_validate_image_dimensions": validate_image_dimensions,
                 "_cg": native, "CGRect": native}
        load_functions("capture.py", {"_capture_window_image"}, scope)
        for window_id, width, height in ((0, 10, 10), (True, 10, 10), (2 ** 32 + 1, 10, 10),
                                          (1, 8193, 1), (1, 4001, 4001), (1, 1.5, 2)):
            with self.subTest(window_id=window_id, width=width, height=height), self.assertRaises(ValueError):
                scope["_capture_window_image"](window_id, 0, 0, width, height)
        native.assert_not_called()

    def test_small_valid_cli_png_and_dimension_mismatch(self):
        """Small valid PNGs work; a converter cannot silently return wrong coordinates."""
        scope = {"_HAS_IMAGEIO": False, "os": os, "tempfile": tempfile, "subprocess": subprocess,
                 "base64": base64, "png_dimensions": png_dimensions,
                 "_validate_image_dimensions": validate_image_dimensions,
                 "time": SimpleNamespace(sleep=lambda _: None)}
        load_functions("capture.py", {"take_screenshot"}, scope)
        wrong = False
        paths = []

        def run(command, **_kwargs):
            """Write only test-owned PNG outputs for mocked capture/conversion calls."""
            paths.append(command[-1])
            height = 31 if wrong and command[0] == "/usr/bin/sips" else 30
            Path(command[-1]).write_bytes(base64.b64decode(image(20, height)))
            return SimpleNamespace(returncode=0)

        with patch.object(subprocess, "run", side_effect=run):
            value, width, height = scope["take_screenshot"](window_id=1, logical_width=20, logical_height=30)
            self.assertEqual((width, height), (20, 30))
            self.assertLess(len(base64.b64decode(value)), 100)
            wrong = True
            with self.assertRaisesRegex(RuntimeError, "dimensions"):
                scope["take_screenshot"](window_id=1, logical_width=20, logical_height=30)
        self.assertTrue(all(not Path(path).exists() for path in paths))


class PixelProviderTests(unittest.TestCase):
    """Provider lengths, bit depths, and backing scales are evidence, never guesses."""

    def setUp(self):
        """Use a two-pixel test-owned provider with real ctypes addressing."""
        self.pixels = (ctypes.c_uint8 * 8)(30, 20, 10, 255, 60, 50, 40, 255)
        self.cg, self.cf = MagicMock(), MagicMock()
        self.cg.CGImageGetWidth.return_value, self.cg.CGImageGetHeight.return_value = 2, 1
        self.cg.CGImageGetBytesPerRow.return_value = 8
        self.cg.CGImageGetBitmapInfo.return_value = 0x2002
        self.cg.CGImageGetBitsPerPixel.return_value, self.cg.CGImageGetBitsPerComponent.return_value = 32, 8
        self.cg.CGImageGetDataProvider.return_value, self.cg.CGDataProviderCopyData.return_value = 7, 8
        self.cf.CFDataGetLength.return_value = 8
        self.cf.CFDataGetBytePtr.return_value = ctypes.addressof(self.pixels)
        self.ns = {"ctypes": ctypes, "_cg": self.cg, "_cf": self.cf}
        load_functions("capture.py", {"_pixel_layout", "_pixel_buffer", "_read_pixels_from_cgimage",
                                      "_read_rect_medians_from_cgimage", "_image_pixel_scale"}, self.ns)

    def test_supported_pixel_formats_keep_rgb_channels(self):
        """The existing little-endian BGRA path still returns exact RGB values."""
        self.assertEqual(self.ns["_read_pixels_from_cgimage"](1, [(0, 0), (1, 0)]), [(10, 20, 30), (40, 50, 60)])
        self.cf.CFRelease.assert_called_once()

    def test_all_supported_alpha_and_byte_orders_keep_exact_rgb(self):
        """Alpha-last little-endian ABGR must not silently exchange red and blue."""
        expected = [(10, 20, 30), (40, 50, 60)]
        for alpha in (1, 2, 3, 4, 5, 6):
            for order in (0, 0x2000, 0x4000):
                first = alpha in (2, 4, 6)
                little = order == 0x2000
                values = []
                for r, g, b in expected:
                    values.extend((b, g, r, 255) if first and little else
                                  (255, r, g, b) if first else
                                  (255, b, g, r) if little else (r, g, b, 255))
                self.pixels[:] = values
                self.cg.CGImageGetBitmapInfo.return_value = order | alpha
                with self.subTest(alpha=alpha, order=order):
                    self.assertEqual(self.ns["_read_pixels_from_cgimage"](1, [(0, 0), (1, 0)]), expected)
                    self.assertEqual(self.ns["_read_rect_medians_from_cgimage"](1, [(0, 0, 1, 1)]), expected[:1])

    def test_unknown_format_is_rejected_before_provider_copy(self):
        """A valid 24-bit RGB image cannot be misread using a four-byte stride."""
        self.cg.CGImageGetBitsPerPixel.return_value = 24
        for name, values in (("_read_pixels_from_cgimage", [(1, 0)]),
                             ("_read_rect_medians_from_cgimage", [(0, 0, 2, 1)])):
            with self.assertRaisesRegex(RuntimeError, "format is unsupported"):
                self.ns[name](1, values)
        self.cg.CGDataProviderCopyData.assert_not_called()

    def test_short_or_null_provider_is_rejected_and_released(self):
        """No ctypes view may reference beyond the native provider's declared bytes."""
        for length, pointer in ((7, ctypes.addressof(self.pixels)), (8, None)):
            self.cf.CFDataGetLength.return_value, self.cf.CFDataGetBytePtr.return_value = length, pointer
            with self.subTest(length=length, pointer_is_null=pointer is None), self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.ns["_read_pixels_from_cgimage"](1, [(1, 0)])
        self.assertEqual(self.cf.CFRelease.call_count, 2)

    def test_mismatched_backing_height_never_reuses_width_only_scale(self):
        """A resize/scale race must require another observation before sampling."""
        self.cg.CGImageGetWidth.return_value, self.cg.CGImageGetHeight.return_value = 40, 59
        with self.assertRaisesRegex(RuntimeError, "dimensions changed"):
            self.ns["_image_pixel_scale"](1, 20, 30)
        self.cg.CGImageGetHeight.return_value = 60
        self.assertEqual(self.ns["_image_pixel_scale"](1, 20, 30), 2.0)


if __name__ == "__main__":
    unittest.main()
