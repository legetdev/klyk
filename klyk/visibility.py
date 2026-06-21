"""
Session-lifecycle hook for activity-log cleanup.

Earlier versions of this module owned a dock-tile badge surface (a colored
dot above each driven app's Dock tile). That surface was removed: the
menu-bar status item is the canonical "what is klyk doing" signal, and
the Dock badges added visual clutter to the user's Dock for marginal
benefit.

What remains: per-session attach / detach hooks that the session
registry calls. attach is a no-op today (kept for symmetry); detach
clears the per-app activity log so a relaunch starts clean.
"""

from __future__ import annotations

import logging

from . import activity

log = logging.getLogger("klyk.visibility")


class _SessionLifecycle:
    """No-op attach; detach clears the activity log."""

    def attach(self, app: str) -> None:
        # Reserved for future per-session surfaces. Currently a no-op so
        # session.py can call this unconditionally without branching on
        # whether any visual surface exists.
        return

    def detach(self, app: str) -> None:
        if not app:
            return
        try:
            activity.recorder.remove_app(app)
        except Exception:
            pass
        try:
            from .menubar import menubar
            menubar.request_refresh()
        except Exception:
            pass


# Module-level singleton — name kept for compatibility with session.py.
visibility = _SessionLifecycle()
