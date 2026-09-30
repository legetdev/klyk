"""CLI for klyk.

The CLI is the user's primary contact surface — `pip install klyk`
gives them the `klyk` command, and from there:

    klyk install [client]   — add klyk to an MCP client's config, grant macOS
                                   permissions, run a health check. One command,
                                   no prompts. Defaults to Claude Code; pass a
                                   client (cursor, windsurf, continue, cline,
                                   codex, opencode, gemini, antigravity/agy,
                                   grok) for another.
                                   Flags: --all (wire every detected client),
                                   --ambient (add a shell-fallback note to the
                                   client's context file), --wait (poll until you
                                   grant permissions), --list (show all clients).
    klyk update             — update klyk to the latest release. Detects how
                                   klyk was installed (pipx / uv / pip) and runs
                                   the matching upgrade, then restarts the
                                   running server so every connected agent gets
                                   the new version on its next call.
                                   --check only reports if an update exists.
    klyk doctor [--fix]     — green/yellow/red health check; --fix auto-repairs
                                   what it can (state dir, config entry).
    klyk restart            — stop the klyk instance currently driving the Mac
                                   (only needed to force a wedged one).
    klyk uninstall [client] — remove klyk. No client → FULL removal (every
                                   client, state, binaries); a client name
                                   removes just that one.
    klyk help / version     — show this message / print the package version.

Goal: a brand-new Mac user runs `pip install klyk && klyk install` — one
command, no prompts (just a one-time, two-click macOS permission grant) — and
klyk works, for any client and any AI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import clients

# Deep links to the exact System Settings panes for the two permissions
# klyk needs. These open the right tab in macOS Settings so the user
# doesn't have to navigate the tree.
_PRIVACY_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_PRIVACY_SCREEN = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "install"
    rest = args[1:]
    if "--help" in rest or "-h" in rest:
        _help()
        return
    flags = {
        "install": {"--all", "--ambient", "--wait", "--list"},
        "uninstall": {"--all"}, "update": {"--check"},
        "doctor": {"--fix", "--json"}, "restart": set(),
        "help": set(), "version": set(), "--version": set(), "-v": set(),
    }
    if cmd in flags:
        positionals = [arg for arg in rest if not arg.startswith("-")]
        unknown = [arg for arg in rest if arg.startswith("-") and arg not in flags[cmd]]
        if (unknown or len(positionals) > (1 if cmd in ("install", "uninstall") else 0)
                or (positionals and "--all" in rest)):
            print(f"Invalid arguments for `klyk {cmd}`. Run `klyk {cmd} --help`.", file=sys.stderr)
            sys.exit(2)
    if cmd == "install":
        _install(rest)
    elif cmd == "uninstall":
        _uninstall(rest)
    elif cmd == "update":
        _update(rest)
    elif cmd == "doctor":
        _doctor(rest)
    elif cmd == "restart":
        _restart()
    elif cmd in ("help", "--help", "-h"):
        _help()
    elif cmd in ("version", "--version", "-v"):
        from . import __version__
        print(f"klyk {__version__}")
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Run `klyk help` for available commands.", file=sys.stderr)
        sys.exit(1)


def _help() -> None:
    print(__doc__)


def _require_macos() -> None:
    if sys.platform != "darwin":
        print(
            "klyk is macOS-only. Detected platform: " + sys.platform,
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor(rest: list[str]) -> None:
    if "--fix" in rest:
        _doctor_fix()
        return
    from .doctor import run_all_checks, format_text, format_json, has_failures
    json_out = "--json" in rest
    results = run_all_checks()
    if json_out:
        print(format_json(results))
    else:
        print(format_text(results))
    sys.exit(1 if has_failures(results) else 0)


def _doctor_fix() -> None:
    """Repair state plus stale configured-client entries, then re-run checks."""
    if not _persistent_install_required():
        sys.exit(1)
    home = Path.home()
    fixed: list[str] = []
    kdir = home / ".klyk"
    if not kdir.exists():
        try:
            kdir.mkdir(parents=True, exist_ok=True)
            fixed.append("created ~/.klyk")
        except Exception as e:
            print(f"  ⚠ could not create ~/.klyk: {e}")
    for c in clients.CLIENTS.values():
        try:
            existing = clients.current_entry(c)
            default_claude = c.key == "claude" and clients.is_present(c)
            legacy_agy = existing is None and clients.legacy_antigravity_entry(c) is not None
            legacy_opencode = existing is None and clients.legacy_opencode_entry(c) is not None
            if (existing is not None or default_claude or legacy_agy or legacy_opencode) and existing != c.entry:
                if clients.write_entry(c) != "unchanged":
                    fixed.append(f"refreshed the {c.label} config entry")
        except Exception:
            # The full doctor output below reports the exact file and remedy.
            continue
    print("Repaired:" if fixed else "Nothing auto-repairable was off.")
    for f in fixed:
        print(f"  ✓ {f}")
    print()
    from .doctor import run_all_checks, format_text, has_failures
    results = run_all_checks()
    print(format_text(results))
    if has_failures(results):
        print("\nRemaining items need you (e.g. granting the macOS permissions).")
        sys.exit(1)


def _update(rest: list[str]) -> None:
    """Update klyk to the latest published release, using the upgrade command
    that matches HOW klyk was installed (pipx / uv tool / pip — detected
    automatically), then restart the running server so every connected agent
    loads the new version on its next call. `--check` only reports whether
    an update exists, changing nothing."""
    from . import __version__ as before, updates

    if "--check" in rest:
        st = updates.check(force=True)
        if not st["enabled"]:
            print(f"klyk {before} — update checks are disabled by KLYK_UPDATE_CHECK=0.")
            return
        if st["latest"] is None:
            print(f"klyk {before} — could not reach PyPI to compare. "
                  "Check your connection and retry.")
            sys.exit(1)
        if st["update_available"]:
            print(f"Update available: {before} → {st['latest']}. Run `klyk update`.")
        else:
            print(f"klyk {before} is the latest release.")
        return

    method = updates.install_method()
    if method == "editable":
        print(f"klyk {before} is a development (editable) install — it tracks "
              "the source checkout, not PyPI. Update it with `git pull` in the repo.")
        return

    cmd = updates.upgrade_command(method)
    print(f"Updating klyk {before} (installed via {method}) — running: "
          f"{' '.join(cmd)}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           env={**os.environ, "PYTHONPATH": ""})
    except FileNotFoundError:
        print(f"✗ `{cmd[0]}` is not on PATH, but this klyk lives in a {method}-"
              f"managed environment. Install {cmd[0]} (or reinstall klyk with "
              "`pipx install klyk`), then re-run `klyk update`.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("✗ Update did not finish within ten minutes. Check the installation before retrying.", file=sys.stderr)
        sys.exit(1)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout)
        print(f"✗ Update failed (see the {cmd[0]} output above).", file=sys.stderr)
        sys.exit(1)

    if method == "uvx":
        # uvx creates a fresh cached environment; the old interpreter remains
        # disposable and must not verify or mutate the replacement environment.
        match = re.fullmatch(r"klyk\s+([0-9]+(?:\.[0-9]+){1,3})", r.stdout.strip())
        after = match[1] if match else None
    else:
        try:
            verified = subprocess.run(
                [sys.executable, "-P", "-c", "import klyk; print(klyk.__version__)"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "PYTHONPATH": ""},
            )
            after = verified.stdout.strip() if verified.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            after = None
    if after is None or updates._parse(after) is None:
        print("✗ The upgrade command finished, but the updated package could not be loaded. No server was restarted.", file=sys.stderr)
        sys.exit(1)
    # Record the fresh state so the doctor and menu-bar stop showing a
    # now-stale "update available" the moment the upgrade lands.
    state = updates.check(force=True)
    if state["latest"] is not None and updates._is_newer(state["latest"], after):
        print("✗ The package manager finished, but this installation is still behind the published version. No server was restarted.", file=sys.stderr)
        sys.exit(1)

    if method == "uvx":
        print(f"✓ Refreshed the disposable uvx environment to klyk {after}.")
        print("For a persistent MCP setup, run `uv tool install klyk`, then `klyk install <client>`.")
        return

    if after == before:
        print(f"✓ This installation loads klyk {after}; no version change was needed.")
        return
    print(f"✓ Updated klyk {before} → {after}.")
    _restart_after_update()


def _installation_process(pid: int):
    """Bind restart permission to this user's exact interpreter and module command."""
    from .launcher import process_identity
    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 < pid <= 2147483647:
        return None
    identity = process_identity(pid, include_command=True)
    if not identity or identity.get("uid") != os.getuid():
        return None
    argv = identity.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return None
    if os.path.abspath(argv[0]) != os.path.abspath(sys.executable):
        return None
    if argv[1:] not in (["-m", "klyk.mcp_server"], ["-P", "-m", "klyk.mcp_server"]):
        return None
    return identity


