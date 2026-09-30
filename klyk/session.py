"""
Session management for native and Electron app testing.
Sessions are keyed by app name — no session_id exposed to agents.
"""

from __future__ import annotations

import asyncio
import logging
import os as _os
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .logs import LogBuffer, StderrReader

log = logging.getLogger("klyk")


@dataclass
class Session:
    session_id: str
    app: str
    target: Literal["native", "electron"]
    pid: int
    window_id: int
    width: int
    height: int
    scale: float
    win_x: int = 0
    win_y: int = 0
    log_buffer: LogBuffer = field(default_factory=LogBuffer)
    last_grade: dict | None = None
    screenshots_taken: int = 0
    # In-session template cache so the agent can pass a short template_id
    # to find_template instead of the full base64 PNG (which corrupts when
    # transcribed by some LLM clients). Capped at 50 entries, FIFO eviction.
    template_cache: dict[str, str] = field(default_factory=dict)
    # Tracks whether the "web AX appears empty" warning has been emitted on
    # an inspect response in this session. Computed dynamically per-call by
    # inspecting the actual AX result count — no stale cached flag.
    # Chromium's AX is lazy: it enables the web a11y tree on first external
    # query, so a probe at session-create time often races and misses. The
    # only reliable signal is the AX read the agent is currently performing.
    ax_disabled_warned_on_inspect: bool = False
    ax_warmup_attempted: bool = False
    # Only wait for the unelapsed part of this window's last mutation repaint.
    last_mutation_at: float = 0.0
    # Input-delivery mode. Default is "autonomous" — klyk prefers invisible
    # delivery (no cursor warp, no focus theft) and auto-activates the
    # target app only when the invisible path can't deliver (e.g. Chromium
    # web content). Every escalation is recorded in escalation_log. The
    # other two modes:
    #   - humanoid: always uses the visible path (real cursor moves, target
    #     app comes to front). The behavior klyk shipped pre-Phase 2.
    #   - background: invisible-first like autonomous, but BAILS with
    #     requires_foreground:true instead of activating — leaves the
    #     decision to the agent. Strict "don't disturb me" mode.
    # Background vs autonomous foreground policy — see the set_mode tool description.
    mode: Literal["humanoid", "background", "autonomous"] = "autonomous"
    # Activity log for autonomous-mode foreground escalations. The user
    # reviews this when they return — every cursor_warp that happened
    # because the invisible path couldn't deliver is recorded with
    # timestamp + reason. Capped at 500 entries (oldest dropped) so a
    # long-running session can't unbound memory.
    escalation_log: list[dict] = field(default_factory=list)
    # Set on sessions for screen-edge / system apps (Dock, SystemUIServer, …)
    # that don't own a regular window. Tools that assume window bounds
    # (screenshot, inspect, click) should refuse on a windowless session;
    # the cross-app-drag target path is the supported use case.
    windowless: bool = False
    # Stable process start identity prevents close/reuse from signalling a recycled PID.
    process_identity: dict | None = field(default=None, repr=False)
    _log_proc: object = field(default=None, repr=False)    # log stream watcher (native) or app proc (electron)
    _log_reader: StderrReader | None = field(default=None, repr=False)


class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._by_app: dict[str, str] = {}

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id!r}")
        return session

    def get_by_app(self, app: str) -> Session | None:
        sid = self._by_app.get(app)
        return self._sessions.get(sid) if sid else None

    def register(self, session: Session, app_key: str | None = None) -> None:
        self._sessions[session.session_id] = session
        if app_key:
            self._by_app[app_key] = session.session_id

    def delete_by_app(self, app: str) -> Session | None:
        sid = self._by_app.pop(app, None)
        return self._sessions.pop(sid, None) if sid else None

    def has_app(self, app: str) -> bool:
        return app in self._by_app and self._by_app[app] in self._sessions

    def list_apps(self) -> list[str]:
        return list(self._by_app.keys())


registry = SessionRegistry()


# Apps that own a screen-edge layer instead of regular windows. They never
# return entries under AXWindows so the standard "wait for first window" path
# would time out at 15 s. Cross-app drag targets these (Finder → Dock Trash),
# so they need their own session-creation shortcut: skip the wait, build a
# session with placeholder window fields, let downstream AX walks use the
# AXChildren fallback inside ax_snapshot.
_WINDOWLESS_APPS = frozenset({
    "Dock",
    "SystemUIServer",
    "ControlCenter",
    "NotificationCenter",
    "Notification Center",
    "Spotlight",
})


