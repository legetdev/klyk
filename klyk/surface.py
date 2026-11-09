"""Accept the argument shapes computer-use models are trained on, without new tools.

Models trained on other computer-use harnesses reach for their own spellings:
`coordinate: [x, y]`, `start_coordinate`, `scroll_direction`, `text` for key
chords, uppercase key arrays such as ["CTRL", "C"], `element_index`, `ref_12`.
Klyk's schemas stay canonical; this module maps those unambiguous synonyms onto
them before validation, so a familiar call succeeds in one pass instead of
failing on a spelling. Canonical arguments always win; nothing is guessed.

Pure functions only (no native imports), so every mapping is unit tested.
"""

from __future__ import annotations

import re

_MODIFIERS = {
    "cmd": "cmd", "command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd", "windows": "cmd",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
}


def _modifier_name(name) -> str | None:
    """Canonical click/drag modifier for one key name, or None if it is not a modifier."""
    if not isinstance(name, str):
        return None
    key = re.sub(r"[\s_]", "", name.strip().lower())
    key = re.sub(r"(l|r|left|right)$", "", key) if key not in _MODIFIERS else key
    return _MODIFIERS.get(key)


def modifiers_from(value) -> list[str]:
    """Parse 'ctrl+shift', ['CTRL', 'SHIFT'] or ['cmd'] into klyk modifier names."""
    if value is None:
        return []
    parts = value.split("+") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else None
    if parts is None:
        raise ValueError("Modifiers must be a list such as ['cmd', 'shift'] or a string such as 'cmd+shift'.")
    result = []
    for part in parts:
        if isinstance(part, str) and not part.strip():
            continue
        name = _modifier_name(part)
        if name is None:
            raise ValueError(f"Unknown modifier {part!r}; use cmd, shift, alt or ctrl.")
        if name not in result:
            result.append(name)
    return result


def parse_element(value) -> int:
    """Accept 12, '12', 'ref_12', 'e12' or '[12]' as element number 12."""
    if isinstance(value, bool):
        raise ValueError("Element must be a number from inspect.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*\[?\s*(?:ref_?|e|element\s*)?(\d{1,9})\s*\]?\s*", value, re.IGNORECASE)
        if not match:
            raise ValueError(f"Element {value!r} is not an element number from inspect.")
        number = int(match.group(1))
    else:
        raise ValueError("Element must be a number from inspect.")
    if number < 1:
        raise ValueError("Element numbers start at 1.")
    return number


def key_chord(keys) -> str | None:
    """Turn ['CMD', 'SHIFT', 'T'] (modifiers, then one key) into 'cmd+shift+T'; else None."""
    if not isinstance(keys, list) or len(keys) < 2 or not all(isinstance(k, str) and k for k in keys):
        return None
    modifiers = [_modifier_name(k) for k in keys[:-1]]
    if any(m is None for m in modifiers) or _modifier_name(keys[-1]) is not None:
        return None
    return "+".join([*dict.fromkeys(modifiers), keys[-1]])


def _move(args: dict, source: str, target: str) -> None:
    """Rename one argument when the canonical name is absent."""
    if source in args and target not in args:
        args[target] = args.pop(source)
    elif source in args:
        args.pop(source)


def _point(args: dict, source: str, x_key: str, y_key: str) -> None:
    """Expand a [x, y] pair argument when the canonical coordinates are absent."""
    value = args.get(source)
    if isinstance(value, (list, tuple)) and len(value) == 2 and x_key not in args and y_key not in args:
        args.pop(source)
        args[x_key], args[y_key] = value[0], value[1]
    elif isinstance(value, dict) and {"x", "y"} <= set(value) and x_key not in args and y_key not in args:
        args.pop(source)
        args[x_key], args[y_key] = value["x"], value["y"]


_POINT_TOOLS = frozenset({"click", "fill_field", "scroll", "read_element", "ax_action", "move_cursor",
                          "long_press", "select_option", "context_menu_select"})


