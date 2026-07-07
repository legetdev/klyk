"""
Klyk MCP Server
OS-level computer use for native macOS and Electron app testing.
Web testing is handled by Playwright MCP — this tool owns the desktop.
"""

import asyncio
import base64
import difflib
import json
import logging
import os
import sys
import time
import traceback
import unicodedata
import uuid
from collections import deque
try:
    # Ships with the MCP SDK; used to give `run`'s nested steps the same
    # input validation the SDK applies to top-level calls. Guarded so a
    # missing install degrades to "no nested validation", never a hard import
    # failure.
    import jsonschema as _jsonschema
except Exception:  # pragma: no cover
    _jsonschema = None
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH = os.path.expanduser("~/klyk.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[RotatingFileHandler(LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")],
)
# Defense-in-depth: the log can contain captured stderr — lock it to owner-only.
# Never let a chmod failure block startup (e.g. unusual filesystem perms).
try:
    os.chmod(LOG_PATH, 0o600)
except OSError:
    pass
log = logging.getLogger("klyk")
log.info("=" * 60)
log.info("Klyk MCP server starting")

# ---------------------------------------------------------------------------
# Startup permission checks
# ---------------------------------------------------------------------------

from .computer import check_accessibility
from .capture import check_screen_recording
for _check_fn in (check_accessibility, check_screen_recording):
    try:
        _check_fn()
        log.info(f"Permission check passed: {_check_fn.__name__}")
    except RuntimeError as _e:
        log.error(f"Permission check failed: {_check_fn.__name__}\n{_e}")
        print(f"[klyk] STARTUP ERROR:\n{_e}", file=sys.stderr)
        sys.exit(1)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from . import __version__
from . import activity
from . import capture
from . import computer
from . import matcher
from . import ocr
from . import ownership
from . import reporter as reporter_mod
from . import skylight
from .launcher import is_browser, CHROMIUM_BROWSERS, is_chromium_renderer_app
from .session import get_or_create_session, close_app, registry, list_sessions as _list_sessions, window_labels
from .ui_thread import ui as _ui

# Browser AX trees explode to hundreds of elements once
# --force-renderer-accessibility is on. Filter to clearly-interactive roles
# so the agent doesn't drown in <span>/<div> noise. Static text, headings,
# tables, and structural containers are dropped — keep things you can click,
# type into, or pick from. The role set lives in ax_roles.py so it can't
# drift from the broader INTERACTIVE_ROLES list computer.py uses for AX scans.
from .ax_roles import (
    BROWSER_INTERACTIVE_ROLES as _BROWSER_INTERACTIVE_ROLES,
    INTERACTIVE_ROLES as _INTERACTIVE_ROLES,
)


# Browser chrome — fixed controls in the toolbar, URL bar, and account area.
# Dropped from the agent-facing AX list so the response token budget goes to
# real page content. The bookmarks bar uses user-defined labels and can't be
# enumerated here; those are caught by the value-less AXPopUpButton heuristic
# in _is_browser_shell below.
_BROWSER_SHELL_LABELS = frozenset({
    "Back", "Forward", "Reload", "Home",
    "View site information",
    "Address and search bar",
    "Translate",
    "Bookmark this tab",
    "Tab groups",
    "Extensions",
    "Menu containing hidden bookmarks",
    "Show Sidebar",
    "Mode",
    "Chrome",
})


def _is_browser_shell(elem: dict) -> bool:
    """
    Heuristic: is this AX element part of the browser chrome rather than
    the page? Hardcoded shell labels first, then the bookmarks-bar pattern
    (an AXPopUpButton with a label, no value, and a small height — the
    typical shape of a single bookmark folder/link in the bookmarks bar).
    Page popups for <select> elements always carry a `value` so they
    aren't mistaken for bookmarks.
    """
    label = elem.get("label", "") or ""
    if label in _BROWSER_SHELL_LABELS:
        return True
    if (
        elem.get("role") == "AXPopUpButton"
        and label
        and not elem.get("value")
        and 0 < int(elem.get("height", 0) or 0) <= 32
    ):
        return True
    return False


def _filter_for_browser(elements: list[dict], app_name: str | None) -> list[dict]:
    """
    Filter a browser's AX element list down to what an agent can actually
    act on inside the current page. Three rules, applied in order:
      1. Drop browser chrome (toolbar buttons, URL bar shell, account picker,
         bookmark-bar entries) — agents rarely target these and they bury
         page content under the response cap.
      2. Keep elements with roles in BROWSER_INTERACTIVE_ROLES — buttons,
         links, inputs, popups.
      3. Also keep AXStaticText whose visible value is 1-3 characters —
         this is the shape of game tiles (Wordle letters), table badges,
         status icons, single-digit counters. Long-form static text
         (paragraphs, headings) is still dropped.
    Returns elements in the original order so the caller's
    matches_found[index] semantics stay stable.
    """
    if not is_browser(app_name):
        return elements
    out: list[dict] = []
    for e in elements:
        if _is_browser_shell(e):
            continue
        role = e.get("role")
        if role in _BROWSER_INTERACTIVE_ROLES:
            out.append(e)
            continue
        if role == "AXStaticText":
            v = (e.get("value") or "").strip()
            if 1 <= len(v) <= 3:
                out.append(e)
    return out


# Map all Unicode hyphen/dash variants to ASCII '-' so a query like "Wi-Fi"
# matches a label rendered with U+2011 (e.g. macOS "Wi‑Fi"). Without this,
# substring matching fails on visually identical strings.
_HYPHEN_VARIANTS = str.maketrans({
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "−": "-",  # minus sign
})


def _normalize_label(s: str) -> str:
    # NFC-normalize so canonically-equivalent forms match: macOS filesystem
    # labels (Finder rows, save/open dialogs) come back as NFD ("e" + combining
    # acute) while an agent's query is almost always NFC ("é"). Without this,
    # accented/umlaut labels — common in non-English locales — silently fail to
    # match. Keep .lower() (not casefold) so ASCII matching is byte-for-byte
    # unchanged (NFC of ASCII is identity). For non-ASCII it canonicalizes BOTH
    # query and candidate, so canonically-equivalent forms now match (the fix);
    # the only matches it can remove are spurious ones that straddled a
    # decomposed combining mark, which no real query intends.
    return unicodedata.normalize("NFC", s).translate(_HYPHEN_VARIANTS).lower()

def _match_tier(text: str, query: str) -> int:
    """Label-match quality for ranking a candidate against a search query.

    0 = exact, 1 = prefix, 2 = substring, 3 = no/empty text. Lower is better.
    Used to prefer an exact label hit over an incidental substring hit when
    several elements match the same query — e.g. the "Bilder" tab (exact)
    over Google's "Suche anhand von Bildern" button (substring). `query` is
    already normalized by the caller; normalize the candidate to match.
    """
    t = _normalize_label(text or "")
    if not t:
        return 3
    if t == query:
        return 0
    if t.startswith(query):
        return 1
    return 2

def _rank_ax_matches(matches: list[dict], query: str) -> None:
    """Stable-sort AX matches in place so exact hits precede substring hits.

    Lets the element the caller actually named win over an incidental
    substring hit (e.g. the "Bilder" tab over "Suche anhand von Bildern"),
    regardless of AX-tree order, while leaving the relative order of genuine
    ties untouched so `index` stays meaningful. Pure in-memory sort of an
    already-capped list — no extra IPC, no measurable latency.
    """
    matches.sort(key=lambda e: min(
        _match_tier(e.get("label", ""), query),
        _match_tier(e.get("value", ""), query),
    ))

def _rank_ocr_matches(matches: list[dict], query: str) -> None:
    """Stable-sort OCR text matches in place, exact hits before substring hits."""
    matches.sort(key=lambda m: _match_tier(m.get("text", ""), query))

def _collapse_ws(s: str) -> str:
    """Remove all whitespace. Used as a last-tier OCR comparison so a label
    Vision fragmented across a stray gap ('EN TER') still matches the intended
    query ('enter') without widening matching to unrelated text."""
    return "".join(s.split())

def _ocr_candidates(observations: list[dict], query: str, limit: int = 8) -> list[dict]:
    """Rank visible on-screen text by similarity to a query that matched
    nothing, and return the closest few as lean {text, x, y, similarity} dicts
    (x/y window-relative, matching every other tool's coordinate space).

    Turns click_element's 'not found' dead-end into a recoverable step: an agent
    — especially a small/fast model — can retry with the exact rendered spelling
    or click the coordinates directly instead of looping blind. Pure in-memory
    ranking over an already-captured observation set: no extra OCR, no IPC."""
    scored: list[tuple[float, dict, str]] = []
    for m in observations:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        norm = _normalize_label(text)
        # Best of raw vs whitespace-collapsed similarity, so a fragmented word
        # still ranks near its query.
        ratio = max(
            difflib.SequenceMatcher(None, query, norm).ratio(),
            difflib.SequenceMatcher(None, _collapse_ws(query), _collapse_ws(norm)).ratio(),
        )
        scored.append((ratio, m, text))
    scored.sort(key=lambda t: -t[0])
    return [
        {
            "text": text,
            "x": int(m.get("x", 0)),
            "y": int(m.get("y", 0)),
            "similarity": round(float(ratio), 2),
        }
        for ratio, m, text in scored[:limit]
    ]

def _win_rel(elem: dict, session) -> dict:
    """Return a shallow copy of an AX element with its screen-space x/y
    translated to window-relative — the coordinate space every klyk tool
    exposes to the agent (it matches screenshot pixels). The original is left
    untouched so the screen-space coords used for click delivery stay intact.
    Caller must ensure session.win_x/win_y are current (call _refresh_window)."""
    out = dict(elem)
    if "x" in out:
        out["x"] = int(out["x"]) - int(session.win_x)
    if "y" in out:
        out["y"] = int(out["y"]) - int(session.win_y)
    return out

# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

async def _check_click_safety(session, x: int, y: int) -> tuple[bool, str]:
    """x, y are window-relative (matching screenshot pixel space)."""
    if session.width > 0:
        if not (0 <= x <= session.width and 0 <= y <= session.height):
            # Failure-mode-specific hint — generic "override" advice was misleading
            # when the real issue is the agent has wrong coords (e.g. picked from a
            # different window of similar layout) or the target is below the viewport.
            parts = []
            if y > session.height:
                parts.append(
                    f"y={y} is below the window viewport (window height {session.height}); "
                    "if the target sits below what's visible, scroll the page first or "
                    "verify you're targeting the right window (heights vary across windows)"
                )
            elif y < 0:
                parts.append(f"y={y} is above the window top (window y starts at 0)")
            if x > session.width:
                parts.append(f"x={x} is past the right edge (window width {session.width})")
            elif x < 0:
                parts.append(f"x={x} is left of the window (window x starts at 0)")
            why = "; ".join(parts) or f"({x},{y}) is outside {session.width}×{session.height}"
            return False, (
                f"Click rejected: {why}. Coordinates are window-relative — (0,0) is top-left, "
                "max is (width-1, height-1). Pass confirm_destructive=true only if you "
                "genuinely want to click outside this window."
            )
    return True, ""


def _to_screen(session, x: int, y: int) -> tuple[int, int]:
    """Convert window-relative coordinates (screenshot pixel space) to screen coordinates."""
    return session.win_x + x, session.win_y + y


