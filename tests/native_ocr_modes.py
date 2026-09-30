"""Diagnose OCR modes on tiny generated pixels, with no desktop activity."""

import argparse
from contextlib import ExitStack
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

_RESULT = "KLYK_NATIVE_OCR_MODE_RESULT "
_TIMEOUT = 15
_TOTAL_TIMEOUT = 225
_LOG_LIMIT = 12000


def _cases():
    """Vary one relevant request setting while keeping pixels and native bindings fixed."""
    cases = []
    for level in (1, 0):
        for thread in ("main", "worker"):
            for language in ("system", "en-US"):
                cases.append({"name": f"production-{level}-{thread}-{language}",
                              "level": level, "thread": thread, "language": language,
                              "compute": "production"})
    for compute in ("configured_last", "automatic", "legacy_cpu"):
        cases.append({"name": f"accurate-worker-{compute}", "level": 0,
                      "thread": "worker", "language": "en-US", "compute": compute})
    cases.append({"name": "accurate-worker-configured-last-system", "level": 0,
                  "thread": "worker", "language": "system", "compute": "configured_last"})
    cases.append({"name": "accurate-worker-autorelease-pool", "level": 0,
                  "thread": "worker", "language": "en-US", "compute": "production",
                  "autorelease_pool": True})
    cases.append({"name": "accurate-worker-roi", "level": 0,
                  "thread": "worker", "language": "en-US", "compute": "production",
                  "region": [210, 80, 200, 120]})
    return cases


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


def _child(case):
    """Run one isolated production OCR request, capturing exact native outcomes."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from klyk import capture, ocr
    import klyk
    from native_image_smoke import _text_png
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
        elif case["compute"] == "legacy_cpu":
            request.setUsesCPUOnly_(True)

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
            """Choose deferred compute only after production finalizes all request properties."""
            request = requests[0]
            report["final_before_compute"] = _request_settings(request)
            if case["compute"] == "configured_last":
                original_compute(request)
            report["final_request"] = _request_settings(request)
            _stage("native performRequests")
            native_started = time.monotonic()
            success, native_error = self.native.performRequests_error_(requests, error)
            report["native_elapsed_seconds"] = round(time.monotonic() - native_started, 3)
            report["native_success"] = bool(success)
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
            guards.enter_context(patch.object(ocr, "_configure_compute", configure))
            guards.enter_context(patch.object(ocr, "VNImageRequestHandler", factory))
            _stage("generated CoreText bitmap")
            encoded = _text_png(capture)
            _stage("production OCR " + case["name"])
            try:
                if case["thread"] == "worker":
                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-diagnostic") as executor:
                        observations = executor.submit(recognize, encoded).result()
                else:
                    observations = recognize(encoded)
                report["observations"] = observations[:10]
                report["passed"] = any(item["text"].strip().upper() == "HELLO" for item in observations)
            finally:
                report["completed"] = True
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(_RESULT + json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
    return 0 if report["completed"] and report["passed"] else 1


def _run_case(case, timeout):
    """Contain one exact test-owned child and preserve failed or timed-out native evidence."""
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-B", "-u", str(Path(__file__).resolve()),
                                 "--child", case["name"]],
                                capture_output=True, text=True, timeout=timeout)
        lines = [line for line in result.stdout.splitlines() if line.startswith(_RESULT)]
        report = json.loads(lines[-1][len(_RESULT):]) if lines else {
            "case": case, "completed": False, "passed": False,
            "failure": {"type": "ChildFailure", "message": "Native child produced no result"}}
        if not isinstance(report, dict) or report.get("case") != case:
            raise ValueError("Native child returned evidence for a different case")
        report["child_exit_code"] = result.returncode
        report["completed"] = report.get("completed") is True
        report["passed"] = bool(report.get("passed") is True and report.get("completed") is True
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
    except (ValueError, TypeError) as error:
        report = {"case": case, "completed": False, "passed": False,
                  "failure": {"type": type(error).__name__, "message": str(error)[:2000]}}
    report["timeout_seconds"] = timeout
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    """Persist the bounded request matrix as diagnostic evidence, never as a release gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("native-ocr-modes.json"))
    parser.add_argument("--child", choices=[case["name"] for case in _cases()])
    arguments = parser.parse_args()
    if arguments.child:
        return _child(next(case for case in _cases() if case["name"] == arguments.child))
    from release_check import fingerprint

    started = time.monotonic()
    report = {"fingerprint": fingerprint(), "scope": "generated-only OCR mode diagnostic",
              "environment": {"macos": platform.mac_ver()[0], "python": sys.version,
                              "mcp": version("mcp")},
              "synthetic_only": True, "desktop_capture_forbidden": True,
              "per_child_timeout_seconds": _TIMEOUT, "total_timeout_seconds": _TOTAL_TIMEOUT,
              "expected_cases": len(_cases()), "cases": [], "checks": []}
    for case in _cases():
        remaining = _TOTAL_TIMEOUT - (time.monotonic() - started)
        if remaining <= 0:
            report["error"] = "The diagnostic reached its total time bound before all cases ran."
            break
        result = _run_case(case, min(_TIMEOUT, remaining))
        report["cases"].append(result)
        report["checks"].append({"name": case["name"], "passed": result["passed"] is True})
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
