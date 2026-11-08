"""System Settings pane names as the running macOS spells them.

macOS 15 renamed Screen Recording to "Screen & System Audio Recording" and macOS 27
renamed the Accessibility privacy pane to "Device Control and Data Access". Setup
and repair messages use the current names so users find the right switch.
"""

from __future__ import annotations

import platform


def _major() -> int:
    """Major macOS version, or 0 when unknown (non-macOS hosts and tests)."""
    try:
        return int((platform.mac_ver()[0] or "0").split(".")[0])
    except ValueError:
        return 0


def accessibility_pane() -> str:
    """Privacy & Security pane that grants Accessibility control."""
    return "Device Control and Data Access" if _major() >= 27 else "Accessibility"


def screen_recording_pane() -> str:
    """Privacy & Security pane that grants screen capture."""
    return "Screen & System Audio Recording" if _major() >= 15 else "Screen Recording"
