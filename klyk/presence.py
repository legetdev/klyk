"""Live presence of Klyk MCP servers, so the menu-bar controls can show who is connected.

Each server process owns one tiny owner-only file, ``~/.klyk/presence/<pid>.json``::

    {"pid": 4242, "client": "claude", "version": "0.7.0", "started_at": 1767225600.0,
     "last_action_at": 1767225612.5, "last_app": "Safari", "last_tool": "click"}

It holds no screen content, no arguments and no results: only which environment is
connected and which app it last worked in. Readers drop files of dead processes.

How the MCP server calls it (klyk/mcp_server.py); every function is cheap and never raises:

* ``presence.connected(policy.current_client())`` once at server start, after the
  client identity is resolved. It writes the file and registers ``clear`` for normal exit.
* ``presence.record(client, app, tool)`` after each tool request this environment was
  allowed to run, with the target app's display name (or ``None``) and the tool name.
  Identical calls rewrite the file at most every 0.5 s; a new app or tool writes at once.
* ``presence.clear()`` on every exit path that skips ``atexit``: put it first in
  ``_cleanup_input_on_exit()``, before its early return, which covers the SIGTERM/SIGINT
  handler, the parent-death watch and the forced shutdown (all end in ``os._exit``).
  A normal interpreter exit runs it through ``atexit``. It takes no lock, so it is safe
  inside a signal handler.

``summary()`` is for the controls: per environment, whether a server is connected and
whether it acted within the last few seconds, and in which app.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from . import __version__, connection_policy as policy
from .private_files import private_directory

_MAX_BYTES = 2048
_MAX_FILES = 64
_MAX_ENTRIES = 256
_MIN_INTERVAL = 0.5
_NAME = re.compile(r"([1-9][0-9]{0,9})\.json")
_TEMPORARY = re.compile(r"\.([1-9][0-9]{0,9})\.[0-9a-f]{8}\.tmp")
_KEYS = {"pid", "client", "version", "started_at", "last_action_at", "last_app", "last_tool"}

_lock = threading.Lock()
_state: dict | None = None
_written = (None, None, float("-inf"))  # (app, tool, monotonic time) of the last write.
_registered = False


def directory() -> Path:
    """Keep presence beside the owner-private connection settings."""
    return policy.policy_path().parent / "presence"


def _text(value, limit: int) -> str | None:
    """Keep display names short, single-line and printable."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    return cleaned[:limit] or None


def _client(value) -> str | None:
    """Accept only a known environment key; anything else is unattributed."""
    return value if isinstance(value, str) and value in policy.CLIENT_KEYS else None


def connected(client: str | None) -> None:
    """Announce this server at startup; never raises."""
    global _state, _written, _registered
    try:
        private_directory(directory().parent)
        with _lock:
            _state = {"pid": os.getpid(), "client": _client(client), "version": __version__,
                      "started_at": time.time(), "last_action_at": None, "last_app": None, "last_tool": None}
            _written = (None, None, float("-inf"))
            if not _registered:
                atexit.register(clear)
                _registered = True
            _write_locked(_state, time.monotonic())
    except Exception:
        pass  # Presence is a display aid; it can never affect a request.


def record(client: str | None, app: str | None, tool: str | None) -> None:
    """Note one allowed request, rewriting at most every 0.5 s unless the app or tool changed."""
    try:
        now = time.monotonic()
        app, tool = _text(app, 80), _text(tool, 64)
        with _lock:
            state = _state
            if state is None or state["pid"] != os.getpid():
                return  # Not announced, cleared at exit, or a forked child.
            state.update(client=_client(client) or state["client"], last_action_at=time.time(),
                         last_app=app, last_tool=tool)
            last_app, last_tool, last_time = _written
            if (app, tool) == (last_app, last_tool) and now - last_time < _MIN_INTERVAL:
                return
            _write_locked(state, now)
    except Exception:
        pass


def clear() -> None:
    """Remove this server's file; later records stay silent until a new announcement."""
    global _state
    _state = None  # No lock: a signal handler may interrupt a record on this thread.
    try:
        os.unlink(directory() / f"{os.getpid()}.json")
    except Exception:
        pass  # Already gone; a racing final write leaves a dead PID that readers remove.


