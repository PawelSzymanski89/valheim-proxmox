#!/usr/bin/env bash
# Installs the Valheim dedicated server + admin panel inside a Debian 12 system.
# Called by install.sh inside a fresh LXC, but it also runs fine on its own on any
# Debian 12 box: bash setup.sh
#
# SETUP_MODE=image (docker/Dockerfile): lay the files down and stop there - no game
# download, no panel.env, nothing started. The container's first boot does those, so the
# image carries no stale build and no ntfy topic shared by everyone who pulls it.
#
# SETUP_MODE=upgrade (panel/panel-update.sh): bring an existing install up to this version -
# scripts, services, dependencies, the panel. Settings, logins, the world, backups and mods
# stay as they are, the game is not downloaded again and a stopped server stays stopped.
set -euo pipefail
IMAGE=${SETUP_MODE:-}; [ "$IMAGE" = image ] || IMAGE=
UPGRADE=${SETUP_MODE:-}; [ "$UPGRADE" = upgrade ] || UPGRADE=

VH_DIR=${VH_DIR:-/opt/valheim}
PANEL_PORT=${PANEL_PORT:-2460}
PANEL_USER=${PANEL_USER:-admin}
PANEL_PASS=${PANEL_PASS:-valheim123}   # always the same on a fresh install, on purpose — you
                                    # change it in the panel and the panel nags until you do
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

# The PlayFab dependency is not optional and it is not just libpulse0: `ldd libparty.so`
# asks for libpulse-mainloop-glib.so.0, which lives in a separate package. Without it
# crossplay dies with "DLL Not Found", the server loops on "begin PlayFab create and join
# network" forever and hands out an empty join code. Diagnosed on a live server that had
# been unreachable for days because of exactly this.
say "Installing packages (32-bit Steam libs, PlayFab dependency, Python)"
export DEBIAN_FRONTEND=noninteractive
# ssh/pct hand us the caller's LANG and LC_*, which the fresh container has no locales
# for — that alone produces a screen of perl and apt-listchanges warnings.
export LANG=C.UTF-8 LC_ALL=C.UTF-8
dpkg --add-architecture i386
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl tar gzip unzip procps \
  lib32gcc-s1 libsdl2-2.0-0:i386 libatomic1 \
  libpulse0 libpulse-mainloop-glib0 \
  python3 python3-venv python3-pip >/dev/null
info "done"

id -u valheim >/dev/null 2>&1 || useradd -m -d "$VH_DIR" -s /bin/bash valheim
mkdir -p "$VH_DIR"/{steamcmd,server,data/worlds_local,backups,panel}
# not on an upgrade: panel.env is root's, and the game user has no business reading the login
[ -n "$UPGRADE" ] || chown -R valheim:valheim "$VH_DIR"

if [ -n "$UPGRADE" ] && [ -x "$VH_DIR/server/valheim_server.x86_64" ]; then
  say "Upgrade - the game stays as it is (the panel keeps it updated)"
else
say "Fetching SteamCMD"
# runuser, not sudo — sudo is not in the stock Debian container image
runuser -u valheim -- env HOME="$VH_DIR" bash -c "cd $VH_DIR/steamcmd && curl -sqL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar zxf -"
info "done"

if [ -n "$IMAGE" ]; then
  say "Image build - the game downloads on the container's first boot"
else
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
  | grep --line-buffered -E 'Update state|Success! App|ERROR!' \
  | awk '{ if (match($0, /progress: [0-9.]+/)) {
             p = int(substr($0, RSTART + 10, RLENGTH - 10) / 10)   # every ~10%, not every poll
             if (p == seen) next
             seen = p }
           print "      " $0; fflush() }'
set -e
[ -x "$VH_DIR/server/valheim_server.x86_64" ] || die "Steam download failed — see $VH_DIR/steam-install.log"
fi
fi

# ---------- launch config ----------
# Settings live here, not in start.sh, so the panel has something to edit - and once the
# panel owns them, running this again must not put the defaults back.
[ -f "$VH_DIR/server.env" ] || cat >"$VH_DIR/server.env" <<EOF
NAME='$SERVER_NAME'
WORLD='$WORLD_NAME'
PASSWORD='$SERVER_PASS'
PORT='$GAME_PORT'
PUBLIC='0'
CROSSPLAY='0'
PRESET=''
MODIFIERS=''
SETKEYS=''
EOF

cat >"$VH_DIR/start.sh" <<'EOF'
#!/bin/bash
# Settings come from server.env (edited by the panel). This only assembles arguments.
export LD_LIBRARY_PATH="/opt/valheim/server/linux64:$LD_LIBRARY_PATH"
export SteamAppId=892970
NAME=Valheim; WORLD=Dedicated; PASSWORD=; PORT=2456; PUBLIC=0; CROSSPLAY=0; PRESET=; MODIFIERS=; SETKEYS=
[ -r /opt/valheim/server.env ] && . /opt/valheim/server.env
cd /opt/valheim/server

