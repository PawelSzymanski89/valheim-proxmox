#!/usr/bin/env bash
# One-liner installer — run this ON THE PROXMOX HOST.
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/install.sh)"
#
# Creates an unprivileged Debian 12 LXC, installs the Valheim dedicated server and the
# admin panel in it, prints the address and the generated panel password.
set -euo pipefail

REPO_RAW=${REPO_RAW:-https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main}
CTID=${CTID:-}
HOSTNAME_=${HOSTNAME_:-valheim}
DISK=${DISK:-30}
CORES=${CORES:-4}
RAM=${RAM:-6144}
BRIDGE=${BRIDGE:-vmbr0}
STORAGE=${STORAGE:-}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-local}
GAME_PORT=${GAME_PORT:-2456}
PANEL_PORT=${PANEL_PORT:-2460}
SERVER_NAME=${SERVER_NAME:-Valheim}
WORLD_NAME=${WORLD_NAME:-Dedicated}
SERVER_PASS=${SERVER_PASS:-}

msg() { echo -e "\033[1;32m==>\033[0m $*"; }
die() { echo -e "\033[1;31mError:\033[0m $*" >&2; exit 1; }

command -v pct >/dev/null || die "pct not found — run this on the Proxmox VE host, not inside a container."
[ "$(id -u)" -eq 0 ] || die "Run as root."

# Valheim needs a password of 5+ characters that does not contain the server or world name.
if [ -z "$SERVER_PASS" ]; then
  SERVER_PASS=$(tr -dc 'a-z0-9' </dev/urandom | head -c 10)
fi

# --- pick a container id ---
if [ -z "$CTID" ]; then
  CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 100)
fi
pct status "$CTID" >/dev/null 2>&1 && die "CTID $CTID already exists. Set CTID=<free id> and retry."

# --- pick storage that can hold a container rootfs ---
if [ -z "$STORAGE" ]; then
  STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR==2{print $1}')
  [ -n "$STORAGE" ] || die "No storage with content type 'rootdir'. Set STORAGE=<name>."
fi

# --- template ---
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
[ -n "$TEMPLATE" ] || die "No debian-12-standard template offered by 'pveam available'."
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  msg "Downloading template $TEMPLATE"
  pveam update >/dev/null 2>&1 || true
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
fi

msg "Creating LXC $CTID ($HOSTNAME_): ${CORES} cores, ${RAM} MB RAM, ${DISK} GB on $STORAGE"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME_" \
  --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "$STORAGE:$DISK" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 --start 1 >/dev/null

msg "Waiting for the network"
for _ in $(seq 1 30); do
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
  [ -n "${IP:-}" ] && break
  sleep 2
done
[ -n "${IP:-}" ] || die "Container $CTID got no IP address."

msg "Installing inside the container (Steam download takes a few minutes)"
pct exec "$CTID" -- bash -c "curl -fsSL $REPO_RAW/setup.sh -o /tmp/setup.sh"
pct exec "$CTID" -- env \
  REPO_RAW="$REPO_RAW" PANEL_PORT="$PANEL_PORT" GAME_PORT="$GAME_PORT" \
  SERVER_NAME="$SERVER_NAME" WORLD_NAME="$WORLD_NAME" SERVER_PASS="$SERVER_PASS" \
  bash /tmp/setup.sh

cat <<EOF

  Container:  $CTID ($HOSTNAME_) at $IP
  Panel:      http://$IP:$PANEL_PORT
  Game:       $IP:$GAME_PORT   password: $SERVER_PASS

  The panel user and generated password are printed above. Change them in
  Settings -> Panel login. To play from the internet, forward UDP
  $GAME_PORT-$((GAME_PORT+2)) to $IP on your router — the panel itself should stay
  on the LAN or behind a VPN.

EOF
