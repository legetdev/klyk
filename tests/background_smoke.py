"""Opt-in MCP acceptance against disposable, overlapping background windows only.

No user app is activated and the real ownership token is untouched. Independent
AppKit state verifies effects; test-app activation aborts the remaining checks.
"""

import argparse
import ast
import base64
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import plistlib
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klyk import capture, computer
from klyk.client import KlykClient
from release_check import fingerprint


def payload(result):
    """Read the structured outcome without retaining inline screenshots in reports."""
    return json.loads(next(b['text'] for b in reversed(result['content']) if b['type'] == 'text'))


def measure(function, count=9):
    """Report warm distributions separately from cold framework startup."""
    samples = []
    for _ in range(count):
        start = time.monotonic()
        function()
        samples.append(round((time.monotonic() - start) * 1000, 2))
    return {'samples_ms': samples, 'median_ms': statistics.median(samples),
            'p90_ms': sorted(samples)[math.ceil(len(samples) * .9) - 1]}


def paired_measure(first, second, count=9):
    """Alternate before/after reads to avoid assigning load drift to an implementation."""
    samples = [[], []]
    for iteration in range(count):
        for index in ((0, 1) if iteration % 2 == 0 else (1, 0)):
            start = time.monotonic()
            (first, second)[index]()
            samples[index].append(round((time.monotonic() - start) * 1000, 2))
    return [{'samples_ms': values, 'median_ms': statistics.median(values),
             'p90_ms': sorted(values)[math.ceil(len(values) * .9) - 1]} for values in samples]