# BepInEx is loaded by doorstop; without these the plugins directory is simply ignored.
# Names taken from start_server_bepinex.sh that ships with the pack — this is Doorstop 4,
# the older DOORSTOP_ENABLE / DOORSTOP_INVOKE_DLL_PATH spelling is silently ignored.
if [ -d /opt/valheim/server/BepInEx ]; then
  export DOORSTOP_ENABLED=1
  export DOORSTOP_TARGET_ASSEMBLY=./BepInEx/core/BepInEx.Preloader.dll
  export LD_LIBRARY_PATH="./doorstop_libs:$LD_LIBRARY_PATH"
  export LD_PRELOAD="libdoorstop_x64.so:$LD_PRELOAD"
  echo "BepInEx: enabled"
fi

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

# A backup copies a file, and that file is only as fresh as the last autosave - up to twenty
# minutes behind. If the admin tools are installed, ask the server to write the world first.
# Failure is not fatal here: an older world is still worth archiving.
if [ -r /opt/valheim/rcon.env ]; then
  python3 /opt/valheim/rcon-save.py 2>/dev/null && sleep 4
fi

ts=$(date +%Y%m%d-%H%M%S)
tar czf "$DST/world-$ts.tar.gz" -C "$SRC" . 2>/dev/null && echo "backup world-$ts.tar.gz"
ls -1t "$DST"/world-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
EOF

cat >"$VH_DIR/update.sh" <<'EOF'
#!/bin/bash
# Since 2026-09-07 the panel installs game updates itself (valheim-update.timer is the
# on/off switch it reads). This script stays so the timer has something to run.
echo "game updates are handled by the panel - see the Log tab"
EOF

for f in adminlist bannedlist permittedlist; do
  [ -f "$VH_DIR/data/$f.txt" ] || echo "// one player id per line" >"$VH_DIR/data/$f.txt"
done
cat >"$VH_DIR/rcon-save.py" <<'EOF'
# Asks the running server to write the world, so a backup taken a second later is current.
# Source RCON: length, request id, type (3 authenticates, 2 runs), body, two nulls.
import re, socket, struct
e = dict(re.findall(r"(\w+)='([^']*)'", open("/opt/valheim/rcon.env").read()))


def pkt(i, t, body):
    d = struct.pack("<ii", i, t) + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(d)) + d


def rd(s):
    n = struct.unpack("<i", s.recv(4))[0]
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk


s = socket.create_connection(("127.0.0.1", int(e["RCON_PORT"])), timeout=5)
s.sendall(pkt(1, 3, e["RCON_PASS"])); rd(s)
s.sendall(pkt(2, 2, "save")); rd(s)
s.close()
EOF

chmod +x "$VH_DIR"/{start.sh,backup.sh,update.sh}

# ---------- panel ----------
say "Installing the admin panel (FastAPI in its own venv)"
# everything panel-update.sh fetches too - one list, so an install and an update never differ
PANEL_FILES_="app.py icon_badge.py index.html login.html icon.svg greetings.json jokes.json requirements.txt VERSION panel-update.sh"
for f in $PANEL_FILES_; do
  if [ -f "$0" ] && [ -d "$(dirname "$0")/panel" ]; then cp "$(dirname "$0")/panel/$f" "$VH_DIR/panel/"
  else curl -fsSL "$REPO_RAW/panel/$f" -o "$VH_DIR/panel/$f"; fi
done
mv "$VH_DIR/panel/panel-update.sh" "$VH_DIR/panel-update.sh"
[ -x "$VH_DIR/panel/.venv/bin/python" ] || python3 -m venv "$VH_DIR/panel/.venv"
"$VH_DIR/panel/.venv/bin/pip" install -q --upgrade pip
"$VH_DIR/panel/.venv/bin/pip" install -q -r "$VH_DIR/panel/requirements.txt"

# The first password is fixed and printed, so there is never a "what was it again" moment.
# It is the same on every install of this repo, which is exactly why the panel keeps warning
# until it is changed — and why the panel has no business being on the internet before that.
if [ -z "$IMAGE" ] && [ ! -f "$VH_DIR/panel.env" ]; then
  cat >"$VH_DIR/panel.env" <<EOF
PANEL_USER='$PANEL_USER'
PANEL_PASS='$PANEL_PASS'
PANEL_PORT='$PANEL_PORT'
NTFY_SERVER='https://ntfy.sh'
NTFY_TOPIC='valheim-$(randstr 'a-z0-9' 10)'
EOF
  chmod 600 "$VH_DIR/panel.env"
fi