async def _nearby_ax_hint(session, x: int, y: int, radius: int = 20) -> dict | None:
    """If a labeled AX element sits within `radius` px of (x, y) in window space, return
    a hint suggesting click_element. Coords passed in are window-relative."""
    try:
        from . import computer as _computer
        elements = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _computer.ax_snapshot(session.pid)
        )
    except Exception:
        return None
    wx, wy = session.win_x, session.win_y
    best = None
    best_dist = radius + 1
    for elem in elements:
        label = elem.get("label") or elem.get("value")
        if not isinstance(label, str) or not label.strip():
            continue
        ex = elem.get("x", 0) - wx
        ey = elem.get("y", 0) - wy
        if abs(ex - x) > radius or abs(ey - y) > radius:
            continue
        dist = ((ex - x) ** 2 + (ey - y) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best = {
                "label": label.strip()[:80],
                "role": elem.get("role", ""),
                "distance_px": round(dist, 1),
                "suggestion": "Prefer click_element(label=...) over click(x, y) when a label exists.",
            }
    return best

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

# Server-level instructions are surfaced to the MCP client and included in
# the model's context for every session that loads klyk. Keep this concise
# (every session pays the tokens) and action-oriented: tell the agent how
# to route browser-vs-native, not what klyk is internally. Tool-level
# descriptions handle per-tool nuance.
_SERVER_INSTRUCTIONS = (
    "klyk drives macOS like a human at the keyboard: real clicks, keypresses, "
    "screen capture, accessibility reads. Native AppKit apps, Electron apps, "
    "system dialogs, the Dock, menu bars, system settings.\n"
    "\n"
    "DEFAULT MODE — every new session starts in 'autonomous': klyk operates "
    "invisibly when it can (no cursor movement, no focus theft) and briefly "
    "activates the target app only when SkyLight cannot deliver (e.g. "
    "Chromium web content). Top priority is getting the task done. The user "
    "sees the EFFECTS of klyk's actions in the target window as they happen "
    "(pages scroll, fields fill, buttons depress); they just don't see "
    "klyk's own cursor. You almost never need to call set_mode — only when "
    "the user explicitly wants a different behavior.\n"
    "\n"
    "ROUTING — browser web content: when the task is reading or interacting "
    "with WEB CONTENT inside a browser (clicking page elements, filling forms, "
    "scrolling pages, extracting rendered text), prefer Playwright MCP if it's "
    "available. Reason: Chromium's renderer trusted-event filter empirically "
    "discards SkyLight-synthesized clicks to a backgrounded Chrome window, so "
    "klyk must briefly bring the browser to the foreground for each web "
    "interaction — visible as a focus flicker. Playwright drives the renderer "
    "via Chrome DevTools Protocol, which the renderer trusts even when the "
    "window is backgrounded.\n"
    "\n"
    "OVERRIDE: if the user explicitly directs klyk to operate on the browser "
    "(\"use klyk here\", \"no, use klyk on this Chrome tab\"), do so — the "
    "session is already in autonomous mode by default, so klyk will activate "
    "the browser before each web action and proceed. The focus flicker is the "
    "documented Chromium trade-off, not a bug.\n"
    "\n"
    "Everything outside browser web content — Finder, system dialogs, native "
    "macOS apps, the menu bar, the Dock, system settings, Electron app chrome "
    "(toolbar, menus, sidebars) — klyk is the right tool and runs fully "
    "invisibly in the default autonomous mode.\n"
    "\n"
    "USING THESE TOOLS: klyk's tools are already loaded and ready to call "
    "directly — start with `screenshot` to see the screen, then act. Do NOT "
    "read or search klyk's source files to learn the API; everything you need "
    "is in these tool descriptions."
)

server = Server("klyk", version=__version__, instructions=_SERVER_INSTRUCTIONS)

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_APP_PARAM = {
    "app": {
        "type": "string",
        "description": (
            "App display name (e.g. 'Youty', 'Finder', 'Safari') or path to .app bundle. "
            "Klyk launches the app automatically on first use."
        ),
    }
}

_APP_LAUNCH_PARAMS = {
    **_APP_PARAM,
    "target": {
        "type": "string",
        "enum": ["native", "electron"],
        "description": "App type. Defaults to 'native'. Use 'electron' for Electron apps.",
    },
    "bundle_id": {
        "type": "string",
        "description": "CFBundleIdentifier for reliable app matching (e.g. 'com.example.Youty').",
    },
    "app_path": {
        "type": "string",
        "description": "Full path to .app bundle. Useful for Electron apps not in /Applications.",
    },
}

_CONFIRM_DESTRUCTIVE = {
    "confirm_destructive": {
        "type": "boolean",
        "default": False,
        "description": "Set true to bypass the safety check.",
    }
}

_WINDOW_ID_PARAM = {
    "window": {
        "type": "string",
        "description": (
            "Optional window label (A, B, C, ...) from list_windows. When set, the tool targets "
            "that specific window — raising it first if needed, and using its bounds for "
            "coordinates and screenshots. Labels are stable per window across calls. "
            "Omit for the common single-window case; default = app's frontmost window."
        ),
    },
    "window_id": {
        "type": "integer",
        "description": (
            "Optional raw CG window ID (advanced). Prefer 'window' (the A/B/C label) for "
            "readability — they refer to the same windows. Either one works."
        ),
    },
}

# Opt-in cheap post-action probe. When true, the action response includes a
# top-level `verify` object: {"focused": {"label","role","value"}, "window_title"}.
# Lets the agent confirm focus / detect a new modal without a follow-up
# `inspect` round-trip (which costs a full AX walk + screenshot). Off by
# default to keep response payloads lean (Design Consideration #4).
_VERIFY_PARAM = {
    "verify": {
        "type": "boolean",
        "default": False,
        "description": (
            "Set true to attach a cheap focused-element + window-title snapshot "
            "to the response. Skips the need for a follow-up `inspect` to confirm "
            "the action's effect. ~5-15 ms overhead. Default false."
        ),
    }
}

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="inspect",
        description=(
            "Look at an app: labeled interactive AX elements (buttons, links, fields) with "
            "window-relative coordinates, plus a screen image. Launches the app if not running. "
            "PREFER AX-ONLY FIRST: for most observations, `ax_snapshot` (pure AX, no image) or "
            "`inspect detail='slim'` already answers the question — what's on screen, where a "
            "control is, whether focus or a value changed — at a fraction of the cost. Default "
            "to AX even on the web: most web and Electron apps (games included) expose rich "
            "ARIA that ax_snapshot / read_grid read exactly. Pull the full image only when AX "
            "genuinely falls short — ax_snapshot came back empty or thin, or the question is "
            "visual (layout, rendering, color). To confirm an action's effect, prefer an AX read "
            "(ax_snapshot / read_grid / read_element) or verify=true — never a screenshot. "
            "Coordinates: x=0 y=0 is the window's top-left; AX coords already match image pixels "
            "and are accepted directly by click/fill/scroll. "
            "The AX element currently holding keyboard focus is marked `focused: true` in the "
            "list — use it after typing or tabbing to confirm input landed, without a separate "
            "call. "
            "Which source is canonical depends on the question: for TARGETING, PRESENCE, and "
            "STATE (is X there, did focus move, did a field's value change) trust AX and the "
            "action's own result — they are current. For VISUAL qualities (layout, what actually "
            "drew) the image is canonical; for color use get_pixel / read_grid (image color is "
            "unreliable due to compression). "
            "A capture taken right after a mutating action automatically waits ~150 ms for the "
            "UI to repaint, so it shows the post-action state, not a stale pre-action frame. "
            "Latency ~90-140 ms passive (image + AX run concurrently), ~240-290 ms right after "
            "an action. AX is best-effort; image never fails. A `focus_warning` means the "
            "captured image may be a different window of the same app — stop and resolve before "
            "acting. An `overlap_warning` means another app's window overlaps this one and its "
            "pixels may bleed into the composited image — trust AX reads, or focus_window first.\n"
            "\n"
            "**`detail` knob** — `detail='slim'` drops the image and caps AX to the 15 "
            "most-actionable elements (~40 ms, a few hundred bytes): the default choice for "
            "focus / modal / presence checks. Use `detail='full'` (image + up to 50 AX) only "
            "when you need pixels per the rule above — slim may not contain your target."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_LAUNCH_PARAMS,
                **_WINDOW_ID_PARAM,
                "detail": {
                    "type": "string",
                    "enum": ["full", "slim"],
                    "default": "full",
                    "description": (
                        "`full` (default) returns the image + up to 50 AX elements — use only "
                        "when you need pixels (sparse-AX/Electron/web/canvas, visual or color "
                        "checks). `slim` drops the image and caps AX to 15 elements — fastest, "
                        "smallest payload, and the preferred default for AX-answerable checks "
                        "(focus, presence, value, spotting a new modal)."
                    ),
                },
                "save_path": {
                    "type": "string",
                    "description": (
                        "Absolute path (or ~-relative) to write the PNG to. When set, the inline "
                        "image is omitted from the response and the path is returned as saved_path. "
                        "Parent directory must already exist — write failure falls back to inline "
                        "image and reports save_error. Ignored when detail='slim'."
                    ),
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="screenshot",
        description=(
            "Capture the app's image only — no AX list. Use sparingly: only when you genuinely "
            "need pixels and no follow-up click. Good fits: diagnosing a visual bug, evaluating "
            "design, before/after frames, or purely visual verification. "
            "Default to AX-only observation instead (`ax_snapshot`, or `inspect detail='slim'`) "
            "— it answers most 'what's there / did it work' questions far cheaper, and when you "
            "do need to click you'll want labels and coordinates. Reach for an image only when an "
            "AX read (ax_snapshot / read_grid) comes back empty or thin, or the question is "
            "genuinely visual. To verify an action worked, read AX or use verify=true — not an "
            "image. "
            "Same coord conventions and `focus_warning` semantics as inspect; "
            "save_path writes to disk instead of inline. "
            "Multi-display: pass `display` (0-based index from screen_info.displays, or 'main') "
            "to capture the whole display instead of the app's window — useful for surveying "
            "a second monitor or system-level UI outside the app. When `display` is set, "
            "`window_id` is ignored and the image covers the full display in screen-space "
            "coordinates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_LAUNCH_PARAMS,
                **_WINDOW_ID_PARAM,
                "display": {
                    "oneOf": [{"type": "integer", "minimum": 0}, {"type": "string", "enum": ["main"]}],
                    "description": (
                        "Capture an entire display instead of the app's window. Pass the 0-based "
                        "`index` from `screen_info`, or the string 'main'. Ignored if not set."
                    ),
                },
                "save_path": {
                    "type": "string",
                    "description": (
                        "Absolute path (or ~-relative) to write the PNG to. When set, the inline "
                        "image is omitted from the response and the path is returned as saved_path."
                    ),
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="click",
        description=(
            "LAST RESORT. Use only when the target is not in the AX tree, has no visible text, "
            "and cannot be templated. Prefer click_element(label=...) for any text label and "
            "get_template + find_template for icons. Eyeballed coordinates bypass the three-tier "
            "targeting that exists to avoid pixel guessing. "
            "Window-relative coords (x=0 y=0 = window's top-left). button='right' for context "
            "menus; modifiers=['cmd'|'shift'|'alt'|'ctrl'] stamp through. Blocked if outside "
            "window bounds — pass confirm_destructive=true to override. If an AX element exists "
            "within 20 px, response includes nearby_ax_hint suggesting click_element. "
            "Native apps (autonomous / background) route through SkyLight and are FULLY INVISIBLE: "
            "the cursor doesn't move, the target window isn't raised, and the user's focus never "
            "changes — the same in both modes (`via:'skylight+keyed'`, or `'skylight'` on the rare "
            "macOS where the key-window helper is unavailable). Chromium-based apps (browsers and "
            "Electron) are the exception: their renderer mishandles synthetic clicks, so klyk uses "
            "a real cursor there — autonomous briefly activates the window and clicks "
            "(`via:'cursor_warp'`, `escalated_from:'chromium_cursor_warp'`); background returns "
            "`{ok:false, requires_foreground:true}`. Humanoid always uses cursor_warp. "
            "Multi-window: a `focus_warning` means the click landed in the wrong window — stop and resolve. "
            "Safety: don't click an unfamiliar URL or a money-moving control (Send, Buy, Confirm "
            "Transfer, Place Order, Sign) without the user's explicit OK in this session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held during click. E.g. ['cmd'] for Cmd+Click, ['shift'] for Shift+Click.",
                },
                **_CONFIRM_DESTRUCTIVE,
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="double_click",
        description=(
            "Double-click at (x, y). Two mouse-down/up pairs with kCGMouseEventClickState set "
            "to 2 on the second pair so apps see a real double-click, not two fast singles. "
            "Coordinates are window-relative (same space as click and screenshot). "
            "Supports modifiers — e.g. modifiers=['cmd'] for Cmd+Double-click. "
            "SEAMLESS MODE (background / autonomous): native apps route through SkyLight fully "
            "invisibly — cursor doesn't move, target window isn't raised, focus doesn't change, "
            "modifier flags stamp through (`via:'skylight+keyed'`, or `'skylight'` if the "
            "key-window helper is unavailable; `+primer` for Chromium). Chromium browser web "
            "content instead uses a real cursor: autonomous activates + clicks (`via:'cursor_warp'`), "
            "background returns `{ok:false, requires_foreground:true}`. Humanoid: `via:'cursor_warp'`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held during the double-click.",
                },
                **_CONFIRM_DESTRUCTIVE,
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="triple_click",
        description=(
            "Triple-click at (x, y). Three mouse-down/up pairs with kCGMouseEventClickState "
            "set to 1, 2, 3 so apps recognise it as a real triple-click — selects the full "
            "paragraph in a text view, the full contents of a single-line field (URL bar, "
            "address bar), or the full line in a code editor. "
            "Coordinates are window-relative (same space as click and screenshot). "
            "Supports modifiers — e.g. modifiers=['shift'] to extend an existing selection. "
            "SEAMLESS MODE (background / autonomous): native apps route through SkyLight fully "
            "invisibly — cursor doesn't move, target window isn't raised, focus doesn't change, "
            "modifier flags stamp through (`via:'skylight+keyed'`, or `'skylight'` if the "
            "key-window helper is unavailable; `+primer` for Chromium). Chromium browser web "
            "content instead uses a real cursor: autonomous activates + clicks (`via:'cursor_warp'`), "
            "background returns `{ok:false, requires_foreground:true}`. Humanoid: `via:'cursor_warp'`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held during the triple-click.",
                },
                **_CONFIRM_DESTRUCTIVE,
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="long_press",
        description=(
            "Press and hold the mouse button at (x, y) for `duration` seconds, then release. "
            "Use for controls whose behavior changes with hold time — context menus that appear "
            "on long-press, app-icon springboards, drag-handles that arm only after a hold, "
            "video-scrub gestures, custom long-press shortcuts. For a normal click, use click. "
            "For dragging from A to B, use drag. Default duration is 1.0 s — bump up for "
            "interactions known to need a longer hold (some iPad-style spring-loaded menus "
            "need 1.5-2 s). The emergency-stop chord (Cmd+Shift+Escape) is checked every 50 ms "
            "during the hold so a long press doesn't block escape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "duration": {
                    "type": "number",
                    "default": 1.0,
                    "minimum": 0.1,
                    "maximum": 10.0,
                    "description": "How long to hold the button down, in seconds.",
                },
                "button": {
                    "type": "string",
                    "default": "left",
                    "enum": ["left", "right"],
                    "description": "Which mouse button to hold.",
                },
                **_CONFIRM_DESTRUCTIVE,
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="ax_action",
        description=(
            "Invoke an accessibility action on the element at (x, y) directly — bypassing "
            "the mouse pipeline. More reliable than click for activating controls whose "
            "hit area is small, whose layout is dynamic, or which respond cleanly to AX "
            "but oddly to synthetic clicks (accessibility-focused apps, custom controls). "
            "Common actions: AXPress (primary action — buttons, links), AXShowMenu "
            "(open contextual menu), AXPick (choose an item in a combobox/popup), "
            "AXIncrement / AXDecrement (sliders, steppers), AXCancel (dismiss / close), "
            "AXConfirm (accept default). On failure the response includes available_actions "
            "for that element so the agent can retry with a supported action — no extra "
            "round-trip needed to discover what the element supports. Coordinates are "
            "window-relative, same space as click."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "action": {
                    "type": "string",
                    "description": (
                        "AX action name to invoke. Standard set: AXPress, AXShowMenu, "
                        "AXPick, AXIncrement, AXDecrement, AXCancel, AXConfirm. "
                        "Other actions are accepted — the response confirms which the "
                        "element actually supports."
                    ),
                },
            },
            "required": ["app", "x", "y", "action"],
        },
    ),
    types.Tool(
        name="drag",
        description=(
            "Drag from (x1, y1) to (x2, y2). Mouse-down, ~20 interpolated drag events, "
            "mouse-up. Window-relative coords. Use for unlabeled drags — sliders, canvas "
            "objects, dividers, custom handles. For labeled drags (file → folder, row "
            "reorder, cross-app), prefer `drag_to_element` so the agent doesn't eyeball "
            "coordinates. "
            "Modifiers (Cmd / Option / Shift) hold across the whole drag and apply in "
            "autonomous/background (SkyLight) mode; humanoid fallback drops them. "
            "`hover_seconds` (default 0) holds the cursor at the target — still pressed — "
            "before releasing, for spring-loaded drops. "
            "Response `via`: 'skylight+keyed' (native, fully invisible — no raise, no focus "
            "change) or '+primer' for Chromium in seamless mode, 'cursor_warp' in humanoid. "
            "Background returns `requires_foreground:true` only for Chromium web content."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x1": {"type": "number", "description": "Drag start x"},
                "y1": {"type": "number", "description": "Drag start y"},
                "x2": {"type": "number", "description": "Drag end x"},
                "y2": {"type": "number", "description": "Drag end y"},
                "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held across the whole drag sequence.",
                },
                "hover_seconds": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 5.0,
                    "description": (
                        "Hold the mouse at the target (still pressed) for this many seconds "
                        "before releasing — for spring-loaded drops (Finder folders that "
                        "open on hover, Dock items that expand)."
                    ),
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "x1", "y1", "x2", "y2"],
        },
    ),
    types.Tool(
        name="drag_to_element",
        description=(
            "Drag from one labeled element to another — no coordinates needed. Resolves each "
            "endpoint via AX search first, OCR fallback, then drags source-center → "
            "target-center. "
            "Use whenever BOTH endpoints have visible text. Same-window drags (reorder rows, "
            "move kanban cards, drag tabs) and CROSS-APP drags (Finder file → Dock Trash, "
            "Photos image → Mail compose) both work — set `target_app` for the cross-app case. "
            "For unlabeled endpoints (slider thumb, canvas, divider) use `drag(x1, y1, x2, y2)` "
            "with explicit coords.\n"
            "\n"
            "`target_app` — when set, the target label is resolved inside that app's AX tree "
            "(klyk launches it if not running). Cross-app drags always go through the visible "
            "cursor path (SkyLight is PID-scoped), so the cursor will move during a cross-app "
            "drag regardless of session mode. The drag still works invisibly within the source "
            "app in autonomous/background mode.\n"
            "\n"
            "`hover_seconds` (default 0) holds the mouse at the target, still pressed, before "
            "releasing — for spring-loaded drops (Finder folders that open on hover, Dock items "
            "that expand). 0.8–1.5 s is typical; keep at 0 for normal drops.\n"
            "\n"
            "Response: `source`, `target`, `source_via` / `target_via` ('ax'|'ocr'), `via` "
            "(delivery path), `cross_app: true` when target_app was used. `source_index` / "
            "`target_index` (default 0) disambiguate multiple matches. `window` scopes the "
            "source-side search. Modifiers stamp across the whole drag in seamless mode; the "
            "cursor_warp fallback (used for cross-app and humanoid mode) doesn't apply them — "
            "same limitation as plain `drag`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "source_label": {
                    "type": "string",
                    "description": "Visible text on the drag source (partial, case-insensitive).",
                },
                "target_label": {
                    "type": "string",
                    "description": "Visible text on the drop target (partial, case-insensitive).",
                },
                "target_app": {
                    "type": "string",
                    "description": (
                        "Optional app for the target label, when different from the source "
                        "`app`. Use for cross-app drags (Finder → Dock Trash, Photos → Mail). "
                        "Klyk launches the target app if it isn't running."
                    ),
                },
                "source_index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Which source match to use when multiple (0-based).",
                },
                "target_index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Which target match to use when multiple (0-based).",
                },
                "hover_seconds": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 5.0,
                    "description": (
                        "Hold the mouse at the target (still pressed) for this many seconds "
                        "before releasing. Use for spring-loaded drops; 0 for normal drops."
                    ),
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "default": "left",
                },
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held across the whole drag sequence.",
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "source_label", "target_label"],
        },
    ),
    types.Tool(
        name="fill_field",
        description=(
            "Focus a field and replace its contents. Two-stage cascade — first path that "
            "succeeds wins, the chosen path is reported in `via`:\n"
            "  1. **`via:'ax_set_value'`** — Pure AX write via AXUIElementSetAttributeValue. "
            "Fully invisible: zero cursor movement, zero keyboard events, zero clipboard "
            "activity, no activation. Fires for native macOS text inputs (AXTextField, "
            "AXTextArea, AXSearchField, AXComboBox) not rooted in AXWebArea — the common case, "
            "and it stays completely in the background. Web-form inputs (Chrome, Safari, "
            "Electron) ignore the AX write because the DOM value isn't bound to the AX cache — "
            "those fall to step 2, and the response includes `ax_skip_reason` (e.g. 'web_backed').\n"
            "  2. **`via:'activated'`** — Fallback when the AX write can't apply (mostly web / "
            "Electron fields). klyk clears with Cmd+A and pastes with Cmd+V — command shortcuts "
            "macOS delivers only to the frontmost app, so klyk briefly brings the target forward "
            "first. NOT atomic: a popup opening between the focus-click and the Cmd+A would "
            "receive the Cmd+A. For already-focused fields prefer type_text (no clear). "
            "Background mode returns `{ok:false, requires_foreground:true}` whenever the AX write "
            "didn't win (the clear+paste needs the app frontmost). "
            "Safety: don't enter payment details, recipient addresses, or transfer amounts "
            "without the user's explicit OK in this session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "text": {"type": "string"},
                **_CONFIRM_DESTRUCTIVE,
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y", "text"],
        },
    ),
    types.Tool(
        name="type_text",
        description=(
            "Type text into the currently focused field. Two delivery modes — "
            "`mode` parameter chooses, default `paste`:\n"
            "\n"
            "• `paste` (default, fast) — places text on the clipboard and fires "
            "Cmd+V. ~10 ms for any text length. Handles every Unicode character "
            "(umlauts, accents, emoji, CJK) because the clipboard is byte-clean. "
            "Restores the user's prior clipboard 150 ms later. Works for nearly "
            "every text field — form inputs, terminal, address bars, code "
            "editors. **Will NOT work in keypress-driven contexts** that listen "
            "only to `keydown` events: web games (Wordle, typing-test sites), "
            "some canvas-rendered editors, vim's normal mode. If you paste and "
            "the screen doesn't change, use `mode='keys'`.\n"
            "\n"
            "• `keys` — sends real `keydown`/`keyup` events one character at a "
            "time. ~15 ms per character (so a 5-letter word is ~75 ms). "
            "Required for: web games, in-page key handlers, anywhere a paste "
            "isn't observed. Non-ASCII characters (ü, é, ñ, 中, etc.) are sent "
            "via CGEventKeyboardSetUnicodeString so they work regardless of "
            "the user's active keyboard layout. **Choose this mode whenever "
            "the target is a game or any web page where you can't be sure a "
            "paste will register.**\n"
            "\n"
            "If you omit `mode`, klyk auto-picks: `keys` on Chromium-based apps "
            "(browsers and Electron — safe for games and inputs alike), `paste` "
            "elsewhere — so you rarely need to set it. Delivery: `keys` reaches a "
            "native app FULLY INVISIBLY (no activation, no focus change), even "
            "backgrounded. `paste` uses Cmd+V, a command shortcut macOS delivers only "
            "to the frontmost app, so in autonomous mode klyk briefly activates a "
            "non-frontmost target for the paste (background returns requires_foreground); "
            "Chromium keystrokes likewise need the window frontmost. So for guaranteed "
            "invisible typing into a backgrounded native app, pass `mode='keys'`. "
            "Safety: don't type unfamiliar URLs into a browser address bar, or payment / "
            "recipient / transfer details, without the user's explicit OK in this session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "text": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["paste", "keys"],
                    "description": (
                        "Omit to auto-pick: real per-char keystrokes on Chromium "
                        "(paste is ignored by keydown-driven web UIs), fast clipboard "
                        "paste elsewhere. Set `paste` (fast, needs a Cmd+V field) or "
                        "`keys` (per-char keydown) to force one."
                    ),
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "text"],
        },
    ),
    types.Tool(
        name="press_key",
        description=(
            "Press a key or key combination. Examples: 'Return', 'Escape', 'Tab', 'Backspace', "
            "'Cmd+S', 'Cmd+Shift+Z'. Arrow keys are 'Up'/'Down'/'Left'/'Right' (the web "
            "'ArrowUp'/'ArrowLeft' names also work). 'Backspace'/'Delete' both map to delete-left; "
            "'forwarddelete'/'del' for forward-delete. "
            "Batch form: pass `keys` (ordered array, mutually exclusive with `key`) and/or "
            "`repeat` to fire a sequence with a single focus raise — e.g. "
            "`{keys:['Down','Right'], repeat:50}` fires 100 keys. Cap: 1000 total per call. An "
            "~18 ms inter-press delay is applied automatically (Chromium coalesces fast repeats). "
            "Keys route to the app's key window. Plain keystrokes reach a backgrounded native "
            "app invisibly (no activation). Two cases need the target frontmost and are handled "
            "for you: Chromium-based apps (browsers/Electron, whose renderer drops keydowns to a "
            "background window) and command-key shortcuts (Cmd+…, which macOS routes through the "
            "frontmost app's menu bar — so e.g. Cmd+A to a backgrounded app would otherwise hit "
            "whatever is in front). In both, autonomous briefly brings the target frontmost so the "
            "keys land (no humanoid needed); background mode returns requires_foreground. "
            "Pass `window`/`window_id` to raise a specific "
            "window first when driving multiple windows of the same app. A `focus_warning` in "
            "the response means the raise didn't take and keys landed in the wrong window — "
            "stop, dismiss the blocker, retry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "key": {"type": "string", "description": "Single key or combo. Mutually exclusive with `keys`."},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 1000,
                    "description": "Ordered sequence of keys to press. Mutually exclusive with `key`.",
                },
                "repeat": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 1,
                    "description": "Repeat the key/keys this many times. Total presses capped at 1000.",
                },
                **_VERIFY_PARAM,
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="hold_key",
        description=(
            "Press a key and hold it down for `duration` seconds, then release. "
            "Use when the target reacts to a key being held — game movement (W/A/S/D, arrows, "
            "Space), browser scroll-spy on Space, push-to-talk shortcuts, any app where a quick "
            "press fires once but a hold drives continuous behaviour. The keydown is re-posted "
            "every 50 ms during the hold so apps that listen for key-repeat see one. "
            "For Shift/Cmd/Option held DURING another action (Shift+click for range select, "
            "Cmd+drag for duplicate, etc.), don't use this — pass `modifiers:[...]` directly to "
            "click/double_click/scroll/drag instead; those already stamp the modifier flag for "
            "the full action invisibly. hold_key is for non-modifier keys. "
            "Routes via CGEventPostToPid — invisible for native apps regardless of session mode "
            "(no cursor move, no focus change). On Chromium-based apps (browsers and Electron) the "
            "renderer drops keydowns to a background window, so autonomous mode briefly brings it "
            "frontmost (background mode "
            "returns requires_foreground). The emergency-stop chord (Cmd+Shift+Escape) is checked "
            "every 50 ms during the hold so a long hold doesn't block escape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "key": {
                    "type": "string",
                    "description": (
                        "Key to hold. Single character ('w', 'a'), named key ('Space', 'Down', "
                        "'Return', 'F5'), or a modifier+key combo ('Shift+a', 'Cmd+Down'). Bare "
                        "modifiers (just 'Shift') are rejected — use the `modifiers` parameter "
                        "on click/scroll/drag/etc. for modifier-while-clicking flows."
                    ),
                },
                "duration": {
                    "type": "number",
                    "default": 1.0,
                    "minimum": 0.05,
                    "maximum": 10.0,
                    "description": "How long to hold the key down, in seconds.",
                },
            },
            "required": ["app", "key"],
        },
    ),
    types.Tool(
        name="press_system_key",
        description=(
            "Fire a system / media key — volume, mute, brightness, play/pause, "
            "track skip, keyboard backlight, eject. These keys live outside the "
            "regular keyboard event path (NX_SYSDEFINED, not CGEventCreateKeyboardEvent), "
            "so they need their own tool — press_key would silently fail on them. "
            "SCOPE: system-wide. volume_up here behaves identically to pressing F12 "
            "on an Apple keyboard — affects the whole OS, not the foreground app. "
            "Supported names: volume_up, volume_down, mute, brightness_up, "
            "brightness_down, play_pause, next_track, previous_track, fast_forward, "
            "rewind, eject, keyboard_brightness_up, keyboard_brightness_down, "
            "keyboard_brightness_toggle. The `app` parameter is required for "
            "session continuity (logging, timing) but doesn't route the keystroke — "
            "media keys are global."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "key": {
                    "type": "string",
                    "description": (
                        "System key name. One of: volume_up, volume_down, mute, "
                        "brightness_up, brightness_down, play_pause, next_track, "
                        "previous_track, fast_forward, rewind, eject, "
                        "keyboard_brightness_up, keyboard_brightness_down, "
                        "keyboard_brightness_toggle."
                    ),
                },
            },
            "required": ["app", "key"],
        },
    ),
    types.Tool(
        name="scroll",
        description=(
            "Scroll at window-relative position (x, y). `direction` is one of "
            "up / down / left / right; `amount` is the line count "
            "(kCGScrollEventUnitLine). "
            "Modifiers: Cmd+scroll typically zooms; Shift+scroll is horizontal in some apps.\n"
            "\n"
            "FOCUSED-CONTAINER CAVEAT (SwiftUI apps — System Settings, parts of "
            "Music / Notes / Mail): SwiftUI scroll views route wheel events by "
            "KEYBOARD FOCUS, not cursor position. If the focused element isn't "
            "where you want to scroll (`inspect` shows it via the `focused: true` "
            "flag), the scroll lands on the wrong pane — typically the sidebar. "
            "Fix before scrolling: `click_element` or `click` any visible row in "
            "the target pane to shift focus there, then scroll. AppKit apps "
            "(Finder, Safari, Mail proper) route by cursor position and aren't "
            "affected.\n"
            "\n"
            "SEAMLESS MODE (background / autonomous): scroll wheel event routes through "
            "SkyLight to the target PID — cursor doesn't move, target window isn't raised, and "
            "the app is never activated (macOS scrolls the window under the pointer without "
            "bringing it forward). Fully invisible whether or not the target is frontmost — ideal "
            "for scrolling a background app behind the user's foreground work. Response carries "
            "`via:'skylight'`. In humanoid mode response carries `via:'cursor_warp'` and the "
            "cursor warps to (x, y) before the wheel event fires."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "default": 3},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "alt", "ctrl"]},
                    "description": "Modifier keys held during scroll. Cmd=zoom, Shift=horizontal in many apps.",
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y", "direction"],
        },
    ),
    types.Tool(
        name="move_cursor",
        description=(
            "Move the real cursor to window-relative (x, y) without clicking. Triggers hover "
            "states (tooltips, on-hover UI reveals, dropdown previews). "
            "`dwell_seconds` (default 0) holds the cursor there before returning — set when "
            "the hover effect takes time to render (lazy tooltips, animated reveals) or when "
            "a follow-up screenshot must capture the hovered state. "
            "Always uses the visible path regardless of session mode — invisible hovering "
            "isn't possible, hover is a cursor-position effect by definition. To click without "
            "moving the cursor, use `click` in autonomous/background mode (SkyLight delivery)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
                "dwell_seconds": {
                    "type": "number",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 10.0,
                    "description": (
                        "Hold the cursor at the target point for this many seconds before "
                        "returning. Use when a hover effect needs time to render before the "
                        "next action (e.g. screenshot of the revealed tooltip)."
                    ),
                },
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="wait",
        description=(
            "LAST RESORT. Sleep for a fixed number of seconds. Use only when the operation has a "
            "genuinely known duration with no observable UI signal (e.g. a file export with no "
            "progress indicator, a post-destructive propagation delay). "
            "Never use wait as a guess for an uncertain duration — that's what wait_for is for. "
            "Most UI responses complete in under 100ms; never add a wait after a plain click, "
            "scroll, or keypress.\n"
            "\n"
            "**Hard rule: `seconds > 2` is almost always wrong.** Blind sleeps that long usually "
            "mean you don't know the readiness signal — find one (AX text → wait_for; pixel change "
            "→ wait_for_visual; nothing observable → wait(0.5) + inspect inside a run, repeat). "
            "Logs show single 30 s waits that the underlying UI satisfied in 200 ms — pure dead "
            "time you pay every call.\n"
            "\n"
            "app is optional — wait is a time delay, not app-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "seconds": {"type": "number", "description": "How long to wait. Values above 0.5 are rarely justified — most UI transitions are under 0.1s. >2 is almost always a misdiagnosis; prefer wait_for or split-poll."},
            },
            "required": ["seconds"],
        },
    ),
    types.Tool(
        name="wait_for",
        description=(
            "Wait until specific text appears in the UI's accessibility tree, then return the "
            "matching element. Polls every 0.1 s and exits the moment the condition is met — "
            "faster than a fixed wait when the readiness signal is real AX text.\n"
            "\n"
            "**HARD PRECONDITION — the readiness signal MUST be visible AX text** (a label, "
            "value, or title that the OS accessibility framework exposes). DO NOT call wait_for "
            "on any of these:\n"
            "  • Web content inside a browser (Chrome, Safari, Edge). Browser web AX is lazy "
            "and incomplete; this is the canonical misuse and burns the full timeout for "
            "nothing.\n"
            "  • Canvas-rendered text (web games, code editors with canvas-only rendering, "
            "Wordle-style games — the cells aren't AX text).\n"
            "  • OCR-only labels (anything that doesn't appear as AXValue/AXLabel/AXTitle).\n"
            "  • Non-text readiness signals (spinner disappearing, button enabling, color "
            "change, animation finishing). For those use a `wait(seconds)` plus `inspect` or "
            "`read_grid` in a `run`.\n"
            "\n"
            "Speculative use is a footgun: if the text never appears, the call sits on the "
            "full timeout before failing — often slower than the naïve fixed wait it was "
            "meant to replace. Default timeout is 4 s for this reason (was 10 s; lowered "
            "after a real session lost 8 s to a single speculative call on web AX). Only "
            "raise the timeout when you're certain the text will appear within the window."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "text": {
                    "type": "string",
                    "description": "Text to wait for (partial match, case-insensitive).",
                },
                "timeout": {
                    "type": "number",
                    "default": 4,
                    "description": (
                        "Max seconds to wait (default 4, max 30). Keep low unless you have "
                        "strong evidence the AX text will appear — failed waits sit on the "
                        "full timeout."
                    ),
                },
            },
            "required": ["app", "text"],
        },
    ),
    types.Tool(
        name="get_logs",
        description=(
            "Return captured app logs (stderr). "
            "Call after major interactions to check for silent errors or crashes."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="read_element",
        description=(
            "Read the accessibility value of the UI element at (x, y). "
            "Use to verify field content after typing, or to read a label programmatically. "
            "Retries up to 4× to handle SwiftUI @State propagation delay. "
            "Returns {value, found, status}: status='ok' (value read), "
            "'no_value' (element has no AXValue — don't keep polling, try a "
            "different verification path), 'no_element' (no AX element at the "
            "coord — coordinate may be wrong, or AX is unavailable on this "
            "surface)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="get_pixel",
        description=(
            "LAST RESORT for a SINGLE-POINT color check. Returns `{r, g, b, hex}` at one "
            "window-relative pixel. ~5 ms.\n"
            "\n"
            "Use only when the target is truly one pixel with no glyph on top (status light, "
            "indicator dot). For:\n"
            "  • Any regular grid (Wordle, sudoku, heatmap, LED matrix) → use `read_grid` — "
            "sampling a cell's center hits the GLYPH not the fill, returning the wrong color.\n"
            "  • Multiple non-grid points/regions → use `get_pixels` (pays off from ~3 samples).\n"
            "\n"
            "Reads the target window's own pixels (z-order independent). Deterministic CG "
            "buffer read — no compression artefacts. Window-relative coords match screenshot."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number", "description": "Window-relative x"},
                "y": {"type": "number", "description": "Window-relative y"},
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="get_pixels",
        description=(
            "Batch pixel read from one window capture. For a REGULAR GRID use `read_grid` "
            "instead — it computes cell rects and returns AX text + color in one call. Reach "
            "for get_pixels when targets aren't gridded (scattered indicators, hand-placed "
            "swatches).\n"
            "\n"
            "Two modes (combinable):\n"
            "  • `points`: exact 1×1 sample at each (x, y) → `{pixels:[{x,y,r,g,b,hex}...]}`.\n"
            "  • `regions`: per-channel median over rect (x, y, width, height). Median ignores "
            "the minority of pixels covered by a centered glyph → returns the surrounding fill, "
            "no glyph-dodge offset needed. → `{regions:[{x,y,width,height,r,g,b,hex}...]}`.\n"
            "\n"
            "One capture (~40 ms) regardless of N — pays off from ~3 samples. Z-order "
            "independent, window-relative. Bounds-validated before capture."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "points": {
                    "type": "array",
                    "description": "List of window-relative (x, y) points for exact 1×1 sampling.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["x", "y"],
                    },
                },
                "regions": {
                    "type": "array",
                    "description": (
                        "List of window-relative rects {x, y, width, height} for "
                        "median sampling. Each rect should bound a single cell (e.g. "
                        "one Wordle tile, one calendar day, one LED). Returns the "
                        "median pixel color inside — robust against letter glyphs or "
                        "icons centered in the cell."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                        },
                        "required": ["x", "y", "width", "height"],
                    },
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="read_grid",
        description=(
            "**Default tool for any grid-shaped UI.** Returns text (AX) AND fill color (median "
            "over a 60%-inset rect — robust against the centered letter glyph that defeats "
            "single-pixel sampling) for every cell in one call. No agent-side image "
            "interpretation, no chain of get_pixel calls.\n"
            "\n"
            "Use for: Wordle-style word games, sudoku/crossword/minesweeper, spreadsheets and "
            "tables, calendar heatmaps, status-indicator grids, LED/equalizer matrices — "
            "anything answering 'what's in cell (r, c) and what state is it in?'. Sampling "
            "one pixel at a cell's center hits the GLYPH, not the fill; this tool exists "
            "specifically to dodge that trap.\n"
            "\n"
            "Geometry: window-local top-left (`x`, `y`), per-cell size, row/col counts, "
            "optional `cell_gap` for gutters. Determine once; cell geometry rarely changes "
            "mid-game.\n"
            "\n"
            "Output: `{ok, rows, cols, cells: [[{row, col, x, y, text, r, g, b, hex}, ...]]}`. "
            "`text` is null if AX exposes none. Note the two sources differ on occlusion: "
            "color is sampled from the target window's own image (correct even if covered), "
            "while `text` reads the frontmost element at that point — so if another window "
            "overlaps the grid, colors stay right but `text` may reflect the overlay. Keep the "
            "grid unobstructed. Window-local coords match click/fill_field. "
            "Latency: ~150 ms for a 30-cell Wordle grid (one capture + batched AX)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "rows": {"type": "integer", "minimum": 1, "maximum": 200,
                         "description": "Number of rows in the grid."},
                "cols": {"type": "integer", "minimum": 1, "maximum": 200,
                         "description": "Number of columns in the grid."},
                "x": {"type": "number",
                      "description": "Window-local x of the grid's top-left corner."},
                "y": {"type": "number",
                      "description": "Window-local y of the grid's top-left corner."},
                "cell_width": {"type": "number", "minimum": 1,
                               "description": "Width of one cell in pixels."},
                "cell_height": {"type": "number", "minimum": 1,
                                "description": "Height of one cell in pixels."},
                "cell_gap": {
                    "type": "number", "default": 0,
                    "description": (
                        "Pixels between adjacent cells (default 0). Use to "
                        "account for tile gutters."
                    ),
                },
            },
            "required": ["app", "rows", "cols", "x", "y", "cell_width", "cell_height"],
        },
    ),
    types.Tool(
        name="set_clipboard",
        description=(
            "Write to the system clipboard. Pass exactly one of text or image_path. "
            "image_path loads a PNG file as image data — use before Cmd+V to paste a picture "
            "into a chat input, attachment field, or document."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "text": {"type": "string", "description": "Text content to copy."},
                "image_path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-relative path to a PNG file. Loaded as image data on the "
                        "clipboard so the next Cmd+V pastes the image, not the path string."
                    ),
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="get_clipboard",
        description=(
            "Read the current contents of the system clipboard. "
            "Use after pressing Cmd+C in any app to capture the copied text."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="click_menu",
        description=(
            "Click a macOS menu-bar item by path, e.g. path=['Tools','Annotate','Arrow']. "
            "For menu-bar menus only (File, Edit, View, Tools, Window, …) — NOT for in-window "
            "buttons, popups, or context menus, which use click_element. The path must include "
            "the top-level menu plus every intermediate submenu down to the leaf item; "
            "labels are matched exactly (case-sensitive, ellipsis as '…')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "path": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "description": "Menu path from top-level menu to leaf item.",
                },
            },
            "required": ["app", "path"],
        },
    ),
    types.Tool(
        name="context_menu_select",
        description=(
            "Right-click at (x, y) to open the in-window context menu, then select the item "
            "whose label matches. One round-trip instead of three (click-right → inspect → "
            "click_element).\n"
            "\n"
            "Precedence: macOS menu-bar items (File / Edit / View) → `click_menu`. In-window "
            "context menus (right-click anywhere in the document, list, sidebar) → this tool. "
            "Regular labeled buttons (not behind a right-click) → `click_element`.\n"
            "\n"
            "Resolution: activate the app, right-click, poll the AX tree up to 2 s for an "
            "AXMenuItem matching `item_label` (case-insensitive partial), then click it. On "
            "an AX miss the menu is dismissed (Escape) and an error with a `hint` is "
            "returned — there is no OCR fallback (a native menu is a separate surface klyk "
            "captures z-order-independently, so OCR can't see it; in-window menus are in the "
            "AX tree).\n"
            "\n"
            "Multi-window note: this tool does NOT auto-raise the target window — AX raising "
            "an already-frontmost window can close a context menu Finder is about to open. "
            "When same-app windows overlap at the right-click point, call `focus_window` "
            "first to make sure the click lands on the intended window. "
            "Response: `via` ('ax'), `matched_item` with role + label, `wait_ms` for "
            "the menu to surface."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "number", "description": "Window-relative x of the right-click."},
                "y": {"type": "number", "description": "Window-relative y of the right-click."},
                "item_label": {
                    "type": "string",
                    "description": "Visible text of the menu item to select (partial, case-insensitive).",
                },
                "item_index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Which match to pick when the label has multiple hits (0-based).",
                },
                "timeout": {
                    "type": "number",
                    "default": 2.0,
                    "minimum": 0.2,
                    "maximum": 10.0,
                    "description": "Seconds to wait for the menu to appear before giving up.",
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "x", "y", "item_label"],
        },
    ),
    types.Tool(
        name="set_window_bounds",
        description=(
            "Move (and optionally resize) a window of an app. Without window/window_id, acts on "
            "the frontmost window — common case. Pass `window` (A/B/C label from list_windows) or "
            "`window_id` (raw CG ID) to position a specific window when the app has multiple, "
            "even if it isn't currently frontmost — use this for tiling Chrome windows across "
            "screen quadrants. "
            "Coordinates are screen-space, origin top-left. Width/height optional — omit both "
            "to move without resizing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {"type": "integer", "description": "Screen x for window top-left."},
                "y": {"type": "integer", "description": "Screen y for window top-left."},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["app", "x", "y"],
        },
    ),
    types.Tool(
        name="verdict",
        description=(
            "Gather all test evidence for your final PASS or FAIL judgment. "
            "Returns the final screenshot, all captured logs, and grading criteria. "
            "Call only when your complete test flow is finished — this ends the test. "
            "You synthesize the result from the evidence: did the feature work, any errors in logs, "
            "and overall UI quality combined."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "test_description": {
                    "type": "string",
                    "description": "Plain-English summary of what you tested.",
                },
                "grade": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include UI grading criteria alongside the evidence.",
                },
            },
            "required": ["app", "test_description"],
        },
    ),
    types.Tool(
        name="handle_system_dialog",
        description=(
            "Interact with a macOS system dialog (NSSavePanel, NSOpenPanel, alert) "
            "that appeared outside the main app window. "
            "Actions: 'save', 'open' (with optional `path`), 'cancel'. For 'save' "
            "with a `path`, klyk sets the filename then navigates to the folder "
            "(via Go To Folder) and confirms — then VERIFIES the file exists at "
            "`path` and returns `saved: true/false` (with `ok:false` + error if it "
            "didn't land, e.g. the panel kept an autosave default or overrode the "
            "extension). It brings the dialog frontmost first and fails loudly if "
            "it can't (rather than typing into the wrong app)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "action": {"type": "string", "enum": ["save", "open", "cancel"]},
                "path": {"type": "string", "description": "File path for save/open (optional)."},
            },
            "required": ["app", "action"],
        },
    ),
    types.Tool(
        name="close_app",
        description=(
            "End the testing session for a single app and clean up. Call when done testing. "
            "For closing several apps at once (typical at end-of-test cleanup), prefer close_apps "
            "to save round-trips."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="close_apps",
        description=(
            "End testing sessions for multiple apps in one call. Use at end-of-test cleanup "
            "instead of calling close_app repeatedly. "
            "Returns per-app status — apps with no active session are reported as was_open=false, "
            "never an error. The call never fails as a whole; partial closes still return ok."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "apps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of app display names to close.",
                },
            },
            "required": ["apps"],
        },
    ),
    types.Tool(
        name="resume",
        description=(
            "Reports emergency-stop status. NOTE: an emergency stop (Cmd+Shift+Escape) "
            "can be cleared ONLY by the user physically pressing Cmd+Shift+Escape again "
            "— this tool CANNOT clear it. If a stop is active, tell the user to press the "
            "chord to resume; do not attempt to resume on their behalf."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="list_sessions",
        description=(
            "List all active app sessions. "
            "Use to check which apps are currently being tested before calling close_app or starting a new session."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_escalation_log",
        description=(
            "Return the autonomous-mode foreground-escalation log for a session. "
            "Each entry records a moment when klyk's invisible (SkyLight) path "
            "could not deliver and the autonomous-mode policy escalated to "
            "cursor-warp instead — capturing the cursor for a fraction of a second. "
            "Call this on user return after a long autonomous run, or when the user "
            "asks 'what did you do that needed my cursor?'. Returns an array of "
            "{tool, x, y, reason, ts} entries (UNIX timestamp). Capped at 500 "
            "entries per session; oldest dropped FIFO."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="set_mode",
        description=(
            "Switch the session's input-delivery policy. **You almost never need to call this** — "
            "every new session starts in 'autonomous', which is right for nearly every workflow "
            "('play this game', 'fill this form', 'work in background while I do X'). Only call "
            "set_mode when the user explicitly asks for different behavior.\n"
            "\n"
            "Decision tree (stop at first match):\n"
            "  1. 'never take focus, bail if you have to' → `background`\n"
            "  2. 'show me each click, move the cursor, humanoid' → `humanoid`\n"
            "  3. otherwise → leave default (`autonomous`)\n"
            "\n"
            "Modes:\n"
            "  • `autonomous` (DEFAULT) — invisible via SkyLight; briefly activates the target "
            "    only when SkyLight can't deliver (e.g. Chromium web). Escalations logged.\n"
            "  • `background` — invisible-first but BAILS instead of activating. Returns "
            "    `{ok:false, requires_foreground:true}` when SkyLight can't deliver.\n"
            "  • `humanoid` — visible cursor moves, target app comes to front. Use only when "
            "    the user wants to watch each action.\n"
            "\n"
            "Session-scoped; returns the previous mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "mode": {
                    "type": "string",
                    "enum": ["humanoid", "background", "autonomous"],
                    "description": "The mode to switch this session into.",
                },
            },
            "required": ["app", "mode"],
        },
    ),
    types.Tool(
        name="list_windows",
        description=(
            "Enumerate all on-screen windows of an app and assign each a stable A/B/C label. Call "
            "this BEFORE driving multiple windows of the same app (e.g. two Chrome windows tiled "
            "side-by-side) so the labels are registered. Each entry: "
            "{window: 'A'|'B'|..., window_id, owner_name, x, y, width, height}. "
            "Use the 'window' label in subsequent calls (screenshot, click, press_key, "
            "set_window_bounds, focus_window, run) — it's stable for the window's lifetime even as "
            "z-order changes. If you only need the largest window (the common case), skip this "
            "and just use the app's default session — tools without 'window' or 'window_id' "
            "always act on the app's frontmost window."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="focus_window",
        description=(
            "Bring a specific window (by 'window' label from list_windows) to front and make it "
            "the key window. Required before sending keyboard input that must land in a specific "
            "window — keys route to whichever window of the app is currently key. "
            "Most tools (screenshot, click, press_key, run) accept the 'window' label directly and "
            "call focus_window internally as needed; use this tool only when you want to raise a "
            "window without performing any other action (e.g. user-visible window switch). "
            "Response shape: {ok, window_id, via, focused, warning?}. `focused=true` confirms the "
            "target is now the key window — keys/clicks will land there. `focused=false` means the "
            "raise didn't take (typically a modal in another window of the same app is holding "
            "focus); the `warning` field explains what to do."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="screen_info",
        description=(
            "Get main display dimensions and all attached displays in screen-space coordinates. "
            "Returns {main: {x,y,width,height,display_id}, displays: [{index, display_id, x, y, "
            "width, height, is_main}, ...], scale}. The `index` is a stable 0-based ordinal "
            "(displays[0], displays[1], …) — pass it to `screenshot(display=N)` to capture an "
            "entire display rather than the app's window. Use the geometry to compute window "
            "placements (e.g. divide main display into quadrants for tiling) without hardcoding "
            "screen sizes. All coordinates are in logical points and match the coordinate space "
            "of set_window_bounds."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="take_control",
        description=(
            "Make THIS session the active klyk driver. Only one session drives "
            "the Mac at a time (two would interleave clicks/keystrokes and "
            "corrupt the target app). On startup a session takes control only if "
            "it's free (the previous driver has exited) — it will NOT auto-steal "
            "from another session that's alive and actively driving, so control "
            "never thrashes between coexisting/respawned instances. Call this "
            "ONLY when the user explicitly wants THIS session to take over from "
            "another live one (e.g. they say to use klyk here). Do NOT call it "
            "reflexively just because a "
            "control action returned blocked:'not_active_session' — if both "
            "sessions auto-reclaimed on every block they would fight over "
            "control endlessly. On a block, tell the user klyk is busy in "
            "another session and let them choose which one drives. Reads and "
            "screenshots are never blocked."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="select_option",
        description=(
            "Select an option from a native dropdown, popup button, or combobox. "
            "Clicks the control at (x, y) to open it, then selects the option by name. "
            "Use for NSPopupButton, NSComboBox, and native macOS option controls. "
            "For web dropdowns, use Playwright MCP select_option instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x": {"type": "number", "description": "X coordinate of the control"},
                "y": {"type": "number", "description": "Y coordinate of the control"},
                "option": {"type": "string", "description": "Exact text of the option to select"},
            },
            "required": ["app", "x", "y", "option"],
        },
    ),
    types.Tool(
        name="ax_snapshot",
        description=(
            "PREFER THIS as your default first look — pure AX, no image, the cheapest and most "
            "current way to see an app. Returns all labeled and interactive UI elements in the "
            "app window as a flat list. "
            "Each element includes role, label (if any), value (if any), center coordinates (x, y), "
            "and `focused: true` on the one element currently holding keyboard focus (when any). "
            "Use to inspect the full UI structure without a screenshot, verify element state "
            "programmatically, or locate controls by label before clicking. "
            "Covers all windows including floating menus and sheets. "
            "On browsers, returns the full document tree (not just the visible viewport) — "
            "use this to answer 'is X anywhere on this page?' without scrolling and re-screenshotting. "
            "Reach for `inspect` (adds the image) or `screenshot` only when AX is thin or empty "
            "(Electron/web/canvas) or the question is genuinely visual (layout, rendering, color)."
        ),
        inputSchema={
            "type": "object",
            "properties": _APP_PARAM,
            "required": ["app"],
        },
    ),
    types.Tool(
        name="read_text",
        description=(
            "Extract visible text from the app window using on-device OCR (Apple Vision). "
            "Use when text is rendered as pixels and not exposed via AX — video captions, "
            "canvas-rendered editors, in-game text, PDFs in a viewer, image-only screenshots "
            "inside the app, anywhere `ax_snapshot` returns nothing useful.\n"
            "\n"
            "Precedence: prefer `ax_snapshot` or `inspect` first — AX is faster (~30 ms vs "
            "~50-150 ms) and returns roles, not just text. Reach for read_text only when AX "
            "is empty for the surface you care about.\n"
            "\n"
            "Optional `x, y, width, height` restricts results to a window-relative rect (the "
            "full window is still OCRed; observations whose center falls outside are filtered "
            "out). Optional `query` narrows results to text containing that substring "
            "(case-insensitive). `level`: 'fast' (default, ~50 ms) or 'accurate' (~150 ms, "
            "catches small/stylized text fast mode misses). "
            "`languages` (BCP-47 list like ['de-DE', 'en-US'] or ['zh-Hans']) — omit to inherit "
            "the macOS system preferred-language list, which already handles a German / French "
            "/ Japanese Mac transparently. Set explicitly only when you need to recognize text "
            "in a language the host system isn't configured for.\n"
            "\n"
            "Returns `{ok, count, observations: [{text, x, y, width, height, confidence}, ...], "
            "full_text}`. Coordinates are window-relative — ready to pass to click/fill_field. "
            "`full_text` concatenates observations in reading order (top→bottom, left→right) for "
            "fast scanning. `via:'ocr'`. Z-order independent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "x": {
                    "type": "number",
                    "description": "Optional left edge of the region to extract (window-relative).",
                },
                "y": {
                    "type": "number",
                    "description": "Optional top edge of the region to extract (window-relative).",
                },
                "width": {
                    "type": "number",
                    "minimum": 1,
                    "description": "Optional width of the region in pixels.",
                },
                "height": {
                    "type": "number",
                    "minimum": 1,
                    "description": "Optional height of the region in pixels.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter. When set, only "
                        "observations whose text contains this substring are returned."
                    ),
                },
                "level": {
                    "type": "string",
                    "enum": ["fast", "accurate"],
                    "default": "fast",
                    "description": (
                        "'fast' (default) is ~3-5× quicker on Apple Silicon and adequate for "
                        "crisp UI text. 'accurate' catches small, low-contrast, or stylized "
                        "text that fast mode misses."
                    ),
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional BCP-47 language codes (e.g. ['de-DE', 'en-US'], "
                        "['zh-Hans'], ['ja-JP']) for Vision to recognize. Omit to use the "
                        "macOS system preferred languages — a non-English Mac just works. "
                        "Set explicitly only when you need a language the system isn't "
                        "configured for, or to constrain recognition to a narrow subset."
                    ),
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="run",
        description=(
            "Execute a sequence of actions in one MCP call. Default workflow: (1) `inspect` to "
            "observe, (2) `run` everything you can predict — INCLUDING the verification "
            "`inspect`/`read_grid` as the final action (not a separate call), (3) reason, "
            "repeat.\n"
            "\n"
            "**ALWAYS reach for `run` when you recognize these patterns** — each one costs "
            "2-3 MCP round-trips and 5-10 s of reasoning per skipped batch:\n"
            "  • `type_text → press_key(\"Return\") → inspect` (form submit, search bar, "
            "Wordle-style guesses) — collapse to one `run`.\n"
            "  • `click_element → inspect` (any 'click then see what happened') — collapse, "
            "or use the action's `verify=true` flag for a cheap focused-state probe instead.\n"
            "  • `click → wait → inspect` after opening a menu, dropdown, or modal.\n"
            "  • Any sequence of N keystrokes — use `press_key`'s `keys[]`+`repeat` or one "
            "`type_text` instead of N separate calls.\n"
            "\n"
            "**The run response includes every inner action's result** in `results[].result`. "
            "After a run ending with `inspect`/`read_grid`/`screenshot`, do NOT re-call that "
            "tool standalone — its payload is already there. Re-calling burns a round trip.\n"
            "\n"
            "Batch only when intermediate state is predictable. Unexpected popups/redirects/"
            "autocompletes are not — split into two runs when in doubt. Each action takes the "
            "same params as its standalone tool; `app` is inherited. Waits should be rare; "
            "prefer `wait_for` inside the run when an AX signal exists. "
            "Multi-window: action-level or run-level `window`/`window_id` targets a specific "
            "window; the top-level cascades unless overridden. A `focus_warnings` array on the "
            "response means those steps landed in the wrong window — stop and resolve.\n"
            "\n"
            'Example: `{"app":"X","actions":[{"tool":"click","x":300,"y":200},'
            '{"tool":"fill_field","x":300,"y":200,"text":"hello"},{"tool":"inspect"}]}`'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"tool": {"type": "string"}},
                        "required": ["tool"],
                        "additionalProperties": True,
                    },
                    "description": (
                        "Sequence of actions. Each object has 'tool' plus that tool's params. "
                        "An action may include 'window_id' to override the run's default window "
                        "for that one step."
                    ),
                },
            },
            "required": ["app", "actions"],
        },
    ),
    types.Tool(
        name="click_element",
        description=(
            "Find a UI element by label and click it — no coordinates needed. Prefer this over "
            "`click(x, y)` whenever the target has visible text. Tries the AX tree first "
            "(case-insensitive partial match), falls back to on-device OCR so it works on "
            "canvas, browser content, and Electron. For menu-bar items use `click_menu`. "
            "`index` picks among multiple matches (default 0). `window` scopes the search to "
            "one window of a multi-window app. Response includes `via` so you can tell which "
            "tier hit: `ax_action` (AXPress/AXOpen at element or parent level), "
            "`ax_match+skylight` (AX-found target, SkyLight-delivered click), `ocr+skylight`, "
            "or `cursor_warp` (humanoid mode). Background mode returns "
            "`{ok:false, requires_foreground:true}` when the app isn't frontmost — never warps. "
            "On a miss, the error carries `visible_text_candidates` — the closest on-screen "
            "text with window-relative `x`/`y` — so retry with the exact spelling shown, or "
            "`click(x, y)` at those coordinates, rather than guessing again. "
            "Safety: don't click a label that opens an unfamiliar URL or fires a money-moving "
            "action (Send, Buy, Confirm Transfer, Place Order, Sign) without the user's "
            "explicit OK in this session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                **_WINDOW_ID_PARAM,
                "label": {
                    "type": "string",
                    "description": "Text label to search for (partial match, case-insensitive).",
                },
                "index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Which match to click if multiple elements match (0-based).",
                },
                **_VERIFY_PARAM,
            },
            "required": ["app", "label"],
        },
    ),
    types.Tool(
        name="get_template",
        description=(
            "Crop a region from the app's current screenshot and return it as a base64 PNG "
            "template. Use when the target has no visible text and is not in the accessibility "
            "tree — e.g. an icon-only button on a canvas surface (Figma, Sketch), a custom "
            "graphic in a web app, or an Electron control rendered without a11y. "
            "Returns a short `template_id` (server-cached, preferred — safe to pass to "
            "find_template without LLM transcription risk). Pass `include_b64=true` to also "
            "receive the raw `template_b64` (typically 5–50 KB) — default is false so common "
            "use stays lean; only set true when you actually need the raw bytes (saving to "
            "disk, sending to another tool). "
            "Crop tightly — include the icon itself with only a few pixels of padding."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "x1": {"type": "integer", "description": "Left edge of crop region (window-relative)"},
                "y1": {"type": "integer", "description": "Top edge of crop region (window-relative)"},
                "x2": {"type": "integer", "description": "Right edge of crop region (window-relative)"},
                "y2": {"type": "integer", "description": "Bottom edge of crop region (window-relative)"},
                "include_b64": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, response also includes raw template_b64. Default false to save tokens.",
                },
            },
            "required": ["app", "x1", "y1", "x2", "y2"],
        },
    ),
    types.Tool(
        name="find_template",
        description=(
            "Find a template image (from get_template) in the app's current screenshot using "
            "pixel-accurate normalized cross-correlation. Takes a fresh screenshot internally, "
            "so it correctly handles scroll drift — if the page scrolled since get_template was "
            "called, the returned coordinates reflect the element's current position. "
            "Returns {x, y, confidence} where x/y are window-relative click coordinates for the "
            "center of the match, ready to pass to click(). "
            "Prefer template_id (short, server-cached) over template_b64 (raw PNG bytes) — "
            "passing one or the other is required. "
            "Use search_region to restrict the search when the same template could appear in "
            "multiple places (e.g. like buttons on multiple comments)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "template_id": {
                    "type": "string",
                    "description": "Server-cached template handle from get_template (preferred).",
                },
                "template_b64": {
                    "type": "string",
                    "description": "Base64 PNG template from get_template (use when template_id is unavailable).",
                },
                "threshold": {
                    "type": "number",
                    "description": (
                        "Minimum confidence 0–1 (default 0.8). 0.95 for exact matches, "
                        "0.75–0.85 for elements with slight rendering variation."
                    ),
                    "default": 0.8,
                },
                "search_region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": (
                        "Optional [x1, y1, x2, y2] to restrict the search area and avoid "
                        "false matches when the template could appear in multiple places."
                    ),
                },
            },
            "required": ["app"],
        },
    ),
    types.Tool(
        name="wait_for_visual",
        description=(
            "Wait until a template image appears in (or disappears from) the app's screen. "
            "Use when the readiness signal is visual and not in AX — spinners, toasts, "
            "animations, canvas renders, color/state changes. "
            "Precedence: `wait_for` (AX text) → `wait_for_visual` (pixel/template) → "
            "`find_template` (one-shot 'is it there now'). "
            "`present=true` (default) waits for appearance; `false` for disappearance. Requires "
            "`template_id` (preferred) or `template_b64` from get_template. Do not call "
            "speculatively — it sits on its full timeout (default 10 s) before failing. "
            "Returns `{found, x, y, confidence, elapsed, polls}` on match; "
            "`{ok:false, timeout:true, ...}` on timeout."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_APP_PARAM,
                "template_id": {
                    "type": "string",
                    "description": "Server-cached template handle from get_template (preferred).",
                },
                "template_b64": {
                    "type": "string",
                    "description": "Base64 PNG template from get_template (use when template_id is unavailable).",
                },
                "present": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "True (default): wait until the template appears. "
                        "False: wait until it disappears (spinner, toast, modal dismissed)."
                    ),
                },
                "threshold": {
                    "type": "number",
                    "default": 0.8,
                    "description": "Minimum match confidence 0–1 (default 0.8).",
                },
                "timeout": {
                    "type": "number",
                    "default": 10,
                    "description": "Max seconds to wait (default 10, max 30).",
                },
                "poll_interval": {
                    "type": "number",
                    "default": 0.5,
                    "description": (
                        "Sleep between polls in seconds (default 0.5, min 0.1). "
                        "NOTE: this is added on top of per-poll work (screenshot ~300ms + match "
                        "~30ms), so effective cycle is roughly poll_interval + 0.3s. Values below "
                        "0.1 give diminishing returns — the screenshot+match floor sets the real "
                        "minimum cycle, not this parameter."
                    ),
                },
                "search_region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "Optional [x1, y1, x2, y2] to restrict the search region.",
                },
            },
            "required": ["app"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_session(args: dict, tool_name: str | None = None):
    """
    Resolve the session for the requested app, launching if needed.

    `tool_name` is passed by the dispatch loop; when it names an action
    the user should see in the menu-bar surface, the activity recorder is
    fed here so all action-handler sites pick up instrumentation by adding
    a single argument to their call.
    """
    session, is_new = await get_or_create_session(
        args["app"],
        target=args.get("target"),
        bundle_id=args.get("bundle_id"),
        app_path=args.get("app_path"),
    )
    if tool_name and tool_name in activity.ACTION_TOOLS:
        # Best-effort instrumentation; record_from_args swallows internally
        # but we guard once more so a bug in activity.py never breaks tool
        # dispatch (independent failure surfaces).
        try:
            activity.record_from_args(session, tool_name, args)
        except Exception:
            pass
    return session, is_new


def _resolve_window(args: dict, app: str) -> int | None:
    """
    Accept either `window` (A/B/C label) or `window_id` (raw CG ID).
    Returns the numeric window_id, or None if neither was supplied.
    Raises RuntimeError with a clear, actionable message on an unknown label.
    """
    label = args.get("window")
    raw_id = args.get("window_id")
    if label is not None:
        wid = window_labels.resolve(app, str(label))
        if wid is None:
            known = list(window_labels._by_app.get(app, {}).values())
            raise RuntimeError(
                f"Window label '{label}' not registered for app '{app}'. "
                f"Known labels: {sorted(known) or '(none — call list_windows first)'}. "
                "Call list_windows to assign labels to the app's current windows."
            )
        return wid
    if raw_id is not None:
        return int(raw_id)
    return None


async def _refresh_window(session, window_id: int | None = None) -> None:
    """
    Refresh session bounds. If window_id is given, target that specific window;
    otherwise fall back to the app's largest on-screen window.
    """
    if window_id is not None:
        win = await asyncio.get_event_loop().run_in_executor(
            None, lambda: capture.get_window_by_id(int(window_id))
        )
        if not win:
            raise RuntimeError(
                f"Window {window_id} not found on screen. It may have been closed, "
                "minimized, or moved to another Space. Call list_windows to refresh."
            )
        if win["pid"] != session.pid:
            raise RuntimeError(
                f"Window {window_id} belongs to pid {win['pid']}, not '{session.app}' "
                f"(pid {session.pid}). Window ID likely went stale across a relaunch."
            )
        session.window_id = win["window_id"]
        session.win_x = win["x"]
        session.win_y = win["y"]
        session.width = win["width"]
        session.height = win["height"]
        return

    win = await asyncio.get_event_loop().run_in_executor(
        None, lambda: capture.get_window_for_pid(session.pid)
    )
    if not win:
        raise RuntimeError(
            f"No visible window found for '{session.app}' (pid {session.pid}). "
            "The app may have quit, crashed, or been minimized. "
            "Call screenshot() again to re-launch, or close_app() to reset the session."
        )
    session.window_id = win["window_id"]
    session.win_x = int(win["bounds"]["X"])
    session.win_y = int(win["bounds"]["Y"])
    session.width = int(win["bounds"]["Width"])
    session.height = int(win["bounds"]["Height"])


async def _focus_if_needed(session, window_id: int | None) -> dict | None:
    """
    Raise the target window via AX before an action that needs it as key window.
    Returns the raise_window status dict, or None when window_id is None.

    When the returned dict has focused=False, callers MUST surface the warning
    to the agent — otherwise keystrokes/clicks may silently land in the wrong
    window of the same app.
    """
    if window_id is None:
        return None
    if session.mode == "background":
        # Background contract: never activate / steal focus. raise_window would
        # call activate_app, so instead check whether the target window is
        # already key. If it is, input lands correctly — proceed. If not, signal
        # requires_foreground so the caller bails rather than foregrounding the
        # app or posting input to the wrong window.
        already_key = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.is_window_key(session.pid, int(window_id))
        )
        if already_key:
            return {"ok": True, "window_id": int(window_id), "via": "already_key", "focused": True}
        return {
            "ok": False,
            "window_id": int(window_id),
            "via": "background_no_activate",
            "focused": False,
            "requires_foreground": True,
            "warning": (
                f"Background mode won't activate {session.app} to make this window key. "
                "Bring it forward yourself, or switch to mode='autonomous'."
            ),
        }
    try:
        return await computer.raise_window(session.pid, int(window_id))
    except Exception as e:
        log.warning(f"raise_window({window_id}) failed: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "window_id": int(window_id),
            "via": "exception",
            "focused": False,
            "warning": f"raise_window failed: {e}",
        }


