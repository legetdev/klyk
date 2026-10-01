"""Pure PNG preflight shared by native image consumers before pixel allocation."""

import base64
import struct


def validate_image_dimensions(width: int, height: int) -> None:
    """Bound image allocations consistently across capture, encoding, and OCR."""
    if (isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, int) or not isinstance(height, int)
            or not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16_000_000)):
        raise ValueError("PNG dimensions exceed the 8192-per-side / 16-megapixel limit")


def validated_png_bytes(b64_png: str) -> bytes:
    """Reject oversized or non-PNG payloads before invoking any native decoder."""
    if not isinstance(b64_png, str):
        raise ValueError("PNG data must be a base64 string")
    if len(b64_png) > 32 * 1024 * 1024:
        raise ValueError("PNG data exceeds the 32 MiB encoded limit; use a smaller template")
    raw = base64.b64decode(b64_png, validate=True)
    if (len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[8:16] != b"\x00\x00\x00\x0dIHDR"):
        raise ValueError("Image must be a PNG with a valid header")
    width, height = struct.unpack(">II", raw[16:24])
    validate_image_dimensions(width, height)
    return raw


def png_dimensions(b64_png: str) -> tuple[int, int]:
    """Read validated dimensions without invoking ImageIO or allocating image pixels."""
    raw = validated_png_bytes(b64_png)
    return struct.unpack(">II", raw[16:24])
