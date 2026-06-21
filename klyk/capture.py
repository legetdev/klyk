"""
Screen capture and window management.
Primary path: CoreGraphics in-memory capture (~40ms, no subprocesses).
Fallback: screencapture + sips CLI pipeline.
All returned dimensions are in logical points matching the CGEvent coordinate space.
"""

import base64
import ctypes
import ctypes.util
import os
import subprocess
import tempfile
import time

# ---------------------------------------------------------------------------
# Framework loading
# ---------------------------------------------------------------------------

_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

try:
    _imageio = ctypes.CDLL(ctypes.util.find_library("ImageIO"))
    _HAS_IMAGEIO = True
except Exception:
    _HAS_IMAGEIO = False

# ---------------------------------------------------------------------------
# Structs
# ---------------------------------------------------------------------------

class CGRect(ctypes.Structure):
    _fields_ = [
        ("x",      ctypes.c_double),
        ("y",      ctypes.c_double),
        ("width",  ctypes.c_double),
        ("height", ctypes.c_double),
    ]

# ---------------------------------------------------------------------------
# CoreFoundation / CoreGraphics function signatures — window enumeration
# ---------------------------------------------------------------------------

_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]

_cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]

_cf.CFDictionaryGetValue.restype = ctypes.c_void_p
_cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

_cf.CFNumberGetValue.restype = ctypes.c_bool
_cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]

_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_cf.CFDataCreateMutable.restype = ctypes.c_void_p
_cf.CFDataCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long]

_cf.CFDataGetLength.restype = ctypes.c_long
_cf.CFDataGetLength.argtypes = [ctypes.c_void_p]

_cf.CFDataGetBytePtr.restype = ctypes.c_void_p
_cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]

_cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
_cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

_cg.CGMainDisplayID.restype = ctypes.c_uint32
_cg.CGMainDisplayID.argtypes = []

_cg.CGDisplayBounds.restype = CGRect
_cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]

_cg.CGGetActiveDisplayList.restype = ctypes.c_int32
_cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]

# CoreGraphics in-memory capture
_cg.CGDisplayCreateImageForRect.restype = ctypes.c_void_p
_cg.CGDisplayCreateImageForRect.argtypes = [ctypes.c_uint32, CGRect]

_cg.CGWindowListCreateImage.restype = ctypes.c_void_p
_cg.CGWindowListCreateImage.argtypes = [CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]

_cg.CGImageGetWidth.restype = ctypes.c_ulong
_cg.CGImageGetWidth.argtypes = [ctypes.c_void_p]

_cg.CGImageGetHeight.restype = ctypes.c_ulong
_cg.CGImageGetHeight.argtypes = [ctypes.c_void_p]

_cg.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p
_cg.CGColorSpaceCreateDeviceRGB.argtypes = []

_cg.CGBitmapContextCreate.restype = ctypes.c_void_p
_cg.CGBitmapContextCreate.argtypes = [
    ctypes.c_void_p,   # data (NULL = auto-allocate)
    ctypes.c_ulong,    # width
    ctypes.c_ulong,    # height
    ctypes.c_ulong,    # bitsPerComponent
    ctypes.c_ulong,    # bytesPerRow
    ctypes.c_void_p,   # colorspace
    ctypes.c_uint32,   # bitmapInfo
]
_cg.CGContextDrawImage.restype = None
_cg.CGContextDrawImage.argtypes = [ctypes.c_void_p, CGRect, ctypes.c_void_p]

_cg.CGContextTranslateCTM.restype = None
_cg.CGContextTranslateCTM.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]

_cg.CGContextScaleCTM.restype = None
_cg.CGContextScaleCTM.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]

_cg.CGBitmapContextCreateImage.restype = ctypes.c_void_p
_cg.CGBitmapContextCreateImage.argtypes = [ctypes.c_void_p]

# Direct CGImage pixel-access path — used by get_pixel/get_pixels so they don't
# round-trip through a separate bitmap context (which historically permuted
# channels and flipped Y in subtle ways depending on host endianness).
_cg.CGImageGetDataProvider.restype = ctypes.c_void_p
_cg.CGImageGetDataProvider.argtypes = [ctypes.c_void_p]
_cg.CGDataProviderCopyData.restype = ctypes.c_void_p
_cg.CGDataProviderCopyData.argtypes = [ctypes.c_void_p]
_cg.CGImageGetBytesPerRow.restype = ctypes.c_ulong
_cg.CGImageGetBytesPerRow.argtypes = [ctypes.c_void_p]
_cg.CGImageGetBitmapInfo.restype = ctypes.c_uint32
_cg.CGImageGetBitmapInfo.argtypes = [ctypes.c_void_p]
_cg.CGImageGetAlphaInfo.restype = ctypes.c_uint32
_cg.CGImageGetAlphaInfo.argtypes = [ctypes.c_void_p]

if _HAS_IMAGEIO:
    _imageio.CGImageDestinationCreateWithData.restype = ctypes.c_void_p
    _imageio.CGImageDestinationCreateWithData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
    ]
    _imageio.CGImageDestinationAddImage.restype = None
    _imageio.CGImageDestinationAddImage.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    _imageio.CGImageDestinationFinalize.restype = ctypes.c_bool
    _imageio.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]

# CGDisplayCopyDisplayMode for scale factor
try:
    _cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
    _cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
    _cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_ulong
    _cg.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
    _cg.CGDisplayModeGetWidth.restype = ctypes.c_ulong
    _cg.CGDisplayModeGetWidth.argtypes = [ctypes.c_void_p]
    _cg.CGDisplayModeRelease.restype = None
    _cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
    _HAS_DISPLAY_MODE = True
except AttributeError:
    _HAS_DISPLAY_MODE = False

# ---------------------------------------------------------------------------
# PNG <-> ndarray codec bindings — first-party replacement for OpenCV's
# imdecode/imencode. Used by matcher.py (template matching). All CoreGraphics /
# CoreFoundation / ImageIO; no third-party dependency, no numpy-2.x pin.
# ---------------------------------------------------------------------------

# CFDataCreate copies the given bytes into an immutable CFData (used to wrap PNG
# bytes for the image source). Pass binary via a ctypes buffer, never c_char_p
# (which would truncate at the first null byte in the PNG stream).
_cf.CFDataCreate.restype = ctypes.c_void_p
_cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]

# Read back a bitmap context's auto-allocated buffer (decode path).
_cg.CGBitmapContextGetData.restype = ctypes.c_void_p
_cg.CGBitmapContextGetData.argtypes = [ctypes.c_void_p]
_cg.CGBitmapContextGetBytesPerRow.restype = ctypes.c_ulong
_cg.CGBitmapContextGetBytesPerRow.argtypes = [ctypes.c_void_p]

