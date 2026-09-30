"""
Template matching engine for Klyk.

Pixel-accurate element location for surfaces where the AX tree is unavailable:
browser web content, canvas apps (Figma, Sketch), Electron web views, Flutter
apps, and anything else rendered outside native AppKit/SwiftUI widgets.

Core API
--------
    crop(screenshot_b64, x1, y1, x2, y2) -> str
        Extract a region from a screenshot as a base64 PNG template.

    find(screenshot_b64, template_b64, threshold, search_region) -> dict | None
        Find the template in a screenshot. Returns the center of the best match.

MCP tool flow (wired into mcp_server.py — see the get_template / find_template
/ wait_for_visual handlers there):

    # 1. Take a screenshot and identify the element approximately
    screenshot(app="Chrome")            # → see YouTube like button near x=91, y=601

    # 2. Extract it as a template
    get_template(app="Chrome", x1=83, y1=594, x2=101, y2=610)
    # → {"template_b64": "iVBORw0KGgo..."}

    # 3. Find its precise location (takes a fresh screenshot internally)
    find_template(app="Chrome", template_b64="iVBORw0KGgo...")
    # → {"x": 93, "y": 602, "confidence": 0.97}

    # 4. Click precisely
    click(app="Chrome", x=93, y=602)

Why this is accurate
--------------------
The matcher runs a normalized cross-correlation (the same TM_CCOEFF_NORMED
metric OpenCV exposes) over every pixel position in the haystack and returns
the location with the highest similarity score. This is deterministic and
sub-element-accurate: even if the crop region includes a few pixels of
surrounding content, the match center lands on the element's visual centroid,
not on the agent's estimated coordinate.

It also handles scroll drift: if the page scrolls between the screenshot and
the click, find_template takes a NEW screenshot as haystack, so it returns the
element's CURRENT position rather than where it was when first observed.

Implementation note
-------------------
No OpenCV. The correlation is computed in pure NumPy: the cross-correlation
numerator via an FFT, and the per-window sums / sums-of-squares for the
zero-normalization via integral images (summed-area tables). PNG decode/encode
go through klyk's first-party CoreGraphics codec (capture.decode_png_to_rgb_array
/ encode_rgb_array_to_png_b64). This keeps the dependency surface to NumPy alone
— OpenCV used to be the only reason klyk pinned numpy>=2, which broke shared
Python environments.
"""

from __future__ import annotations

import math
import numpy as np

from . import capture
from .image_bounds import png_dimensions, validated_png_bytes

_TEMPLATE_CACHE_BYTES = 8 * 1024 * 1024
_TEMPLATE_CACHE_ENTRIES = 50


# ---------------------------------------------------------------------------
# Normalized cross-correlation (TM_CCOEFF_NORMED equivalent), pure NumPy
# ---------------------------------------------------------------------------

def _window_sums(plane: np.ndarray, th: int, tw: int) -> np.ndarray:
    """
    Sum of `plane` over every th×tw window, returned as a
    (H-th+1, W-tw+1) array. Computed in O(H·W) via an integral image
    (summed-area table) rather than re-summing each window.
    """
    h, w = plane.shape
    integ = np.zeros((h + 1, w + 1), dtype=np.float64)
    integ[1:, 1:] = np.cumsum(np.cumsum(plane, axis=0), axis=1)
    oh, ow = h - th + 1, w - tw + 1
    return (
        integ[th:th + oh, tw:tw + ow]
        - integ[0:oh, tw:tw + ow]
        - integ[th:th + oh, 0:ow]
        + integ[0:oh, 0:ow]
    )


def _next_fast_len(target: int) -> int:
    """
    Smallest 5-smooth integer (2^a·3^b·5^c) >= target. numpy's FFT is fastest
    on such lengths; padding the transform up to one avoids the pathological
    slowdown when h+th-1 or w+tw-1 lands on a large prime (e.g. a 1169-tall
    window → 1192 = 8·149, where the 149 factor makes the FFT ~10× slower).
    """
    if target <= 6:
        return target
    best = float("inf")
    five = 1
    while five < target * 2:
        three = five
        while three < target * 2:
            two = three
            while two < target:
                two *= 2
            if two < best:
                best = two
            three *= 3
        five *= 5
    return int(best)


