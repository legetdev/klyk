"""Verify the generated-only OCR diagnostic's process and evidence bounds hermetically."""

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_ocr_modes", Path(__file__).with_name("native_ocr_modes.py"))
modes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modes)


class NativeOcrModeTests(unittest.TestCase):
    """Portable regressions must never execute the native diagnostic child."""

    def test_core_matrix_distinguishes_level_language_and_executor(self):
        """Eight production cases isolate all three actual request differences."""
        cases = modes._cases()
        production = [case for case in cases if case["compute"] == "production"
                      and not case.get("autorelease_pool") and not case.get("region")]
        self.assertEqual({(case["level"], case["thread"], case["language"]) for case in production},
                         {(level, thread, language) for level in (0, 1)
                          for thread in ("main", "worker") for language in ("system", "en-US")})
        self.assertEqual(len({case["name"] for case in cases}), len(cases))
        self.assertLessEqual(len(cases) * modes._TIMEOUT, modes._TOTAL_TIMEOUT)

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
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
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


if __name__ == "__main__":
    unittest.main()
