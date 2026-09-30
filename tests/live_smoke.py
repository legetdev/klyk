"""Opt-in real macOS fixture verification; output stays in the ignored .verification directory."""
import argparse
import base64
import json
import os
from pathlib import Path
import plistlib
import subprocess
import shutil
import sys
import time

import jsonschema

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from klyk.client import KlykClient
from release_check import expected_tools, fingerprint
from importlib.metadata import version


def text_payload(result):
    """Extract protocol text while retaining image metadata separately."""
    blocks=[b for b in result.get('content',[]) if b.get('type')=='text']
    return json.loads(blocks[-1]['text']) if blocks else {}


def fixture_environment(state_path, *, receiver=False):
    """Keep default desktop geometry while fitting remote compact fixtures without shrinking controls."""
    environment={**os.environ,'KLYK_FIXTURE_STATE':str(state_path)}
    if environment.get('KLYK_FIXTURE_COMPACT')=='1':
        environment.update(KLYK_FIXTURE_OFFSET='0',KLYK_FIXTURE_OVERLAP='1' if receiver else '0')
    elif receiver:
        environment['KLYK_FIXTURE_OFFSET']='880'
    return environment


def compact_layout_matches(state, *, receiver=False, moved=False):
    """Validate independently reported native bounds and unchanged control content dimensions."""
    windows=state.get('windows',[])
    expected={'Klyk Fixture A':120,'Klyk Fixture B':120 if receiver else 560 if moved else 540}
    return state.get('layout')=='compact' and len(windows)==2 and {w.get('title') for w in windows}==set(expected) and all(
        abs(window['x']-expected[window['title']])<=1 and abs(window['y']-20)<=1
        and abs(window['width']-400)<=1 and abs(window['content_width']-400)<=1
        and abs(window['content_height']-600)<=1 and 600<=window['height']<=650
        for window in windows)



def held_key_released(before, after, *, key_code=124):
    """Require a fixture-observed key-down and exactly one matching key-up, including repeats."""
    previous=before.get('input_events');current=after.get('input_events')
    if not isinstance(previous,dict) or not isinstance(current,dict):
        return False
    down=f'key_down:{key_code}';up=f'key_up:{key_code}'
    return current.get(down,0)>previous.get(down,0) and current.get(up,0)==previous.get(up,0)+1


def fixture_input_unchanged(before, after):
    """Refusal needs no delivered input or independently observed mutable fixture effect."""
    fields=('input_events','fields','clicks','selections','selection','scroll','drops','opened')
    return all(field in before and field in after and before[field]==after[field] for field in fields)


