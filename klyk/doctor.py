"""
klyk doctor — health check for the install + permissions + runtime
state. Runs every dependency, every permission, every config grant klyk
needs in order to function, and reports each as ok / warn / fail with the
exact next-step remedy on anything that's not green.

Two output modes:
  - human-readable (default): one line per check, colored marker, indented
    remedy on yellow/red.
  - JSON (`--json` flag): structured array of {name, status, detail,
    remedy}, suitable for piping into other tooling.

Used in two places:
  1. `klyk doctor` — on-demand health check the user runs when
     something's off. Returns rc=0 if every check is ok, rc=1 otherwise
     so it composes with `&&` in scripts.
  2. `klyk install` — the same check functions are invoked
     individually during install so we can verify each permission grant
     immediately after the user comes back from System Settings.

Every check is non-raising: it returns a CheckResult regardless of what
the underlying API does. The doctor never crashes; it always produces a
status the user can act on.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path


# Minimum macOS major version klyk is verified against. SkyLight has been
# stable across macOS 14+; earlier versions may work but aren't tested.
_MIN_MACOS_MAJOR = 14

# Minimum Python version klyk targets. Set by pyproject's requires-python.
_MIN_PY = (3, 11)


@dataclass
class CheckResult:
    name: str        # short label shown on the left of each line
    status: str      # "ok" | "warn" | "fail"
    detail: str      # one-line detail shown next to the label
    remedy: str = "" # multi-line remedy shown indented on warn/fail


# ---------------------------------------------------------------------------
# Individual checks. Each returns a CheckResult; never raises.
# ---------------------------------------------------------------------------


def check_platform() -> CheckResult:
    """klyk is macOS-only — pyobjc, SkyLight, AX, NSStatusBar all require it."""
    if sys.platform == "darwin":
        return CheckResult("Platform", "ok", "macOS (darwin)")
    return CheckResult(
        "Platform", "fail", f"not running on darwin (got {sys.platform})",
        "klyk is macOS-only. There's no path forward on other platforms — "
        "use klyk on a Mac, or use Playwright MCP for cross-platform browser work.",
    )


def check_macos_version() -> CheckResult:
    """Catch ancient macOS where SkyLight or other private framework signatures
    might differ. Warn rather than fail — older versions sometimes work."""
    if sys.platform != "darwin":
        return CheckResult("macOS version", "warn", "skipped (not darwin)")
    ver = platform.mac_ver()[0]
    try:
        major = int(ver.split(".")[0])
    except (ValueError, IndexError):
        return CheckResult("macOS version", "warn", f"could not parse {ver!r}")
    if major >= _MIN_MACOS_MAJOR:
        return CheckResult("macOS version", "ok", f"macOS {ver}")
    return CheckResult(
        "macOS version", "warn", f"macOS {ver} (tested on {_MIN_MACOS_MAJOR}+)",
        f"klyk is verified against macOS {_MIN_MACOS_MAJOR} and later. "
        "Earlier versions may work but private framework signatures sometimes "
        "drift between releases. If something breaks, the most likely culprit "
        "is the SkyLight binding in klyk/skylight.py.",
    )


def check_python_version() -> CheckResult:
    """pyproject.toml requires >=3.11."""
    v = sys.version_info
    if (v.major, v.minor) >= _MIN_PY:
        return CheckResult(
            "Python version", "ok",
            f"{v.major}.{v.minor}.{v.micro}",
        )
    return CheckResult(
        "Python version", "fail",
        f"{v.major}.{v.minor}.{v.micro} (need {_MIN_PY[0]}.{_MIN_PY[1]}+)",
        f"Upgrade to Python {_MIN_PY[0]}.{_MIN_PY[1]} or newer. With Homebrew: "
        f"`brew install python@{_MIN_PY[0]}.{_MIN_PY[1]}`. Then "
        f"`pip install --upgrade klyk`.",
    )


def check_pyobjc_imports() -> CheckResult:
    """klyk depends on AppKit, Foundation, Quartz from pyobjc. Verify
    they load."""
    if sys.platform != "darwin":
        return CheckResult("PyObjC imports", "warn", "skipped (not darwin)")
    missing: list[str] = []
    for mod in ("AppKit", "Foundation", "Quartz"):
        try:
            importlib.import_module(mod)
        except Exception as e:
            missing.append(f"{mod} ({type(e).__name__})")
    if not missing:
        return CheckResult(
            "PyObjC imports", "ok", "AppKit, Foundation, Quartz",
        )
    return CheckResult(
        "PyObjC imports", "fail", f"could not load: {', '.join(missing)}",
        "Reinstall the macOS bindings: "
        "`pip install --upgrade --force-reinstall pyobjc-framework-Quartz "
        "pyobjc-framework-Vision`. If the failure persists, your Python "
        "install is likely broken — try installing klyk with `pipx install "
        "klyk` to get an isolated env.",
    )


def check_skylight() -> CheckResult:
    """SkyLight is the private framework that powers invisible input. If
    it's not loadable, klyk still works in humanoid mode (cursor warp);
    the seamless modes degrade to cursor-warp automatically."""
    if sys.platform != "darwin":
        return CheckResult("SkyLight framework", "warn", "skipped (not darwin)")
    try:
        from . import skylight  # noqa
        if skylight.is_available():
            if skylight.keywin_available():
                return CheckResult(
                    "SkyLight framework", "ok",
                    "loaded (invisible input + key-window routing available)",
                )
            return CheckResult(
                "SkyLight framework", "ok",
                "loaded; key-window routing unavailable — clicks deliver raw",
                "Invisible clicks still work, but the key-window helper "
                "(make_window_key) didn't resolve its symbols, so clicks land as "
                "raw posts: simple controls (buttons, menus) work, but text-caret "
                "and list/row selection on a backgrounded window may not. Most "
                "likely a macOS change to a private symbol — file an issue.",
            )
        return CheckResult(
            "SkyLight framework", "warn",
            "loadable but required symbols missing",
            "klyk still works in humanoid mode (cursor moves visibly). "
            "Invisible/seamless modes won't be available. Most likely a "
            "macOS update changed a private symbol — file an issue.",
        )
    except Exception as e:
        return CheckResult(
            "SkyLight framework", "warn",
            f"could not check: {type(e).__name__}: {e}",
            "klyk will fall back to humanoid mode for all input.",
        )


def check_skylight_delivery() -> CheckResult:
    """Beyond loading, does SkyLight actually DELIVER a stamped click on this
    macOS build? is_available() only confirms the private symbols resolved; a
    macOS update can keep them while changing delivery semantics so clicks
    silently no-op (the dangerous failure mode). This runs the in-process
    delivery self-test — an off-screen sink, no focus change, no cursor move —
    and reports whether a real stamped click landed."""
    if sys.platform != "darwin":
        return CheckResult("SkyLight delivery", "warn", "skipped (not darwin)")
    try:
        from . import skylight
        if not skylight.is_available():
            return CheckResult(
                "SkyLight delivery", "warn", "skipped — SkyLight not loaded",
                "The framework didn't load (see the check above), so there's "
                "nothing to delivery-test. klyk runs in humanoid mode "
                "(visible cursor).",
            )
        ok = skylight.self_test(timeout=0.5) or skylight.self_test(timeout=0.5)
        if ok:
            return CheckResult(
                "SkyLight delivery", "ok",
                "stamped click delivered to in-process sink",
            )
        if skylight.delivery_verified() is False:
            return CheckResult(
                "SkyLight delivery", "fail",
                "loads but does NOT deliver — invisible clicks would silently no-op",
                "SkyLight resolved its symbols but a real stamped click did not "
                "reach an in-process test window. This is the silent-failure "
                "mode: a macOS update likely changed the private delivery API. "
                "klyk auto-falls-back to the visible cursor (autonomous mode) "
                "so clicks still land — you just lose the invisible path. Please "
                "file an issue with your exact macOS version.",
            )
        return CheckResult(
            "SkyLight delivery", "warn",
            "inconclusive — could not run the delivery self-test",
            "The self-test harness couldn't be built on this run. klyk "
            "proceeds normally; invisible delivery is unverified. Re-run "
            "`klyk doctor`.",
        )
    except Exception as e:
        return CheckResult(
            "SkyLight delivery", "warn", f"could not check: {type(e).__name__}",
            "klyk proceeds normally; invisible delivery is unverified this run.",
        )


def check_klyk_call() -> CheckResult:
    """The `klyk-call` CLI lets any shell-driven agent drive klyk even without
    native MCP. Verify its module imports."""
    try:
        from . import client  # noqa: F401
    except Exception as e:
        return CheckResult(
            "klyk-call (shell front door)", "warn",
            f"unavailable: klyk.client ({type(e).__name__})",
            "klyk-call lets any shell agent drive klyk without native MCP. "
            "Reinstall: `pip install --upgrade klyk`. Native stdio MCP still "
            "works without it.",
        )
    return CheckResult(
        "klyk-call (shell front door)", "ok", "CLI ready",
    )


def check_accessibility_permission() -> CheckResult:
    """AX must be granted to the process running klyk (typically the
    terminal app like Ghostty / Terminal / iTerm2). Read via
    AXIsProcessTrustedWithOptions(NULL) so we don't prompt the user
    just by checking."""
    if sys.platform != "darwin":
        return CheckResult("Accessibility permission", "warn", "skipped (not darwin)")
    try:
        # Lazy import — computer.py is a heavy module.
        from . import computer
        trusted = computer._appserv.AXIsProcessTrustedWithOptions(None)
    except Exception as e:
        return CheckResult(
            "Accessibility permission", "fail",
            f"check itself failed: {type(e).__name__}",
            "Could not query the OS for permission state. This usually means "
            "klyk itself isn't installed correctly. Re-run "
            "`pip install --upgrade klyk`.",
        )
    if trusted:
        return CheckResult(
            "Accessibility permission", "ok", "granted to this process",
        )
    return CheckResult(
        "Accessibility permission", "fail", "NOT granted",
        "Without Accessibility, klyk can't read the AX tree (no "
        "inspect/click_element targeting) or post keyboard events. Grant it:\n"
        "  1. System Settings → Privacy & Security → Accessibility\n"
        "  2. Click + and add the terminal app you launch your MCP client from "
        "(Ghostty, Terminal, iTerm2, or the MCP client itself if it embeds Python).\n"
        "  3. Toggle it ON.\n"
        "  4. Re-run `klyk doctor` to verify.",
    )


def check_screen_recording_permission() -> CheckResult:
    """Screen Recording is needed for take_screenshot. Verify by running
    screencapture on a temp file — if it produces a non-empty PNG, perm
    is granted. If the file is missing or tiny, the OS denied it
    silently."""
    if sys.platform != "darwin":
        return CheckResult("Screen Recording permission", "warn", "skipped (not darwin)")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", tmp_path],
            capture_output=True, timeout=5,
        )
        ok = (
            result.returncode == 0
            and os.path.exists(tmp_path)
            and os.path.getsize(tmp_path) >= 100
        )
        if ok:
            return CheckResult(
                "Screen Recording permission", "ok", "granted to this process",
            )
        return CheckResult(
            "Screen Recording permission", "fail", "NOT granted",
            "Without Screen Recording, klyk can't capture window contents "
            "(no screenshots, no inspect images, no read_grid). Grant it:\n"
            "  1. System Settings → Privacy & Security → Screen Recording\n"
            "  2. Click + and add your terminal app (same one as Accessibility).\n"
            "  3. Toggle it ON.\n"
            "  4. Re-run `klyk doctor` to verify.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "Screen Recording permission", "warn",
            "screencapture timed out — could not verify",
            "If screenshots fail at runtime, grant Screen Recording: "
            "System Settings → Privacy & Security → Screen Recording.",
        )
    except Exception as e:
        return CheckResult(
            "Screen Recording permission", "warn",
            f"check failed: {type(e).__name__}: {e}",
        )
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def check_klyk_dir_writable() -> CheckResult:
    """klyk writes ~/.klyk/owner for the control-ownership token and
    appends to ~/klyk.log for diagnostics. If the parent dir isn't
    writable, things degrade silently."""
    klyk_dir = Path.home() / ".klyk"
    try:
        klyk_dir.mkdir(parents=True, exist_ok=True)
        probe = klyk_dir / ".doctor_probe"
        probe.write_text("ok\n")
        probe.unlink()
        return CheckResult("~/.klyk/ writable", "ok", str(klyk_dir))
    except Exception as e:
        return CheckResult(
            "~/.klyk/ writable", "fail",
            f"{klyk_dir}: {type(e).__name__}: {e}",
            "klyk needs ~/.klyk/ for the control-ownership token and metadata. "
            "Check that your home directory is writable and not on a "
            "read-only volume.",
        )


def check_klyk_log_writable() -> CheckResult:
    """klyk logs to ~/klyk.log. Append-only; if the file is locked or
    the parent isn't writable, logging silently degrades."""
    log_path = Path.home() / "klyk.log"
    try:
        with open(log_path, "a") as f:
            f.write("")  # touch
        return CheckResult("~/klyk.log writable", "ok", str(log_path))
    except Exception as e:
        return CheckResult(
            "~/klyk.log writable", "warn",
            f"{log_path}: {type(e).__name__}",
            "klyk will still run but logs are disabled. Check the parent "
            "directory's write permissions.",
        )