def _restart_after_update() -> None:
    """Restart only verified servers using this installation, rechecking PID identity."""
    from .launcher import terminate_pid
    try:
        candidates = subprocess.run(
            ["/usr/bin/pgrep", "-u", str(os.getuid()), "-f", "klyk[.]mcp_server"],
            capture_output=True, text=True, timeout=3,
        )
        pids = {int(line) for line in candidates.stdout.splitlines() if line.isdigit()}
    except (OSError, subprocess.TimeoutExpired):
        pids = set()
    restarted = 0
    failed = 0
    for pid in sorted(pids):
        identity = _installation_process(pid)
        if identity is None:
            continue
        if terminate_pid(pid, expected_identity=identity):
            restarted += 1
        else:
            failed += 1
    if restarted:
        print(f"  ✓ Restarted {restarted} verified klyk server(s) using this installation.")
    else:
        print("  No matching server was stopped. New sessions load the updated installation.")
    if failed:
        print("  ⚠ Some servers could not be stopped safely. Restart their AI clients to load the update.")


# ---------------------------------------------------------------------------
# restart — stop a running/wedged instance, free the lock
# ---------------------------------------------------------------------------


def _restart() -> None:
    """Stop the klyk instance currently driving the Mac.

    Rarely needed now: the newest session automatically becomes the active
    driver (a connection is never blocked), and a superseded session reclaims
    with `take_control`. Use this only to force a wedged klyk process to exit."""
    from .ownership import current_owner
    from .launcher import terminate_pid

    owner = current_owner()
    if not owner:
        print("No klyk is currently running. The next session starts a fresh one.")
        return
    identity = _installation_process(owner)
    if identity is None:
        print("The recorded process could not be verified as this installation's klyk server. No process was stopped.", file=sys.stderr)
        sys.exit(1)
    print(f"Stopping the active klyk (pid {owner})…")
    if terminate_pid(owner, expected_identity=identity):
        print("✓ Stopped. The next session — or a take_control from another — becomes the driver.")
    else:
        print(
            "✗ The server could not be stopped safely. Restart the AI client that launched it.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# install — configure a client, grant permissions, verify
# ---------------------------------------------------------------------------


def _resolve_client(rest: list[str]):
    """Pick the client from args (default: claude). Returns a Client or exits."""
    key = next((a for a in rest if not a.startswith("-")), "claude")
    client = clients.get(key)
    if client is None:
        print(f"Unknown client: {key}\n", file=sys.stderr)
        print(clients.list_text(), file=sys.stderr)
        sys.exit(1)
    return client


def _configure_client(client) -> bool:
    """Refresh the client's klyk entry; print a paste snippet if it cannot be edited.
    Returns True if the client is left configured."""
    try:
        clients.current_entry(client)
    except Exception as e:
        path = clients.config_path(client)
        print(f"  ✗ Could not read {path}: {e}")
        print("    Fix the file or OpenCode version detection, then re-run.")
        return False

    # Idempotent: the "klyk" key is ours to manage, so we refresh it in place
    # with no prompt. write_entry() returns "unchanged" when it already matches.
    try:
        action = clients.write_entry(client)
    except clients.ManualEditRequired as e:
        print(f"  ⚠ {e}")
        print("    Add this block yourself:")
        print(_indent(e.snippet))
        return False
    except Exception as e:
        path = clients.config_path(client)
        print(f"  ✗ Could not write {path}: {e}")
        try:
            block = clients.snippet(client)
        except (OSError, ValueError):
            print("    Resolve the config or version error above before retrying.")
        else:
            print("    Add this manually instead:")
            print(_indent(block))
        return False

    path = clients.config_path(client)
    msg = {
        "added": f"✓ Added klyk to {client.label} ({path})",
        "updated": f"✓ Updated klyk in {client.label} ({path})",
        "unchanged": f"✓ klyk already configured in {client.label}",
    }[action]
    print(f"  {msg}")
    return True


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def _install(rest: list[str]) -> None:
    if "--list" in rest:
        print(clients.list_text())
        return

    _require_macos()
    if not _persistent_install_required():
        sys.exit(1)
    all_mode = "--all" in rest
    ambient = "--ambient" in rest  # opt-in: write the klyk-call note into GEMINI.md etc.
    wait = "--wait" in rest         # poll until macOS permissions are granted

    if all_mode:
        targets = [c for c in clients.CLIENTS.values() if clients.is_present(c)]
        if not targets:
            print("No supported AI clients detected on this Mac.")
            print("Install one (Claude Code, Cursor, Gemini CLI, …) and re-run,")
            print("or configure a specific client: klyk install <client>.")
            return
    else:
        targets = [_resolve_client(rest)]

    if any(c.key == "opencode" for c in targets) and _opencode_executable() is None:
        print("OpenCode CLI is not installed, so its MCP connection cannot be verified.")
        print("Install OpenCode, then re-run `klyk install opencode`.")
        sys.exit(1)

    print("klyk install")
    print("=================")
    print()

    # Step 1 — client MCP config(s).
    label = f"{len(targets)} detected clients" if all_mode else targets[0].label
    print(f"Step 1 — Configure {label}")
    failed: list = []
    for c in targets:
        if not _configure_client(c):
            failed.append(c)
    print()
    if failed:
        names = ", ".join(c.label for c in failed)
        print(f"Configuration failed for: {names}.")
        print("Nothing was silently skipped. Fix the file issue above and re-run the same command.")
        sys.exit(1)

    # Step 2 — macOS permissions (apply to the process running klyk, for any client).
    print("Step 2 — macOS permissions")
    print("  klyk needs two permissions to function. We'll check each,")
    print("  open System Settings if needed, and verify the grant came through.")
    print()

    from .doctor import (
        check_accessibility_permission,
        check_screen_recording_permission,
    )
    ax_ok = _verify_permission(
        "Accessibility", check_accessibility_permission, _PRIVACY_AX, wait,
    )
    print()
    sr_ok = _verify_permission(
        "Screen Recording", check_screen_recording_permission, _PRIVACY_SCREEN, wait,
    )
    print()

    # Step 3 — full doctor pass.
    print("Step 3 — Final health check")
    from .doctor import run_all_checks, format_text, has_failures
    results = run_all_checks()
    print()
    print(format_text(results))
    print()

    if has_failures(results):
        print("Some items above need attention. Fix them, then re-run `klyk doctor`.")
        sys.exit(1)

    # OpenCode runs MCP subprocesses under its own macOS responsibility chain,
    # so terminal permissions alone do not prove the real client can connect.
    # Its native status command is the only cheap end-to-end setup check.
    if any(c.key == "opencode" for c in targets):
        print("Step 4 — Verify OpenCode connection")
        if not _verify_opencode_connection():
            sys.exit(1)
        print()

    if ax_ok and sr_ok:
        if all_mode:
            print("Restart each client above to load klyk.")
        else:
            print(targets[0].note)
        print()
        print("Try it: ask your AI to `inspect Finder` to see klyk in action.")
    else:
        print("Permissions still pending. Finish granting them in System Settings,")
        print("then run `klyk doctor` to confirm everything's ready.")

    # Context-file note (GEMINI.md etc.) — opt-in via --ambient, never prompted.
    # Native MCP remains the default; shell access is an optional fallback.
    if ambient:
        for c in targets:
            if c.context_file is not None:
                _write_context_guide(c)

    # List other detected clients without changing their configuration. Each
    # launcher's macOS permission chain still needs its own verification.
    if not all_mode:
        print()
        _wire_other_clients(targets[0])


def _opencode_executable() -> str | None:
    """Resolve OpenCode from PATH or its standard macOS installer location."""
    return clients.opencode_executable()


def _verify_opencode_connection() -> bool:
    """Use OpenCode itself to prove the newly configured klyk server connects."""
    executable = _opencode_executable()
    if executable is None:
        print("  ✗ OpenCode CLI is not installed, so the MCP connection cannot be verified.")
        print("    Install OpenCode, then re-run `klyk install opencode`.")
        return False
    try:
        result = subprocess.run(
            [executable, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "KLYK_UPDATE_CHECK": "0"},
        )
    except subprocess.TimeoutExpired:
        print("  ✗ `opencode mcp list` did not finish within 20 seconds.")
        print("    Stop any wedged OpenCode process, then re-run `klyk install opencode`.")
        return False
    except OSError as exc:
        print(f"  ✗ Could not run {executable}: {exc}")
        return False

    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + "\n" + result.stderr)
    connected = any(re.fullmatch(
        r"[\s│┃├└┌┐┘┤─━+✓✔●○]*klyk\s*:?\s+(?i:connected)(?:\s.*)?", line,
    ) for line in output.splitlines())
    if result.returncode == 0 and connected:
        print("  ✓ OpenCode started klyk and reports it connected")
        return True
    print("  ✗ OpenCode could not start klyk (`opencode mcp list` is not connected).")
    print("    Grant Accessibility and Screen Recording to the app that launches OpenCode")
    print("    (Terminal, Ghostty, iTerm, or OpenCode Desktop). If it is already enabled,")
    print("    add the OpenCode executable too:")
    print(f"      {executable}")
    print("    Fully restart the launcher and OpenCode, then")
    print("    re-run `klyk install opencode`. No fallback was substituted.")
    return False