# ---------------------------------------------------------------------------
# Window labels — human-readable A-Z aliases for CG window IDs.
# Assigned on first list_windows for each window, stable for that window's
# lifetime. Freed when the window disappears from the active list. After Z
# overflow continues AA, AB, ...
# ---------------------------------------------------------------------------

class WindowLabelRegistry:
    # Bound the number of apps tracked so a long-lived server doesn't accumulate
    # label maps for apps that were labeled but never explicitly closed
    # (Consideration #9 — registries get a size cap + eviction). Matches the
    # activity recorder's per-app cap. An evicted app just re-labels on its next
    # list_windows call.
    _MAX_APPS = 64

    def __init__(self) -> None:
        # {app_name: {window_id: label}}
        self._by_app: dict[str, dict[int, str]] = {}

    def _next_label(self, used: set[str]) -> str:
        # A..Z, then AA..ZZ, then AAA..; deterministic.
        import string
        letters = string.ascii_uppercase
        i = 0
        while True:
            n = i
            label = ""
            while True:
                label = letters[n % 26] + label
                n = n // 26 - 1
                if n < 0:
                    break
            if label not in used:
                return label
            i += 1

    def assign(self, app: str, window_ids_in_zorder: list[int]) -> dict[int, str]:
        """
        Return {window_id: label} for the given app, assigning labels to any
        new windows and freeing labels of windows no longer present. Labels
        are stable: a window keeps its label across calls until it disappears.
        """
        current = self._by_app.setdefault(app, {})
        # Mark this app most-recently-used (move to dict tail) so the LRU cap
        # below never evicts the app we're actively labeling.
        self._by_app[app] = self._by_app.pop(app)
        present = set(window_ids_in_zorder)
        # Free labels for windows that vanished
        for wid in list(current.keys()):
            if wid not in present:
                del current[wid]
        # Assign labels to new windows in z-order (frontmost first gets earliest free letter)
        used = set(current.values())
        for wid in window_ids_in_zorder:
            if wid not in current:
                label = self._next_label(used)
                current[wid] = label
                used.add(label)
        # Evict least-recently-used apps (dict head) past the cap. The current
        # app sits at the tail (touched above), so it's never the one dropped.
        while len(self._by_app) > self._MAX_APPS:
            del self._by_app[next(iter(self._by_app))]
        return dict(current)

    def resolve(self, app: str, label: str) -> int | None:
        """label → window_id. Case-insensitive. Returns None if unknown."""
        if not label:
            return None
        target = label.upper()
        for wid, l in self._by_app.get(app, {}).items():
            if l == target:
                return wid
        return None

    def label_for(self, app: str, window_id: int) -> str | None:
        return self._by_app.get(app, {}).get(window_id)

    def forget_app(self, app: str) -> None:
        """
        Drop all labels for an app. Called when the app's session closes so
        labels don't accumulate across relaunches — without this, an agent
        holding `window="A"` from a previous launch would resolve against a
        stale CG window_id that no longer exists, getting silent None back
        from `resolve`.
        """
        self._by_app.pop(app, None)


window_labels = WindowLabelRegistry()


