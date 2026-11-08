"""Model-ready screenshot frames: one coordinate space for images, inputs and results.

Vision models resize large images before they see them, and then answer in the
resized frame. Klyk therefore sizes every agent-facing window image itself and
uses that image's pixels as the coordinate space for every window-relative
input and result. A window that already fits is unchanged (scale 1).

The default budget is the strictest current mainstream model tier: at most 1568
pixels on the long edge and 1568 28-pixel tiles. Images inside it are passed
through unchanged by every current model provider, so coordinates read from the
image are the coordinates the agent sends back. KLYK_IMAGE_TIER=high raises the
budget to 2576 pixels / 4784 tiles for models documented to accept it.

Pure functions only: no native imports, no hidden state.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

_PATCH = 28
_TIERS = {
    "standard": (1568, 1568),
    "high": (2576, 4784),
}


def budget() -> tuple[int, int]:
    """Return (max long edge, max tiles) for the configured tier; unknown values use standard."""
    tier = os.environ.get("KLYK_IMAGE_TIER", "standard").strip().lower()
    return _TIERS.get(tier, _TIERS["standard"])


def _fits(width: int, height: int, max_edge: int, max_tiles: int) -> bool:
    """True when an image is accepted without any provider-side resize."""
    return (max(width, height) <= max_edge
            and math.ceil(width / _PATCH) * math.ceil(height / _PATCH) <= max_tiles)


def fit(width: int, height: int, max_edge: int | None = None, max_tiles: int | None = None) -> tuple[int, int]:
    """Return the largest same-aspect size that fits the budget, never enlarging."""
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise ValueError("Frame dimensions must be positive")
    width, height = int(width), int(height)
    default_edge, default_tiles = budget()
    max_edge = default_edge if max_edge is None else max_edge
    max_tiles = default_tiles if max_tiles is None else max_tiles
    if _fits(width, height, max_edge, max_tiles):
        return width, height
    # Binary search the long edge, preserving aspect ratio with round-half-even.
    long_side = max(width, height)
    low, high = 1, min(long_side, max_edge)
    best = (1, 1)
    while low <= high:
        edge = (low + high) // 2
        scale = edge / long_side
        candidate = (max(1, round(width * scale)), max(1, round(height * scale)))
        if _fits(*candidate, max_edge, max_tiles):
            best = candidate
            low = edge + 1
        else:
            high = edge - 1
    return best


@dataclass(frozen=True)
class Frame:
    """Map between a window's logical points and its agent-facing image pixels."""

    width: int
    height: int
    image_width: int
    image_height: int

    @classmethod
    def for_window(cls, width: int, height: int) -> "Frame":
        """Build the frame Klyk uses for a window of this logical size."""
        image_width, image_height = fit(width, height)
        return cls(int(width), int(height), image_width, image_height)

    @property
    def identity(self) -> bool:
        """True when image pixels and logical points coincide."""
        return self.image_width == self.width and self.image_height == self.height

    @property
    def scale(self) -> float:
        """Image pixels per logical point (rounded for reporting)."""
        return round(self.image_width / self.width, 4)

    def to_logical(self, value: float, axis: str) -> float:
        """Convert an agent coordinate or length to logical window points."""
        if axis == "x":
            return value * self.width / self.image_width
        return value * self.height / self.image_height

    def to_image(self, value: float, axis: str) -> int:
        """Convert a logical coordinate or length to agent image pixels."""
        if axis == "x":
            return int(round(value * self.image_width / self.width))
        return int(round(value * self.image_height / self.height))


# ---------------------------------------------------------------------------
# Input conversion: which window-relative arguments each tool accepts.
# ---------------------------------------------------------------------------

_POINT = (("x", "x"), ("y", "y"))
INPUTS: dict[str, tuple] = {
    **{name: _POINT for name in (
        "click", "double_click", "triple_click", "long_press", "ax_action", "read_element",
        "get_pixel", "fill_field", "scroll", "move_cursor", "context_menu_select", "select_option",
    )},
    "drag": (("x1", "x"), ("y1", "y"), ("x2", "x"), ("y2", "y")),
    "read_grid": (("x", "x"), ("y", "y"), ("cell_width", "x"), ("cell_height", "y"), ("cell_gap", "x")),
    "read_text": (("x", "x"), ("y", "y"), ("width", "x"), ("height", "y")),
    "get_template": (("x1", "x"), ("y1", "y"), ("x2", "x"), ("y2", "y")),
}
_BOX_INPUTS = {"find_template": "search_region", "wait_for_visual": "search_region", "zoom": "region"}
_LIST_INPUTS = {"get_pixels": (("points", ("x", "y")), ("regions", ("x", "y", "width", "height")))}