# Build a CGImage from a raw pixel buffer (encode path). CGDataProviderCreateWithData
# does NOT copy — the backing buffer must outlive the provider.
_cg.CGDataProviderCreateWithData.restype = ctypes.c_void_p
_cg.CGDataProviderCreateWithData.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
]
_cg.CGImageCreate.restype = ctypes.c_void_p
_cg.CGImageCreate.argtypes = [
    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_bool, ctypes.c_uint32,
]

if _HAS_IMAGEIO:
    _imageio.CGImageSourceCreateWithData.restype = ctypes.c_void_p
    _imageio.CGImageSourceCreateWithData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _imageio.CGImageSourceCreateImageAtIndex.restype = ctypes.c_void_p
    _imageio.CGImageSourceCreateImageAtIndex.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListOptionIncludingWindow = 1 << 3
kCGWindowImageDefault = 0
kCGWindowImageBoundsIgnoreFraming = 1 << 0
kCGNullWindowID = 0
kCFNumberSInt32Type = 3
kCFNumberDoubleType = 13
kCFStringEncodingUTF8 = 0x08000100

# Bitmap context: 32-bit, alpha as MSB component, host byte order.
# All modern Apple Macs (Intel and Apple Silicon) are little-endian, so "Host"
# must resolve to kCGBitmapByteOrder32Little (0x2000) — NOT Big (0x4000), which
# was the original value and silently corrupted every byte-level read of the
# rendered buffer (memory ended up ARGB instead of BGRA, so get_pixel and
# get_pixels returned channels permuted while the screenshot path stayed correct
# because the PNG encoder reads the bitmap flags and converts accordingly).
_kCGImageAlphaNoneSkipFirst = 6
_kCGBitmapByteOrder32Little = 0x2000
_CG_BITMAP_INFO = _kCGImageAlphaNoneSkipFirst | _kCGBitmapByteOrder32Little  # 8198

# ---------------------------------------------------------------------------
# CoreGraphics CFString constants
# ---------------------------------------------------------------------------

def _load_cg_constant(name: str) -> int:
    try:
        return ctypes.c_void_p.in_dll(_cg, name).value or 0
    except (OSError, AttributeError):
        raise RuntimeError(f"Could not load CoreGraphics constant: {name}")


_kCGWindowOwnerName = _load_cg_constant("kCGWindowOwnerName")
_kCGWindowOwnerPID  = _load_cg_constant("kCGWindowOwnerPID")
_kCGWindowNumber    = _load_cg_constant("kCGWindowNumber")
_kCGWindowBounds    = _load_cg_constant("kCGWindowBounds")
_kCGWindowLayer     = _load_cg_constant("kCGWindowLayer")


# ---------------------------------------------------------------------------
# CoreFoundation helpers
# ---------------------------------------------------------------------------

def _cf_num_to_int(ref: int) -> int:
    if not ref:
        return 0
    val = ctypes.c_int32(0)
    _cf.CFNumberGetValue(ctypes.c_void_p(ref), kCFNumberSInt32Type, ctypes.byref(val))
    return val.value


def _cf_num_to_double(ref: int) -> float:
    if not ref:
        return 0.0
    val = ctypes.c_double(0.0)
    _cf.CFNumberGetValue(ctypes.c_void_p(ref), kCFNumberDoubleType, ctypes.byref(val))
    return val.value


def _cf_str_to_py(ref: int) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(512)
    _cf.CFStringGetCString(ctypes.c_void_p(ref), buf, 512, kCFStringEncodingUTF8)
    return buf.value.decode("utf-8", errors="replace")


def _make_cf_str(s: str) -> int:
    return _cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)


def _get_bounds_dict(bounds_ref: int) -> dict:
    result = {"X": 0.0, "Y": 0.0, "Width": 0.0, "Height": 0.0}
    if not bounds_ref:
        return result
    for key in result:
        cf_key = _make_cf_str(key)
        try:
            val_ref = _cf.CFDictionaryGetValue(ctypes.c_void_p(bounds_ref), ctypes.c_void_p(cf_key))
            result[key] = _cf_num_to_double(val_ref)
        finally:
            if cf_key:
                _cf.CFRelease(ctypes.c_void_p(cf_key))
    return result


# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------

def _list_all_windows() -> list[dict]:
    win_list = _cg.CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    if not win_list:
        return []
    windows = []
    try:
        count = _cf.CFArrayGetCount(win_list)
        for i in range(count):
            win_dict = _cf.CFArrayGetValueAtIndex(win_list, i)
            if not win_dict:
                continue

            layer_ref = _cf.CFDictionaryGetValue(win_dict, ctypes.c_void_p(_kCGWindowLayer))
            if _cf_num_to_int(layer_ref) != 0:
                continue

            wid_ref = _cf.CFDictionaryGetValue(win_dict, ctypes.c_void_p(_kCGWindowNumber))
            pid_ref = _cf.CFDictionaryGetValue(win_dict, ctypes.c_void_p(_kCGWindowOwnerPID))
            name_ref = _cf.CFDictionaryGetValue(win_dict, ctypes.c_void_p(_kCGWindowOwnerName))
            bounds_ref = _cf.CFDictionaryGetValue(win_dict, ctypes.c_void_p(_kCGWindowBounds))

            bounds = _get_bounds_dict(bounds_ref)
            windows.append({
                "window_id": _cf_num_to_int(wid_ref),
                "pid": _cf_num_to_int(pid_ref),
                "owner_name": _cf_str_to_py(name_ref),
                "bounds": bounds,
            })
    finally:
        _cf.CFRelease(ctypes.c_void_p(win_list))
    return windows


def get_window_for_pid(pid: int) -> dict | None:
    """Return the largest on-screen window belonging to pid."""
    windows = _list_all_windows()
    candidates = [w for w in windows if w["pid"] == pid]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w["bounds"]["Width"] * w["bounds"]["Height"])


def frontmost_pid() -> int | None:
    """
    PID owning the topmost user-level (layer 0) on-screen window — the
    WindowServer's view of the active app. Reads CGWindowList live each
    call. NSWorkspace.frontmostApplication caches via distributed
    notifications that never reach a process without a pumped main run
    loop, so this is the only reliable check inside a long-running asyncio
    MCP server. Returns None only when no normal-level windows are on
    screen (rare; login window or all apps minimized).
    """
    win_list = _cg.CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
    )
    if not win_list:
        return None
    try:
        count = _cf.CFArrayGetCount(win_list)
        for i in range(count):
            win_dict = _cf.CFArrayGetValueAtIndex(win_list, i)
            if not win_dict:
                continue
            layer_ref = _cf.CFDictionaryGetValue(
                win_dict, ctypes.c_void_p(_kCGWindowLayer),
            )
            if _cf_num_to_int(layer_ref) != 0:
                continue
            pid_ref = _cf.CFDictionaryGetValue(
                win_dict, ctypes.c_void_p(_kCGWindowOwnerPID),
            )
            return _cf_num_to_int(pid_ref)
        return None
    finally:
        _cf.CFRelease(ctypes.c_void_p(win_list))


