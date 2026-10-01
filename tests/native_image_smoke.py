"""Bounded native image/OCR evidence from generated pixels, without desktop activity."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch

_RESULT = "KLYK_NATIVE_IMAGE_RESULT "
_TIMEOUT = 30


def _stage(name):
    """Expose the last bounded native stage if the child does not finish."""
    print(json.dumps({"stage": name}), flush=True)


def _text_png(capture):
    """Draw HELLO through CoreText into a private 512×256 bitmap, without AppKit."""
    import base64
    import ctypes
    import ctypes.util

    ct = ctypes.CDLL(ctypes.util.find_library("CoreText"))
    cf, cg = capture._cf, capture._cg
    for library, name, result, arguments in (
        (ct, "CTFontCreateWithName", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_double, ctypes.c_void_p]),
        (ct, "CTLineCreateWithAttributedString", ctypes.c_void_p, [ctypes.c_void_p]),
        (ct, "CTLineDraw", None, [ctypes.c_void_p, ctypes.c_void_p]),
        (cf, "CFDictionaryCreateMutable", ctypes.c_void_p,
         [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]),
        (cf, "CFDictionarySetValue", None, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]),
        (cf, "CFAttributedStringCreate", ctypes.c_void_p,
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]),
        (cg, "CGContextSetRGBFillColor", None, [ctypes.c_void_p] + [ctypes.c_double] * 4),
        (cg, "CGContextFillRect", None, [ctypes.c_void_p, capture.CGRect]),
        (cg, "CGContextSetTextPosition", None, [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]),
    ):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    owned = []

    def retain(value):
        """Retain each created object until drawing finishes, then release in reverse."""
        if not value:
            raise RuntimeError("Synthetic text object creation failed")
        owned.append(value)
        return value

    try:
        color_space = retain(cg.CGColorSpaceCreateDeviceRGB())
        context = retain(cg.CGBitmapContextCreate(None, 512, 256, 8, 512 * 4,
                                                 color_space, capture._CG_BITMAP_INFO))
        cg.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
        cg.CGContextFillRect(context, capture.CGRect(0, 0, 512, 256))
        name = retain(capture._make_cf_str("Helvetica"))
        font = retain(ct.CTFontCreateWithName(name, 42, None))
        attributes = retain(cf.CFDictionaryCreateMutable(None, 0, None, None))
        font_key = ctypes.c_void_p.in_dll(ct, "kCTFontAttributeName").value
        cf.CFDictionarySetValue(attributes, font_key, font)
        text = retain(capture._make_cf_str("HELLO"))
        attributed = retain(cf.CFAttributedStringCreate(None, text, attributes))
        line = retain(ct.CTLineCreateWithAttributedString(attributed))
        cg.CGContextSetRGBFillColor(context, 0, 0, 0, 1)
        cg.CGContextSetTextPosition(context, 230, 100)
        ct.CTLineDraw(line, context)
        image = retain(cg.CGBitmapContextCreateImage(context))
        return base64.b64encode(capture._cgimage_to_png_bytes(image)).decode("ascii")
    finally:
        for value in reversed(owned):
            cf.CFRelease(ctypes.c_void_p(value))


def _native_boxes(ocr, image, region=None):
    """Read raw Vision boxes for the same CGImage under full-image and ROI requests."""
    handler = ocr.VNImageRequestHandler.alloc().initWithCGImage_options_(
        image, ocr.NSDictionary.dictionary())
    request = ocr.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1)
    request.setUsesLanguageCorrection_(False)
    ocr._configure_compute(request)
    request.setRecognitionLanguages_(ocr.NSArray.arrayWithArray_(["en-US"]))
    if region is not None:
        x, y, width, height = region
        request.setRegionOfInterest_(((x / 512, 1 - (y + height) / 256),
                                     (width / 512, height / 256)))
    success, error = handler.performRequests_error_(ocr.NSArray.arrayWithObject_(request), None)
    if not success:
        raise RuntimeError(f"Native OCR failed: {error or 'no native result'}")
    results = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        box = observation.boundingBox()
        results.append({"text": str(candidates[0].string()),
                        "box": [float(box.origin.x), float(box.origin.y),
                                float(box.size.width), float(box.size.height)]})
    return results


def _pixel_formats(capture, check):
    """Verify native provider RGB samples against native PNG conversion for every layout."""
    import base64
    import ctypes
    import numpy as np

    expected = [(10, 20, 30), (40, 50, 60)]
    for alpha in (1, 2, 3, 4, 5, 6):
        for order in (0, 0x2000, 0x4000):
            first, little = alpha in (2, 4, 6), order == 0x2000
            values = []
            for red, green, blue in expected:
                values.extend((blue, green, red, 255) if first and little else
                              (255, red, green, blue) if first else
                              (255, blue, green, red) if little else (red, green, blue, 255))
            buffer = (ctypes.c_uint8 * 8)(*values)
            color_space = provider = image = 0
            try:
                color_space = capture._cg.CGColorSpaceCreateDeviceRGB()
                provider = capture._cg.CGDataProviderCreateWithData(None, buffer, 8, None)
                image = capture._cg.CGImageCreate(2, 1, 8, 32, 8, color_space,
                                                 alpha | order, provider, None, False, 0)
                if not image:
                    raise RuntimeError("Native generated pixel format is unavailable")
                samples = capture._read_pixels_from_cgimage(image, [(0, 0), (1, 0)])
                png = base64.b64encode(capture._cgimage_to_png_bytes(image)).decode("ascii")
                decoded = capture.decode_png_to_rgb_array(png)
                check("native bitmap RGB layout", samples == expected and np.array_equal(decoded[0], expected),
                      {"alpha": alpha, "byte_order": order, "samples": samples, "decoded": decoded[0].tolist()})
            finally:
                for value in (image, provider, color_space):
                    if value:
                        capture._cf.CFRelease(ctypes.c_void_p(value))


def _image_box(box, region=(0, 0, 512, 256)):
    """Convert one bottom-left normalized box into top-left image pixels."""
    bx, by, bw, bh = box
    x, y, width, height = region
    return [x + bx * width, y + (1 - by - bh) * height, bw * width, bh * height]


def _box_error(first, second):
    """Compare centers and dimensions so merely being inside the ROI cannot pass."""
    return max(abs(first[0] + first[2] / 2 - second[0] - second[2] / 2),
               abs(first[1] + first[3] / 2 - second[1] - second[3] / 2),
               abs(first[2] - second[2]), abs(first[3] - second[3]))


def _hello(results):
    """Select the intentionally generated text without any caller-owned input."""
    return next((result for result in results if result["text"] == "HELLO"), None)


def _child():
    """Run native work only in a test-owned child that its parent can terminate."""
    import base64
    import numpy as np
    import klyk
    from klyk import capture, ocr

    report = {"synthetic_only": True, "desktop_capture_forbidden": True,
              "platform": platform.mac_ver()[0], "package": str(klyk.__file__),
              "checks": [], "completed": False, "passed": False}
    started = time.monotonic()

    def check(name, passed, detail=None):
        """Record direct evidence and retain failures without exposing images."""
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    try:
        with ExitStack() as guards:
            for name in ("CGWindowListCreateImage", "CGDisplayCreateImageForRect"):
                guards.enter_context(patch.object(capture._cg, name,
                    side_effect=AssertionError("Desktop capture is forbidden in this fixture")))
            _stage("generated RGB ImageIO roundtrip")
            y, x = np.indices((12, 16))
            rgb = np.stack((x * 17, y * 21, (x + y) * 9), axis=-1).astype(np.uint8)
            encoded = capture.encode_rgb_array_to_png_b64(rgb)
            decoded = capture.decode_png_to_rgb_array(encoded)
            check("native RGB ImageIO roundtrip", np.array_equal(rgb, decoded), list(decoded.shape))
            _stage("private native bitmap format roundtrips")
            _pixel_formats(capture, check)
            _stage("private CoreText bitmap")
            encoded = _text_png(capture)
            pixels = capture.decode_png_to_rgb_array(encoded)
            dark_y, dark_x = np.where(pixels.min(axis=2) < 128)
            if not len(dark_x):
                raise RuntimeError("Synthetic CoreText bitmap has no glyph pixels")
            glyph = [int(dark_x.min()), int(dark_y.min()),
                     int(dark_x.max() - dark_x.min() + 1), int(dark_y.max() - dark_y.min() + 1)]
            report["glyph_pixel_box"] = glyph
            raw = base64.b64decode(encoded, validate=True)
            data = ocr.NSData.dataWithBytes_length_(raw, len(raw))
            source = ocr.CGImageSourceCreateWithData(data, None)
            image = ocr.CGImageSourceCreateImageAtIndex(source, 0, None)
            if image is None:
                raise RuntimeError("Generated text image is not decodable")
            region = (210, 80, 200, 120)
            _stage("native full-image fast OCR")
            full_raw = _native_boxes(ocr, image)
            _stage("native ROI fast OCR")
            region_raw = _native_boxes(ocr, image, region)
            report["raw_full"] = full_raw
            report["raw_roi"] = region_raw
            report["region"] = list(region)
            full, regional = _hello(full_raw), _hello(region_raw)
            check("native text recognition", bool(full and regional))
            if full is None or regional is None:
                raise RuntimeError("Native OCR did not recognize the generated HELLO fixture")
            full_box = _image_box(full["box"])
            original_box = _image_box(regional["box"])
            region_box = _image_box(regional["box"], region)
            original_error = _box_error(full_box, original_box)
            relative_error = _box_error(full_box, region_box)
            contract = ("full_image" if original_error <= 6 < relative_error else
                        "roi_relative" if relative_error <= 6 < original_error else "ambiguous")
            report["native_roi_contract"] = contract
            report["native_contract_errors"] = {"full_image": original_error, "roi_relative": relative_error}
            check("raw full-image OCR aligns with generated glyphs", _box_error(full_box, glyph) <= 8,
                  {"observed": full_box, "glyphs": glyph})
            check("native ROI coordinate contract is distinguishable", contract != "ambiguous")
            _stage("production full-image fast OCR")
            full_production = ocr.recognize_all(encoded, level=1, languages=["en-US"])
            _stage("production ROI fast OCR")
            region_production = ocr.recognize_all(encoded, level=1, languages=["en-US"], region=region)
            report["production_full"] = full_production
            report["production_roi"] = region_production
            for name, observations in (("full-image", full_production), ("ROI", region_production)):
                observation = _hello(observations)
                production_box = ([observation["x"] - observation["width"] / 2,
                                   observation["y"] - observation["height"] / 2,
                                   observation["width"], observation["height"]]
                                  if observation else None)
                check(f"production {name} coordinates align with generated glyphs",
                      production_box is not None and _box_error(production_box, glyph) <= 8,
                      {"observed": production_box, "glyphs": glyph})
            report["completed"] = True
            report["passed"] = all(check["passed"] for check in report["checks"])
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(_RESULT + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["passed"] else 1


def main():
    """Persist exact native evidence, containing failures in one 30-second child."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("native-image.json"))
    parser.add_argument("--child", action="store_true")
    arguments = parser.parse_args()
    if arguments.child:
        return _child()
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-u", str(Path(__file__).resolve()), "--child"],
                                capture_output=True, text=True, timeout=_TIMEOUT)
        lines = [line for line in result.stdout.splitlines() if line.startswith(_RESULT)]
        report = json.loads(lines[-1][len(_RESULT):]) if lines else {
            "completed": False, "passed": False,
            "failure": {"type": "ChildFailure", "message": "Native fixture child produced no result"}}
        report["child_exit_code"] = result.returncode
        report["passed"] = bool(report["passed"] and report.get("completed") and result.returncode == 0)
        report["child_stages"] = [line for line in result.stdout.splitlines() if not line.startswith(_RESULT)]
        report["child_stderr"] = result.stderr
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        report = {"completed": False, "passed": False, "timed_out": True,
                  "failure": {"type": "TimeoutExpired", "message": "Native fixture exceeded its 30-second hard timeout"},
                  "child_stages": stdout.decode("utf-8", errors="replace").splitlines() if isinstance(stdout, bytes) else stdout.splitlines(),
                  "child_stderr": stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr}
    report["synthetic_only"] = True
    report["desktop_capture_forbidden"] = True
    report["timeout_seconds"] = _TIMEOUT
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "completed": report["completed"],
                      "output": str(arguments.output), "wall_seconds": report["wall_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
