"""External safety adapters for a real MCP entry-point smoke, never desktop acceptance."""

import atexit
import builtins
import ctypes
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading


def main():
    """Inhibit every active desktop boundary before importing the production entry point."""
    inert_held_input = len(sys.argv) == 4 and sys.argv[3] == '--inert-held-input'
    if sys.platform != 'darwin' or (len(sys.argv) != 3 and not inert_held_input):
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
    audit = {'scope': 'real-entry protocol integration with inhibited desktop boundaries',
             'desktop_acceptance': False, 'blocked_attempts': [], 'permission_queries': [],
             'substitutions': [], 'entry_completed': False}

    def save_audit():
        """Keep a private, content-free account of inhibited and attempted boundaries."""
        (work / 'boundaries.json').write_text(json.dumps(audit, indent=2))

    def forbidden(name):
        """Fail closed before any capture, input, activation, clipboard, or app-launch call."""
        def reject(*args, **kwargs):
            audit['blocked_attempts'].append(name)
            save_audit()
            raise RuntimeError(f'Silent protocol smoke prohibits {name}')
        return reject

    original_cdll = ctypes.CDLL
    danger_prefixes = ('CGEventCreate', 'CGEventPost', 'CGEventTapCreate', 'CGWarpMouse',
                       'CGAssociateMouse', 'CGDisplayMoveCursor', 'CGDisplayHideCursor',
                       'CGDisplayShowCursor', 'CGDisplayCreateImage', 'CGWindowListCreateImage',
                       'CGRequest',
                       'AXUIElementSetAttribute', 'AXUIElementPerformAction', 'SetFrontProcess',
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
        return result

    builtins.__import__ = guarded_import
    original_start = threading.Thread.start

    def guarded_start(thread):
        """Replace only the import-time global event-tap thread, retaining real SDK workers."""
        if thread.name == 'klyk-stop':
            audit['substitutions'].append('global emergency-stop event tap')
            return
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

    from klyk import computer, capture, logs, skylight, ui_thread
    audit['loaded_package_path'] = sys.modules['klyk'].__file__
    original_logging = logs.configure_logging
    logs.configure_logging = lambda *args, **kwargs: original_logging(str(work / 'klyk.log'))
    for method in ('take_screenshot', 'take_display_screenshot'):
        if hasattr(capture, method):
            setattr(capture, method, forbidden(f'capture.{method}'))
    for method in ('_snapshot_pasteboard', '_restore_pasteboard', 'activate_app'):
        setattr(computer, method, forbidden(f'computer.{method}'))

    if inert_held_input:
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

    # Replacing the UI coordinator keeps the real production bootstrap/SDK lifecycle intact.
    finished = threading.Event()

    def inert_install():
        """Keep UI unavailable, so the real menu installer returns before creating a status item."""
        audit['substitutions'].append('AppKit main loop and status item')
        save_audit()
        return False

    def inert_loop():
        """Wait for the real MCP worker to observe EOF without starting NSApplication."""
        if not finished.wait(45):
            raise TimeoutError('The real MCP worker did not stop within the test deadline')

    ui_thread.ui.install_on_main_thread = inert_install
    ui_thread.ui.run_blocking = inert_loop
    ui_thread.ui.shutdown = finished.set
    skylight.is_available = lambda: False
    audit['substitutions'].append('SkyLight availability, inhibiting its window/input self-test')
    audit['substitutions'].append('private log and control-token paths; update check disabled')
    audit['native_call_guard_installed'] = True
    save_audit()
    atexit.register(save_audit)
    runpy.run_module('klyk.mcp_server', run_name='__main__')
    audit['nsapp_after_entry_is_nil'] = bool(real_nsapp) and real_nsapp[0]() is None
    audit['entry_completed'] = True
    save_audit()


if __name__ == '__main__':
    main()
