#!/usr/bin/env bash
# Installs the Valheim dedicated server + admin panel inside a Debian 12 system.
# Called by install.sh inside a fresh LXC, but it also runs fine on its own on any
# Debian 12 box: bash setup.sh
set -euo pipefail

VH_DIR=${VH_DIR:-/opt/valheim}
PANEL_PORT=${PANEL_PORT:-2460}
GAME_PORT=${GAME_PORT:-2456}
SERVER_NAME=${SERVER_NAME:-Valheim}
WORLD_NAME=${WORLD_NAME:-Dedicated}
SERVER_PASS=${SERVER_PASS:-valheim123}
REPO_RAW=${REPO_RAW:-https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main}
APPID=896660

STEP=0; TOTAL=5
say()  { STEP=$((STEP + 1)); echo -e "\033[1;32m[$STEP/$TOTAL]\033[0m $*"; }
info() { echo "      $*"; }
die()  { echo -e "\033[1;31mError:\033[0m $*" >&2; exit 1; }
trap 'echo -e "\033[1;31mSetup failed at line $LINENO\033[0m" >&2' ERR

# No `| head -c N` here: head closing the pipe kills the writer with SIGPIPE, and with
# `set -o pipefail` that aborts the whole script before it prints anything.
randstr() { local s; s=$(head -c 48 /dev/urandom | base64 | tr -dc "$1"); echo "${s:0:$2}"; }

say "Installing packages (32-bit Steam libs, Python)"
export DEBIAN_FRONTEND=noninteractive
# ssh/pct hand us the caller's LANG and LC_*, which the fresh container has no locales
# for — that alone produces a screen of perl and apt-listchanges warnings.
export LANG=C.UTF-8 LC_ALL=C.UTF-8
dpkg --add-architecture i386
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl tar gzip procps \
  lib32gcc-s1 libsdl2-2.0-0:i386 libatomic1 \
  python3 python3-venv python3-pip >/dev/null
info "done"

id -u valheim >/dev/null 2>&1 || useradd -m -d "$VH_DIR" -s /bin/bash valheim
mkdir -p "$VH_DIR"/{steamcmd,server,data/worlds_local,backups,panel}
chown -R valheim:valheim "$VH_DIR"

say "Fetching SteamCMD"
# runuser, not sudo — sudo is not in the stock Debian container image
runuser -u valheim -- env HOME="$VH_DIR" bash -c "cd $VH_DIR/steamcmd && curl -sqL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar zxf -"
info "done"

say "Downloading the Valheim server (~1.5 GB, this is the slow part)"
# First run of steamcmd updates steamcmd itself and re-executes, dropping whatever else
# was on the command line — the app_update then dies with "Missing configuration".
# So: one warm-up login, then the real download.
runuser -u valheim -- env HOME="$VH_DIR" "$VH_DIR/steamcmd/steamcmd.sh" \
  +login anonymous +quit >"$VH_DIR/steam-install.log" 2>&1 || true
set +e
runuser -u valheim -- env HOME="$VH_DIR" "$VH_DIR/steamcmd/steamcmd.sh" +force_install_dir "$VH_DIR/server" \
  +login anonymous +app_update $APPID validate +quit 2>&1 \
  | tee -a "$VH_DIR/steam-install.log" | tr '\r' '\n' \
  | grep --line-buffered -E 'Update state|Success! App|ERROR!' | sed -u 's/^/      /'
set -e
[ -x "$VH_DIR/server/valheim_server.x86_64" ] || die "Steam download failed — see $VH_DIR/steam-install.log"

# ---------- launch config ----------
# Settings live here, not in start.sh, so the panel has something to edit.
cat >"$VH_DIR/server.env" <<EOF
NAME='$SERVER_NAME'
WORLD='$WORLD_NAME'
PASSWORD='$SERVER_PASS'
PORT='$GAME_PORT'
PUBLIC='0'
CROSSPLAY='1'
PRESET=''
MODIFIERS=''
SETKEYS=''
EOF

cat >"$VH_DIR/start.sh" <<'EOF'
#!/bin/bash
# Settings come from server.env (edited by the panel). This only assembles arguments.
export LD_LIBRARY_PATH="/opt/valheim/server/linux64:$LD_LIBRARY_PATH"
export SteamAppId=892970
NAME=Valheim; WORLD=Dedicated; PASSWORD=; PORT=2456; PUBLIC=0; CROSSPLAY=1; PRESET=; MODIFIERS=; SETKEYS=
[ -r /opt/valheim/server.env ] && . /opt/valheim/server.env
cd /opt/valheim/server

ARGS=(-nographics -batchmode -name "$NAME" -port "$PORT" -world "$WORLD" -savedir /opt/valheim/data -public "$PUBLIC")
[ -n "$PASSWORD" ] && ARGS+=(-password "$PASSWORD")
[ "$CROSSPLAY" = "1" ] && ARGS+=(-crossplay)
# preset before modifiers — a preset sets everything, a single modifier overrides it
[ -n "$PRESET" ] && ARGS+=(-preset "$PRESET")
for m in $MODIFIERS; do ARGS+=(-modifier "${m%%:*}" "${m#*:}"); done
for k in $SETKEYS; do ARGS+=(-setkey "$k"); done

echo "start: ${ARGS[*]//$PASSWORD/***}"
exec ./valheim_server.x86_64 "${ARGS[@]}"
EOF

cat >"$VH_DIR/backup.sh" <<'EOF'
#!/bin/bash
SRC=/opt/valheim/data/worlds_local; DST=/opt/valheim/backups
[ -d "$SRC" ] || exit 0
ts=$(date +%Y%m%d-%H%M%S)
tar czf "$DST/world-$ts.tar.gz" -C "$SRC" . 2>/dev/null && echo "backup world-$ts.tar.gz"
ls -1t "$DST"/world-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
EOF