def normalize(tool: str, arguments: dict | None) -> dict:
    """Map familiar synonyms onto one public tool's canonical arguments."""
    args = dict(arguments or {})
    for alias in ("element_index", "ref", "element_id"):
        _move(args, alias, "element")
    _move(args, "confirm_destructive", "allow_outside_window")  # Earlier name of the same switch.
    if "element" in args and not isinstance(args["element"], bool):
        try:
            args["element"] = parse_element(args["element"])
        except ValueError:
            pass  # The schema reports the invalid value.
    if tool in _POINT_TOOLS:
        _point(args, "coordinate", "x", "y")
        _point(args, "position", "x", "y")
    if tool == "click":
        _move(args, "mouse_button", "button")
        _move(args, "clicks", "click_count")
        _move(args, "num_clicks", "click_count")
        for alias in ("text", "keys", "key"):
            if alias in args and "modifiers" not in args and _all_modifiers(args[alias]):
                args["modifiers"] = modifiers_from(args.pop(alias))
        if "modifiers" in args and isinstance(args["modifiers"], (str, list)):
            try:
                args["modifiers"] = modifiers_from(args["modifiers"])
            except ValueError:
                pass
    elif tool == "drag":
        _point(args, "start_coordinate", "x1", "y1")
        _point(args, "coordinate", "x2", "y2")
        for source, target in (("from_x", "x1"), ("from_y", "y1"), ("to_x", "x2"), ("to_y", "y2")):
            _move(args, source, target)
        path = args.get("path")
        if isinstance(path, list) and len(path) == 2 and not {"x1", "y1", "x2", "y2"} & set(args):
            start, end = path
            if all(isinstance(p, dict) and {"x", "y"} <= set(p) for p in path):
                args.pop("path")
                args.update(x1=start["x"], y1=start["y"], x2=end["x"], y2=end["y"])
        for alias in ("text", "keys"):
            if alias in args and "modifiers" not in args and _all_modifiers(args[alias]):
                args["modifiers"] = modifiers_from(args.pop(alias))
    elif tool == "scroll":
        _move(args, "scroll_direction", "direction")
        _move(args, "scroll_amount", "amount")
        if "direction" not in args and ("scroll_x" in args or "scroll_y" in args):
            dx, dy = args.pop("scroll_x", 0) or 0, args.pop("scroll_y", 0) or 0
            if isinstance(dx, (int, float)) and isinstance(dy, (int, float)) and (dx or dy):
                vertical = abs(dy) >= abs(dx)
                delta = dy if vertical else dx
                args["direction"] = ("down" if delta > 0 else "up") if vertical else ("right" if delta > 0 else "left")
                args.setdefault("amount", max(1, min(1000, round(abs(delta) / 50))))
        if "pages" in args and "amount" not in args:
            pages = args.pop("pages")
            if isinstance(pages, (int, float)) and not isinstance(pages, bool) and pages > 0:
                args["amount"] = max(1, min(1000, round(pages * 10)))
    elif tool == "press_key":
        if "key" not in args and "keys" not in args:
            _move(args, "text", "key")
        if "keys" in args and "key" not in args:
            chord = key_chord(args["keys"])
            if chord is not None:
                args.pop("keys")
                args["key"] = chord
    elif tool == "hold_key":
        _move(args, "text", "key")
        _move(args, "seconds", "duration")
    elif tool == "type_text":
        _move(args, "value", "text")
    elif tool == "fill_field":
        _move(args, "value", "text")
    elif tool == "wait":
        _move(args, "duration", "seconds")
        _move(args, "time", "seconds")
    elif tool == "screenshot":
        _move(args, "zoom", "region")
    return args


def _all_modifiers(value) -> bool:
    """True for a nonempty modifier chord such as 'shift', 'ctrl+shift' or ['CMD']."""
    try:
        return bool(modifiers_from(value))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# run steps written in other harnesses' action dialects
# ---------------------------------------------------------------------------

_ANTHROPIC_CLICKS = {
    "left_click": {"button": "left"}, "right_click": {"button": "right"},
    "double_click": {"click_count": 2},
    "triple_click": {"click_count": 3},
}
_UNSUPPORTED = {
    "middle_click": "Klyk delivers left and right clicks; use click with button 'left' or 'right'.",
    "cursor_position": "Klyk never moves your cursor to act, so there is no agent cursor position; inspect instead.",
    "left_mouse_down": "Use drag with hover_seconds, or long_press, instead of separate mouse down/up steps.",
    "left_mouse_up": "Use drag with hover_seconds, or long_press, instead of separate mouse down/up steps.",
}


def translate_step(step: dict) -> dict:
    """Return a run step in klyk's {tool, ...arguments} form.

    Accepts klyk steps unchanged, action objects with `type` (click, double_click,
    drag, move, scroll, keypress, type, wait, screenshot) and step names such as
    left_click, left_click_drag, mouse_move, key, type, zoom.
    """
    if not isinstance(step, dict):
        return step
    step = dict(step)
    if "tool" not in step and isinstance(step.get("type"), str):
        step["tool"] = step.pop("type")
        if step["tool"] == "type":
            step["tool"] = "type_text"
    name = step.get("tool")
    if not isinstance(name, str):
        return step
    if name in _UNSUPPORTED:
        raise ValueError(_UNSUPPORTED[name])
    if name in _ANTHROPIC_CLICKS:
        step.update({"tool": "click", **{k: v for k, v in _ANTHROPIC_CLICKS[name].items() if k not in step}})
    elif name == "left_click_drag":
        step["tool"] = "drag"
    elif name in ("mouse_move", "move"):
        step["tool"] = "move_cursor"
    elif name in ("key", "keypress"):
        step["tool"] = "press_key"
    elif name == "type":
        step["tool"] = "type_text"
    elif name == "zoom":
        step["tool"] = "screenshot"
    elif name == "wait" and not {"seconds", "duration", "text", "template_id", "template_b64"} & set(step):
        step["seconds"] = 1
    return step