def _focus_warning_from(status: dict | None) -> dict | None:
    """
    Extract an agent-facing warning dict from a raise_window status, or None
    when focus succeeded / wasn't requested. The dict shape is stable so agents
    can rely on it: {window_id, via, message}.
    """
    if not status or status.get("focused"):
        return None
    return {
        "window_id": status.get("window_id"),
        "via": status.get("via"),
        "message": status.get("warning") or "Target window is not the app's key window — input may route elsewhere.",
    }


# Which apps drive a Chromium renderer (browsers + Electron/CEF). An app's
# engine never changes, so we resolve once per app name. Tiny (one entry per
# distinct app touched). Keyed by display name; the Electron/CEF probe needs
# the pid, taken from the session on first lookup.
_chromium_based_cache: dict[str, bool] = {}


def _is_chromium_based(session) -> bool:
    """True for apps whose UI is a Chromium renderer — Chromium browsers AND
    Electron/CEF apps. These mishandle synthetic SkyLight clicks/keys, so they
    take the real-cursor + activation path; native apps (incl. Tauri/WebKit)
    stay on the invisible SkyLight path. Result cached per app name."""
    app = session.app
    cached = _chromium_based_cache.get(app)
    if cached is not None:
        return cached
    result = (app in CHROMIUM_BROWSERS) or is_chromium_renderer_app(session.pid)
    _chromium_based_cache[app] = result
    return result


