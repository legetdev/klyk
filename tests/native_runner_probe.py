"""Read existing permissions and display metadata on an isolated GitHub macOS runner."""

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys


class Point(ctypes.Structure):
    """Represent a 64-bit CoreGraphics point without importing a UI framework."""
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class Size(ctypes.Structure):
    """Represent a 64-bit CoreGraphics size in logical display points."""
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class Rect(ctypes.Structure):
    """Preserve the native CGRect layout for the metadata-only display query."""
    _fields_ = [('origin', Point), ('size', Size)]


def main():
    """Fail without requesting permissions or touching the desktop when a gate is absent."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.verification/runner-probe.json')
    parser.add_argument('--full-layout', action='store_true')
    args = parser.parse_args()
    report = {'completed': False, 'desktop_acceptance': False, 'checks': [],
              'environment': {'python': sys.version, 'macos': platform.mac_ver()[0],
                              'architecture': platform.machine(),
                              'image_version': os.environ.get('ImageVersion'),
                              'runner_label': os.environ.get('KLYK_RUNNER_LABEL')}}

    def check(name, condition):
        """Keep missing gates explicit in the retained probe evidence."""
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise RuntimeError(name)

    try:
        check('isolated GitHub-hosted macOS runner', sys.platform == 'darwin'
              and os.environ.get('GITHUB_ACTIONS') == 'true'
              and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
              and os.environ.get('RUNNER_OS') == 'macOS')
        appservices = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        appservices.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        appservices.AXIsProcessTrustedWithOptions.restype = ctypes.c_ubyte
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGPreflightScreenCaptureAccess.argtypes = []
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = Rect
        accessibility = bool(appservices.AXIsProcessTrustedWithOptions(None))
        capture = bool(cg.CGPreflightScreenCaptureAccess())
        display_id = cg.CGMainDisplayID()
        bounds = cg.CGDisplayBounds(display_id)
        report['permissions'] = {'accessibility': accessibility, 'screen_capture': capture,
                                 'prompt_requested': False}
        coordinates = {'x': bounds.origin.x, 'y': bounds.origin.y,
                       'width': bounds.size.width, 'height': bounds.size.height}
        report['display'] = {'id': display_id, **{
            name: value if math.isfinite(value) else None for name, value in coordinates.items()}}
        check('existing Accessibility authorization', accessibility)
        check('existing Screen Recording authorization', capture)
        check('active finite desktop display', display_id != 0
              and all(math.isfinite(value) for value in coordinates.values())
              and bounds.size.width > 0 and bounds.size.height > 0)
        swift = subprocess.run(['xcrun', '--find', 'swiftc'], capture_output=True,
                               text=True, timeout=10)
        report['swift_compiler'] = swift.stdout.strip()
        check('installed native Swift compiler', swift.returncode == 0 and bool(swift.stdout.strip()))
        chrome = Path('/Applications/Google Chrome.app/Contents/Info.plist')
        check('installed Chrome fixture target', chrome.is_file())
        report['chrome_version'] = plistlib.loads(chrome.read_bytes()).get('CFBundleShortVersionString')
        if args.full_layout:
            # Existing two-window fixtures plus receiver offset reach x=1820;
            # refuse an undersized desktop rather than weakening those tests.
            check('desktop fits the existing native fixture layout',
                  bounds.size.width >= 1820 and bounds.size.height >= 800)
        report['completed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False))
        print(json.dumps(report, allow_nan=False), flush=True)
    if not report['completed']:
        raise SystemExit('Runner preflight failed; no permissions were requested or changed.')


if __name__ == '__main__':
    main()
