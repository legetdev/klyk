"""Fresh, window-only ScreenCaptureKit screenshots through the existing PyObjC bridge.

Only native filter/configuration objects are cached, never pixels. The bounded
cache expires after ten seconds; a failed capture evicts its plan immediately.
"""

import base64
from collections import OrderedDict
import threading
import time

_lock = threading.RLock()
_classes = None
_plans = OrderedDict()
_CAPACITY = 16
_TTL = 10.0
_TIMEOUT = 2.0


def _load():
    """Load Apple's framework without adding another PyObjC framework package."""
    global _classes
    if _classes is not None:
        return _classes
    import objc
    from Foundation import NSBundle
    import Quartz

    if not NSBundle.bundleWithPath_(
        '/System/Library/Frameworks/ScreenCaptureKit.framework'
    ).load():
        raise RuntimeError('ScreenCaptureKit is unavailable.')
    for class_name, selector, result_type in (
        (b'SCShareableContent', b'getShareableContentExcludingDesktopWindows:onScreenWindowsOnly:completionHandler:', b'@'),
        (b'SCScreenshotManager', b'captureImageWithFilter:configuration:completionHandler:',
         Quartz.CGImageGetWidth.__metadata__()['arguments'][0]['type']),
    ):
        objc.registerMetaDataForSelector(class_name, selector, {
            'arguments': {4: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}, 1: {'type': result_type}, 2: {'type': b'@'}},
            }}},
        })
    _classes = tuple(objc.lookUpClass(name) for name in (
        'SCShareableContent', 'SCContentFilter', 'SCStreamConfiguration', 'SCScreenshotManager',
    ))
    return _classes


def _complete(start, deadline):
    """Wait for one native completion, retaining its values through Python ownership."""
    finished = threading.Event()
    result = []

    def receive(value, error):
        """Retain callback values even if completion arrives after the caller times out."""
        result.extend((value, error))
        finished.set()

    start(receive)
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError('Window screenshot timed out.')
    value, error = result
    if error is not None or value is None:
        raise RuntimeError(f'Window screenshot unavailable: {error or "empty native result"}')
    return value


def take(window_id, width, height):
    """Return fresh PNG pixels at logical window dimensions; never widen capture scope."""
    from .image_bounds import png_dimensions, validate_image_dimensions

    if (isinstance(window_id, bool) or not isinstance(window_id, int)
            or not 1 <= window_id <= 0xffffffff):
        raise ValueError('Window capture requires a valid positive window ID.')
    validate_image_dimensions(width, height)
    key = (window_id, width, height)
    import objc
    import Quartz
    from Foundation import NSMutableData
    if not Quartz.CGPreflightScreenCaptureAccess():
        raise RuntimeError('Screen Recording permission is required.')
    deadline = time.monotonic() + _TIMEOUT
    with _lock, objc.autorelease_pool():
        content_class, filter_class, config_class, manager = _load()
        now = time.monotonic()
        for old_key, (created, _, _) in list(_plans.items()):
            if now - created >= _TTL:
                del _plans[old_key]
        plan = _plans.get(key)
        if plan is None:
            content = _complete(
                lambda done: content_class.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, False, done),
                deadline,
            )
            window = next((win for win in content.windows() if int(win.windowID()) == key[0]), None)
            if window is None:
                raise RuntimeError('The requested window is no longer capturable.')
            window_filter = filter_class.alloc().initWithDesktopIndependentWindow_(window)
            config = config_class.alloc().init()
            config.setWidth_(key[1])
            config.setHeight_(key[2])
            config.setShowsCursor_(False)
            if hasattr(config, 'setIgnoreShadowsSingleWindow_'):
                config.setIgnoreShadowsSingleWindow_(True)
            if hasattr(config, 'setScalesToFit_'):
                config.setScalesToFit_(True)
            plan = (time.monotonic(), window_filter, config)
            _plans[key] = plan
            while len(_plans) > _CAPACITY:
                _plans.popitem(last=False)
        try:
            image = _complete(
                lambda done: manager.captureImageWithFilter_configuration_completionHandler_(plan[1], plan[2], done),
                deadline,
            )
            actual = (int(Quartz.CGImageGetWidth(image)), int(Quartz.CGImageGetHeight(image)))
            if actual != key[1:]:
                raise RuntimeError('Window screenshot dimensions changed during capture; observe again.')
            data = NSMutableData.data()
            destination = Quartz.CGImageDestinationCreateWithData(data, 'public.png', 1, None)
            if destination is None:
                raise RuntimeError('Window screenshot encoder is unavailable.')
            Quartz.CGImageDestinationAddImage(destination, image, None)
            if not Quartz.CGImageDestinationFinalize(destination):
                raise RuntimeError('Window screenshot encoding failed.')
            if len(data) > 24 * 1024 * 1024:
                raise ValueError('PNG output exceeds the supported size; use a smaller window or crop.')
            encoded = base64.b64encode(bytes(data)).decode('ascii')
            if png_dimensions(encoded) != actual:
                raise RuntimeError('Window screenshot encoding changed its dimensions; observe again.')
            return encoded, *actual
        except Exception:
            _plans.pop(key, None)
            raise
