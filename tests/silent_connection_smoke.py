"""Actual SDK stdio On/Off transitions and late-reply rejection with desktop boundaries inhibited."""

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import jsonschema

from silent_protocol_smoke import ROOT, open_private, payload, rejected, select_package
from release_check import expected_tools, fingerprint


def access_off(result):
    """Require the fixed refusal banner rather than accepting any unrelated failed tool."""
    data = payload(result)
    return (set(data) == {'ok', 'blocked', 'message'} and data['ok'] is False
            and data['blocked'] == 'access_off' and isinstance(data['message'], str))


def permission_denied(data, audit):
    """Qualify only an actual nonprompting denial and its matching plain permission error."""
    error = data.get('error')
    if (data.get('ok') is not False or not isinstance(error, str)
            or set(data) - {'ok', 'error', '_meta'}):
        return False
    expected = {'AXIsProcessTrustedWithOptions': 'klyk needs Accessibility permission',
                'CGPreflightScreenCaptureAccess': 'klyk needs Screen Recording permission'}
    return any(item.get('allowed') is False and item.get('api') in expected
               and error.startswith(expected[item['api']])
               for item in audit.get('permission_queries', []))


def budget_error(result):
    """Identify the real handler's bounded-batch rejection, which needs no native permission."""
    data = payload(result)
    return (data.get('ok') is False and 'blocked' not in data
            and str(data.get('error', '')).startswith('run supports at most 8 nested levels;'))


def wait_for(path, timeout):
    """Wait only for our child-owned marker, with a finite wall-clock deadline."""
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError('The inert child did not reach the pre-write barrier')
        time.sleep(0.005)


