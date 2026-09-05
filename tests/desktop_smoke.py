"""Opt-in Chrome/Electron checks using disposable data and independently observed outcomes."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import plistlib
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klyk.client import KlykClient
from live_smoke import text_payload
from release_check import fingerprint


def main():
    """Open one temporary Chrome window and an isolated VS Code profile, then clean up both."""
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.verification/desktop.json');args=parser.parse_args()
    work=ROOT/'.verification';work.mkdir(exist_ok=True)
    state={};report={'environment':{'mcp':version('mcp'),'python':sys.version,'macos':subprocess.check_output(['sw_vers','-productVersion'],text=True).strip(),'apps':{name:plistlib.loads(Path('/Applications',name+'.app/Contents/Info.plist').read_bytes()).get('CFBundleShortVersionString') for name in ('Google Chrome','Visual Studio Code')}},'fingerprint':fingerprint(),'checks':[],'calls':[],'browser_state':state};chrome_window=None;editor=None
    def check(name, predicate, timeout=4):
        """Wait for an independent outcome without retrying an input action."""
        deadline=time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:time.sleep(.1)
        ok=bool(predicate());report['checks'].append({'name':name,'passed':ok})
        if not ok:raise AssertionError(name)
    def call(c,tool,app='Google Chrome',**args):
        """Record real MCP results without retaining image payloads."""
        start=time.monotonic();data=text_payload(c.call(tool,{'app':app,**args}))
        report['calls'].append({'tool':tool,'app':app,'wall_ms':round((time.monotonic()-start)*1000),'result':data})
        print(tool,app,'ERROR' if data.get('error') else data.get('ok',True),flush=True)
        return data
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
            initial=call(c,'list_windows');before={w['window_id'] for w in initial['windows']}
            url=f'http://127.0.0.1:{server.server_port}/'
            script=f'tell application "Google Chrome"\nset w to make new window\nset bounds of w to {{100, 100, 1000, 800}}\nset URL of active tab of w to "{url}"\nreturn id of w\nend tell'
            chrome_window=int(subprocess.check_output(['osascript','-e',script],text=True).strip())
            check('browser fixture loaded',lambda:'count' in state)
            windows=call(c,'list_windows')['windows'];new=[w for w in windows if w['window_id'] not in before]
            check('one isolated browser window identified',lambda:len(new)==1)
            wid=new[0]['window_id']
            observation=call(c,'inspect',window_id=wid)
            check('browser fixture observed',lambda:'Klyk Browser Fixture' in json.dumps(observation))
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
            executable=Path('/Applications/Visual Studio Code.app/Contents/MacOS/Code')
            if not executable.is_file():raise RuntimeError('VS Code fixture host is not installed')
            existing=subprocess.run(['pgrep','-f','^/Applications/Visual Studio Code.app/Contents/MacOS/Code'],capture_output=True,text=True)
            if existing.stdout.strip():raise RuntimeError('An existing VS Code process prevents isolated app-name targeting')
            editor=subprocess.Popen([str(executable),'--user-data-dir',str(work/'vscode-profile'),'--extensions-dir',str(work/'vscode-extensions'),'--disable-extensions','--disable-workspace-trust','--skip-welcome','--skip-release-notes','--new-window',str(document)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            report['editor_pid']=editor.pid
            ready=capture.wait_for_window(editor.pid,timeout=20)
            check('isolated Electron window exists',lambda:ready is not None)
            identity=call(c,'list_windows',app='Klyk Electron Fixture',bundle_id='com.microsoft.VSCode')
            check('Electron PID matches isolated profile',lambda:identity.get('pid')==editor.pid)
            observation=call(c,'inspect',app='Klyk Electron Fixture')
            check('Electron document matches fixture before input',lambda:'electron-fixture.txt' in json.dumps(observation) and 'CLAUDE.md' not in json.dumps(observation))
            call(c,'press_key',app='Klyk Electron Fixture',key='cmd+1')
            call(c,'press_key',app='Klyk Electron Fixture',key='cmd+a')
            call(c,'type_text',app='Klyk Electron Fixture',text='Electron verified 🧭',mode='keys')
            call(c,'press_key',app='Klyk Electron Fixture',key='cmd+s')
            check('Electron editor saved exact Unicode text',lambda:document.read_text()=='Electron verified 🧭')
            call(c,'close_app',app='Klyk Electron Fixture')
            report['completed']=True
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}';raise
    finally:
        (ROOT/args.output).write_text(json.dumps(report,indent=2))
        if chrome_window is not None:
            subprocess.run(['osascript','-e',f'tell application "Google Chrome" to close window id {chrome_window}'],capture_output=True)
        if editor is not None and editor.poll() is None:
            editor.terminate()
            try:editor.wait(timeout=5)
            except subprocess.TimeoutExpired:editor.kill();editor.wait()
        server.shutdown();server.server_close()
        computer._restore_pasteboard(clipboard)
        if previous is not None:previous.activateWithOptions_(0)
        (ROOT/args.output).write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