def _write_context_guide(client) -> None:
    """--ambient: write the short klyk-call note into the client's context file
    (e.g. GEMINI.md) so the agent can fall back when native MCP is unavailable."""
    try:
        if clients.context_block_present(client):
            print(f"  ✓ klyk note already in {client.context_file}")
            return
        action = clients.write_context_block(client)
        verb = {"added": "Added", "updated": "Updated", "unchanged": "Already had"}[action]
        print(f"  ✓ {verb} the klyk note in {client.context_file}")
    except Exception as e:
        print(f"  ✗ Could not write {client.context_file}: {e} — paste this yourself:")
        print(_indent(clients.context_block()))


def _wire_other_clients(configured) -> None:
    """Detect and LIST the other AI clients on this Mac (and the no-MCP front
    doors) so 'what's next' is never a guess. No prompt — `klyk install --all`
    wires every detected client in one shot."""
    pending, done = [], []
    for c in clients.CLIENTS.values():
        if c.key == configured.key or not clients.is_present(c):
            continue
        try:
            (done if clients.current_entry(c) is not None else pending).append(c)
        except Exception:
            pending.append(c)

    if pending:
        width = max(len(c.key) for c in pending) + 2
        print("Other AI clients detected (verify permissions from each launcher):")
        for c in pending:
            print(f"    klyk install {c.key:<{width}} {c.label}")
        print("    …or `klyk install --all` to wire every detected client at once.")
        print()
    if done:
        print(f"  Already configured: {', '.join(c.label for c in done)}.")
    print("  No-MCP front door: `klyk-call` (any shell agent). See the README.")