def get_window_for_name(name_fragment: str) -> dict | None:
    """Fuzzy match on owner name (case-insensitive)."""
    fragment = name_fragment.lower()
    windows = _list_all_windows()
    candidates = [w for w in windows if fragment in w["owner_name"].lower()]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w["bounds"]["Width"] * w["bounds"]["Height"])


def wait_for_window(pid: int, timeout: float = 15.0) -> dict | None:
    """Poll until a window for pid appears or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = get_window_for_pid(pid)
        if win and win["bounds"]["Width"] > 10:
            return win
        time.sleep(0.5)
    return None


def list_windows_for_pid(pid: int, min_size: int = 50) -> list[dict]:
    """
    Return all on-screen windows belonging to pid, ordered front-to-back
    (closest to the top of the window list = frontmost first).
    Filters out tiny utility windows (< min_size in either dimension) to
    keep the list focused on real document/content windows. Each entry:
    {window_id, pid, owner_name, x, y, width, height}.
    """
    result = []
    for w in _list_all_windows():
        if w["pid"] != pid:
            continue
        b = w["bounds"]
        if b["Width"] < min_size or b["Height"] < min_size:
            continue
        result.append({
            "window_id": w["window_id"],
            "pid": w["pid"],
            "owner_name": w["owner_name"],
            "x": int(b["X"]),
            "y": int(b["Y"]),
            "width": int(b["Width"]),
            "height": int(b["Height"]),
        })
    return result


def window_occluders(window_id: int, pid: int) -> list[dict]:
    """
    Return on-screen windows from OTHER processes that sit ABOVE `window_id`
    (more frontmost) and overlap its rect — i.e. windows whose pixels bleed into
    a composited-region screenshot of the target window. Empty when the target
    is unobstructed (or frontmost). Used to attach an `overlap_warning` so the
    agent knows a screenshot image may include another app's pixels.

    `_list_all_windows()` returns layer-0 windows front-to-back, so any window at
    a lower index than the target is drawn on top of it.
    """
    wins = _list_all_windows()
    ti = next((i for i, w in enumerate(wins) if w["window_id"] == window_id), -1)
    if ti < 0:
        return []
    tb = wins[ti]["bounds"]
    tx0, ty0 = tb["X"], tb["Y"]
    tx1, ty1 = tx0 + tb["Width"], ty0 + tb["Height"]
    out: list[dict] = []
    seen: set[str] = set()
    for i in range(ti):  # only windows above the target
        w = wins[i]
        if w["pid"] == pid:
            continue  # same app — not a cross-app bleed
        b = w["bounds"]
        if b["Width"] <= 0 or b["Height"] <= 0:
            continue
        bx0, by0 = b["X"], b["Y"]
        bx1, by1 = bx0 + b["Width"], by0 + b["Height"]
        if bx0 < tx1 and bx1 > tx0 and by0 < ty1 and by1 > ty0:
            name = w["owner_name"] or "another app"
            if name not in seen:
                seen.add(name)
                out.append({"owner_name": name, "pid": w["pid"]})
    return out


def get_window_by_id(window_id: int) -> dict | None:
    """Find a specific window by its CG window ID. Returns None if not found / not on screen."""
    for w in _list_all_windows():
        if w["window_id"] == window_id:
            b = w["bounds"]
            return {
                "window_id": w["window_id"],
                "pid": w["pid"],
                "owner_name": w["owner_name"],
                "x": int(b["X"]),
                "y": int(b["Y"]),
                "width": int(b["Width"]),
                "height": int(b["Height"]),
            }
    return None


def screen_info() -> dict:
    """
    Main display + all active displays in screen-space coordinates.
    Returns:
      {
        main: {width, height, x, y, display_id},
        displays: [{index, display_id, x, y, width, height, is_main}, ...],
        scale: <main display backing scale>,
      }
    The `index` is a stable 0-based ordinal — agents pass it back as
    `display=N` on screenshot to capture an entire display.
    All coords are in logical points (matches CGEvent coordinate space).
    """
    main_id = _cg.CGMainDisplayID()
    main_rect = _cg.CGDisplayBounds(main_id)
    main_entry = {
        "display_id": int(main_id),
        "x": int(main_rect.x),
        "y": int(main_rect.y),
        "width": int(main_rect.width),
        "height": int(main_rect.height),
    }

    displays: list[dict] = []
    # First call: get count
    count = ctypes.c_uint32(0)
    err = _cg.CGGetActiveDisplayList(0, None, ctypes.byref(count))
    if err == 0 and count.value > 0:
        ids = (ctypes.c_uint32 * count.value)()
        err = _cg.CGGetActiveDisplayList(count.value, ids, ctypes.byref(count))
        if err == 0:
            for i in range(count.value):
                did = int(ids[i])
                rect = _cg.CGDisplayBounds(did)
                displays.append({
                    "index": i,
                    "display_id": did,
                    "x": int(rect.x),
                    "y": int(rect.y),
                    "width": int(rect.width),
                    "height": int(rect.height),
                    "is_main": did == int(main_id),
                })

    if not displays:
        # Fallback: just main
        displays = [{"index": 0, **main_entry, "is_main": True}]

    return {
        "main": main_entry,
        "displays": displays,
        "scale": get_scale_factor(),
    }


def resolve_display(spec) -> dict | None:
    """
    Map a `display` argument (0-based index int, or 'main') to a display
    entry from screen_info(). Returns None if the spec doesn't match any
    attached display. Used by take_display_screenshot.
    """
    info = screen_info()
    displays = info["displays"]
    if spec == "main" or (isinstance(spec, str) and spec.lower() == "main"):
        for d in displays:
            if d.get("is_main"):
                return d
        return displays[0] if displays else None
    if isinstance(spec, int) and 0 <= spec < len(displays):
        return displays[spec]
    return None


def take_display_screenshot(display_entry: dict) -> tuple[str, int, int]:
    """
    Capture an entire display by index/main spec. Returns (base64_png,
    logical_width, logical_height) matching the take_screenshot contract.
    Uses CGWindowListCreateImage with the display's global bounds — works
    for any attached monitor without subprocess.
    """
    win_x = int(display_entry["x"])
    win_y = int(display_entry["y"])
    width = int(display_entry["width"])
    height = int(display_entry["height"])
    # Reuse the existing window-region capture path. CGWindowListCreateImage
    # with kCGNullWindowID + screen-space rect captures every visible window
    # in that rect → effectively a full-display screenshot.
    return _take_screenshot_cg(win_x, win_y, width, height)


# ---------------------------------------------------------------------------
# Scale factor
# ---------------------------------------------------------------------------

def get_scale_factor() -> float:
    """Return the main display backing scale factor (1.0 or 2.0)."""
    if _HAS_DISPLAY_MODE:
        try:
            display = _cg.CGMainDisplayID()
            mode = _cg.CGDisplayCopyDisplayMode(display)
            if mode:
                pixel_w = _cg.CGDisplayModeGetPixelWidth(mode)
                logical_w = _cg.CGDisplayModeGetWidth(mode)
                _cg.CGDisplayModeRelease(mode)
                if logical_w > 0:
                    return float(pixel_w) / float(logical_w)
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.lower()
        if "retina" in output or "hidpi" in output:
            return 2.0
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# CoreGraphics in-memory screenshot helpers
# ---------------------------------------------------------------------------

def _cgimage_to_png_bytes(image: int) -> bytes:
    """Encode a CGImageRef to PNG bytes entirely in memory via ImageIO."""
    data = _cf.CFDataCreateMutable(None, 0)
    if not data:
        raise RuntimeError("CFDataCreateMutable failed")

    kUTTypePNG = _cf.CFStringCreateWithCString(None, b"public.png", kCFStringEncodingUTF8)
    dest = _imageio.CGImageDestinationCreateWithData(
        ctypes.c_void_p(data), ctypes.c_void_p(kUTTypePNG), 1, None
    )
    _cf.CFRelease(ctypes.c_void_p(kUTTypePNG))

    if not dest:
        _cf.CFRelease(ctypes.c_void_p(data))
        raise RuntimeError("CGImageDestinationCreateWithData failed")

    _imageio.CGImageDestinationAddImage(
        ctypes.c_void_p(dest), ctypes.c_void_p(image), None
    )
    ok = _imageio.CGImageDestinationFinalize(ctypes.c_void_p(dest))
    _cf.CFRelease(ctypes.c_void_p(dest))

    if not ok:
        _cf.CFRelease(ctypes.c_void_p(data))
        raise RuntimeError("CGImageDestinationFinalize failed")

    length   = _cf.CFDataGetLength(ctypes.c_void_p(data))
    byte_ptr = _cf.CFDataGetBytePtr(ctypes.c_void_p(data))
    result   = bytes(ctypes.string_at(byte_ptr, length))
    _cf.CFRelease(ctypes.c_void_p(data))
    return result


def decode_png_to_rgb_array(b64_png: str):
    """
    Decode a base64 PNG into an (H, W, 3) float64 RGB ndarray.

    First-party replacement for cv2.imdecode. The decoded image is drawn into
    a canonical 32-bit context (_CG_BITMAP_INFO = NoneSkipFirst | 32Little →
    memory layout B,G,R,X) so that ANY input PNG format — 24-bit RGB, 32-bit
    RGBA, palette, grayscale — normalizes to a single known byte order we can
    slice deterministically. Channel permutation and the host-endianness traps
    documented in _read_pixels_from_cgimage are sidestepped because CG performs
    the format conversion during the draw.
    """
    import numpy as np
    if not _HAS_IMAGEIO:
        raise RuntimeError("ImageIO not available — cannot decode template image")
    raw = base64.b64decode(b64_png)
    if not raw:
        raise ValueError("empty image data")
    src_buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    cfdata = _cf.CFDataCreate(None, src_buf, len(raw))
    if not cfdata:
        raise ValueError("CFDataCreate failed wrapping PNG bytes")
    src = img = ctx = cs = 0
    try:
        src = _imageio.CGImageSourceCreateWithData(ctypes.c_void_p(cfdata), None)
        if not src:
            raise ValueError("not a valid image (CGImageSourceCreateWithData)")
        img = _imageio.CGImageSourceCreateImageAtIndex(ctypes.c_void_p(src), 0, None)
        if not img:
            raise ValueError("image has no decodable frame at index 0")
        w = int(_cg.CGImageGetWidth(ctypes.c_void_p(img)))
        h = int(_cg.CGImageGetHeight(ctypes.c_void_p(img)))
        if w <= 0 or h <= 0:
            raise ValueError(f"decoded image has invalid size {w}x{h}")
        cs = _cg.CGColorSpaceCreateDeviceRGB()
        # bytesPerRow=0 → let CG choose (it may pad the stride for alignment);
        # we read the real stride back via CGBitmapContextGetBytesPerRow.
        ctx = _cg.CGBitmapContextCreate(None, w, h, 8, 0, ctypes.c_void_p(cs), _CG_BITMAP_INFO)
        if not ctx:
            raise RuntimeError("CGBitmapContextCreate failed during decode")
        _cg.CGContextDrawImage(
            ctypes.c_void_p(ctx), CGRect(0.0, 0.0, float(w), float(h)), ctypes.c_void_p(img)
        )
        bpr = int(_cg.CGBitmapContextGetBytesPerRow(ctypes.c_void_p(ctx)))
        ptr = _cg.CGBitmapContextGetData(ctypes.c_void_p(ctx))
        if not ptr:
            raise RuntimeError("CGBitmapContextGetData returned NULL")
        buf = (ctypes.c_uint8 * (bpr * h)).from_address(int(ptr))
        rows = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr)
        pix = rows[:, : w * 4].reshape(h, w, 4)  # memory bytes [B, G, R, X]
        # Copy out into an owned RGB array BEFORE the context (and its buffer)
        # are released in the finally below.
        rgb = np.empty((h, w, 3), dtype=np.float64)
        rgb[:, :, 0] = pix[:, :, 2]  # R
        rgb[:, :, 1] = pix[:, :, 1]  # G
        rgb[:, :, 2] = pix[:, :, 0]  # B
        return rgb
    finally:
        if ctx:
            _cf.CFRelease(ctypes.c_void_p(ctx))
        if cs:
            _cf.CFRelease(ctypes.c_void_p(cs))
        if img:
            _cf.CFRelease(ctypes.c_void_p(img))
        if src:
            _cf.CFRelease(ctypes.c_void_p(src))
        _cf.CFRelease(ctypes.c_void_p(cfdata))


def encode_rgb_array_to_png_b64(rgb_uint8) -> str:
    """
    Encode an (H, W, 3) uint8 RGB ndarray to a base64 PNG string.

    First-party replacement for cv2.imencode. Builds a canonical BGRX buffer
    matching _CG_BITMAP_INFO (so encode and decode share one byte-order
    convention), wraps it in a CGImage, and serializes via the existing
    ImageIO PNG path. The backing buffer is held alive until the encode
    completes (CGDataProviderCreateWithData does not copy).
    """
    import numpy as np
    if not _HAS_IMAGEIO:
        raise RuntimeError("ImageIO not available — cannot encode template image")
    arr = np.ascontiguousarray(rgb_uint8, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected an (H, W, 3) RGB array, got shape {arr.shape}")
    h, w = arr.shape[:2]
    if w <= 0 or h <= 0:
        raise ValueError(f"cannot encode empty image {w}x{h}")
    bgrx = np.empty((h, w, 4), dtype=np.uint8)
    bgrx[:, :, 0] = arr[:, :, 2]  # B
    bgrx[:, :, 1] = arr[:, :, 1]  # G
    bgrx[:, :, 2] = arr[:, :, 0]  # R
    bgrx[:, :, 3] = 255           # X (alpha is skipped by _CG_BITMAP_INFO)
    data = bgrx.tobytes()
    data_buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    provider = img = cs = 0
    try:
        provider = _cg.CGDataProviderCreateWithData(None, data_buf, len(data), None)
        if not provider:
            raise RuntimeError("CGDataProviderCreateWithData failed")
        cs = _cg.CGColorSpaceCreateDeviceRGB()
        img = _cg.CGImageCreate(
            w, h, 8, 32, w * 4, ctypes.c_void_p(cs), _CG_BITMAP_INFO,
            ctypes.c_void_p(provider), None, False, 0,
        )
        if not img:
            raise RuntimeError("CGImageCreate failed")
        png_bytes = _cgimage_to_png_bytes(img)
    finally:
        if img:
            _cf.CFRelease(ctypes.c_void_p(img))
        if cs:
            _cf.CFRelease(ctypes.c_void_p(cs))
        if provider:
            _cf.CFRelease(ctypes.c_void_p(provider))
    return base64.b64encode(png_bytes).decode("utf-8")


def _resize_cgimage(image: int, target_w: int, target_h: int) -> int:
    """
    Resize a CGImageRef to (target_w, target_h) using a bitmap context.

    No CTM flip. Earlier versions applied a TranslateCTM(0, h) +
    ScaleCTM(1, -1) on the theory that CGBitmapContext is bottom-left and
    the source CGImage is top-left, so the destination needed flipping
    to land the image upright. Empirically — verified against both
    AppKit (Finder) and SwiftUI (System Settings) windows on macOS 15 —
    the flip was the bug, not the fix: `CGContextDrawImage` already
    places the source image in the destination's coordinate space
    correctly, and the extra flip silently mirrored every Retina-
    downsampled capture along the Y axis. The bug went unnoticed in
    most tooling because AX-based interactions use canonical
    coordinates and never read the captured pixels back; visual
    inspection of the inspect/screenshot image was the only surface
    that exposed the flip, and only on layouts where the flip was
    obvious (e.g. System Settings sidebar with familiar item order).

    Returns the resized CGImageRef (caller must CFRelease), or the
    original on failure.
    """
    cs = _cg.CGColorSpaceCreateDeviceRGB()
    if not cs:
        return image

    ctx = _cg.CGBitmapContextCreate(
        None, target_w, target_h, 8, target_w * 4,
        ctypes.c_void_p(cs), _CG_BITMAP_INFO
    )
    _cf.CFRelease(ctypes.c_void_p(cs))

    if not ctx:
        return image

    draw_rect = CGRect(x=0.0, y=0.0, width=float(target_w), height=float(target_h))
    _cg.CGContextDrawImage(ctypes.c_void_p(ctx), draw_rect, ctypes.c_void_p(image))

    resized = _cg.CGBitmapContextCreateImage(ctypes.c_void_p(ctx))
    _cf.CFRelease(ctypes.c_void_p(ctx))
    return resized if resized else image


def _take_screenshot_cg(
    win_x: int, win_y: int,
    logical_width: int, logical_height: int,
) -> tuple[str, int, int]:
    """
    Capture a screen region directly via CoreGraphics — no subprocess, no temp files.
    Uses CGWindowListCreateImage with global coordinates so the window can be on any
    monitor. Falls back to CGDisplayCreateImageForRect on the main display if needed.
    Returns (base64_png, logical_width, logical_height).
    """
    rect = CGRect(
        x=float(win_x), y=float(win_y),
        width=float(logical_width), height=float(logical_height),
    )
    raw = _cg.CGWindowListCreateImage(
        rect,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
        kCGWindowImageDefault,
    )
    if not raw:
        # Fallback: main display capture
        display = _cg.CGMainDisplayID()
        raw = _cg.CGDisplayCreateImageForRect(display, rect)
    if not raw:
        raise RuntimeError("CGWindowListCreateImage and CGDisplayCreateImageForRect both returned NULL")

    try:
        px_w = _cg.CGImageGetWidth(ctypes.c_void_p(raw))
        px_h = _cg.CGImageGetHeight(ctypes.c_void_p(raw))

        # Resize to logical dimensions if on Retina (2x pixels per point)
        if px_w != logical_width or px_h != logical_height:
            resized = _resize_cgimage(raw, logical_width, logical_height)
        else:
            resized = raw

        try:
            png_bytes = _cgimage_to_png_bytes(resized)
        finally:
            if resized != raw:
                _cf.CFRelease(ctypes.c_void_p(resized))
    finally:
        _cf.CFRelease(ctypes.c_void_p(raw))

    return base64.b64encode(png_bytes).decode("utf-8"), logical_width, logical_height


# ---------------------------------------------------------------------------
# Pixel sampling
# ---------------------------------------------------------------------------

def _capture_window_image(window_id: int, win_x: int, win_y: int, win_w: int, win_h: int):
    """
    Capture a specific window's content as a CGImage — bypasses z-order, so
    overlapping windows do not affect the result. The explicit rect (window's
    own screen bounds) constrains the image to that window's region; combined
    with kCGWindowListOptionIncludingWindow + the window's CG id, only that
    window's content is captured. Returns a CGImage ref (caller CFReleases)
    or 0 on failure.
    """
    rect = CGRect(
        x=float(win_x), y=float(win_y),
        width=float(win_w), height=float(win_h),
    )
    return _cg.CGWindowListCreateImage(
        rect,
        kCGWindowListOptionIncludingWindow,
        int(window_id),
        kCGWindowImageBoundsIgnoreFraming,
    )


def _read_pixels_from_cgimage(raw_image: int, points_xy: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """
    Read pixel RGB values directly from a CGImage's underlying data provider.
    This bypasses CGBitmapContextCreate + CGContextDrawImage entirely — those
    are buggy to use for raw byte reads because the rendered buffer's byte
    order, alpha-channel position, and y-orientation all silently change with
    the bitmap-info flags and host endianness, and historically permuted
    channels (BGRA vs ARGB) and mirrored Y on this codebase.

    The CGImage's data provider returns the bytes EXACTLY as the source put
    them, with the layout described by CGImageGetBitmapInfo + CGImageGetAlphaInfo.
    We honor that layout when extracting channels.

    points_xy: list of (x, y) in PIXEL coordinates of the CGImage. Caller is
               responsible for any logical→pixel scaling on Retina.
    Returns: list of (r, g, b) tuples (0-255 each), one per input point, in
             the same order. Off-image points raise RuntimeError up-front.
    """
    width  = int(_cg.CGImageGetWidth(ctypes.c_void_p(raw_image)))
    height = int(_cg.CGImageGetHeight(ctypes.c_void_p(raw_image)))
    bpr    = int(_cg.CGImageGetBytesPerRow(ctypes.c_void_p(raw_image)))
    info   = int(_cg.CGImageGetBitmapInfo(ctypes.c_void_p(raw_image)))
    alpha  = info & 0x1f  # kCGImageAlphaInfoMask = 0x1f

    # Validate all coords up-front so partial work isn't done on a bad request.
    for (x, y) in points_xy:
        if x < 0 or y < 0 or x >= width or y >= height:
            raise RuntimeError(
                f"_read_pixels_from_cgimage: point ({x}, {y}) is outside image "
                f"bounds ({width}×{height})."
            )

    # Decide where R, G, B live in each 4-byte pixel. We support the two
    # formats the macOS window-content path actually produces:
    # (a) Premultiplied-First or NoneSkip-First + 32Little → memory order BGRA
    # (b) Premultiplied-First or NoneSkip-First + 32Big    → memory order ARGB
    # Anything else is rare for window captures; we still pick the best guess
    # and label the path so a future bug is easier to spot.
    byte_order = info & 0x7000  # kCGBitmapByteOrderMask = 0x7000
    # 0x2000 = 32Little, 0x4000 = 32Big
    little_endian = (byte_order == 0x2000)
    # alpha "first" in pixel-value order (PremultipliedFirst=2, AlphaFirst=4, NoneSkipFirst=6)
    alpha_first = alpha in (2, 4, 6)

    if alpha_first and little_endian:
        # Pixel value 0xAARRGGBB stored little-endian → memory bytes [BB, GG, RR, AA]
        r_off, g_off, b_off = 2, 1, 0
    elif alpha_first and not little_endian:
        # Big-endian ARGB → memory bytes [AA, RR, GG, BB]
        r_off, g_off, b_off = 1, 2, 3
    elif not alpha_first and little_endian:
        # Pixel value 0xBBGGRRAA stored little-endian → [AA, RR, GG, BB]
        # (PremultipliedLast/AlphaLast/NoneSkipLast = RGBA in pixel-component order)
        r_off, g_off, b_off = 1, 2, 3
    else:
        # Big-endian RGBA → memory bytes [RR, GG, BB, AA]
        r_off, g_off, b_off = 0, 1, 2

    prov = _cg.CGImageGetDataProvider(ctypes.c_void_p(raw_image))
    if not prov:
        raise RuntimeError("CGImageGetDataProvider returned NULL")
    data = _cg.CGDataProviderCopyData(ctypes.c_void_p(prov))
    if not data:
        raise RuntimeError("CGDataProviderCopyData returned NULL")
    try:
        length = int(_cf.CFDataGetLength(ctypes.c_void_p(data)))
        ptr    = _cf.CFDataGetBytePtr(ctypes.c_void_p(data))
        buf    = (ctypes.c_uint8 * length).from_address(int(ptr))
        out: list[tuple[int, int, int]] = []
        for (x, y) in points_xy:
            off = y * bpr + x * 4
            out.append((int(buf[off + r_off]), int(buf[off + g_off]), int(buf[off + b_off])))
        return out
    finally:
        _cf.CFRelease(ctypes.c_void_p(data))


def _image_pixel_scale(raw_image: int, win_w: int, win_h: int) -> float:
    """
    Detect Retina factor of the captured image: CG may return the buffer at
    physical pixels (e.g. 1920×1050) for a 960×525 logical window. Returns the
    multiplier from logical→pixel coords. Always 1.0 or 2.0 on current Macs.
    """
    iw = int(_cg.CGImageGetWidth(ctypes.c_void_p(raw_image)))
    if iw == win_w * 2:
        return 2.0
    return 1.0


def get_pixel(
    screen_x: float, screen_y: float,
    window_id: int | None = None,
    window_bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int]:
    """
    Read the RGB color at a single point and return (r, g, b), each 0-255.

    When window_id and window_bounds=(win_x, win_y, win_w, win_h) are supplied,
    capture is sourced from THAT window's content only — independent of z-order,
    so a window covered by others still reports the correct pixel. Without them,
    falls back to composited desktop capture (legacy behavior).

    Reads bytes directly from the CGImage's data provider (no intermediate
    bitmap context), honoring the image's actual bitmap-info / alpha layout.
    Handles Retina (image returned at 2× physical pixels for a 1× logical
    window) by scaling the read coordinate. ~5 ms.
    """
    if window_id is not None and window_bounds is not None:
        win_x, win_y, win_w, win_h = window_bounds
        rx = int(screen_x) - int(win_x)
        ry = int(screen_y) - int(win_y)
        if rx < 0 or ry < 0 or rx >= win_w or ry >= win_h:
            raise RuntimeError(
                f"get_pixel: point ({screen_x}, {screen_y}) is outside window "
                f"{window_id} bounds ({win_x}, {win_y}, {win_w}×{win_h}). "
                "Window coordinates must fall inside the window."
            )
        raw = _capture_window_image(int(window_id), win_x, win_y, win_w, win_h)
        if not raw:
            raise RuntimeError(
                f"get_pixel: window {window_id} capture returned NULL. "
                "Window may have closed, been minimized, or moved to another Space. "
                "Call list_windows to refresh."
            )
        try:
            scale = _image_pixel_scale(raw, int(win_w), int(win_h))
            px = int(rx * scale)
            py = int(ry * scale)
            (r, g, b), = _read_pixels_from_cgimage(raw, [(px, py)])
        finally:
            _cf.CFRelease(ctypes.c_void_p(raw))
        return r, g, b

    # Legacy 1×1 path: composited desktop capture for a single screen-space point.
    rect = CGRect(x=float(screen_x), y=float(screen_y), width=1.0, height=1.0)
    raw = _cg.CGWindowListCreateImage(
        rect,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
        kCGWindowImageDefault,
    )
    if not raw:
        display = _cg.CGMainDisplayID()
        raw = _cg.CGDisplayCreateImageForRect(display, rect)
    if not raw:
        raise RuntimeError(
            f"get_pixel: CG capture returned NULL at ({screen_x}, {screen_y}). "
            "Point may be off-screen, or Screen Recording permission is missing."
        )
    try:
        # The 1×1 capture may come back at 2×2 on Retina — read whichever pixel exists.
        iw = int(_cg.CGImageGetWidth(ctypes.c_void_p(raw)))
        ih = int(_cg.CGImageGetHeight(ctypes.c_void_p(raw)))
        (r, g, b), = _read_pixels_from_cgimage(raw, [(iw // 2, ih // 2) if iw > 1 or ih > 1 else (0, 0)])
    finally:
        _cf.CFRelease(ctypes.c_void_p(raw))
    return r, g, b


def get_pixels(
    points: list[tuple[int, int]],
    window_id: int,
    window_bounds: tuple[int, int, int, int],
) -> list[tuple[int, int, int]]:
    """
    Batch pixel read: capture the target window ONCE, then sample N points
    directly from the CGImage's data provider. Independent of z-order.
    ~40 ms for the capture, then ~µs per point — pays off for any sequence
    of 3+ samples.

    points are SCREEN-space coordinates (caller has already converted from
    window-relative). window_bounds = (win_x, win_y, win_w, win_h).
    Returns a list of (r, g, b) tuples in the same order as `points`.
    Raises RuntimeError if any point falls outside the window.
    """
    win_x, win_y, win_w, win_h = window_bounds
    rel: list[tuple[int, int]] = []
    for px, py in points:
        rx = int(px) - int(win_x)
        ry = int(py) - int(win_y)
        if rx < 0 or ry < 0 or rx >= win_w or ry >= win_h:
            raise RuntimeError(
                f"get_pixels: point ({px}, {py}) is outside window {window_id} "
                f"bounds ({win_x}, {win_y}, {win_w}×{win_h})."
            )
        rel.append((rx, ry))

    raw = _capture_window_image(int(window_id), win_x, win_y, win_w, win_h)
    if not raw:
        raise RuntimeError(
            f"get_pixels: window {window_id} capture returned NULL. "
            "Window may have closed, been minimized, or moved to another Space. "
            "Call list_windows to refresh."
        )
    try:
        scale = _image_pixel_scale(raw, int(win_w), int(win_h))
        scaled = [(int(rx * scale), int(ry * scale)) for rx, ry in rel]
        return _read_pixels_from_cgimage(raw, scaled)
    finally:
        _cf.CFRelease(ctypes.c_void_p(raw))


# Per-channel median over a strided sample of pixels inside each rect.
# Robust against a minority of "outlier" pixels (e.g. a white letter glyph
# centered inside a colored Wordle tile) — sorting puts the glyph pixels at
# the extremes and the median lands squarely in the background fill.
#
# Why stride-sampled rather than full-rect: a 50×50 logical rect on Retina is
# 100×100 = 10k pixels per channel. Sorting that takes ~milliseconds, but the
# median is just as accurate from ~256 samples. We target ~16 columns and
# ~16 rows per rect (max), which is plenty robust and stays sub-millisecond
# per rect regardless of rect size.
def _read_rect_medians_from_cgimage(
    raw_image: int,
    rects_xywh: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int]]:
    width  = int(_cg.CGImageGetWidth(ctypes.c_void_p(raw_image)))
    height = int(_cg.CGImageGetHeight(ctypes.c_void_p(raw_image)))
    bpr    = int(_cg.CGImageGetBytesPerRow(ctypes.c_void_p(raw_image)))
    info   = int(_cg.CGImageGetBitmapInfo(ctypes.c_void_p(raw_image)))
    alpha  = info & 0x1f

    # Validate every rect first so partial work isn't done on a bad request.
    for (x, y, w, h) in rects_xywh:
        if w <= 0 or h <= 0:
            raise RuntimeError(
                f"_read_rect_medians: rect ({x},{y},{w}×{h}) has non-positive size."
            )
        if x < 0 or y < 0 or x + w > width or y + h > height:
            raise RuntimeError(
                f"_read_rect_medians: rect ({x},{y},{w}×{h}) extends outside image "
                f"bounds ({width}×{height})."
            )

    # Channel offsets — same byte-order resolution as _read_pixels_from_cgimage.
    byte_order = info & 0x7000
    little_endian = (byte_order == 0x2000)
    alpha_first = alpha in (2, 4, 6)
    if alpha_first and little_endian:
        r_off, g_off, b_off = 2, 1, 0
    elif alpha_first and not little_endian:
        r_off, g_off, b_off = 1, 2, 3
    elif not alpha_first and little_endian:
        r_off, g_off, b_off = 1, 2, 3
    else:
        r_off, g_off, b_off = 0, 1, 2

    prov = _cg.CGImageGetDataProvider(ctypes.c_void_p(raw_image))
    if not prov:
        raise RuntimeError("CGImageGetDataProvider returned NULL")
    data = _cg.CGDataProviderCopyData(ctypes.c_void_p(prov))
    if not data:
        raise RuntimeError("CGDataProviderCopyData returned NULL")
    try:
        length = int(_cf.CFDataGetLength(ctypes.c_void_p(data)))
        ptr    = _cf.CFDataGetBytePtr(ctypes.c_void_p(data))
        buf    = (ctypes.c_uint8 * length).from_address(int(ptr))
        out: list[tuple[int, int, int]] = []
        target_per_axis = 16
        for (x, y, w, h) in rects_xywh:
            sx = max(1, w // target_per_axis)
            sy = max(1, h // target_per_axis)
            rs: list[int] = []
            gs: list[int] = []
            bs: list[int] = []
            row_y = y
            while row_y < y + h:
                row_off = row_y * bpr
                col_x = x
                while col_x < x + w:
                    off = row_off + col_x * 4
                    rs.append(buf[off + r_off])
                    gs.append(buf[off + g_off])
                    bs.append(buf[off + b_off])
                    col_x += sx
                row_y += sy
            rs.sort(); gs.sort(); bs.sort()
            mid = len(rs) // 2
            out.append((int(rs[mid]), int(gs[mid]), int(bs[mid])))
        return out
    finally:
        _cf.CFRelease(ctypes.c_void_p(data))


def get_pixels_in_rects(
    rects: list[tuple[int, int, int, int]],
    window_id: int,
    window_bounds: tuple[int, int, int, int],
) -> list[tuple[int, int, int]]:
    """
    Region-sampled batch pixel read: capture the target window ONCE, then for
    each rect return a per-channel median of a strided sample of pixels inside
    it. Median is robust against a small fraction of outlier pixels — e.g. a
    white letter glyph centered in a colored Wordle tile, an icon over a status
    badge — and reports the surrounding fill rather than the glyph.

    Use whenever you need to classify a cell's *state* by color (filled vs
    empty, green/yellow/gray, on/off) and there's a chance a glyph or icon
    sits over the center. Single-point sampling forces the caller to guess an
    offset that misses the glyph; region sampling removes the guesswork.

    rects: list of (x, y, w, h) in SCREEN-space coordinates. Each rect must
           lie fully inside the target window.
    Returns: list of (r, g, b) tuples, one per rect, same order as input.
    """
    win_x, win_y, win_w, win_h = window_bounds
    rel: list[tuple[int, int, int, int]] = []
    for (rx, ry, rw, rh) in rects:
        x0 = int(rx) - int(win_x)
        y0 = int(ry) - int(win_y)
        if rw <= 0 or rh <= 0:
            raise RuntimeError(
                f"get_pixels_in_rects: rect ({rx},{ry},{rw}×{rh}) has non-positive size."
            )
        if x0 < 0 or y0 < 0 or x0 + rw > win_w or y0 + rh > win_h:
            raise RuntimeError(
                f"get_pixels_in_rects: rect ({rx},{ry},{rw}×{rh}) extends outside "
                f"window {window_id} bounds ({win_x},{win_y},{win_w}×{win_h})."
            )
        rel.append((x0, y0, int(rw), int(rh)))

    raw = _capture_window_image(int(window_id), win_x, win_y, win_w, win_h)
    if not raw:
        raise RuntimeError(
            f"get_pixels_in_rects: window {window_id} capture returned NULL. "
            "Window may have closed, been minimized, or moved to another Space. "
            "Call list_windows to refresh."
        )
    try:
        scale = _image_pixel_scale(raw, int(win_w), int(win_h))
        scaled = [
            (int(x0 * scale), int(y0 * scale), max(1, int(w * scale)), max(1, int(h * scale)))
            for (x0, y0, w, h) in rel
        ]
        return _read_rect_medians_from_cgimage(raw, scaled)
    finally:
        _cf.CFRelease(ctypes.c_void_p(raw))


# ---------------------------------------------------------------------------
# Screenshot — CG primary, screencapture fallback
# ---------------------------------------------------------------------------

def take_screenshot(
    window_id: int | None = None,
    logical_width: int | None = None,
    logical_height: int | None = None,
    win_x: int | None = None,
    win_y: int | None = None,
    settle_ms: int = 50,
) -> tuple[str, int, int]:
    """
    Capture a window and return (base64_png, logical_width, logical_height).

    Primary path: CGWindowListCreateImage with global coordinates — works on any
    monitor seamlessly. If the window moves to a second display mid-session,
    the next call picks up the new position from _refresh_window and captures correctly.
    Fallback: screencapture -R region capture (also uses global coords, any monitor).
    """
    if settle_ms > 0:
        time.sleep(settle_ms / 1000)

    # Primary: CoreGraphics in-memory (no subprocess, ~40ms)
    if _HAS_IMAGEIO and win_x is not None and win_y is not None and logical_width and logical_height:
        try:
            return _take_screenshot_cg(win_x, win_y, logical_width, logical_height)
        except Exception:
            pass  # fall through to screencapture

    # Fallback: screencapture + sips subprocess pipeline
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as raw_f:
        raw_path = raw_f.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as out_f:
        out_path = out_f.name

    try:
        if win_x is not None and win_y is not None and logical_width and logical_height:
            region = f"{int(win_x)},{int(win_y)},{int(logical_width)},{int(logical_height)}"
            cmd = ["screencapture", "-x", "-t", "png", "-R", region, raw_path]
        elif window_id:
            cmd = ["screencapture", "-x", "-t", "png", "-l", str(window_id), "-o", raw_path]
        else:
            cmd = ["screencapture", "-x", "-t", "png", raw_path]

        result = subprocess.run(cmd, capture_output=True, timeout=10)

        if result.returncode != 0 or not os.path.exists(raw_path) or os.path.getsize(raw_path) < 100:
            fb = subprocess.run(
                ["screencapture", "-x", "-t", "png", raw_path],
                timeout=10, capture_output=True
            )
            if fb.returncode != 0 or not os.path.exists(raw_path) or os.path.getsize(raw_path) < 100:
                raise RuntimeError(
                    "screencapture failed. Ensure Screen Recording permission is granted to your terminal."
                )

        if logical_width and logical_height:
            max_dim = max(logical_width, logical_height)
            subprocess.run(
                ["sips", "-Z", str(max_dim), raw_path, "--out", out_path],
                capture_output=True, timeout=10
            )
            final_path = out_path
            final_w, final_h = logical_width, logical_height
        else:
            scale = get_scale_factor()
            info = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", raw_path],
                capture_output=True, text=True, timeout=10
            )
            pw, ph = _parse_sips_dimensions(info.stdout)
            final_w = int(pw / scale)
            final_h = int(ph / scale)
            if scale > 1.0:
                subprocess.run(
                    ["sips", "-Z", str(max(final_w, final_h)), raw_path, "--out", out_path],
                    capture_output=True, timeout=10
                )
                final_path = out_path
            else:
                final_path = raw_path

        with open(final_path, "rb") as f:
            data = f.read()

        return base64.b64encode(data).decode("utf-8"), final_w, final_h

    finally:
        for p in (raw_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _parse_sips_dimensions(sips_output: str) -> tuple[int, int]:
    pw = ph = 0
    for line in sips_output.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            pw = int(line.split(":")[-1].strip())
        elif line.startswith("pixelHeight:"):
            ph = int(line.split(":")[-1].strip())
    return pw or 1280, ph or 800


def check_screen_recording() -> None:
    """Raise RuntimeError if Screen Recording permission is not granted."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", tmp],
            capture_output=True, timeout=5
        )
        if result.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 100:
            raise RuntimeError(
                "klyk needs Screen Recording permission to capture window contents "
                "(used by screenshot / inspect / read_grid). Grant it:\n"
                "  System Settings → Privacy & Security → Screen Recording\n"
                "  Add your terminal app (Ghostty, Terminal, iTerm2, etc.), toggle ON.\n"
                "Then `klyk doctor` to verify, and restart your MCP client."
            )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
