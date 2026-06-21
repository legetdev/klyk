"""
App launching and process management for native and Electron apps.
"""

import ctypes
import os
import signal
import subprocess
import time

# Chromium-family browsers. When Klyk launches one of these, it appends
# --force-renderer-accessibility so the AX tree exposes web content
# (buttons, links, fields inside pages) — not just the chrome around tabs.
# Only takes effect when Klyk launches the browser cold; if the user
# already has it running, the flag is a no-op until they restart. The
# `was_already_running` return from launch_native_app surfaces this so
# callers can warn the agent that AX is likely empty for web content.
CHROMIUM_BROWSERS = {
    "Google Chrome", "Google Chrome Canary", "Google Chrome Beta", "Google Chrome Dev",
    "Chromium", "Brave Browser", "Microsoft Edge", "Arc", "Vivaldi", "Opera",
}

# All known browser app names (for AX filtering decisions).
BROWSERS = CHROMIUM_BROWSERS | {"Safari", "Safari Technology Preview", "Firefox"}


def is_browser(app_name: str | None) -> bool:
    """True if app_name matches a known browser."""
    return bool(app_name and app_name in BROWSERS)


# Frameworks that mark an app whose UI is a Chromium renderer — so it shares
# the same trusted-event filter as a Chromium browser and mishandles synthetic
# SkyLight clicks/keys. Electron apps (VS Code, Slack, Discord, …) ship the
# Electron Framework; CEF apps (e.g. Spotify) ship the Chromium Embedded
# Framework. Native apps — including Tauri/WebKit ones — ship neither, so this
# never produces a false positive.
_CHROMIUM_RENDERER_FRAMEWORKS = (
    "Contents/Frameworks/Electron Framework.framework",
    "Contents/Frameworks/Chromium Embedded Framework.framework",
)


def _bundle_path_for_pid(pid: int) -> str | None:
    """Resolve the enclosing .app bundle path for a running pid via libproc.
    Returns None if the path can't be read or the process isn't inside a .app."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidpath.restype = ctypes.c_int
        libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        buf = ctypes.create_string_buffer(4096)
        n = libproc.proc_pidpath(int(pid), buf, 4096)
        if n <= 0:
            return None
        exec_path = buf.value.decode("utf-8", "replace")  # …/Slack.app/Contents/MacOS/Slack
        idx = exec_path.find(".app/")
        if idx == -1:
            return None
        return exec_path[: idx + len(".app")]
    except Exception:
        return None


def is_chromium_renderer_app(pid: int) -> bool:
    """True if the running pid is an Electron or CEF app — a desktop app whose
    UI is a Chromium renderer. Detection is by the Chromium framework shipped
    in the bundle (which native apps never carry). Conservative: any failure →
    False, i.e. treated as a native app."""
    bundle = _bundle_path_for_pid(pid)
    if not bundle:
        return False
    try:
        return any(
            os.path.isdir(os.path.join(bundle, fw))
            for fw in _CHROMIUM_RENDERER_FRAMEWORKS
        )
    except Exception:
        return False


def probe_web_ax_alive(pid: int) -> bool:
    """
    Empirically check whether a (Chromium) browser's web AX tree is
    exposing content. Chrome lazily builds its accessibility tree —
    --force-renderer-accessibility forces it at launch, but for an
    already-running browser the tree often becomes populated anyway
    once any AT-style query hits it. So instead of trusting the launch
    flag, we look at what's actually there.

    Returns True when the focused window's subtree contains at least a
    few AXStaticText nodes with non-empty values (rendered web text,
    tiles, cells, labels). Returns False when only the Chrome shell
    surfaces — that's the truly-disabled case worth warning about.
    Cost: one AXFocusedWindow read + shallow walk; bounded by the
    messaging timeout so it never blocks longer than ~200 ms.
    """
    import ctypes
    from . import computer
    try:
        app_ptr = computer._appserv.AXUIElementCreateApplication(pid)
        if not app_ptr:
            return False
        try:
            try:
                computer._appserv.AXUIElementSetMessagingTimeout(
                    ctypes.c_void_p(app_ptr), 0.1,
                )
            except Exception:
                pass
            focused_ptr = computer._ax_read_attr_ptr(app_ptr, b"AXFocusedWindow")
            if not focused_ptr:
                return False
            try:
                results: list = []
                computer._ax_collect(focused_ptr, results, 0, 12, 30, 0.0)
                # Web content surfaces as many AXStaticText nodes with
                # values (every tile, every label). Chrome shell has very
                # few. Threshold = 5 to avoid false-negatives on minimal
                # pages, false-positives on populated toolbars.
                rendered_text = sum(
                    1 for e in results
                    if e.get("role") == "AXStaticText"
                    and (e.get("value") or "").strip()
                )
                return rendered_text >= 5
            finally:
                computer._cf.CFRelease(ctypes.c_void_p(focused_ptr))
        finally:
            computer._cf.CFRelease(ctypes.c_void_p(app_ptr))
    except Exception:
        return False


def _validate_app_identifier(value: str, field: str) -> None:
    """
    Reject control characters and AppleScript-quote-breaking characters in
    values that will be interpolated into an osascript string. Defense in
    depth alongside _as_quote(): even if escaping ever regresses, control
    characters (newline / CR / NUL) and embedded double-quote / backslash
    chains can't slip into the AppleScript layer.
    """
    if not value or len(value) > 256:
        raise ValueError(f"{field} must be 1–256 chars (got {len(value)}).")
    if any(ch in value for ch in ('\n', '\r', '\0')):
        raise ValueError(f"{field} contains control characters: {value!r}")


def _as_quote(value: str) -> str:
    """
    Escape a string for safe interpolation inside double-quoted AppleScript.
    Inside AS double-quoted literals the only meta-characters are `\\` and
    `\"`. Escape both. Together with `_validate_app_identifier`, this closes
    the AppleScript-injection surface in the PID-lookup path.
    """
    return value.replace('\\', '\\\\').replace('"', '\\"')


def _quick_pid_for_app(bundle_id: str | None, app_name: str | None) -> int | None:
    """Single-attempt PID lookup, ~50 ms. Returns None if app isn't running."""
    try:
        if bundle_id:
            _validate_app_identifier(bundle_id, "bundle_id")
            script = (
                f'tell application "System Events" to '
                f'get unix id of first process whose bundle identifier is "{_as_quote(bundle_id)}"'
            )
        elif app_name:
            _validate_app_identifier(app_name, "app_name")
            script = (
                f'tell application "System Events" to '
                f'get unix id of first process whose name is "{_as_quote(app_name)}"'
            )
        else:
            return None
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    return None


