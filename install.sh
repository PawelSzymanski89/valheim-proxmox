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
FIREWALL=${FIREWALL:-1}        # 0 = no Proxmox firewall rules for the container
IP=${IP:-dhcp}                 # or a fixed address: IP=192.168.89.21/24 GW=192.168.89.1
GW=${GW:-}
STORAGE=${STORAGE:-}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-local}
GAME_PORT=${GAME_PORT:-2456}
PANEL_PORT=${PANEL_PORT:-2460}
SERVER_NAME=${SERVER_NAME:-Valheim}
WORLD_NAME=${WORLD_NAME:-Dedicated}
SERVER_PASS=${SERVER_PASS:-}
PANEL_USER=${PANEL_USER:-admin}
PANEL_PASS=${PANEL_PASS:-}            # empty = setup.sh generates one and prints it

usage() {
  cat <<USAGE
Valheim on Proxmox — creates an LXC and installs the server + admin panel.

  --ctid N            container id            (default: next free)
  --hostname NAME     container hostname      (default: $HOSTNAME_)
  --cores N           cpu cores               (default: $CORES)
  --ram MB            memory cap in MB        (default: $RAM)
  --disk GB           rootfs size in GB       (default: $DISK)
  --storage NAME      proxmox storage         (default: first one taking a rootfs)
  --bridge NAME       network bridge          (default: $BRIDGE)
  --no-firewall       do not write Proxmox firewall rules for the container
  --ip ADDR           static address, e.g. 192.168.89.21/24 (default: dhcp)
  --gw ADDR           gateway for a static address
  --game-port N       game port, uses N..N+2  (default: $GAME_PORT)
  --panel-port N      panel port              (default: $PANEL_PORT)
  --server-name NAME  name in the server list (default: $SERVER_NAME)
  --world NAME        world name              (default: $WORLD_NAME)
  --password PASS     game password           (default: generated)
  --panel-user NAME   panel login             (default: $PANEL_USER)
  --panel-pass PASS   panel password          (default: generated and printed)
  -h, --help          this text

Every flag also works as an environment variable (CTID=250 RAM=8192 …).
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID=$2; shift 2;;
    --hostname) HOSTNAME_=$2; shift 2;;
    --cores) CORES=$2; shift 2;;
    --ram) RAM=$2; shift 2;;
    --disk) DISK=$2; shift 2;;
    --storage) STORAGE=$2; shift 2;;
    --bridge) BRIDGE=$2; shift 2;;
    --no-firewall) FIREWALL=0; shift;;
    --ip) IP=$2; shift 2;;
    --gw) GW=$2; shift 2;;
    --game-port) GAME_PORT=$2; shift 2;;
    --panel-port) PANEL_PORT=$2; shift 2;;
    --server-name) SERVER_NAME=$2; shift 2;;
    --world) WORLD_NAME=$2; shift 2;;
    --password) SERVER_PASS=$2; shift 2;;
    --panel-user) PANEL_USER=$2; shift 2;;
    --panel-pass) PANEL_PASS=$2; shift 2;;
    -h|--help) usage;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 1;;
  esac
done

msg() { echo -e "\033[1;32m==>\033[0m $*"; }
die() { echo -e "\033[1;31mError:\033[0m $*" >&2; exit 1; }
trap 'echo -e "\033[1;31mInstall failed at line $LINENO\033[0m" >&2' ERR

# No `| head -c N` here: head closing the pipe kills the writer with SIGPIPE, and with
# `set -o pipefail` that aborts the whole script before it prints anything.
randstr() { local s; s=$(head -c 48 /dev/urandom | base64 | tr -dc "$1"); echo "${s:0:$2}"; }

command -v pct >/dev/null || die "pct not found — run this on the Proxmox VE host, not inside a container."
[ "$(id -u)" -eq 0 ] || die "Run as root."

# Valheim needs a password of 5+ characters that does not contain the server or world name.
if [ -z "$SERVER_PASS" ]; then
  SERVER_PASS=$(randstr 'a-z0-9' 10)
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