async def _seamless_post(
    session,
    tool_name: str,
    post_fn,                         # callable(primer_first: bool) -> bool
    log_coords: tuple[int, int] | None = None,
    needs_primer: bool | None = None,
    target_wid: int | None = None,   # window to make key before a native click
) -> dict:
    """
    Generic seamless-mode dispatch. Owns the delivery/self-test gate, the
    Chromium-vs-native routing decision, and the post call itself. `post_fn` is
    a callable taking `primer_first: bool` that performs the actual SkyLight post
    for the specific event type (click, double-click, drag, scroll). Returning
    True/False from `post_fn` is the only success signal; raising from `post_fn`
    is caught and surfaced as `{ok: False, error: ...}`.

    Native click-family delivery is fully invisible: `make_window_key(target_wid)`
    flips the target window to key WITHOUT activating the app, raising the window,
    or moving the cursor — so both simple and key-window-dependent controls
    interact while the user's foreground stays put, in autonomous AND background
    mode. Only Chromium web content still needs activation (its renderer distrusts
    synthetic clicks), so that path alone can return requires_foreground.

    Returns one of:
      {ok: True, via: "skylight+keyed" | "skylight" | "...+primer"}
      {ok: False, requires_foreground: True, reason, app, suggestion}  # Chromium only
      {ok: False, error: "skylight_post_failed"}
      {ok: False, error: "invisible_delivery_error"}
      {ok: False, error: "chromium_cursor_warp" | "activation_failed"}  # Chromium path

    `log_coords` is the (x, y) used in autonomous-mode escalation log entries
    (Chromium path). Pass None for tools without a single canonical coordinate.

    `needs_primer` overrides the default "use primer for Chromium apps" rule when
    False/True is explicitly passed. Default None = auto-detect. Scroll passes
    False and posts directly regardless of frontmost state.

    `target_wid` is the window made key before a native click; when None (or the
    key-window helper is unavailable) delivery falls back to a raw click, which
    still fires simple controls.
    """
    # Delivery gate: if a startup/doctor self-test conclusively found that
    # SkyLight loads but no longer DELIVERS on this macOS build (a private-API
    # change), skip the invisible path entirely rather than posting into the
    # void and reporting a click that never landed. Only an explicit False
    # downgrades; None (untested / inconclusive) proceeds as normal.
    if skylight.delivery_verified() is False:
        if session.mode == "background":
            return {
                "ok": False,
                "requires_foreground": True,
                "reason": "skylight_delivery_unavailable",
                "app": session.app,
                "suggestion": (
                    "klyk's invisible-input path (SkyLight) loaded but a delivery "
                    "self-test failed on this macOS build, so an invisible click would "
                    "silently no-op. Switch to mode='autonomous' to let klyk click "
                    "visibly, or run `klyk doctor` for details."
                ),
            }
        # Autonomous: signal the caller to fall through to the visible cursor-warp
        # path (it records escalated_from='skylight_delivery_unavailable').
        return {"ok": False, "error": "skylight_delivery_unavailable"}

    if needs_primer is None:
        needs_primer = _is_chromium_based(session)

    # Chromium clicks: don't trust SkyLight. `needs_primer` is True only for a
    # click-type event on a Chromium-based app (browser or Electron/CEF) — the
    # exact case where the renderer
    # hit-tests synthetic SkyLight mouse events unreliably (rapid clicks get
    # reordered / mis-placed / silently dropped, and the OS post still reports
    # success, so klyk can't detect the miss). A real cursor click is
    # hit-tested correctly, so for these we skip SkyLight entirely:
    #   • background  → bail (a real cursor would steal the user's focus)
    #   • autonomous  → activate the app (so the caller's real-cursor click
    #                   lands on the right window) and signal cursor-warp; the
    #                   caller's existing autonomous fall-through does the click.
    # Scroll passes needs_primer=False (wheel events use a reliable input path)
    # so it keeps the invisible SkyLight route; native apps keep it too.
    if needs_primer:
        if session.mode == "background":
            return {
                "ok": False,
                "requires_foreground": True,
                "reason": "chromium_click_needs_foreground",
                "app": session.app,
                "suggestion": (
                    f"Reliable clicking in {session.app} (a Chromium renderer) needs "
                    "a real cursor — its trusted-event filter mishandles synthetic "
                    "clicks. Bring it forward, or use mode='autonomous' so klyk can "
                    "activate it and click visibly."
                ),
            }
        is_active = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.is_frontmost_app(session.pid)
        )
        if not is_active:
            await computer.activate_app(session.pid)
            await asyncio.sleep(0.26)
            still_active = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.is_frontmost_app(session.pid)
            )
            if not still_active:
                return {"ok": False, "error": "activation_failed"}
        # Signal the caller to use its real-cursor (cursor-warp) path, which
        # the Chromium renderer hit-tests correctly. Not requires_foreground,
        # so the caller's autonomous branch handles it.
        return {"ok": False, "error": "chromium_cursor_warp"}

    # Scroll posts directly regardless of frontmost state: macOS lets a scroll
    # gesture affect whatever window is under the pointer without bringing it
    # forward (same as a trackpad scroll over a background window) — verified
    # 2026-07-02. Click-family delivery (handled after this branch) makes the
    # target window key WITHOUT raising it; neither path activates or steals
    # the user's focus.
    if tool_name == "scroll":
        try:
            ok = await asyncio.get_event_loop().run_in_executor(
                None, lambda: post_fn(needs_primer),
            )
        except Exception as e:
            log.warning(f"skylight post raised in {tool_name}: {type(e).__name__}: {e}")
            return {"ok": False, "error": "invisible_delivery_error"}
        if not ok:
            return {"ok": False, "error": "skylight_post_failed"}
        return {"ok": True, "via": "skylight"}

    # Native click-family (click / double / triple-click / drag). Deliver
    # invisibly with NO activation, NO window raise, NO focus theft — the same
    # in autonomous AND background mode.
    #
    # make_window_key flips the target window to key for input routing (yabai's
    # SLPSPostEventRecordTo pattern) WITHOUT bringing it forward or changing the
    # OS-active app. A raw backgrounded SkyLight click already fires simple
    # controls (buttons, menu items); the keyed step additionally lets
    # key-window-dependent controls interact — text-field caret, list / table /
    # sidebar row selection — which otherwise respond only inside the key window.
    # Verified empirically (2026-07-06, 6/6 reproducible): a backgrounded native
    # window's button AND text field both interact after this, with the user's
    # active app and window stack completely unchanged. This replaced the old
    # activate-and-raise path, which stole focus and raised the window — the very
    # behavior autonomous mode exists to avoid, and the same over-activation that
    # was removed from scroll on 2026-07-02, now removed from clicks too.
    keyed = False
    if target_wid is not None:
        keyed = await asyncio.get_event_loop().run_in_executor(
            None, lambda: skylight.make_window_key(session.pid, int(target_wid)),
        )
    try:
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: post_fn(needs_primer),
        )
    except Exception as e:
        # ctypes-level or framework-level failure inside skylight.py. Honor
        # the docstring contract — caller gets {ok: False, error}, never a raw
        # exception they can't react to. Autonomous callers fall through to the
        # visible cursor-warp; background callers surface it.
        log.warning(f"skylight post raised in {tool_name}: {type(e).__name__}: {e}")
        return {"ok": False, "error": "invisible_delivery_error"}
    if not ok:
        return {"ok": False, "error": "skylight_post_failed"}
    return {"ok": True, "via": "skylight+keyed" if keyed else "skylight"}


def _is_command_shortcut(keys: list[str]) -> bool:
    """True if any combo carries a Cmd modifier — a menu/command shortcut
    (Cmd+N, Shift+Cmd+T, …). macOS routes these through the FRONTMOST app's
    menu bar, so they only reach the target when it's frontmost; plain
    keystrokes reach a backgrounded app fine via CGEventPostToPid."""
    for k in keys:
        if not isinstance(k, str):
            continue
        if "⌘" in k:  # ⌘
            return True
        toks = [t.strip().lower() for t in k.split("+")]
        if "cmd" in toks or "command" in toks:
            return True
    return False


async def _ensure_key_delivery(
    session, tool_name: str, command_shortcut: bool = False,
) -> dict | None:
    """Keyboard analogue of the click seamless path. Plain keystrokes
    (CGEventPostToPid) reach a BACKGROUNDED native app fine — that's klyk's
    invisible-typing property — so they need no activation. Two cases DO need
    the target frontmost, and are handled identically here:

      • Chromium renderers discard keydowns unless their window is OS-frontmost
        (the trusted-event filter that also drops background SkyLight clicks).
      • Command-key shortcuts (Cmd+…) on ANY app route through the frontmost
        app's menu bar, so a shortcut posted to a non-frontmost native app is
        silently handled by whatever IS frontmost (e.g. Cmd+A hitting Finder).

    When either applies and the target isn't frontmost:
      • background  → requires_foreground (never steal the user's focus)
      • autonomous  → activate + settle, logged, then proceed
    Otherwise return None immediately — keys stay fully invisible, zero overhead
    (plain typing and already-frontmost targets skip the frontmost check too).
    Returns a requires_foreground payload to abort, or None to proceed.
    """
    if session.mode not in ("background", "autonomous"):
        return None
    is_chromium = _is_chromium_based(session)
    if not (is_chromium or command_shortcut):
        return None
    is_active = await asyncio.get_event_loop().run_in_executor(
        None, lambda: computer.is_frontmost_app(session.pid)
    )
    if is_active:
        return None
    if session.mode == "background":
        if is_chromium:
            reason = "target_app_not_active"
            why = (
                f"Key delivery to {session.app} (a Chromium renderer) needs it "
                "frontmost — its trusted-event filter drops keydowns to a "
                "background window."
            )
        else:
            reason = "command_shortcut_needs_frontmost"
            why = (
                f"A command-key shortcut (Cmd+…) for {session.app} needs it "
                "frontmost — macOS routes menu shortcuts through the active app, "
                "so it would otherwise land in whatever app is in front."
            )
        return {
            "ok": False,
            "requires_foreground": True,
            "reason": reason,
            "app": session.app,
            "suggestion": (
                f"{why} Bring it forward, or use mode='autonomous' to let klyk "
                "activate it automatically."
            ),
        }
    # Autonomous: bring the target frontmost so the keys land, then settle.
    # Chromium needs ~250 ms for its renderer input handler to warm up after
    # focus; a native menu bar switches over in ~100 ms.
    await computer.activate_app(session.pid)
    await asyncio.sleep(0.26 if is_chromium else 0.12)
    _log_escalation(session, tool_name, None, None, "activate_for_keys")
    return None


async def _await_frontmost(session, timeout: float = 1.2) -> bool:
    """Activate the session app and wait until it is actually OS-frontmost, so
    keystrokes land on its modal panel (save/open dialog) and not on whatever
    the user is looking at. A single activate+sleep is unreliable under focus
    contention — poll instead. Returns True once frontmost, False on timeout."""
    await computer.activate_app(session.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.is_frontmost_app(session.pid)
        )
        if active:
            return True
        await asyncio.sleep(0.05)
    return False


async def _seamless_click(
    session,
    target_wid: int,
    x: float,
    y: float,
    button: str,
    tool_name: str,
    modifier_flags: int = 0,
) -> dict:
    """Thin wrapper: build a click post_fn and dispatch via _seamless_post.
    Modifier flags (Cmd, Shift, Option, Ctrl) stamp through SkyLight so
    Cmd+click → open-in-new-tab and Shift+click → range-select land
    invisibly the same way a plain click does."""
    return await _seamless_post(
        session, tool_name,
        lambda primer: skylight.post_mouse_click(
            session.pid, target_wid, float(x), float(y), button,
            modifier_flags=modifier_flags, primer_first=primer,
        ),
        log_coords=(int(x), int(y)),
        target_wid=target_wid,
    )


async def _seamless_double_click(
    session,
    target_wid: int,
    x: float,
    y: float,
    tool_name: str,
    modifier_flags: int = 0,
) -> dict:
    """Two stamped click pairs with click_state=2 on the second pair."""
    return await _seamless_post(
        session, tool_name,
        lambda primer: skylight.post_double_click(
            session.pid, target_wid, float(x), float(y),
            modifier_flags=modifier_flags, primer_first=primer,
        ),
        log_coords=(int(x), int(y)),
        target_wid=target_wid,
    )


async def _seamless_triple_click(
    session,
    target_wid: int,
    x: float,
    y: float,
    tool_name: str,
    modifier_flags: int = 0,
) -> dict:
    """Three stamped click pairs with click_state 1/2/3 — paragraph / full-content select."""
    return await _seamless_post(
        session, tool_name,
        lambda primer: skylight.post_triple_click(
            session.pid, target_wid, float(x), float(y),
            modifier_flags=modifier_flags, primer_first=primer,
        ),
        log_coords=(int(x), int(y)),
        target_wid=target_wid,
    )


async def _seamless_drag(
    session,
    target_wid: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tool_name: str,
    button: str = "left",
    modifier_flags: int = 0,
) -> dict:
    """Down → interpolated dragged events → up, all stamped for SkyLight."""
    return await _seamless_post(
        session, tool_name,
        lambda primer: skylight.post_drag(
            session.pid, target_wid,
            float(x1), float(y1), float(x2), float(y2),
            button=button, modifier_flags=modifier_flags, primer_first=primer,
        ),
        log_coords=(int(x1), int(y1)),
        target_wid=target_wid,
    )


async def _seamless_scroll(
    session,
    target_wid: int,
    x: float,
    y: float,
    direction: str,
    amount: int,
    tool_name: str,
    modifier_flags: int = 0,
) -> dict:
    """Stamped scroll-wheel event. Primer is omitted — Chromium's wheel-event
    path doesn't share the renderer trust filter that clicks hit, so the
    primer click would just add latency without changing delivery."""
    return await _seamless_post(
        session, tool_name,
        lambda _primer: skylight.post_scroll(
            session.pid, target_wid, float(x), float(y),
            direction, int(amount), modifier_flags=modifier_flags,
        ),
        log_coords=(int(x), int(y)),
        needs_primer=False,
    )


def _log_escalation(session, tool: str, x: int | None, y: int | None, reason: str) -> None:
    """
    Append an entry to the session's escalation log. Called when autonomous
    mode falls back from the invisible path to cursor-warp so the user can
    review on return exactly what klyk did that touched their cursor.
    Capped at 500 entries — oldest dropped to keep memory bounded under long
    autonomous runs.
    """
    entry = {
        "tool": tool,
        "x": x,
        "y": y,
        "reason": reason,
        "ts": time.time(),
    }
    session.escalation_log.append(entry)
    if len(session.escalation_log) > 500:
        # Drop oldest in O(N) shift — N is tiny (500) and escalations are
        # rare enough that this isn't on the hot path. Avoids importing
        # collections.deque for a one-line cap.
        del session.escalation_log[:len(session.escalation_log) - 500]
    log.info(f"escalation: tool={tool} reason={reason} app={session.app}")


async def _take_screenshot(session, window_id: int | None = None) -> tuple[str, int, int, dict | None]:
    """
    Capture the target window's screenshot. Returns (b64_png, width, height,
    focus_status). focus_status is the raise_window dict when a specific window
    was requested, or None when capturing the app's default window. Callers
    should propagate focus_warning when focus_status.focused is False so the
    agent can see that the captured image may be of a different window than
    requested.
    """
    # Activate first so any focus-triggered scroll (e.g. YouTube JS) settles
    # before we capture coordinates. Clicks must NOT re-activate or they'd
    # cause the same scroll after the screenshot. SKIP all activation and
    # window-raising in seamless modes — the whole point of those modes is
    # to never disturb the user's foreground, and the capture path itself is
    # z-order independent so it doesn't need the target to be frontmost.
    focus_status: dict | None = None
    if session.mode in ("background", "autonomous"):
        # Seamless: never activate, never raise. Just refresh bounds.
        pass
    elif window_id is not None:
        focus_status = await _focus_if_needed(session, window_id)
        await asyncio.sleep(0.05)
    else:
        await computer.activate_app(session.pid)
        await asyncio.sleep(0.25)
    await _refresh_window(session, window_id=window_id)
    # Wait for the repaint only when the previous leaf action mutated the UI;
    # passive looks stay near-instant. Fixes stale frames after click/type on
    # slow-repainting (Electron/web) surfaces — see _POST_ACTION_SETTLE_MS.
    settle = _POST_ACTION_SETTLE_MS if _last_action_mutated else _PASSIVE_SETTLE_MS
    img_b64, w, h = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: capture.take_screenshot(
            window_id=session.window_id,
            logical_width=session.width,
            logical_height=session.height,
            win_x=session.win_x,
            win_y=session.win_y,
            settle_ms=settle,
        ),
    )
    return img_b64, w, h, focus_status


async def _resolve_label_in_window(
    session,
    query: str,
    index: int,
    filter_wid: int | None,
    filter_bounds: tuple[int, int, int, int] | None,
    cached_img_b64: str | None = None,
) -> dict:
    """
    Resolve a label to a SCREEN coordinate for the target element. AX search
    first (one batched walker call), OCR fallback when AX misses. Used by
    tools that need to find one or more labeled elements without performing
    the action themselves (drag_to_element resolves both endpoints this way).

    The returned `elem["x"]/["y"]` are always SCREEN-space (absolute Mac
    coordinates) regardless of which tier hit — AX is naturally screen-space
    (AXPosition is absolute), and OCR results, which come back window-local
    relative to the captured window, are translated to screen-space here so
    callers don't have to track which tier they're on. Width/height are in
    pixels; both spaces share the same scale.

    Returns one of:
      {ok: True, elem, via: 'ax'|'ocr', img_b64?}
      {ok: False, error, matches?}
    `img_b64` is set when OCR ran so callers can reuse the same capture for a
    second resolve in the same tool call.
    """
    # --- Tier 1: AX search ---
    # Windowless apps don't have an AXFocusedWindow — go straight to the
    # full ax_snapshot walker which falls back to AXChildren of the app
    # element, where Dock items / control-center widgets live.
    if getattr(session, "windowless", False):
        elements = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.ax_snapshot(session.pid, max_results=400)
        )
        elements = _filter_for_browser(elements, session.app)
        ax_matches = [
            e for e in elements
            if query in _normalize_label(e.get("label", "") or "")
            or query in _normalize_label(e.get("value", "") or "")
        ]
    elif filter_bounds is None:
        # Generous candidate cap (>= 32): the walker returns AX-tree order and
        # stops at the cap, so it must be wide enough that an exact label hit
        # isn't truncated behind incidental substring hits before the caller's
        # _rank_ax_matches can promote it. Walker deadline still bounds latency.
        ax_matches = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: computer.ax_search_focused(
                session.pid, query, max_results=max(index + 8, 32),
            ),
        )
        ax_matches = _filter_for_browser(ax_matches, session.app)
    else:
        elements = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.ax_snapshot(session.pid)
        )
        elements = _filter_for_browser(elements, session.app)
        x0, y0, x1, y1 = filter_bounds
        elements = [
            e for e in elements
            if x0 <= e.get("x", 0) <= x1 and y0 <= e.get("y", 0) <= y1
        ]
        ax_matches = [
            e for e in elements
            if query in _normalize_label(e.get("label", "") or "")
            or query in _normalize_label(e.get("value", "") or "")
        ]

    # Prefer an exact label/value hit over an incidental substring hit before
    # honouring `index` — keeps drag endpoints (and any other caller) locked to
    # the element actually named rather than whatever sorts first in the tree.
    _rank_ax_matches(ax_matches, query)

    if ax_matches:
        if index >= len(ax_matches):
            return {
                "ok": False,
                "error": f"index {index} out of range — {len(ax_matches)} AX match(es).",
                "matches": ax_matches,
            }
        return {"ok": True, "elem": ax_matches[index], "via": "ax"}

    # --- Tier 2: OCR fallback ---
    # Windowless system apps (Dock, etc.) have no capture surface, so OCR
    # isn't possible — AX is the only path. Surface a clean error if the AX
    # walk missed.
    if getattr(session, "windowless", False):
        return {
            "ok": False,
            "error": (
                f"No AX match for '{query}' in {session.app!r} and OCR isn't "
                "available for windowless system apps. Verify the label exists "
                "(e.g. in the Dock)."
            ),
            "matches": [],
        }
    if not ocr.is_available():
        return {"ok": False, "error": "AX miss and OCR unavailable.", "matches": []}

    img_b64 = cached_img_b64
    if img_b64 is None:
        img_b64, _, _, _ = await _take_screenshot(session, window_id=filter_wid)

    def _ocr_match() -> list[dict]:
        fast = [
            m for m in ocr.recognize_all(img_b64, level=1)
            if query in _normalize_label(m["text"])
        ]
        if fast:
            return fast
        return [
            m for m in ocr.recognize_all(img_b64, level=0)
            if query in _normalize_label(m["text"])
        ]

    ocr_matches = await asyncio.get_event_loop().run_in_executor(None, _ocr_match)
    _rank_ocr_matches(ocr_matches, query)
    if not ocr_matches:
        return {
            "ok": False,
            "error": f"No AX or OCR match for '{query}'.",
            "matches": [],
            "img_b64": img_b64,
        }
    if index >= len(ocr_matches):
        return {
            "ok": False,
            "error": f"index {index} out of range — {len(ocr_matches)} OCR match(es).",
            "matches": ocr_matches,
            "img_b64": img_b64,
        }
    # OCR coords come back relative to the captured window; translate to
    # screen-space so the returned `elem` is in the same space as AX matches.
    # The captured window is `filter_wid` (when explicit) or the session's
    # current window (when None) — _take_screenshot resolved that already.
    if filter_wid is not None:
        win = await asyncio.get_event_loop().run_in_executor(
            None, lambda: capture.get_window_by_id(int(filter_wid))
        )
        win_x = int(win["x"]) if win else session.win_x
        win_y = int(win["y"]) if win else session.win_y
    else:
        win_x, win_y = session.win_x, session.win_y
    elem = dict(ocr_matches[index])
    elem["x"] = int(elem["x"]) + win_x
    elem["y"] = int(elem["y"]) + win_y
    return {"ok": True, "elem": elem, "via": "ocr", "img_b64": img_b64}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

# Schema lookup for validating `run`'s nested steps. The MCP SDK validates
# every TOP-LEVEL call against these inputSchemas, but `run` dispatches its
# steps through the internal call_tool, bypassing that check — so a nested
# step with a missing/out-of-range arg would otherwise surface as an opaque
# KeyError/ValueError. No tool schema uses additionalProperties:false, so the
# keys `run` injects (app, window_id) never trip validation.
_TOOL_SCHEMAS = {t.name: t.inputSchema for t in TOOLS}
# Pre-build one validator per tool. The bare jsonschema.validate() convenience
# function rebuilds (and re-check_schemas) the validator on EVERY call — ~1 ms
# each, so a long `run` paid hundreds of ms of pure validation overhead. Cached
# validators are ~70x faster and behaviour-identical (same draft auto-selected
# via validator_for). Built once at import; empty if jsonschema is unavailable.
if _jsonschema is not None:
    _TOOL_VALIDATORS = {
        name: _jsonschema.validators.validator_for(schema)(schema)
        for name, schema in _TOOL_SCHEMAS.items()
    }
else:
    _TOOL_VALIDATORS = {}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


# Per-call latency + inter-call gap. gap_ms approximates model reasoning time
# between top-level tool calls; nested calls from `run` don't update the gap
# anchor so they don't pollute the measurement.
_last_response_time: float | None = None
_call_depth = 0

# ---------------------------------------------------------------------------
# Call-pattern hints (A3) and post-action verify (B1)
# ---------------------------------------------------------------------------
#
# A bounded ring of the last few TOP-LEVEL tool names. After each call we
# look back over this window and inject a one-line hint when the agent's
# pattern matches a known time-waster (consecutive standalone actions
# instead of `run`; a long blind `wait`; action+inspect pair instead of
# `verify=true` or a `run` ending in inspect).
#
# The ring is process-wide (the MCP server already serializes calls — it
# does not handle concurrent agents in this build). Cap is fixed and
# named in any miss so the structure satisfies the hidden-state design
# spec (#9 — size cap, no unbounded growth, named in errors).
_HINT_HISTORY_CAP = 8
_call_history: "deque[str]" = deque(maxlen=_HINT_HISTORY_CAP)

# Actions an agent often follows with a redundant `inspect` and which
# should be reached via `run` when chained.
_BATCHABLE_ACTIONS = frozenset({
    "click", "click_element", "type_text", "press_key", "fill_field",
    "scroll", "drag", "drag_to_element", "context_menu_select",
    "double_click", "triple_click", "long_press", "ax_action",
})
# Tools whose entire job is "look at the screen" — pairing one of these
# immediately after a batchable action is the canonical anti-pattern that
# verify=true (or `run` with a trailing inspect) replaces.
_OBSERVATION_TOOLS = frozenset({"inspect", "screenshot", "read_grid", "ax_snapshot"})

# Post-mutation settle (B). A mutating action leaves the UI mid-repaint —
# Chromium/Electron especially needs ~150 ms to paint a closed modal, freshly
# typed text, or a new view. A capture fired immediately after such an action
# returns the PRE-action frame (the stale-screenshot bug). This flag records
# "the previous leaf call changed the UI"; the next capture reads it to wait
# for the repaint, and any non-mutating leaf clears it so passive looks stay
# instant. Process-wide because the server serializes calls (no concurrent
# agents in this build) — same justification as _call_history.
_POST_ACTION_SETTLE_MS = 150
_PASSIVE_SETTLE_MS = 10
_last_action_mutated = False


def _detect_hint(name: str, args: dict) -> str | None:
    """Return a one-line (<200 char) anti-pattern hint or None.

    Pure read of `_call_history` + current args; never raises (any failure
    swallowed and the call proceeds without a hint).
    """
    try:
        recent = list(_call_history)[-3:]

        # Pattern 1: blind `wait` long enough to almost always be wrong.
        # 2 s is the empirical line — under that, the agent is usually
        # right; over it, wait_for or split-poll wins.
        if name == "wait":
            secs = args.get("seconds")
            if isinstance(secs, (int, float)) and secs > 2:
                return (
                    f"wait(seconds={secs}) blocks the full duration even if the UI "
                    "is ready at 200 ms. Prefer wait_for on an AX signal, or split "
                    "into <=1 s wait + inspect inside a run."
                )

        # Pattern 2: action immediately followed by a separate observation.
        # The whole point of run + trailing inspect (or verify=true on the
        # action) is to fold these two round-trips into one.
        if name in _OBSERVATION_TOOLS and recent and recent[-1] in _BATCHABLE_ACTIONS:
            ax_nudge = (
                " And to CHECK an action's effect you rarely need pixels — prefer "
                "verify=true or an AX read (ax_snapshot / read_grid / read_element); "
                "they're faster, exact, and cheaper. Use a screenshot only for genuinely "
                "visual questions."
                if name in ("screenshot", "inspect")
                else ""
            )
            return (
                f"{recent[-1]} → {name} is the textbook pair to fold into one "
                "round-trip. Use `run` with the observation as the final action, "
                "or pass verify=true on the action for a cheap focused-state probe."
                + ax_nudge
            )

        # Pattern 3: three standalone batchable actions in a row — collapse.
        if name in _BATCHABLE_ACTIONS and len(recent) >= 2:
            tail = recent[-2:] + [name]
            if all((t in _BATCHABLE_ACTIONS or t == "wait") for t in tail):
                return (
                    f"{tail[0]} → {tail[1]} → {tail[2]} as separate top-level calls. "
                    "Collapse into one `run` — each round-trip is ~2 s tool + 5-10 s "
                    "model reasoning that batching skips."
                )

        return None
    except Exception:
        return None


def _record_call(name: str) -> None:
    """Append to the bounded history ring. Never raises."""
    try:
        _call_history.append(name)
    except Exception:
        pass


async def _post_action_verify(app_name: str | None) -> dict | None:
    """Per-app focused-element + window-title snapshot for verify=true. None on failure.

    Reads the session's app — NOT system-wide. Autonomous mode (klyk's
    default) leaves OS focus on whatever the user is reading, so a
    system-wide focus read would report the user's foreground app, not
    the app the action ran in. ~5-15 ms, failure-isolated (any exception
    returns None and the caller drops the field — Design Consideration #5).
    """
    if not app_name:
        return None
    try:
        session = registry.get_by_app(app_name)
        if session is None:
            return None
        snap = await asyncio.get_event_loop().run_in_executor(
            None, lambda: computer.ax_focused_summary(session.pid),
        )
        return snap or None
    except Exception:
        return None


def _response_indicates_ok(response: list) -> bool:
    """True if the last TextContent's JSON payload looks like a successful action.
    Used as a gate before running the post-action verify probe — a verify
    snapshot on a refused/failed action is misleading."""
    try:
        for item in reversed(response):
            if isinstance(item, types.TextContent):
                payload = json.loads(item.text)
                if not isinstance(payload, dict):
                    return False
                if "error" in payload:
                    return False
                if payload.get("blocked") is True:
                    return False
                if payload.get("requires_foreground") is True:
                    return False
                if "ok" in payload:
                    return bool(payload.get("ok"))
                # Tools like type_text return {"ok": True, "mode": "..."}.
                # Tools without an `ok` field but no error are treated as success.
                return True
        return False
    except Exception:
        return False


def _inject_meta(
    response: list,
    duration_ms: int,
    gap_ms: int | None,
    hint: str | None = None,
    verify: dict | None = None,
) -> None:
    """Attach _meta timing block (and optional hint / verify) to the last
    TextContent in the response, in place. No-op if the response has no
    JSON-decodable text payload."""
    meta = {"duration_ms": duration_ms}
    if gap_ms is not None:
        meta["gap_ms"] = gap_ms
    if hint is not None:
        meta["hint"] = hint
    for item in reversed(response):
        if isinstance(item, types.TextContent):
            try:
                payload = json.loads(item.text)
            except Exception:
                return
            if isinstance(payload, dict):
                payload["_meta"] = meta
                if verify is not None:
                    payload["verify"] = verify
                item.text = json.dumps(payload)
            return


# Tools that NEVER require control ownership: pure observation (safe to run
# from any session concurrently) and per-session meta/config (affects only
# this instance). Everything NOT listed here is a control action that drives
# the Mac, so it's gated on ownership — a non-owner gets one clear
# take_control message instead of silently racing input with the active
# session. Gating by default (allowlist the safe ones) means a newly-added
# control tool is protected automatically; the worst case for a misclassified
# read-only tool is a needless take_control, never a corrupted input race.
_OWNERSHIP_EXEMPT = frozenset({
    # observation — no machine control, safe concurrently
    "inspect", "screenshot", "screen_info", "list_windows", "read_element",
    "read_text", "read_grid", "get_pixel", "get_pixels", "get_clipboard",
    "ax_snapshot", "find_template", "get_template", "get_logs",
    "get_escalation_log", "list_sessions", "wait", "wait_for",
    "wait_for_visual",
    # meta / per-session config — affects only this instance
    "verdict", "set_mode", "resume",
    # the reclaim itself
    "take_control",
})


def _refresh_menubar() -> None:
    """Nudge the menu-bar header to re-read ownership after it changed for
    this session (blocked → inactive, or take_control → active). No-op if the
    menu isn't installed (non-macOS / not yet built). Never raises — display
    must not break tool dispatch."""
    try:
        from .menubar import menubar as _menubar
        _menubar.request_refresh()
    except Exception:
        pass


