"""
Control-ownership token for klyk.

Only one klyk instance may DRIVE the Mac (deliver clicks/keystrokes) at a
time — two would interleave input and corrupt the target app. But the MCP
*connection* must never be gated: every instance starts and serves all tools
instantly, so a client never sees "failed to connect". We separate the two
concerns:

  - Connection: unconditional. Nothing blocks startup. (See _main_entry.)
  - Control: a transferable token. A starting instance takes it only if it's
    free (no live owner) — it will not steal from an instance that is alive
    and actively driving, so control never thrashes when servers respawn or
    coexist. Switching to another live session is an explicit `take_control`.
    A non-owner is blocked only at the moment it tries a control action, with
    one clear message, and reclaims with a single `take_control` call.

The token is a tiny file (~/.klyk/owner) holding the current owner pid.
Every claim/check briefly flocks it, so transfers are atomic; the lock is
never held for the process lifetime (unlike the old singleton). A dead owner
counts as "no owner" — any instance may then claim — so a crashed session
never wedges control.

Why this beats a startup lock: connecting is always instant and never
ambiguous, control stays with whoever holds it until it's free or explicitly
transferred (no thrash between respawned/coexisting instances), and the only
thing an agent ever sees is "it works" or one actionable line telling it to
call take_control. No investigation, ever.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

# Owner file location. Overridable via env so tests can isolate themselves
# from the real token (and never disturb a live session).
OWNER_PATH = Path(
    os.environ.get("KLYK_OWNER_FILE", str(Path.home() / ".klyk" / "owner"))
)

# Captured once at import — stable for the process lifetime.
_MY_PID = os.getpid()


def _read_pid(fh) -> int:
    """Read the owner pid from an open, positioned file handle. 0 if empty
    or unparseable."""
    fh.seek(0)
    raw = fh.read().strip()
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _write_pid(fh, pid: int) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(f"{pid}\n")
    fh.flush()


def _alive(pid: int) -> bool:
    """True if pid names a live process. <=1 is never a real owner."""
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but signalling denied — treat as alive
    return True


def _open():
    """Open the owner file (a+), creating the dir. Returns the handle, or
    None if the filesystem won't cooperate (then ownership is unenforceable
    and callers degrade to 'always allowed' — better flaky than refusing to
    work)."""
    try:
        OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
        return open(OWNER_PATH, "a+")
    except OSError:
        return None


def claim_ownership() -> int:
    """Make THIS process the control owner. Always succeeds (latest claimer
    wins). Returns the previous owner pid, or 0 if there was none / it was
    us. This is the FORCEFUL claim — it is `take_control`. Startup uses
    `claim_ownership_if_unowned` instead so a respawn doesn't yank control
    from a live, active driver."""
    fh = _open()
    if fh is None:
        return 0
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        prev = _read_pid(fh)
        _write_pid(fh, _MY_PID)
        return prev if prev != _MY_PID else 0
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def claim_ownership_if_unowned() -> int:
    """Claim control ONLY if it is currently free — no live owner. Returns the
    pid that owns control after the call: ours if we claimed (the slot was free,
    held by a dead pid, or already us), or the EXISTING live owner's pid if we
    deferred to it.

    Used at server startup. A freshly-launched instance takes control when it's
    free (the common case — the previous session has exited) but does NOT steal
    it from another instance that is alive and actively driving. That distinction
    is what stops control from thrashing when servers respawn or several
    instances coexist: each new process used to `claim_ownership()` at startup
    and yank the token away from whoever was mid-task. Explicit `take_control`
    remains the one way to forcibly transfer."""
    fh = _open()
    if fh is None:
        return _MY_PID  # unenforceable filesystem — behave as if we own
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        owner = _read_pid(fh)
        if owner == _MY_PID or not _alive(owner):
            _write_pid(fh, _MY_PID)
            return _MY_PID
        return owner
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def is_owner() -> bool:
    """True if THIS process currently owns control. A dead or absent owner
    counts as 'no owner' and we transparently claim it — so a crashed prior
    session never blocks us. Cheap: one short-lived flock per call."""
    fh = _open()
    if fh is None:
        return True  # unenforceable — never block the user over a missing file
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        owner = _read_pid(fh)
        if owner == _MY_PID:
            return True
        if not _alive(owner):
            # Owner crashed/exited — take over transparently.
            _write_pid(fh, _MY_PID)
            return True
        return False
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def current_owner() -> int:
    """The current owner pid (0 if none). Informational — for doctor / CLI."""
    fh = _open()
    if fh is None:
        return 0
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        return _read_pid(fh)
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()
