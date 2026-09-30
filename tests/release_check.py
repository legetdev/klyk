"""Verify release contents and fresh evidence proportionate to the changed behavior."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {'roadmap.md', 'polarstar.md', 'claude.md', 'publish.md', 'limitations_review.md', '.env'}
PRIVATE_DIRECTORIES = {'.verification', '__pycache__', '.claude', '.codex', '.git'}

# These are independently observed outcomes from the native suite, not just tool names.
NATIVE_CHECKS = frozenset({
    'fixture started', '48 tools discovered', 'all 48 real SDK schemas validate', 'app-only focus rejected',
    'semantic click changed native counter', 'duplicate AX labels click nothing',
    'explicit duplicate index clicks once', 'click delivered native input', 'double_click delivered native input',
    'triple_click delivered native input', 'long_press delivered native input',
    'AX action delivered', 'AX field value changed', 'keyboard input changed field',
    'held key released', 'paste input changed field', 'clipboard round trip',
    'grid observation returned', 'template matches current native control',
    'visual readiness found', 'screenshot saved', 'popup choice observed',
    'native menu changed counter', 'context menu changed counter',
    'native slider drag changed state', 'semantic drag delivered payload',
    'scroll moved native document', 'explicit window moved',
    'native save produced expected file', 'native open read exact saved contents',
    'missing save panel sends no input', 'system mute toggled',
    'failed batch stops dependent input', 'verdict discloses unverified evidence',
    'receiver started', 'background native click delivered',
    'background native click preserved focus and cursor',
    'background visible input refused', 'cross-app drop independently received',
    'non-owner input blocked', 'dead owner recovered',
    'closed-window animation finished', 'selected window closed and sibling survived',
    'stale window refused without closing app', 'close_app closed fixture',
    'second fixture started', 'close_apps closed fixture', 'all 48 tools exercised',
})
DESKTOP_CHECKS = frozenset({
    'browser fixture loaded', 'one isolated browser window identified',
    'browser fixture observed', 'background Chromium click refused',
    'Chromium click delivered once', 'Chromium keys delivered',
    'Chromium paste delivered', 'isolated Electron window exists',
    'Electron PID matches isolated profile', 'Electron document matches fixture before input',
    'Electron editor saved exact Unicode text',
})


def fingerprint():
    """Bind a live report to the runtime, public documentation, tests, and release controls."""
    paths = [*ROOT.glob('klyk/*.py'), *ROOT.glob('tests/*.py'), *ROOT.glob('tests/*.swift'), *ROOT.glob('tests/*.html'), *ROOT.glob('tests/*.md'),
             *ROOT.glob('.github/workflows/*.yml')]
    paths += [ROOT / name for name in ('.gitignore', 'pyproject.toml', 'requirements.txt', 'README.md', 'SECURITY.md', 'ARCHITECTURE.md', 'LICENSE', 'release.sh')]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def check_names(names):
    """Reject private planning, captured evidence, caches, and secrets at publication boundaries."""
    for name in names:
        if not isinstance(name, str) or not name or '\0' in name:
            raise ValueError('Invalid path in publication')
        normalized = name.replace('\\', '/')
        parts = tuple(part.casefold() for part in normalized.split('/'))
        if normalized.startswith('/') or re.match(r'^[a-zA-Z]:', normalized) or '..' in parts:
            raise ValueError(f'Unsafe path in publication: {name}')
        if any(part in PRIVATE or part in PRIVATE_DIRECTORIES for part in parts):
            raise ValueError(f'Private or generated content in publication: {name}')
        if normalized.casefold().endswith(('.pyc', '.log')):
            raise ValueError(f'Generated content in publication: {name}')
        if any(part.startswith('.env.') and part != '.env.example' for part in parts):
            raise ValueError(f'Environment secrets in publication: {name}')


def expected_tools():
    """Read declared tool names without importing a module that operates the desktop."""
    tree = ast.parse((ROOT / 'klyk/mcp_server.py').read_text())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if not any(isinstance(target, ast.Name) and target.id == 'TOOLS' for target in targets):
            continue
        if not isinstance(node.value, ast.List):
            break
        names = []
        for item in node.value.elts:
            if not isinstance(item, ast.Call):
                raise ValueError('The tool declaration cannot be verified statically')
            name = next((argument.value for argument in item.keywords if argument.arg == 'name'), None)
            if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                raise ValueError('A tool declaration has no literal name')
            names.append(name.value)
        if not names or len(names) != len(set(names)):
            raise ValueError('The declared tool names are empty or duplicated')
        return set(names)
    raise ValueError('The tool declaration cannot be verified statically')


def validate_report(report, path, candidate):
    """Require fresh, completed evidence with real boolean assertions and readable names."""
    if not isinstance(report, dict) or report.get('fingerprint') != candidate:
        raise ValueError(f'Evidence is stale or malformed: {path}; rerun the applicable checks')
    if report.get('completed') is not True or report.get('error') not in (None, ''):
        raise ValueError(f'Candidate evidence is incomplete or failed: {path}')
    checks = report.get('checks')
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, dict) or check.get('passed') is not True
        or not isinstance(check.get('name'), str) or not check['name'].strip()
        for check in checks
    ):
        raise ValueError(f'Candidate evidence has missing or invalid checks: {path}')
    return {check['name'] for check in checks}


def sdk_major(report, path):
    """Require a recorded supported MCP version instead of guessing from the filename."""
    environment = report.get('environment')
    value = environment.get('mcp') if isinstance(environment, dict) else None
    match = re.fullmatch(r'([12])\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)', value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f'Evidence needs a supported MCP version in environment.mcp: {path}')
    return int(match.group(1))


def validate_full_report(report, path, check_names, *, native):
    """Full acceptance needs the promised independent workflows and authoritative tool coverage."""
    required = NATIVE_CHECKS if native else DESKTOP_CHECKS
    missing = required - check_names
    if missing:
        raise ValueError(f'Independent acceptance checks are missing: {path}: {sorted(missing)}')
    calls = report.get('calls')
    if not isinstance(calls, list) or not calls or any(
        not isinstance(call, dict) or not isinstance(call.get('tool'), str) for call in calls
    ):
        raise ValueError(f'Actual tool calls are missing or malformed: {path}')
    declared = expected_tools()
    called = {call['tool'] for call in calls}
    if not called <= declared:
        raise ValueError(f'Evidence contains unknown tool calls: {path}')
    if native:
        tools = report.get('tools')
        if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
            raise ValueError(f'Native tool discovery is missing or malformed: {path}')
        if len(tools) != len(declared) or set(tools) != declared or called != declared:
            raise ValueError(f'Native evidence must discover and exercise every declared tool: {path}')
    return sdk_major(report, path)


def main():
    """Check the Git index, built archives, and optional live reports before a release."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--archives', action='store_true')
    parser.add_argument('--live', action='append', default=[])
    parser.add_argument('--desktop', action='append', default=[])
    parser.add_argument('--targeted', action='append', default=[])
    args = parser.parse_args()
    if args.targeted and (args.live or args.desktop):
        raise ValueError('Choose targeted or full verification, not both')
    if bool(args.live) != bool(args.desktop):
        raise ValueError('Full verification needs native evidence for both MCP majors and Chrome/Electron evidence')
    # Git display quoting can hide a private prefix in non-ASCII or newline-containing names.
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT)
    if tracked and not tracked.endswith(b'\0'):
        raise ValueError('Git returned malformed filename records')
    check_names(tracked.decode('utf-8', errors='surrogateescape').split('\0')[:-1])
    if args.archives:
        archives = [*ROOT.glob('dist/*.whl'), *ROOT.glob('dist/*.tar.gz')]
        if len(archives) != 2:
            raise ValueError('Expected exactly one wheel and one source archive in dist/')
        for archive in archives:
            if archive.suffix == '.whl':
                with zipfile.ZipFile(archive) as bundle: names = bundle.namelist()
            else:
                with tarfile.open(archive) as bundle: names = bundle.getnames()
            check_names(names)
            print(f'Archive privacy: {archive.name} ({len(names)} entries)')
    candidate = fingerprint()
    native_majors = set()
    for kind, paths in (('native', args.live), ('desktop', args.desktop), ('targeted', args.targeted)):
        for path in paths:
            report = json.loads(Path(path).read_text())
            checks = validate_report(report, path, candidate)
            if kind == 'targeted':
                if (report.get('scope') != 'minor' or not isinstance(report.get('rationale'), str)
                        or not report['rationale'].strip()):
                    raise ValueError(f'Targeted evidence needs scope=minor and a change-specific rationale: {path}')
            else:
                major = validate_full_report(report, path, checks, native=kind == 'native')
                if kind == 'native':
                    native_majors.add(major)
            print(f'Candidate evidence verified: {path}')
    if args.live and native_majors != {1, 2}:
        raise ValueError('Full native evidence must cover both supported MCP major versions, 1 and 2')
    print('Publication checks passed')


if __name__ == '__main__':
    main()
