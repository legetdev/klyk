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
            if (existing is not None or default_claude) and existing != c.entry:
                clients.write_entry(c)
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
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"✗ `{cmd[0]}` is not on PATH, but this klyk lives in a {method}-"
              f"managed environment. Install {cmd[0]} (or reinstall klyk with "
              "`pipx install klyk`), then re-run `klyk update`.", file=sys.stderr)
        sys.exit(1)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout)
        print(f"✗ Update failed (see the {cmd[0]} output above).", file=sys.stderr)
        sys.exit(1)

    after = subprocess.run(
        [sys.executable, "-c", "import klyk; print(klyk.__version__)"],
        capture_output=True, text=True,
    ).stdout.strip() or before
    # Record the fresh state so the doctor and menu-bar stop showing a
    # now-stale "update available" the moment the upgrade lands.
    updates.check(force=True)

    if after == before:
        print(f"✓ Already on the latest version ({before}).")
        return
    print(f"✓ Updated klyk {before} → {after}.")
    _restart_after_update()


def _restart_after_update() -> None:
    """Stop the currently running klyk server (if any) so the MCP client
    respawns it with the new code on the next tool call — no manual client
    restart needed. Fully transparent about what happened."""
    from .ownership import current_owner
    from .launcher import terminate_pid
    owner = current_owner()
    if not owner:
        print("  No klyk server was running — the new version loads on the next session.")
        return
    try:
        os.kill(owner, 0)
    except OSError:
        print("  No klyk server was running — the new version loads on the next session.")
        return
    if terminate_pid(owner):
        print(f"  ✓ Restarted the running klyk server (pid {owner}) — connected "
              "agents load the new version on their next call.")
    else:
        print(f"  ⚠ Could not stop the running klyk server (pid {owner}). "
              "Run `klyk restart` (or restart your AI client) to load the new version.")


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
    try:
        os.kill(owner, 0)
    except OSError:
        print(f"The recorded klyk (pid {owner}) is no longer running — nothing to stop.")
        return
    print(f"Stopping the active klyk (pid {owner})…")
    if terminate_pid(owner):
        print("✓ Stopped. The next session — or a take_control from another — becomes the driver.")
    else:
        print(
            f"✗ Could not stop pid {owner}. Stop it manually: `kill -9 {owner}`.",
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
    """Write klyk into the client's config. Prompts before replacing a
    differing entry; prints a paste snippet when a file can't be auto-edited.
    Returns True if the client is left configured."""
    try:
        clients.current_entry(client)
    except Exception as e:
        path = clients.config_path(client)
        print(f"  ✗ Could not read {path}: {e}")
        print("    Fix the file, then re-run. Or add this manually:")
        print(_indent(clients.snippet(client)))
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
        print("    Add this manually instead:")
        print(_indent(clients.snippet(client)))
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
    # It's only a fallback for clients whose native MCP is flaky; most never need it.
    if ambient:
        for c in targets:
            if c.context_file is not None:
                _write_context_guide(c)

    # Finally, offer to wire every other AI client on the Mac. Permissions are
    # already granted (they attach to the user, not the client), so the rest is
    # pure config writes — one prompt instead of a command per client. Skipped in
    # --all mode, which already configured everything detected.
    if not all_mode:
        print()
        _wire_other_clients(targets[0])


def _opencode_executable() -> str | None:
    """Resolve OpenCode from PATH or its standard macOS installer location."""
    found = shutil.which("opencode")
    if found:
        return found
    fallback = Path.home() / ".opencode" / "bin" / "opencode"
    return str(fallback) if fallback.is_file() else None


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
    connected = any(
        re.search(r"\bklyk\b", line, re.IGNORECASE)
        and re.search(r"\bconnected\b", line, re.IGNORECASE)
        and not re.search(
            r"\bnot\s+connected\b|\bfailed\b|\berror\b", line, re.IGNORECASE,
        )
        for line in output.splitlines()
    )
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
    (e.g. GEMINI.md) so the agent can fall back to the shell if its MCP is flaky."""
    if clients.context_block_present(client):
        print(f"  ✓ klyk note already in {client.context_file}")
        return
    try:
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
        print("Other AI clients detected (permissions already carry over):")
        for c in pending:
            print(f"    klyk install {c.key:<{width}} {c.label}")
        print("    …or `klyk install --all` to wire every detected client at once.")
        print()
    if done:
        print(f"  Already configured: {', '.join(c.label for c in done)}.")
    print("  No-MCP front door: `klyk-call` (any shell agent). See the README.")


def _open_settings(label: str, deep_link: str) -> None:
    """Open the exact System Settings pane — no prompt, no blocking. Granting is
    the one OS-mandated step klyk can't do for you, so we take you straight there."""
    subprocess.run(["open", deep_link], check=False)
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
            if clients.current_entry(c) is not None or clients.context_block_present(c):
                if not _unwire_client(c):
                    client_failures = True
                any_client = True
        except Exception as e:
            print(f"  ✗ {c.label}: could not inspect its config: {e}")
            client_failures = True
            any_client = True
    if not any_client:
        print("  · none were configured")

    print("\nFiles:")
    home = Path.home()
    for b in ("klyk", "klyk-call"):
        f = home / ".local" / "bin" / b
        if f.is_symlink() or f.exists():
            f.unlink(missing_ok=True)
            print(f"  ✓ removed {f}")
    for p in (home / ".klyk", home / "klyk.log"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  ✓ removed {p}/")
        elif p.exists():
            p.unlink(missing_ok=True)
            print(f"  ✓ removed {p}")

    if client_failures:
        print("\nSome client configuration could not be removed.")
        print("Fix the errors above, then re-run.")
        sys.exit(1)
    print("\nklyk's config, state, and binaries are gone.")
    print("Finally, remove the package itself:  pip uninstall klyk")


if __name__ == "__main__":
    main()
