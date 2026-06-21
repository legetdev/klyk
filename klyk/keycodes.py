# macOS virtual key codes + layout-aware character mapping.
#
# Two kinds of keys live here:
#
#   1. Named physical keys (Return, Tab, Escape, F-keys, arrows). These are
#      the same keycode on every Mac keyboard, regardless of the active
#      input source. They live in NAMED_KEYS and never change.
#
#   2. Character keys (letters, digits, punctuation). The keycode that
#      produces "z" depends on the user's active keyboard layout — on US
#      QWERTY z=6, on German QWERTZ z=16. We build a layout-correct
#      char -> (keycode, modifier_flags) map at runtime via TIS +
#      UCKeyTranslate, cache it, and rebuild when the input source changes.
#
# Not supported here on purpose: media/volume/brightness keys, caps_lock, and
# the fn modifier. Those use NX_SYSDEFINED events rather than
# CGEventCreateKeyboardEvent, so adding them to this table would silently fail
# at event-post time. Route through press_system_key instead.

import ctypes
import ctypes.util
import logging
import threading

logger = logging.getLogger("klyk.keycodes")


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# CGEventFlags bitmasks
MODIFIER_FLAGS: dict[str, int] = {
    "cmd": 0x100000, "command": 0x100000,
    "shift": 0x020000,
    "option": 0x080000, "alt": 0x080000,
    "ctrl": 0x040000, "control": 0x040000,
}

_SHIFT = MODIFIER_FLAGS["shift"]
_OPTION = MODIFIER_FLAGS["option"]


# Layout-independent named keys. Same keycode on every Mac keyboard.
NAMED_KEYS: dict[str, int] = {
    "return": 36, "enter": 36,
    "tab": 48, "space": 49,
    "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "f17": 64, "f18": 79, "f19": 80, "f20": 90,
    "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101,
    "f11": 103, "f13": 105, "f16": 106, "f14": 107, "f10": 109,
    "f12": 111, "f15": 113,
    "help": 114, "home": 115, "pageup": 116,
    "forwarddelete": 117, "del": 117,
    "f4": 118, "end": 119, "f2": 120, "pagedown": 121, "f1": 122,
    "left": 123, "right": 124, "down": 125, "up": 126,
    # Arrow-key aliases for the web KeyboardEvent.key names many models reach for
    # (ArrowLeft, ArrowUp, ...). Same keycodes — pure synonyms, no ambiguity.
    "arrowleft": 123, "arrowright": 124, "arrowdown": 125, "arrowup": 126,
}


# ---------------------------------------------------------------------------
# US-QWERTY fallback tables. Used only when TIS / UCKeyTranslate is unreachable
# (CI, sandboxed harness, non-macOS hosts). Kept as module-level dicts for
# backwards compatibility with external callers.
# ---------------------------------------------------------------------------

KEY_CODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    **NAMED_KEYS,
}