def check_claude_json_entry() -> CheckResult:
    """Verify ~/.claude.json has a klyk entry pointing at a resolvable
    Python executable. Specific to Claude Code (which reads that file);
    other MCP clients have their own config paths and are NOT covered
    by this check — that's expected, this warn is purely "Claude Code
    isn't configured to use klyk yet, run klyk install if you
    want it to be"."""
    claude_path = Path.home() / ".claude.json"
    if not claude_path.exists():
        return CheckResult(
            "~/.claude.json klyk entry", "warn",
            "~/.claude.json not present",
            "Run `klyk install` to add klyk to Claude Code's MCP "
            "config. If you only use klyk via a different MCP client "
            "(Cursor, Cline, Continue, Windsurf, etc.), ignore this — "
            "those clients have their own MCP config locations. See the "
            "README's `Use with other MCP clients` section for the snippet.",
        )
    try:
        with open(claude_path) as f:
            cfg = json.load(f)
    except Exception as e:
        return CheckResult(
            "~/.claude.json klyk entry", "fail",
            f"could not parse: {type(e).__name__}",
            f"~/.claude.json is corrupted JSON ({e}). Fix the file manually "
            "and re-run `klyk doctor`.",
        )
    servers = cfg.get("mcpServers") or {}
    entry = servers.get("klyk")
    if not entry:
        return CheckResult(
            "~/.claude.json klyk entry", "warn",
            "klyk not configured in Claude Code",
            "Run `klyk install` to add it. Other MCP clients use their "
            "own config files (see README) and are not checked here.",
        )
    cmd = entry.get("command", "")
    args = entry.get("args", [])
    resolved = shutil.which(cmd) if cmd else None
    if not resolved:
        return CheckResult(
            "~/.claude.json klyk entry", "fail",
            f"command {cmd!r} does not resolve on PATH",
            f"Claude Code will fail to spawn klyk because {cmd!r} isn't on "
            "PATH. Re-run `klyk install` to refresh the entry, or edit "
            "~/.claude.json to point at a valid Python (`which python3` "
            "gives you a working path).",
        )
    # Verify the path klyk.mcp_server or the legacy shim resolves
    target = args[-1] if args else ""
    if target.startswith("/") and not Path(target).exists():
        return CheckResult(
            "~/.claude.json klyk entry", "fail",
            f"target script {target!r} does not exist",
            f"~/.claude.json points at {target!r} but the file isn't there. "
            "Re-run `klyk install` to point at the installed package.",
        )
    return CheckResult(
        "~/.claude.json klyk entry", "ok",
        f"{cmd} {' '.join(args)}",
    )


