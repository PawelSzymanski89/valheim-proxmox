#!/bin/bash
# Publish a signed release: scripts/release.sh v1.23.0 "title" notes.md [--prerelease]
#
# The tag must exist and match panel/VERSION. The archive is built from the tag with
# git archive (not GitHub's codeload tarball, whose bytes are not guaranteed to stay the
# same), signed here with the key that never leaves this machine, and uploaded with its
# .sig. Installs refuse a release without a valid .sig - see panel/panel-update.sh.
set -euo pipefail
TAG=${1:?tag}; TITLE=${2:?title}; NOTES=${3:?notes file}; PRE=${4:-}
cd "$(dirname "$0")/.."
PY=${VH_SIGN_PY:-$HOME/.config/valheim-proxmox/venv/bin/python}
[ "$(git show "$TAG:panel/VERSION" | tr -d '\n')" = "$TAG" ] || { echo "panel/VERSION at $TAG is not $TAG"; exit 1; }
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
F="$OUT/valheim-proxmox-$TAG.tar.gz"
git archive --prefix="valheim-proxmox-$TAG/" "$TAG" | gzip -n >"$F"
"$PY" scripts/sign-release.py "$F"
"$PY" scripts/sign-release.py --verify "$F"
gh release create "$TAG" --title "$TITLE" --notes-file "$NOTES" --verify-tag ${PRE:+--prerelease} "$F" "$F.sig"