# Tools whose coordinates are window-relative; everything else is screen space or none.
WINDOW_TOOLS = frozenset({
    *INPUTS, *_BOX_INPUTS, *_LIST_INPUTS, "inspect", "screenshot", "ax_snapshot", "wait_for",
    "click_element", "drag_to_element", "find_template", "wait_for_visual",
})
# Result keys a tool reports in screen points rather than window pixels.
_SCREEN_RESULT_KEYS = {"drag_to_element": frozenset({"source", "target", "matches", "source_matches",
                                                     "target_matches", "candidates"})}
_INTEGER_INPUTS = frozenset({("get_template", "x1"), ("get_template", "y1"), ("get_template", "x2"),
                             ("get_template", "y2")})


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def has_inputs(tool: str, args: dict) -> bool:
    """True when the call carries window coordinates that a frame would convert."""
    if any(key in args for key, _ in INPUTS.get(tool, ())):
        return True
    if _BOX_INPUTS.get(tool) in args:
        return True
    return any(key in args for key, _ in _LIST_INPUTS.get(tool, ()))


def args_to_logical(tool: str, args: dict, frame: Frame | None) -> dict:
    """Return a copy of args with agent image coordinates converted to logical points."""
    if frame is None or frame.identity or tool not in WINDOW_TOOLS:
        return args
    result = dict(args)

    def convert(key, value, axis, integer=False):
        converted = frame.to_logical(value, axis)
        return int(round(converted)) if integer else converted

    for key, axis in INPUTS.get(tool, ()):
        if _number(result.get(key)):
            result[key] = convert(key, result[key], axis, (tool, key) in _INTEGER_INPUTS)
    box_key = _BOX_INPUTS.get(tool)
    if box_key and isinstance(result.get(box_key), list) and len(result[box_key]) == 4 \
            and all(_number(v) for v in result[box_key]):
        x1, y1, x2, y2 = result[box_key]
        result[box_key] = [int(round(frame.to_logical(x1, "x"))), int(round(frame.to_logical(y1, "y"))),
                           int(round(frame.to_logical(x2, "x"))), int(round(frame.to_logical(y2, "y")))]
    for list_key, fields in _LIST_INPUTS.get(tool, ()):
        items = result.get(list_key)
        if isinstance(items, list):
            converted = []
            for item in items:
                if isinstance(item, dict):
                    item = dict(item)
                    for field_name in fields:
                        if _number(item.get(field_name)):
                            axis = "x" if field_name in ("x", "width") else "y"
                            item[field_name] = frame.to_logical(item[field_name], axis)
                converted.append(item)
            result[list_key] = converted
    return result


# ---------------------------------------------------------------------------
# Output conversion: window-relative geometry in results.
# ---------------------------------------------------------------------------

_X_KEYS = frozenset({"x", "width"})
_Y_KEYS = frozenset({"y", "height"})
_BOX_KEYS = frozenset({"region", "search_region", "requested_region", "box", "last_box"})
# Subtrees that are screen-space, non-geometric, or already converted.
_SKIP_KEYS = frozenset({"_meta", "focus_warning", "verify", "display", "displays", "main",
                        "windows", "frame", "win_x", "win_y"})


def result_to_image(tool: str, payload, frame: Frame | None):
    """Convert window-relative geometry in a parsed result to agent image pixels."""
    if frame is None or frame.identity or tool not in WINDOW_TOOLS:
        return payload

    skip = _SKIP_KEYS | _SCREEN_RESULT_KEYS.get(tool, frozenset())

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key in skip:
                out[key] = item
            elif key in _X_KEYS and _number(item):
                out[key] = frame.to_image(item, "x")
            elif key in _Y_KEYS and _number(item):
                out[key] = frame.to_image(item, "y")
            elif key in _BOX_KEYS and isinstance(item, list) and len(item) == 4 and all(_number(v) for v in item):
                out[key] = [frame.to_image(item[0], "x"), frame.to_image(item[1], "y"),
                            frame.to_image(item[2], "x"), frame.to_image(item[3], "y")]
            elif key == "size" and isinstance(item, list) and len(item) == 2 and all(_number(v) for v in item):
                out[key] = [frame.to_image(item[0], "x"), frame.to_image(item[1], "y")]
            else:
                out[key] = walk(item)
        return out

    return walk(payload)
