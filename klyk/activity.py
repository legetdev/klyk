"""
Activity recorder — single source of truth for what klyk is doing right now.

MCP tool dispatchers call record() before each user-visible action; the
menu-bar status item reads this state to display activity to the user.
Decoupled from the UI surfaces: when nobody is watching, record() is a
sub-microsecond append into a bounded deque; when the menubar dropdown
is open, it reads from this state via get_summary() / get_recent()
without coupling to any call site.

Design considerations:
- 4. Token / payload bloat: this module returns nothing to the agent —
  it feeds local UI surfaces only. No risk of bloating tool responses.
- 9. Hidden state across calls: the recorder IS hidden state. Capped
  bounded (200 entries/app, 64 apps), evicted FIFO, and surfaced through
  the menu-bar dropdown so the user sees what's accumulating.
- 5. Failure coupling: record() catches every exception internally.
  UI surfaces are observers; an observer that raises does not affect
  other observers or the calling tool.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

# Bounded retention. Per-app 200 entries — a typical 30-min seamless run
# fires ~50-150 actions, so a single session never hits the cap; a long
# autonomous run drops the oldest first. The 64-app outer cap is defensive
# only; klyk hardly ever runs against more than 4 apps concurrently.
_MAX_PER_APP = 200
_MAX_APPS = 64


class ActivityRecorder:
    """Thread-safe; observers are invoked outside the lock to avoid re-entrancy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {app_name: deque[entry]}. Insertion order preserved by dict in 3.7+.
        self._per_app: dict[str, deque[dict]] = {}
        # {app_name: last_entry} so menu-bar can show "current" without
        # peeking inside the deque.
        self._last: dict[str, dict] = {}
        # {app_name: {"mode": str, "escalation_count": int, "pid": int}} —
        # snapshot of session state at last record(). Kept here so the
        # menu-bar can render without holding a session lock.
        self._session_state: dict[str, dict] = {}
        self._observers: list[Callable[[dict], None]] = []

    # -- writers --

    def record(
        self,
        app: str,
        tool: str,
        *,
        x: int | float | None = None,
        y: int | float | None = None,
        x2: int | float | None = None,
        y2: int | float | None = None,
        detail: str | None = None,
        via: str | None = None,
        session_mode: str | None = None,
        escalation_count: int | None = None,
        pid: int | None = None,
        win_x: int | None = None,
        win_y: int | None = None,
    ) -> dict:
        """
        Append an entry and notify observers. Coordinates are window-relative
        (the screenshot coord space klyk uses everywhere). win_x/win_y are
        recorded so a future observer that wants screen-space coords doesn't
        have to re-read session state.
        """
        if not app or not tool:
            return {}
        entry = {
            "app": app,
            "tool": tool,
            "ts": time.time(),
            "x": int(x) if x is not None else None,
            "y": int(y) if y is not None else None,
            "x2": int(x2) if x2 is not None else None,
            "y2": int(y2) if y2 is not None else None,
            "detail": detail,
            "via": via,
            "win_x": win_x,
            "win_y": win_y,
        }
        observers_snapshot: list[Callable[[dict], None]] = []
        with self._lock:
            dq = self._per_app.get(app)
            if dq is None:
                # First time we see this app. Defend the outer cap by
                # dropping the least-recently-used app's entries when full.
                if len(self._per_app) >= _MAX_APPS:
                    # Drop the app whose last_ts is oldest.
                    victim = min(
                        self._per_app.keys(),
                        key=lambda a: (self._last.get(a) or {}).get("ts", 0.0),
                    )
                    self._per_app.pop(victim, None)
                    self._last.pop(victim, None)
                    self._session_state.pop(victim, None)
                dq = deque(maxlen=_MAX_PER_APP)
                self._per_app[app] = dq
            dq.append(entry)
            self._last[app] = entry
            if (
                session_mode is not None
                or escalation_count is not None
                or pid is not None
            ):
                state = self._session_state.setdefault(app, {})
                if session_mode is not None:
                    state["mode"] = session_mode
                if escalation_count is not None:
                    state["escalation_count"] = escalation_count
                if pid is not None:
                    state["pid"] = pid
            observers_snapshot = list(self._observers)
        # Fire observers outside the lock. A buggy observer must not
        # block other observers or block record() callers.
        for cb in observers_snapshot:
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def remove_app(self, app: str) -> None:
        """Drop everything we have for app. Called when a session closes."""
        with self._lock:
            self._per_app.pop(app, None)
            self._last.pop(app, None)
            self._session_state.pop(app, None)

    # -- readers --

    def get_summary(self) -> list[dict]:
        """
        Per-app summary, freshest activity first. Each entry:
          {app, mode, escalation_count, last_action, last_action_age_s,
           recent_count}
        """
        now = time.time()
        out: list[dict] = []
        with self._lock:
            for app, dq in self._per_app.items():
                last = self._last.get(app)
                state = self._session_state.get(app, {})
                out.append({
                    "app": app,
                    "mode": state.get("mode"),
                    "escalation_count": state.get("escalation_count", 0),
                    "pid": state.get("pid"),
                    "last_action": last["tool"] if last else None,
                    "last_action_via": last.get("via") if last else None,
                    "last_action_age_s": (now - last["ts"]) if last else None,
                    "recent_count": len(dq),
                })
        out.sort(
            key=lambda r: (r["last_action_age_s"] is None, r["last_action_age_s"] or 0.0),
        )
        return out

    def get_recent(self, app: str, n: int = 20) -> list[dict]:
        with self._lock:
            dq = self._per_app.get(app)
            if not dq:
                return []
            return list(dq)[-n:]

    def get_last(self, app: str) -> dict | None:
        with self._lock:
            return self._last.get(app)

    # -- observers --

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            if cb not in self._observers:
                self._observers.append(cb)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._observers.remove(cb)
            except ValueError:
                pass


