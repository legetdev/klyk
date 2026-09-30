"""
UI grading: returns the screenshot + platform-appropriate grading criteria.
No internal AI calls — the calling agent evaluates using its vision.
"""

from __future__ import annotations
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session

_CRITERIA_BASE = [
    "Visual hierarchy — primary actions are immediately obvious",
    "Spacing and padding — consistent, not cramped or bloated",
    "Typography — readable sizes, appropriate weight and contrast",
    "Color contrast — WCAG AA minimum (4.5:1 for normal text)",
    "Alignment — elements adhere to an implicit grid",
    "Completeness — no broken images, placeholder text, or missing elements",
    "Polish — looks like a finished, shippable product",
]

_CRITERIA_MACOS_EXTRA = [
    "macOS materials — window uses vibrancy/system materials where appropriate",
    "HIG compliance — controls follow macOS Human Interface Guidelines sizing and placement",
    "Native feel — does not look like a web UI running inside a frame",
]

_CRITERIA_WEB_EXTRA = [
    "Responsiveness — layout adapts correctly at the current window width",
    "Loading states — spinners or skeletons shown during async operations",
]

CRITERIA_BY_PLATFORM = {
    "native":   _CRITERIA_BASE + _CRITERIA_MACOS_EXTRA,
    "electron": _CRITERIA_BASE + _CRITERIA_MACOS_EXTRA,
    "web":      _CRITERIA_BASE + _CRITERIA_WEB_EXTRA,
}


def ui_pass_threshold() -> tuple[float, str | None]:
    """Keep malformed owner configuration from breaking grading or evidence output."""
    try:
        threshold = float(os.getenv("KLYK_UI_PASS_THRESHOLD", "7.0"))
        if math.isfinite(threshold) and 0 <= threshold <= 10:
            return threshold, None
    except (TypeError, ValueError):
        pass
    return 7.0, "UI pass threshold must be a finite number from 0 to 10; using the default 7.0."


def grade_ui(session: "Session") -> dict:
    """Capture the current window geometry and return platform grading criteria."""
    from . import capture

    win = capture.get_window_by_id(session.window_id)
    if not win or win["pid"] != session.pid:
        raise RuntimeError("The selected window is unavailable; inspect and select a current window before requesting a grade.")
    session.win_x, session.win_y = int(win["x"]), int(win["y"])
    session.width, session.height = int(win["width"]), int(win["height"])

    screenshot_b64, w, h = capture.take_screenshot(
        window_id=session.window_id,
        logical_width=session.width,
        logical_height=session.height,
        win_x=session.win_x,
        win_y=session.win_y,
    )

    threshold, warning = ui_pass_threshold()
    criteria = CRITERIA_BY_PLATFORM.get(session.target, _CRITERIA_BASE)

    result = {
        "screenshot": screenshot_b64,
        "width": w,
        "height": h,
        "platform": session.target,
        "criteria": criteria,
        "pass_threshold": threshold,
        "instruction": (
            f"Score this {session.target} UI from 0.0 to 10.0 against the criteria above. "
            f"Score >= {threshold} is a pass. "
            "Identify specific issues. "
            "Respond with: score, issues list, passed (bool)."
        ),
    }
    if warning:
        result["configuration_warning"] = warning
    return result
