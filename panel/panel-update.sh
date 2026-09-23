#!/bin/bash
# Update this install to the newest release on GitHub - the panel, the scripts, the
# services and the dependencies, all through setup.sh in upgrade mode. The world, the
# backups, server.env, the panel login and the mods stay exactly as they are; the world is
# backed up first anyway. If the new panel does not answer within half a minute, the
# previous one comes back.
#
# The panel runs this on its own when a release is out and nobody is playing (Settings ->
# automatic updates), and from the "Update" button. By hand, on any install however old -
# inside the container:
#   curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/panel/panel-update.sh | bash
# or from the Proxmox host:
#   pct exec <CTID> -- bash -c "curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/panel/panel-update.sh | bash"
#
# A specific release or branch: ... | bash -s v1.20.0
set -euo pipefail
VH=/opt/valheim
REPO=PawelSzymanski89/valheim-proxmox
[ -d /opt/valheim-image ] && { echo "docker install: rebuild the image instead (docker compose up -d --build)"; exit 2; }
[ -x $VH/panel/.venv/bin/python ] || { echo "no panel in $VH - this is not a valheim-proxmox install"; exit 1; }
exec 9>/run/valheim-update.lock
flock -n 9 || { echo "another update is running"; exit 3; }

REF=${1:-$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -oP '"tag_name":\s*"\K[^"]+')}
[ -n "$REF" ] || { echo "could not ask GitHub for the latest release"; exit 1; }
echo "updating to $REF"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar xz -C "$TMP" --strip-components=1
[ -f "$TMP/setup.sh" ] && [ -f "$TMP/panel/app.py" ] || { echo "$REF has no setup.sh / panel"; exit 1; }
# a release from before this engine would run its setup.sh as a fresh install - over the settings
[ -f "$TMP/panel/VERSION" ] || { echo "$REF predates the update engine - not installing it"; exit 1; }
NEW=$(cat "$TMP/panel/VERSION"); OLD=$(cat $VH/panel/VERSION 2>/dev/null || echo v0.0.0)
if [ -z "${1:-}" ] && [ "$(printf '%s\n' "$OLD" "$NEW" | sort -V | tail -1)" != "$NEW" ]; then
  echo "installed $OLD is newer than $NEW - nothing to do"; exit 0
fi
$VH/panel/.venv/bin/python -m py_compile "$TMP/panel/app.py" "$TMP/panel/icon_badge.py"

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
SETUP_MODE=upgrade bash "$TMP/setup.sh" || { echo "setup.sh failed"; rollback; }
systemctl restart valheim-panel
for _ in $(seq 1 30); do
  sleep 1
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && { echo "updated to $REF - world and settings untouched"; exit 0; }
done
echo "the new panel did not come up"
rollback
