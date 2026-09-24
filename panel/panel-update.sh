#!/bin/bash
# Update this install to the newest release on GitHub - the panel, the scripts, the
# services and the dependencies, all through setup.sh in upgrade mode. The world, the
# backups, server.env, the panel login and the mods stay exactly as they are; the world is
# backed up first anyway. If the new panel does not answer within half a minute, the
# previous one comes back.
#
# Only a SIGNED release is installed. Each release carries valheim-proxmox-<tag>.tar.gz and
# its .sig, made on the maintainer's machine with a key that is not on GitHub; the public
# half is below. A stolen GitHub account can publish a release, but no install takes it.
#
# The panel runs this on its own when a release is out and nobody is playing (Settings ->
# automatic updates), and from the "Update" button. By hand, on any install however old -
# inside the container:
#   curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/panel/panel-update.sh | bash
# or from the Proxmox host:
#   pct exec <CTID> -- bash -c "curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/panel/panel-update.sh | bash"
#
# A specific release: ... | bash -s v1.23.0
# An unsigned branch or commit, for testing only: ALLOW_UNSIGNED=1 ... | bash -s <ref>
set -euo pipefail

# Everything is inside main(), called on the last line: piped from curl, a connection that
# drops half way would otherwise hand bash a cut-off script - "rm -rf $VH/panel.prev" cut
# after "$VH" is "rm -rf /opt/valheim".
main() {
VH=/opt/valheim
REPO=PawelSzymanski89/valheim-proxmox
RELEASE_KEY=WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0=
[ -d /opt/valheim-image ] && { echo "docker install: rebuild the image instead (docker compose up -d --build)"; exit 2; }
[ -x $VH/panel/.venv/bin/python ] || { echo "no panel in $VH - this is not a valheim-proxmox install"; exit 1; }
exec 9>/run/valheim-update.lock
flock -n 9 || { echo "another update is running"; exit 3; }

REF=${1:-$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -oP '"tag_name":\s*"\K[^"]+')}
[ -n "$REF" ] || { echo "could not ask GitHub for the latest release"; exit 1; }
[[ "$REF" =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || { echo "odd release name: $REF"; exit 1; }
echo "updating to $REF"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ASSET="https://github.com/$REPO/releases/download/$REF/valheim-proxmox-$REF.tar.gz"
if curl -fsSL -o "$TMP/release.tar.gz" "$ASSET" && curl -fsSL -o "$TMP/release.sig" "$ASSET.sig"; then
  # openssl, not Python: it is on every Debian and in the Docker image, and an old install's
  # panel venv may not have the crypto library yet
  { echo "-----BEGIN PUBLIC KEY-----"
    { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; echo "$RELEASE_KEY" | base64 -d; } | base64
    echo "-----END PUBLIC KEY-----"; } >"$TMP/key.pem"
  base64 -d "$TMP/release.sig" >"$TMP/release.sig.bin" 2>/dev/null || { echo "$REF: the signature file is damaged - not installing"; exit 1; }
  openssl pkeyutl -verify -pubin -inkey "$TMP/key.pem" -rawin -in "$TMP/release.tar.gz" \
    -sigfile "$TMP/release.sig.bin" >/dev/null 2>&1 \
    || { echo "$REF: the signature does not match - not installing (tampered or damaged download)"; exit 1; }
  echo "signature verified"
  mkdir "$TMP/src" && tar xzf "$TMP/release.tar.gz" -C "$TMP/src" --strip-components=1
elif [ "${ALLOW_UNSIGNED:-}" = 1 ]; then
  echo "WARNING: $REF has no signed release - installing unsigned because ALLOW_UNSIGNED=1"
  mkdir "$TMP/src"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar xz -C "$TMP/src" --strip-components=1
else
  echo "$REF has no signed release files - not installing"
  exit 1
fi
TMP_SRC="$TMP/src"
[ -f "$TMP_SRC/setup.sh" ] && [ -f "$TMP_SRC/panel/app.py" ] || { echo "$REF has no setup.sh / panel"; exit 1; }
# a release from before this engine would run its setup.sh as a fresh install - over the settings
[ -f "$TMP_SRC/panel/VERSION" ] || { echo "$REF predates the update engine - not installing it"; exit 1; }
NEW=$(cat "$TMP_SRC/panel/VERSION"); OLD=$(cat $VH/panel/VERSION 2>/dev/null || echo v0.0.0)
num() { local n; n=$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' <<<"$1" | head -1); echo "${n:-0.0.0}"; }   # "test-v1.24.0-rc2" -> 1.24.0
if [ -z "${1:-}" ] && [ "$(printf '%s\n' "$(num "$OLD")" "$(num "$NEW")" | sort -V | tail -1)" != "$(num "$NEW")" ]; then
  echo "installed $OLD is newer than $NEW - nothing to do"; exit 0
fi
$VH/panel/.venv/bin/python -m py_compile "$TMP_SRC/panel/app.py" "$TMP_SRC/panel/icon_badge.py"

[ -x $VH/backup.sh ] && runuser -u valheim -- $VH/backup.sh || echo "backup skipped"

PORT=$(grep -oP "PANEL_PORT='\K[^']*" $VH/panel.env 2>/dev/null || echo 2460)
rm -rf $VH/panel.prev && mkdir -p $VH/panel.prev
cp -a $VH/panel/. $VH/panel.prev/ 2>/dev/null || true
rm -rf $VH/panel.prev/.venv $VH/panel.prev/__pycache__
cp -a $VH/panel.version $VH/panel.prev/ 2>/dev/null || true

rollback() {
  echo "rolling back to $(cut -d' ' -f1 $VH/panel.prev/panel.version 2>/dev/null || echo the previous panel)"
  cp -a $VH/panel.prev/. $VH/panel/
  rm -f $VH/panel/panel.version
  echo "$(cut -d' ' -f1 $VH/panel.prev/panel.version 2>/dev/null || echo unknown) $(date -u +%FT%TZ) rollback" >$VH/panel.version
  systemctl restart valheim-panel
  exit 1
}
SETUP_MODE=upgrade bash "$TMP_SRC/setup.sh" || { echo "setup.sh failed"; rollback; }
systemctl restart valheim-panel
for _ in $(seq 1 30); do
  sleep 1
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && { echo "updated to $REF - world and settings untouched"; exit 0; }
done
echo "the new panel did not come up"
rollback
}

main "$@"