def check_control_owner() -> CheckResult:
    """Report which klyk session currently owns control (the active driver).
    Purely INFORMATIONAL — control is a transferable token, not a health gate:
    the newest session always becomes the driver, and a superseded session is
    blocked only when it tries a control action, reclaiming with one
    `take_control` call. 'free' just means no klyk is running right now."""
    try:
        from . import ownership
        owner = ownership.current_owner()
    except Exception as e:
        return CheckResult("Control owner", "warn", f"could not check: {type(e).__name__}")
    if not owner:
        return CheckResult("Control owner", "ok", "free — no klyk running")
    alive = True
    try:
        os.kill(owner, 0)
    except OSError:
        alive = False
    if alive:
        return CheckResult("Control owner", "ok", f"pid {owner} (active driver)")
    return CheckResult(
        "Control owner", "ok",
        f"pid {owner} (stale — the next session takes over automatically)",
    )


# ---------------------------------------------------------------------------
# Aggregator + formatters
# ---------------------------------------------------------------------------


def run_all_checks() -> list[CheckResult]:
    """Run every check in a deterministic order and return the list.
    Order is logical (env first, then permissions, then config, then
    runtime state) so a fresh user reads the output top-to-bottom and
    fixes earlier items before later items."""
    return [
        check_platform(),
        check_macos_version(),
        check_python_version(),
        check_pyobjc_imports(),
        check_skylight(),
        check_skylight_delivery(),
        check_klyk_call(),
        check_klyk_dir_writable(),
        check_klyk_log_writable(),
        check_accessibility_permission(),
        check_screen_recording_permission(),
        check_claude_json_entry(),
        check_control_owner(),
    ]


