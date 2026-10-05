"""Private per-environment access switches, shared by the controls and MCP servers.

One bounded file is the source of truth. Missing or invalid settings deny access;
generation tokens prevent an old request resuming after an Off/On transition.
This is a user control inside Klyk's trusted-local boundary, not an OS sandbox.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from . import clients, jsonc
from .private_files import open_private, private_directory

CLIENT_KEYS = (*clients.CLIENTS, "other")
_MAX_BYTES = 16_384
_MAX_GENERATION = 2**53 - 1


class AccessDisabled(RuntimeError):
    """A client has no current permission to use Klyk's computer tools."""

    def __init__(self, message: str = "Klyk access is off. Turn it on in the Klyk menu-bar controls."):
        super().__init__(message)


def policy_path() -> Path:
    """Use a fixed owner-local path; client environment cannot redirect policy."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".klyk" / "connections.json"


def current_client() -> str | None:
    """Resolve a configured identity, with metadata-only legacy compatibility."""
    return clients.current_process_client()


def _default() -> dict:
    """Return a fresh, explicitly disabled state for every supported environment."""
    return {"version": 1, "revision": 0, "clients": {
        key: {"enabled": False, "generation": 0} for key in CLIENT_KEYS
    }}


def _pairs(pairs: list[tuple]) -> dict:
    """Reject duplicate policy keys rather than silently selecting one value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate connection setting")
        result[key] = value
    return result


def _validated(data: object) -> dict:
    """Require exact Boolean switches and bounded monotonically increasing tokens."""
    if not isinstance(data, dict) or set(data) != {"version", "revision", "clients"}:
        raise ValueError("Invalid connection settings")
    revision = data["revision"]
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported connection settings")
    if type(revision) is not int or not 0 <= revision <= _MAX_GENERATION:
        raise ValueError("Invalid connection revision")
    states = data["clients"]
    if not isinstance(states, dict) or set(states) != set(CLIENT_KEYS):
        raise ValueError("Invalid environment list")
    for state in states.values():
        if not isinstance(state, dict) or set(state) != {"enabled", "generation"}:
            raise ValueError("Invalid environment setting")
        if type(state["enabled"]) is not bool or type(state["generation"]) is not int:
            raise ValueError("Invalid environment switch")
        if not 0 <= state["generation"] <= revision:
            raise ValueError("Invalid environment generation")
    return data


def _read() -> dict:
    """Read only an owned private regular file, with no symlinks, hard links or FIFOs."""
    path = policy_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _default()
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _MAX_BYTES):
            raise ValueError("Unsafe connection settings file")
        raw = os.read(fd, _MAX_BYTES + 1)
        after = os.fstat(fd)
        present = path.stat(follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if len(raw) > _MAX_BYTES or identity(info) != identity(after) or identity(info) != identity(present):
            raise ValueError("Connection settings changed while being read")
        return _validated(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs))
    finally:
        os.close(fd)


def snapshot() -> dict:
    """Give the UI confirmed state, or a disabled state with a plain read-error banner."""
    try:
        return _read()
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {**_default(), "error": "Connection settings could not be read. Klyk access stays off."}


def enabled(client: str | None = None) -> bool:
    """Check the requested environment without accepting an unknown identity."""
    key = current_client() if client is None else client
    state = snapshot()["clients"].get(key)
    return bool(state and state["enabled"])


def token(client: str | None = None) -> tuple[str, int]:
    """Capture one enabled generation before a request enters a queue or worker."""
    key = current_client() if client is None else client
    state = snapshot()["clients"].get(key)
    if not state or not state["enabled"]:
        raise AccessDisabled()
    return key, state["generation"]


def allows(value: tuple[str, int]) -> bool:
    """Reject late work and late results after any transition of its environment."""
    if (not isinstance(value, tuple) or len(value) != 2
            or not isinstance(value[0], str) or type(value[1]) is not int):
        return False
    state = snapshot()["clients"].get(value[0])
    return bool(state and state["enabled"] and state["generation"] == value[1])


@contextmanager
def _writer():
    """Serialize switches across controllers, with a short bounded lock wait."""
    path = policy_path()
    private_directory(path.parent)
    with open_private(path.parent / "connections.lock", "a+") as lock:
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Connection settings are busy. Try the switch again.") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def set_enabled(client: str, value: bool) -> dict:
    """Atomically change one environment while preserving every other switch."""
    if client not in CLIENT_KEYS or type(value) is not bool:
        raise ValueError("Unknown environment or invalid switch")
    with _writer():
        original = jsonc.read_snapshot(policy_path(), missing_ok=True)
        data = _read()
        if data["clients"][client]["enabled"] == value and original.identity is not None:
            return data
        if data["revision"] >= _MAX_GENERATION:
            raise RuntimeError("Connection settings need recovery; access stays off.")
        data["revision"] += 1
        data["clients"][client] = {"enabled": value, "generation": data["revision"]}
        jsonc.atomic_write(policy_path(), json.dumps(data, separators=(",", ":")) + "\n", expected=original)
        return data


def initialize(client: str | None = None) -> dict:
    """Create first-use state without ever changing an existing user's preference."""
    if client is not None and client not in CLIENT_KEYS:
        raise ValueError("Unknown environment")
    with _writer():
        original = jsonc.read_snapshot(policy_path(), missing_ok=True)
        if original.identity is not None:
            return _read()
        data = _default()
        if client is not None:
            data["revision"] = 1
            data["clients"][client] = {"enabled": True, "generation": 1}
        jsonc.atomic_write(policy_path(), json.dumps(data, separators=(",", ":")) + "\n", expected=original)
        return data
