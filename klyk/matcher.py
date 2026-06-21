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

import numpy as np

from . import capture


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
    img = capture.decode_png_to_rgb_array(screenshot_b64)
    h, w = img.shape[:2]

    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    if x2 <= x1 or y2 <= y1:
        raise ValueError(
            f"Invalid crop region: ({x1},{y1})→({x2},{y2}) "
            f"for screenshot {w}×{h}"
        )

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
    haystack = capture.decode_png_to_rgb_array(screenshot_b64)
    needle = capture.decode_png_to_rgb_array(template_b64)

    nh, nw = needle.shape[:2]

    # Optionally restrict the search area
    offset_x, offset_y = 0, 0
    if search_region is not None:
        sx1, sy1, sx2, sy2 = search_region
        h, w = haystack.shape[:2]
        sx1, sy1 = max(0, sx1), max(0, sy1)
        sx2, sy2 = min(w, sx2), min(h, sy2)
        haystack = haystack[sy1:sy2, sx1:sx2]
        offset_x, offset_y = sx1, sy1

    hh, hw = haystack.shape[:2]
    if nw > hw or nh > hh:
        raise ValueError(
            f"Template ({nw}×{nh}) is larger than search area ({hw}×{hh})"
        )

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




