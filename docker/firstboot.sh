#!/bin/bash
# Every boot: the volume gets the current panel and scripts from the image, and keeps
# everything else it already has (worlds, backups, mods, settings, the panel login).
# First boot only: panel.env and server.env from the container environment, and the
# game download from Steam.
set -euo pipefail
IMG=/opt/valheim-image
VH=/opt/valheim
APPID=896660
randstr() { local s; s=$(head -c 48 /dev/urandom | base64 | tr -dc "$1"); echo "${s:0:$2}"; }

mkdir -p "$VH"
# -n: never overwrite what the volume already has (state, worlds, mods, a downloaded game)
cp -an "$IMG/." "$VH/"
# ...except the code, which follows the image
cp -a "$IMG/panel/." "$VH/panel/"
cp -a "$IMG"/{start.sh,backup.sh,update.sh,rcon-save.py,panel-passwd.sh,panel-update.sh} "$VH/"

if [ ! -f "$VH/panel.env" ]; then
  cat >"$VH/panel.env" <<ENV
PANEL_USER='${PANEL_USER:-admin}'
PANEL_PASS='${PANEL_PASS:-valheim123}'
PANEL_PORT='${PANEL_PORT:-2460}'
NTFY_SERVER='${NTFY_SERVER:-https://ntfy.sh}'
NTFY_TOPIC='${NTFY_TOPIC:-valheim-$(randstr 'a-z0-9' 10)}'
ENV
  chmod 600 "$VH/panel.env"
fi
if [ ! -f "$VH/server.env.configured" ]; then
  cat >"$VH/server.env" <<ENV
NAME='${SERVER_NAME:-Valheim}'
WORLD='${WORLD_NAME:-Dedicated}'
PASSWORD='${SERVER_PASS:-valheim123}'
PORT='${GAME_PORT:-2456}'
PUBLIC='0'
CROSSPLAY='0'
PRESET=''
MODIFIERS=''
SETKEYS=''
ENV
  touch "$VH/server.env.configured"
fi
for f in adminlist bannedlist permittedlist; do
  [ -f "$VH/data/$f.txt" ] || echo "// one player id per line" >"$VH/data/$f.txt"
done
# same split as lock_down() in setup.sh: root owns what root runs, the game user what it writes
chown root:root "$VH"; chmod 755 "$VH"
for d in steamcmd server data backups .steam Steam .config .local .cache .mono; do
  mkdir -p "$VH/$d"; chown -hR valheim:valheim "$VH/$d"
done
chown -hR root:root "$VH/panel"; chmod 700 "$VH/panel"
chown -h root:root "$VH"/{start.sh,backup.sh,update.sh,rcon-save.py,panel-passwd.sh,panel-update.sh}
chown -h root:valheim "$VH/server.env"; chmod 640 "$VH/server.env"
chown -h root:root "$VH/panel.env" 2>/dev/null; chmod 600 "$VH/panel.env" 2>/dev/null || true

if [ ! -x "$VH/server/valheim_server.x86_64" ]; then
  echo "first boot: downloading the Valheim server from Steam (~1.5 GB)"
  runuser -u valheim -- env HOME="$VH" "$VH/steamcmd/steamcmd.sh" +login anonymous +quit >"$VH/steam-install.log" 2>&1 || true
  runuser -u valheim -- env HOME="$VH" "$VH/steamcmd/steamcmd.sh" +force_install_dir "$VH/server" \
    +login anonymous +app_update $APPID validate +quit >>"$VH/steam-install.log" 2>&1
  [ -x "$VH/server/valheim_server.x86_64" ] || { echo "Steam download failed - see $VH/steam-install.log"; exit 1; }
fi
echo "ready: panel on port $(grep -oP "PANEL_PORT='\K[0-9]+" "$VH/panel.env"), game on $(grep -oP "PORT='\K[0-9]+" "$VH/server.env")/udp"