async def create_session(
    target: str,
    app_name: str | None = None,
    bundle_id: str | None = None,
    app_path: str | None = None,
    *,
    allow_launch: bool = True,
) -> Session:
    """Attach without activation; cold launches require the caller's explicit launch policy."""
    from . import capture, launcher, computer

    if target == "native":
        if allow_launch:
            pid, was_running = await computer.run_input(
                lambda: launcher.launch_native_app(app_name=app_name, bundle_id=bundle_id, check_stop=computer._check_stop)
            )
        else:
            pid = await asyncio.get_event_loop().run_in_executor(
                None, lambda: launcher._quick_pid_for_app(bundle_id, app_name)
            )
            if pid is None:
                raise RuntimeError("The app is not running; this request cannot launch it. Open the app or use an authorized control request outside background mode.")
            was_running = True
        identity = launcher.process_identity(pid)
        if identity is None:
            raise RuntimeError("The app process identity could not be verified; no input was sent. Inspect the running app before retrying.")
        # Windowless system apps (Dock, SystemUIServer, ControlCenter,
        # NotificationCenter, Spotlight) never expose AXWindows — they own
        # screen-edge layers, not windows. Skip the 15 s wait and build a
        # session with placeholder window fields; downstream callers that
        # need AX use the AXChildren fallback path inside ax_snapshot.
        # Cross-app drag targets like Finder → Dock Trash depend on this.
        if (app_name or "").strip() in _WINDOWLESS_APPS:
            # Popen returns immediately; keep creation and ownership assignment
            # in the same task turn so cancellation cannot orphan the watcher.
            log_proc = launcher.start_native_log_stream(pid)
            session = Session(
                session_id=str(uuid.uuid4()),
                app=app_name or "",
                target="native",
                pid=pid,
                window_id=0,
                win_x=0, win_y=0,
                width=0, height=0,
                scale=capture.get_scale_factor(),
                process_identity=identity,
            )
            session._log_proc = log_proc
            session._log_reader = StderrReader(log_proc.stdout, session.log_buffer)
            session.windowless = True
            # Skip computer.activate_app — these apps shouldn't be activated.
            # registry.register with the app_key happens in get_or_create_session
            # to match the regular path; do the keyless register here so
            # session_id lookups also work, same as the standard path.
            registry.register(session)
            return session
        # Hard wall-clock ceiling on top of wait_for_window's own internal 15 s
        # budget: run_in_executor only starts counting once a worker thread is
        # free, so under heavy concurrent load (many overlapping tool calls,
        # rapid window churn) the *submission* can queue for minutes before
        # the call even begins — silently turning a documented "15 s" wait
        # into an unbounded one. wait_for's timer runs independently of the
        # executor queue, so this fires within ~2 s of the stated budget
        # regardless of how backed up the executor is.
        try:
            win = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: capture.wait_for_window(pid, timeout=15.0)
                ),
                timeout=17.0,
            )
        except asyncio.TimeoutError:
            win = None
        if win is None:
            raise RuntimeError(
                f"No window appeared for {app_name!r} (pid={pid}) within 15 s. "
                "The app launched but never opened a visible window. Common "
                "causes: the app is starting in the background only, or "
                "Screen Recording permission isn't granted (klyk can't see "
                "the window without it). Run `klyk doctor` to check."
            )
        if launcher.process_identity(pid) != identity:
            raise RuntimeError("The app process changed while its window opened; no input was sent. Inspect the running app again.")
        log_proc = launcher.start_native_log_stream(pid)
        session = Session(
            session_id=str(uuid.uuid4()),
            app=app_name or "",
            target="native",
            pid=pid,
            window_id=win["window_id"],
            win_x=int(win["bounds"]["X"]),
            win_y=int(win["bounds"]["Y"]),
            width=int(win["bounds"]["Width"]),
            height=int(win["bounds"]["Height"]),
            scale=capture.get_scale_factor(),
            process_identity=identity,
            # No precomputed ax_disabled flag — the inspect handler decides
            # dynamically based on the actual AX read.
        )
        session._log_proc = log_proc
        session._log_reader = StderrReader(log_proc.stdout, session.log_buffer)
        # Running-app attachment is always passive. Launch itself determines
        # presentation; observation never adds a separate activation.

    elif target == "electron":
        if not app_path and app_name:
            import subprocess
            # The `app` field may itself carry a full bundle path (e.g.
            # "/Applications/Codex.app") or a name that already ends in .app.
            # Use a real bundle path directly; otherwise do a Spotlight name
            # lookup, appending .app only when it's absent — so path or
            # already-suffixed input never becomes "<input>.app".
            expanded = _os.path.expanduser(app_name)
            if expanded.endswith(".app") and _os.path.isdir(expanded):
                app_path = expanded
            else:
                fs_name = app_name if app_name.endswith(".app") else f"{app_name}.app"
                result = subprocess.run(
                    ["mdfind", f"kMDItemFSName == '{fs_name}'"],
                    capture_output=True, text=True, timeout=5,
                )
                paths = [p for p in result.stdout.strip().splitlines() if p.endswith(".app")]
                if not paths:
                    raise RuntimeError(f"Could not locate {fs_name} via Spotlight")
                app_path = paths[0]
        elif not app_path:
            raise ValueError("app_path or app_name required for electron target")

        if allow_launch:
            pid, proc = await computer.run_input(lambda: launcher.launch_electron_app(app_path, check_stop=computer._check_stop))
        else:
            lookup_name = app_name or _os.path.basename(app_path)
            lookup_name = _os.path.basename(lookup_name)
            pid = await asyncio.get_event_loop().run_in_executor(
                None, lambda: launcher._quick_pid_for_app(bundle_id, lookup_name)
            )
            if pid is None:
                raise RuntimeError("The app is not running; this request cannot launch it. Open the app or use an authorized control request outside background mode.")
            proc = None
        identity = launcher.process_identity(pid)
        if identity is None:
            raise RuntimeError("The app process identity could not be verified; no input was sent. Inspect the running app before retrying.")
        win = await asyncio.get_event_loop().run_in_executor(
            None, lambda: capture.wait_for_window(pid, timeout=15.0)
        )
        if win is None:
            raise RuntimeError(
                f"No window appeared for Electron app at {app_path!r} within 15 s. "
                "The bundle launched but never opened a visible window. Run "
                "`klyk doctor` to verify Screen Recording is granted."
            )
        if launcher.process_identity(pid) != identity:
            raise RuntimeError("The app process changed while its window opened; no input was sent. Inspect the running app again.")

        session = Session(
            session_id=str(uuid.uuid4()),
            app=app_name or app_path,
            target="electron",
            pid=pid,
            window_id=win["window_id"],
            win_x=int(win["bounds"]["X"]),
            win_y=int(win["bounds"]["Y"]),
            width=int(win["bounds"]["Width"]),
            height=int(win["bounds"]["Height"]),
            scale=capture.get_scale_factor(),
            process_identity=identity,
        )
        session._log_proc = proc
        session._log_reader = StderrReader(proc.stderr, session.log_buffer) if proc is not None else None

    else:
        raise ValueError(
            f"Unknown target: {target!r}. Klyk supports 'native' and 'electron' only."
        )

    # Single registration point. Earlier versions registered here without an
    # app_key and then again in get_or_create_session — harmless but redundant
    # and made the data-flow harder to reason about. The app_key is set by the
    # caller via get_or_create_session.
    registry.register(session)
    return session