@server.call_tool()
async def call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent]:
    global _last_response_time, _call_depth, _last_action_mutated
    args = arguments or {}
    start = time.monotonic()
    is_top_level = _call_depth == 0
    gap_ms = (
        round((start - _last_response_time) * 1000)
        if (is_top_level and _last_response_time is not None)
        else None
    )
    _call_depth += 1
    log.info(f"tool: {name} | app: {args.get('app')} | {({k: v for k, v in args.items() if k not in ('app', 'text')})}")

    response: list = []

    async def _dispatch():
        # --- control-ownership gate ---
        # Only the active (owner) session may DRIVE the Mac. A superseded
        # session is blocked here, at the action, with one clear, actionable
        # line — never a silent input race with the active session.
        # Observation/meta tools (the exempt set) always pass.
        if name not in _OWNERSHIP_EXEMPT and not ownership.is_owner():
            _refresh_menubar()  # this session just learned it's superseded
            return [types.TextContent(type="text", text=json.dumps({
                "ok": False,
                "blocked": "not_active_session",
                "message": (
                    "Another klyk session is the active driver right now — "
                    "only one session drives klyk at a time, and control "
                    "passed to a more recently active session. Do NOT reclaim "
                    "automatically: the other session may be mid-task, and if "
                    "both sessions grabbed control back on every block they'd "
                    "fight over it endlessly. Instead, tell the user klyk is "
                    "in use by another session, and call `take_control` only if "
                    "the user wants THIS session to drive. Reads and screenshots "
                    "are never blocked."
                ),
            }))]

        # --- take_control ---
        if name == "take_control":
            prev = ownership.claim_ownership()
            _refresh_menubar()  # this session is now the active driver
            msg = "This session now controls klyk."
            if prev:
                msg += (
                    f" The previously-active session (pid {prev}) is now blocked "
                    "from control actions until it calls take_control."
                )
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "message": msg}))]

        # --- screenshot ---
        if name == "screenshot" or name == "inspect":
            # inspect = image + AX (the default observation tool, ~95% of calls).
            # screenshot = image only, for diagnostics / pure-visual evaluation.
            # The handler logic is identical except for the AX-include gate;
            # tool name is the only switch.
            include_ax = (name == "inspect")
            # Slim mode (inspect only): skip the screenshot entirely, walk a
            # smaller AX cap, return text-only. ~50-70 ms vs ~100-140 ms for
            # full inspect; payload drops from 50-200 kB to a few hundred
            # bytes. Ignored on `screenshot` (the whole point of screenshot
            # is the image — detail flag is silently dropped if passed).
            detail_mode = args.get("detail", "full")
            slim = (name == "inspect" and detail_mode == "slim")
            session, is_new = await _get_session(args, name)

            # Multi-display: full-display capture path. When `display` is set
            # we bypass window-based capture entirely and grab the whole screen
            # in screen-space coords. Mutually exclusive with `window_id` —
            # display wins if both are passed (the agent is asking for the
            # bigger frame). `inspect`'s AX walk is unchanged (still scoped
            # to the session's app PID, not the screen).
            display_spec = args.get("display")
            if display_spec is not None and name == "screenshot":
                display_entry = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: capture.resolve_display(display_spec)
                )
                if display_entry is None:
                    return [types.TextContent(type="text", text=json.dumps({
                        "ok": False,
                        "error": f"display={display_spec!r} not found",
                        "hint": "Call screen_info to list available displays and their indices.",
                    }))]
                try:
                    img_b64, dw, dh = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: capture.take_display_screenshot(display_entry)
                    )
                except Exception as e:
                    return [types.TextContent(type="text", text=json.dumps({
                        "ok": False, "error": f"display capture failed: {e}",
                    }))]
                meta = {
                    "width": dw, "height": dh,
                    "display": {
                        "index": display_entry["index"],
                        "display_id": display_entry["display_id"],
                        "x": display_entry["x"], "y": display_entry["y"],
                        "is_main": display_entry.get("is_main", False),
                    },
                    "coord_space": "screen",
                }
                save_path = args.get("save_path")
                include_image = True
                if save_path:
                    resolved = os.path.abspath(os.path.expanduser(save_path))
                    try:
                        with open(resolved, "wb") as f:
                            f.write(base64.b64decode(img_b64))
                        meta["saved_path"] = resolved
                        include_image = False
                    except Exception as e:
                        log.warning(f"display screenshot save_path write failed ({resolved}): {type(e).__name__}: {e}")
                        meta["save_error"] = f"{e}"
                payload: list = []
                if include_image:
                    payload.append(types.ImageContent(type="image", data=img_b64, mimeType="image/png"))
                payload.append(types.TextContent(type="text", text=json.dumps(meta)))
                return payload
            # Phase 5 — Speed:
            # Image capture and AX walk are independent post-activation;
            # _take_screenshot owns the activation+focus dance, then the
            # actual pixel grab is just a CG capture. The AX walk just
            # queries the AX tree by pid. Run them concurrently with
            # asyncio.gather so the agent sees max(image, ax) instead of
            # image + ax. Empirically this halves the post-activation
            # cost on inspect (image ~60 ms, ax ~70 ms — total drops
            # from ~130 ms sequential to ~70-80 ms in parallel).
            #
            # Failure isolation: each task is awaited independently.
            # An AX failure must NOT break the screenshot (current
            # contract); a screenshot failure does propagate (it's the
            # primary product of inspect).
            # Slim mode skips the screenshot dance entirely (no image in
            # response). Full mode runs the screenshot + AX walk in
            # parallel (Phase-5 speed work below).
            if slim:
                screenshot_task = None
                # Smaller raw walk: agent is asked to keep slim to focus /
                # modal checks; 60 elements pre-filter is plenty.
                raw_walk_cap = 60
            else:
                screenshot_task = asyncio.create_task(
                    _take_screenshot(session, window_id=_resolve_window(args, args["app"]))
                )
                raw_walk_cap = 300

            async def _walk_ax_top() -> list[dict]:
                # Cap the raw walk — inspect surfaces a capped element list
                # to the agent (50 in full mode, 15 in slim), so walking
                # many more is wasted IPC on pathologically heavy trees.
                return await asyncio.get_event_loop().run_in_executor(
                    None, lambda: computer.ax_snapshot(session.pid, max_results=raw_walk_cap),
                )

            ax_task = (
                asyncio.create_task(_walk_ax_top())
                if include_ax
                else None
            )

            if screenshot_task is not None:
                img_b64, w, h, focus_status = await screenshot_task
                session.screenshots_taken += 1
                meta = {
                    "width": w, "height": h,
                    "win_x": session.win_x, "win_y": session.win_y,
                    "app_launched": is_new,
                }
                warn = _focus_warning_from(focus_status)
                if warn is not None:
                    meta["focus_warning"] = warn
                # The image is a composited-region capture, so another app's
                # window sitting above and overlapping this one bleeds its pixels
                # into the frame (klyk doesn't raise the target in seamless
                # modes). Warn loudly so the agent doesn't trust a contaminated
                # image — AX reads stay correct, or raise via focus_window.
                try:
                    occ = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: capture.window_occluders(
                            int(session.window_id), session.pid,
                        ),
                    )
                except Exception:
                    occ = []
                if occ:
                    names = ", ".join(o["owner_name"] for o in occ)
                    meta["overlap_warning"] = (
                        f"Another window overlaps this one ({names}). This image is "
                        "a composited region capture, so those pixels may appear in "
                        "it. Prefer AX reads (ax_snapshot/read_grid), or call "
                        "focus_window to raise this window before screenshotting."
                    )
            else:
                # Slim path: no image, no width/height. AX coords are
                # window-relative once translated below, same as full mode.
                # We still raise the requested window (so the AX walk
                # targets it) and propagate any focus_warning from that
                # raise — slim mode must not lose the safety signal that
                # full mode gets for free out of _take_screenshot.
                img_b64 = ""
                # Reads don't need the window frontmost (the AX walk is by PID),
                # so in seamless modes skip the raise entirely — this matches full
                # inspect's seamless path and keeps slim inspect invisible too,
                # instead of being the one read that steals focus.
                focus_status = None
                if session.mode not in ("background", "autonomous"):
                    focus_status = await _focus_if_needed(
                        session, _resolve_window(args, args["app"]),
                    )
                meta = {
                    "win_x": session.win_x, "win_y": session.win_y,
                    "app_launched": is_new,
                    "detail": "slim",
                }
                warn = _focus_warning_from(focus_status)
                if warn is not None:
                    meta["focus_warning"] = warn
            if include_ax:
                try:
                    raw = await ax_task
                    # Auto-retry on suspiciously empty AX — two distinct races
                    # share this fix:
                    #   (a) Chromium: web a11y enables on first external AX
                    #       query, so the first walk after navigation races
                    #       with the renderer's tree population.
                    #   (b) SwiftUI apps (System Settings, parts of Music /
                    #       Notes / Mail): post-launch AX tree takes
                    #       300-700 ms to populate; the first inspect after
                    #       launch can race ahead and come back empty.
                    # Both recover with a single 250 ms re-walk. The 250 ms
                    # cost on genuinely-empty windows is bounded and rare
                    # (an agent inspecting a window with truly no AX content
                    # is unusual). Sequential after the parallel screenshot/
                    # AX pair because it depends on observing the first
                    # walk's emptiness.
                    if len(raw) < 8:
                        await asyncio.sleep(0.25)
                        raw = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: computer.ax_snapshot(session.pid, max_results=300),
                        )
                    elements = _filter_for_browser(raw, session.app)
                    wx, wy = session.win_x, session.win_y
                    for elem in elements:
                        elem["x"] -= wx
                        elem["y"] -= wy
                    # Rank real content/targets ahead of decorative containers
                    # (AXWindow, AXImage, AXGroup, AXScrollArea) so the capped list
                    # surfaces what the agent can act on — the "most-actionable" set
                    # the slim description promises — not just the first N in tree
                    # order. Use the BROAD interactive set (incl. AXRow/AXCell),
                    # which are genuine targets in native list UIs (Finder, Mail);
                    # the narrow browser set would wrongly demote them. Stable sort
                    # preserves tree/reading order within each tier.
                    elements.sort(
                        key=lambda e: 0 if e.get("role") in _INTERACTIVE_ROLES else 1
                    )
                    AX_CAP = 15 if slim else 50
                    truncated = len(elements) > AX_CAP
                    if truncated:
                        head = elements[:AX_CAP]
                        # Keep the focused element even if it ranked past the cap —
                        # the agent relies on the focused:true marker to confirm
                        # where typed input will land.
                        tail_focused = [e for e in elements[AX_CAP:] if e.get("focused")]
                        elements = head + tail_focused
                    meta["ax_elements"] = elements
                    meta["ax_element_count"] = len(elements)
                    if truncated:
                        meta["ax_truncated"] = True
                        meta["ax_hint"] = (
                            f"AX list capped at {AX_CAP}"
                            + (" (slim mode — re-call without detail='slim' for the full list)"
                               if slim
                               else f" — call ax_snapshot for the full tree if the target isn't here.")
                        )
                except Exception as e:
                    log.warning(f"inspect AX read failed for {session.app}: {type(e).__name__}: {e}")
                    meta["ax_elements"] = []
                    meta["ax_element_count"] = 0
                    meta["ax_error"] = f"{e}"

                # After the auto-retry, if AX is STILL nearly empty on a
                # browser, the renderer genuinely isn't exposing web content
                # (rare — usually means Chrome was launched without
                # --force-renderer-accessibility AND for some reason its
                # lazy-enable isn't firing). Warn the agent once.
                if (
                    is_browser(session.app)
                    and not session.ax_disabled_warned_on_inspect
                    and meta.get("ax_element_count", 0) < 5
                ):
                    meta["ax_disabled_warning"] = (
                        f"{session.app}'s web AX tree is empty even after a wake retry. "
                        "click_element will fall through to OCR for web targets. If this "
                        "persists, quit the browser fully and let klyk relaunch it. "
                        "(Warning fires once per session.)"
                    )
                    session.ax_disabled_warned_on_inspect = True
                # Non-browser app whose AX surface is genuinely empty even
                # after the auto-retry — SwiftUI apps that render their
                # content area as custom-drawn views, canvas-based UI,
                # apps with non-standard view trees. Point the agent at
                # the OCR / pixel fallbacks so the next call hits the
                # right primitive instead of another empty inspect.
                elif (
                    not is_browser(session.app)
                    and meta.get("ax_element_count", 0) == 0
                ):
                    meta["ax_empty_hint"] = (
                        "AX surface is empty for this window even after a "
                        "retry. Common with SwiftUI / canvas / custom-drawn "
                        "content. Use `read_text` for text content, "
                        "`get_pixel` / `read_grid` for colors, or `screenshot` "
                        "for purely visual inspection. AX-based tools "
                        "(`click_element`, `wait_for`, `read_element`) will "
                        "fall through to OCR or fail outright on this window."
                    )

            # Optional disk write. On success, omit the inline image to save tokens.
            # On failure, keep the inline image so the agent still gets the screenshot.
            include_image = not slim
            save_path = args.get("save_path")
            if save_path and not slim:
                resolved = os.path.abspath(os.path.expanduser(save_path))
                try:
                    with open(resolved, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    meta["saved_path"] = resolved
                    include_image = False
                except Exception as e:
                    log.warning(f"screenshot save_path write failed ({resolved}): {type(e).__name__}: {e}")
                    meta["save_error"] = f"{e}"

            payload: list = []
            if include_image:
                payload.append(types.ImageContent(type="image", data=img_b64, mimeType="image/png"))
            payload.append(types.TextContent(type="text", text=json.dumps(meta)))
            return payload

        # --- click ---
        elif name == "click":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            x, y = int(args["x"]), int(args["y"])
            button = args.get("button", "left")
            modifiers = args.get("modifiers")

            # Safety guard runs first regardless of mode — clicks outside the
            # target window are blocked in every mode (no opt-out via mode).
            if not args.get("confirm_destructive", False):
                safe, reason = await _check_click_safety(session, x, y)
                if not safe:
                    log.warning(f"click BLOCKED ({x},{y}): {reason}")
                    return [types.TextContent(type="text", text=json.dumps({"ok": False, "blocked": True, "reason": reason}))]

            # --- Seamless path (background / autonomous) ---
            # Route through SkyLight when seamless mode is on and the private
            # framework is loadable. Modifier keys (Cmd/Shift/Option/Ctrl) are
            # supported as of Phase 2.5 — they stamp onto the SkyLight events
            # the same way they would on a HID-tap click. Skip _focus_if_needed
            # and all window-raising; SkyLight delivers to the target PID's
            # event queue directly without needing the window to be key.
            seamless_eligible = (
                session.mode in ("background", "autonomous")
                and skylight.is_available()
            )
            escalated_from: str | None = None  # set when autonomous falls through to cursor-warp
            if seamless_eligible:
                # Refresh window bounds so (x, y) maps to a valid window-local
                # point — we still need accurate session.window_id even though
                # we never raise it.
                await _refresh_window(session, window_id=window_id)
                target_wid = window_id if window_id is not None else int(session.window_id)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_click(
                    session, target_wid, float(x), float(y), button, "click",
                    modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                # Background mode bails here with the structured failure.
                if seamless_result.get("requires_foreground"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                # Autonomous: SkyLight itself failed (rare). Log + fall through to cursor-warp,
                # marked so the agent's response distinguishes "humanoid mode" cursor-warp
                # from "autonomous mode escalated to cursor-warp."
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "click", x, y, escalated_from)

            # --- Visible cursor-warp path (humanoid + autonomous fallback) ---
            focus_status: dict | None = None
            if window_id is not None:
                focus_status = await _focus_if_needed(session, window_id)
                await _refresh_window(session, window_id=window_id)
            sx, sy = _to_screen(session, x, y)
            await computer.click(sx, sy, button, modifiers)
            hint = await _nearby_ax_hint(session, x, y)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if escalated_from is not None:
                # Autonomous mode landed here because the invisible path
                # failed — distinguish this from humanoid-mode cursor-warp so
                # the agent (or human reviewer) sees that the cursor moved
                # as part of an escalation, not as normal humanoid behavior.
                result["escalated_from"] = escalated_from
            if hint is not None:
                result["nearby_ax_hint"] = hint
                log.info(
                    f"click ({x},{y}) near AX element '{hint['label']}' "
                    f"({hint['role']}) — prefer click_element next time"
                )
            warn = _focus_warning_from(focus_status)
            if warn is not None:
                result["focus_warning"] = warn
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- double_click ---
        elif name == "double_click":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            x, y = int(args["x"]), int(args["y"])
            modifiers = args.get("modifiers")
            if not args.get("confirm_destructive", False):
                safe, reason = await _check_click_safety(session, x, y)
                if not safe:
                    return [types.TextContent(type="text", text=json.dumps({"ok": False, "blocked": True, "reason": reason}))]

            # Seamless path — two stamped click pairs, second carries
            # click_state=2 so apps interpret as a real double-click.
            escalated_from: str | None = None
            if session.mode in ("background", "autonomous") and skylight.is_available():
                await _refresh_window(session, window_id=window_id)
                target_wid = window_id if window_id is not None else int(session.window_id)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_double_click(
                    session, target_wid, float(x), float(y), "double_click",
                    modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                if seamless_result.get("requires_foreground"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "double_click", x, y, escalated_from)

            sx, sy = _to_screen(session, x, y)
            await computer.double_click(sx, sy, modifiers)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if escalated_from is not None:
                result["escalated_from"] = escalated_from
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- triple_click ---
        elif name == "triple_click":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            x, y = int(args["x"]), int(args["y"])
            modifiers = args.get("modifiers")
            if not args.get("confirm_destructive", False):
                safe, reason = await _check_click_safety(session, x, y)
                if not safe:
                    return [types.TextContent(type="text", text=json.dumps({"ok": False, "blocked": True, "reason": reason}))]

            # Seamless path — three stamped click pairs, click_state 1/2/3
            # so apps recognise a real triple-click.
            escalated_from: str | None = None
            if session.mode in ("background", "autonomous") and skylight.is_available():
                await _refresh_window(session, window_id=window_id)
                target_wid = window_id if window_id is not None else int(session.window_id)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_triple_click(
                    session, target_wid, float(x), float(y), "triple_click",
                    modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                if seamless_result.get("requires_foreground"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "triple_click", x, y, escalated_from)

            sx, sy = _to_screen(session, x, y)
            await computer.triple_click(sx, sy, modifiers)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if escalated_from is not None:
                result["escalated_from"] = escalated_from
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- ax_action ---
        elif name == "ax_action":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            # Always refresh the origin before _to_screen — even with no window_id
            # — so a window that moved since the last refresh doesn't leave a
            # stale origin that lands the action at the wrong screen point.
            await _refresh_window(session, window_id=window_id)
            x, y = int(args["x"]), int(args["y"])
            action_name = str(args["action"])
            sx, sy = _to_screen(session, x, y)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.ax_perform_action_at(float(sx), float(sy), action_name)
            )
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- long_press ---
        elif name == "long_press":
            session, _ = await _get_session(args, name)
            # Refresh the origin + bounds (the safety check below uses window
            # width/height) so a moved window doesn't leave stale coords — same
            # fix as click / ax_action.
            await _refresh_window(session, window_id=_resolve_window(args, args["app"]))
            x, y = int(args["x"]), int(args["y"])
            duration = float(args.get("duration", 1.0))
            button = args.get("button", "left")
            if not args.get("confirm_destructive", False):
                safe, reason = await _check_click_safety(session, x, y)
                if not safe:
                    return [types.TextContent(type="text", text=json.dumps({"ok": False, "blocked": True, "reason": reason}))]
            sx, sy = _to_screen(session, x, y)
            await computer.long_press(sx, sy, duration=duration, button=button)
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "duration": duration}))]

        # --- drag ---
        elif name == "drag":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            x1, y1 = int(args["x1"]), int(args["y1"])
            x2, y2 = int(args["x2"]), int(args["y2"])
            modifiers = args.get("modifiers")
            button = args.get("button", "left")
            hover_seconds = max(0.0, min(float(args.get("hover_seconds", 0.0)), 5.0))
            # SkyLight delivers drag events without moving the OS-level cursor,
            # so the target window's hover-detector never fires — spring-loaded
            # drops need a real cursor on the target. Force cursor_warp when
            # any hover hold is requested.
            skylight_eligible = hover_seconds == 0

            # Seamless path — mouse-down, interpolated dragged events,
            # mouse-up. Modifier flags (e.g. Option-drag for snap) stay
            # stamped across the full sequence. Skipped entirely when
            # hover_seconds > 0 — see skylight_eligible above.
            escalated_from: str | None = None
            if (
                skylight_eligible
                and session.mode in ("background", "autonomous")
                and skylight.is_available()
            ):
                await _refresh_window(session, window_id=window_id)
                target_wid = window_id if window_id is not None else int(session.window_id)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_drag(
                    session, target_wid,
                    float(x1), float(y1), float(x2), float(y2),
                    "drag", button=button, modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                if seamless_result.get("requires_foreground"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "drag", x1, y1, escalated_from)

            sx1, sy1 = _to_screen(session, x1, y1)
            sx2, sy2 = _to_screen(session, x2, y2)
            await computer.drag(sx1, sy1, sx2, sy2, hover_target_seconds=hover_seconds)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if escalated_from is not None:
                result["escalated_from"] = escalated_from
            if hover_seconds > 0:
                result["hovered_seconds"] = hover_seconds
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- drag_to_element ---
        elif name == "drag_to_element":
            session, _ = await _get_session(args, name)
            source_query = _normalize_label(args["source_label"])
            target_query = _normalize_label(args["target_label"])
            source_index = int(args.get("source_index", 0))
            target_index = int(args.get("target_index", 0))
            modifiers = args.get("modifiers")
            button = args.get("button", "left")
            hover_seconds = max(0.0, min(float(args.get("hover_seconds", 0.0)), 5.0))

            # Cross-app: resolve target inside a different app's AX tree. Launches
            # the target app if needed. SkyLight drag is PID-scoped, so cross-app
            # drags always go through the visible cursor_warp path.
            target_app_name = args.get("target_app")
            cross_app = bool(target_app_name) and target_app_name != args["app"]

            filter_wid = _resolve_window(args, args["app"])
            filter_bounds: tuple[int, int, int, int] | None = None
            if filter_wid is not None:
                win = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: capture.get_window_by_id(int(filter_wid))
                )
                if not win or win["pid"] != session.pid:
                    return [types.TextContent(type="text", text=json.dumps({
                        "error": (
                            f"Window {filter_wid} not found or doesn't belong to "
                            f"'{args['app']}'. Call list_windows to refresh labels."
                        ),
                    }))]
                filter_bounds = (
                    win["x"], win["y"],
                    win["x"] + win["width"], win["y"] + win["height"],
                )

            src = await _resolve_label_in_window(
                session, source_query, source_index, filter_wid, filter_bounds,
            )
            if not src["ok"]:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "endpoint": "source",
                    "error": src["error"],
                    "matches": src.get("matches", []),
                }))]

            # Resolve target — same session for within-app, target_app's session
            # for cross-app. Cross-app drags can't share the OCR screenshot
            # since it covers a different window.
            if cross_app:
                target_session, _ = await get_or_create_session(target_app_name)
                tgt = await _resolve_label_in_window(
                    target_session, target_query, target_index,
                    filter_wid=None, filter_bounds=None,
                )
            else:
                target_session = session
                cached = src.get("img_b64")
                tgt = await _resolve_label_in_window(
                    session, target_query, target_index, filter_wid, filter_bounds,
                    cached_img_b64=cached,
                )
            if not tgt["ok"]:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "endpoint": "target",
                    "source": src["elem"],
                    "source_via": src["via"],
                    "error": tgt["error"],
                    "matches": tgt.get("matches", []),
                }))]

            src_elem = src["elem"]
            tgt_elem = tgt["elem"]
            # Resolver guarantees SCREEN coords for both AX and OCR matches.
            sx1, sy1 = int(src_elem["x"]), int(src_elem["y"])
            sx2, sy2 = int(tgt_elem["x"]), int(tgt_elem["y"])

            escalated_from: str | None = None
            # SkyLight (invisible) drag is eligible only when:
            #   - within-app (SkyLight is PID-scoped)
            #   - hover_seconds == 0 (SkyLight events don't actually move the
            #     OS cursor, so the target's hover-detection never fires)
            #   - session mode wants invisibility
            if (
                not cross_app
                and hover_seconds == 0
                and session.mode in ("background", "autonomous")
                and skylight.is_available()
            ):
                await _refresh_window(session, window_id=filter_wid)
                target_wid = filter_wid if filter_wid is not None else int(session.window_id)
                # SkyLight expects window-local coords; convert from screen.
                wlx1 = float(sx1) - float(session.win_x)
                wly1 = float(sy1) - float(session.win_y)
                wlx2 = float(sx2) - float(session.win_x)
                wly2 = float(sy2) - float(session.win_y)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_drag(
                    session, target_wid,
                    wlx1, wly1, wlx2, wly2,
                    "drag_to_element", button=button, modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps({
                        "ok": True,
                        "source": src_elem,
                        "target": tgt_elem,
                        "source_via": src["via"],
                        "target_via": tgt["via"],
                        "via": seamless_result["via"],
                    }))]
                if seamless_result.get("requires_foreground"):
                    seamless_result["source"] = src_elem
                    seamless_result["target"] = tgt_elem
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "drag_to_element", sx1, sy1, escalated_from)

            # Cursor-warp path: coords already screen-space, hand to computer.drag
            # which expects absolute screen coords. This path is also taken for
            # cross-app drags and for any drag with hover_seconds > 0.
            await computer.drag(
                sx1, sy1, sx2, sy2, hover_target_seconds=hover_seconds,
            )
            result = {
                "ok": True,
                "source": src_elem,
                "target": tgt_elem,
                "source_via": src["via"],
                "target_via": tgt["via"],
                "via": "cursor_warp",
            }
            if cross_app:
                result["cross_app"] = True
                result["target_app"] = target_app_name
            if hover_seconds > 0:
                result["hovered_seconds"] = hover_seconds
            if escalated_from is not None:
                result["escalated_from"] = escalated_from
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- fill_field ---
        elif name == "fill_field":
            session, _ = await _get_session(args, name)
            x, y = int(args["x"]), int(args["y"])
            text = args["text"]
            if not args.get("confirm_destructive", False):
                safe, reason = await _check_click_safety(session, x, y)
                if not safe:
                    return [types.TextContent(type="text", text=json.dumps({"ok": False, "blocked": True, "reason": reason}))]

            # Cascade — tried in order, first to succeed wins, the chosen
            # path is reflected in `via` so the agent can verify cheaply
            # which mechanism delivered:
            #
            #   1. AXSetValue           — pure AX write, zero cursor/keyboard
            #                              side effects. Only fires for native
            #                              text inputs not rooted in AXWebArea
            #                              (web inputs ignore the AX write).
            #   2. SkyLight focus-click + Cmd+A + paste  — background/autonomous
            #                              modes, invisible click path.
            #   3. Cursor-warp focus-click + Cmd+A + paste — humanoid mode or
            #                              when SkyLight isn't usable.
            #
            # 1 is uncommon in practice (most fields agents target are web
            # forms) but it's the only path that touches *nothing* visible,
            # so it's worth trying first when the field IS native.

            # --- 1. AXSetValue fast path ---
            sx_for_ax, sy_for_ax = _to_screen(session, x, y)
            ax_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.ax_set_value_at(float(sx_for_ax), float(sy_for_ax), text)
            )
            if ax_result.get("ok"):
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": True, "via": "ax_set_value", "role": ax_result.get("role"),
                }))]
            ax_skip_reason = ax_result.get("status")  # for the response trail

            # --- 2. Focus-click, then clear (Cmd+A) and paste (Cmd+V) ---
            # Unlike a plain click, this path uses command shortcuts (Cmd+A /
            # Cmd+V), which macOS delivers only to the ACTIVE app's menu bar — a
            # keyed background window is not enough (verified 2026-07-06: both
            # no-op on a non-active window). The invisible AX write above handles
            # native text inputs; reaching here means it couldn't (mostly web /
            # Electron fields, which are Chromium and get activated anyway), so a
            # brief activation here is correct, not a focus-theft regression.
            if session.mode == "background":
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "requires_foreground": True,
                    "reason": "fill_field_needs_foreground",
                    "app": session.app,
                    "ax_skip_reason": ax_skip_reason,
                    "suggestion": (
                        f"This field didn't accept the invisible AX write, so klyk must clear "
                        f"and paste with Cmd+A / Cmd+V — which macOS delivers only to the "
                        f"frontmost app. Bring {session.app} forward, or use mode='autonomous'."
                    ),
                }))]
            # autonomous / humanoid: bring the app frontmost so the shortcuts land.
            frontmost = await _await_frontmost(session)
            await _refresh_window(session)
            sx, sy = _to_screen(session, x, y)
            await computer.click(sx, sy)
            await asyncio.sleep(0.01)
            await computer.press_key("Cmd+A", session.pid)
            await asyncio.sleep(0.005)
            await computer.type_text(text, session.pid)
            result: dict = {"ok": True, "via": "activated"}
            if not frontmost:
                result["warning"] = (
                    "Could not confirm the app came frontmost; the field may not have been "
                    "cleared/filled. Verify with inspect or read_element."
                )
            if ax_skip_reason:
                # Surface why the invisible AX write didn't win — useful for agents
                # and for klyk's own telemetry.
                result["ax_skip_reason"] = ax_skip_reason
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- type_text ---
        elif name == "type_text":
            session, _ = await _get_session(args, name)
            # Effective default: real keystrokes on Chromium (clipboard paste is
            # ignored by keydown-driven web UIs — games, rich editors), fast
            # paste everywhere else. An explicit `mode` always wins.
            mode = args.get("mode")
            if mode is None:
                mode = "keys" if _is_chromium_based(session) else "paste"
            # Paste is Cmd+V — a command shortcut macOS routes through the ACTIVE
            # app's menu bar, so it needs the target frontmost (a keyed background
            # window isn't enough). Per-char keys reach a keyed window invisibly,
            # so gate only paste as a command shortcut: autonomous activates,
            # background returns requires_foreground rather than silently pasting
            # into the void.
            gate = await _ensure_key_delivery(
                session, "type_text", command_shortcut=(mode == "paste"),
            )
            if gate is not None:
                return [types.TextContent(type="text", text=json.dumps(gate))]
            if mode == "keys":
                await computer.type_text_char_by_char(args["text"], session.pid)
            else:
                await computer.type_text(args["text"], session.pid)
            return [types.TextContent(
                type="text", text=json.dumps({"ok": True, "mode": mode}),
            )]

        # --- press_key ---
        elif name == "press_key":
            session, _ = await _get_session(args, name)
            # PostToPid routes keyboard events directly to the process — no activation needed
            # for the app, but if a specific window must receive the key, raise it first so
            # it becomes the app's key window.
            key = args.get("key")
            keys = args.get("keys")
            repeat = int(args.get("repeat", 1))
            if key is None and keys is None:
                raise ValueError("press_key needs either `key` or `keys`")
            if key is not None and keys is not None:
                raise ValueError("press_key: pass `key` or `keys`, not both")
            if repeat < 1:
                raise ValueError("press_key: repeat must be >= 1")
            sequence = [key] if key is not None else list(keys)
            total = len(sequence) * repeat
            if total > 1000:
                raise ValueError(f"press_key: total presses {total} exceeds cap of 1000")
            gate = await _ensure_key_delivery(
                session, "press_key", _is_command_shortcut(sequence),
            )
            if gate is not None:
                return [types.TextContent(type="text", text=json.dumps(gate))]
            focus_status = await _focus_if_needed(session, _resolve_window(args, args["app"]))
            if focus_status and focus_status.get("requires_foreground"):
                # Background mode, target window isn't key — don't post keys to
                # the wrong window. Surface the structured refusal instead.
                return [types.TextContent(type="text", text=json.dumps(focus_status))]
            if total == 1:
                await computer.press_key(sequence[0], session.pid)
            else:
                await computer.press_keys(sequence * repeat, session.pid)
            result: dict = {"ok": True}
            warn = _focus_warning_from(focus_status)
            if warn is not None:
                result["focus_warning"] = warn
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- hold_key ---
        elif name == "hold_key":
            session, _ = await _get_session(args, name)
            key = args.get("key")
            if not key or not isinstance(key, str):
                raise ValueError("hold_key needs `key` (string)")
            duration = float(args.get("duration", 1.0))
            if duration < 0.05 or duration > 10.0:
                raise ValueError("hold_key: duration must be between 0.05 and 10.0 seconds")
            gate = await _ensure_key_delivery(
                session, "hold_key", _is_command_shortcut([key]),
            )
            if gate is not None:
                return [types.TextContent(type="text", text=json.dumps(gate))]
            focus_status = await _focus_if_needed(session, _resolve_window(args, args["app"]))
            if focus_status and focus_status.get("requires_foreground"):
                # Background mode, target window isn't key — refuse rather than
                # hold a key against the wrong window.
                return [types.TextContent(type="text", text=json.dumps(focus_status))]
            try:
                await computer.hold_key(key, duration, session.pid)
            except ValueError as e:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False, "error": str(e),
                }))]
            result: dict = {"ok": True, "key": key, "duration": duration}
            warn = _focus_warning_from(focus_status)
            if warn is not None:
                result["focus_warning"] = warn
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- press_system_key ---
        elif name == "press_system_key":
            await _get_session(args, name)  # session for logging continuity; key is global
            key_name = str(args["key"])
            try:
                await computer.press_system_key(key_name)
            except ValueError as e:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": str(e),
                    "supported": computer.SYSTEM_KEY_NAMES,
                }))]
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "key": key_name}))]

        # --- scroll ---
        elif name == "scroll":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            x, y = int(args["x"]), int(args["y"])
            direction = args["direction"]
            amount = int(args.get("amount", 3))
            modifiers = args.get("modifiers")

            # Seamless path — stamped scroll-wheel event delivered to the
            # target window's PID without warping the cursor. Useful for
            # scrolling a background app behind the user's foreground work.
            # Cmd+scroll (zoom) and Shift+scroll (horizontal in some apps)
            # supported via modifier_flags.
            escalated_from: str | None = None
            if session.mode in ("background", "autonomous") and skylight.is_available():
                await _refresh_window(session, window_id=window_id)
                target_wid = window_id if window_id is not None else int(session.window_id)
                mod_flags = computer.modifier_flags_from_list(modifiers)
                seamless_result = await _seamless_scroll(
                    session, target_wid, float(x), float(y),
                    direction, amount, "scroll",
                    modifier_flags=mod_flags,
                )
                if seamless_result.get("ok"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                if seamless_result.get("requires_foreground"):
                    return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                escalated_from = seamless_result.get("error", "skylight_unknown")
                _log_escalation(session, "scroll", x, y, escalated_from)

            sx, sy = _to_screen(session, x, y)
            await computer.scroll(sx, sy, direction, amount)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if escalated_from is not None:
                result["escalated_from"] = escalated_from
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- move_cursor ---
        elif name == "move_cursor":
            session, _ = await _get_session(args, name)
            sx, sy = _to_screen(session, int(args["x"]), int(args["y"]))
            await computer.move_cursor(sx, sy)
            dwell = max(0.0, min(float(args.get("dwell_seconds", 0.0)), 10.0))
            if dwell > 0:
                await asyncio.sleep(dwell)
            result: dict = {"ok": True, "via": "cursor_warp"}
            if dwell > 0:
                result["dwelled_seconds"] = dwell
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- wait ---
        elif name == "wait":
            if args.get("app"):
                await _get_session(args, name)
            seconds = min(float(args.get("seconds", 1)), 30)
            await asyncio.sleep(seconds)
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "waited": seconds}))]

        # --- wait_for ---
        elif name == "wait_for":
            session, _ = await _get_session(args, name)
            text = args["text"]
            timeout = min(float(args.get("timeout", 4)), 30)
            query = _normalize_label(text)
            import time as _time
            start = _time.monotonic()
            found = None
            matched_on: str | None = None
            while _time.monotonic() - start < timeout:
                elements = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: computer.ax_snapshot(session.pid)
                )
                # Collect every match, then prefer an exact label/value hit over
                # an incidental substring hit (the same ranking click_element
                # uses) so a caller that clicks `found` lands on the element it
                # named — not whatever sorts first in AX-tree order.
                matches = [
                    e for e in elements
                    if query in _normalize_label(e.get("label", "") or "")
                    or query in _normalize_label(e.get("value", "") or "")
                ]
                if matches:
                    _rank_ax_matches(matches, query)
                    found = matches[0]
                    matched_on = (
                        "label" if query in _normalize_label(found.get("label", "") or "")
                        else "value"
                    )
                    break
                await asyncio.sleep(0.1)
            elapsed = round(_time.monotonic() - start, 2)
            if found:
                # Convert from screen-space to window-relative so coords match screenshot pixels
                found = dict(found)
                found["x"] -= session.win_x
                found["y"] -= session.win_y
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": True, "found": found, "matched_on": matched_on, "elapsed": elapsed,
                }))]
            return [types.TextContent(type="text", text=json.dumps({
                "ok": False, "timeout": True, "elapsed": elapsed,
                "message": f"'{text}' did not appear in the UI within {timeout}s.",
            }))]

        # --- wait_for_visual ---
        elif name == "wait_for_visual":
            session, _ = await _get_session(args, name)
            template_id = args.get("template_id")
            template_b64 = args.get("template_b64")
            if template_id:
                cached = session.template_cache.get(template_id)
                if cached is None:
                    return [types.TextContent(type="text", text=json.dumps({
                        "error": (
                            f"Unknown template_id '{template_id}'. Template cache is "
                            "per-session and capped at 50 entries — call get_template "
                            "again to refresh, or pass template_b64 directly."
                        ),
                    }))]
                # LRU touch: mark this template most-recently-used so an actively
                # reused one isn't evicted by 50 newer insertions (eviction drops
                # the front, i.e. the least-recently-used entry).
                session.template_cache[template_id] = session.template_cache.pop(template_id)
                template_b64 = cached
            elif not template_b64:
                return [types.TextContent(type="text", text=json.dumps({
                    "error": "wait_for_visual requires either 'template_id' or 'template_b64'.",
                }))]
            present = bool(args.get("present", True))
            threshold = float(args.get("threshold", 0.8))
            timeout = min(float(args.get("timeout", 10)), 30)
            poll_interval = max(float(args.get("poll_interval", 0.5)), 0.1)
            search_region = args.get("search_region")
            if search_region is not None:
                search_region = tuple(int(v) for v in search_region)
            import time as _time
            start = _time.monotonic()
            polls = 0
            last_match: dict | None = None
            last_error: str | None = None
            loop = asyncio.get_event_loop()
            while True:
                polls += 1
                try:
                    screenshot_b64, _w, _h, _focus = await _take_screenshot(session)
                    last_match = await loop.run_in_executor(
                        None, lambda: matcher.find(screenshot_b64, template_b64, threshold, search_region)
                    )
                    last_error = None
                except Exception as e:
                    last_match = None
                    last_error = f"{e}"
                    log.warning(f"wait_for_visual poll failed: {last_error}")
                matched = last_match is not None
                if matched == present:
                    elapsed = round(_time.monotonic() - start, 2)
                    result: dict = {
                        "ok": True, "elapsed": elapsed, "polls": polls,
                        "present": present, "found": matched,
                    }
                    if matched and isinstance(last_match, dict):
                        result.update(last_match)
                    return [types.TextContent(type="text", text=json.dumps(result))]
                if _time.monotonic() - start >= timeout:
                    elapsed = round(_time.monotonic() - start, 2)
                    last_confidence = (
                        last_match.get("confidence") if isinstance(last_match, dict) else None
                    )
                    last_confidence_str = (
                        f"{last_confidence:.3f}" if isinstance(last_confidence, (int, float)) else "n/a"
                    )
                    msg = (
                        f"Template did not {'appear' if present else 'disappear'} within {timeout}s. "
                        f"polls={polls}, last_confidence={last_confidence_str}."
                    )
                    if last_error:
                        msg += f" last_poll_error: {last_error}"
                    timeout_payload = {
                        "ok": False, "timeout": True, "elapsed": elapsed,
                        "polls": polls, "present": present,
                        "last_confidence": last_confidence,
                        "last_error": last_error,
                        "message": msg,
                    }
                    # If waiting for APPEARANCE timed out, an occluding window is a
                    # likely reason the template never showed in the composited
                    # capture (the pixels would be the occluder's). Surface it so
                    # the agent raises the window instead of giving up.
                    if present:
                        try:
                            occ = await loop.run_in_executor(
                                None,
                                lambda: capture.window_occluders(int(session.window_id), session.pid),
                            )
                        except Exception:
                            occ = []
                        if occ:
                            names = ", ".join(o["owner_name"] for o in occ)
                            timeout_payload["overlap_warning"] = (
                                f"Another window overlaps this one ({names}); the capture "
                                "may show its pixels — the template may be covered, not "
                                "absent. focus_window to raise this window and retry."
                            )
                    return [types.TextContent(type="text", text=json.dumps(timeout_payload))]
                await asyncio.sleep(poll_interval)

        # --- get_logs ---
        elif name == "get_logs":
            session, _ = await _get_session(args, name)
            # Cap the serialized payload (~12 KB) so a chatty app's stderr can't
            # blow the tool-result token budget; most-recent lines are kept.
            return [types.TextContent(type="text", text=json.dumps(
                session.log_buffer.to_dict(max_chars=12000)
            ))]

        # --- read_element ---
        elif name == "read_element":
            session, _ = await _get_session(args, name)
            sx, sy = _to_screen(session, int(args["x"]), int(args["y"]))
            value, status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.ax_value_at_detailed(float(sx), float(sy))
            )
            # status: "ok" | "no_value" | "no_element"
            # Surface to agent so it can distinguish transient AX failure
            # (no_element — retry/observe) from "this element doesn't expose
            # a value at all" (no_value — stop polling, try another approach).
            return [types.TextContent(type="text", text=json.dumps({
                "value": value,
                "found": value is not None,
                "status": status,
            }))]

        # --- get_pixel ---
        elif name == "get_pixel":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            await _refresh_window(session, window_id=window_id)
            x, y = int(args["x"]), int(args["y"])
            sx, sy = _to_screen(session, x, y)
            bounds = (session.win_x, session.win_y, session.width, session.height)
            r, g, b = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: capture.get_pixel(
                    float(sx), float(sy),
                    window_id=int(session.window_id) if session.window_id else None,
                    window_bounds=bounds,
                ),
            )
            return [types.TextContent(type="text", text=json.dumps({
                "r": r, "g": g, "b": b, "hex": f"#{r:02x}{g:02x}{b:02x}",
            }))]

        # --- get_pixels ---
        elif name == "get_pixels":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            await _refresh_window(session, window_id=window_id)
            raw_points = args.get("points") or []
            raw_regions = args.get("regions") or []
            if not isinstance(raw_points, list):
                raw_points = []
            if not isinstance(raw_regions, list):
                raw_regions = []
            if not raw_points and not raw_regions:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": "get_pixels requires a non-empty 'points' list and/or 'regions' list.",
                }))]
            bounds = (session.win_x, session.win_y, session.width, session.height)
            if not session.window_id:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": "get_pixels: no window_id resolved for the session. Call list_windows first.",
                }))]
            response: dict = {"ok": True}
            if raw_points:
                screen_points: list[tuple[int, int]] = []
                for p in raw_points:
                    px = int(p["x"]); py = int(p["y"])
                    sx, sy = _to_screen(session, px, py)
                    screen_points.append((sx, sy))
                samples = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: capture.get_pixels(
                        screen_points,
                        window_id=int(session.window_id),
                        window_bounds=bounds,
                    ),
                )
                response["pixels"] = [
                    {
                        "x": int(raw_points[i]["x"]),
                        "y": int(raw_points[i]["y"]),
                        "r": r, "g": g, "b": b,
                        "hex": f"#{r:02x}{g:02x}{b:02x}",
                    }
                    for i, (r, g, b) in enumerate(samples)
                ]
            if raw_regions:
                screen_rects: list[tuple[int, int, int, int]] = []
                for rg in raw_regions:
                    rx = int(rg["x"]); ry = int(rg["y"])
                    rw = int(rg["width"]); rh = int(rg["height"])
                    sx, sy = _to_screen(session, rx, ry)
                    screen_rects.append((sx, sy, rw, rh))
                region_samples = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: capture.get_pixels_in_rects(
                        screen_rects,
                        window_id=int(session.window_id),
                        window_bounds=bounds,
                    ),
                )
                response["regions"] = [
                    {
                        "x": int(raw_regions[i]["x"]),
                        "y": int(raw_regions[i]["y"]),
                        "width": int(raw_regions[i]["width"]),
                        "height": int(raw_regions[i]["height"]),
                        "r": r, "g": g, "b": b,
                        "hex": f"#{r:02x}{g:02x}{b:02x}",
                    }
                    for i, (r, g, b) in enumerate(region_samples)
                ]
            return [types.TextContent(type="text", text=json.dumps(response))]

        # --- read_grid ---
        elif name == "read_grid":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            await _refresh_window(session, window_id=window_id)
            if not session.window_id:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": "read_grid: no window_id resolved for the session. Call list_windows first.",
                }))]
            rows = int(args["rows"])
            cols = int(args["cols"])
            gx = float(args["x"])
            gy = float(args["y"])
            cw = float(args["cell_width"])
            ch = float(args["cell_height"])
            gap = float(args.get("cell_gap", 0))
            # Sample MOST of the cell area so the median dominates over any
            # centered glyph. The naïve "small centered sample" approach lands
            # smack on the letter — for a 48 px Wordle tile with a 30 px glyph
            # centered in it, anything < 60% of cell width returns the glyph
            # colour, not the fill. 70% keeps a small margin from the tile
            # border (so we don't pick up the un-filled gap pixels) while
            # giving the median plenty of background pixels to dominate.
            ix = max(2, cw * 0.7)
            iy = max(2, ch * 0.7)

            screen_rects: list[tuple[int, int, int, int]] = []
            cell_centers: list[tuple[int, int, int, int]] = []  # (r, c, screen_x, screen_y)
            for r in range(rows):
                for c in range(cols):
                    cell_x = gx + c * (cw + gap)
                    cell_y = gy + r * (ch + gap)
                    center_x_local = cell_x + cw / 2
                    center_y_local = cell_y + ch / 2
                    # Pixel-sample rect (window-local) → convert to screen.
                    sample_local_x = cell_x + (cw - ix) / 2
                    sample_local_y = cell_y + (ch - iy) / 2
                    sx, sy = _to_screen(session, sample_local_x, sample_local_y)
                    screen_rects.append((int(sx), int(sy), int(ix), int(iy)))
                    sx_center, sy_center = _to_screen(session, center_x_local, center_y_local)
                    cell_centers.append((r, c, int(sx_center), int(sy_center)))

            bounds = (session.win_x, session.win_y, session.width, session.height)
            # One window capture, all colours.
            region_samples = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: capture.get_pixels_in_rects(
                    screen_rects,
                    window_id=int(session.window_id),
                    window_bounds=bounds,
                ),
            )
            # Per-cell AX text — one element resolve + one batched multi-attr
            # read per cell. Runs in the thread pool so the asyncio loop isn't
            # blocked across all cells.
            def _read_text_all():
                return [
                    computer.ax_cell_text_at(float(sx), float(sy))
                    for (_r, _c, sx, sy) in cell_centers
                ]
            text_values = await asyncio.get_event_loop().run_in_executor(
                None, _read_text_all,
            )

            grid: list[list[dict]] = [[None] * cols for _ in range(rows)]  # type: ignore
            for idx, (r, c, _sx, _sy) in enumerate(cell_centers):
                pr, pg, pb = region_samples[idx] if idx < len(region_samples) else (0, 0, 0)
                grid[r][c] = {
                    "row": r,
                    "col": c,
                    "x": int(gx + c * (cw + gap) + cw / 2),
                    "y": int(gy + r * (ch + gap) + ch / 2),
                    "text": text_values[idx],
                    "r": pr, "g": pg, "b": pb,
                    "hex": f"#{pr:02x}{pg:02x}{pb:02x}",
                }
            return [types.TextContent(type="text", text=json.dumps({
                "ok": True, "rows": rows, "cols": cols, "cells": grid,
            }))]

        # --- set_clipboard ---
        elif name == "set_clipboard":
            await _get_session(args, name)
            text = args.get("text")
            image_path = args.get("image_path")
            if (text is None) == (image_path is None):
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": "set_clipboard requires exactly one of text or image_path",
                }))]
            if image_path is not None:
                resolved = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: computer.set_clipboard_image(image_path)
                )
                return [types.TextContent(type="text", text=json.dumps({"ok": True, "image_path": resolved}))]
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.set_clipboard(text)
            )
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "text_length": len(text)}))]

        # --- get_clipboard ---
        elif name == "get_clipboard":
            await _get_session(args, name)
            text = await asyncio.get_event_loop().run_in_executor(None, computer.get_clipboard)
            # Safety cap: a clipboard can hold an arbitrarily large copy (a whole
            # file's contents), and unlike read_text it isn't bounded by what's
            # on screen. Bound what we dump into context; surface the true length
            # + a truncated flag so the agent knows the full size and that it was
            # cut. Common case (a snippet) is well under the cap and unaffected.
            CAP = 100_000
            out: dict = {"text": text[:CAP], "length": len(text)}
            if len(text) > CAP:
                out["truncated"] = True
            return [types.TextContent(type="text", text=json.dumps(out))]

        # --- click_menu ---
        elif name == "click_menu":
            session, _ = await _get_session(args, name)
            path = args["path"]
            await computer.activate_app(session.pid)
            await asyncio.sleep(0.1)
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.click_menu(session.pid, path)
            )
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "path": path}))]

        # --- context_menu_select ---
        elif name == "context_menu_select":
            session, _ = await _get_session(args, name)
            x, y = int(args["x"]), int(args["y"])
            query = _normalize_label(args["item_label"])
            item_index = int(args.get("item_index", 0))
            timeout = max(0.2, min(float(args.get("timeout", 2.0)), 10.0))

            # Right-click must hit the target app frontmost — context menus
            # don't open in background apps via SkyLight. Activate, then click.
            #
            # Multi-window note: an explicit AX raise of the target window
            # before the right-click was tried and caused a regression on the
            # common case (re-raising an already-frontmost window appears to
            # close any context menu Finder is about to open). The agent is
            # responsible for calling focus_window beforehand when same-app
            # windows overlap at the right-click point.
            await computer.activate_app(session.pid)
            await asyncio.sleep(0.12)
            window_id = _resolve_window(args, args["app"])
            await _refresh_window(session, window_id=window_id)
            # Deliver the right-click via pid-targeted SkyLight at window-relative
            # coords — this reliably opens the contextual menu, including on
            # secondary displays where a global CGEventPost at screen coords can
            # land in the wrong place and never trigger the menu.
            target_wid = window_id if window_id is not None else int(session.window_id)
            if skylight.is_available():
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: skylight.post_mouse_click(
                        session.pid, target_wid, float(x), float(y), button="right",
                    ),
                )
            else:
                sx, sy = _to_screen(session, x, y)
                await computer.click(sx, sy, button="right")

            # Poll for the menu to surface. macOS context menus appear as
            # AXMenu / AXMenuItem in the app's AX tree; we look for any
            # menu-item-like role whose label matches.
            menu_roles = {
                "AXMenuItem", "AXMenuBarItem", "AXMenuButton",
                # Some Electron apps expose context items under generic roles
                # — accept those when the label matches.
                "AXButton",
            }
            deadline = time.monotonic() + timeout
            matched: dict | None = None
            wait_ms_start = time.monotonic()
            via = "ax"
            while time.monotonic() < deadline:
                await asyncio.sleep(0.08)
                # A right-click contextual menu surfaces as an open AXMenu inside
                # the window subtree, but ax_snapshot's per-node child cap (20)
                # truncates it (a sidebar/list outline has more rows than the cap,
                # and the AXMenu is appended after them). ax_read_open_menu finds
                # any open AXMenu's items directly and fast (~0.2s) — use it as the
                # primary. Run it ALONE (concurrent AX walks contend and stall),
                # and only fall back to the window-scoped scan for in-window menus
                # it didn't catch. App-level menu items rank first.
                menu_items = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: computer.ax_read_open_menu(
                        session.pid, deadline_seconds=0.8,
                    ),
                )
                cands = [
                    e for e in menu_items
                    if query in _normalize_label(e.get("label", "") or "")
                ]
                if not cands:
                    # Fallback: window-scoped scan (Electron in-window menus,
                    # menu-bar items). Generous child cap so menu items past the
                    # default truncation point are included.
                    elements = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: computer.ax_snapshot(
                            session.pid, max_children_per_node=80,
                            max_results=0, deadline_seconds=0.8,
                        ),
                    )
                    cands = [
                        e for e in elements
                        if e.get("role") in menu_roles
                        and query in _normalize_label(e.get("label", "") or "")
                    ]
                # Exact item label wins over a substring sibling (e.g. "Copy"
                # over "Copy Link") before `item_index` is applied.
                _rank_ax_matches(cands, query)
                if len(cands) > item_index:
                    matched = cands[item_index]
                    break

            wait_ms = int((time.monotonic() - wait_ms_start) * 1000)

            # No OCR fallback for menus. klyk captures the window
            # z-order-independently, so a native context menu (a separate surface)
            # never appears in the capture — OCR could only match coincidental
            # window-content text that happens to contain the label and mis-click
            # WHILE the menu is open. In-window / Electron menus are reliably in
            # the AX tree (renderer a11y is forced), so the AX poll above already
            # covers them. On a genuine AX miss we fail cleanly below rather than
            # risk a stray click on the wrong element.

            if matched is None:
                # Dismiss the open menu so it doesn't trap the user's input.
                await computer.press_keys(["Escape"])
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": (
                        f"Context menu opened but no item matching '{args['item_label']}' "
                        f"surfaced within {timeout:g}s. Menu was dismissed."
                    ),
                    "wait_ms": wait_ms,
                    "hint": (
                        "If same-app windows overlap at this point, call focus_window "
                        "first so the right-click lands on the intended window. The "
                        "right-click also opens a file-specific menu when it lands on a "
                        "file icon — verify your coord is in blank content area for "
                        "menus like 'Show View Options'."
                    ),
                }))]

            # Click the matched item. AX coords are screen-space; OCR coords
            # are window-relative and need _to_screen.
            if via == "ax":
                click_x = int(matched["x"])
                click_y = int(matched["y"])
            else:
                click_x, click_y = _to_screen(
                    session, int(matched["x"]), int(matched["y"]),
                )
            await computer.click(click_x, click_y)

            return [types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "matched_item": _win_rel(matched, session),
                "via": via,
                "wait_ms": wait_ms,
            }))]

        # --- set_window_bounds ---
        elif name == "set_window_bounds":
            session, _ = await _get_session(args, name)
            x, y = int(args["x"]), int(args["y"])
            w = args.get("width")
            h = args.get("height")
            window_id = _resolve_window(args, args["app"])
            if window_id is not None:
                # AX-direct path: works on any window, even non-frontmost. Fast.
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: computer.set_window_bounds_by_id(
                        session.pid, int(window_id), x, y,
                        int(w) if w is not None else None,
                        int(h) if h is not None else None,
                    ),
                )
                # The AX move propagates to CGWindowList asynchronously, so a
                # single immediate refresh can read the pre-move position. Poll
                # briefly until the bounds reflect the requested move (or settle,
                # e.g. when macOS clamps an off-screen request) so the returned
                # coordinates are accurate, not stale.
                for _ in range(5):
                    await _refresh_window(session, window_id=int(window_id))
                    if abs(session.win_x - x) <= 2 and abs(session.win_y - y) <= 2:
                        break
                    await asyncio.sleep(0.04)
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": True,
                    "window_id": int(window_id),
                    "win_x": session.win_x, "win_y": session.win_y,
                    "width": session.width, "height": session.height,
                }))]
            # Default path: frontmost window via osascript (backward-compatible).
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: computer.set_window_bounds(
                    session.pid, x, y,
                    int(w) if w is not None else None,
                    int(h) if h is not None else None,
                ),
            )
            for _ in range(5):
                await _refresh_window(session)
                if abs(session.win_x - x) <= 2 and abs(session.win_y - y) <= 2:
                    break
                await asyncio.sleep(0.04)
            return [types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "win_x": session.win_x, "win_y": session.win_y,
                "width": session.width, "height": session.height,
            }))]

        # --- list_windows ---
        elif name == "list_windows":
            session, _ = await _get_session(args, name)
            # Window enumeration is normally sub-100ms (a WindowServer query,
            # not an AX walk); 10 s is a generous ceiling that only trips
            # under genuine executor-queue backup (many concurrent tool calls
            # or rapid window churn stalling the OS), not normal variance.
            try:
                windows = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: capture.list_windows_for_pid(session.pid)
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Timed out listing windows for {session.app!r} after 10 s. "
                    "macOS's window server may be under heavy load (many apps "
                    "or windows churning at once) — wait a moment and retry, "
                    "or run `klyk doctor` if this persists."
                )
            # Assign / refresh A-Z labels in z-order
            label_map = window_labels.assign(args["app"], [w["window_id"] for w in windows])
            for w in windows:
                w["window"] = label_map.get(w["window_id"], "?")
            return [types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "app": session.app,
                "pid": session.pid,
                "count": len(windows),
                "windows": windows,
            }))]

        # --- focus_window ---
        elif name == "focus_window":
            session, _ = await _get_session(args, name)
            window_id = _resolve_window(args, args["app"])
            if window_id is None:
                raise RuntimeError("focus_window requires 'window' (label) or 'window_id'.")
            result = await computer.raise_window(session.pid, window_id)
            await _refresh_window(session, window_id=window_id)
            result["window"] = window_labels.label_for(args["app"], window_id)
            result["win_x"] = session.win_x
            result["win_y"] = session.win_y
            result["width"] = session.width
            result["height"] = session.height
            return [types.TextContent(type="text", text=json.dumps(result))]

        # --- screen_info ---
        elif name == "screen_info":
            info = await asyncio.get_event_loop().run_in_executor(
                None, capture.screen_info
            )
            return [types.TextContent(type="text", text=json.dumps(info))]

        # --- verdict ---
        elif name == "verdict":
            session, _ = await _get_session(args, name)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: reporter_mod.generate_verdict(session, args["test_description"])
            )
            if args.get("grade", True):
                from .grader import CRITERIA_BY_PLATFORM, _CRITERIA_BASE
                result["grading_criteria"] = CRITERIA_BY_PLATFORM.get(session.target, _CRITERIA_BASE)
            img_b64 = result.pop("screenshot")
            return [
                types.ImageContent(type="image", data=img_b64, mimeType="image/png"),
                types.TextContent(type="text", text=json.dumps(result)),
            ]

        # --- handle_system_dialog ---
        elif name == "handle_system_dialog":
            session, _ = await _get_session(args, name)
            action = args["action"]
            path = args.get("path")
            # Bring the session app (and its modal save/open panel) truly
            # frontmost before typing. A single activate+sleep is unreliable
            # under focus contention — keys would then leak into the user's
            # foreground app (only a stray final Return registering, saving with
            # defaults). Poll until frontmost.
            #
            # Keys here are delivered GLOBALLY (no pid), NOT pid-targeted: the
            # save/open panel is rendered by a separate process
            # (com.apple.appkit.xpc.openAndSavePanelService), so a keystroke
            # posted to the host app's pid lands in the document behind the panel,
            # not the panel. Global HID events go to the key window — the panel.
            frontmost = await _await_frontmost(session)
            if not frontmost:
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": (
                        f"Could not bring {session.app}'s dialog frontmost to "
                        "drive it reliably (focus contention). Nothing was typed. "
                        "Retry, or set mode='humanoid' and handle it visibly."
                    ),
                }))]

            if action == "cancel":
                await computer.press_key("Escape")
                await asyncio.sleep(0.3)
                return [types.TextContent(type="text", text=json.dumps({"ok": True, "action": "cancel"}))]

            elif action == "open":
                if path:
                    # Navigate to the full path directly via Go To Folder.
                    await computer.press_key("Cmd+Shift+G")
                    await asyncio.sleep(0.7)
                    await computer.type_text_char_by_char(path)
                    await asyncio.sleep(0.2)
                    await computer.press_key("Return")  # confirm go-to
                    await asyncio.sleep(0.5)
                await computer.press_key("Return")  # open
                await asyncio.sleep(0.5)
                return [types.TextContent(type="text", text=json.dumps({"ok": True, "action": action, "path": path}))]

            elif action == "save":
                import os as _os
                saved_path = _os.path.abspath(_os.path.expanduser(path)) if path else None
                loop = asyncio.get_event_loop()
                if saved_path:
                    directory = _os.path.dirname(saved_path)
                    filename = _os.path.basename(saved_path)
                    # GUARD: confirm a save panel is actually open AND give its
                    # 'Save As' field keyboard focus (which makes the panel SHEET
                    # the key window). ax_focus_save_field returns False when no
                    # save field exists — i.e. no panel is open. Without this guard
                    # the Go-To-Folder keystrokes below leak into the document
                    # behind the panel: Cmd+Shift+G becomes TextEdit's "Find
                    # Previous" and the path gets typed into the body. Never type
                    # blind into a dialog that isn't there.
                    panel_focused = await loop.run_in_executor(
                        None, lambda: computer.ax_focus_save_field(session.pid)
                    )
                    if not panel_focused:
                        return [types.TextContent(type="text", text=json.dumps({
                            "ok": False, "action": "save", "saved": False,
                            "error": (
                                "No save dialog is open (its filename field wasn't "
                                "found via accessibility), so nothing was typed. Open "
                                "the Save dialog first (e.g. press Cmd+S), then call "
                                "handle_system_dialog again."
                            ),
                        }))]
                    # Directory via AX — select the matching sidebar location.
                    # Fully invisible: no cursor, no keystrokes. Replaces the old
                    # Go-To-Folder shortcut, which macOS misrouted into the host
                    # app's document (the panel is a separate sandboxed process, so
                    # global keystrokes never reach it). AX bridges into the panel —
                    # the same channel that sets the filename and presses Save.
                    # Returns the location landed on, or None when the directory
                    # isn't a sidebar entry (a nested subfolder) — the saved-check
                    # below then reports that honestly rather than saving wrong.
                    nav_to = None
                    if directory:
                        nav_to = await loop.run_in_executor(
                            None,
                            lambda: computer.ax_navigate_save_panel(session.pid, directory),
                        )
                        if not nav_to:
                            # Couldn't reach the requested directory. Do NOT fall
                            # through to set-filename + Save — that would drop the
                            # file in the panel's CURRENT location (the wrong place).
                            # Cancel the panel and report; nothing gets saved.
                            await loop.run_in_executor(
                                None,
                                lambda: computer.ax_press_panel_button(
                                    session.pid, ("Cancel",)
                                ),
                            )
                            return [types.TextContent(type="text", text=json.dumps({
                                "ok": False, "action": "save", "saved": False,
                                "path": saved_path,
                                "error": (
                                    f"Couldn't navigate the save panel to {directory!r}: "
                                    "it isn't one of the panel's sidebar locations (home, "
                                    "Desktop, Downloads, iCloud, or a Favourite). klyk "
                                    "navigates the panel invisibly via its sidebar; a "
                                    "nested subfolder that isn't a Favourite isn't "
                                    "reachable that way. The panel was cancelled — nothing "
                                    "was saved. Save to a sidebar location, or add the "
                                    "folder to Finder's Favourites first."
                                ),
                            }))]
                    # Filename via AX, AFTER navigating — deterministic and
                    # focus-independent; cleanly overwrites any leaked Go-To-Folder
                    # text so the name is always correct.
                    if filename:
                        ok_name = await loop.run_in_executor(
                            None,
                            lambda: computer.ax_set_save_filename(session.pid, filename),
                        )
                        if not ok_name:
                            await computer.press_key("Cmd+A")
                            await asyncio.sleep(0.05)
                            await computer.type_text_char_by_char(filename)
                            await asyncio.sleep(0.15)
                # 3) Press Save via AX (deterministic). Fall back to Return.
                pressed = await loop.run_in_executor(
                    None, lambda: computer.ax_press_panel_button(session.pid, ("Save",))
                )
                if not pressed:
                    await computer.press_key("Return")
                await asyncio.sleep(0.6)
                # 4) Handle any alert that follows Save. An extension-mismatch
                #    confirmation ("you used the extension .txt …") we auto-resolve
                #    in favour of the requested extension. An ERROR ("you don't have
                #    permission", "the volume is read-only" — e.g. a sandboxed app
                #    refused the destination) we READ and SURFACE, never dismiss
                #    blindly — that message is the reason the save failed. Loop a
                #    few times since an extension confirm can be followed by an error.
                dialog_error = None
                ext = _os.path.splitext(saved_path)[1] if saved_path else ""
                for _ in range(3):
                    if saved_path and _os.path.exists(saved_path):
                        break
                    alert = await loop.run_in_executor(
                        None, lambda: computer.ax_read_alert(session.pid)
                    )
                    if not alert:
                        break
                    buttons = alert.get("buttons", [])
                    keep = next(
                        (b for b in buttons if ext and ext.lower() in b.lower()), None
                    )
                    if keep:
                        await loop.run_in_executor(
                            None,
                            lambda b=keep: computer.ax_press_panel_button(session.pid, (b,)),
                        )
                        await asyncio.sleep(0.5)
                    else:
                        dialog_error = alert.get("text")
                        await loop.run_in_executor(
                            None,
                            lambda: computer.ax_press_panel_button(
                                session.pid, ("OK", "Cancel", "Done", "Close")
                            ),
                        )
                        await asyncio.sleep(0.3)
                        break
                # 5) Verify the file actually landed — never report a misleading
                #    success, and surface the OS's own reason when it refused.
                result: dict = {"ok": True, "action": action, "path": saved_path}
                if saved_path:
                    exists = _os.path.exists(saved_path)
                    result["saved"] = exists
                    if nav_to:
                        result["navigated_to"] = nav_to
                    if not exists:
                        result["ok"] = False
                        # The save didn't land — dismiss the still-open panel via
                        # AX Cancel so a leftover modal sheet can't block the app
                        # (a stuck save panel made subsequent activations hang for
                        # minutes). Focus-independent; safe if already closed.
                        try:
                            await loop.run_in_executor(
                                None,
                                lambda: computer.ax_press_panel_button(
                                    session.pid, ("Cancel",)
                                ),
                            )
                        except Exception:
                            pass
                        if dialog_error:
                            result["error"] = (
                                f"macOS refused the save to {saved_path!r}: "
                                f"{dialog_error}"
                            )
                            result["dialog_message"] = dialog_error
                        elif directory and not nav_to:
                            result["error"] = (
                                f"Couldn't navigate the save panel to {directory!r}: it "
                                "isn't one of the panel's sidebar locations (home, "
                                "Desktop, Downloads, iCloud, or a Favourite). klyk "
                                "navigates the save panel invisibly via its sidebar; a "
                                "nested subfolder that isn't a Favourite isn't reachable "
                                "that way, so the file was NOT saved there. Save to a "
                                "sidebar location, or add this folder to Finder's "
                                "Favourites first."
                            )
                        else:
                            result["error"] = (
                                f"Save dialog handled (filename + location set via AX), "
                                f"but no file exists at {saved_path!r} and no error alert "
                                "was read — the panel may have kept a different default "
                                "location."
                            )
                return [types.TextContent(type="text", text=json.dumps(result))]

            return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown action: {action}"}))]

        # --- close_app ---
        elif name == "close_app":
            # session.close_app() handles dock badge + activity log teardown
            # via visibility.detach. No extra cleanup needed here.
            await close_app(args["app"])
            return [types.TextContent(type="text", text=json.dumps({"ok": True}))]

        # --- close_apps ---
        elif name == "close_apps":
            apps = args.get("apps") or []
            results = []
            for app_name in apps:
                if not isinstance(app_name, str) or not app_name.strip():
                    results.append({"app": app_name, "closed": False, "was_open": False, "error": "invalid app name"})
                    continue
                was_open = registry.get_by_app(app_name) is not None
                try:
                    await close_app(app_name)
                    results.append({"app": app_name, "closed": was_open, "was_open": was_open})
                except Exception as e:
                    results.append({"app": app_name, "closed": False, "was_open": was_open, "error": str(e)})
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "results": results}))]

        # --- resume ---
        elif name == "resume":
            # Hardened: the agent CANNOT clear an emergency stop — only the user can,
            # by pressing Cmd+Shift+Escape again. This tool just reports status so a
            # hijacked/injected agent can't un-pause a stop the user just triggered.
            if computer.emergency_stop_active():
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "emergency_stop": "active",
                    "message": "An emergency stop is active and can be cleared ONLY by the user pressing Cmd+Shift+Escape again. Ask them to press the chord to resume — the resume tool cannot clear it.",
                }))]
            return [types.TextContent(type="text", text=json.dumps({"ok": True, "emergency_stop": "inactive", "message": "No emergency stop is active."}))]

        # --- run ---
        elif name == "run":
            app_name = args["app"]
            # Resolve top-level window/window_id to a single window_id once, so the
            # inheritance is on the same logical window for every action and so the
            # 'window=A' label form works too (previously only window_id raw IDs
            # cascaded — labels did not).
            default_window_id = _resolve_window(args, app_name)
            all_results = []
            response_items = []
            step_timings = []
            focus_warnings: list[dict] = []
            # Seamless-mode escalations and explicit refusals bubble up here so
            # an agent reading run summary sees them without inspecting every
            # per-action payload. Same pattern as focus_warnings.
            requires_foreground_events: list[dict] = []
            escalations: list[dict] = []
            # Cache the window_id already focused by a prior action in this run so
            # repeat-same-window actions skip the ~50-80ms raise_window dance
            # (AX queries + 30ms unconditional settle inside raise_window). Reset
            # on focus_warning so a stuck window gets retried.
            focused_wid: int | None = None
            for action in args.get("actions", []):
                tool_name = action.get("tool")
                if not tool_name:
                    continue
                tool_args = {k: v for k, v in action.items() if k != "tool"}
                tool_args["app"] = app_name
                # Per-action window/window_id overrides run's default; otherwise inherit.
                if "window" not in tool_args and "window_id" not in tool_args and default_window_id is not None:
                    tool_args["window_id"] = default_window_id
                # Resolve target window for focus-cache comparison. Silently fall
                # back to None on unknown labels — the inner handler will raise.
                try:
                    target_wid = _resolve_window(tool_args, app_name)
                except RuntimeError:
                    target_wid = None
                if target_wid is not None and target_wid == focused_wid:
                    tool_args.pop("window", None)
                    tool_args.pop("window_id", None)
                # Validate the step against its schema before dispatch — `run`
                # bypasses the SDK's top-level validation, so without this a
                # missing/out-of-range arg would surface as an opaque KeyError
                # the agent can't act on. On failure: record a clean step and
                # skip dispatch (no half-run side effect).
                _validator = _TOOL_VALIDATORS.get(tool_name)
                if _validator is not None:
                    try:
                        _validator.validate(tool_args)
                    except _jsonschema.ValidationError as ve:
                        all_results.append({
                            "tool": tool_name, "ok": False,
                            "error": f"step '{tool_name}': {ve.message}",
                        })
                        step_timings.append(f"{tool_name}=INVALID")
                        continue
                step_start = time.monotonic()
                try:
                    result = await call_tool(tool_name, tool_args)
                    step_ms = round((time.monotonic() - step_start) * 1000)
                    action_result: dict = {"tool": tool_name, "ok": True, "duration_ms": step_ms}
                    had_focus_warning = False
                    for item in result:
                        if isinstance(item, types.ImageContent):
                            response_items.append(item)
                            action_result["has_image"] = True
                        elif isinstance(item, types.TextContent):
                            try:
                                parsed = json.loads(item.text)
                                action_result["result"] = parsed
                                # Bubble focus warnings up to the run-level so an
                                # agent skimming the summary sees them even if it
                                # doesn't read every per-action result.
                                if isinstance(parsed, dict) and "focus_warning" in parsed:
                                    focus_warnings.append({"step": tool_name, **parsed["focus_warning"]})
                                    had_focus_warning = True
                                # Seamless mode bubbling — if background mode
                                # refused, surface it at run level. If
                                # autonomous escalated to cursor-warp, surface
                                # that too so the agent sees what touched the
                                # user's cursor at a glance.
                                if isinstance(parsed, dict) and parsed.get("requires_foreground"):
                                    requires_foreground_events.append({
                                        "step": tool_name,
                                        "reason": parsed.get("reason"),
                                        "suggestion": parsed.get("suggestion"),
                                    })
                                    action_result["ok"] = False  # explicit refusal counts as not-done
                                if isinstance(parsed, dict) and parsed.get("escalated_from"):
                                    escalations.append({
                                        "step": tool_name,
                                        "escalated_from": parsed["escalated_from"],
                                    })
                                # A safety-blocked action (click outside window, etc.)
                                # returns the payload normally — its handler didn't
                                # raise — but the action did NOT happen. Mark the
                                # step as ok=False so an agent skimming step_timings
                                # doesn't assume the click landed.
                                if isinstance(parsed, dict) and parsed.get("blocked") is True:
                                    action_result["ok"] = False
                                # Same for tool-level errors that came back as a
                                # payload rather than an exception.
                                if isinstance(parsed, dict) and "error" in parsed and "ok" not in parsed:
                                    action_result["ok"] = False
                            except Exception:
                                action_result["result"] = item.text
                    # Per-step verify: the run description tells agents to set
                    # verify=true on actions inside run, but the top-level verify
                    # path only fires for is_top_level calls — nested steps run at
                    # depth>=1 and would otherwise get nothing. Honor the flag here
                    # (only on a batchable action that actually landed) so the
                    # recommendation isn't a no-op. Cost is paid only when asked.
                    if (
                        tool_args.get("verify")
                        and tool_name in _BATCHABLE_ACTIONS
                        and action_result.get("ok")
                        and isinstance(action_result.get("result"), dict)
                    ):
                        _v = await _post_action_verify(app_name)
                        if _v is not None:
                            action_result["result"]["verify"] = _v
                    # Collapse contiguous boring same-tool actions into a single
                    # {tool, ok, count, duration_ms} entry to keep long batches
                    # (e.g. 200 press_key) from ballooning the response payload.
                    # "Boring" = ok:True with no meaningful additional info to
                    # inspect. Specifically: no image, no warning, no hint, no
                    # escalation marker. A {ok:True, via:"skylight"} response
                    # from a seamless click is boring — `via` is bookkeeping the
                    # agent can derive from session mode if it cares. The
                    # presence of "count" marks an entry as collapsible.
                    payload = action_result.get("result")
                    # Only ACTION tools collapse. Observation/read tools
                    # (read_grid, ax_snapshot, read_text, read_element, get_*)
                    # carry data the agent needs — collapsing them to a bare
                    # {ok, count} drops the payload and forces a re-read (a
                    # standalone screenshot, ironically). Gate on
                    # _BATCHABLE_ACTIONS so reads always return in full.
                    is_boring = (
                        tool_name in _BATCHABLE_ACTIONS
                        and not action_result.get("has_image")
                        and isinstance(payload, dict)
                        and payload.get("ok") is True
                        and "nearby_ax_hint" not in payload
                        and "focus_warning" not in payload
                        and "escalated_from" not in payload
                        and not payload.get("requires_foreground")
                        and "error" not in payload
                        and "verify" not in payload
                    )
                    if (
                        is_boring
                        and all_results
                        and all_results[-1].get("tool") == tool_name
                        and "count" in all_results[-1]
                    ):
                        all_results[-1]["count"] += 1
                        all_results[-1]["duration_ms"] += step_ms
                        c = all_results[-1]["count"]
                        step_timings[-1] = f"{tool_name}×{c}={all_results[-1]['duration_ms']}ms"
                    elif is_boring:
                        all_results.append({
                            "tool": tool_name, "ok": True, "count": 1, "duration_ms": step_ms,
                        })
                        step_timings.append(f"{tool_name}={step_ms}ms")
                    else:
                        all_results.append(action_result)
                        step_timings.append(f"{tool_name}={step_ms}ms")
                    if target_wid is not None:
                        # Only treat the window as focused for the NEXT step if this
                        # step actually landed and raised it cleanly. A failed /
                        # blocked / requires_foreground step did NOT focus it, so
                        # caching it would make the next same-window action skip its
                        # own focus raise and post input to the wrong window.
                        focused_wid = (
                            target_wid
                            if (action_result.get("ok", True) and not had_focus_warning)
                            else None
                        )
                except Exception as e:
                    step_ms = round((time.monotonic() - step_start) * 1000)
                    step_timings.append(f"{tool_name}=ERR({step_ms}ms)")
                    all_results.append({"tool": tool_name, "ok": False, "error": str(e), "duration_ms": step_ms})
            log.info(f"run summary: [{', '.join(step_timings)}]")
            # Top-level ok reflects whether EVERY step landed. Collapsed boring
            # entries are ok:True; full entries carry the ok the per-step logic
            # set (False for blocked / requires_foreground / errored steps). An
            # agent that checks only the envelope must not read a batch with a
            # blocked or failed step as success.
            failed = [r for r in all_results if not r.get("ok", True)]
            summary: dict = {
                "ok": not failed,
                "results": all_results,
                "step_timings": step_timings,
            }
            if failed:
                summary["failed_steps"] = [
                    {"tool": r.get("tool"), "error": r.get("error")} for r in failed
                ]
            if focus_warnings:
                summary["focus_warnings"] = focus_warnings
            if requires_foreground_events:
                summary["requires_foreground_events"] = requires_foreground_events
            if escalations:
                summary["escalations"] = escalations
            response_items.append(types.TextContent(type="text", text=json.dumps(summary)))
            return response_items

        # --- list_sessions ---
        elif name == "list_sessions":
            return [types.TextContent(type="text", text=json.dumps({"sessions": _list_sessions()}))]

        # --- get_escalation_log ---
        elif name == "get_escalation_log":
            session, _ = await _get_session(args, name)
            return [types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "app": session.app,
                "mode": session.mode,
                "entries": list(session.escalation_log),
                "count": len(session.escalation_log),
            }))]

        # --- set_mode ---
        elif name == "set_mode":
            session, _ = await _get_session(args, name)
            new_mode = args["mode"]
            if new_mode not in ("humanoid", "background", "autonomous"):
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": f"invalid mode {new_mode!r}; must be one of humanoid/background/autonomous",
                }))]
            # The two invisible modes require SkyLight to actually be loadable.
            # On a future macOS where SkyLight is gone, fall back to humanoid with
            # a clear reason rather than pretending the mode is set — silent
            # partial success here is the worst failure mode.
            if new_mode in ("background", "autonomous") and not skylight.is_available():
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": "skylight_unavailable",
                    "message": "SkyLight private framework is not loadable on this macOS — "
                               "background / autonomous modes need it. Stay on 'humanoid' for "
                               "now; klyk still works, clicks just use the cursor-warp path.",
                    "applied_mode": session.mode,
                }))]
            previous = session.mode
            session.mode = new_mode
            payload: dict = {"ok": True, "mode": new_mode, "previous_mode": previous}
            if new_mode == "autonomous":
                payload["note"] = (
                    "Autonomous mode (the default): klyk auto-escalates to cursor-warp "
                    "when the invisible path can't deliver. Every escalation is logged on "
                    "the session — review later via list_sessions, the menu-bar status "
                    "item dropdown, or get_escalation_log."
                )
            return [types.TextContent(type="text", text=json.dumps(payload))]

        # --- select_option ---
        elif name == "select_option":
            session, _ = await _get_session(args, name)
            x, y = int(args["x"]), int(args["y"])
            option = args["option"]
            sx, sy = _to_screen(session, x, y)
            await computer.click(sx, sy)
            await asyncio.sleep(0.25)
            await computer.type_text_char_by_char(option, session.pid)
            await asyncio.sleep(0.1)
            await computer.press_key("Return", session.pid)
            # Read the control back so the result reflects what was ACTUALLY
            # selected — type-to-select matches on a prefix and can land on the
            # wrong item, so we must not blindly report ok:true. ax_value_at
            # reads the popup's AXValue (its selected item's title) at the
            # control's screen position.
            value = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.ax_value_at(float(sx), float(sy))
            )
            ok = value is not None and _normalize_label(option) in _normalize_label(value)
            payload: dict = {"ok": ok, "selected": value, "requested": option}
            if not ok:
                payload["warning"] = (
                    "Selected value doesn't match the requested option — the popup "
                    "matches on prefix only, or the option text differs. Re-read the "
                    "control's options and retry, or pick by exact visible label."
                )
            return [types.TextContent(type="text", text=json.dumps(payload))]

        # --- ax_snapshot ---
        elif name == "ax_snapshot":
            session, _ = await _get_session(args, name)
            elements = await asyncio.get_event_loop().run_in_executor(
                None, lambda: computer.ax_snapshot(session.pid)
            )
            elements = _filter_for_browser(elements, session.app)
            # Convert from screen-space to window-relative so coords match screenshot pixels
            wx, wy = session.win_x, session.win_y
            for elem in elements:
                elem["x"] -= wx
                elem["y"] -= wy
            # Cap the payload: an uncapped tree on a rich/multi-window app (e.g.
            # Finder with several windows → 600+ elements, ~50 KB) overflows the
            # MCP client's response token limit and becomes unreadable. Cap the
            # element count and bound any oversized value string so the snapshot
            # is always ingestible; the agent narrows with a window or `inspect`.
            _AX_SNAPSHOT_CAP = 200
            total = len(elements)
            kept = elements[:_AX_SNAPSHOT_CAP]
            for elem in kept:
                v = elem.get("value")
                if isinstance(v, str) and len(v) > 200:
                    elem["value"] = v[:200] + "…"
            payload: dict = {
                "element_count": total,
                "returned": len(kept),
                "elements": kept,
            }
            if total > _AX_SNAPSHOT_CAP:
                payload["ax_truncated"] = True
                payload["ax_hint"] = (
                    f"AX tree has {total} elements; returning the first "
                    f"{_AX_SNAPSHOT_CAP} (the full set would exceed the response "
                    "size limit). Narrow with a `window` label or use `inspect` "
                    "for the interactive subset."
                )
            # Dynamic warning — only fires when this snapshot really came
            # back nearly empty on a browser. No stale cached flag.
            if is_browser(session.app) and len(elements) < 5:
                payload["ax_disabled_warning"] = (
                    f"{session.app}'s web AX tree is empty — this snapshot covers "
                    "only browser-shell elements (toolbar, tabs). Web content "
                    "(page buttons, links, form fields) isn't reaching the AX layer. "
                    "If this persists across calls, quit the browser fully and let "
                    "klyk relaunch it so --force-renderer-accessibility takes effect."
                )
            return [types.TextContent(type="text", text=json.dumps(payload))]

        # --- read_text ---
        elif name == "read_text":
            if not ocr.is_available():
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": False,
                    "error": (
                        "Vision OCR bindings unavailable. Run: "
                        "pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
                    ),
                }))]
            session, _ = await _get_session(args, name)
            filter_wid = _resolve_window(args, session.app)
            level_str = args.get("level", "fast")
            level = 1 if level_str == "fast" else 0
            query = args.get("query")
            # Match the same normalization click_element uses (hyphen variants +
            # NFC + lowercase) so read_text(query=...) and click_element agree on
            # what a label "contains" — same fact, one rule.
            q_norm = _normalize_label(query.strip()) if query else None
            languages_arg = args.get("languages")
            languages = (
                [str(x) for x in languages_arg]
                if isinstance(languages_arg, list) and languages_arg
                else None
            )

            # Optional region (window-relative).
            rx = args.get("x")
            ry = args.get("y")
            rw = args.get("width")
            rh = args.get("height")
            has_region = all(v is not None for v in (rx, ry, rw, rh))
            if has_region:
                rx, ry, rw, rh = float(rx), float(ry), float(rw), float(rh)

            img_b64, _, _, focus_status = await _take_screenshot(
                session, window_id=filter_wid
            )

            def _run_ocr() -> list[dict]:
                return ocr.recognize_all(img_b64, level=level, languages=languages)

            observations = await asyncio.get_event_loop().run_in_executor(
                None, _run_ocr
            )

            if has_region:
                observations = [
                    m for m in observations
                    if rx <= m["x"] <= rx + rw and ry <= m["y"] <= ry + rh
                ]
            if q_norm:
                observations = [
                    m for m in observations if q_norm in _normalize_label(m["text"])
                ]

            # Reading-order full_text: top-to-bottom, left-to-right with a small
            # row-binning tolerance so wrapped lines collate cleanly.
            row_tolerance = 12
            sorted_obs = sorted(
                observations, key=lambda m: (round(m["y"] / row_tolerance), m["x"])
            )
            full_text = "\n".join(m["text"] for m in sorted_obs)

            payload = {
                "ok": True,
                "via": "ocr",
                "level": level_str,
                "count": len(observations),
                "observations": observations,
                "full_text": full_text,
            }
            warn = _focus_warning_from(focus_status)
            if warn is not None:
                payload["focus_warning"] = warn
            return [types.TextContent(type="text", text=json.dumps(payload))]

        # --- click_element ---
        elif name == "click_element":
            session, _ = await _get_session(args, name)
            query = _normalize_label(args["label"])
            index = int(args.get("index", 0))

            # Optional window filter: scope the AX scan (and OCR fallback's screenshot)
            # to a single window of this app. Without it, multi-window apps surface every
            # label match across all windows, forcing the agent to enumerate index=0,1,2…
            filter_wid = _resolve_window(args, args["app"])
            filter_bounds: tuple[int, int, int, int] | None = None
            if filter_wid is not None:
                win = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: capture.get_window_by_id(int(filter_wid))
                )
                if not win or win["pid"] != session.pid:
                    return [types.TextContent(type="text", text=json.dumps({
                        "error": (
                            f"Window {filter_wid} not found or doesn't belong to "
                            f"'{args['app']}'. Call list_windows to refresh labels."
                        ),
                    }))]
                filter_bounds = (
                    win["x"], win["y"],
                    win["x"] + win["width"], win["y"] + win["height"],
                )

            # Tier 1: accessibility tree, search-aware.
            # ax_search_focused walks AXFocusedWindow with one batched IPC
            # per element for label/role/value/children and only spends
            # the second IPC for pos/size when the label actually matches.
            # On real apps this returns in ~100-500 ms (vs ~45 s for the
            # naive walker), inside the klyk hard 1 s tool budget.
            # Misses fall through to OCR — content-area text matches
            # belong there anyway, not in another AX scan.
            def _filter_bounds(els: list[dict]) -> list[dict]:
                els = _filter_for_browser(els, session.app)
                if filter_bounds is not None:
                    x0, y0, x1, y1 = filter_bounds
                    els = [
                        e for e in els
                        if x0 <= e.get("x", 0) <= x1 and y0 <= e.get("y", 0) <= y1
                    ]
                return els

            if filter_bounds is None:
                # Collect a generous candidate set (>= 32) before ranking.
                # The walker returns matches in AX-tree order and stops at the
                # cap, so the cap must be wide enough that an exact label hit
                # isn't truncated away behind incidental substring hits before
                # _rank_ax_matches can promote it. 32 comfortably exceeds the
                # substring-collision count of any real window for a specific
                # label, while the walker's own deadline still bounds latency.
                ax_matches = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: computer.ax_search_focused(
                        session.pid, query, max_results=max(index + 8, 32),
                    ),
                )
                ax_matches = _filter_bounds(ax_matches)
            else:
                # Explicit window filter: walk just that window's bounds
                # via a snapshot (cap=100 keeps the snapshot itself fast)
                # then post-filter to the query.
                elements = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: computer.ax_snapshot(session.pid)
                )
                elements = _filter_bounds(elements)
                ax_matches = [
                    e for e in elements
                    if query in _normalize_label(e.get("label", "") or "")
                    or query in _normalize_label(e.get("value", "") or "")
                ]

            # Rank exact label hits ahead of incidental substring hits so the
            # element the agent actually named lands at index 0, regardless of
            # AX-tree order.
            _rank_ax_matches(ax_matches, query)

            if ax_matches:
                # Refresh the window origin so the screen-space AX coords can be
                # reported back to the agent in window-relative space (what every
                # other tool returns), and so SkyLight delivery below translates
                # against a current origin. One CGWindowList read — negligible
                # against the AX search that just ran.
                await _refresh_window(session, window_id=filter_wid)
                if index >= len(ax_matches):
                    return [types.TextContent(type="text", text=json.dumps({
                        "error": f"Index {index} out of range — {len(ax_matches)} match(es) found.",
                        "matches": [_win_rel(e, session) for e in ax_matches],
                    }))]
                elem = ax_matches[index]
                # Seamless mode: layered invisible-delivery cascade so the
                # majority of real elements click without cursor movement.
                #   1. AX action chain (AXPress → AXOpen) on the matched
                #      element. AXPress covers buttons/links; AXOpen covers
                #      rows/files that don't AXPress (Finder, Mail).
                #   2. Same chain on up to 2 parent levels — Finder sidebar
                #      rows expose AXOpen on AXRow, not on the inner
                #      AXStaticText that matched the label.
                #   3. SkyLight click at the matched element's coords. AX
                #      matched the label so the target is correct; only the
                #      AX-action API can't trigger it. SkyLight delivers
                #      invisibly without re-OCR'ing.
                #   4. Companion: bail with structured error. Autonomous:
                #      log escalation + fall through to cursor-warp.
                if session.mode in ("background", "autonomous"):
                    ax_result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: computer.ax_resolve_and_act(
                            float(elem["x"]), float(elem["y"]),
                            action_chain=("AXPress", "AXOpen"),
                            max_levels_up=2,
                        ),
                    )
                    if ax_result.get("ok"):
                        return [types.TextContent(type="text", text=json.dumps({
                            "ok": True,
                            "clicked": _win_rel(elem, session),
                            "matches_found": len(ax_matches),
                            "via": "ax_action",
                            "action": ax_result.get("action"),
                            "level": ax_result.get("level"),
                        }))]

                    # AX-action chain exhausted at element + parents. Try
                    # SkyLight click at the matched coords next.
                    if skylight.is_available():
                        target_wid = filter_wid if filter_wid is not None else int(session.window_id)
                        await _refresh_window(session, window_id=filter_wid)
                        # AX coords are screen-space (kAXValueCGPointType is
                        # absolute), but SkyLight's post_mouse_click expects
                        # window-local — translate before delivery. Symmetric
                        # with drag_to_element's same-shape fix in 6b6c801.
                        wlx = float(elem["x"]) - float(session.win_x)
                        wly = float(elem["y"]) - float(session.win_y)
                        seamless_result = await _seamless_click(
                            session, target_wid, wlx, wly,
                            "left", "click_element",
                        )
                        if seamless_result.get("ok"):
                            return [types.TextContent(type="text", text=json.dumps({
                                "ok": True,
                                "clicked": _win_rel(elem, session),
                                "matches_found": len(ax_matches),
                                "via": f"ax_match+{seamless_result['via']}",
                                "ax_actions_unsupported": ax_result.get("available_actions", {}),
                            }))]
                        if seamless_result.get("requires_foreground"):
                            seamless_result["matched_element"] = _win_rel(elem, session)
                            return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                        # Unrecoverable SkyLight failure (rare). Companion
                        # bails loudly; autonomous logs and falls through.
                        if session.mode == "background":
                            return [types.TextContent(type="text", text=json.dumps({
                                "ok": False,
                                "requires_foreground": True,
                                "reason": "skylight_post_failed",
                                "matched_element": _win_rel(elem, session),
                                "skylight_error": seamless_result.get("error"),
                                "ax_actions_unsupported": ax_result.get("available_actions", {}),
                                "suggestion": "Element exposes no AXPress/AXOpen and SkyLight delivery failed. Switch to mode='autonomous' to allow cursor-warp fallback.",
                            }))]
                        _log_escalation(session, "click_element", elem.get("x"), elem.get("y"),
                                        seamless_result.get("error", "skylight_unknown"))
                    elif session.mode == "background":
                        # SkyLight not loaded on this system + no AX action.
                        return [types.TextContent(type="text", text=json.dumps({
                            "ok": False,
                            "requires_foreground": True,
                            "reason": "ax_no_action_skylight_unavailable",
                            "clicked_target": _win_rel(elem, session),
                            "ax_actions_unsupported": ax_result.get("available_actions", {}),
                            "suggestion": "Element exposes no AXPress/AXOpen action and SkyLight is unavailable on this system. Switch to mode='autonomous' to allow cursor-warp fallback.",
                        }))]
                    else:
                        _log_escalation(session, "click_element", elem.get("x"), elem.get("y"),
                                        "ax_no_action_no_skylight")
                # Compat (or autonomous fall-through after every invisible
                # path failed). ax_snapshot coords are screen-space.
                await computer.click(elem["x"], elem["y"])
                return [types.TextContent(type="text", text=json.dumps({
                    "ok": True,
                    "clicked": _win_rel(elem, session),
                    "matches_found": len(ax_matches),
                    "via": "ax",
                }))]

            # Tier 2: on-device OCR. Re-screenshot the window and scan for the
            # query as visible text. Catches anything rendered outside the AX
            # tree (canvas surfaces, Electron, browser content without forced a11y).
            # Three-tier under the hood: fast substring → accurate substring →
            # whitespace-collapsed exact (rescues a word Vision fragmented).
            if ocr.is_available():
                img_b64, _, _, _focus = await _take_screenshot(session, window_id=filter_wid)

                def _ocr_match() -> tuple[list[dict], list[dict], str]:
                    # Returns (matches, observations, via). `observations` is the
                    # richest set scanned — reused to build recovery candidates on
                    # a total miss without a second OCR pass. `via` records how the
                    # match was made so the agent sees it in the result.
                    fast_obs = ocr.recognize_all(img_b64, level=1)
                    fast = [m for m in fast_obs if query in _normalize_label(m["text"])]
                    if fast:
                        return fast, fast_obs, "ocr"
                    # Accurate pass catches small / low-contrast / stylized text
                    # that fast mode drops.
                    acc_obs = ocr.recognize_all(img_b64, level=0)
                    acc = [m for m in acc_obs if query in _normalize_label(m["text"])]
                    if acc:
                        return acc, acc_obs, "ocr"
                    # Last tier: Vision occasionally splits a single rendered word
                    # ("ENTER" → "EN TER") or inserts a stray gap. Match only on
                    # EXACT whitespace-collapsed equality — never substring — so
                    # this strictly rescues the same fragmented token and can't
                    # widen matching to an unrelated element.
                    qz = _collapse_ws(query)
                    if qz:
                        despaced = [
                            m for m in acc_obs
                            if qz == _collapse_ws(_normalize_label(m["text"]))
                        ]
                        if despaced:
                            return despaced, acc_obs, "ocr_despaced"
                    return [], acc_obs, "ocr"

                ocr_matches, ocr_obs, ocr_via = await asyncio.get_event_loop().run_in_executor(
                    None, _ocr_match
                )
                # Same exact-first ranking as the AX tier: a word that exactly
                # matches the query beats one that merely contains it.
                _rank_ocr_matches(ocr_matches, query)
                if ocr_matches:
                    if index >= len(ocr_matches):
                        return [types.TextContent(type="text", text=json.dumps({
                            "error": f"Index {index} out of range — {len(ocr_matches)} OCR match(es) found.",
                            "matches": ocr_matches,
                        }))]
                    m = ocr_matches[index]
                    # Seamless mode: route OCR coord clicks through SkyLight too —
                    # AXPress isn't an option here (OCR found visible text, not an
                    # AX element with an action), but invisible coord delivery still
                    # works the same way the click tool does it.
                    if session.mode in ("background", "autonomous") and skylight.is_available():
                        target_wid = filter_wid if filter_wid is not None else int(session.window_id)
                        await _refresh_window(session, window_id=filter_wid)
                        seamless_result = await _seamless_click(
                            session, target_wid, float(m["x"]), float(m["y"]), "left", "click_element",
                        )
                        if seamless_result.get("ok"):
                            return [types.TextContent(type="text", text=json.dumps({
                                "ok": True,
                                "clicked": m,
                                "matches_found": len(ocr_matches),
                                "via": f"{ocr_via}+{seamless_result['via']}",
                            }))]
                        if seamless_result.get("requires_foreground"):
                            seamless_result["ocr_target"] = m
                            return [types.TextContent(type="text", text=json.dumps(seamless_result))]
                        # Autonomous, SkyLight failed: log + fall through.
                        _log_escalation(session, "click_element", m.get("x"), m.get("y"),
                                        seamless_result.get("error", "skylight_unknown"))
                    # OCR returns window-relative pixel coords — convert to screen.
                    sx, sy = _to_screen(session, m["x"], m["y"])
                    await computer.click(sx, sy)
                    return [types.TextContent(type="text", text=json.dumps({
                        "ok": True,
                        "clicked": m,
                        "matches_found": len(ocr_matches),
                        "via": ocr_via,
                    }))]

                # Nothing matched in AX or OCR. Don't dead-end: hand back the
                # closest visible on-screen text (ranked, with window-relative
                # coords) so the agent can retry with the exact spelling or click
                # the coordinates directly — instead of looping blind. Critical
                # for small/fast models on web/Electron surfaces where the AX
                # tree is thin. See Design Considerations #2 (fail loudly) and
                # #10 (return enough evidence to decide the next move).
                candidates = _ocr_candidates(ocr_obs, query)
                payload = {
                    "error": (
                        f"No element found matching '{args['label']}' in the "
                        "accessibility tree or in visible on-screen text."
                    ),
                }
                if candidates:
                    payload["visible_text_candidates"] = candidates
                    payload["hint"] = (
                        "Closest on-screen text is listed above (x/y are "
                        "window-relative pixels). If your target is among them "
                        "under a different spelling, call click_element again with "
                        "that exact text, or click(x, y) at its coordinates. "
                        "Otherwise call ax_snapshot() to list interactive elements."
                    )
                else:
                    payload["hint"] = (
                        "Call ax_snapshot() to see available elements, or "
                        "get_template + find_template for pixel-based targeting."
                    )
                return [types.TextContent(type="text", text=json.dumps(payload))]

            return [types.TextContent(type="text", text=json.dumps({
                "error": (
                    f"No element found matching '{args['label']}' in the accessibility "
                    "tree or in visible on-screen text. Call ax_snapshot() to see what is "
                    "available, or use get_template + find_template for pixel-based targeting."
                ),
            }))]

        # --- get_template ---
        elif name == "get_template":
            session, _ = await _get_session(args, name)
            screenshot_b64, _, _, _focus = await _take_screenshot(session)
            x1, y1 = int(args["x1"]), int(args["y1"])
            x2, y2 = int(args["x2"]), int(args["y2"])
            template_b64 = await asyncio.get_event_loop().run_in_executor(
                None, lambda: matcher.crop(screenshot_b64, x1, y1, x2, y2)
            )
            # Cache in session so the agent can reference by id and avoid
            # round-tripping the full base64 (which is fragile at scale).
            template_id = f"tpl_{uuid.uuid4().hex[:12]}"
            if len(session.template_cache) >= 50:
                session.template_cache.pop(next(iter(session.template_cache)))
            session.template_cache[template_id] = template_b64
            payload = {
                "template_id": template_id,
                "region": [x1, y1, x2, y2],
                "size": [x2 - x1, y2 - y1],
            }
            # Raw b64 is opt-in — at ~5-50 KB per template, returning it by
            # default was paid on every call by every agent even though most
            # only ever use the template_id.
            if args.get("include_b64", False):
                payload["template_b64"] = template_b64
            return [types.TextContent(type="text", text=json.dumps(payload))]

        # --- find_template ---
        elif name == "find_template":
            session, _ = await _get_session(args, name)
            screenshot_b64, _, _, _focus = await _take_screenshot(session)
            template_id = args.get("template_id")
            template_b64 = args.get("template_b64")
            if template_id:
                cached = session.template_cache.get(template_id)
                if cached is None:
                    return [types.TextContent(type="text", text=json.dumps({
                        "error": (
                            f"Unknown template_id '{template_id}'. Template cache is "
                            "per-session and capped at 50 entries — call get_template "
                            "again to refresh, or pass template_b64 directly."
                        ),
                    }))]
                # LRU touch: mark this template most-recently-used so an actively
                # reused one isn't evicted by 50 newer insertions (eviction drops
                # the front, i.e. the least-recently-used entry).
                session.template_cache[template_id] = session.template_cache.pop(template_id)
                template_b64 = cached
            elif not template_b64:
                return [types.TextContent(type="text", text=json.dumps({
                    "error": "find_template requires either 'template_id' or 'template_b64'.",
                }))]
            threshold = float(args.get("threshold", 0.8))
            search_region = args.get("search_region")
            if search_region is not None:
                search_region = tuple(int(v) for v in search_region)
            # Pass threshold=None so matcher returns the absolute best match with its
            # confidence regardless of whether it crossed the threshold. This lets the
            # response surface `last_confidence` on misses — the agent can then decide
            # whether 0.78 was a near-miss worth lowering the threshold for, or 0.12
            # was hopeless and the template needs recapturing.
            best = await asyncio.get_event_loop().run_in_executor(
                None, lambda: matcher.find(screenshot_b64, template_b64, None, search_region)
            )
            if best is None or best["confidence"] < threshold:
                payload = {
                    "ok": True,
                    "found": False,
                    "threshold": threshold,
                    "message": (
                        "No region of the screenshot matched the template above the confidence "
                        "threshold. The element may have moved off-screen, changed appearance "
                        "(theme, hover state, animation), or the threshold may be too strict — "
                        "try lowering it to 0.7 or recapturing the template."
                    ),
                }
                if best is not None:
                    payload["last_confidence"] = best["confidence"]
                    payload["last_box"] = best["box"]
                # Occlusion check: find_template's internal screenshot is a
                # composited region capture, so if another window covers this one
                # the captured pixels are the OCCLUDER's — a "no match" then means
                # "covered", not "gone". Surface it (as screenshot does) so the
                # agent raises the window rather than concluding the element
                # disappeared.
                try:
                    occ = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: capture.window_occluders(int(session.window_id), session.pid),
                    )
                except Exception:
                    occ = []
                if occ:
                    names = ", ".join(o["owner_name"] for o in occ)
                    payload["overlap_warning"] = (
                        f"Another window overlaps this one ({names}); the internal "
                        "capture may show its pixels, not the target — the template "
                        "is likely just covered, not gone. focus_window to raise this "
                        "window (or activate the app), then retry."
                    )
                return [types.TextContent(type="text", text=json.dumps(payload))]
            return [types.TextContent(type="text", text=json.dumps({
                "ok": True,
                "found": True,
                **best,
            }))]

        else:
            return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    try:
        response = await _dispatch()
    except Exception as e:
        log.error(f"tool {name}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        # Agent-facing payload carries the message only — the Python exception
        # type and traceback are a technical identifier the agent can't act on
        # and are already in log.error above.
        response = [types.TextContent(type="text", text=json.dumps({"ok": False, "error": str(e)}))]
    finally:
        duration_ms = round((time.monotonic() - start) * 1000)
        _call_depth -= 1
        # Maintain the post-mutation settle flag at the leaf level. `run` is
        # skipped — its sub-actions (which re-enter call_tool) already set it,
        # and the wrapper finishing must not clobber the last leaf's value. A
        # mutating action that actually landed arms the next capture's repaint
        # wait; any other leaf clears it so passive observation stays instant.
        if name != "run":
            _last_action_mutated = (
                name in _BATCHABLE_ACTIONS and _response_indicates_ok(response)
            )
        if is_top_level:
            _last_response_time = time.monotonic()
            # Hint: cheap pure-Python pattern check on recent call history.
            hint = _detect_hint(name, args)
            # Verify: opt-in cheap focused-state probe after a batchable
            # action. Skip if the action itself failed — verify on a
            # failed click is misleading. Skip on `run` because each
            # nested step already has its own opportunity to set verify.
            verify_data: dict | None = None
            if (
                args.get("verify")
                and name in _BATCHABLE_ACTIONS
                and _response_indicates_ok(response)
            ):
                verify_data = await _post_action_verify(args.get("app"))
            _inject_meta(
                response,
                duration_ms=duration_ms,
                gap_ms=gap_ms,
                hint=hint,
                verify=verify_data,
            )
            _record_call(name)
        gap_str = f" gap_ms={gap_ms}" if gap_ms is not None else ""
        depth_str = "" if is_top_level else " nested=1"
        log.info(f"done: {name} duration_ms={duration_ms}{gap_str}{depth_str}")
    return response


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _install_signal_handlers() -> None:
    """
    Translate SIGTERM/SIGINT into a clean, prompt exit.

    An MCP client that stops klyk by sending SIGTERM (rather than closing
    stdin) would, under Python's default handler, terminate the process
    *without* running atexit — leaving any clipboard klyk borrowed for a
    paste un-restored. The handler instead restores the clipboard, then
    hard-exits promptly, so we never leave a borrowed clipboard or a zombie
    process behind.

    Must run on the main thread. The 20 ms AppKit drain timer keeps the
    interpreter checking signals even while NSApp.run blocks, so delivery
    stays prompt (~one tick).
    """
    import signal

    def _graceful_exit(_signum, _frame):
        try:
            from .computer import _flush_clipboard_restore
            _flush_clipboard_restore()
        except Exception:
            pass
        os._exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful_exit)
        except (ValueError, OSError):
            # signal() only works on the main thread; skip silently if not.
            pass


