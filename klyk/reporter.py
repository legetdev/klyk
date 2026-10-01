"""
Verdict: aggregates all session evidence and returns it so the calling agent can synthesize PASS/FAIL.
No internal AI calls — the agent calling this tool already has vision and reasoning.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from .grader import ui_pass_threshold

if TYPE_CHECKING:
    from .session import Session


def generate_verdict(session: "Session", test_description: str) -> dict:
    """
    Take a fresh screenshot and aggregate all session evidence.
    The calling agent (Claude) synthesizes the PASS/FAIL verdict.
    Returns: {screenshot, width, height, logs, test_description, pass_threshold, instruction}
    """
    from . import capture

    win = capture.get_window_by_id(session.window_id)
    if not win or win["pid"] != session.pid:
        raise RuntimeError("The selected window is unavailable; inspect and select a current window before requesting a verdict.")
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
    # Cap the log payload so a chatty app can't blow the verdict token budget
    # (the final screenshot already costs a lot); most-recent lines are kept.
    logs = session.log_buffer.to_dict(max_chars=12000)

    result = {
        "screenshot": screenshot_b64,
        "width": w,
        "height": h,
        "test_description": test_description,
        "logs": logs,
        "screenshots_taken": session.screenshots_taken,
        "pass_threshold": threshold,
        "instruction": (
            "Assess the stated test using the screenshot, observed action outcomes, and available logs. "
            "Separate confirmed results from missing or ambiguous evidence; report unverified areas. "
            "Empty error lists do not prove console or network health, and diagnostic log lines are "
            "not necessarily app failures. Do not infer successful completion from action delivery. "
            f"If you score visual quality, explain the judgement (configured threshold: {threshold}). "
            "Respond with result (PASS, FAIL, or UNVERIFIED), evidence, limitations, and recommendation."

        ),
    }
    if warning:
        result["configuration_warning"] = warning
    return result
