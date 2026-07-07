"""
Menu-bar status item.

A small, always-on indicator that lets the user see at a glance:
  - whether any klyk session is currently active
  - per-app: mode (humanoid/background/autonomous), last action, escalations
  - last 20 actions per app (timestamp + tool + via)
  - quick "stop / reset emergency stop" entry

The status item lives on the AppKit main thread (NSStatusBar requirement);
all manipulation is dispatched via klyk.ui_thread.ui. Subscribers from the
activity recorder run on whatever thread fired the action — they enqueue a
refresh onto the UI thread; the actual NSMenu mutation happens there.

Refresh strategy: throttled to ~10 Hz. Per-action refreshes coalesce so
a fast action burst doesn't drown the run loop in NSMenu rebuilds; idle
state is exactly zero NSMenu work.

Design considerations:
- 3. Alienation: this surface IS the seamless story's user feedback.
  Without it, invisible-mode klyk gives the user nothing to look at.
- 5. Failure coupling: every entry-point catches exceptions so a bad
  state cannot prevent the menu from opening.
- 10. Verification burden: the menu carries the same `via` field klyk's
  tool responses do — the user sees exactly how a click landed without
  needing to read the agent's tool log.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from . import activity
from .ui_thread import ui

log = logging.getLogger("klyk.menubar")


# SF Symbol names for the status item. Set as template images on the
# NSStatusItem button so macOS auto-tints them (white on dark menu bar,
# dark on light) to match the system appearance.
#   - 'eye' when idle / no recent activity
#   - 'eye.fill' for ~2 s after each action, as a quiet "klyk just did
#     something" pulse
# Fallback Unicode characters (👁 / ●) are used when SF Symbols can't be
# loaded — keeps klyk functional on older macOS / unusual setups.
_SYMBOL_IDLE = "eye"
_SYMBOL_BUSY = "eye.fill"
_FALLBACK_IDLE = "\U0001F441"   # 👁
_FALLBACK_BUSY = "●"       # ●
_BUSY_WINDOW_S = 2.0  # an action within this window keeps the eye filled


class MenuBarController:
    """
    Holds the NSStatusItem + NSMenu lifecycle. Mutation methods MUST run
    on the main thread (callers go through ui.dispatch).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed = False
        self._status_item = None
        self._menu = None
        self._refresh_pending = False
        self._refresh_lock = threading.Lock()
        # Subscriber installed once; survives shutdown.
        self._subscribed = False

    # --- public API (any thread) ------------------------------------------

    def install_if_needed(self) -> bool:
        """
        Make sure the status item is up. Idempotent; safe to call from any
        thread. Returns True if the status item is installed (or already
        was).
        """
        with self._lock:
            if self._installed:
                return True
            if not ui.is_available():
                return False
            self._installed = True

        def _do_install() -> None:
            try:
                self._build_status_item()
            except Exception as e:
                log.error("menubar install failed: %s", e, exc_info=True)
                # Roll the flag back so a later retry can attempt again.
                with self._lock:
                    self._installed = False

        ui.dispatch(_do_install)
        if not self._subscribed:
            self._subscribed = True
            activity.recorder.subscribe(self._on_activity)
            self._start_ownership_watch()
        return True

    def request_refresh(self) -> None:
        """Coalesced refresh request — schedules a single rebuild even if
        many actions fired since the last tick."""
        if not self._installed:
            return
        with self._refresh_lock:
            if self._refresh_pending:
                return
            self._refresh_pending = True
        ui.dispatch(self._refresh)

    # --- subscriber (writer threads call this) ----------------------------

    def _on_activity(self, _entry: dict) -> None:
        self.request_refresh()

    # --- main-thread internals --------------------------------------------

    def _build_status_item(self) -> None:
        """Runs on main thread. One-time NSStatusItem creation."""
        from AppKit import (
            NSStatusBar,
            NSVariableStatusItemLength,
            NSMenu,
        )
        bar = NSStatusBar.systemStatusBar()
        self._status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self._status_item.button()
        # Tooltip — quick affordance on hover.
        button.setToolTip_("klyk — OS-level computer use")
        # Apply the idle icon now; _refresh below will set the right state
        # once it sees the activity recorder. The title is cleared because
        # the image carries the whole signal — no "klyk" text needed.
        button.setTitle_("")
        self._apply_icon(button, busy=False)
        self._menu = NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._status_item.setMenu_(self._menu)
        self._refresh()

    def _apply_icon(self, button, *, busy: bool) -> None:
        """Set the status-item button's image to the appropriate SF Symbol
        (template-tinted), falling back to a plain Unicode glyph in the
        title if SF Symbols can't be loaded on this macOS."""
        try:
            from AppKit import NSImage
            symbol = _SYMBOL_BUSY if busy else _SYMBOL_IDLE
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol, "klyk",
            )
            if img is not None:
                img.setTemplate_(True)
                button.setImage_(img)
                button.setTitle_("")
                return
        except Exception as e:
            log.warning("menubar icon load failed: %s", e)
        # Fallback path: clear any prior image and use a Unicode glyph.
        try:
            button.setImage_(None)
        except Exception:
            pass
        button.setTitle_(_FALLBACK_BUSY if busy else _FALLBACK_IDLE)

    def _refresh(self) -> None:
        """
        Rebuild the menu from current activity state. Runs on main
        thread. Constructs items declaratively from snapshots so we
        don't carry stale NSMenuItem refs that race with subscribers.
        """
        with self._refresh_lock:
            self._refresh_pending = False
        if self._menu is None or self._status_item is None:
            return
        try:
            self._rebuild_locked()
        except Exception as e:
            log.error("menubar refresh failed: %s", e, exc_info=True)

    def _is_active_driver(self) -> bool:
        """True only if THIS process is the recorded control owner — the one
        instance that shows its menu-bar eye. Read-only (never claims). Any
        other owner returns False, so a superseded session hides its eye and
        exactly one eye is ever visible: the active driver. owner==0 (no token
        written yet / degraded) shows the eye, so a lone klyk is never
        eyeless."""
        try:
            from . import ownership
            owner = ownership.current_owner()
        except Exception:
            return True
        return owner == 0 or owner == os.getpid()

    def _apply_ownership_visibility(self) -> None:
        """Show the status item only when this process is the active driver,
        so N connected sessions still surface exactly one eye. Main thread."""
        if self._status_item is None:
            return
        try:
            from AppKit import NSVariableStatusItemLength
            self._status_item.setLength_(
                NSVariableStatusItemLength if self._is_active_driver() else 0.0
            )
        except Exception as e:
            log.warning("menubar visibility update failed: %s", e)

    def _start_ownership_watch(self) -> None:
        """Background poll: keep the eye's visibility in sync with ownership
        even when this session is idle, so a superseded session hides its eye
        within ~2 s. Touches the UI only when the state flips — stable
        ownership means zero menu work."""
        def _watch() -> None:
            last = None
            while True:
                try:
                    cur = self._is_active_driver()
                    if cur != last:
                        last = cur
                        ui.dispatch(self._apply_ownership_visibility)
                        # A newly-visible eye may carry a menu built long ago
                        # (e.g. an update notice that has since resolved) —
                        # rebuild so it never shows stale state.
                        self.request_refresh()
                except Exception:
                    pass
                time.sleep(2.0)
        threading.Thread(target=_watch, name="klyk-eye-watch", daemon=True).start()

    def _rebuild_locked(self) -> None:
        from AppKit import NSMenu, NSMenuItem
        # Snapshot the activity state at one instant so concurrent
        # subscribers can't change it mid-build.
        summary = activity.recorder.get_summary()
        now = time.time()

        # ---- icon (busy vs idle) ----
        any_busy = any(
            (entry["last_action_age_s"] is not None
             and entry["last_action_age_s"] < _BUSY_WINDOW_S)
            for entry in summary
        )
        n_apps = len(summary)
        self._apply_icon(self._status_item.button(), busy=any_busy)

        # ---- rebuild menu ----
        new_menu = NSMenu.alloc().init()
        new_menu.setAutoenablesItems_(False)

        # Header item — non-clickable status line. Shows whether THIS session
        # is the active driver: only one klyk session drives the Mac at a
        # time (latest-wins), and a superseded session is the blocked one.
        active = self._is_active_driver()
        if active:
            header_title = f"klyk — active · {n_apps} session{'s' if n_apps != 1 else ''}"
        else:
            header_title = "klyk — inactive · another session has control"
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            header_title, None, "",
        )
        header.setEnabled_(False)
        new_menu.addItem_(header)
        if not active:
            note = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "  control passed to a newer session", None, "",
            )
            note.setEnabled_(False)
            new_menu.addItem_(note)

        # Update notice — one line, only when the daily cached check found a
        # newer release. status() is cache-only (a stat() in the common case),
        # so this adds no network work to the rebuild. Failure-isolated: a
        # broken check can never break the menu.
        try:
            upd = _update_line()
            if upd:
                upd_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    upd, None, "",
                )
                upd_item.setEnabled_(False)
                new_menu.addItem_(upd_item)
        except Exception as e:
            log.warning("menubar update line failed: %s", e)

        if n_apps == 0:
            empty = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "idle — no active sessions", None, "",
            )
            empty.setEnabled_(False)
            new_menu.addItem_(empty)
        else:
            new_menu.addItem_(NSMenuItem.separatorItem())
            for entry in summary:
                self._append_app_entries(new_menu, entry, now)

        # ---- global controls ----
        new_menu.addItem_(NSMenuItem.separatorItem())
        quit_help = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Cmd+Shift+Esc = emergency stop", None, "",
        )
        quit_help.setEnabled_(False)
        new_menu.addItem_(quit_help)

        self._status_item.setMenu_(new_menu)
        self._menu = new_menu

    def _append_app_entries(self, menu, entry: dict, now: float) -> None:
        from AppKit import NSMenuItem
        app = entry["app"]
        mode = entry.get("mode") or "autonomous"
        last = entry.get("last_action") or "—"
        via = entry.get("last_action_via") or ""
        age = entry.get("last_action_age_s")
        if age is None:
            age_str = ""
        elif age < 1.0:
            age_str = "just now"
        elif age < 60:
            age_str = f"{int(age)}s ago"
        else:
            age_str = f"{int(age / 60)}m ago"
        esc = entry.get("escalation_count", 0) or 0
        # App row: name + mode + age. Bold via attributed string would be
        # nicer but a plain title keeps the implementation tight.
        app_title = f"{app} · {mode}"
        if esc:
            app_title += f" · ⚠ {esc}"
        app_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            app_title, None, "",
        )
        app_item.setEnabled_(False)
        menu.addItem_(app_item)

        # Sub-row: last action.
        via_str = f" [{via}]" if via else ""
        last_text = f"  ↳ {last}{via_str} · {age_str}" if age_str else f"  ↳ {last}{via_str}"
        last_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            last_text, None, "",
        )
        last_item.setEnabled_(False)
        menu.addItem_(last_item)

        # Recent-actions submenu (last 20).
        recent = activity.recorder.get_recent(app, n=20)
        if recent:
            from AppKit import NSMenu
            sub = NSMenu.alloc().init()
            sub.setAutoenablesItems_(False)
            for r in reversed(recent):  # newest first
                age_r = now - r["ts"]
                if age_r < 1.0:
                    a = "now"
                elif age_r < 60:
                    a = f"{int(age_r)}s"
                else:
                    a = f"{int(age_r / 60)}m"
                tool = r["tool"]
                rv = r.get("via")
                coords = ""
                if r.get("x") is not None and r.get("y") is not None:
                    coords = f" @ ({r['x']},{r['y']})"
                detail = r.get("detail")
                line = f"{a:>5}  {tool}{coords}"
                if rv:
                    line += f"  [{rv}]"
                if detail:
                    line += f"  · {detail}"
                mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(line, None, "")
                mi.setEnabled_(False)
                sub.addItem_(mi)
            recent_holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "  ↳ recent actions", None, "",
            )
            recent_holder.setSubmenu_(sub)
            menu.addItem_(recent_holder)

        menu.addItem_(NSMenuItem.separatorItem())


def _update_line() -> str | None:
    """The menu's update-notice text, or None when klyk is current (or the
    check is disabled / has never succeeded). Pure formatting over the shared
    cache — unit-testable without AppKit."""
    from . import updates
    st = updates.status()
    if not st["update_available"]:
        return None
    return (f"⬆ Update available: {st['installed']} → {st['latest']} — "
            "run `klyk update`")


# Module-level singleton.
menubar = MenuBarController()
