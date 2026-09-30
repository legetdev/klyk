"""Diagnose OCR modes on tiny generated pixels, with no desktop activity."""

import argparse
import base64
from contextlib import ExitStack
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

_RESULT = "KLYK_NATIVE_OCR_MODE_RESULT "
_FIXTURE_RESULT = "KLYK_NATIVE_OCR_FIXTURE_RESULT "
_TIMEOUT = 12
_TOTAL_TIMEOUT = 225
_FIXTURE_TIMEOUT = 15
_COMPILER_TIMEOUT = 30
_FIND_TIMEOUT = 5
_FIXTURE_LIMIT = 65536
_LOG_LIMIT = 12000


def _cases():
    """Keep production controls and isolate native configuration, bridge, and revision."""
    cases = [{"name": f"production-{level}-worker-system", "backend": "production",
              "level": level, "thread": "worker", "language": "system",
              "compute": "production"} for level in (1, 0)]
    for mode in ("defaults", "native_nsstring", "language_correction"):
        cases.append({"name": f"python-accurate-{mode}", "backend": "python_raw",
                      "level": 0, "thread": "main", "mode": mode})
    for revision in (1, 2):
        cases.append({"name": f"python-accurate-revision-{revision}", "backend": "python_raw",
                      "level": 0, "thread": "main", "mode": "production", "revision": revision})
    for mode, level in (("defaults", 0), ("production", 0), ("production", 1)):
        cases.append({"name": f"swift-{level}-{mode}", "backend": "swift",
                      "level": level, "thread": "main", "mode": mode})
    for revision in (1, 2):
        cases.append({"name": f"swift-accurate-revision-{revision}", "backend": "swift",
                      "level": 0, "thread": "main", "mode": "production", "revision": revision})
    return cases


def _fixture_bytes(path):
    """Accept only the test-owned, small, fixed-size generated PNG in this comparison."""
    with Path(path).open("rb") as stream:
        raw = stream.read(_FIXTURE_LIMIT + 1)
    if (len(raw) > _FIXTURE_LIMIT or len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[12:16] != b"IHDR" or int.from_bytes(raw[16:20], "big") != 512
            or int.from_bytes(raw[20:24], "big") != 256):
        raise ValueError("The diagnostic requires its generated 512 by 256 PNG fixture.")
    return raw


def _stage(name):
    """Retain the last native stage when a test-owned child exceeds its bound."""
    print(json.dumps({"stage": name}), flush=True)


def _native_error(error):
    """Record a native error without assuming its truthiness or losing a nil result."""
    if error is None:
        return None
    result = {"type": type(error).__name__, "message": str(error)[:2000]}
    for name in ("domain", "code", "localizedDescription"):
        try:
            value = getattr(error, name)()
            result[name] = int(value) if name == "code" else str(value)[:2000]
        except Exception:
            pass
    return result


def _request_settings(request):
    """Read scalar configuration without warming any supported-device/model queries."""
    return {"level": int(request.recognitionLevel()), "revision": int(request.revision()),
            "language_correction": bool(request.usesLanguageCorrection()),
            "languages": [str(value) for value in request.recognitionLanguages()]}


