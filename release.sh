#!/usr/bin/env bash
#
# release.sh — one command to cut a klyk release. PyPI-only, Trusted Publishing.
#
#     ./release.sh vX.Y.Z [--notes "one-line changelog"] [--dry-run]
#
# In order:
#   1. Pre-flight: gh authed, on main, clean tree, in sync with origin, tag is new.
#   2. Bump klyk/__init__.py __version__ to match and commit "Release vX.Y.Z"
#      (skipped if it already matches — e.g. the first v0.1.0 release).
#   3. Build wheel+sdist and run `twine check` LOCALLY — catch a broken package
#      before tagging, because a PyPI version can never be re-uploaded.
#   4. Tag vX.Y.Z, push it, and cut the GitHub Release.
#   5. Publishing the Release fires .github/workflows/publish-pypi.yml, which
#      builds + uploads to PyPI over OIDC. No token anywhere.
#
# The release surface that has to live in the repo is the workflow; this script
# is the local kick-off convenience. It is NOT shipped in the pip package
# (pyproject's sdist include lists only klyk/, README, LICENSE, SECURITY).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY="$HOME/.klyk/venv/bin/python"
REPO="legetdev/klyk"
bold() { printf "\033[1m%s\033[0m\n" "$*"; }
die()  { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# ---- args ----
VERSION="${1:-}"; [ -n "$VERSION" ] && shift || true
NOTES=""; DRY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --notes)   NOTES="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$VERSION" ] || die "usage: ./release.sh vX.Y.Z [--notes \"...\"] [--dry-run]"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must look like v1.2.3"
SEMVER="${VERSION#v}"

# ---- pre-flight ----
bold "▸ Pre-flight"
[ -x "$PY" ]                     || die "klyk venv python not found at $PY"
command -v gh >/dev/null         || die "gh CLI not installed"
gh auth status >/dev/null 2>&1   || die "gh not authenticated (run: gh auth login)"
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || die "not on main"
git diff --quiet && git diff --cached --quiet     || die "working tree is dirty — commit or stash first"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "local main is not in sync with origin/main"
! git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null            || die "tag $VERSION already exists locally"
! git ls-remote --exit-code --tags origin "$VERSION" >/dev/null 2>&1   || die "tag $VERSION already exists on origin"
CUR="$("$PY" -c 'import klyk; print(klyk.__version__)')"
bold "  on main · clean · in sync · __version__=$CUR → releasing $SEMVER"

if [ "$DRY" = 1 ]; then
  bold "  [dry-run] pre-flight OK — would bump (if needed) → build → twine check → tag → release"
  exit 0
fi

# ---- bump version if needed ----
if [ "$CUR" != "$SEMVER" ]; then
  bold "▸ Bump __version__ $CUR → $SEMVER"
  "$PY" - "$SEMVER" <<'PY'
import re, sys, pathlib
v = sys.argv[1]; p = pathlib.Path("klyk/__init__.py"); s = p.read_text()
new = re.sub(r'__version__\s*=\s*["\'][^"\']+["\']', f'__version__ = "{v}"', s, count=1)
assert new != s, "no __version__ assignment found to bump"
p.write_text(new)
PY
  git add klyk/__init__.py
  git commit -q -m "Release $VERSION"
  git push -q origin main
fi

# ---- build + validate locally (before tagging) ----
bold "▸ Build + twine check"
rm -rf dist
"$PY" -m build >/dev/null
"$PY" -m twine check dist/* || die "twine check failed — fix before releasing"

# ---- tag + GitHub Release (auto-triggers the PyPI publish workflow) ----
bold "▸ Tag + GitHub Release"
git tag -a "$VERSION" -m "$VERSION"
git push -q origin "$VERSION"
if [ -n "$NOTES" ]; then
  gh release create "$VERSION" --repo "$REPO" --title "$VERSION" --notes "$NOTES"
else
  gh release create "$VERSION" --repo "$REPO" --title "$VERSION" --generate-notes
fi

bold "✓ $VERSION released — PyPI publish (OIDC) is now running:"
sleep 2
gh run list --repo "$REPO" --workflow=publish-pypi.yml --limit 1 || true
echo "  watch:  gh run watch --repo $REPO    ·    https://github.com/$REPO/actions"