def launch_native_app(
    app_name: str | None = None,
    bundle_id: str | None = None,
) -> tuple[int, bool]:
    """
    Launch a native macOS app. Returns (pid, was_already_running).

    was_already_running=True means the app was running before Klyk called
    `open -a`, so the --force-renderer-accessibility flag for Chromium browsers
    was silently ignored. In practice Chromium's lazy a11y still enables on
    first external query (klyk auto-retries the inspect AX walk if it comes
    back empty on a browser), so the agent rarely sees an empty tree.
    """
    prior_pid = _quick_pid_for_app(bundle_id, app_name)
    was_already_running = prior_pid is not None

    if bundle_id:
        subprocess.Popen(["open", "-b", bundle_id])
    elif app_name:
        cmd = ["open", "-a", app_name]
        if app_name in CHROMIUM_BROWSERS and not was_already_running:
            cmd += ["--args", "--force-renderer-accessibility"]
        subprocess.Popen(cmd)
    else:
        raise ValueError("Either app_name or bundle_id must be provided")

    # If the app was already running, we don't need the full launch settle —
    # the PID is known. For a cold launch, give the process a moment to
    # register before osascript queries System Events.
    if was_already_running and prior_pid is not None:
        return prior_pid, True

    time.sleep(1.0)
    pid = _find_pid_for_app(bundle_id=bundle_id, app_name=app_name)
    return pid, False


def start_native_log_stream(pid: int) -> subprocess.Popen:
    """Start a log stream watcher for a native app's unified log output. Returns the Popen."""
    return subprocess.Popen(
        ["log", "stream", "--predicate", f"processID == {pid}",
         "--level", "default", "--style", "compact"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def launch_electron_app(app_path: str) -> tuple[int, subprocess.Popen]:
    """
    Launch an Electron .app bundle.
    Returns (pid, proc) where proc.stderr is a pipe for log capture.
    """
    if not os.path.exists(app_path):
        raise FileNotFoundError(f"App not found: {app_path}")
    executable = _find_app_executable(app_path)
    proc = subprocess.Popen([executable], stderr=subprocess.PIPE)
    time.sleep(1.5)
    return proc.pid, proc


def _find_pid_for_app(
    bundle_id: str | None,
    app_name: str | None,
    timeout: float = 10.0,
) -> int:
    """Find PID of a running app by bundle ID or name via osascript."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            if bundle_id:
                _validate_app_identifier(bundle_id, "bundle_id")
                script = (
                    f'tell application "System Events" to '
                    f'get unix id of first process whose bundle identifier is "{_as_quote(bundle_id)}"'
                )
            else:
                _validate_app_identifier(app_name or "", "app_name")
                script = (
                    f'tell application "System Events" to '
                    f'get unix id of first process whose name is "{_as_quote(app_name or "")}"'
                )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError):
            pass
        _time.sleep(0.5)
    raise RuntimeError(
        f"Could not find PID for app (bundle_id={bundle_id!r}, name={app_name!r}). "
        "Likely causes: app isn't installed in /Applications, the bundle id is "
        "misspelled, or the app failed to launch silently. Run `klyk doctor` "
        "if you suspect a permissions issue."
    )


def _find_app_executable(app_path: str) -> str:
    """Find the main executable inside a .app bundle."""
    macos_dir = os.path.join(app_path, "Contents", "MacOS")
    if os.path.isdir(macos_dir):
        executables = [
            os.path.join(macos_dir, f)
            for f in os.listdir(macos_dir)
            if os.access(os.path.join(macos_dir, f), os.X_OK)
        ]
        if executables:
            return executables[0]
    raise RuntimeError(f"No executable found in {app_path}")


def pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists (any owner)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM = exists but owned by another user — still alive from our perspective.
        return True
    return True


def terminate_pid(pid: int, term_timeout: float = 3.0) -> bool:
    """
    Best-effort terminate. Sends SIGTERM, polls up to `term_timeout` seconds,
    escalates to SIGKILL if still alive, then verifies. Returns True iff the
    process is gone after the call. Without escalation, an app holding a modal
    save dialog would ignore SIGTERM and `close_app` would return success while
    the app was still alive holding its window — the next session lookup would
    find the live PID and reuse it against an "un-closed" app.
    """
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone
    except OSError:
        # Couldn't signal — fall through to the escalation, which may also fail
        # but won't mask the original problem.
        pass

    deadline = time.monotonic() + term_timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.1)

    # SIGTERM ignored — escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    # Final verification.
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return True
    return False