def main():
    """Exercise the real isolated policy, unchanged dispatcher, and both SDK writer paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.verification/silent-connection.json')
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'actual stdio access controls and final reply revocation; all active desktop boundaries inhibited',
              'desktop_acceptance': False, 'fingerprint': fingerprint(),
              'environment': {'mcp': version('mcp'), 'python': sys.version},
              'checks': [], 'completed': False, 'native_permission_ready': False,
              'native_delivery_verified': False}

    def check(name, condition):
        """Keep failed evidence explicit rather than treating a connected process as success."""
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    try:
        package_root, package = select_package(args.installed)
        report['package'] = package
        check('actual package origin and runtime match reviewed checkout', package['origin_correct']
              and package['runtime_matches_reviewed_checkout'])
        from klyk import connection_policy as policy
        from klyk.client import KlykClient
        nested = {'tool': 'wait', 'seconds': 0}
        for _ in range(9):
            nested = {'tool': 'run', 'actions': [nested]}
        invalid_budget = {'app': 'Inert Fixture', 'actions': [nested]}

        with tempfile.TemporaryDirectory(prefix='klyk-silent-connection-') as directory:
            work = Path(directory)
            command = [sys.executable, '-B', '-u', str(ROOT / 'tests/silent_protocol_bootstrap.py'),
                       str(package_root), str(work), '--access-off', '--pause-replies']
            client = KlykClient(command, timeout=20)
            with patch.object(policy, 'policy_path', return_value=work / 'connections.json'):
                try:
                    client.start()
                    child = client._proc
                    initial_pid = child.pid
                    tools = client.list_tools()
                    names = [tool['name'] for tool in tools]
                    check('Off discovers every declared tool', len(names) == len(set(names))
                          and set(names) == expected_tools())
                    for tool in tools:
                        schema = tool['inputSchema']
                        jsonschema.validators.validator_for(schema).check_schema(schema)
                    check('Off discovery retains valid actual SDK schemas', True)
                    check('Off keeps protocol ping connected', client._request('ping', {}) == {})
                    check('Off blocks metadata-only computer reads', access_off(client.call('screen_info', {})))
                    check('Off blocks valid input requests', access_off(client.call('press_key', {'app': 'Inert Fixture', 'key': 'a'})))
                    initial = json.loads((work / 'boundaries.json').read_text())
                    report['off_before_native'] = initial
                    check('Off never imports core native facilities', initial['startup_native_modules'] == []
                          and initial['loaded_native_modules'] == [])
                    check('Off never queries native permissions', initial['permission_queries'] == [])
                    policy.set_enabled('claude', True)
                    other_token = policy.token('claude')
                    check('another environment On cannot enable this connection', access_off(client.call('screen_info', {})))

                    policy.set_enabled('codex', True)
                    check('invalid enabled request fails through actual SDK', rejected(client.call('focus_window', {'app': 'Inert Fixture'})))
                    before_init = json.loads((work / 'boundaries.json').read_text())
                    check('invalid enabled request never initializes native access', before_init['loaded_native_modules'] == []
                          and before_init['permission_queries'] == [])
                    screen = payload(client.call('screen_info', {}))
                    on = json.loads((work / 'boundaries.json').read_text())
                    ready = (screen.get('main', {}).get('width', 0) > 0
                             and screen.get('main', {}).get('height', 0) > 0)
                    report['native_permission_ready'] = ready
                    report['native_initialization_result'] = screen
                    check('On honors the actual native permission result without a substituted grant',
                          ready or permission_denied(screen, on))
                    check('On retains the initialized stdio process', client._proc is child and child.pid == initial_pid and child.poll() is None)
                    check('On loads native modules only through the inhibited adapter',
                          set(on['loaded_native_modules']) == {'computer', 'capture', 'skylight', 'ocr', 'matcher'}
                          and on['native_call_guard_installed'] is True)

                    # Pause a completed real tool frame before the actual writer validates its token.
                    old_token = policy.token('codex')
                    (work / 'pause-next-tool-reply').write_text('armed')
                    replies = queue.Queue(maxsize=1)

                    def receive_late_reply():
                        """Use one owned client thread solely while its completed reply is held."""
                        try:
                            replies.put((True, client.call('run', invalid_budget)))
                        except BaseException as error:
                            replies.put((False, type(error).__name__))

                    receiver = threading.Thread(target=receive_late_reply, name='inert-reply-reader', daemon=True)
                    receiver.start()
                    try:
                        wait_for(work / 'tool-reply-ready', 4)
                        check('actual tool frame reached the external pre-write barrier', receiver.is_alive())
                        policy.set_enabled('codex', False)
                        policy.set_enabled('codex', True)
                        check('Off then On permanently revokes the original reply generation', not policy.allows(old_token))
                    finally:
                        (work / 'release-tool-reply').write_text('release')
                        receiver.join(timeout=3)
                    check('late reply thread finishes without leaking or hanging', not receiver.is_alive())
                    successful, late = replies.get_nowait()
                    check('actual SDK stdout writer suppresses revoked completed reply', successful
                          and late.get('isError') is True and access_off(late))
                    check('fresh On handler serves after old reply suppression', budget_error(client.call('run', invalid_budget)))

                    owner_path = work / 'owner'
                    owner_bytes = owner_path.read_bytes() if owner_path.is_file() else None
                    before_off = json.loads((work / 'boundaries.json').read_text())
                    policy.set_enabled('codex', False)
                    check('Off blocks take_control without changing ownership', access_off(client.call('take_control', {}))
                          and (owner_path.read_bytes() if owner_path.is_file() else None) == owner_bytes)
                    blocked_read = client.call('screen_info', {})
                    off_after_init = json.loads((work / 'boundaries.json').read_text())
                    check('Off blocks reads after runtime was initialized', access_off(blocked_read)
                          and off_after_init['permission_queries'] == before_off['permission_queries'])
                    check('Off still lists every tool after revocation', {tool['name'] for tool in client.list_tools()} == expected_tools())
                    policy.set_enabled('codex', True)
                    check('On resumes without reconnecting or replacing the process',
                          budget_error(client.call('run', invalid_budget))
                          and client._proc is child and child.pid == initial_pid)
                    check('all Codex switches preserve the other environment generation', policy.allows(other_token))
                finally:
                    client.close()
                    audit_path = work / 'boundaries.json'
                    if audit_path.is_file():
                        report['boundaries'] = json.loads(audit_path.read_text())
                audit = report['boundaries']
                check('EOF reaps the unchanged stdio child', child.returncode == 0)
                check('EOF closes every child pipe', all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr)))
                check('real production entry completes under external inhibition', audit.get('entry_completed') is True)
                check('NSApplication stays nil before and after all switches', audit.get('nsapp_before_entry_is_nil') is True
                      and audit.get('nsapp_after_entry_is_nil') is True)
                check('no actual UI input clipboard capture AX or launch boundary attempted', audit.get('blocked_attempts') == [])
                check('exactly one completed tool reply was held for final writer proof', audit.get('tool_reply_pauses') == 1)
                queries = audit.get('permission_queries', [])
                check('On records actual nonprompting permission queries with denial retained', bool(queries)
                      and {item['api'] for item in queries} <= {'AXIsProcessTrustedWithOptions', 'CGPreflightScreenCaptureAccess'}
                      and all(type(item['allowed']) is bool for item in queries)
                      and (ready or permission_denied(screen, audit)))
                check('policy logs control state and boundary evidence remain owner-only', all(
                    (work / name).stat().st_mode & 0o777 == 0o600
                    for name in ('connections.json', 'connections.lock', 'klyk.log', 'boundaries.json'))
                    and (not owner_path.exists() or owner_path.stat().st_mode & 0o777 == 0o600))
                report['completed'] = True
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        with open_private(output, 'wb') as stream:
            stream.write(json.dumps(report, indent=2).encode())
        print(json.dumps({'completed': report['completed'], 'checks': len(report['checks']),
                          'desktop_acceptance': False, 'error': report.get('error')}), flush=True)


if __name__ == '__main__':
    main()