def _request_metadata(request):
    """Inspect supported devices and languages only after the actual native call returns."""
    result = _request_settings(request)
    revisions = request.__class__.supportedRevisions()
    result["supported_revisions"] = [value for value in range(1, 10) if revisions.containsIndex_(value)]
    try:
        languages, error = request.supportedRecognitionLanguagesAndReturnError_(None)
        result["supported_languages"] = [str(value) for value in languages or []][:100]
        result["supported_language_error"] = _native_error(error)
    except Exception as error:
        result["supported_language_error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    try:
        stages, error = request.supportedComputeStageDevicesAndReturnError_(None)
        result["compute_error"] = _native_error(error)
        result["compute_stages"] = [{
            "stage": str(stage), "supported_devices": [type(device).__name__ for device in devices],
            "assigned_device": (type(selected).__name__ if selected is not None else None),
        } for stage, devices in (stages or {}).items()
            for selected in [request.computeDeviceForComputeStage_(stage)]]
    except Exception as error:
        result["compute_error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    return result


def _raw_request(case, raw, ocr, report):
    """Compare typed native defaults and language elements without production wrappers."""
    from Quartz import CGImageGetBitsPerComponent, CGImageGetBitsPerPixel, CGImageGetAlphaInfo

    data = ocr.NSData.dataWithBytes_length_(raw, len(raw))
    source = ocr.CGImageSourceCreateWithData(data, None)
    image = ocr.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None or (ocr.CGImageGetWidth(image), ocr.CGImageGetHeight(image)) != (512, 256):
        raise RuntimeError("The generated fixture could not be decoded at its fixed size.")
    report["image"] = {"width": 512, "height": 256, "bits_per_component": int(CGImageGetBitsPerComponent(image)),
                       "bits_per_pixel": int(CGImageGetBitsPerPixel(image)), "alpha_info": int(CGImageGetAlphaInfo(image))}
    request = ocr.VNRecognizeTextRequest.alloc().init()
    report["initial_request"] = _request_settings(request)
    report["native_performed"] = False
    if "revision" in case:
        supported = request.__class__.supportedRevisions()
        report["revision_supported"] = bool(supported.containsIndex_(case["revision"]))
        if not report["revision_supported"]:
            raise RuntimeError("The requested diagnostic revision is not advertised as supported.")
        request.setRevision_(case["revision"])
    if case["mode"] != "defaults":
        request.setRecognitionLevel_(case["level"])
        ocr._configure_compute(request)
        request.setUsesLanguageCorrection_(case["mode"] == "language_correction")
        if case["mode"] == "native_nsstring":
            from Foundation import NSString
            language = NSString.alloc().initWithUTF8String_(b"en-US")
            report["language_construction"] = "NSString.alloc().initWithUTF8String_"
            report["language_bridge_type"] = type(language).__name__
            native_language = language.nsstring()
            report["language_native_class"] = type(native_language).__name__
            languages = ocr.NSArray.arrayWithObject_(native_language)
        else:
            languages = ocr.NSArray.arrayWithArray_(["en-US"])
        request.setRecognitionLanguages_(languages)
    report["final_request"] = _request_settings(request)
    handler = ocr.VNImageRequestHandler.alloc().initWithCGImage_options_(image, ocr.NSDictionary.dictionary())
    _stage("raw native performRequests")
    started = time.monotonic()
    success, error = handler.performRequests_error_(ocr.NSArray.arrayWithObject_(request), None)
    report["native_elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["native_performed"] = True
    report["native_success"], report["native_error"] = bool(success), _native_error(error)
    results = request.results()
    report["native_results_nil"] = results is None
    report["native_result_count"] = len(results) if results is not None else None
    report["native_metadata_after_request"] = _request_metadata(request)
    if not success:
        raise RuntimeError(f"Text recognition could not complete: {error or 'no native result'}")
    observations = []
    for result in (results or [])[:10]:
        candidates = result.topCandidates_(1)
        if candidates:
            candidate, box = candidates[0], result.boundingBox()
            observations.append({"text": str(candidate.string())[:2000],
                "confidence": float(candidate.confidence()),
                "x": int((box.origin.x + box.size.width / 2) * 512),
                "y": int((1 - box.origin.y - box.size.height / 2) * 256),
                "width": int(box.size.width * 512), "height": int(box.size.height * 256)})
    return observations


def _child(case, fixture):
    """Run one isolated Python OCR request on the same test-owned generated PNG."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from klyk import capture, ocr
    import klyk
    from Quartz import CGImageGetBitsPerComponent, CGImageGetBitsPerPixel, CGImageGetAlphaInfo

    report = {"case": case, "completed": False, "passed": False,
              "package": klyk.__file__, "system_languages": ocr.system_languages(),
              "platform": platform.mac_ver()[0]}
    started = time.monotonic()
    original_compute, native_handler = ocr._configure_compute, ocr.VNImageRequestHandler

    def configure(request):
        """Apply one explicit compute policy inside this diagnostic process only."""
        report["compute_at_initial_configuration"] = _request_settings(request)
        if case["compute"] == "production":
            original_compute(request)

    class Handler:
        """Forward the real native request while recording settings and returned errors."""

        def __init__(self, image, options):
            """Retain the same native handler and generated image used by production."""
            self.native = native_handler.alloc().initWithCGImage_options_(image, options)
            report["image"] = {"width": int(ocr.CGImageGetWidth(image)),
                               "height": int(ocr.CGImageGetHeight(image)),
                               "bits_per_component": int(CGImageGetBitsPerComponent(image)),
                               "bits_per_pixel": int(CGImageGetBitsPerPixel(image)),
                               "alpha_info": int(CGImageGetAlphaInfo(image))}

        def performRequests_error_(self, requests, error):
            """Record the exact native request and its outcome without changing configuration."""
            request = requests[0]
            report["final_before_compute"] = _request_settings(request)
            report["final_request"] = _request_settings(request)
            _stage("native performRequests")
            native_started = time.monotonic()
            success, native_error = self.native.performRequests_error_(requests, error)
            report["native_elapsed_seconds"] = round(time.monotonic() - native_started, 3)
            report["native_success"] = bool(success)
            report["native_performed"] = True
            report["native_success_repr"] = repr(success)[:100]
            report["native_error"] = _native_error(native_error)
            results = request.results()
            report["native_results_nil"] = results is None
            report["native_result_count"] = len(results) if results is not None else None
            report["native_metadata_after_request"] = _request_metadata(request)
            return success, native_error

    factory = SimpleNamespace(alloc=lambda: SimpleNamespace(initWithCGImage_options_=Handler))

    def recognize(encoded):
        """Exercise the actual production function on the requested thread and language list."""
        report["thread"] = {"name": threading.current_thread().name,
                            "is_main": threading.current_thread() is threading.main_thread()}
        arguments = {"level": case["level"],
                     "languages": ["en-US"] if case["language"] == "en-US" else None,
                     "region": case.get("region")}
        if case.get("autorelease_pool"):
            with ocr.objc.autorelease_pool():
                return ocr.recognize_all(encoded, **arguments)
        return ocr.recognize_all(encoded, **arguments)

    try:
        with ExitStack() as guards:
            for name in ("CGWindowListCreateImage", "CGDisplayCreateImageForRect"):
                guards.enter_context(patch.object(capture._cg, name,
                    side_effect=AssertionError("Desktop capture is forbidden in this fixture")))
            raw = _fixture_bytes(fixture)
            report["input_sha256"] = hashlib.sha256(raw).hexdigest()
            encoded = base64.b64encode(raw).decode("ascii")
            _stage("Python OCR " + case["name"])
            try:
                if case["backend"] == "python_raw":
                    observations = _raw_request(case, raw, ocr, report)
                else:
                    guards.enter_context(patch.object(ocr, "_configure_compute", configure))
                    guards.enter_context(patch.object(ocr, "VNImageRequestHandler", factory))
                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-diagnostic") as executor:
                        observations = executor.submit(recognize, encoded).result()
                report["observations"] = observations[:10]
                report["passed"] = any(item["text"].strip().upper() == "HELLO" for item in observations)
            finally:
                report["completed"] = True
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(_RESULT + json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
    return 0 if report["completed"] and report["passed"] else 1


def _generate_fixture(path):
    """Generate the sole comparison image in a bounded, test-owned native child."""
    from klyk import capture
    from native_image_smoke import _text_png

    report = {"completed": False, "passed": False, "package": capture.__file__}
    try:
        with ExitStack() as guards:
            for name in ("CGWindowListCreateImage", "CGDisplayCreateImageForRect"):
                guards.enter_context(patch.object(capture._cg, name,
                    side_effect=AssertionError("Desktop capture is forbidden in this fixture")))
            _stage("single generated CoreText bitmap")
            raw = base64.b64decode(_text_png(capture), validate=True)
            if len(raw) > _FIXTURE_LIMIT:
                raise ValueError("The generated fixture exceeds the diagnostic byte limit.")
            Path(path).write_bytes(raw)
            raw = _fixture_bytes(path)
            report.update({"completed": True, "passed": True, "image": [512, 256],
                           "png_bytes": len(raw), "input_sha256": hashlib.sha256(raw).hexdigest()})
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    print(_FIXTURE_RESULT + json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
    return 0 if report["passed"] else 1


def _prepare_fixture(path, timeout):
    """Verify generated-image ownership, size, and digest before any comparison request."""
    result = subprocess.run([sys.executable, "-B", "-u", str(Path(__file__).resolve()),
                             "--generate-fixture", str(path)],
                            capture_output=True, text=True, timeout=timeout)
    lines = [line for line in result.stdout.splitlines() if line.startswith(_FIXTURE_RESULT)]
    report = json.loads(lines[-1][len(_FIXTURE_RESULT):]) if lines else {}
    raw = _fixture_bytes(path)
    if (not isinstance(report, dict) or report.get("completed") is not True
            or report.get("passed") is not True or result.returncode != 0
            or report.get("input_sha256") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("Generated fixture setup did not return matching successful evidence.")
    report.update({"child_exit_code": result.returncode, "timeout_seconds": timeout,
                   "child_stderr": result.stderr[:_LOG_LIMIT]})
    return report


def _prepare_swift(directory, timeout):
    """Compile the first-party typed reference once, with one deadline for all setup."""
    deadline = time.monotonic() + timeout
    source = Path(__file__).with_name("native_ocr_reference.swift").resolve()
    binary = Path(directory) / "native-ocr-reference"
    report = {"completed": False, "passed": False, "timeout_seconds": timeout,
              "source": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    try:
        found = subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True, text=True,
                               timeout=min(_FIND_TIMEOUT, max(0.001, deadline - time.monotonic())))
        compiler = found.stdout.strip()
        if found.returncode != 0 or not Path(compiler).is_absolute() or not Path(compiler).is_file():
            raise ValueError("The system Swift compiler could not be located.")
        report["compiler"] = compiler
        located_sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True,
                                     timeout=min(_FIND_TIMEOUT, max(0.001, deadline - time.monotonic())))
        sdk = located_sdk.stdout.strip()
        if located_sdk.returncode != 0 or not Path(sdk).is_absolute() or not Path(sdk).is_dir():
            raise ValueError("The system macOS SDK could not be located.")
        report["sdk"] = sdk
        identified = subprocess.run([compiler, "--version"], capture_output=True, text=True,
                                    timeout=min(_FIND_TIMEOUT, max(0.001, deadline - time.monotonic())))
        if identified.returncode != 0:
            raise ValueError("The system Swift compiler could not identify its version.")
        report["compiler_version"] = identified.stdout[:2000]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Swift compiler setup exhausted its total time bound.")
        compiled = subprocess.run([compiler, "-sdk", sdk, str(source), "-module-cache-path", str(Path(directory) / "swift-cache"),
                                   "-o", str(binary)], capture_output=True, text=True, timeout=remaining)
        report.update({"completed": True, "child_exit_code": compiled.returncode,
                       "passed": compiled.returncode == 0 and binary.is_file(),
                       "child_stdout": compiled.stdout[:_LOG_LIMIT], "child_stderr": compiled.stderr[:_LOG_LIMIT]})
    except (OSError, ValueError, subprocess.TimeoutExpired, TimeoutError) as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
        if isinstance(error, subprocess.TimeoutExpired):
            report["timed_out"] = True
            stderr = error.stderr or b""
            report["child_stderr"] = (stderr.decode("utf-8", errors="replace")
                                       if isinstance(stderr, bytes) else stderr)[:_LOG_LIMIT]
    return binary, report


def _run_case(case, timeout, fixture=None, swift_binary=None, input_sha256=None):
    """Contain one exact test-owned child and preserve failed or timed-out native evidence."""
    started = time.monotonic()
    try:
        if case["backend"] == "swift":
            command = [str(swift_binary), case["name"], str(fixture), case["mode"],
                       str(case["level"]), str(case.get("revision", 0))]
        else:
            command = [sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--child", case["name"]]
            if fixture is not None:
                command.extend(["--fixture", str(fixture)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        lines = [line for line in result.stdout.splitlines() if line.startswith(_RESULT)]
        report = json.loads(lines[-1][len(_RESULT):]) if lines else {
            "case": case, "completed": False, "passed": False,
            "failure": {"type": "ChildFailure", "message": "Native child produced no result"}}
        if isinstance(report, dict) and case["backend"] == "swift" and report.get("case_name") == case["name"]:
            report["case"] = case
        if not isinstance(report, dict) or report.get("case") != case:
            raise ValueError("Native child returned evidence for a different case")
        if input_sha256 is not None and report.get("input_sha256") != input_sha256:
            raise ValueError("Native child used a different image from the generated comparison fixture")
        report["child_exit_code"] = result.returncode
        report["completed"] = report.get("completed") is True
        report["passed"] = bool(report.get("passed") is True and report.get("completed") is True
                                and report.get("native_success") is True and report.get("native_performed") is True
                                and result.returncode == 0)
        report["child_stages"] = [line[:1000] for line in result.stdout.splitlines()
                                  if not line.startswith(_RESULT)][:30]
        report["child_stderr"] = result.stderr[:_LOG_LIMIT]
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout or b"", error.stderr or b""
        report = {"case": case, "completed": False, "passed": False, "timed_out": True,
                  "failure": {"type": "TimeoutExpired", "message": "Native child exceeded its hard timeout"},
                  "child_stages": (stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes)
                                   else stdout)[:_LOG_LIMIT].splitlines(),
                  "child_stderr": (stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes)
                                   else stderr)[:_LOG_LIMIT]}
    except (OSError, ValueError, TypeError) as error:
        report = {"case": case, "completed": False, "passed": False,
                  "failure": {"type": type(error).__name__, "message": str(error)[:2000]}}
    report["timeout_seconds"] = timeout
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    """Persist the bounded request matrix as diagnostic evidence, never as a release gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("native-ocr-modes.json"))
    parser.add_argument("--child", choices=[case["name"] for case in _cases() if case["backend"] != "swift"])
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--generate-fixture", type=Path)
    arguments = parser.parse_args()
    if arguments.generate_fixture:
        return _generate_fixture(arguments.generate_fixture)
    if arguments.child:
        if arguments.fixture is None:
            parser.error("Native diagnostic children require their test-owned generated fixture.")
        return _child(next(case for case in _cases() if case["name"] == arguments.child), arguments.fixture)
    from release_check import fingerprint

    started = time.monotonic()
    report = {"fingerprint": fingerprint(), "scope": "generated-only Python/Swift OCR configuration diagnostic",
              "diagnostic_version": 2,
              "environment": {"macos": platform.mac_ver()[0], "python": sys.version,
                              "mcp": version("mcp")},
              "synthetic_only": True, "desktop_capture_forbidden": True,
              "per_child_timeout_seconds": _TIMEOUT, "total_timeout_seconds": _TOTAL_TIMEOUT,
              "fixture_timeout_seconds": _FIXTURE_TIMEOUT, "compiler_timeout_seconds": _COMPILER_TIMEOUT,
              "expected_cases": len(_cases()), "cases": [], "checks": []}
    with tempfile.TemporaryDirectory(prefix="klyk-native-ocr-") as directory:
        fixture = Path(directory) / "generated-hello.png"
        try:
            report["fixture"] = _prepare_fixture(fixture, _FIXTURE_TIMEOUT)
            remaining = _TOTAL_TIMEOUT - (time.monotonic() - started)
            swift_binary, report["swift_compiler"] = _prepare_swift(directory, min(_COMPILER_TIMEOUT, remaining))
            for case in _cases():
                remaining = _TOTAL_TIMEOUT - (time.monotonic() - started)
                if remaining <= 0:
                    report["error"] = "The diagnostic reached its total time bound before all cases ran."
                    break
                if case["backend"] == "swift" and not report["swift_compiler"]["passed"]:
                    result = {"case": case, "completed": False, "passed": False,
                              "failure": {"type": "CompilerFailure", "message": "The typed native reference did not compile."}}
                else:
                    result = _run_case(case, min(_TIMEOUT, remaining), fixture, swift_binary,
                                       report["fixture"]["input_sha256"])
                report["cases"].append(result)
                report["checks"].append({"name": case["name"], "passed": result["passed"] is True})
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
            report["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    report["completed"] = (len(report["cases"]) == report["expected_cases"]
                           and all(case["completed"] is True for case in report["cases"]))
    report["passed"] = (report["completed"] and all(check["passed"] for check in report["checks"]))
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                                encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "completed": report["completed"],
                      "output": str(arguments.output), "cases": len(report["cases"]),
                      "wall_seconds": report["wall_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