def _install_parent_death_watch() -> None:
    """
    Exit if the MCP client that spawned us dies.

    Normally a client stops klyk by closing stdin, which the worker sees
    as EOF and shuts down cleanly. This is the backstop for a client that's
    hard-killed (crash, Force Quit) where the EOF never arrives or the
    stdin reader is wedged. klyk would otherwise linger as an orphan — a
    stray process still showing a menu-bar item and a stale ownership
    record. (It can't block a new session: control is latest-wins, so the
    next session just claims it.) We poll the parent pid; when it changes
    (on orphaning, the OS reparents us to launchd), the client is gone, so
    we restore the clipboard and exit, keeping the environment clean.

    Skipped if we were started without a tracked parent (already pid 1 /
    daemonized), so an intentionally standalone klyk is never killed.
    """
    initial_ppid = os.getppid()
    if initial_ppid <= 1:
        return

    def _watch() -> None:
        while True:
            try:
                if os.getppid() != initial_ppid:
                    try:
                        from .computer import _flush_clipboard_restore
                        _flush_clipboard_restore()
                    except Exception:
                        pass
                    os._exit(0)
            except Exception:
                pass
            time.sleep(2.0)

    import threading as _threading
    _threading.Thread(target=_watch, name="klyk-parent-watch", daemon=True).start()


