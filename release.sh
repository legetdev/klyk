#!/usr/bin/env bash
# Publish a verified, committed candidate through the existing PyPI Trusted Publisher.
# Usage: ./release.sh vX.Y.Z --live report.json --desktop desktop.json [--live second.json] [--notes-file notes.md] [--dry-run]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PY="${KLYK_RELEASE_PYTHON:-$HOME/.klyk/venv/bin/python}"
REPO="legetdev/klyk"
# Stop before publication when any release condition is missing.
die() { printf '%s\n' "$*" >&2; exit 1; }
VERSION="${1:-}"; [ -n "$VERSION" ] && shift || true
NOTES_FILE=""; DRY=0; EVIDENCE=(); HAS_NATIVE=0; HAS_DESKTOP=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --live) [ "$#" -ge 2 ] || die 'Missing live report'; EVIDENCE+=(--live "$2"); HAS_NATIVE=1; shift 2 ;;
    --desktop) [ "$#" -ge 2 ] || die 'Missing desktop report'; EVIDENCE+=(--desktop "$2"); HAS_DESKTOP=1; shift 2 ;;
    --notes-file) [ "$#" -ge 2 ] || die 'Missing notes file'; NOTES_FILE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Version must look like v1.2.3'
[ -x "$PY" ] || die "Python not found: $PY"
[ "$HAS_NATIVE" = 1 ] && [ "$HAS_DESKTOP" = 1 ] || die 'Fresh native and browser/Electron evidence is required: --live native.json --desktop desktop.json'
[ -z "$NOTES_FILE" ] || [ -f "$NOTES_FILE" ] || die 'Release notes file is missing'
[ "$(git branch --show-current)" = main ] || die 'Release from main'
[ -z "$(git status --porcelain)" ] || die 'Commit the reviewed candidate first (including its version bump)'
[ "$("$PY" -c 'import klyk; print(klyk.__version__)')" = "${VERSION#v}" ] || die 'Commit the requested version before live verification'
"$PY" -B -m unittest discover -s tests
"$PY" tests/release_check.py "${EVIDENCE[@]}"
gh auth status >/dev/null 2>&1 || die 'GitHub authentication is unavailable'
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die 'main must match origin/main'
! git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null || die 'Local tag already exists'
! git ls-remote --exit-code --tags origin "$VERSION" >/dev/null 2>&1 || die 'Remote tag already exists'
# Preserve previous build output instead of deleting it.
if [ -d dist ]; then
  PREVIOUS_BUILD="$(mktemp -d "${TMPDIR:-/tmp}/klyk-previous-build.XXXXXX")"
  mv dist "$PREVIOUS_BUILD/dist"
fi
"$PY" -m build
"$PY" -m twine check dist/*
"$PY" tests/release_check.py --archives "${EVIDENCE[@]}"
[ "$DRY" = 0 ] || { printf '%s\n' 'Dry run passed; nothing was tagged or published.'; exit 0; }
git tag -a "$VERSION" -m "$VERSION"
git push origin "$VERSION"
if [ -n "$NOTES_FILE" ]; then
  gh release create "$VERSION" --repo "$REPO" --title "$VERSION" --notes-file "$NOTES_FILE"
else
  gh release create "$VERSION" --repo "$REPO" --title "$VERSION" --generate-notes
fi
printf '%s\n' "$VERSION tagged; verify the Trusted Publishing workflow and public PyPI installation before declaring release complete."
gh run list --repo "$REPO" --workflow publish-pypi.yml --limit 1