def _write_locked(state: dict, now: float) -> None:
    """Atomically replace this server's file with a fresh owner-only copy."""
    global _written
    folder = directory()
    try:
        _ensure_directory(folder)
    except FileNotFoundError:
        return  # Uninstall removed Klyk's state; never recreate it from a lingering server.
    data = json.dumps(state, separators=(",", ":")).encode("utf-8")
    temporary = folder / f".{state['pid']}.{secrets.token_hex(4)}.tmp"
    target = folder / f"{state['pid']}.json"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(temporary, target)  # Replaces a planted link rather than following it.
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _written = (state["last_app"], state["last_tool"], now)
    if _state is not state:
        os.unlink(target)  # clear() ran during this write; leave no file behind.


def _ensure_directory(folder: Path) -> None:
    """Create only the presence folder inside existing Klyk state, refusing links and other owners."""
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("Klyk's presence folder is not a private directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(folder, 0o700)  # Checked above to be a real directory, not a link.


def _alive(pid: int, started_at) -> bool:
    """A live process that started after the file was written reuses a dead server's PID."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False  # Gone, or a number no process can have.
    except OSError:
        pass  # Exists; signalling it is merely not permitted.
    if pid == os.getpid():
        return True
    try:
        from .launcher import process_identity
        identity = process_identity(pid)
    except Exception:
        identity = None
    if identity and isinstance(started_at, (int, float)):
        return identity["started"][0] <= started_at + 1.0
    return True


def _remove(path: Path) -> None:
    """Drop a stale or invalid entry; another reader may already have removed it."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _load(path: Path, pid: int) -> dict | None:
    """Read one bounded regular file this user owns; None means remove it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}  # Removed concurrently; nothing to drop.
    except OSError:
        return None  # Symlink or another special entry.
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_size > _MAX_BYTES):
            return None
        data = json.loads(os.read(fd, _MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    if (not isinstance(data, dict) or set(data) != _KEYS or data["pid"] != pid
            or type(data["pid"]) is not int
            or not (data["client"] is None or _client(data["client"]))
            or not isinstance(data["started_at"], (int, float))
            or not (data["last_action_at"] is None or isinstance(data["last_action_at"], (int, float)))
            or not all(data[key] is None or isinstance(data[key], str)
                       for key in ("version", "last_app", "last_tool"))):
        return None
    return data


def entries() -> list[dict]:
    """Return live server records, removing dead, malformed, oversized or foreign files."""
    found = []
    reads = 0
    try:
        with os.scandir(directory()) as listing:
            for index, entry in enumerate(listing):
                if index >= _MAX_ENTRIES or reads >= _MAX_FILES:
                    break
                path = Path(entry.path)
                temporary = _TEMPORARY.fullmatch(entry.name)
                name = _NAME.fullmatch(entry.name)
                try:
                    if temporary and not _alive(int(temporary[1]), None):
                        _remove(path)  # Left by a server that died mid-write.
                    if not name:
                        continue
                    reads += 1
                    data = _load(path, int(name[1]))
                    if data is None or (data and not _alive(data["pid"], data["started_at"])):
                        _remove(path)
                    elif data:
                        found.append(data)
                except Exception:
                    continue  # One odd entry never hides the others.
    except Exception:
        pass  # No folder yet, or unreadable: nobody is shown as connected.
    return found


def summary(now: float | None = None, active_window: float = 5.0) -> dict[str, dict]:
    """Per environment: connected, active within the window, and the app it is working in."""
    now = time.time() if now is None else now
    result = {key: {"connected": False, "active": False, "app": None} for key in policy.CLIENT_KEYS}
    latest: dict[str, float] = {}
    try:
        for data in entries():
            key = data["client"]
            if key not in result:
                continue
            row = result[key]
            row["connected"] = True
            acted = data["last_action_at"]
            if acted is None or not -1.0 <= now - acted <= active_window:
                continue
            row["active"] = True
            if acted >= latest.get(key, float("-inf")):
                latest[key] = acted
                row["app"] = data["last_app"]
    except Exception:
        pass
    return result
