"""
Update awareness + self-update plumbing for klyk.

One module owns everything version-freshness related, so the CLI, the
doctor, and the menu-bar all read the same state and can never disagree:

  - check():   cached, at-most-once-a-day, offline-safe lookup of the latest
               klyk release on PyPI (package metadata only — nothing about
               the user or their screen is ever sent). Never raises; uses a short
               socket timeout and bounded metadata read budget.
               Disabled entirely with KLYK_UPDATE_CHECK=0.
  - status():  the last known answer, read from the shared cache file with
               no network I/O — cheap enough for the menu-bar rebuild.
  - install_method() / upgrade_command(): how this klyk was installed
               (pipx / uv tool / uvx / pip / editable), derived from the
               interpreter + package paths — so `klyk update` runs the ONE
               correct upgrade command for every install style.

Shared state: ~/.klyk/update_check.json — {"checked_at", "latest"}.
A single small file, overwritten atomically in place (never grows), and
named in every log line so a stale entry is always diagnosable. Using a
file (not process memory) is deliberate: `klyk update` in a terminal and a
long-lived MCP server are different processes, and both must see the same
freshness state immediately.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import sys
import threading
import tempfile
import time
import urllib.request
from pathlib import Path

from .private_files import private_directory

from . import __version__

log = logging.getLogger("klyk.updates")

_CACHE_PATH = Path.home() / ".klyk" / "update_check.json"
_TTL_S = 24 * 3600           # re-ask PyPI at most once per day
_FETCH_TIMEOUT_S = 3.0       # per-socket timeout and metadata read budget
_PYPI_URL = "https://pypi.org/pypi/klyk/json"
_MAX_CACHE_BYTES = 16 * 1024
_MAX_METADATA_BYTES = 1024 * 1024

# In-process memoization on file identity so status() is a
# stat() in the common case — safe to call from the throttled menu rebuild.
_memo_lock = threading.Lock()
_memo_mtime: tuple | None = None
_memo_data: dict | None = None


def enabled() -> bool:
    """The check is on by default; KLYK_UPDATE_CHECK=0 (or false/no) opts out."""
    return os.environ.get("KLYK_UPDATE_CHECK", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _parse(version: str) -> tuple[int, ...] | None:
    """Tolerant numeric parse of an X.Y.Z version. Returns None when a part
    isn't numeric (pre-releases etc.) — callers then compare conservatively
    (no update signalled on an unparseable pair)."""
    if (not isinstance(version, str) or len(version) > 64
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)):
        return None
    return tuple(int(p) for p in version.split("."))


def _is_newer(latest: str, installed: str) -> bool:
    """True only when `latest` is strictly newer than `installed`. Unparseable
    versions never signal an update — a false 'update available' nag is worse
    than a missed one (the daily re-check catches up)."""
    a, b = _parse(latest), _parse(installed)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _read_cache() -> dict | None:
    """Read bounded regular cache metadata, memoized on its complete file identity."""
    global _memo_mtime, _memo_data
    try:
        info = _CACHE_PATH.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > _MAX_CACHE_BYTES):
            return None
        signature = (info.st_dev, info.st_ino, info.st_size,
                     info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return None
    with _memo_lock:
        if _memo_mtime == signature and _memo_data is not None:
            return _memo_data
    fd = None
    try:
        fd = os.open(_CACHE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_size > _MAX_CACHE_BYTES
                or (opened.st_dev, opened.st_ino, opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns) != signature):
            return None
        raw = os.read(fd, _MAX_CACHE_BYTES + 1)
        after = os.fstat(fd)
        if (len(raw) > _MAX_CACHE_BYTES
                or (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns) != signature):
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cache is not a JSON object")
        checked_at = data.get("checked_at")
        if (isinstance(checked_at, bool) or not isinstance(checked_at, (int, float))
                or not 0 <= checked_at <= 253402300799 or not math.isfinite(checked_at)):
            raise ValueError("cache checked_at must be a finite nonnegative timestamp")
        if data.get("latest") is not None and _parse(data["latest"]) is None:
            raise ValueError("cache latest must be a version string or null")
    except (OSError, ValueError, RecursionError) as e:
        log.warning("update cache %s unreadable (%s) — will refetch", _CACHE_PATH, type(e).__name__)
        return None
    finally:
        if fd is not None:
            os.close(fd)
    with _memo_lock:
        _memo_mtime, _memo_data = signature, data
    return data


def _write_cache(latest: str | None) -> None:
    """Atomically overwrite the cache. latest=None records a failed fetch so
    we still respect the TTL and don't hammer PyPI while offline."""
    data = {"checked_at": time.time(), "latest": latest}
    tmp = None
    try:
        if _CACHE_PATH.parent == Path.home() / ".klyk":
            private_directory(_CACHE_PATH.parent)
        else:
            _CACHE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Each process owns its temporary file; concurrent servers cannot
        # truncate or rename one another's pending cache writes.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=_CACHE_PATH.parent, prefix=".update-check-", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(json.dumps(data))
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        log.warning("could not write update cache %s: %s", _CACHE_PATH, e)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the metadata request on its fixed HTTPS PyPI endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Treat redirects as an unavailable check instead of changing trust domains."""
        return None


def _fetch_latest() -> str | None:
    """Read bounded metadata from PyPI under a short socket and read-time budget."""
    try:
        req = urllib.request.Request(
            _PYPI_URL, headers={"User-Agent": f"klyk/{__version__} update-check"},
        )
        deadline = time.monotonic() + _FETCH_TIMEOUT_S
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=_FETCH_TIMEOUT_S) as resp:
            raw = bytearray()
            while len(raw) <= _MAX_METADATA_BYTES:
                if time.monotonic() >= deadline:
                    raise TimeoutError("metadata read budget exceeded")
                chunk = resp.read1(min(65536, _MAX_METADATA_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_METADATA_BYTES:
                raise ValueError("PyPI metadata exceeds the size limit")
            data = json.loads(raw)
            info = data.get("info", {}) if isinstance(data, dict) else {}
            latest = info.get("version") if isinstance(info, dict) else None
        if _parse(latest) is not None:
            log.info("update check: installed %s, latest on PyPI %s", __version__, latest)
            return latest
        log.warning("update check: PyPI response had no info.version")
    except Exception as e:
        log.info("update check: PyPI unavailable (%s) — skipped", type(e).__name__)
    return None


def check(force: bool = False) -> dict:
    """Refresh the freshness state, respecting the daily TTL unless forced.
    Always returns a status dict (see status()); never raises."""
    if not enabled():
        return status()
    cache = _read_cache()
    fresh = cache is not None and 0 <= (time.time() - cache["checked_at"]) < _TTL_S
    if force or not fresh:
        _write_cache(_fetch_latest())
    return status()


def status() -> dict:
    """Cache-only view — no network. Keys:
    installed, latest (None = never successfully checked), update_available,
    checked_at (None = never checked), enabled."""
    cache = _read_cache() if enabled() else None
    latest = cache.get("latest") if cache else None
    return {
        "installed": __version__,
        "latest": latest,
        "update_available": bool(latest) and _is_newer(latest, __version__),
        "checked_at": cache.get("checked_at") if cache else None,
        "enabled": enabled(),
    }


def start_background_check(on_checked=None) -> None:
    """Daemon thread for the long-lived server: run check() now, then every
    6 h (the TTL still limits real fetches to one/day). Calls on_checked()
    after EVERY pass — not just when an update appears — so the menu-bar
    line also clears promptly after an update lands (going stale in either
    direction is the failure mode)."""
    if not enabled():
        log.info("update check disabled (KLYK_UPDATE_CHECK=0)")
        return
    log.info("update check: background thread started (daily; cache %s)", _CACHE_PATH)

    def _loop() -> None:
        while True:
            try:
                check()
                if on_checked is not None:
                    on_checked()
            except Exception as e:
                log.warning("background update check failed: %s: %s", type(e).__name__, e)
            time.sleep(6 * 3600)

    threading.Thread(target=_loop, name="klyk-update-check", daemon=True).start()


# ---------------------------------------------------------------------------
# Install-method detection — which upgrade command actually works here.
# ---------------------------------------------------------------------------


def _detect_method(prefix: str, pkg_file: str) -> str:
    """Pure classifier (unit-testable): where is this klyk running from?
    'editable' — a source checkout (pip install -e / repo on sys.path);
    'pipx' / 'uv' — their managed tool venvs; 'pip' — anything else."""
    if "site-packages" not in pkg_file and "dist-packages" not in pkg_file:
        return "editable"
    p = prefix.replace("\\", "/")
    if "/pipx/venvs/" in p:
        return "pipx"
    if "/uv/tools/" in p:
        return "uv"
    if "/uv/archive-v" in p:
        return "uvx"
    return "pip"


def install_method() -> str:
    """Include custom managed directories and disposable uvx environments."""
    from . import __file__ as pkg_file
    if pkg_file and ("site-packages" in pkg_file or "dist-packages" in pkg_file):
        prefix = Path(sys.prefix)
        if (prefix / "uv-receipt.toml").is_file():
            return "uv"
        if (prefix / "pipx_metadata.json").is_file():
            return "pipx"
        custom_cache = os.environ.get("UV_CACHE_DIR")
        if custom_cache and prefix.resolve().is_relative_to(Path(custom_cache).expanduser().resolve()):
            return "uvx"
    return _detect_method(sys.prefix, pkg_file or "")


def upgrade_command(method: str | None = None) -> list[str] | None:
    """The one shell command that upgrades THIS install. None for editable
    (a dev checkout updates via git, not pip)."""
    m = method or install_method()
    return {
        "pipx": ["pipx", "upgrade", "klyk"],
        "uv": ["uv", "tool", "upgrade", "klyk"],
        "uvx": ["uv", "tool", "run", "--upgrade", "--from", "klyk", "klyk", "version"],
        "pip": [sys.executable, "-P", "-m", "pip", "install", "--upgrade", "klyk"],
        "editable": None,
    }[m]