cat >"$VH_DIR/update.sh" <<'EOF'
#!/bin/bash
# Only restarts when Steam actually has a newer build — a blind restart would kick
# players for nothing.
APP=896660
MANIFEST=/opt/valheim/server/steamapps/appmanifest_$APP.acf
installed=$(awk -F\" "/\"buildid\"/{print \$4; exit}" "$MANIFEST" 2>/dev/null)
latest=$(runuser -u valheim -- env HOME=/opt/valheim /opt/valheim/steamcmd/steamcmd.sh +login anonymous +app_info_update 1 +app_info_print $APP +quit 2>/dev/null \
  | sed -n "/\"branches\"/,/^}/p" | sed -n "/\"public\"/,/}/p" | grep -m1 "\"buildid\"" | grep -oE "[0-9]+")
if [ -z "$latest" ]; then echo "no latest buildid (skipping, no restart)"; exit 0; fi
if [ "$installed" = "$latest" ]; then echo "up to date (build $installed)"; exit 0; fi
echo "UPDATE $installed -> $latest"
systemctl stop valheim
runuser -u valheim -- env HOME=/opt/valheim /opt/valheim/steamcmd/steamcmd.sh +force_install_dir /opt/valheim/server +login anonymous +app_update $APP validate +quit >/opt/valheim/steam-update.log 2>&1
systemctl start valheim
echo "updated to $latest and started"
EOF

for f in adminlist bannedlist permittedlist; do
  [ -f "$VH_DIR/data/$f.txt" ] || echo "// one player id per line" >"$VH_DIR/data/$f.txt"
done
chmod +x "$VH_DIR"/{start.sh,backup.sh,update.sh}
chown -R valheim:valheim "$VH_DIR"

# ---------- panel ----------
say "Installing the admin panel (FastAPI in its own venv)"
if [ -f "$0" ] && [ -d "$(dirname "$0")/panel" ]; then
  cp "$(dirname "$0")/panel/app.py" "$(dirname "$0")/panel/index.html" "$VH_DIR/panel/"
else
  curl -fsSL "$REPO_RAW/panel/app.py" -o "$VH_DIR/panel/app.py"
  curl -fsSL "$REPO_RAW/panel/index.html" -o "$VH_DIR/panel/index.html"
fi
python3 -m venv "$VH_DIR/panel/.venv"
"$VH_DIR/panel/.venv/bin/pip" install -q --upgrade pip
"$VH_DIR/panel/.venv/bin/pip" install -q fastapi "uvicorn[standard]"

# A fixed default password would be the same on every install on the planet, so the
# password is generated here and printed once. Change it later from the panel.
if [ ! -f "$VH_DIR/panel.env" ]; then
  PANEL_PASS=$(randstr 'A-Za-z0-9' 16)
  cat >"$VH_DIR/panel.env" <<EOF
PANEL_USER='admin'
PANEL_PASS='$PANEL_PASS'
PANEL_PORT='$PANEL_PORT'
EOF
  chmod 600 "$VH_DIR/panel.env"
fi

# ---------- systemd ----------
cat >/etc/systemd/system/valheim.service <<'EOF'
[Unit]
Description=Valheim dedicated server
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=valheim
ExecStart=/opt/valheim/start.sh
Restart=on-failure
RestartSec=8
# SIGINT is what makes the server save the world before dying; SIGTERM loses it
KillSignal=SIGINT
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/valheim-panel.service <<'EOF'
[Unit]
Description=Valheim admin panel
After=network-online.target
[Service]
Type=simple
EnvironmentFile=/opt/valheim/panel.env
WorkingDirectory=/opt/valheim/panel
ExecStart=/bin/sh -c '/opt/valheim/panel/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PANEL_PORT}'
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/valheim-backup.service <<'EOF'
[Unit]
Description=Valheim world backup
[Service]
Type=oneshot
User=valheim
ExecStart=/opt/valheim/backup.sh
EOF

cat >/etc/systemd/system/valheim-backup.timer <<'EOF'
[Unit]
Description=Valheim world backup every 2h
[Timer]
OnBootSec=10min
OnUnitActiveSec=2h
[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/valheim-update.service <<'EOF'
[Unit]
Description=Valheim server update check
[Service]
Type=oneshot
ExecStart=/opt/valheim/update.sh
EOF

cat >/etc/systemd/system/valheim-update.timer <<'EOF'
[Unit]
Description=Valheim update check every 2h
[Timer]
OnBootSec=15min
OnUnitActiveSec=2h
[Install]
WantedBy=timers.target
EOF

say "Starting services"
systemctl daemon-reload
systemctl enable --now valheim.service valheim-panel.service valheim-backup.timer valheim-update.timer >/dev/null 2>&1
for _ in $(seq 1 20); do
  systemctl is-active --quiet valheim-panel && break
  sleep 1
done
info "game server: $(systemctl is-active valheim) · panel: $(systemctl is-active valheim-panel)"
info "the world is generated on first start, give it ~30 s"

echo
echo "  Panel:    http://$(hostname -I | awk '{print $1}'):$(grep -oP "PANEL_PORT='\K[0-9]+" "$VH_DIR/panel.env")"
echo "  User:     $(grep -oP "PANEL_USER='\K[^']+" "$VH_DIR/panel.env")"
echo "  Password: $(grep -oP "PANEL_PASS='\K[^']+" "$VH_DIR/panel.env")   <- shown once, change it in the panel"
echo
echo "  Game:     $(hostname -I | awk '{print $1}'):$GAME_PORT   password: $SERVER_PASS"
echo