async def get_or_create_session(
    app: str,
    target: str | None = None,
    bundle_id: str | None = None,
    app_path: str | None = None,
    *,
    allow_launch: bool = True,
) -> tuple[Session, bool]:
    """
    Return (session, is_new), where new means attached or launched this call.
    Reuse requires the original process start identity and selected CG window.
    A missing live window fails without closing the app; a dead process releases
    session resources before a new session follows the caller's launch policy.
    """
    from . import capture, launcher

    existing = registry.get_by_app(app)
    if existing is not None:
        if launcher.pid_alive(existing.pid):
            identity = launcher.process_identity(existing.pid)
            if existing.process_identity is None or identity != existing.process_identity:
                raise RuntimeError("The app process identity changed or could not be verified; no input was sent. Close the stale session and inspect the app again.")
            if existing.windowless:
                # Windowless system apps never acquire a CG window number.
                # Process identity, rather than a permanently missing window 0, is canonical.
                return existing, False
            # Confirm the window is still ours. Cheap (~5 ms) and catches PID
            # recycling silently.
            win = capture.get_window_by_id(existing.window_id)
            if win and win.get("pid") == existing.pid:
                return existing, False
            # Losing a window is not permission to terminate a live app and
            # its other documents. The caller can enumerate and explicitly
            # select another live window, or close the session deliberately.
            raise RuntimeError(
                f"The selected window for {app!r} is no longer available. "
                "No app was closed. Call list_windows and choose a current window_id."
            )
        _close_session(existing)
        registry.delete_by_app(app)
        # Mirror close_app's cleanup so the relaunch doesn't inherit stale A/B/C
        # window labels (mapping to the dead pid's CG window IDs) or leave the
        # activity recorder pointing at the old process. Without this, only the
        # explicit close_app tool cleaned up — the common crash-relaunch path
        # leaked both.
        window_labels.forget_app(app)
        try:
            from .visibility import visibility as _visibility
            _visibility.detach(app)
        except Exception:
            pass

    if len(registry.list_apps()) >= 64:
        raise RuntimeError("The 64-app session limit was reached; close an unused session before opening another app.")
    resolved_target = target or "native"
    session = await create_session(
        target=resolved_target,
        app_name=app,
        bundle_id=bundle_id,
        app_path=app_path,
        allow_launch=allow_launch,
    )
    registry.register(session, app_key=app)
    # Session-lifecycle hook. Today this is a no-op (the dock-tile badge
    # surface was removed); kept as the symmetric pair of detach() so a
    # future per-session surface can be added without touching the
    # registration path. Lazy-imported in case visibility itself ever
    # picks up AppKit dependencies again.
    try:
        from .visibility import visibility as _visibility
        _visibility.attach(app)
    except Exception:
        pass
    return session, True


