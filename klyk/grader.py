"""
UI grading: returns the screenshot + platform-appropriate grading criteria.
No internal AI calls — the calling agent evaluates using its vision.
"""

from __future__ import annotations
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


def grade_ui(session: "Session") -> dict:
    from . import capture

    win = capture.get_window_for_pid(session.pid)
    if win:
        session.window_id = win["window_id"]
        session.width = int(win["bounds"]["Width"])
        session.height = int(win["bounds"]["Height"])

    screenshot_b64, w, h = capture.take_screenshot(
        window_id=session.window_id,
        logical_width=session.width,
        logical_height=session.height,
        win_x=session.win_x,
        win_y=session.win_y,
    )

    threshold = float(os.getenv("KLYK_UI_PASS_THRESHOLD", "7.0"))
    criteria = CRITERIA_BY_PLATFORM.get(session.target, _CRITERIA_BASE)

    return {
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