_GLYPH = {"ok": "✓", "warn": "⚠", "fail": "✗"}


def format_text(results: list[CheckResult]) -> str:
    """Human-readable, colored-marker output. The marker glyph plus the
    short label plus a one-line detail are aligned; the remedy (if any)
    appears indented under the line on yellow/red."""
    out: list[str] = []
    name_width = max(len(r.name) for r in results) + 2
    for r in results:
        marker = _GLYPH.get(r.status, "?")
        out.append(f"  {marker} {r.name:<{name_width}} {r.detail}")
        if r.status != "ok" and r.remedy:
            for line in r.remedy.splitlines():
                out.append(f"      {line}")
    fails = sum(1 for r in results if r.status == "fail")
    warns = sum(1 for r in results if r.status == "warn")
    if fails == 0 and warns == 0:
        out.append("")
        out.append("All checks passed. klyk is ready.")
    else:
        out.append("")
        parts = []
        if fails:
            parts.append(f"{fails} failure{'s' if fails != 1 else ''}")
        if warns:
            parts.append(f"{warns} warning{'s' if warns != 1 else ''}")
        out.append(
            f"Result: {' and '.join(parts)}. Fix the items above, then "
            "re-run `klyk doctor` to verify."
        )
    return "\n".join(out)


def format_json(results: list[CheckResult]) -> str:
    """JSON output for tooling. Stable schema: array of objects with
    name / status / detail / remedy keys."""
    return json.dumps([asdict(r) for r in results], indent=2)


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.status == "fail" for r in results)
