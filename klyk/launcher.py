"""
App launching and process management for native and Electron apps.
"""

import ctypes
import os
import signal
import subprocess
import sys
import time

# Chromium-family browsers. When Klyk launches one of these, it appends
# --force-renderer-accessibility so the AX tree exposes web content
# (buttons, links, fields inside pages) — not just the chrome around tabs.
# Only takes effect when Klyk launches the browser cold; if the user
# already has it running, attachment preserves that configuration. Inspection
# reports the AX content actually available instead of assuming it is empty.
CHROMIUM_BROWSERS = {
    "Google Chrome", "Google Chrome Canary", "Google Chrome Beta", "Google Chrome Dev",
    "Chromium", "Brave Browser", "Microsoft Edge", "Arc", "Vivaldi", "Opera",
}

# All known browser app names (for AX filtering decisions).
BROWSERS = CHROMIUM_BROWSERS | {"Safari", "Safari Technology Preview", "Firefox"}


def _check_request_access() -> None:
    """Stop later app/log/signal stages of a revoked request; CLI metadata stays independent."""
    from . import connection_gate
    request = connection_gate.current_scope()
    if request is not None:
        connection_gate.checkpoint(request)


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
    """Reject empty, excessive or control-containing application identifiers."""
    if not value or len(value) > 256:
        raise ValueError(f"{field} must be 1–256 chars (got {len(value)}).")
    if any(ch in value for ch in ('\n', '\r', '\0')):
        raise ValueError(f"{field} contains control characters: {value!r}")


def _quick_pid_for_app(bundle_id: str | None, app_name: str | None) -> int | None:
    """Query native running apps; an uncertain lookup must never relaunch an existing app."""
    if not bundle_id and not app_name:
        return None
    _validate_app_identifier(bundle_id or app_name, "bundle_id" if bundle_id else "app_name")
    _check_request_access()
    try:
        from AppKit import NSRunningApplication, NSWorkspace
        if bundle_id:
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        else:
            name = app_name.casefold().removesuffix(".app")
            apps = [app for app in NSWorkspace.sharedWorkspace().runningApplications()
                    if str(app.localizedName() or "").casefold() == name
                    or (app.bundleURL() is not None and
                        str(app.bundleURL().lastPathComponent()).casefold().removesuffix(".app") == name)]
        pids = {int(app.processIdentifier()) for app in apps
                if not app.isTerminated() and int(app.processIdentifier()) > 0}
    except Exception as error:
        raise RuntimeError('Could not check running applications; app identity is unknown. Inspect the target before retrying.') from error
    if len(pids) > 1:
        raise RuntimeError('More than one running application matches; use a unique app name or bundle identifier.')
    return next(iter(pids), None)


def launch_native_app(
    app_name: str | None = None,
    bundle_id: str | None = None,
    *,
    check_stop=None,
) -> tuple[int, bool]:
    """
    Launch a native macOS app. Returns (pid, was_already_running).

    Attach to a running app without opening or activating it. Cold Chromium
    launches request renderer accessibility; already-running browsers retain
    their current configuration and inspection reports the observed AX state.
    """
    prior_pid = _quick_pid_for_app(bundle_id, app_name)
    was_already_running = prior_pid is not None
    if prior_pid is not None:
        # Attaching must not open another default-profile instance (notably
        # Electron apps launched with an isolated user-data directory).
        return prior_pid, True

    if check_stop is not None:
        check_stop()
    if bundle_id:
        subprocess.Popen(["/usr/bin/open", "-b", bundle_id])
    elif app_name:
        cmd = ["/usr/bin/open", "-a", app_name]
        if app_name in CHROMIUM_BROWSERS and not was_already_running:
            cmd += ["--args", "--force-renderer-accessibility"]
        subprocess.Popen(cmd)
    else:
        raise ValueError("Either app_name or bundle_id must be provided")

    # Return as soon as native process registration is visible; session creation
    # independently waits for a window instead of imposing a blind launch delay.
    pid = _find_pid_for_app(bundle_id=bundle_id, app_name=app_name)
    return pid, False


