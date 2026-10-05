"""External safety adapters for a real MCP entry-point smoke, never desktop acceptance."""

import atexit
import builtins
import ctypes
import json
import os
from pathlib import Path
import queue
import runpy
import subprocess
import sys
import threading
import time


def inhibit_stop_tap(computer, audit):
    """Replace the native listener boundary without creating an unstarted Thread."""
    computer._start_emergency_stop_tap = lambda: None
    audit['substitutions'].append('direct global emergency-stop event tap startup')


def main():
    """Inhibit every active desktop boundary before importing the production entry point."""
    options = sys.argv[3:]
    inert_held_input = '--inert-held-input' in options
    start_off = '--access-off' in options
    pause_replies = '--pause-replies' in options
    if (sys.platform != 'darwin' or len(sys.argv) < 3 or len(options) != len(set(options))
            or set(options) - {'--inert-held-input', '--access-off', '--pause-replies'}
            or (inert_held_input and start_off)):
        raise RuntimeError('This guarded child requires macOS, a source root, and a private work directory')
    source = Path(sys.argv[1]).resolve()
    work = Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    work.chmod(0o700)
    os.umask(0o077)
    sys.path.insert(0, str(source))
    os.environ['KLYK_OWNER_FILE'] = str(work / 'owner')
    os.environ['KLYK_UPDATE_CHECK'] = '0'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['KLYK_CLIENT'] = 'codex'
    audit = {'scope': 'real-entry protocol integration with inhibited desktop boundaries',
             'desktop_acceptance': False, 'blocked_attempts': [], 'permission_queries': [],
             'substitutions': [], 'entry_completed': False, 'started_access_off': start_off}
    native_names = ('computer', 'capture', 'skylight', 'ocr', 'matcher')
    audit_lock = threading.RLock()

    def save_audit():
        """Keep a private, content-free account of inhibited and attempted boundaries."""
        with audit_lock:
            audit['loaded_native_modules'] = [name for name in native_names if f'klyk.{name}' in sys.modules]
            temporary = work / 'boundaries.tmp'
            temporary.write_text(json.dumps(audit, indent=2))
            os.replace(temporary, work / 'boundaries.json')

    def forbidden(name):
        """Fail closed before any capture, input, activation, clipboard, or app-launch call."""
        def reject(*args, **kwargs):
            audit['blocked_attempts'].append(name)
            save_audit()
            raise RuntimeError(f'Silent protocol smoke prohibits {name}')
        return reject

    original_cdll = ctypes.CDLL
    danger_prefixes = ('CGEvent', 'CGWarpMouse',
                       'CGAssociateMouse', 'CGDisplayMoveCursor', 'CGDisplayHideCursor',
                       'CGDisplayShowCursor', 'CGDisplayCreateImage', 'CGWindowListCreateImage',
                       'CGRequest',
                       'AXUIElement', 'CGWindowListCopyWindowInfo', 'SetFrontProcess',
                       'SLEventPost', 'SLPS', '_SLPS')
    queried_permissions = {'AXIsProcessTrustedWithOptions', 'CGPreflightScreenCaptureAccess'}

    class GuardedLibrary:
        """Keep real native metadata bindings while denying active desktop C functions."""

        def __init__(self, library):
            self.library = library
            self.functions = {}

        def __getattr__(self, name):
            if name in self.functions:
                return self.functions[name]
            if name.startswith(danger_prefixes):
                function = forbidden(name)
            elif name in queried_permissions:
                native = getattr(self.library, name)

                def function(*args, **kwargs):
                    """Record the actual permission result without requesting a grant."""
                    if name == 'AXIsProcessTrustedWithOptions' and args != (None,):
                        return forbidden('prompt-capable accessibility options')()
                    native.restype = getattr(function, 'restype', native.restype)
                    native.argtypes = getattr(function, 'argtypes', native.argtypes)
                    result = native(*args, **kwargs)
                    audit['permission_queries'].append({'api': name, 'allowed': bool(result)})
                    save_audit()
                    return result
            else:
                return getattr(self.library, name)
            self.functions[name] = function
            return function

    # The guard is active even during imports, before the computer module starts its tap.
    ctypes.CDLL = lambda *args, **kwargs: GuardedLibrary(original_cdll(*args, **kwargs))
    original_import = builtins.__import__
    real_nsapp = []
    guarded_frameworks = set()
    guarded_runtime = set()
    guarded_ui_names = {'NSApplication', 'NSWindow', 'NSPanel', 'NSStatusBar', 'NSStatusItem',
                        'NSPasteboard', 'NSSound', 'NSWorkspace', 'NSEvent', 'NSCursor',
                        'NSAlert', 'NSOpenPanel', 'NSSavePanel', 'NSMenu', 'NSMenuItem', 'NSApp'}

    class GuardedUIClass:
        """Permit binding metadata imports but prohibit every active Cocoa entry point."""

        def __init__(self, name):
            self.name = name

        def __getattr__(self, name):
            return forbidden(f'AppKit.{self.name}.{name}')

        def __call__(self, *args, **kwargs):
            return forbidden(f'AppKit.{self.name}')()

    def guard_appkit(appkit):
        """Quartz requires AppKit metadata; verify it created no app and guard actual UI calls."""
        if real_nsapp:
            return
        proxy = sys.modules['AppKit._nsapp'].NSApp
        if proxy() is not None:
            raise RuntimeError('The binding import unexpectedly initialized NSApplication')
        real_nsapp.append(proxy)
        audit['nsapp_before_entry_is_nil'] = True
        for name in guarded_ui_names:
            setattr(appkit, name, GuardedUIClass(name))
        for name in ('NSBeep', 'NSRunAlertPanel', 'NSBeginAlertSheet', 'NSPerformService'):
            setattr(appkit, name, forbidden(f'AppKit.{name}'))
        import objc
        original_lookup = objc.lookUpClass
        objc.lookUpClass = lambda name: GuardedUIClass(name) if name in guarded_ui_names else original_lookup(name)
        audit['substitutions'].append('AppKit active UI classes/functions; metadata imports remain real')
        save_audit()

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Guard Cocoa as soon as its required metadata import has finished."""
        result = original_import(name, globals, locals, fromlist, level)
        appkit = sys.modules.get('AppKit')
        if appkit is not None and not getattr(appkit.__spec__, '_initializing', False):
            guard_appkit(appkit)
        for framework_name in ('Quartz', 'Quartz.CoreGraphics', 'ScreenCaptureKit'):
            framework = sys.modules.get(framework_name)
            if framework_name in guarded_frameworks or framework is None or getattr(framework.__spec__, '_initializing', False):
                continue
            guarded_frameworks.add(framework_name)
            for api_name in dir(framework):
                if api_name.startswith(danger_prefixes):
                    setattr(framework, api_name, forbidden(f'{framework_name}.{api_name}'))
            if framework_name == 'ScreenCaptureKit':
                for api_name in ('SCShareableContent', 'SCScreenshotManager', 'SCStream'):
                    setattr(framework, api_name, GuardedUIClass(f'{framework_name}.{api_name}'))
            audit['substitutions'].append(f'{framework_name} active capture/input/permission-request entry points')
            save_audit()
        for module_name in native_names:
            module = sys.modules.get(f'klyk.{module_name}')
            if (module is None or module_name in guarded_runtime
                    or getattr(module.__spec__, '_initializing', False)):
                continue
            guarded_runtime.add(module_name)
            if module_name == 'capture':
                for method in ('take_screenshot', 'take_display_screenshot'):
                    if hasattr(module, method):
                        setattr(module, method, forbidden(f'capture.{method}'))
            elif module_name == 'computer':
                for method in ('_snapshot_pasteboard', '_restore_pasteboard', 'activate_app'):
                    setattr(module, method, forbidden(f'computer.{method}'))
                inhibit_stop_tap(module, audit)
                if inert_held_input:
                    prepare_held_input(module)
            elif module_name == 'skylight':
                module.is_available = lambda: False
                audit['substitutions'].append('SkyLight availability, inhibiting its window/input self-test')
            save_audit()
        return result

    builtins.__import__ = guarded_import
    original_start = threading.Thread.start

    def guarded_start(thread):
        """Refuse any unexpected event-tap thread while retaining real SDK and policy workers."""
        if thread.name == 'klyk-stop':
            return forbidden('unexpected native emergency-stop thread startup')()
        return original_start(thread)

    threading.Thread.start = guarded_start
    original_kill = os.kill

    def guarded_kill(pid, signum):
        """Allow process-liveness queries while refusing any process termination."""
        if signum != 0:
            return forbidden('process signal')()
        return original_kill(pid, signum)

    os.kill = guarded_kill
    class GuardedPopen(subprocess.Popen):
        """Retain the SDK's generic Popen type annotations while refusing every launch."""

        def __init__(self, *args, **kwargs):
            forbidden('subprocess.Popen')()

    subprocess.Popen = GuardedPopen
    for method in ('run', 'check_output', 'check_call', 'call'):
        setattr(subprocess, method, forbidden(f'subprocess.{method}'))
    os.system = forbidden('os.system')
    os.startfile = forbidden('os.startfile')

    from klyk import connection_gate, connection_policy, controls, logs, ui_thread
    # Only this external adapter redirects policy; production accepts no test-path flag.
    connection_policy.policy_path = lambda: work / 'connections.json'
    connection_policy.initialize()
    if not start_off:
        connection_policy.set_enabled('codex', True)
    audit['loaded_package_path'] = sys.modules['klyk'].__file__
    audit['isolated_policy_path'] = str(connection_policy.policy_path())
    audit['startup_native_modules'] = [name for name in native_names if f'klyk.{name}' in sys.modules]
    original_logging = logs.configure_logging
    logs.configure_logging = lambda *args, **kwargs: original_logging(str(work / 'klyk.log'))

    def inert_controls_start():
        """Keep the independent controls launcher thread, replacing only its UI process start."""
        audit['controls_launch_inhibited'] = True
        save_audit()
        return True

    controls.start_background = inert_controls_start
    audit['substitutions'].append('independent UI-only controls process launcher')
    if pause_replies:
        original_write = connection_gate.GuardedWriter.write
        paused = [False]

        def pause_before_real_writer(writer, text):
            """Hold one completed tool frame before the unchanged final generation check."""
            if not paused[0] and (work / 'pause-next-tool-reply').is_file():
                try:
                    message = json.loads(text)
                    is_tool_reply = isinstance(message.get('result', {}).get('content'), list)
                except (ValueError, TypeError, AttributeError):
                    is_tool_reply = False
                if is_tool_reply:
                    paused[0] = True
                    audit['tool_reply_pauses'] = 1
                    save_audit()
                    (work / 'tool-reply-ready').write_text('ready')
                    deadline = time.monotonic() + 8
                    while not (work / 'release-tool-reply').is_file():
                        if time.monotonic() >= deadline:
                            raise TimeoutError('The external pre-write barrier was not released')
                        threading.Event().wait(0.005)
            return original_write(writer, text)

        connection_gate.GuardedWriter.write = pause_before_real_writer
        audit['substitutions'].append('private one-frame barrier before the unchanged final stdout generation check')

    def prepare_held_input(computer):
        """Seed only inert callbacks once lazy initialization imports the real input registry."""
        def marker(phase, kind=None):
            """Record only inert callbacks, so hard-exit cleanup remains independently observable."""
            item = {'phase': phase, 'kind': kind, 'stop_active': computer.emergency_stop_active(),
                    'held_count': len(computer._held_inputs),
                    'nsapp_is_nil': bool(real_nsapp) and real_nsapp[0]() is None}
            with (work / 'lifecycle.jsonl').open('a') as stream:
                stream.write(json.dumps(item) + '\n')

        def inert_release(kind):
            """Use the real held-input registry with fake delivery and test queued-down refusal."""
            marker('release', kind)
            try:
                computer._begin_input(('queued', kind), lambda: marker('unexpected_down', kind), lambda: None)
            except computer.EmergencyStop:
                marker('queued_down_blocked', kind)

        for kind in ('keyboard', 'mouse', 'media'):
            computer._begin_input(('inert', kind), lambda kind=kind: marker('down', kind),
                                  lambda kind=kind: inert_release(kind))
        computer._flush_clipboard_restore = lambda: marker('clipboard_cleanup')
        audit['substitutions'].append('inert held-input callbacks and clipboard cleanup marker')

    # AppKit metadata is real; all active classes are guarded before production can use them.
    import AppKit
    guard_appkit(AppKit)

    # Keep actual dispatch_sync generation checks, replacing only the native event-loop service.
    finished = threading.Event()

    def inert_install():
        """Permit the pure Python work queue without creating NSApplication or a menu item."""
        ui_thread.ui._available = True
        ui_thread.ui._ready_event.set()
        audit['substitutions'].append('AppKit main loop and status item')
        save_audit()
        return True

    def inert_loop():
        """Service real guarded callbacks on main thread, with no Cocoa event processing."""
        deadline = time.monotonic() + 45
        while not finished.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError('The real MCP worker did not stop within the test deadline')
            try:
                callback = ui_thread.ui._queue.get(timeout=0.02)
            except queue.Empty:
                continue
            callback()
            save_audit()

    ui_thread.ui.install_on_main_thread = inert_install
    ui_thread.ui.run_blocking = inert_loop
    ui_thread.ui.shutdown = finished.set
    ui_thread.ui.is_available = lambda: False
    audit['substitutions'].append('private policy, log and control-token paths; update check disabled')
    audit['native_call_guard_installed'] = True
    save_audit()
    atexit.register(save_audit)
    runpy.run_module('klyk.mcp_server', run_name='__main__')
    audit['nsapp_after_entry_is_nil'] = bool(real_nsapp) and real_nsapp[0]() is None
    audit['entry_completed'] = True
    save_audit()


if __name__ == '__main__':
    main()
