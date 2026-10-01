"""Verify the generated-only OCR diagnostic's process and evidence bounds hermetically."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("native_ocr_modes", Path(__file__).with_name("native_ocr_modes.py"))
modes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modes)


class _Request(SimpleNamespace):
    """Expose supported revisions through a fake class API without loading Foundation."""

    @classmethod
    def supportedRevisions(cls):
        """Advertise the three bounded revisions under test."""
        return SimpleNamespace(containsIndex_=lambda value: value in (1, 2, 3))


class NativeOcrModeTests(unittest.TestCase):
    """Portable regressions must never execute the native diagnostic child."""

    def test_core_matrix_retains_controls_and_native_differentials(self):
        """Refuted variants give way to native configuration and revision comparisons."""
        cases = modes._cases()
        production = [case for case in cases if case["backend"] == "production"]
        self.assertEqual({(case["level"], case["thread"], case["language"]) for case in production},
                         {(level, "worker", "system") for level in (0, 1)})
        raw = [case for case in cases if case["backend"] == "python_raw"]
        self.assertEqual({case["mode"] for case in raw},
                         {"defaults", "native_nsstring", "language_correction", "production"})
        self.assertEqual({(case["backend"], case.get("revision")) for case in cases if "revision" in case},
                         {(backend, revision) for backend in ("python_raw", "swift") for revision in (1, 2)})
        self.assertTrue(any(case["backend"] == "swift" and case["level"] == 1 for case in cases))
        self.assertEqual({case["level"] for case in cases if case.get("mode") == "modern"}, {0, 1})
        self.assertEqual(len(cases), 14)
        self.assertEqual(len({case["name"] for case in cases}), len(cases))
        self.assertLessEqual(len(cases) * modes._TIMEOUT + modes._FIXTURE_TIMEOUT + modes._COMPILER_TIMEOUT,
                             modes._TOTAL_TIMEOUT)

    def test_completed_native_failure_remains_failed(self):
        """A false native result is retained rather than converted to empty success."""
        case = modes._cases()[0]
        child = {"case": case, "completed": True, "passed": False,
                 "native_success": False, "native_error": None}
        result = SimpleNamespace(returncode=1, stdout=modes._RESULT + json.dumps(child), stderr="native note")
        with patch.object(modes.subprocess, "run", return_value=result) as run:
            report = modes._run_case(case, modes._TIMEOUT)
        self.assertTrue(report["completed"])
        self.assertFalse(report["passed"])
        self.assertIsNone(report["native_error"])
        self.assertEqual(run.call_args.kwargs["timeout"], modes._TIMEOUT)
        self.assertEqual(report["child_exit_code"], 1)
        self.assertIn("--child", run.call_args.args[0])

    def test_timeout_contains_exact_child_and_retains_bounded_stderr(self):
        """A native hang cannot become a pass or spill unlimited diagnostic logs."""
        case = modes._cases()[0]
        timeout = subprocess.TimeoutExpired(["test-owned-child"], 15,
            output=b'{"stage":"native performRequests"}\n', stderr=b"x" * 20000)
        with patch.object(modes.subprocess, "run", side_effect=timeout):
            report = modes._run_case(case, 15)
        self.assertFalse(report["completed"])
        self.assertFalse(report["passed"])
        self.assertTrue(report["timed_out"])
        self.assertEqual(len(report["child_stderr"]), modes._LOG_LIMIT)
        self.assertIn("performRequests", report["child_stages"][0])

    def test_failed_process_cannot_pass_from_positive_json(self):
        """The exact child must exit successfully as well as report recognized glyphs."""
        case = modes._cases()[0]
        result = SimpleNamespace(returncode=1, stdout=modes._RESULT + json.dumps(
            {"case": case, "completed": True, "passed": True}), stderr="")
        with patch.object(modes.subprocess, "run", return_value=result):
            self.assertFalse(modes._run_case(case, 15)["passed"])

    def test_wrong_case_and_missing_result_cannot_pass(self):
        """Evidence from a different request or no result never qualifies this case."""
        case = modes._cases()[0]
        for stdout in ("", modes._RESULT + json.dumps(
                {"case": modes._cases()[1], "completed": True, "passed": True})):
            with self.subTest(stdout=bool(stdout)), patch.object(modes.subprocess, "run", return_value=
                    SimpleNamespace(returncode=0, stdout=stdout, stderr="")):
                report = modes._run_case(case, 15)
            self.assertFalse(report["passed"])
            self.assertFalse(report["completed"])

    def test_malformed_or_nonboolean_completion_cannot_pass(self):
        """Missing or malformed completion evidence must remain a retained failure."""
        case = modes._cases()[0]
        for child in ([], {"case": case, "passed": True},
                      {"case": case, "completed": "true", "passed": True}):
            with self.subTest(child=child), patch.object(modes.subprocess, "run", return_value=
                    SimpleNamespace(returncode=0, stdout=modes._RESULT+json.dumps(child), stderr="")):
                report = modes._run_case(case, 15)
            self.assertFalse(report["passed"])
            self.assertFalse(report["completed"])

    def test_supported_model_queries_are_after_native_perform(self):
        """Instrumentation cannot warm device or model queries before production executes."""
        import ast
        source = Path(modes.__file__).read_text()
        tree = ast.parse(source)
        child = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_child")
        calls = [node for node in ast.walk(child) if isinstance(node, ast.Call)]
        metadata_calls = [node for node in calls if isinstance(node.func, ast.Name)
                          and node.func.id == "_request_metadata"]
        native_call = next(node for node in calls if isinstance(node.func, ast.Attribute)
                          and node.func.attr == "performRequests_error_")
        self.assertEqual(len(metadata_calls), 1)
        self.assertGreater(metadata_calls[0].lineno, native_call.lineno)

    def test_generated_fixture_is_small_and_fixed_size_before_native_decode(self):
        """Arbitrary or oversized images never become native diagnostic input."""
        raw = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
        raw += (512).to_bytes(4, "big") + (256).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 4
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "generated.png"
            fixture.write_bytes(raw)
            self.assertEqual(modes._fixture_bytes(fixture), raw)
            for invalid in (b"bad", raw[:16] + (513).to_bytes(4, "big") + raw[20:],
                            raw + b"x" * modes._FIXTURE_LIMIT):
                with self.subTest(size=len(invalid)):
                    fixture.write_bytes(invalid)
                    with self.assertRaises(ValueError):
                        modes._fixture_bytes(fixture)

    def test_image_digest_mismatch_cannot_pass_either_backend(self):
        """Each native result must bind to the same generated PNG as the reference."""
        for backend in ("production", "swift"):
            case = next(case for case in modes._cases() if case["backend"] == backend)
            child = {"case": case, "case_name": case["name"], "completed": True,
                     "passed": True, "input_sha256": "different"}
            with self.subTest(backend=backend), patch.object(modes.subprocess, "run", return_value=
                    SimpleNamespace(returncode=0, stdout=modes._RESULT + json.dumps(child), stderr="")):
                report = modes._run_case(case, 12, "/test-owned/generated.png", "/test-owned/reference", "expected")
            self.assertFalse(report["passed"])
            self.assertFalse(report["completed"])

    def test_swift_exact_case_and_digest_are_required(self):
        """The direct native reference shares the process timeout and exact input binding."""
        case = next(case for case in modes._cases() if case["backend"] == "swift")
        child = {"case_name": case["name"], "completed": True, "passed": True, "input_sha256": "expected",
                 "native_success": True, "native_performed": True}
        with patch.object(modes.subprocess, "run", return_value=
                SimpleNamespace(returncode=0, stdout=modes._RESULT + json.dumps(child), stderr="")) as run:
            report = modes._run_case(case, 12, "/test-owned/generated.png", "/test-owned/reference", "expected")
        self.assertTrue(report["passed"])
        self.assertEqual(run.call_args.args[0][:3], ["/test-owned/reference", case["name"], "/test-owned/generated.png"])
        self.assertEqual(run.call_args.kwargs["timeout"], 12)

    def test_compiler_setup_shares_one_deadline_and_keeps_failures(self):
        """Compiler identification and compilation cannot each consume a fresh total budget."""
        responses = [SimpleNamespace(returncode=0, stdout="/test-owned/swiftc\n", stderr=""),
                     SimpleNamespace(returncode=0, stdout="/test-owned/MacOSX.sdk\n", stderr=""),
                     SimpleNamespace(returncode=0, stdout="Swift version 6.4\n", stderr=""),
                     SimpleNamespace(returncode=1, stdout="", stderr="type error" * 3000)]
        with patch.object(modes.subprocess, "run", side_effect=responses) as run, \
                patch.object(modes.time, "monotonic", side_effect=[100, 101, 102, 103, 104]), \
                patch.object(modes.Path, "is_file", return_value=True), patch.object(modes.Path, "is_dir", return_value=True):
            binary, report = modes._prepare_swift("/test-owned", 30)
        self.assertTrue(report["completed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["child_exit_code"], 1)
        self.assertEqual(len(report["child_stderr"]), modes._LOG_LIMIT)
        self.assertEqual(run.call_args_list[-1].kwargs["timeout"], 26)
        self.assertEqual(run.call_args_list[-1].args[0][1:3], ["-sdk", "/test-owned/MacOSX.sdk"])
        self.assertEqual(run.call_args_list[-1].args[0][3:5], ["-swift-version", "6"])
        self.assertEqual(report["sdk"], "/test-owned/MacOSX.sdk")
        self.assertEqual(str(binary), "/test-owned/native-ocr-reference")

    def test_reference_has_no_ui_imports_or_optional_json_nil(self):
        """The generated typed reference has no desktop surface or ambiguous nil JSON."""
        source = Path(modes.__file__).with_name("native_ocr_reference.swift").read_text()
        imports = {line.split()[1] for line in source.splitlines() if line.startswith("import ")}
        self.assertEqual(imports, {"Foundation", "CoreGraphics", "ImageIO", "Vision", "CoreML", "CryptoKit"})
        for forbidden in ("AppKit", "NSApplication", "CGWindowListCreateImage", "CGDisplayCreateImage",
                          "ScreenCaptureKit", "NSPasteboard", "NSEvent", "AudioToolbox"):
            self.assertNotIn(forbidden, source)
        self.assertIn("request.results.map { $0.count as Any } ?? NSNull()", source)

    def test_raw_default_request_preserves_framework_settings(self):
        """The defaults comparison must not accidentally retain production setters."""
        namespace, request, handler = self._raw_namespace()
        case = next(case for case in modes._cases() if case["name"] == "python-accurate-defaults")
        quartz = SimpleNamespace(CGImageGetBitsPerComponent=lambda image: 8,
                                CGImageGetBitsPerPixel=lambda image: 32, CGImageGetAlphaInfo=lambda image: 5)
        with patch.dict(sys.modules, {"Quartz": quartz}), patch.object(modes, "_stage"), \
                patch.object(modes, "_request_metadata", return_value={}):
            report = {}
            modes._raw_request(case, b"test-owned", namespace, report)
        namespace._configure_compute.assert_not_called()
        request.setRecognitionLevel_.assert_not_called()
        request.setRecognitionLanguages_.assert_not_called()
        request.setUsesLanguageCorrection_.assert_not_called()
        handler.performRequests_error_.assert_called_once()
        self.assertTrue(report["native_success"])

    def _raw_namespace(self, request=None, success=True):
        """Build only fake native objects for the raw diagnostic's observable boundaries."""
        if request is None:
            request = _Request(recognitionLevel=lambda: 0, revision=lambda: 3,
                               usesLanguageCorrection=lambda: True, recognitionLanguages=lambda: ["en_US"],
                               setRecognitionLevel_=MagicMock(), setRevision_=MagicMock(),
                               setUsesLanguageCorrection_=MagicMock(), setRecognitionLanguages_=MagicMock(),
                               results=lambda: None)
        handler = SimpleNamespace(performRequests_error_=MagicMock(return_value=(success, None)))
        namespace = SimpleNamespace(NSData=SimpleNamespace(dataWithBytes_length_=lambda raw, size: "data"),
            CGImageSourceCreateWithData=lambda data, opts: "source",
            CGImageSourceCreateImageAtIndex=lambda source, index, opts: "image",
            CGImageGetWidth=lambda image: 512, CGImageGetHeight=lambda image: 256,
            VNRecognizeTextRequest=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: request)),
            VNImageRequestHandler=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithCGImage_options_=lambda image, opts: handler)),
            NSDictionary=SimpleNamespace(dictionary=lambda: {}),
            NSArray=SimpleNamespace(arrayWithArray_=list, arrayWithObject_=lambda item: [item]),
            _configure_compute=MagicMock())
        return namespace, request, handler

    def test_raw_revision_is_checked_and_set_before_compute_and_native_execution(self):
        """Unsupported revisions cannot execute, and supported revisions configure their own model."""
        namespace, request, handler = self._raw_namespace()
        case = next(case for case in modes._cases() if case["name"] == "python-accurate-revision-2")
        steps = []
        request.setRevision_.side_effect = lambda revision: steps.append(("revision", revision))
        namespace._configure_compute.side_effect = lambda request: steps.append(("compute",))
        handler.performRequests_error_.side_effect = lambda requests, error: (steps.append(("perform",)) or (True, None))
        quartz = SimpleNamespace(CGImageGetBitsPerComponent=lambda image: 8,
                                CGImageGetBitsPerPixel=lambda image: 32, CGImageGetAlphaInfo=lambda image: 5)
        with patch.dict(sys.modules, {"Quartz": quartz}), patch.object(modes, "_stage"), \
                patch.object(modes, "_request_metadata", return_value={}):
            modes._raw_request(case, b"test-owned", namespace, {})
            self.assertEqual(steps, [("revision", 2), ("compute",), ("perform",)])
            handler.performRequests_error_.reset_mock()
            with patch.object(_Request, "supportedRevisions", return_value=SimpleNamespace(containsIndex_=lambda value: False)), \
                    self.assertRaisesRegex(RuntimeError, "not advertised"):
                modes._raw_request(case, b"test-owned", namespace, {})
        handler.performRequests_error_.assert_not_called()

    def test_false_nil_native_outcome_is_preserved_before_raw_failure(self):
        """Raw PyObjC failure and metadata remain available for comparison with typed Swift."""
        namespace, request, handler = self._raw_namespace(success=False)
        case = next(case for case in modes._cases() if case["name"] == "python-accurate-defaults")
        quartz = SimpleNamespace(CGImageGetBitsPerComponent=lambda image: 8,
                                CGImageGetBitsPerPixel=lambda image: 32, CGImageGetAlphaInfo=lambda image: 5)
        report = {}
        with patch.dict(sys.modules, {"Quartz": quartz}), patch.object(modes, "_stage"), \
                patch.object(modes, "_request_metadata", return_value={"supported_revisions": [1, 2, 3]}), \
                self.assertRaisesRegex(RuntimeError, "no native result"):
            modes._raw_request(case, b"test-owned", namespace, report)
        self.assertFalse(report["native_success"])
        self.assertIsNone(report["native_error"])
        self.assertTrue(report["native_results_nil"])
        self.assertTrue(report["native_performed"])
        self.assertEqual(report["native_metadata_after_request"]["supported_revisions"], [1, 2, 3])

    def test_native_string_elements_and_correction_are_independent_variants(self):
        """Native string creation and correction changes remain distinct request comparisons."""
        quartz = SimpleNamespace(CGImageGetBitsPerComponent=lambda image: 8,
                                CGImageGetBitsPerPixel=lambda image: 32, CGImageGetAlphaInfo=lambda image: 5)
        native_string = object()
        initializer = MagicMock(return_value=SimpleNamespace(nsstring=lambda: native_string))
        foundation = SimpleNamespace(NSString=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithUTF8String_=initializer)))
        for mode in ("native_nsstring", "language_correction"):
            case = next(case for case in modes._cases() if case.get("mode") == mode)
            namespace, request, handler = self._raw_namespace()
            with self.subTest(mode=mode), patch.dict(sys.modules, {"Quartz": quartz, "Foundation": foundation}), \
                    patch.object(modes, "_stage"), patch.object(modes, "_request_metadata", return_value={}):
                modes._raw_request(case, b"test-owned", namespace, {})
            request.setUsesLanguageCorrection_.assert_called_once_with(mode == "language_correction")
            request.setRecognitionLanguages_.assert_called_once_with([native_string] if mode == "native_nsstring" else ["en-US"])
        initializer.assert_called_once_with(b"en-US")

    def test_native_false_or_missing_execution_never_qualifies_a_positive_payload(self):
        """A glyph claim also requires direct native execution and successful native return."""
        case = modes._cases()[0]
        for success, performed in ((False, True), (None, True), (True, False), (True, "true")):
            child = {"case": case, "completed": True, "passed": True,
                     "native_success": success, "native_performed": performed}
            with self.subTest(success=success, performed=performed), patch.object(modes.subprocess, "run", return_value=
                    SimpleNamespace(returncode=0, stdout=modes._RESULT + json.dumps(child), stderr="")):
                report = modes._run_case(case, 12)
            self.assertFalse(report["passed"])


if __name__ == "__main__":
    unittest.main()