def _run_on_macos() -> None:
    """
    macOS entry point.

    AppKit's NSStatusBar / NSWindow APIs assert pthread main-thread, so we
    can't run them from the asyncio event loop (which Python invokes from
    main by default). Instead we flip the threading model: AppKit lives
    on the main thread, asyncio runs on a daemon worker.

    Bootstrap order:
      1. install UI thread (NSApp + activation policy + drain timer)
      2. install the always-on menubar status item
      3. spawn the asyncio worker (MCP stdio server)
      4. block the main thread on NSApp.run() until the worker requests
         shutdown (stdin closed)
    """
    # 1. AppKit on the main thread, idempotent.
    _ui.install_on_main_thread()
    # 1a. Signal handlers — turn SIGTERM/SIGINT into a clean, prompt exit
    #     that restores the clipboard and leaves no zombie behind.
    _install_signal_handlers()
    # 1b. Parent-death watch — if the client is hard-killed, exit so we
    #     don't linger as a stray process / stale menu-bar item.
    _install_parent_death_watch()
    # 2. Menu-bar status item — always-on; shows klyk is alive even when
    #    no session has been created yet. Per-app dock-tile badges appear
    #    automatically whenever a session opens (no opt-in).
    try:
        from .menubar import menubar as _menubar
        _menubar.install_if_needed()
    except Exception as e:
        log.warning("menubar install failed at startup: %s", e)

    # 2b. SkyLight delivery self-test — confirm the invisible-input path doesn't
    #     just LOAD but actually DELIVERS on this macOS build. Runs its own
    #     bounded NSApp loop on the main thread (off-screen sink, no focus
    #     change) BEFORE the worker serves any tool, so delivery_verified() is
    #     populated before the first click. If delivery is broken (e.g. a macOS
    #     update changed the private API), the seamless dispatch falls back to
    #     the visible cursor instead of silently no-op'ing. Best-effort and
    #     self-bounding (its finish timer always stops the loop) — never blocks
    #     boot; one retry guards against a transient first-attempt miss.
    try:
        if skylight.is_available():
            verified = skylight.self_test(timeout=0.4) or skylight.self_test(timeout=0.4)
            if not verified and skylight.delivery_verified() is False:
                log.warning(
                    "SkyLight loaded but the delivery self-test failed — seamless "
                    "modes will fall back to the visible cursor on this macOS build. "
                    "Run `klyk doctor` for details."
                )
    except Exception as e:
        log.warning("SkyLight delivery self-test skipped: %s: %s", type(e).__name__, e)

    # 2c. Keyboard-layout warm — build the char→keycode map on the MAIN thread.
    #     Carbon/TIS input-source APIs (used to map characters to layout-correct
    #     keycodes) assert main-thread on macOS 14+; the old per-call staleness
    #     probe ran them on the asyncio worker thread and intermittently trapped
    #     the process (SIGTRAP) on the input-source-list rebuild path. Warming
    #     once here on the main thread populates the cache before the worker
    #     serves any tool, so char_to_keycode never touches TIS off-main. A
    #     mid-session keyboard-layout switch is handled on demand via
    #     keycodes.refresh_keyboard_layout() — we deliberately do NOT poll TIS on
    #     a timer, keeping all TIS work to this one contention-free moment.
    try:
        from . import keycodes as _keycodes
        _keycodes.warm_keyboard_layout()
    except Exception as e:
        log.warning("keyboard-layout warm failed at startup: %s", e)

    # 3. asyncio worker thread runs the MCP stdio server.
    def _worker() -> None:
        try:
            asyncio.run(main())
        except Exception as e:
            log.error("MCP worker terminated: %s: %s", type(e).__name__, e, exc_info=True)
        finally:
            # Signal AppKit to stop so the main thread can exit cleanly.
            try:
                _ui.shutdown()
            except Exception:
                pass

    import threading as _threading
    worker_thread = _threading.Thread(target=_worker, name="klyk", daemon=False)
    worker_thread.start()

    # 4. Block the main thread on NSApp.run() — returns when worker
    #    finishes and calls _ui.shutdown().
    _ui.run_blocking()

    # Final join with a short grace period.
    worker_thread.join(timeout=2.0)

    # Backstop: if the worker is still alive here it's wedged (asyncio
    # teardown hung, or we unblocked NSApp via a signal while the worker
    # still blocked on stdin). A non-daemon thread would keep the
    # interpreter alive indefinitely, leaving a zombie process (and its
    # menu-bar item) lingering. So guarantee exit: run the one
    # atexit-critical cleanup (clipboard restore) explicitly, then
    # hard-exit. The common path never reaches this — the worker has
    # already finished by the time NSApp.run returns.
    if worker_thread.is_alive():
        log.warning(
            "MCP worker did not exit within grace period; forcing shutdown"
        )
        try:
            from .computer import _flush_clipboard_restore
            _flush_clipboard_restore()
        except Exception:
            pass
        os._exit(0)