def _validate_match_budget(
    height: int, width: int, template_height: int, template_width: int,
    decoded_pixels: int | None = None,
    encoded_bytes: int = 0,
) -> None:
    """Reject expensive decoding/FFT/map allocations using a 512 MiB work estimate."""
    if min(height, width, template_height, template_width) <= 0:
        raise ValueError("Template matching requires non-empty images")
    if template_width > width or template_height > height:
        raise ValueError(
            f"Template ({template_width}×{template_height}) is larger than search area ({width}×{height})"
        )
    fh = _next_fast_len(height + template_height - 1)
    fw = _next_fast_len(width + template_width - 1)
    area = height * width
    valid_area = (height - template_height + 1) * (width - template_width + 1)
    if decoded_pixels is None:
        decoded_pixels = area + template_height * template_width
    # RGB float64 inputs, padded complex transforms (including temporaries and
    # the previous channel), integral-image passes, and all live correlation maps.
    fft_bytes = decoded_pixels * 24 + encoded_bytes + 8 * (6 * fh * fw + 6 * area + 8 * valid_area)
    # A 16-bit RGBA PNG can occupy 8 native bytes per pixel before conversion
    # into the 4-byte canonical bitmap and 24-byte float64 RGB output.
    decode_bytes = decoded_pixels * 36 + encoded_bytes * 4
    if max(fft_bytes, decode_bytes) > 512 * 1024 * 1024:
        raise ValueError(
            "Template search exceeds the 512 MiB work limit; use a smaller screenshot, crop the template, or use a smaller search_region."
        )


def _clip_region(region, width: int, height: int, kind: str) -> tuple[int, int, int, int]:
    """Validate and clip one integer rectangle without NumPy negative-index wrapping."""
    if not isinstance(region, (tuple, list)) or len(region) != 4 or any(
        isinstance(value, bool) or not isinstance(value, (int, np.integer)) for value in region
    ):
        raise ValueError(f"{kind} region must contain four integer coordinates")
    x1, y1, x2, y2 = (int(value) for value in region)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Invalid {kind.lower()} region: ({x1},{y1})→({x2},{y2}) for screenshot {width}×{height}")
    return x1, y1, x2, y2


def cache_template(cache: dict[str, str], template_id: str, png_b64: str) -> None:
    """Keep a 50-entry / 8 MiB LRU cache, validating new images before any eviction."""
    validated_png_bytes(png_b64)
    entry_bytes = len(png_b64)
    if entry_bytes > _TEMPLATE_CACHE_BYTES:
        raise ValueError("Template exceeds the 8 MiB cache budget; crop a smaller region.")
    remaining_bytes = sum(len(value) for key, value in cache.items() if key != template_id)
    cache.pop(template_id, None)
    while cache and (len(cache) >= _TEMPLATE_CACHE_ENTRIES
                     or remaining_bytes + entry_bytes > _TEMPLATE_CACHE_BYTES):
        oldest = next(iter(cache))
        remaining_bytes -= len(cache.pop(oldest))
    cache[template_id] = png_b64


def _xcorr_valid(plane: np.ndarray, template: np.ndarray) -> np.ndarray:
    """
    Cross-correlation Σ(plane · template) over each fully-overlapping window,
    via FFT. Returns the 'valid' region, shape (H-th+1, W-tw+1) — the same
    positions OpenCV's matchTemplate reports. The transform is padded up to a
    5-smooth length for speed; the extra padding only affects wrap-around
    coefficients at indices we never read.
    """
    h, w = plane.shape
    th, tw = template.shape
    _validate_match_budget(h, w, th, tw)
    fh = _next_fast_len(h + th - 1)
    fw = _next_fast_len(w + tw - 1)
    fft_plane = np.fft.rfft2(plane, (fh, fw))
    fft_templ = np.fft.rfft2(template[::-1, ::-1], (fh, fw))
    full = np.fft.irfft2(fft_plane * fft_templ, (fh, fw))
    return full[th - 1:h, tw - 1:w]


