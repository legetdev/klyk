"""Portable OCR contracts using fake Foundation, Quartz, and Vision objects."""

import base64
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from test_input_cleanup import load_functions


class _NativeArray:
    """Small native-array stand-in that records the requested boundary."""

    def __init__(self, values):
        self.values = values


class _NativeDictionary:
    """Native dictionary stand-in used to reject plain Python mappings."""


class OcrContractTests(unittest.TestCase):
    """Exercise Vision setup and coordinate contracts without native imports."""

    def _namespace(self, request, handler):
        """Build fake native bindings for one isolated `recognize_all` call."""
        data = SimpleNamespace(dataWithBytes_length_=MagicMock(return_value="data"))
        dictionary = SimpleNamespace(dictionary=MagicMock(return_value=_NativeDictionary()))
        array = SimpleNamespace(
            arrayWithArray_=lambda values: _NativeArray(values),
            arrayWithObject_=lambda value: _NativeArray([value]),
        )
        init_handler = MagicMock(return_value=handler)
        handler._init_handler = init_handler
        return {
            "base64": base64,
            "NSData": data,
            "NSDictionary": dictionary,
            "NSArray": array,
            "CGImageSourceCreateWithData": MagicMock(return_value="source"),
            "CGImageSourceCreateImageAtIndex": MagicMock(return_value="image"),
            "CGImageGetWidth": MagicMock(return_value=200),
            "CGImageGetHeight": MagicMock(return_value=100),
            "VNImageRequestHandler": SimpleNamespace(
                alloc=lambda: SimpleNamespace(initWithCGImage_options_=init_handler)
            ),
            "VNRecognizeTextRequest": SimpleNamespace(
                alloc=lambda: SimpleNamespace(init=lambda: request)
            ),
            "_AVAILABLE": True,
            "_SYSTEM_LANGS": ["en-US"],
            "_require": lambda: None,
            "objc": SimpleNamespace(lookUpClass=lambda name: object()),
        }

    def test_invalid_region_is_rejected_before_recognition(self):
        """An out-of-bounds region must fail before Vision receives a request."""
        request = MagicMock()
        handler = MagicMock()
        ns = self._namespace(request, handler)
        load_functions("ocr.py", {"recognize_all"}, ns)
        with self.assertRaisesRegex(ValueError, "fit inside"):
            ns["recognize_all"]("aGVsbG8=", region=(150, 80, 60, 30))
        handler.performRequests_error_.assert_not_called()

    def test_region_is_normalized_and_results_return_full_image_coordinates(self):
        """Vision gets a bottom-left ROI while observations return screenshot pixels."""
        request = MagicMock()
        request.results.return_value = [
            SimpleNamespace(
                topCandidates_=lambda count: [
                    SimpleNamespace(string=lambda: "Hello", confidence=lambda: 0.91)
                ],
                boundingBox=lambda: SimpleNamespace(
                    origin=SimpleNamespace(x=0.25, y=0.2),
                    size=SimpleNamespace(width=0.2, height=0.1),
                ),
            )
        ]
        request.supportedComputeStageDevicesAndReturnError_.return_value = ({}, None)
        handler = MagicMock()
        handler.performRequests_error_.return_value = (True, None)
        ns = self._namespace(request, handler)
        load_functions("ocr.py", {"recognize_all", "_configure_compute"}, ns)
        result = ns["recognize_all"]("aGVsbG8=", region=(20, 10, 100, 40))
        self.assertEqual(request.setRegionOfInterest_.call_args.args[0], ((0.1, 0.5), (0.5, 0.4)))
        self.assertEqual(result[0]["text"], "Hello")
        self.assertEqual((result[0]["x"], result[0]["y"], result[0]["width"], result[0]["height"]), (55, 40, 20, 4))

    def test_recognition_failure_raises(self):
        """A native Vision failure must not be reported as an empty OCR result."""
        request = MagicMock()
        request.supportedComputeStageDevicesAndReturnError_.return_value = ({}, None)
        handler = MagicMock()
        handler.performRequests_error_.return_value = (False, "Vision failed")
        ns = self._namespace(request, handler)
        load_functions("ocr.py", {"recognize_all", "_configure_compute"}, ns)
        with self.assertRaisesRegex(RuntimeError, "Vision failed"):
            ns["recognize_all"]("aGVsbG8=")

    def test_modern_compute_selects_supported_cpu_devices(self):
        """Modern Vision stages select an available ML CPU device per stage."""
        request = MagicMock()
        cpu_class = object()
        cpu = MagicMock()
        cpu.isKindOfClass_.side_effect = lambda cls: cls is cpu_class
        other = MagicMock()
        other.isKindOfClass_.return_value = False
        request.supportedComputeStageDevicesAndReturnError_.return_value = ({"a": [other, cpu]}, None)
        ns = {
            "objc": SimpleNamespace(lookUpClass=lambda name: cpu_class),
        }
        load_functions("ocr.py", {"_configure_compute"}, ns)
        ns["_configure_compute"](request)
        request.setComputeDevice_forComputeStage_.assert_called_once_with(cpu, "a")
        request.setUsesCPUOnly_.assert_not_called()

    def test_old_compute_api_uses_cpu_only_fallback(self):
        """Older Vision APIs retain the supported CPU-only compatibility path."""
        request = SimpleNamespace(setUsesCPUOnly_=MagicMock())
        ns = {"objc": SimpleNamespace(lookUpClass=lambda name: object())}
        load_functions("ocr.py", {"_configure_compute"}, ns)
        ns["_configure_compute"](request)
        request.setUsesCPUOnly_.assert_called_once_with(True)

    def test_native_foundation_collections_cross_vision_boundary(self):
        """Vision receives native NSDictionary/NSArray wrappers at both boundaries."""
        request = MagicMock()
        request.supportedComputeStageDevicesAndReturnError_.return_value = ({}, None)
        handler = MagicMock()
        handler.performRequests_error_.return_value = (True, None)
        ns = self._namespace(request, handler)
        load_functions("ocr.py", {"recognize_all", "_configure_compute"}, ns)
        ns["recognize_all"]("aGVsbG8=", languages=["de-DE"])
        self.assertIsInstance(handler._init_handler.call_args.args[1], _NativeDictionary)
        requests = handler.performRequests_error_.call_args.args[0]
        self.assertIsInstance(requests, _NativeArray)
        self.assertIsInstance(request.setRecognitionLanguages_.call_args.args[0], _NativeArray)


if __name__ == "__main__":
    unittest.main()
