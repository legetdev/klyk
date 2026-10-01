"""Keep synthetic native evidence honest without invoking any native framework."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_image_smoke", Path(__file__).with_name("native_image_smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class NativeImageSmokeTests(unittest.TestCase):
    """An interrupted native child cannot be mistaken for complete verified evidence."""

    def test_full_image_and_roi_transforms_are_distinguishable(self):
        """A full-image box rescaled a second time fails dimensions and center checks."""
        full = [233 / 512, 1 - (125 + 32) / 256, 133 / 512, 32 / 256]
        original = smoke._image_box(full)
        transformed = smoke._image_box(full, (210, 80, 200, 120))
        self.assertEqual(smoke._box_error(original, [233, 125, 133, 32]), 0)
        self.assertGreater(smoke._box_error(original, transformed), 80)

    def test_failed_child_preserves_evidence_and_nonzero_exit(self):
        """A completed diagnostic with incorrect production boxes remains a failure."""
        child_report = {"completed": True, "passed": False, "native_roi_contract": "full_image"}
        result = SimpleNamespace(returncode=1, stdout=smoke._RESULT + json.dumps(child_report), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "native-image.json"
            with patch.object(sys, "argv", ["fixture", "--output", str(output)]), \
                    patch.object(smoke.subprocess, "run", return_value=result) as run, \
                    patch("builtins.print"):
                self.assertEqual(smoke.main(), 1)
            report = json.loads(output.read_text())
            self.assertFalse(report["passed"])
            self.assertEqual(report["native_roi_contract"], "full_image")
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_timeout_retains_last_stage_and_never_claims_pass(self):
        """The standard-library subprocess timeout contains the exact fixture child."""
        timeout = subprocess.TimeoutExpired(["test-owned-child"], 30,
            output=b'{"stage":"native ROI fast OCR"}\n', stderr=b"native limitation")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "native-image.json"
            with patch.object(sys, "argv", ["fixture", "--output", str(output)]), \
                    patch.object(smoke.subprocess, "run", side_effect=timeout), \
                    patch("builtins.print"):
                self.assertEqual(smoke.main(), 1)
            report = json.loads(output.read_text())
            self.assertTrue(report["timed_out"])
            self.assertFalse(report["completed"])
            self.assertFalse(report["passed"])
            self.assertIn("native ROI fast OCR", report["child_stages"][0])
            self.assertEqual(report["child_stderr"], "native limitation")

    def test_inconsistent_child_exit_cannot_claim_pass(self):
        """A successful-looking report is insufficient when its process failed."""
        result = SimpleNamespace(returncode=1,
            stdout=smoke._RESULT + json.dumps({"completed": True, "passed": True}), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "native-image.json"
            with patch.object(sys, "argv", ["fixture", "--output", str(output)]), \
                    patch.object(smoke.subprocess, "run", return_value=result), \
                    patch("builtins.print"):
                self.assertEqual(smoke.main(), 1)
            self.assertFalse(json.loads(output.read_text())["passed"])


if __name__ == "__main__":
    unittest.main()