def start_native_log_stream(pid: int) -> subprocess.Popen:
    """Start a log stream watcher for a native app's unified log output. Returns the Popen."""
    _check_request_access()
    return subprocess.Popen(
        ["/usr/bin/log", "stream", "--predicate", f"processID == {pid}",
         "--level", "default", "--style", "compact"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def launch_electron_app(app_path: str, *, check_stop=None) -> tuple[int, subprocess.Popen]:
    """
    Launch an Electron .app bundle.
    Returns (pid, proc) where proc.stderr is a pipe for log capture.
    """
    if not os.path.exists(app_path):
        raise FileNotFoundError(f"App not found: {app_path}")
    executable = _find_app_executable(app_path)
    if check_stop is not None:
        check_stop()
    proc = subprocess.Popen([executable], stderr=subprocess.PIPE)
    time.sleep(1.5)
    return proc.pid, proc


def _find_pid_for_app(
    bundle_id: str | None,
    app_name: str | None,
    timeout: float = 10.0,
) -> int:
    """Wait only for native process registration, preserving lookup errors and ambiguity."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        _check_request_access()
        pid = _quick_pid_for_app(bundle_id, app_name)
        if pid is not None:
            return pid
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
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


class _ProcessInfo(ctypes.Structure):
    """Public macOS proc_bsdinfo layout, including microsecond process start identity."""

    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _process_argv(pid: int) -> list[str] | None:
    """Read exact kernel argv boundaries without returning any process environment values."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.restype = ctypes.c_int
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid.
    length = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(length), None, 0) != 0 or not 5 <= length.value <= 1024 * 1024:
        return None
    data = ctypes.create_string_buffer(length.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(length), None, 0) != 0:
        return None
    raw = data.raw[:length.value]
    argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    path_end = raw.find(b"\0", 4)
    if not 1 <= argc <= 4096 or path_end < 0:
        return None
    offset = path_end + 1
    # The executable path is followed by padding before the argv[0] string.
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    arguments = []
    for _ in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            return None
        arguments.append(raw[offset:end].decode("utf-8", "strict"))
        offset = end + 1
    return arguments


def process_identity(pid: int, *, include_command: bool = False) -> dict | None:
    """Read a process start token without activating apps; optional command data stays internal."""
    if sys.platform != "darwin" or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        info = _ProcessInfo()
        if libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        if info.pid != pid or not info.start_sec:
            return None
        identity = {"pid": pid, "uid": int(info.uid), "started": (int(info.start_sec), int(info.start_usec))}
        if include_command:
            libproc.proc_pidpath.restype = ctypes.c_int
            libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            path = ctypes.create_string_buffer(4096)
            if libproc.proc_pidpath(pid, path, len(path)) <= 0:
                return None
            arguments = _process_argv(pid)
            if arguments is None:
                return None
            identity["executable"] = path.value.decode("utf-8", "strict")
            identity["argv"] = arguments
            # The command must belong to the same process observed before the read.
            current = process_identity(pid)
            if current != {key: identity[key] for key in ("pid", "uid", "started")}:
                return None
        return identity
    except (OSError, ValueError, UnicodeError, AttributeError):
        return None


def _same_process(pid: int, expected_identity: dict) -> bool:
    """Compare the stable token rather than trusting a PID that the OS may have reused."""
    current = process_identity(pid)
    return bool(current and all(current.get(key) == expected_identity.get(key) for key in ("pid", "uid", "started")))


def _process_status(pid: int, expected_identity: dict) -> str:
    """Separate a vanished original process from a failed identity lookup."""
    current = process_identity(pid)
    if current is None:
        return "unknown" if pid_alive(pid) else "gone"
    if all(current.get(key) == expected_identity.get(key) for key in ("pid", "uid", "started")):
        return "same"
    return "gone"


def terminate_pid(pid: int, term_timeout: float = 3.0, *, expected_identity: dict | None = None) -> bool:
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
    expected_identity = expected_identity or process_identity(pid)
    if expected_identity is None:
        return not pid_alive(pid)
    status = _process_status(pid, expected_identity)
    if status != "same":
        return status == "gone"
    _check_request_access()
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
        _check_request_access()
        status = _process_status(pid, expected_identity)
        if status != "same":
            return status == "gone"  # Never signal a replacement or an unknown process.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False  # Permission failure does not prove the process exited.
        time.sleep(0.1)

    # SIGTERM ignored — escalate.
    status = _process_status(pid, expected_identity)
    if status != "same":
        return status == "gone"
    _check_request_access()
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    # Final verification.
    time.sleep(0.2)
    status = _process_status(pid, expected_identity)
    if status != "same":
        return status == "gone"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False
