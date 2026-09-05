"""Verify release contents and require fresh, private real-Mac evidence locally."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {'roadmap.md', 'polarstar.md', 'claude.md', 'publish.md', 'limitations_review.md', '.env'}


def fingerprint():
    """Bind a live report to the runtime, public documentation, tests, and release controls."""
    paths = [*ROOT.glob('klyk/*.py'), *ROOT.glob('tests/*.py'), *ROOT.glob('tests/*.swift'), *ROOT.glob('tests/*.html'), *ROOT.glob('tests/*.md'),
             *ROOT.glob('.github/workflows/*.yml')]
    paths += [ROOT / name for name in ('.gitignore', 'pyproject.toml', 'README.md', 'SECURITY.md', 'ARCHITECTURE.md', 'LICENSE', 'release.sh')]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def check_names(names):
    """Reject private planning, captured evidence, caches, and secrets at publication boundaries."""
    for name in names:
        parts = Path(name).parts
        if any(part.lower() in PRIVATE or part in {'.verification', '__pycache__', '.claude', '.codex'} for part in parts):
            raise ValueError(f'Private or generated content in publication: {name}')
        if name.endswith(('.pyc', '.log')):
            raise ValueError(f'Generated content in publication: {name}')


def main():
    """Check the Git index, built archives, and optional live reports before a release."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--archives', action='store_true')
    parser.add_argument('--live', action='append', default=[])
    parser.add_argument('--desktop', action='append', default=[])
    args = parser.parse_args()
    check_names(subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines())
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
    for path in args.live + args.desktop:
        report = json.loads(Path(path).read_text())
        if report.get('fingerprint') != fingerprint():
            raise ValueError(f'Live evidence is stale: {path}; rerun tests/live_smoke.py')
        if not report.get('completed') or report.get('error') or not all(c['passed'] for c in report['checks']):
            raise ValueError(f'Live evidence is incomplete or failed: {path}')
        if path in args.live and set(report['tools']) != {c['tool'] for c in report['calls']}:
            raise ValueError(f'Live tool coverage is incomplete: {path}')
        print(f'Live candidate verified: {path}')
    print('Publication checks passed')


if __name__ == '__main__':
    main()