SHIFT_CHARS: dict[str, tuple[str, bool]] = {
    "~": ("`", True), "!": ("1", True), "@": ("2", True), "#": ("3", True),
    "$": ("4", True), "%": ("5", True), "^": ("6", True), "&": ("7", True),
    "*": ("8", True), "(": ("9", True), ")": ("0", True), "_": ("-", True),
    "+": ("=", True), "{": ("[", True), "}": ("]", True), "|": ("\\", True),
    ":": (";", True), '"': ("'", True), "<": (",", True), ">": (".", True),
    "?": ("/", True),
    **{c: (c.lower(), True) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
}


def _build_us_qwerty_char_map() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for ch, kc in KEY_CODES.items():
        if len(ch) == 1:
            out[ch] = (kc, 0)
    for ch, (base, needs_shift) in SHIFT_CHARS.items():
        kc = KEY_CODES.get(base)
        if kc is not None:
            out[ch] = (kc, _SHIFT if needs_shift else 0)
    return out


_US_FALLBACK_MAP: dict[str, tuple[int, int]] = _build_us_qwerty_char_map()


# ---------------------------------------------------------------------------
# Carbon / TIS bindings (loaded lazily, gracefully unavailable elsewhere).
# ---------------------------------------------------------------------------

_kUCKeyActionDisplay = 3
_kUCKeyTranslateNoDeadKeysMask = 1  # bit 0
_kCFStringEncodingUTF8 = 0x08000100


def _try_load(*paths: str | None):
    for path in paths:
        if not path:
            continue
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


_carbon = _try_load(
    "/System/Library/Frameworks/Carbon.framework/Carbon",
    ctypes.util.find_library("Carbon"),
)
_cf = _try_load(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
    ctypes.util.find_library("CoreFoundation"),
)

_tis_available = False
_kTISPropertyUnicodeKeyLayoutData = None
_kTISPropertyInputSourceID = None

if _carbon is not None and _cf is not None:
    try:
        _carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
        _carbon.TISCopyCurrentKeyboardLayoutInputSource.argtypes = []

        _carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        _carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        _carbon.LMGetKbdType.restype = ctypes.c_uint8
        _carbon.LMGetKbdType.argtypes = []

        _carbon.UCKeyTranslate.restype = ctypes.c_int32
        _carbon.UCKeyTranslate.argtypes = [
            ctypes.c_void_p,                       # const UCKeyboardLayout *
            ctypes.c_uint16,                       # UInt16 virtualKeyCode
            ctypes.c_uint16,                       # UInt16 keyAction
            ctypes.c_uint32,                       # UInt32 modifierKeyState
            ctypes.c_uint32,                       # UInt32 keyboardType
            ctypes.c_uint32,                       # OptionBits keyTranslateOptions
            ctypes.POINTER(ctypes.c_uint32),       # UInt32 *deadKeyState
            ctypes.c_ulong,                        # UniCharCount maxStringLength
            ctypes.POINTER(ctypes.c_ulong),        # UniCharCount *actualStringLength
            ctypes.POINTER(ctypes.c_uint16),       # UniChar *unicodeString
        ]

        _cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        _cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        _cf.CFRelease.restype = None
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
        _cf.CFStringGetLength.restype = ctypes.c_long
        _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        _cf.CFStringGetCString.restype = ctypes.c_bool
        _cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]

        _kTISPropertyUnicodeKeyLayoutData = ctypes.c_void_p.in_dll(
            _carbon, "kTISPropertyUnicodeKeyLayoutData"
        )
        _kTISPropertyInputSourceID = ctypes.c_void_p.in_dll(
            _carbon, "kTISPropertyInputSourceID"
        )
        _tis_available = True
    except (AttributeError, OSError, ValueError) as e:
        logger.debug("Carbon/TIS unavailable, falling back to US-QWERTY: %s", e)
        _tis_available = False