def _main_entry() -> None:
    """
    Single platform-agnostic entry point. Takes the control-ownership token
    ONLY if it's free — no live owner — then dispatches to the appropriate
    runner. It NEVER blocks on the token: the MCP connection always starts and
    serves, so a client can't see "failed to connect". Claiming only-if-unowned
    (rather than unconditionally) is deliberate: many klyk server processes can
    coexist (every MCP client gets its own, and a client respawns its server on
    reconnect), and if each grabbed the token at startup they'd thrash control
    away from whichever instance is mid-task. So a fresh server takes control
    when the previous session is gone (the common case), but a live, active
    driver keeps it — switching sessions is an explicit `take_control`. A
    non-owner stays fully connected and is blocked only when it tries a control
    action (the ownership gate in call_tool). Used by both the package entry
    (`python -m klyk.mcp_server`) and the legacy shim at the repo root.
    """
    from . import ownership
    ownership.claim_ownership_if_unowned()  # take control only if it's free
    if sys.platform == "darwin":
        _run_on_macos()
    else:
        # Non-darwin builds are not officially supported (klyk is macOS
        # only — see pyproject classifiers + cli.py guard), but keep the
        # asyncio-only path so module-level imports stay testable on
        # Linux dev environments.
        asyncio.run(main())


if __name__ == "__main__":
    _main_entry()