def main():
    """Run only against a disposable fixture and preserve evidence even on failure."""
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.verification/live.json');args=parser.parse_args()
    out=ROOT/args.output;out.parent.mkdir(parents=True,exist_ok=True)
    work=ROOT/'.verification';bundle=work/'Fixture.app';binary=bundle/'Contents/MacOS/Fixture'
    binary.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['xcrun','swiftc','-module-cache-path',str(work/'swift-cache'),str(ROOT/'tests/Fixture.swift'),'-o',str(binary)],check=True)
    (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'org.klyk.regression.fixture','CFBundleName':'Klyk Fixture','CFBundleExecutable':'Fixture','CFBundlePackageType':'APPL','NSHighResolutionCapable':True}))
    state=work/'fixture-state.json'
    if state.exists(): state.unlink()
    receiver=None
    compact=os.environ.get('KLYK_FIXTURE_COMPACT')=='1'
    fixture=subprocess.Popen([str(binary)],env=fixture_environment(state),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    report={'fingerprint':fingerprint(),'environment':{'mcp':version('mcp'),'numpy':version('numpy'),'klyk':__import__('klyk').__version__,'swift':subprocess.check_output(['xcrun','swiftc','--version'],text=True).strip(),'python':sys.version,'macos':subprocess.check_output(['sw_vers','-productVersion'],text=True).strip()},'calls':[],'checks':[],
            'fixture_layout':{'mode':'compact' if compact else 'default','bounds_coordinates':'Cocoa frame/content points; bottom-left origin'}}
    def check(name,condition):
        """Record an independent observable check before raising on failure."""
        report['checks'].append({'name':name,'passed':bool(condition)})
        if not condition: raise AssertionError(name)
    def settled(predicate, timeout=2):
        """Wait for asynchronous native effects without repeating the action."""
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return True
            time.sleep(.05)
        return False
    def call(client,name,**kw):
        """Invoke the real MCP tool and record its result without inline image payloads."""
        start=time.monotonic(); result=client.call(name,{'app':'Klyk Fixture',**kw}); data=text_payload(result)
        report['calls'].append({'tool':name,'arguments':kw,'wall_ms':round((time.monotonic()-start)*1000),'payload_bytes':len(json.dumps(result)),'result':data,'images':sum(b.get('type')=='image' for b in result.get('content',[]))})
        time.sleep(.1)
        report['calls'][-1]['fixture_state']=json.loads(state.read_text()) if state.exists() else {}
        out.write_text(json.dumps(report,indent=2))
        print(name, 'ERROR '+str(data.get('error')) if data.get('error') else ('ok='+str(data.get('ok',True))),flush=True)
        return data
    try:
        deadline=time.monotonic()+10
        while not state.exists() and time.monotonic()<deadline: time.sleep(.1)
        check('fixture started',state.exists())
        report['fixture_layout']['primary_initial']=json.loads(state.read_text())['windows']
        if compact:
            check('compact primary native bounds preserve controls',compact_layout_matches(json.loads(state.read_text())))
        with KlykClient(timeout=30) as client:
            tools=client.list_tools(); report['schema_bytes']=len(json.dumps(tools)); report['tools']=[t['name'] for t in tools]
            check('48 tools discovered',len(report['tools'])==len(set(report['tools']))==48 and set(report['tools'])==expected_tools())
            for tool in tools:
                schema=tool['inputSchema']
                jsonschema.validators.validator_for(schema).check_schema(schema)
            check('all 48 real SDK schemas validate',True)
            call(client,'take_control')
            call(client,'screen_info')
            # MCP can reject invalid input before dispatch with a plain-text error.
            invalid_focus=client.call('focus_window',{'app':'Klyk Fixture'})
            report['invalid_focus']=invalid_focus
            check('app-only focus rejected',invalid_focus.get('isError') is True or text_payload(invalid_focus).get('ok') is False)
            windows=call(client,'list_windows',bundle_id='org.klyk.regression.fixture',app_path=str(bundle))
            if compact:
                target_id=next(window['id'] for window in json.loads(state.read_text())['windows'] if window['title']=='Klyk Fixture B')
                target=next(window for window in windows['windows'] if window['window_id']==target_id)
                report['fixture_layout']['selected_primary_window_id']=target_id
            else:
                target=windows['windows'][0]
            observation=call(client,'inspect',detail='full',**({'window_id':target['window_id']} if compact else {}))
            call(client,'ax_snapshot')
            call(client,'read_text',level='accurate')
            # Resolve all control coordinates from this live observation.
            elements=observation['ax_elements']
            def element(label):
                """Use current semantic anchors instead of hardcoded screen coordinates."""
                return next(e for e in elements if e.get('label')==label)
            def point(label):
                """Return window-relative center coordinates for a known control."""
                e=element(label);return {'x':e['x'],'y':e['y']}
            def current():
                """Read independent AppKit state after asynchronous UI delivery settles."""
                time.sleep(.15);return json.loads(state.read_text())
            label=next(e['label'] for e in elements if e.get('label','').startswith('Increment '))
            field_label=next(e['label'] for e in elements if e.get('label','').startswith('Input '))
            field_index=int(field_label.split()[-1]);button=point(label);field=point(field_label)
            before=current()['clicks']
            call(client,'click_element',label=label,verify=True)
            check('semantic click changed native counter',current()['clicks']==before+1)
            before=current()['clicks']
            ambiguous=call(client,'click_element',label='Duplicate')
            check('duplicate AX labels click nothing',ambiguous.get('ambiguous') and current()['clicks']==before)
            call(client,'click_element',label='Duplicate',index=1)
            check('explicit duplicate index clicks once',current()['clicks']==before+1)
            for tool in ('click','double_click','triple_click','long_press'):
                before=current()['clicks']
                call(client,tool,**button,**({'duration':.1} if tool=='long_press' else {}))
                check(tool+' delivered native input',current()['clicks']>before)
            before=current()['clicks']
            call(client,'ax_action',**button,action='AXPress')
            check('AX action delivered',current()['clicks']==before+1)
            call(client,'fill_field',**field,text='Filled value',verify=True)
            check('AX field value changed',current()['fields'][field_index]=='Filled value')
            call(client,'click',**field)
            call(client,'press_key',key='cmd+a')
            call(client,'type_text',text='Typed value',mode='keys')
            check('keyboard input changed field',current()['fields'][field_index]=='Typed value')
            held_before=current()
            call(client,'hold_key',key='Right',duration=.1)
            check('held key released',held_key_released(held_before,current()))
            call(client,'press_key',key='a')
            call(client,'press_key',key='cmd+a')
            call(client,'type_text',text='Pasted value',mode='paste')
            check('paste input changed field',current()['fields'][field_index]=='Pasted value')
            # Preserve all original clipboard types around explicit clipboard tool tests.
            from klyk import computer
            clipboard=computer._snapshot_pasteboard()
            try:
                call(client,'set_clipboard',text='Klyk clipboard fixture')
                clip=call(client,'get_clipboard')
                check('clipboard round trip','Klyk clipboard fixture' in json.dumps(clip))
            finally:
                computer._restore_pasteboard(clipboard)
            call(client,'read_element',**field)
            call(client,'wait_for',text=label,timeout=1)
            call(client,'wait',seconds=.01)
            call(client,'get_pixel',**button)
            call(client,'get_pixels',points=[button])
            grid=call(client,'read_grid',rows=1,cols=1,x=button['x'],y=button['y'],cell_width=10,cell_height=10)
            check('grid observation returned',not grid.get('error'))
            template=call(client,'get_template',x1=int(button['x']-25),y1=int(button['y']-10),x2=int(button['x']+25),y2=int(button['y']+10))
            match=call(client,'find_template',template_id=template['template_id'])
            check('template matches current native control',match.get('found'))
            visible=call(client,'wait_for_visual',template_id=template['template_id'],timeout=1)
            check('visual readiness found',visible.get('found'))
            screenshot=call(client,'screenshot',save_path=str(work/'fixture.png'))
            check('screenshot saved',Path(screenshot['saved_path']).is_file())
            call(client,'move_cursor',**button,dwell_seconds=.1)
            call(client,'select_option',**point('Choice'),option='Second')
            slim=call(client,'inspect',detail='slim')
            check('popup choice observed','Second' in json.dumps(slim))
            before=current()['clicks']
            call(client,'click_menu',path=['File','Increment'])
            check('native menu changed counter',current()['clicks']==before+1)
            before=current()['clicks']
            context=call(client,'context_menu_select',x=380,y=590,item_label='Increment context')
            check('context menu changed counter',context.get('ok') and settled(lambda: current()['clicks']==before+1))
            slider=point('Level')
            before=current()['selections']
            call(client,'drag',x1=slider['x'],y1=slider['y'],x2=slider['x']+80,y2=slider['y'])
            check('native slider drag changed state',current()['selections']>before)
            call(client,'set_mode',mode='humanoid')
            drop=call(client,'drag_to_element',source_label='Drag sample',target_label='Drop target')
            check('semantic drag delivered payload',drop.get('ok') and 'Klyk drag payload' in current()['drops'])
            call(client,'set_mode',mode='autonomous')
            before=current()['scroll'][field_index]
            call(client,'scroll',x=190,y=130,direction='down',amount=6)
            check('scroll moved native document',current()['scroll'][field_index]!=before)
            call(client,'set_window_bounds',window_id=target['window_id'],x=target['x']+20,y=target['y'],width=target['width'],height=target['height'])
            moved=call(client,'list_windows')
            check('explicit window moved',any(w['window_id']==target['window_id'] and abs(w['x']-target['x']-20)<3 for w in moved['windows']))
            report['fixture_layout']['primary_after_move']=current()['windows']
            if compact:
                check('compact selected native window stays on right',compact_layout_matches(current(),moved=True))
            call(client,'focus_window',window_id=target['window_id'])
            # Test the actual save sheet and verify a file independently of the tool result.
            saved=work/'saved-fixture.txt'
            if saved.exists(): saved.unlink()
            call(client,'click_menu',path=['File','Save Test File'])
            call(client,'wait_for',text='Save As:',timeout=3)
            call(client,'handle_system_dialog',action='save')
            check('native save produced expected file',(work/'saved-fixture.txt').read_text()=='Klyk fixture saved')
            call(client,'click_menu',path=['File','Open Test File'])
            call(client,'wait_for',text='Open',timeout=3)
            opened=call(client,'handle_system_dialog',action='open',path=str(saved))
            check('native open read exact saved contents',opened.get('ok') and settled(lambda:current()['opened']=='Klyk fixture saved'))
            missing_before=current()
            missing=call(client,'handle_system_dialog',action='save')
            check('missing save panel sends no input',not missing.get('ok') and fixture_input_unchanged(missing_before,current()))
            call(client,'click_menu',path=['File','Save Test File'])
            call(client,'wait_for',text='Save As:',timeout=3)
            call(client,'handle_system_dialog',action='cancel')
            # Global media input is exercised as a reversible mute toggle.
            mute_before=subprocess.check_output(['osascript','-e','output muted of (get volume settings)'],text=True).strip()
            try:
                call(client,'press_system_key',key='mute')
                time.sleep(.2)
                mute_after=subprocess.check_output(['osascript','-e','output muted of (get volume settings)'],text=True).strip()
                check('system mute toggled',mute_after!=mute_before)
            finally:
                subprocess.run(['osascript','-e','set volume output muted '+mute_before],check=True)
            before=current()['clicks']
            stopped=call(client,'run',actions=[{'tool':'click','x':99999,'y':99999},{'tool':'click_element','label':label}])
            check('failed batch stops dependent input',not stopped['ok'] and stopped['skipped_steps']==1 and current()['clicks']==before)
            call(client,'set_mode',mode='background')
            call(client,'set_mode',mode='autonomous')
            call(client,'get_logs');call(client,'get_escalation_log');call(client,'list_sessions');call(client,'resume')
            verdict=call(client,'verdict',test_description='Fixture actions independently verified')
            check('verdict discloses unverified evidence','UNVERIFIED' in verdict.get('instruction',''))
            # Cross-app delivery and invisible background behavior use a second
            # instance with its own bundle identity and independent state file.
            receiver_bundle=work/'Receiver.app';receiver_binary=receiver_bundle/'Contents/MacOS/Receiver'
            receiver_binary.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(binary,receiver_binary)
            (receiver_bundle/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'org.klyk.regression.receiver','CFBundleName':'Klyk Receiver','CFBundleExecutable':'Receiver','CFBundlePackageType':'APPL'}))
            receiver_state=work/'receiver-state.json'
            if receiver_state.exists():receiver_state.unlink()
            receiver=subprocess.Popen([str(receiver_binary)],env=fixture_environment(receiver_state,receiver=True),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            check('receiver started',settled(lambda:receiver_state.exists()))
            report['fixture_layout']['receiver_initial']=json.loads(receiver_state.read_text())['windows']
            if compact:
                check('compact receiver native bounds stay on left',compact_layout_matches(json.loads(receiver_state.read_text()),receiver=True))
            call(client,'list_windows',app='Klyk Receiver',bundle_id='org.klyk.regression.receiver',app_path=str(receiver_bundle))
            from klyk import capture
            import Quartz
            foreground=capture.frontmost_pid();cursor=Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            call(client,'set_mode',mode='background')
            before=current()['clicks']
            call(client,'click',window_id=target['window_id'],**button)
            check('background native click delivered',settled(lambda:current()['clicks']==before+1))
            after_cursor=Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            check('background native click preserved focus and cursor',capture.frontmost_pid()==foreground and abs(cursor.x-after_cursor.x)<1 and abs(cursor.y-after_cursor.y)<1)
            refused=call(client,'long_press',**button,duration=.1)
            check('background visible input refused',refused.get('requires_foreground'))
            call(client,'set_mode',mode='autonomous')
            dropped=call(client,'drag_to_element',source_label='Drag sample',target_label='Drop target',target_app='Klyk Receiver')
            check('cross-app drop independently received',dropped.get('ok') and settled(lambda:'Klyk drag payload' in json.loads(receiver_state.read_text())['drops']))
            # A second real MCP connection can take control; blocked clients must
            # not deliver input, and a dead owner must not wedge the next action.
            before=current()['clicks']
            with KlykClient(timeout=30) as second:
                second.call('take_control',{})
                blocked=call(client,'click',**button)
                check('non-owner input blocked',blocked.get('blocked')=='not_active_session' and current()['clicks']==before)
            call(client,'click',window_id=target['window_id'],**button)
            check('dead owner recovered',settled(lambda:current()['clicks']==before+1))
            call(client,'close_app',app='Klyk Receiver')
            receiver.wait(timeout=5)
            # Closing one selected window must not terminate the other window.
            call(client,'press_key',window_id=target['window_id'],key='cmd+w')
            check('closed-window animation finished',settled(lambda:capture.get_window_by_id(target['window_id']) is None))
            remaining=call(client,'list_windows')
            check('selected window closed and sibling survived',remaining['count']==1 and fixture.poll() is None)
            stale=call(client,'click',window_id=target['window_id'],**button)
            check('stale window refused without closing app',bool(stale.get('error')) and fixture.poll() is None)
            call(client,'inspect',window_id=remaining['windows'][0]['window_id'])
            call(client,'close_app')
            fixture.wait(timeout=5)
            check('close_app closed fixture',fixture.poll() is not None)
            fixture=subprocess.Popen([str(binary)],env=fixture_environment(state),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            check('second fixture started',settled(lambda: current()['pid']==fixture.pid))
            call(client,'list_windows',bundle_id='org.klyk.regression.fixture',app_path=str(bundle))
            call(client,'close_apps',apps=['Klyk Fixture'])
            fixture.wait(timeout=5)
            check('close_apps closed fixture',fixture.poll() is not None)
            check('all 48 tools exercised',set(report['tools'])=={c['tool'] for c in report['calls']})
            report['completed']=True
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        out.write_text(json.dumps(report,indent=2))
        if receiver is not None and receiver.poll() is None:
            receiver.terminate();receiver.wait(timeout=5)
        fixture.terminate()
        try:fixture.wait(timeout=5)
        except subprocess.TimeoutExpired: fixture.kill();fixture.wait()


if __name__=='__main__': main()
