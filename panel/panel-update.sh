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
# The release keys: the working one and a backup kept offline. A release signed by either is
# accepted. A release may carry release-keys.txt (+ .sig, signed by a key trusted now): from
# then on that list replaces these two - how a lost or leaked key is swapped out without a
# manual update anywhere. The list in use is kept in $VH/release-keys.
BUILTIN_KEYS="WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0= 649uL/TAv45znSgfclQMBTS3IhUV45Fh3ax2vsYaRDA="
trusted() { if [ -s "$VH/release-keys" ]; then grep -v '^#' "$VH/release-keys" | grep .; else printf '%s\n' $BUILTIN_KEYS; fi; }
verify() {  # verify FILE SIGFILE(base64) -> 0 when any trusted key signed it
  local k
  base64 -d "$2" >"$2.bin" 2>/dev/null || return 1
  for k in $(trusted); do
    { echo "-----BEGIN PUBLIC KEY-----"
      { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; echo "$k" | base64 -d; } | base64
      echo "-----END PUBLIC KEY-----"; } >"$2.pem" 2>/dev/null || continue
    openssl pkeyutl -verify -pubin -inkey "$2.pem" -rawin -in "$1" -sigfile "$2.bin" >/dev/null 2>&1 && return 0
  done
  return 1
}
[ -d /opt/valheim-image ] && { echo "docker install: rebuild the image instead (docker compose up -d --build)"; exit 2; }
[ -x $VH/panel/.venv/bin/python ] || { echo "no panel in $VH - this is not a valheim-proxmox install"; exit 1; }
exec 9>/run/valheim-update.lock
flock -n 9 || { echo "another update is running"; exit 3; }

# The outcome goes where the panel reads it: "<tag> <ok|rollback|refused|network> <epoch>".
# "network" means GitHub was not reached - the panel tries that release again later, where
# every other outcome is final for it. (A DNS hiccup used to read as "no signed release"
# and left the release uninstalled for good.)
result() { echo "${REF:-unknown} $1 $(date +%s)" >"$VH/update-result"; }
fetch() {  # fetch URL FILE -> 0 ok, 22 not there (HTTP error), anything else: network trouble
  local rc=0; curl -fsSL --retry 3 --retry-delay 5 -o "$2" "$1" || rc=$?; return $rc
}

latest=$(curl -fsSL --retry 3 --retry-delay 5 "https://api.github.com/repos/$REPO/releases/latest") \
  || { [ -n "${1:-}" ] || { REF=unknown; result network; echo "GitHub could not be reached"; exit 4; }; }