def _persistent_install_required() -> bool:
    """Never save an MCP command pointing into a disposable uvx environment."""
    from . import updates
    if updates.install_method() != "uvx":
        return True
    print("This is a disposable uvx environment. Run `uv tool install klyk`, then use the installed `klyk install` command.", file=sys.stderr)
    return False


def _open_settings(label: str, deep_link: str) -> None:
    """Open the exact System Settings pane — no prompt, no blocking. Granting is
    the one OS-mandated step klyk can't do for you, so we take you straight there."""
    subprocess.run(["/usr/bin/open", deep_link], check=False)
    print(f"    → Opened Privacy & Security → {label}. Add your terminal/AI app, "
          "toggle it ON, then re-run `klyk doctor`.")


def _verify_permission(label: str, check_fn, deep_link: str, wait: bool = False) -> bool:
    """Run a doctor check; if missing, open the right Settings pane (non-blocking).
    With wait=True, poll until the grant lands (up to 2 min, Ctrl-C to skip)."""
    res = check_fn()
    if res.status == "ok":
        print(f"  ✓ {label}: {res.detail}")
        return True
    print(f"  ✗ {label}: {res.detail}")
    _open_settings(label, deep_link)
    if not wait:
        return False
    print(f"  … waiting — toggle {label} ON (Ctrl-C to skip)", flush=True)
    deadline = time.time() + 120
    try:
        while time.time() < deadline:
            time.sleep(2)
            if check_fn().status == "ok":
                print(f"  ✓ {label}: now granted")
                return True
    except KeyboardInterrupt:
        print()
    print(f"  ✗ {label}: still not granted — re-run `klyk doctor` when ready.")
    return False


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def _unwire_client(client) -> bool:
    """Remove one client's klyk entry/context note and report full success."""
    ok = True
    try:
        removed = clients.remove_entry(client)
        print(f"  ✓ removed klyk from {client.label}" if removed
              else f"  · {client.label}: not configured")
    except clients.ManualEditRequired as e:
        print(f"  ⚠ {client.label}: {e}")
        ok = False
    except Exception as e:
        print(f"  ✗ {client.label}: could not remove klyk: {e}")
        ok = False
    if client.context_file is not None:
        try:
            if clients.remove_context_block(client):
                print(f"  ✓ removed the klyk note from {client.context_file}")
        except Exception as e:
            print(f"  ⚠ could not edit {client.context_file}: {e}")
            ok = False
    return ok