def _close_session(session: Session) -> None:
    """
    Terminate the app and clean up all session-attached state.
    Without thorough cleanup, long-running MCP servers leak: zombie processes
    (no proc.wait()), pipes (no close), daemon threads (StderrReader blocking
    on a never-closed pipe), and unbounded template cache memory.
    """
    from . import launcher
    # Stop our log child first, then stop its nonblocking reader. An Electron
    # application's process must pass the same stable-identity guard as a
    # native app before any signal; the Popen wrapper alone is not identity.
    proc = getattr(session, "_log_proc", None)
    app_process = proc is not None and getattr(proc, "pid", None) == session.pid
    app_terminated = False
    if proc is not None:
        if app_process:
            if session.process_identity is not None:
                app_terminated = launcher.terminate_pid(session.pid, expected_identity=session.process_identity)
        else:
            try:
                proc.terminate()
            except Exception:
                pass
        # Best-effort wait so we don't leave a zombie. Short timeout — the
        # interpreter will reap on exit if this fails.
        try:
            if hasattr(proc, "wait") and (not app_process or app_terminated):
                proc.wait(timeout=1.0)
        except Exception:
            try:
                if not app_process and hasattr(proc, "kill"):
                    proc.kill()
                    proc.wait(timeout=1.0)
            except Exception:
                pass
        session._log_proc = None
    # Reader shutdown remains bounded even if an Electron descendant inherited
    # the stderr write end and outlived the parent.
    reader = getattr(session, "_log_reader", None)
    if reader is not None:
        try:
            reader.stop()
        except Exception:
            pass
        session._log_reader = None
    # Free in-session caches.
    try:
        session.template_cache.clear()
    except Exception:
        pass
    # Terminate the app itself (with escalation in terminate_pid).
    if app_process:
        if not app_terminated and launcher.pid_alive(session.pid):
            raise RuntimeError("The app was left running because its original process could not be safely verified or closed.")
    elif session.process_identity is not None:
        if not launcher.terminate_pid(session.pid, expected_identity=session.process_identity):
            raise RuntimeError("The app could not be safely closed; its process identity or termination could not be verified.")
    elif launcher.pid_alive(session.pid):
        raise RuntimeError("The app was left running because this session has no verified process identity.")


async def close_app(app: str) -> None:
    """Close a tracked app only after identity checks, draining any cancelled cleanup worker."""
    session = registry.delete_by_app(app)
    if session:
        # Drop window labels so a relaunch doesn't inherit stale labels.
        window_labels.forget_app(app)
        # Tear down the dock-tile badge + activity log. Same lazy-import
        # pattern as create_session — keeps session.py importable on
        # non-darwin dev rigs.
        try:
            from .visibility import visibility as _visibility
            _visibility.detach(app)
        except Exception:
            pass
        from . import computer
        # Cancellation drains the bounded native cleanup before ownership can
        # transfer. A timed-out worker must never continue closing an app after
        # a new server has taken control.
        try:
            await asyncio.wait_for(
                computer.run_input(lambda: _close_session(session)),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            log.warning("App cleanup exceeded its time budget; the cleanup worker has finished.")


def list_sessions() -> list[dict]:
    """
    Return a summary of all active sessions. `alive` reflects whether the
    OS still has a process at session.pid — agents can tell zombies from
    healthy sessions without making a probe call themselves. `mode` and
    `escalation_count` surface the Seamless Mode state so a fresh agent
    picking up an existing session can see how it's currently configured.
    """
    from . import launcher
    result = []
    for app_name in registry.list_apps():
        session = registry.get_by_app(app_name)
        if session:
            result.append({
                "app": app_name,
                "target": session.target,
                "pid": session.pid,
                "width": session.width,
                "height": session.height,
                "alive": launcher.pid_alive(session.pid),
                "mode": session.mode,
                "escalation_count": len(session.escalation_log),
            })
    return result