REF=${1:-$(grep -oP '"tag_name":\s*"\K[^"]+' <<<"$latest")}
[ -n "$REF" ] || { REF=unknown; result network; echo "could not ask GitHub for the latest release"; exit 4; }
[[ "$REF" =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || { echo "odd release name: $REF"; exit 1; }
echo "updating to $REF"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ASSET="https://github.com/$REPO/releases/download/$REF/valheim-proxmox-$REF.tar.gz"
rc=0; fetch "$ASSET" "$TMP/release.tar.gz" || rc=$?
[ $rc -eq 0 ] && { fetch "$ASSET.sig" "$TMP/release.sig" || rc=$?; }
if [ $rc -ne 0 ] && [ $rc -ne 22 ]; then
  result network; echo "$REF: GitHub could not be reached (curl $rc) - trying again later"; exit 4
fi
if [ $rc -eq 0 ]; then
  # openssl, not Python: it is on every Debian and in the Docker image, and an old install's
  # panel venv may not have the crypto library yet
  verify "$TMP/release.tar.gz" "$TMP/release.sig" \
    || { result refused; echo "$REF: the signature does not match - not installing (tampered or damaged download)"; exit 1; }
  echo "signature verified"
  # a new key list, if this release carries one signed by a key trusted now
  if fetch "${ASSET%/*}/release-keys.txt" "$TMP/keys" && fetch "${ASSET%/*}/release-keys.txt.sig" "$TMP/keys.sig"; then
    if verify "$TMP/keys" "$TMP/keys.sig" \
       && [ "$(grep -v '^#' "$TMP/keys" | grep -c .)" -ge 1 ] \
       && ! grep -v '^#' "$TMP/keys" | grep . | grep -qvE '^[A-Za-z0-9+/]{43}=$'; then
      install -m 600 "$TMP/keys" "$VH/release-keys"
      echo "release keys updated: $(grep -v '^#' "$VH/release-keys" | grep -c .) key(s) trusted from now on"
    else
      echo "WARNING: $REF carries a key list that is not signed by a trusted key - ignored"
    fi
  fi
  mkdir "$TMP/src" && tar xzf "$TMP/release.tar.gz" -C "$TMP/src" --strip-components=1
elif [ "${ALLOW_UNSIGNED:-}" = 1 ] && [ -n "${TEST_SRC_DIR:-}" ]; then
  # CI: a local tree instead of a download (the deliberately broken release of the upgrade test)
  echo "WARNING: installing the local tree $TEST_SRC_DIR, unsigned (ALLOW_UNSIGNED=1)"
  mkdir "$TMP/src" && cp -a "$TEST_SRC_DIR/." "$TMP/src/"
elif [ "${ALLOW_UNSIGNED:-}" = 1 ]; then
  echo "WARNING: $REF has no signed release - installing unsigned because ALLOW_UNSIGNED=1"
  mkdir "$TMP/src"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar xz -C "$TMP/src" --strip-components=1
else
  result refused; echo "$REF has no signed release files - not installing"
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
# The whole of what setup.sh may change, set aside before it runs: the panel with its venv,
# the scripts, the systemd units and whether each is enabled. A rollback used to restore the
# panel files alone - the new release's units, scripts and packages stayed, and the old
# panel could be left running on dependencies it was never built for.
SNAP=$VH/rollback
UNITS="valheim.service valheim-panel.service valheim-backup.service valheim-backup.timer valheim-update.service valheim-update.timer"
rm -rf "$SNAP" "$VH/panel.prev" && mkdir -p "$SNAP/panel" "$SNAP/scripts" "$SNAP/units" && chmod 700 "$SNAP"
cp -a $VH/panel/. "$SNAP/panel/"
rm -rf "$SNAP/panel/__pycache__"
for f in start.sh backup.sh update.sh rcon-save.py panel-passwd.sh panel-update.sh panel.version; do
  [ -e "$VH/$f" ] && cp -a "$VH/$f" "$SNAP/scripts/"
done
for u in $UNITS; do
  [ -f "/etc/systemd/system/$u" ] && cp -a "/etc/systemd/system/$u" "$SNAP/units/"
  echo "$u $(systemctl is-enabled "$u" 2>/dev/null || echo missing)"
done >"$SNAP/enabled"

rollback() {
  result rollback
  echo "rolling back to $(cut -d' ' -f1 "$SNAP/scripts/panel.version" 2>/dev/null || echo the previous panel)"
  rm -rf "$VH/panel.failed" && mv "$VH/panel" "$VH/panel.failed"
  cp -a "$SNAP/panel" "$VH/panel"
  cp -a "$SNAP/scripts/." "$VH/"
  # units: the old files back, anything the new release added removed, each unit enabled or
  # not exactly as before; the game's own running state is left alone
  for u in $UNITS; do
    if [ -f "$SNAP/units/$u" ]; then cp -a "$SNAP/units/$u" "/etc/systemd/system/$u"
    else rm -f "/etc/systemd/system/$u"; fi
  done
  systemctl daemon-reload
  while read -r u state; do
    case "$state" in
      enabled) case "$u" in *.timer) systemctl enable --now "$u" ;; *) systemctl enable "$u" ;; esac ;;
      disabled) case "$u" in *.timer) systemctl disable --now "$u" ;; *) systemctl disable "$u" ;; esac ;;
    esac >/dev/null 2>&1 || true
  done <"$SNAP/enabled"
  echo "$(cut -d' ' -f1 "$SNAP/scripts/panel.version" 2>/dev/null || echo unknown) $(date -u +%FT%TZ) rollback" >$VH/panel.version
  systemctl restart valheim-panel
  rm -rf "$VH/panel.failed"
  exit 1
}
SETUP_MODE=upgrade bash "$TMP_SRC/setup.sh" || { echo "setup.sh failed"; rollback; }
systemctl restart valheim-panel
# Kept only once the panel says it works (/api/health: settings, status, a pass of the
# background loop, state files, signing) - "the login page answers" let through a panel
# that was broken everywhere past the login. A panel from before v1.26.0 has no health
# route; for that one, the old test.
for _ in $(seq 1 180); do
  sleep 1
  h=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" || true)
  [ "$h" = 200 ] && { result ok; echo "updated to $REF - health checks pass, world and settings untouched"; exit 0; }
  [ "$h" = 404 ] && curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && { result ok; echo "updated to $REF (a panel without health checks)"; exit 0; }
done
echo "the new panel did not pass its health checks:"
curl -s "http://127.0.0.1:$PORT/api/health" || true
echo
rollback
}

main "$@"
