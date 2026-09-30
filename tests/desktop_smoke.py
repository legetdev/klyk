"""Opt-in Chrome/Electron checks using disposable data and independently observed outcomes."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import plistlib
import re
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klyk.client import KlykClient
from live_smoke import finalize_report, fixture_panel_diagnostic, stop_fixture_process, text_payload
from release_check import fingerprint


def electron_editor_ready(computer, pid, expected):
    """Require the owned editor's focused native text value before replacing its contents."""
    focus=computer.ax_focused_summary(pid).get('focused',{})
    return focus.get('role') in ('AXTextArea','AXTextField') and focus.get('value')==expected


def electron_fixture_diagnostic(computer, pid, document):
    """Keep bounded read-only focus and file evidence from this disposable editor only."""
    result={'scope':'owned disposable Electron fixture and generated document only'}
    try:
        with document.open('rb') as stream:
            data=stream.read(8193)
        result.update(saved_text=data[:8192].decode('utf-8',errors='replace'),
                      saved_text_truncated=len(data)>8192)
    except (OSError,UnicodeError) as error:
        result['file_read_error']=type(error).__name__
    try:
        result['native']=fixture_panel_diagnostic(computer,pid)
    except Exception as error:
        result['native_probe_error']=type(error).__name__
    return result


def main():
    """Open one temporary Chrome window and an isolated VS Code profile, then clean up both."""
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.verification/desktop.json');args=parser.parse_args()
    work=ROOT/'.verification';work.mkdir(exist_ok=True)
    state={};report={'environment':{'mcp':version('mcp'),'python':sys.version,'macos':subprocess.check_output(['sw_vers','-productVersion'],text=True).strip(),'apps':{name:plistlib.loads(Path('/Applications',name+'.app/Contents/Info.plist').read_bytes()).get('CFBundleShortVersionString') for name in ('Google Chrome','Visual Studio Code')}},'fingerprint':fingerprint(),'checks':[],'calls':[],'browser_state':state};chrome=None;editor=None;document=None
    chrome_profile=tempfile.TemporaryDirectory(prefix='chrome-profile-',dir=work)
    def check(name, predicate, timeout=4):
        """Wait for an independent outcome without retrying an input action."""
        deadline=time.monotonic()+timeout
        while True:
            ok=bool(predicate())
            remaining=deadline-time.monotonic()
            if ok or remaining<=0:break
            time.sleep(min(.1,remaining))
        report['checks'].append({'name':name,'passed':ok})
        if not ok:raise AssertionError(name)
    def call(c,tool,app='Google Chrome',**args):
        """Record real MCP results without retaining image payloads."""
        start=time.monotonic();data=text_payload(c.call(tool,{'app':app,**args}))
        report['calls'].append({'tool':tool,'app':app,'wall_ms':round((time.monotonic()-start)*1000),'result':data})
        print(tool,app,'ERROR' if data.get('error') else data.get('ok',True),flush=True)
        return data
    def observed_fixture(c,app,wid,required):
        """Read fresh evidence while a cold renderer loads; never repeat an input action."""
        observation=call(c,'inspect',app=app,window_id=wid)
        labels=json.dumps(observation)
        return all(label in labels for label in required) and 'CLAUDE.md' not in labels
    class Handler(BaseHTTPRequestHandler):
        """Serve only a static fixture and collect its disposable event state."""
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write((ROOT/'tests/browser_fixture.html').read_bytes())
        def do_POST(self):
            data=json.loads(self.rfile.read(min(int(self.headers.get('Content-Length','0')),4096)));state.update(data)
            self.send_response(204);self.end_headers()
        def log_message(self,*args):
            """Keep HTTP noise and request data out of terminal logs."""
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    from klyk import capture, computer
    from AppKit import NSWorkspace
    previous=NSWorkspace.sharedWorkspace().frontmostApplication()
    clipboard=computer._snapshot_pasteboard()
    try:
        with KlykClient(timeout=45) as c:
            call(c,'take_control')
            url=f'http://127.0.0.1:{server.server_port}/'
            # The fixture owns this process/profile and needs no additional AppleEvents grant.
            chrome_app=Path('/Applications/Google Chrome.app')
            chrome_info=plistlib.loads((chrome_app/'Contents/Info.plist').read_bytes())
            chrome_executable=chrome_app/'Contents/MacOS'/chrome_info['CFBundleExecutable']
            existing=subprocess.run(['pgrep','-f','^'+re.escape(str(chrome_executable))+'( |$)'],capture_output=True,text=True)
            if existing.returncode not in (0,1):raise RuntimeError('Chrome process isolation could not be checked')
            if existing.stdout.strip():raise RuntimeError('An existing Chrome process prevents isolated app-name targeting')
            chrome=subprocess.Popen([str(chrome_executable),'--user-data-dir='+chrome_profile.name,
                                     '--no-first-run','--no-default-browser-check','--force-renderer-accessibility','--new-window',
                                     '--window-position=50,50','--window-size=900,650',url],
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            report['browser_pid']=chrome.pid
            ready=capture.wait_for_window(chrome.pid,timeout=20)
            if ready is None:raise RuntimeError('The isolated Chrome fixture window did not appear')
            check('browser fixture loaded',lambda:'count' in state)
            identity=call(c,'list_windows')
            windows=identity['windows']
            check('one isolated browser window identified',lambda:identity.get('pid')==chrome.pid and len(windows)==1)
            wid=windows[0]['window_id']
            check('browser fixture observed',lambda:observed_fixture(c,'Google Chrome',wid,
                  ('Klyk Browser Fixture','Increment browser','Fixture input')),timeout=20)
            call(c,'set_mode',mode='background')
            refused=call(c,'click',window_id=wid,x=400,y=300)
            check('background Chromium click refused',lambda:refused.get('requires_foreground') and state['count']==0)
            call(c,'set_mode',mode='autonomous')
            call(c,'click_element',window_id=wid,label='Increment browser')
            call(c,'inspect',window_id=wid)
            check('Chromium click delivered once',lambda:state.get('count')==1)
            call(c,'click_element',window_id=wid,label='Fixture input')
            call(c,'type_text',window_id=wid,text='Browser input',mode='keys')
            check('Chromium keys delivered',lambda:state.get('text')=='Browser input')
            call(c,'press_key',window_id=wid,key='cmd+a')
            call(c,'type_text',window_id=wid,text='Browser paste',mode='paste')
            check('Chromium paste delivered',lambda:state.get('text')=='Browser paste')
            # VS Code is an installed Electron host; separate user/extension data
            # isolates settings and workspace state from any normal editor window.
            document=work/'electron-fixture.txt';document.write_text('Electron baseline')
            editor_app=Path('/Applications/Visual Studio Code.app')
            editor_info=plistlib.loads((editor_app/'Contents/Info.plist').read_bytes())
            executable=editor_app/'Contents/MacOS'/editor_info['CFBundleExecutable']
            if not executable.is_file():raise RuntimeError('VS Code fixture host is not installed')
            existing=subprocess.run(['pgrep','-f','^'+re.escape(str(executable))+'( |$)'],capture_output=True,text=True)
            if existing.returncode not in (0,1):raise RuntimeError('VS Code process isolation could not be checked')
            if existing.stdout.strip():raise RuntimeError('An existing VS Code process prevents isolated app-name targeting')
            editor=subprocess.Popen([str(executable),'--user-data-dir',str(work/'vscode-profile'),'--extensions-dir',str(work/'vscode-extensions'),'--disable-extensions','--disable-workspace-trust','--force-renderer-accessibility','--skip-welcome','--skip-release-notes','--new-window',str(document)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            report['editor_pid']=editor.pid
            ready=capture.wait_for_window(editor.pid,timeout=20)
            check('isolated Electron window exists',lambda:ready is not None)
            identity=call(c,'list_windows',app='Klyk Electron Fixture',bundle_id='com.microsoft.VSCode')
            check('Electron PID matches isolated profile',lambda:identity.get('pid')==editor.pid)
            editor_wid=identity['windows'][0]['window_id']
            check('Electron document matches fixture before input',lambda:observed_fixture(c,'Klyk Electron Fixture',
                  editor_wid,('electron-fixture.txt',)),timeout=20)
            call(c,'press_key',app='Klyk Electron Fixture',window_id=editor_wid,key='cmd+1')
            check('Electron editor focused before replacement',lambda:electron_editor_ready(
                  computer,editor.pid,'Electron baseline'),timeout=10)
            call(c,'press_key',app='Klyk Electron Fixture',window_id=editor_wid,key='cmd+a')
            call(c,'type_text',app='Klyk Electron Fixture',window_id=editor_wid,text='Electron verified 🧭',mode='keys')
            report['electron_after_typing']=computer.ax_focused_summary(editor.pid)
            call(c,'press_key',app='Klyk Electron Fixture',window_id=editor_wid,key='cmd+s')
            check('Electron editor saved exact Unicode text',lambda:document.read_text()=='Electron verified 🧭')
            call(c,'close_app',app='Klyk Electron Fixture')
            report['completed']=True
    except BaseException as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        if editor is not None and editor.poll() is None and document is not None:
            report['electron_failure_diagnostic']=electron_fixture_diagnostic(computer,editor.pid,document)
        raise
    finally:
        finalize_report(report,ROOT/args.output,(
            lambda:stop_fixture_process(chrome),lambda:stop_fixture_process(editor),
            server.shutdown,server.server_close,chrome_profile.cleanup,
            lambda:computer._restore_pasteboard(clipboard),
            lambda:previous.activateWithOptions_(0) if previous is not None else None,
        ))


if __name__=='__main__':main()
