"""Real signal and EOF cleanup through the guarded entry point, with inert input callbacks."""

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from silent_protocol_smoke import open_private, select_package
from release_check import expected_tools, fingerprint


def main():
    """Drive only disposable Python children; no real input, clipboard, UI, or capture is allowed."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.verification/silent-lifecycle.json')
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'real-entry EOF/SIGTERM/SIGINT cleanup with inert held-input callbacks',
              'desktop_acceptance': False, 'fingerprint': fingerprint(),
              'environment': {'mcp': version('mcp'), 'python': sys.version},
              'checks': [], 'cases': [], 'completed': False}

    def check(name, condition):
        """Persist assertions based on child exit state and independently written callback markers."""
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    try:
        package_root, package = select_package(args.installed)
        report['package'] = package
        check('actual package origin and runtime match reviewed checkout', package['origin_correct']
              and package['runtime_matches_reviewed_checkout'])
        from klyk.client import KlykClient
        for label, signum in (('EOF', None), ('SIGTERM', signal.SIGTERM), ('SIGINT', signal.SIGINT)):
            with tempfile.TemporaryDirectory(prefix='klyk-silent-lifecycle-') as directory:
                work = Path(directory)
                command = [sys.executable, '-B', '-u', str(ROOT / 'tests/silent_protocol_bootstrap.py'),
                           str(package_root), str(work), '--inert-held-input']
                client = KlykClient(command, timeout=20)
                case = {'shutdown': label}
                report['cases'].append(case)
                try:
                    client.start()
                    child = client._proc
                    check(f'{label}: real SDK initialized', {tool['name'] for tool in client.list_tools()} == expected_tools())
                    started = time.monotonic()
                    if signum is None:
                        client.close()
                    else:
                        child.send_signal(signum)
                        child.wait(timeout=3)
                    case['shutdown_ms'] = round((time.monotonic() - started) * 1000)
                    check(f'{label}: prompt successful child exit', child.returncode == 0 and case['shutdown_ms'] < 3000)
                finally:
                    client.close()
                    markers_path = work / 'lifecycle.jsonl'
                    case['markers'] = [json.loads(line) for line in markers_path.read_text().splitlines()] if markers_path.is_file() else []
                    audit_path = work / 'boundaries.json'
                    case['boundaries'] = json.loads(audit_path.read_text()) if audit_path.is_file() else {}
                check(f'{label}: every subprocess pipe closed', all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr)))
                markers = case['markers']
                downs = [item for item in markers if item['phase'] == 'down']
                releases = [item for item in markers if item['phase'] == 'release']
                check(f'{label}: inert held inputs released exactly once',
                      sorted(item['kind'] for item in downs) == sorted(item['kind'] for item in releases) == ['keyboard', 'media', 'mouse'])
                check(f'{label}: stop engaged before every release', all(item['stop_active'] and item['held_count'] == 0 for item in releases))
                check(f'{label}: queued downs blocked during cleanup',
                      not any(item['phase'] == 'unexpected_down' for item in markers)
                      and sum(item['phase'] == 'queued_down_blocked' for item in markers) == 3)
                cleanup = [index for index, item in enumerate(markers) if item['phase'] == 'clipboard_cleanup']
                release_positions = [index for index, item in enumerate(markers) if item['phase'] == 'release']
                check(f'{label}: clipboard hook followed all input releases',
                      bool(cleanup) and min(cleanup) > max(release_positions)
                      and all(markers[index]['held_count'] == 0 and markers[index]['stop_active'] for index in cleanup))
                check(f'{label}: NSApplication remained uninitialized during cleanup', all(item['nsapp_is_nil'] for item in releases))
                check(f'{label}: no forbidden desktop boundary attempted', case['boundaries'].get('blocked_attempts') == [])
                check(f'{label}: callback evidence is owner-only', markers_path.stat().st_mode & 0o777 == 0o600)
        report['completed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        with open_private(output, 'wb') as stream:
            stream.write(json.dumps(report, indent=2).encode())
        print(json.dumps({'completed': report['completed'], 'checks': len(report['checks']),
                          'desktop_acceptance': False, 'error': report.get('error')}), flush=True)


if __name__ == '__main__':
    main()
