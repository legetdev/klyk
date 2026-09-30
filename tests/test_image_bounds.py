"""Reject malicious image dimensions before native decoding or allocation."""

import ast
import base64
from pathlib import Path
import struct
import unittest
from unittest.mock import MagicMock
from klyk.image_bounds import validate_image_dimensions, validated_png_bytes


class ImageBoundsTests(unittest.TestCase):
    """Compile the actual codec boundary with inert native framework bindings."""

    def test_hostile_images_are_rejected_before_native_decode(self):
        """Tiny compressed bombs and malformed base64 must never reach ImageIO."""
        source = Path(__file__).resolve().parents[1] / "klyk/capture.py"
        functions = [n for n in ast.parse(source.read_text()).body
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "decode_png_to_rgb_array"]
        native = MagicMock()
        scope = {"base64": base64, "_HAS_IMAGEIO": True, "_cf": native, "_imageio": native,
                 "_validated_png_bytes": validated_png_bytes,
                 "_validate_image_dimensions": validate_image_dimensions}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
        for width, height in ((0, 1), (1, 0), (100000, 100000), (8192, 8192), (8193, 1)):
            raw = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\0" * 9
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                scope["decode_png_to_rgb_array"](base64.b64encode(raw).decode())
        for value in ("not base64", "eA==", "A" * (32 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                scope["decode_png_to_rgb_array"](value)
        self.assertFalse(native.mock_calls)


if __name__ == "__main__":
    unittest.main()