# Module-level singleton. Importers receive the same recorder so the
# menu-bar (subscriber) and the mcp_server tool handlers (writer) share
# state without a passed-around handle.
recorder = ActivityRecorder()


# Convenience entry points so mcp_server doesn't repeat boilerplate.
# These pick the canonical coord pair from a tool's args dict and translate
# window-relative -> recorder format. Failures swallowed — instrumentation
# must never break a tool call.

# Tools whose dispatch represents a user-visible "action" the UI should
# surface. Read-only / introspection tools (list_sessions, screen_info,
# get_pixel, etc.) are excluded so the menubar shows actual activity
# rather than just call counts.
ACTION_TOOLS = frozenset({
    "click", "double_click", "long_press", "drag", "scroll", "ax_action",
    "click_element", "click_menu", "fill_field", "type_text",
    "press_key", "press_system_key", "select_option",
    "inspect", "screenshot", "wait_for", "wait_for_visual", "find_template",
    "handle_system_dialog", "set_window_bounds", "focus_window",
    "set_clipboard", "run",
})


def record_from_args(session, tool_name: str, args: dict, via: str | None = None) -> None:
    """
    Best-effort recorder call from the MCP dispatch path. Pulls
    coords/mode/pid from the resolved session + args and never raises.
    """
    if tool_name not in ACTION_TOOLS:
        return
    try:
        # Pull common coordinate pairs. Drag uses x1/y1 → x2/y2; everything
        # else uses x/y. Tools without coords (type_text, press_key) record
        # without spatial info — the menubar log just omits the coord column.
        x = args.get("x")
        y = args.get("y")
        x2 = args.get("x2")
        y2 = args.get("y2")
        if "x1" in args and x is None:
            x = args["x1"]
        if "y1" in args and y is None:
            y = args["y1"]
        # Detail is a short human-readable hint shown in the menubar log.
        detail = None
        if tool_name == "type_text":
            txt = args.get("text", "") or ""
            detail = f"{len(txt)} chars" if txt else None
        elif tool_name == "press_key":
            key = args.get("key") or args.get("keys")
            if isinstance(key, list):
                detail = "+".join(map(str, key))
            elif key:
                detail = str(key)[:32]
        elif tool_name == "click_element":
            detail = (args.get("label") or "")[:48]
        elif tool_name == "fill_field":
            txt = args.get("text", "") or ""
            detail = f"{len(txt)} chars"
        elif tool_name == "scroll":
            detail = f"{args.get('direction','?')} x{args.get('amount', 3)}"
        recorder.record(
            app=session.app,
            tool=tool_name,
            x=x, y=y, x2=x2, y2=y2,
            detail=detail,
            via=via,
            session_mode=getattr(session, "mode", None),
            escalation_count=len(getattr(session, "escalation_log", []) or []),
            pid=getattr(session, "pid", None),
            win_x=getattr(session, "win_x", None),
            win_y=getattr(session, "win_y", None),
        )
    except Exception:
        # Silent: this is instrumentation, not a contract surface.
        pass


def record_via(app: str, tool: str, via: str) -> None:
    """Update the last entry's via field once a seamless dispatch resolves.
    Records-and-forgets — used by the seamless paths to stamp 'skylight',
    'cursor_warp', etc. onto the most recent entry so the menubar's
    last-action row shows the actual delivery mode."""
    try:
        last = recorder.get_last(app)
        if last and last.get("tool") == tool and last.get("via") is None:
            # Mutate in place — both deque and _last reference the same dict.
            last["via"] = via
    except Exception:
        pass
