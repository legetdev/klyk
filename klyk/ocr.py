"""
On-device OCR via Apple's Vision framework (VNRecognizeTextRequest).

Used as the AX-fallback inside click_element: when the accessibility tree has
no element matching a label (common in canvas surfaces, browser content
without forced a11y, and Electron apps), Klyk reads the screen as text and
clicks the matching glyph directly.

Runs locally through Apple Vision with no network or model download. Returns
coordinates in the same window-relative pixel space the rest of Klyk uses.
"""

from __future__ import annotations

import base64

try:
    import objc
    from Foundation import NSData, NSDictionary, NSArray, NSLocale
    from Quartz import (
        CGImageSourceCreateWithData,
        CGImageSourceCreateImageAtIndex,
        CGImageGetWidth,
        CGImageGetHeight,
    )
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


# Default recognition languages. Read the macOS preferred-languages list once
# at import so an OCR call on a German / Japanese / Chinese Mac picks up the
# user's locale without the caller having to know. Vision's own default is
# ["en-US"] only, which silently mis-recognizes accented and CJK text.
# Falls back to ["en-US"] on any read failure (e.g. exotic test environments).
_SYSTEM_LANGS: list[str] = ["en-US"]
if _AVAILABLE:
    try:
        prefs = NSLocale.preferredLanguages()
        if prefs:
            _SYSTEM_LANGS = [str(p) for p in prefs]
    except Exception:
        pass


def is_available() -> bool:
    return _AVAILABLE


def system_languages() -> list[str]:
    """Return the recognition-language list that defaults to this Mac's locale."""
    return list(_SYSTEM_LANGS)


def _require() -> None:
    if not _AVAILABLE:
        raise RuntimeError(
            "PyObjC Vision bindings are not installed. "
            "Run: pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
        )


def _configure_compute(request) -> None:
    """Choose supported CPU stages to avoid accelerator compilation on interactive OCR."""
    if hasattr(request, 'supportedComputeStageDevicesAndReturnError_'):
        stages, error = request.supportedComputeStageDevicesAndReturnError_(None)
        if error is not None or stages is None:
            raise RuntimeError('Text recognition compute devices are unavailable.')
        cpu_class = objc.lookUpClass('MLCPUComputeDevice')
        for stage, devices in stages.items():
            cpu = next((device for device in devices if device.isKindOfClass_(cpu_class)), None)
            if cpu is not None:
                request.setComputeDevice_forComputeStage_(cpu, stage)
    else:
        request.setUsesCPUOnly_(True)  # Compatibility with macOS before per-stage selection.


def recognize_all(
    image_b64: str,
    level: int = 1,
    languages: list[str] | None = None,
    region: tuple[float, float, float, float] | None = None,
) -> list[dict]:
    """
    Run OCR on a base64 PNG and return every recognized text observation.

    Each result: {text, x, y, width, height, confidence}.
    x, y are the center of the bounding box in top-left-origin pixel coords
    matching the input image's dimensions. Sorted by confidence descending.

    level=1 (fast) is the default for crisp UI text. level=0 (accurate) catches small,
    low-contrast, or stylized text that fast mode misses.

    `languages` is a list of BCP-47 codes (["de-DE", "en-US"], ["zh-Hans"], ...).
    None (default) uses the macOS system preferred-language list so OCR works
    on a non-English Mac without the caller specifying anything. Pass an
    explicit list to recognize text in a language the system isn't configured
    for, or to constrain to a subset for ambiguous strings.
    """
    _require()

    img_bytes = base64.b64decode(image_b64)
    ns_data = NSData.dataWithBytes_length_(img_bytes, len(img_bytes))

    src = CGImageSourceCreateWithData(ns_data, None)
    if src is None:
        raise RuntimeError("CGImageSourceCreateWithData failed (invalid PNG?)")
    cg_image = CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg_image is None:
        raise RuntimeError("CGImageSourceCreateImageAtIndex failed")

    width = CGImageGetWidth(cg_image)
    height = CGImageGetHeight(cg_image)

    # Native NSDictionary returns nil for absent option keys. A bridged Python
    # dict raises on those lookups in recent Vision builds, before OCR starts.
    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, NSDictionary.dictionary())
    request = VNRecognizeTextRequest.alloc().init()
    if region is not None:
        x, y, w, h = region
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError('OCR region must fit inside the captured window.')
        # Vision expects a normalized rectangle with its origin at bottom-left.
        request.setRegionOfInterest_(((x / width, 1 - (y + h) / height), (w / width, h / height)))
    request.setRecognitionLevel_(level)
    _configure_compute(request)
    request.setUsesLanguageCorrection_(False)
    langs = languages if languages else _SYSTEM_LANGS
    # Best-effort: older Vision builds without setRecognitionLanguages_ keep
    # the framework default rather than failing the whole call.
    try:
        request.setRecognitionLanguages_(NSArray.arrayWithArray_(langs))
    except Exception:
        pass

    # Vision may read these collections on internal workers. Native arrays avoid
    # callbacks into Python while the synchronous recognition call is waiting.
    success, error = handler.performRequests_error_(NSArray.arrayWithObject_(request), None)
    if not success:
        raise RuntimeError(f'Text recognition could not complete: {error or "no native result"}')

    results: list[dict] = []
    for obs in (request.results() or []):
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        cand = candidates[0]
        text = str(cand.string())
        bbox = obs.boundingBox()
        # Vision normalized coords: [0,1], origin bottom-left.
        bx = bbox.origin.x
        by = bbox.origin.y
        bw = bbox.size.width
        bh = bbox.size.height

        # Vision normalizes observations to its region of interest. Restore the
        # region's offset and scale before reporting full-screenshot coordinates.
        rx, ry, rw, rh = region if region is not None else (0, 0, width, height)
        px = rx + bx * rw
        py = ry + (1.0 - by - bh) * rh
        pw = bw * rw
        ph = bh * rh

        results.append({
            "text": text,
            "x": int(px + pw / 2),
            "y": int(py + ph / 2),
            "width": int(pw),
            "height": int(ph),
            "confidence": round(float(cand.confidence()), 4),
        })

    results.sort(key=lambda m: -m["confidence"])
    return results


def find_text(
    image_b64: str,
    query: str,
    languages: list[str] | None = None,
) -> list[dict]:
    """
    Find every text observation whose recognized string contains `query`
    (case-insensitive substring). Returns matches sorted by confidence.

    Two-pass: fast mode first (level 1), then accurate mode (level 0) if fast
    yielded no match. Small or thin text (sub-15px links, captions, icon
    glyphs) often slips past fast mode but is caught by accurate mode.

    `languages` follows the same default as `recognize_all`: None → system
    preferred languages.
    """
    q = query.lower().strip()
    if not q:
        return []
    fast = [
        m for m in recognize_all(image_b64, level=1, languages=languages)
        if q in m["text"].lower()
    ]
    if fast:
        return fast
    return [
        m for m in recognize_all(image_b64, level=0, languages=languages)
        if q in m["text"].lower()
    ]
