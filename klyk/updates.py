"""
Update awareness + self-update plumbing for klyk.

One module owns everything version-freshness related, so the CLI, the
doctor, and the menu-bar all read the same state and can never disagree:

  - check():   cached, at-most-once-a-day, offline-safe lookup of the latest
               klyk release on PyPI (package metadata only — nothing about
               the user or their screen is ever sent). Never raises, never
               blocks longer than the socket timeout, logs every outcome.
               Disabled entirely with KLYK_UPDATE_CHECK=0.
  - status():  the last known answer, read from the shared cache file with
               no network I/O — cheap enough for the menu-bar rebuild.
  - install_method() / upgrade_command(): how this klyk was installed
               (pipx / uv tool / pip / editable), derived from the
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
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger("klyk.updates")

_CACHE_PATH = Path.home() / ".klyk" / "update_check.json"
_TTL_S = 24 * 3600           # re-ask PyPI at most once per day
_FETCH_TIMEOUT_S = 3.0       # hard cap on the network wait
_PYPI_URL = "https://pypi.org/pypi/klyk/json"

# In-process memoization of the cache file (mtime-keyed) so status() is a
# stat() in the common case — safe to call from the throttled menu rebuild.
_memo_lock = threading.Lock()
_memo_mtime: float | None = None
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
    try:
        return tuple(int(p) for p in version.strip().split("."))
    except (ValueError, AttributeError):
        return None


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
    """Read the cache file, memoized on mtime. Returns None when absent or
    unreadable (a corrupt cache heals itself on the next check())."""
    global _memo_mtime, _memo_data
    try:
        mtime = _CACHE_PATH.stat().st_mtime
    except OSError:
        return None
    with _memo_lock:
        if _memo_mtime == mtime and _memo_data is not None:
            return _memo_data
    try:
        data = json.loads(_CACHE_PATH.read_text())
        if not isinstance(data, dict):
            raise ValueError("cache is not a JSON object")
    except (OSError, ValueError) as e:
        log.warning("update cache %s unreadable (%s) — will refetch", _CACHE_PATH, e)
        return None
    with _memo_lock:
        _memo_mtime, _memo_data = mtime, data
    return data


def _write_cache(latest: str | None) -> None:
    """Atomically overwrite the cache. latest=None records a failed fetch so
    we still respect the TTL and don't hammer PyPI while offline."""
    data = {"checked_at": time.time(), "latest": latest}
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _CACHE_PATH)
    except OSError as e:
        log.warning("could not write update cache %s: %s", _CACHE_PATH, e)


def _fetch_latest() -> str | None:
    """Ask PyPI for the newest published klyk version. Never raises."""
    try:
        req = urllib.request.Request(
            _PYPI_URL, headers={"User-Agent": f"klyk/{__version__} update-check"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            latest = json.load(resp).get("info", {}).get("version")
        if isinstance(latest, str) and latest:
            log.info("update check: installed %s, latest on PyPI %s", __version__, latest)
            return latest
        log.warning("update check: PyPI response had no info.version")
    except Exception as e:
        log.info("update check: PyPI unreachable (%s: %s) — skipped", type(e).__name__, e)
    return None


def check(force: bool = False) -> dict:
    """Refresh the freshness state, respecting the daily TTL unless forced.
    Always returns a status dict (see status()); never raises."""
    if not enabled():
        return status()
    cache = _read_cache()
    fresh = cache is not None and (time.time() - cache.get("checked_at", 0)) < _TTL_S
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
    return "pip"


def install_method() -> str:
    from . import __file__ as pkg_file
    return _detect_method(sys.prefix, pkg_file or "")


def upgrade_command(method: str | None = None) -> list[str] | None:
    """The one shell command that upgrades THIS install. None for editable
    (a dev checkout updates via git, not pip)."""
    m = method or install_method()
    return {
        "pipx": ["pipx", "upgrade", "klyk"],
        "uv": ["uv", "tool", "upgrade", "klyk"],
        "pip": [sys.executable, "-m", "pip", "install", "--upgrade", "klyk"],
        "editable": None,
    }[m]