# password recovery: no reset dance, just set a new one from the host
cat >"$VH_DIR/panel-passwd.sh" <<'EOF'
#!/bin/bash
# Reset the panel login without the panel. Run inside the container:
#   /opt/valheim/panel-passwd.sh [user] <password>
set -eu
ENV=/opt/valheim/panel.env
[ $# -ge 1 ] || { echo "usage: $0 [user] <password>"; exit 1; }
if [ $# -ge 2 ]; then USER_=$1; PASS=$2; else USER_=$(grep -oP "PANEL_USER='\K[^']*" $ENV || echo admin); PASS=$1; fi
[ ${#PASS} -ge 8 ] || { echo "password must be at least 8 characters"; exit 1; }
PORT=$(grep -oP "PANEL_PORT='\K[^']*" $ENV 2>/dev/null || echo 2460)
printf "PANEL_USER='%s'\nPANEL_PASS='%s'\nPANEL_PORT='%s'\n" "$USER_" "$PASS" "$PORT" >$ENV
chmod 600 $ENV
echo "panel login is now $USER_ / $PASS (no restart needed)"
EOF
chmod +x "$VH_DIR/panel-passwd.sh"

chmod +x "$VH_DIR/panel-update.sh"
echo "$(cat "$VH_DIR/panel/VERSION") $(date -u +%FT%TZ)" >"$VH_DIR/panel.version"

# ---------- ownership ----------
# Root owns everything root runs or trusts; the game user gets only what the game writes.
# Until v1.20.1 all of /opt/valheim was the game user's, so anything running as that user -
# one bad mod from a share code - could edit backup.sh or the panel and have root run it.
# Runs on every install and every upgrade, which is how existing servers get fixed.
lock_down() {
  local d f
  chown root:root "$VH_DIR"; chmod 755 "$VH_DIR"
  # what the game, Steam, Unity and Mono write - the install dir is also the user's HOME
  for d in steamcmd server data backups .steam Steam .config .local .cache .mono; do
    mkdir -p "$VH_DIR/$d"; chown -hR valheim:valheim "$VH_DIR/$d"
  done
  mkdir -p "$VH_DIR/panel"; chown -hR root:root "$VH_DIR/panel"; chmod 700 "$VH_DIR/panel"
  for f in start.sh backup.sh update.sh rcon-save.py panel-update.sh panel-passwd.sh; do
    [ -f "$VH_DIR/$f" ] && chown -h root:root "$VH_DIR/$f" && chmod 755 "$VH_DIR/$f"
  done
  [ -f "$VH_DIR/server.env" ] && chown -h root:valheim "$VH_DIR/server.env" && chmod 640 "$VH_DIR/server.env"
  [ -f "$VH_DIR/panel.env" ] && chown -h root:root "$VH_DIR/panel.env" && chmod 600 "$VH_DIR/panel.env"
  [ -f "$VH_DIR/rcon.env" ] && chown -h valheim:valheim "$VH_DIR/rcon.env" && chmod 600 "$VH_DIR/rcon.env"
  return 0
}
lock_down

# ---------- systemd ----------
# What exists before the unit files are rewritten. An upgrade enables only units that are
# new; the rest stay exactly as the admin (or an armed launch) left them - rewriting a unit
# file does not touch whether it is enabled, and neither may this script.
UNITS_BEFORE=""
for u in valheim.service valheim-panel.service valheim-backup.timer valheim-update.timer; do
  [ -f "/etc/systemd/system/$u" ] && UNITS_BEFORE="$UNITS_BEFORE $u"
done
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

if [ -n "$UPGRADE" ]; then
  say "Reloading services"
  systemctl daemon-reload
  # Only what did not exist before. The game update timer switched off, the game service
  # disabled by an armed launch: re-enabling either is exactly what an update must not do.
  for u in valheim.service valheim-panel.service valheim-backup.timer valheim-update.timer; do
    case " $UNITS_BEFORE " in *" $u "*) ;; *)
      case "$u" in *.timer) systemctl enable --now "$u" >/dev/null 2>&1 ;;
                   *) systemctl enable "$u" >/dev/null 2>&1 ;; esac
      info "enabled new unit $u" ;;
    esac
  done
  info "upgraded to $(cat "$VH_DIR/panel/VERSION")"
  exit 0
fi
if [ -n "$IMAGE" ]; then
  say "Enabling services for the container's first boot"
  systemctl enable valheim.service valheim-panel.service valheim-backup.timer valheim-update.timer >/dev/null 2>&1
  exit 0
fi
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
echo "  Password: $(grep -oP "PANEL_PASS='\K[^']+" "$VH_DIR/panel.env")   <- same on every install, change it in Settings"
echo
echo "  Game:     $(hostname -I | awk '{print $1}'):$GAME_PORT   password: $SERVER_PASS"
echo
echo "  Alerts:   subscribe in the ntfy app to  $(grep -oP "NTFY_TOPIC='\\K[^']+" "$VH_DIR/panel.env")"
echo "            (server down, players joining, backups, disk - switch them on in the panel)"
echo
