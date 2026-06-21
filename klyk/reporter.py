"""
Verdict: aggregates all session evidence and returns it so the calling agent can synthesize PASS/FAIL.
No internal AI calls — the agent calling this tool already has vision and reasoning.
"""

from __future__ import annotations
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session


def generate_verdict(session: "Session", test_description: str) -> dict:
    """
    Take a fresh screenshot and aggregate all session evidence.
    The calling agent (Claude) synthesizes the PASS/FAIL verdict.
    Returns: {screenshot, width, height, logs, test_description, pass_threshold, instruction}
    """
    from . import capture

    win = capture.get_window_for_pid(session.pid)
    if win:
        session.window_id = win["window_id"]
        session.win_x = int(win["bounds"]["X"])
        session.win_y = int(win["bounds"]["Y"])
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
    # Cap the log payload so a chatty app can't blow the verdict token budget
    # (the final screenshot already costs a lot); most-recent lines are kept.
    logs = session.log_buffer.to_dict(max_chars=12000)

    return {
        "screenshot": screenshot_b64,
        "width": w,
        "height": h,
        "test_description": test_description,
        "logs": logs,
        "screenshots_taken": session.screenshots_taken,
        "pass_threshold": threshold,
        "instruction": (
            "Based on the screenshot and evidence above, determine if this app PASSES or FAILS. "
            f"PASS requires: UI score >= {threshold}, zero console errors, zero network failures, "
            "no broken states visible. "
            "Respond with: result ('PASS'/'FAIL'), ui_score, ui_issues, functional_errors, recommendation."
        ),
    }