msg "Creating LXC $CTID ($HOSTNAME_): ${CORES} cores, ${RAM} MB RAM, ${DISK} GB on $STORAGE, ip=$IP"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME_" \
  --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "$STORAGE:$DISK" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$IP${GW:+,gw=$GW}$([ "$FIREWALL" = 1 ] && echo ,firewall=1)" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 --start 1 >/dev/null

# The container's own firewall: the game from anywhere, the panel from private networks only,
# everything else - the RCON port of the admin tools above all, which binds to every
# interface and has no setting to stop it - dropped. DHCP and IPv6 neighbour discovery are
# allowed explicitly, or the container would lose its address at the first lease renewal.
if [ "$FIREWALL" = 1 ]; then
  cat >"/etc/pve/firewall/$CTID.fw" <<FW
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT
dhcp: 1
ndp: 1

[RULES]
IN ACCEPT -p udp -dport $GAME_PORT:$((GAME_PORT + 2)) -log nolog # Valheim game and query
IN ACCEPT -p tcp -dport $PANEL_PORT -source 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10 -log nolog # panel, private networks and CGNAT VPNs
IN ACCEPT -p tcp -dport $PANEL_PORT -source fc00::/7 -log nolog # panel, private IPv6
IN Ping(ACCEPT) -log nolog
FW
  if ! grep -qs "^enable: *1" /etc/pve/firewall/cluster.fw; then
    FW_NOTE="The container has firewall rules, but the Proxmox firewall is off for the whole datacenter, so they do nothing yet (Datacenter -> Firewall -> Options -> Firewall: Yes)."
  fi
fi

msg "Waiting for the network"
for _ in $(seq 1 30); do
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
  [ -n "${IP:-}" ] && break
  sleep 2
done
[ -n "${IP:-}" ] || die "Container $CTID got no IP address."

msg "Installing inside the container (Steam download takes a few minutes)"
# The stock Debian template ships without curl, so the host fetches setup.sh and
# pushes it in. Also lets you run this straight from a git clone.
# mktemp, not a fixed name in /tmp: this runs as root on the host.
HOST_TMP=$(mktemp -d); trap 'rm -rf "$HOST_TMP"' EXIT
if [ -f "$0" ] && [ -f "$(dirname "$0")/setup.sh" ]; then
  pct push "$CTID" "$(dirname "$0")/setup.sh" /tmp/setup.sh
else
  curl -fsSL "$REPO_RAW/setup.sh" -o "$HOST_TMP/setup.sh"
  pct push "$CTID" "$HOST_TMP/setup.sh" /tmp/setup.sh
fi
# The settings travel as a root-only file, not on the command line: arguments of
# "pct exec ... env PANEL_PASS=..." show up in ps for every user of the host.
( umask 077
  for v in REPO_RAW PANEL_PORT GAME_PORT PANEL_USER PANEL_PASS SERVER_NAME WORLD_NAME SERVER_PASS; do
    printf 'export %s=%q\n' "$v" "${!v}"
  done >"$HOST_TMP/setup.env" )
pct push "$CTID" "$HOST_TMP/setup.env" /root/.valheim-setup.env --perms 0600
pct exec "$CTID" -- bash -c '. /root/.valheim-setup.env && rm -f /root/.valheim-setup.env && bash /tmp/setup.sh'

cat <<EOF

  Container:  $CTID ($HOSTNAME_) at $IP
  Panel:      http://$IP:$PANEL_PORT
  Game:       $IP:$GAME_PORT   password: $SERVER_PASS

  The panel user and generated password are printed above. Change them in
  Settings -> Panel login. To play from the internet, forward UDP
  $GAME_PORT-$((GAME_PORT+2)) to $IP on your router — the panel itself should stay
  on the LAN or behind a VPN.
${FW_NOTE:+
  $FW_NOTE
}
EOF