def _cfstring_to_str(cfstr: int | None) -> str | None:
    if not cfstr:
        return None
    length = _cf.CFStringGetLength(cfstr)
    buf_size = max(64, int(length) * 4 + 1)
    buf = ctypes.create_string_buffer(buf_size)
    if _cf.CFStringGetCString(cfstr, buf, buf_size, _kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", errors="replace")
    return None


def _current_source_id() -> str | None:
    if not _tis_available:
        return None
    src = _carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not src:
        return None
    try:
        cfstr = _carbon.TISGetInputSourceProperty(src, _kTISPropertyInputSourceID)
        return _cfstring_to_str(cfstr)
    finally:
        _cf.CFRelease(src)


def _build_layout_char_map() -> dict[str, tuple[int, int]] | None:
    """Walk the active layout's UCKeyboardLayout and emit char -> (kc, flags).

    Probes the four modifier states the OS uses for printable characters:
    none, shift, option, shift+option. Lower-mod entries win on collision,
    so plain letters map to no-modifier chords while symbols requiring
    Option (German "@" on Option+L, "|" on Option+7) get their proper chord.
    """
    if not _tis_available:
        return None

    src = _carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not src:
        return None

    try:
        layout_data = _carbon.TISGetInputSourceProperty(src, _kTISPropertyUnicodeKeyLayoutData)
        if not layout_data:
            return None
        layout_bytes = _cf.CFDataGetBytePtr(layout_data)
        if not layout_bytes:
            return None
        kbd_type = int(_carbon.LMGetKbdType())

        dead = ctypes.c_uint32(0)
        out_len = ctypes.c_ulong(0)
        out_buf = (ctypes.c_uint16 * 8)()
        options = _kUCKeyTranslateNoDeadKeysMask

        # Order matters: simpler chords first so they win on collision.
        mod_states: list[tuple[int, int]] = [
            (0, 0),
            (2, _SHIFT),                # shift
            (8, _OPTION),               # option
            (10, _SHIFT | _OPTION),     # shift+option
        ]

        char_map: dict[str, tuple[int, int]] = {}
        for kc in range(128):
            for mod_state, flags in mod_states:
                dead.value = 0
                out_len.value = 0
                ret = _carbon.UCKeyTranslate(
                    ctypes.c_void_p(layout_bytes),
                    kc,
                    _kUCKeyActionDisplay,
                    mod_state,
                    kbd_type,
                    options,
                    ctypes.byref(dead),
                    8,
                    ctypes.byref(out_len),
                    out_buf,
                )
                if ret != 0 or out_len.value != 1:
                    continue
                code = out_buf[0]
                if code < 0x20 or code == 0x7F:
                    continue  # control char, never claim it
                ch = chr(code)
                if ch not in char_map:
                    char_map[ch] = (kc, flags)
        return char_map
    finally:
        _cf.CFRelease(src)


# ---------------------------------------------------------------------------
# Cached char map with cheap staleness check on every read.
# ---------------------------------------------------------------------------

_map_lock = threading.Lock()
_cached_char_map: dict[str, tuple[int, int]] | None = None
_cached_source_id: str | None = None


def warm_keyboard_layout() -> None:
    """Build (or refresh) the cached layout char map.

    MUST run on the process MAIN THREAD. The Carbon/TIS input-source APIs this
    uses (TISCopyCurrentKeyboardLayoutInputSource / TISGetInputSourceProperty)
    assert they run on the main thread on macOS 14+ — internally via
    libdispatch's `dispatch_assert_queue` on the input-source-list path. Calling
    them from a background thread (as the old per-call staleness check did, on
    the asyncio worker) traps the whole process with SIGTRAP, intermittently
    (only when the input-source list needs rebuilding). So ALL TIS access is
    funnelled through this one function, which the MCP entrypoint invokes once
    at startup on the main thread and re-warms periodically on the main thread
    (via the UI dispatch queue) to pick up a mid-session layout switch.

    Never raises — on any failure the US-QWERTY fallback stands in.
    """
    global _cached_char_map, _cached_source_id
    if not _tis_available:
        with _map_lock:
            if _cached_char_map is None:
                _cached_char_map = _US_FALLBACK_MAP
        return
    try:
        current_id = _current_source_id()
        with _map_lock:
            if _cached_char_map is not None and current_id == _cached_source_id:
                return  # layout unchanged — keep the cached map
        built = _build_layout_char_map()
        with _map_lock:
            _cached_char_map = built if built else _US_FALLBACK_MAP
            _cached_source_id = current_id
    except Exception as e:
        logger.warning("warm_keyboard_layout failed (%s); using US fallback", e)
        with _map_lock:
            if _cached_char_map is None:
                _cached_char_map = _US_FALLBACK_MAP


def _get_char_map() -> dict[str, tuple[int, int]]:
    """Return the active char map. READ-ONLY off the main thread: it never
    calls the TIS/Carbon APIs (those are main-thread-only — see
    warm_keyboard_layout). The map is populated by the startup warm; the
    per-call TIS staleness probe that used to live here is gone because it ran
    on the asyncio worker thread and SIGTRAP'd the process.

    If the cache is somehow empty when a lookup runs, we build inline only when
    we're on the main thread; off the main thread we return the US-QWERTY
    fallback rather than touch a main-thread-only API from a worker.
    """
    with _map_lock:
        if _cached_char_map is not None:
            return _cached_char_map
    if threading.current_thread() is threading.main_thread():
        warm_keyboard_layout()
        with _map_lock:
            if _cached_char_map is not None:
                return _cached_char_map
    return _US_FALLBACK_MAP


def refresh_keyboard_layout() -> None:
    """Force a rebuild of the cached layout map on next lookup."""
    global _cached_char_map, _cached_source_id
    with _map_lock:
        _cached_char_map = None
        _cached_source_id = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def char_to_keycode(char: str) -> tuple[int | None, int]:
    """Return (keycode, modifier_flags) for a single printable character on
    the active keyboard layout, or (None, 0) if the character is not reachable
    via any single-key chord (caller should fall back to UnicodeString).
    """
    if not char:
        return None, 0
    cmap = _get_char_map()
    if char in cmap:
        return cmap[char]
    # Some layouts only enumerate the unshifted glyph; synthesize Shift+lower
    # for the uppercase letter case.
    if char.isalpha() and char != char.lower():
        kc, mods = cmap.get(char.lower(), (None, 0))
        if kc is not None:
            return kc, mods | _SHIFT
    return None, 0


def parse_key_combo(key_string: str) -> tuple[int, int]:
    """Parse a key or combo into (keycode, modifier_flags).

    Accepts:
      • a single character (any printable): 'a', 'A', '*', '+', 'ö'
      • a named non-character key: 'Return', 'Tab', 'Escape', 'F5', 'Up'
      • a Cmd/Shift/Option/Ctrl combination joined by '+':
        'Cmd+Z', 'Cmd+Shift+S', 'Ctrl+Alt+Delete', 'Cmd++' (Cmd plus '+')

    The character part of a combo is resolved against the active layout, so
    'Cmd+Z' fires the OS's undo shortcut on QWERTZ as well as QWERTY — the
    keycode produced is whatever physical key types 'z' on the user's layout.
    """
    if not key_string:
        raise ValueError("Empty key string")

    # Single-character form first — covers bare '+', '*', single letters,
    # and Unicode chars like 'ö' without splitting them.
    if len(key_string) == 1:
        low = key_string.lower()
        if low in NAMED_KEYS:
            return NAMED_KEYS[low], 0
        kc, mods = char_to_keycode(key_string)
        if kc is None:
            raise ValueError(f"Unknown key {key_string!r}")
        return kc, mods

    # Whole-string named key (Return, F5, PageUp, ...).
    low_whole = key_string.lower()
    if low_whole in NAMED_KEYS:
        return NAMED_KEYS[low_whole], 0
    if low_whole in MODIFIER_FLAGS:
        raise ValueError(f"{key_string!r} is a modifier, not a key")

    # Combo path. 'Cmd++' (Cmd plus '+') splits to ['Cmd', '', ''] — collapse
    # the empty pair into a literal '+'.
    parts = key_string.split("+")
    normalized: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        if p == "" and i + 1 < len(parts) and parts[i + 1].strip() == "":
            normalized.append("+")
            i += 2
            continue
        if p == "":
            i += 1
            continue
        normalized.append(p)
        i += 1

    flags = 0
    keycode: int | None = None
    for part in normalized:
        low = part.lower()
        if low in MODIFIER_FLAGS:
            flags |= MODIFIER_FLAGS[low]
            continue
        if low in NAMED_KEYS:
            keycode = NAMED_KEYS[low]
            continue
        # Treat single uppercase letter inside a combo as the plain letter
        # (so 'Cmd+A' = Cmd+a, not Cmd+Shift+a — matches the historical and
        # commonly-expected behavior).
        ch = part
        if len(ch) == 1 and ch.isalpha() and ch.isupper():
            ch = ch.lower()
        kc, mods = char_to_keycode(ch)
        if kc is None:
            raise ValueError(
                f"Unknown key {part!r} in {key_string!r}. Use a single character, "
                "a named key (Return, Tab, Escape, Up/Down/Left/Right, F1-F12, "
                "Home, End, PageUp, PageDown), or a combo like 'Cmd+S'."
            )
        keycode = kc
        flags |= mods

    if keycode is None:
        raise ValueError(f"No non-modifier key in {key_string!r}")
    return keycode, flags