def _uninstall(rest: list[str]) -> None:
    """A named client → unwire just that one. No client (or --all) → FULL removal:
    every configured client, ~/.klyk state, and the binaries.
    (The pip package itself is removed with `pip uninstall klyk`.)"""
    specific = next((a for a in rest if not a.startswith("-")), None)
    if specific is not None and "--all" not in rest:
        if not _unwire_client(_resolve_client(rest)):
            sys.exit(1)
        return

    print("Uninstalling klyk completely…\n")
    print("Clients:")
    any_client = False
    client_failures = False
    for c in clients.CLIENTS.values():
        try:
            if (clients.current_entry(c) is not None or clients.context_block_present(c)
                    or clients.legacy_opencode_entry(c) is not None):
                if not _unwire_client(c):
                    client_failures = True
                any_client = True
        except Exception as e:
            print(f"  ✗ {c.label}: could not inspect its config: {e}")
            client_failures = True
            any_client = True
    if not any_client:
        print("  · none were configured")

    if client_failures:
        print("\nSome client configuration could not be removed. The state and runtime were retained.")
        print("Fix the errors above, then re-run.")
        sys.exit(1)

    print("\nFiles:")
    home = Path.home()
    file_failures = False
    for b in ("klyk", "klyk-call"):
        f = home / ".local" / "bin" / b
        if f.is_symlink() or f.exists():
            try:
                f.unlink()
                print(f"  ✓ removed {f}")
            except OSError:
                file_failures = True
                print(f"  ✗ could not remove {f}")
    files = (home / ".klyk", home / "klyk.log", *(home / f"klyk.log.{index}" for index in range(1, 6)))
    for p in files:
        try:
            if p.is_symlink():
                p.unlink()  # Never traverse a linked state or log destination.
            elif p == home / ".klyk" and p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                if p.is_dir():
                    raise OSError("unexpected directory at a log path")
                p.unlink()
            else:
                continue
            if p.exists() or p.is_symlink():
                raise OSError("destination remains")
            print(f"  ✓ removed {p}")
        except OSError:
            file_failures = True
            print(f"  ✗ could not remove {p}")
    if file_failures:
        print("\nSome files remain. Resolve their permissions, then re-run uninstall.")
        sys.exit(1)
    print("\nklyk's config, state, and binaries are gone.")
    print("Restart any AI clients with a cached klyk configuration to stop their old sessions.")
    print("The Python package remains installed. Remove it with the package manager used to install klyk.")


if __name__ == "__main__":
    main()