def main():
    """Build a fixture, exercise the real protocol, and persist scoped evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.verification/background.json')
    args = parser.parse_args()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'fingerprint': fingerprint(), 'checks': [], 'calls': [], 'benchmarks': {},
              'scope': 'disposable overlapping AppKit windows; no browser or user-app activation',
              'foreground_transitions': [],
              'environment': {'python': sys.version, 'mcp': version('mcp'), 'macos': subprocess.check_output(
                  ['sw_vers', '-productVersion'], text=True).strip()}}

    def check(name, condition):
        """Retain failed assertions as evidence rather than only successful results."""
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    with tempfile.TemporaryDirectory(prefix='klyk-background-') as work:
        work = Path(work)
        bundle = work / 'KlykParityFixture.app'
        binary = bundle / 'Contents/MacOS/Fixture'
        binary.parent.mkdir(parents=True)
        subprocess.run(['xcrun', 'swiftc', '-module-cache-path', '/private/tmp/klyk-parity-swift-cache',
                        str(ROOT / 'tests/Fixture.swift'), '-o', str(binary)], check=True)
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'org.klyk.parity.fixture', 'CFBundleName': 'KlykParityFixture',
            'CFBundleExecutable': 'Fixture', 'CFBundlePackageType': 'APPL',
            'NSHighResolutionCapable': True, 'LSUIElement': True}))
        state = work / 'state.json'
        foreground = capture.frontmost_pid()
        old_owner = os.environ.get('KLYK_OWNER_FILE')
        old_update = os.environ.get('KLYK_UPDATE_CHECK')
        os.environ['KLYK_OWNER_FILE'] = str(work / 'owner')
        os.environ['KLYK_UPDATE_CHECK'] = '0'
        fixture = subprocess.Popen([str(binary)], env={**os.environ,
            'KLYK_FIXTURE_STATE': str(state), 'KLYK_FIXTURE_BACKGROUND': '1',
            'KLYK_FIXTURE_OVERLAP': '1'}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def foreground_safe(server_pid=None):
            """Allow the user to change apps, but never let fixture or test server take front."""
            now = capture.frontmost_pid()
            if now != foreground:
                report['foreground_transitions'].append({'before': foreground, 'after': now,
                                                        'fixture': fixture.pid, 'server': server_pid})
            return now not in (fixture.pid, server_pid) if now is not None else True

        def current():
            """Read actual fixture state, independent of the agent's action response."""
            return json.loads(state.read_text())

        def settled(predicate):
            """Wait only for a known fixture state without repeating any action."""
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(.03)
            return False

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                windows = capture.list_windows_for_pid(fixture.pid)
                if len(windows) >= 2 and state.exists():
                    break
                time.sleep(.05)
            check('two overlapping fixture windows available', len(windows) == 2)
            check('fixture launch stayed in background', foreground_safe())
            time.sleep(.3)  # One launch-animation settle, never an input retry.
            with KlykClient(timeout=20) as client:
                tools = client.list_tools()
                report['schema_bytes'] = len(json.dumps(tools))
                check('48 tools discovered over real MCP', len(tools) == 48)

                def call(name, **arguments):
                    """Call the shipped MCP entry point and enforce the background boundary."""
                    start = time.monotonic()
                    result = client.call(name, {'app': 'KlykParityFixture', **arguments})
                    data = payload(result)
                    report['calls'].append({'tool': name, 'wall_ms': round((time.monotonic() - start) * 1000, 2),
                        'result': data, 'images': sum(b['type'] == 'image' for b in result['content'])})
                    check(name + ' avoided test-app foreground', foreground_safe(client._proc.pid))
                    if data.get('error') or data.get('ok') is False:
                        raise AssertionError(f'{name}: {data}')
                    return data, result

                call('list_windows', bundle_id='org.klyk.parity.fixture', app_path=str(bundle))
                call('set_mode', mode='background')
                ids = {}
                for window in capture.list_windows_for_pid(fixture.pid):
                    nodes = computer.ax_snapshot(fixture.pid, window_id=window['window_id'], max_results=60)
                    label = next(e['label'] for e in nodes if e.get('label', '').startswith('Input '))
                    ids['Klyk Fixture A' if label == 'Input 0' else 'Klyk Fixture B'] = window['window_id']
                for index, title in enumerate(('Klyk Fixture A', 'Klyk Fixture B')):
                    wid = ids[title]
                    data, result = call('inspect', window_id=wid)
                    elements = data['ax_elements']
                    check(title + ' AX excludes sibling field', not any(e.get('label') == f'Input {1-index}' for e in elements))
                    field = next(e for e in elements if e.get('label') == f'Input {index}')
                    image = next(b['data'] for b in result['content'] if b['type'] == 'image')
                    pixels = capture.decode_png_to_rgb_array(image)
                    check(title + ' logical image dimensions match', pixels.shape[:2] == (data['height'], data['width']))
                    text = f'Window {index}: München 日本語 🧭'
                    filled, _ = call('fill_field', x=field['x'], y=field['y'], text=text)
                    check(title + ' fill readback verified', filled.get('verified') is True)
                    check(title + ' independent native field changed', settled(lambda: current()['fields'][index] == text))
                    read, _ = call('read_element', x=field['x'], y=field['y'])
                    check(title + ' covered read targets selected window', read['value'] == text)
                    before = current()['clicks']
                    call('click_element', label=f'Increment {index}')
                    check(title + ' semantic action occurred exactly once', settled(lambda: current()['clicks'] == before + 1))
                    # Duplicate labels are intentional fixture ambiguity, not a failed acceptance call.
                    ambiguous = payload(client.call('click_element', {'app': 'KlykParityFixture', 'label': 'Duplicate'}))
                    check(title + ' duplicate controls remain ambiguous', ambiguous.get('ambiguous') is True)
                    check(title + ' ambiguous action sent no input', current()['clicks'] == before + 1)
                    ax, _ = call('ax_snapshot')
                    check(title + ' expanded AX retains selection', ax['window_id'] == wid)
                    anchor = next(e for e in ax['elements'] if e.get('value') == f'Klyk visual anchor {index}')
                    rx, ry = max(0, anchor['x'] - anchor['width'] // 2 - 3), max(0, anchor['y'] - 30)
                    rw, rh = min(data['width'] - rx, anchor['width'] + 6), min(data['height'] - ry, 55)
                    read, _ = call('read_text', level='accurate', x=rx, y=ry, width=rw, height=rh)
                    if f'Klyk visual anchor {index}' not in read['full_text']:
                        output.with_suffix('.png').write_bytes(base64.b64decode(image))
                        report['ocr_region'] = [rx, ry, rw, rh]
                    check(title + ' covered OCR sees own visual anchor', f'Klyk visual anchor {index}' in read['full_text'])
                    check(title + ' regional OCR retains full-window coordinates', all(
                        rx <= e['x'] <= rx + rw and ry <= e['y'] <= ry + rh for e in read['observations']))
                    grid, _ = call('read_grid', rows=1, cols=1, x=field['x']-10, y=field['y']-5,
                                   cell_width=20, cell_height=10)
                    check(title + ' grid text excludes overlapping sibling', grid['cells'][0][0]['text'] == text)

                # Background mouse delivery, distinct from AX actions, must change native state.
                data, _ = call('inspect')
                button = next(e for e in data['ax_elements'] if e.get('label') == 'Increment 1')
                before = current()['clicks']
                call('click', x=button['x'], y=button['y'])
                check('background pointer click delivered once', settled(lambda: current()['clicks'] == before + 1))
                slider = next(e for e in data['ax_elements'] if e.get('label') == 'Level')
                before = current()['selections']
                call('drag', x1=slider['x']-65, y1=slider['y'], x2=slider['x']+65, y2=slider['y'])
                check('background drag reached native slider', settled(lambda: current()['selections'] > before))
                field = next(e for e in data['ax_elements'] if e.get('label') == 'Input 1')
                call('fill_field', x=field['x'], y=field['y'], text='')
                call('click', x=field['x'], y=field['y'])
                call('type_text', text='Typed München', mode='keys')
                check('background native keystrokes reached selected field', settled(
                    lambda: current()['fields'][1] == 'Typed München'))
                before_scroll = current()['scroll'][1]
                call('scroll', x=180, y=132, direction='down', amount=5)
                check('background scroll changed selected view', settled(
                    lambda: current()['scroll'][1] != before_scroll))
                # Subsequent looks must reflect fresh content, not cached pixels.
                first, first_result = call('inspect')
                before_pixels = capture.decode_png_to_rgb_array(next(
                    b['data'] for b in first_result['content'] if b['type'] == 'image'))
                field = next(e for e in first['ax_elements'] if e.get('label') == 'Input 1')
                long_text = 'Résumé 🧭 日本語 ' * 250
                call('fill_field', x=field['x'], y=field['y'], text=long_text)
                check('long Unicode native write preserved', settled(lambda: current()['fields'][1] == long_text))
                _, fresh_result = call('inspect')
                after_pixels = capture.decode_png_to_rgb_array(next(
                    b['data'] for b in fresh_result['content'] if b['type'] == 'image'))
                row = field['y']
                check('capture plan cache returns fresh field pixels', bool(
                    (before_pixels[row-10:row+10, 25:340] != after_pixels[row-10:row+10, 25:340]).any()))
                slim, _ = call('inspect', detail='slim')
                check('long native values stay bounded in observations', all(len(e.get('value', '')) <= 201 for e in slim['ax_elements']))
                check('fixture actions stayed in background', foreground_safe(client._proc.pid))

                # Time repeatable real protocol reads after initial framework warmup.
                report['benchmarks']['inspect_warm'] = measure(lambda: payload(client.call('inspect', {'app': 'KlykParityFixture'})))
                report['benchmarks']['inspect_slim_warm'] = measure(lambda: payload(client.call('inspect', {'app': 'KlykParityFixture', 'detail': 'slim'})))
                # Compare the old all-window traversal to selected-window traversal on identical UI.
                baseline_source = subprocess.check_output(['git', 'show', '355b32f:klyk/computer.py'], cwd=ROOT, text=True)
                node = next(n for n in ast.parse(baseline_source).body if isinstance(n, ast.FunctionDef) and n.name == 'ax_snapshot')
                namespace = dict(computer.__dict__)
                exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<baseline>', 'exec'), namespace)
                old, new = paired_measure(
                    lambda: namespace['ax_snapshot'](fixture.pid, max_results=300),
                    lambda: computer.ax_snapshot(fixture.pid, max_results=300, window_id=wid))
                report['benchmarks']['baseline_all_windows_ax'] = old
                report['benchmarks']['selected_window_ax'] = new
                check('benchmark reads preserved foreground', foreground_safe())
                report['completed'] = True
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            fixture.terminate()
            fixture.wait(timeout=5)
            for name, value in (('KLYK_OWNER_FILE', old_owner), ('KLYK_UPDATE_CHECK', old_update)):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps({'checks': len(report['checks']), 'completed': report.get('completed', False),
                              'error': report.get('error'), 'benchmarks': report['benchmarks']}), flush=True)


if __name__ == '__main__':
    main()