def _match_ncc(haystack: np.ndarray, template: np.ndarray) -> np.ndarray:
    """
    Compute the TM_CCOEFF_NORMED correlation map between a multi-channel
    haystack and template (both float64, shape (H,W,C) / (th,tw,C)). Each
    channel is zero-normalized with its own mean and the channels are summed,
    matching OpenCV's color-image behaviour. Returns a (H-th+1, W-tw+1) array
    of scores in [-1, 1]; 1.0 is a pixel-perfect match.
    """
    h, w, c = haystack.shape
    th, tw, _ = template.shape
    if c != 3 or template.shape[2] != 3:
        raise ValueError("Template matching requires RGB images")
    _validate_match_budget(h, w, th, tw)
    n = th * tw
    num = np.zeros((h - th + 1, w - tw + 1), dtype=np.float64)
    den_hay = np.zeros_like(num)
    den_templ = 0.0
    for ch in range(c):
        plane = haystack[:, :, ch]
        templ = template[:, :, ch]
        sum_i = _window_sums(plane, th, tw)
        sum_i2 = _window_sums(plane * plane, th, tw)
        corr = _xcorr_valid(plane, templ)
        sum_t = float(templ.sum())
        sum_t2 = float((templ * templ).sum())
        num += corr - sum_i * sum_t / n
        den_hay += sum_i2 - (sum_i * sum_i) / n
        den_templ += sum_t2 - (sum_t * sum_t) / n
    denom = np.sqrt(np.maximum(den_hay, 0.0) * max(den_templ, 0.0))
    result = np.zeros_like(num)
    nonzero = denom > 1e-9
    result[nonzero] = num[nonzero] / denom[nonzero]
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def crop(
    screenshot_b64: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> str:
    """
    Crop a region from a base64 PNG screenshot and return it as a base64 PNG.

    Coordinates are window-relative, matching Klyk's coordinate system.
    The returned value is a template suitable for passing to find().
    """
    w, h = png_dimensions(screenshot_b64)
    x1, y1, x2, y2 = _clip_region((x1, y1, x2, y2), w, h, "Crop")
    source_pixels = w * h
    crop_pixels = (x2 - x1) * (y2 - y1)
    decode_bytes = source_pixels * 36 + len(screenshot_b64) * 4
    # Float64 source, cropped uint8, BGRX/provider copies, and PNG/base64 output.
    encode_bytes = source_pixels * 24 + crop_pixels * 32 + len(screenshot_b64)
    if max(decode_bytes, encode_bytes) > 512 * 1024 * 1024:
        raise ValueError("Template extraction exceeds the 512 MiB work limit; use a smaller screenshot or crop.")
    img = capture.decode_png_to_rgb_array(screenshot_b64)
    return capture.encode_rgb_array_to_png_b64(img[y1:y2, x1:x2].astype(np.uint8))


def find(
    screenshot_b64: str,
    template_b64: str,
    threshold: float | None = 0.8,
    search_region: tuple[int, int, int, int] | None = None,
) -> dict | None:
    """
    Find a template image within a screenshot using normalized cross-correlation.

    Parameters
    ----------
    screenshot_b64 : str
        Full window screenshot as base64 PNG (haystack).
    template_b64 : str
        Template image as base64 PNG (needle). Produced by crop().
    threshold : float | None
        Minimum confidence score 0–1 (default 0.8). Lower = more lenient.
        0.95+ for pixel-identical matches; 0.80 for slight scale/lighting
        variation; below 0.70 produces too many false positives.
        Pass None to always return the best match regardless of confidence —
        callers (e.g. the find_template MCP handler) use this to surface
        last_confidence on misses so the agent can decide whether to lower
        the threshold or recapture.
    search_region : (x1, y1, x2, y2) | None
        Restrict search to a sub-region of the screenshot. Use to avoid
        false matches when the same element appears multiple times (e.g.
        multiple like buttons in a comment thread). Coordinates are
        window-relative.

    Returns
    -------
    dict with keys:
        x, y         — center of best match, window-relative
        confidence   — match score 0–1
        box          — [x1, y1, x2, y2] bounding box of match
    None if threshold was a float and no match exceeded it.
    """
    if threshold is not None:
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float, np.number))
                or not math.isfinite(float(threshold)) or not 0 <= threshold <= 1):
            raise ValueError("Match threshold must be a finite number between 0 and 1")
    hw, hh = png_dimensions(screenshot_b64)
    nw, nh = png_dimensions(template_b64)
    decoded_pixels = hw * hh + nw * nh
    offset_x, offset_y = 0, 0
    if search_region is not None:
        sx1, sy1, sx2, sy2 = _clip_region(search_region, hw, hh, "Search")
        offset_x, offset_y = sx1, sy1
        hw, hh = sx2 - sx1, sy2 - sy1
    _validate_match_budget(
        hh, hw, nh, nw, decoded_pixels=decoded_pixels,
        encoded_bytes=len(screenshot_b64) + len(template_b64),
    )
    haystack = capture.decode_png_to_rgb_array(screenshot_b64)
    needle = capture.decode_png_to_rgb_array(template_b64)
    if search_region is not None:
        haystack = haystack[sy1:sy2, sx1:sx2]
    result = _match_ncc(haystack, needle)
    # result is (rows=y, cols=x); argmax gives the top-left corner of the match.
    max_y, max_x = np.unravel_index(int(np.argmax(result)), result.shape)
    max_val = float(result[max_y, max_x])
    # Float math can nudge a perfect match a hair past 1.0; clamp to [-1, 1].
    max_val = max(-1.0, min(1.0, max_val))

    if threshold is not None and max_val < threshold:
        return None

    cx = offset_x + int(max_x) + nw // 2
    cy = offset_y + int(max_y) + nh // 2

    return {
        "x": int(cx),
        "y": int(cy),
        "confidence": round(max_val, 4),
        "box": [
            offset_x + int(max_x),
            offset_y + int(max_y),
            offset_x + int(max_x) + nw,
            offset_y + int(max_y) + nh,
        ],
    }
