"""Real stdio protocol and permission integration with all active desktop boundaries inhibited."""

import argparse
import hashlib
from importlib.metadata import version
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from release_check import fingerprint, expected_tools

# Load only the reviewed pure-file helper so installed mode cannot import a source client first.
_private_spec = importlib.util.spec_from_file_location('silent_evidence_private_files', ROOT / 'klyk/private_files.py')
_private_module = importlib.util.module_from_spec(_private_spec)
_private_spec.loader.exec_module(_private_module)
open_private = _private_module.open_private


def select_package(installed):
    """Select the real source or installed package and compare all runtime bytes before launch."""
    if installed:
        sys.path[:] = [entry for entry in sys.path if entry and Path(entry).resolve() != ROOT]
    else:
        sys.path.insert(0, str(ROOT))
    import klyk
    package_root = Path(klyk.__file__).resolve().parents[1]

    def runtime_digest(root):
        """Bind every Python runtime file, including unexpected files, to this reviewed checkout."""
        digest = hashlib.sha256()
        for path in sorted((root / 'klyk').glob('*.py')):
            digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
        return digest.hexdigest()

    expected = runtime_digest(ROOT)
    actual = runtime_digest(package_root)
    correct_origin = (package_root.is_relative_to(Path(sys.prefix).resolve()) and package_root != ROOT
                      if installed else package_root == ROOT)
    return package_root, {'mode': 'installed' if installed else 'source', 'path': str(package_root),
                          'version': klyk.__version__, 'origin_correct': correct_origin,
                          'runtime_sha256': actual, 'reviewed_runtime_sha256': expected,
                          'runtime_matches_reviewed_checkout': actual == expected}


def payload(result):
    """Read the final structured result without accepting a transport success as tool success."""
    return json.loads(next(block['text'] for block in reversed(result.get('content', []))
                           if block.get('type') == 'text'))


def rejected(result):
    """SDKs may reject a request before dispatch with either protocol or structured errors."""
    if result.get('isError') is True:
        return True
    try:
        return payload(result).get('ok') is False
    except (ValueError, StopIteration):
        return False


def main():
    """Record the exact scope and real wire results; never call this a native desktop pass."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.verification/silent-protocol.json')
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'real-entry stdio protocol; UI, event tap, input, clipboard, app launch and capture inhibited',
              'desktop_acceptance': False, 'fingerprint': fingerprint(),
              'environment': {'mcp': version('mcp'), 'python': sys.version},
              'checks': [], 'completed': False}

    def check(name, condition):
        """Persist both successful and failed observable conditions."""
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    try:
        package_root, package = select_package(args.installed)
        report['package'] = package
        check('actual package origin and runtime match reviewed checkout', package['origin_correct']
              and package['runtime_matches_reviewed_checkout'])
        from klyk.client import KlykClient
        with tempfile.TemporaryDirectory(prefix='klyk-silent-protocol-') as directory:
            work = Path(directory)
            sentinel = 'private-protocol-sentinel-8d924d'
            child_command = [sys.executable, '-B', '-u', str(ROOT / 'tests/silent_protocol_bootstrap.py'),
                             str(package_root), str(work)]
            client = KlykClient(child_command, timeout=20)
            tool_requests_started = False
            try:
                with client:
                    child = client._proc
                    tool_requests_started = True
                    tools = client.list_tools()
                    names = [tool['name'] for tool in tools]
                    check('real SDK discovered every declared tool', len(names) == len(set(names)) and set(names) == expected_tools())
                    for tool in tools:
                        schema = tool['inputSchema']
                        jsonschema.validators.validator_for(schema).check_schema(schema)
                    check('real SDK tool schemas validate', True)
                    screen = payload(client.call('screen_info', {}))
                    check('real metadata-only display query returned dimensions', screen.get('main', {}).get('width', 0) > 0
                          and screen.get('main', {}).get('height', 0) > 0)
                    invalid = client.call('type_text', {'app': 'Silent Fixture', 'text': [sentinel]})
                    check('malformed request rejected through real SDK', rejected(invalid))
                    invalid_focus = client.call('focus_window', {'app': 'Silent Fixture'})
                    check('missing target rejected through real SDK', rejected(invalid_focus))
                    report['wire_calls'] = ['tools/list', 'screen_info', 'type_text (invalid)', 'focus_window (invalid)']
            finally:
                audit_path = work / 'boundaries.json'
                if audit_path.is_file():
                    report['boundaries'] = json.loads(audit_path.read_text())
                if not tool_requests_started:
                    # No tool request data was sent; retain the startup diagnostic tail privately.
                    report['startup_diagnostics'] = client._stderr_tail.decode('utf-8', errors='replace')
            check('EOF reaped the real server child', child.poll() is not None)
            check('EOF closed every subprocess pipe', all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr)))
            audit = json.loads((work / 'boundaries.json').read_text())
            report['boundaries'] = audit
            check('real entry completed without starting AppKit', audit.get('entry_completed') is True)
            check('real NSApplication global stayed uninitialized', audit.get('nsapp_before_entry_is_nil') is True
                  and audit.get('nsapp_after_entry_is_nil') is True)
            check('no forbidden active desktop boundary was attempted', audit.get('blocked_attempts') == [])
            required = {'AXIsProcessTrustedWithOptions', 'CGPreflightScreenCaptureAccess'}
            permissions = {item['api'] for item in audit.get('permission_queries', []) if item.get('allowed') is True}
            check('real permission preflight queries succeeded', permissions == required)
            check('persistent diagnostics exclude rejected request contents', sentinel not in (work / 'klyk.log').read_text())
            check('control state and logs are owner-only', all((work / name).stat().st_mode & 0o777 == 0o600
                  for name in ('owner', 'klyk.log', 'boundaries.json', 'connections.json', 'connections.lock')))
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
