#!/bin/bash
# The update path, end to end, on every push: the latest release installed the way a server
# has it, then updated to this commit by the script that server already runs - and, second,
# a deliberately broken release that must be rolled back. What must survive: the world, the
# settings, the panel login, the admin's choices (a switched-off timer), and the game process.
#   runs on the CI host; needs docker and $GITHUB_SHA
set -euo pipefail
REPO=PawelSzymanski89/valheim-proxmox
KEY=WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0=
x() { docker exec vhu bash -c "$*"; }
fail() { echo "FAIL: $*"; x "journalctl --no-pager -n 60" || true; exit 1; }

echo "== the latest release, checked against the release key"
AUTH=(); [ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GH_TOKEN")
TAG=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/releases/latest" | grep -oP '"tag_name":\s*"\K[^"]+')
curl -fsSL -o prev.tar.gz "https://github.com/$REPO/releases/download/$TAG/valheim-proxmox-$TAG.tar.gz"
curl -fsSL -o prev.sig "https://github.com/$REPO/releases/download/$TAG/valheim-proxmox-$TAG.tar.gz.sig"
{ echo "-----BEGIN PUBLIC KEY-----"; { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; echo $KEY | base64 -d; } | base64
  echo "-----END PUBLIC KEY-----"; } > key.pem
base64 -d prev.sig > prev.sig.bin
openssl pkeyutl -verify -pubin -inkey key.pem -rawin -in prev.tar.gz -sigfile prev.sig.bin
echo "installing $TAG"

docker build -q -t vh-systemd -f .github/ci/systemd.Dockerfile .github/ci >/dev/null
docker run -d --name vhu --privileged --cgroupns=host --network host \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp -v /sys/fs/cgroup:/sys/fs/cgroup:rw vh-systemd >/dev/null
for _ in $(seq 1 30); do x "systemctl is-system-running" 2>/dev/null | grep -qE "running|degraded" && break; sleep 1; done
docker cp prev.tar.gz vhu:/root/prev.tar.gz
x "mkdir /root/prev && tar xzf /root/prev.tar.gz -C /root/prev --strip-components=1"
x "cd /root/prev && PANEL_PASS=ci-upgrade-1 SERVER_NAME=CI WORLD_NAME=CIWorld bash setup.sh" | tail -4

echo "== the game comes up on $TAG, then the admin's own state is laid down"
for _ in $(seq 1 90); do x "journalctl -u valheim --no-pager | grep -q 'Game server connected'" && break; sleep 5; done
x "journalctl -u valheim --no-pager | grep -q 'Game server connected'" || fail "the game never came up on $TAG"
x "systemctl disable --now valheim-update.timer >/dev/null 2>&1; echo 76561198000000042 >> /opt/valheim/data/adminlist.txt"
snap() { x "cd /opt/valheim && md5sum server.env panel.env data/*.txt && find data/worlds_local -type f -exec md5sum {} + | sort -k2 | md5sum && systemctl is-enabled valheim-update.timer || true"; }
BEFORE=$(snap); PID=$(x "systemctl show valheim -p MainPID --value")

echo "== update to this commit, by the script $TAG installed"
x "ALLOW_UNSIGNED=1 /opt/valheim/panel-update.sh $GITHUB_SHA" | tail -3
[ "$(x cat /opt/valheim/panel/VERSION)" = "$(cat panel/VERSION)" ] || fail "VERSION is not this commit's"
[ "$(snap)" = "$BEFORE" ] || { diff <(echo "$BEFORE") <(snap); fail "the world, settings or the admin's choices changed"; }
[ "$(x "systemctl show valheim -p MainPID --value")" = "$PID" ] || fail "the game was restarted"
for _ in $(seq 1 60); do x "curl -sf http://127.0.0.1:2460/api/health" >/dev/null && break; sleep 2; done
x "curl -sf http://127.0.0.1:2460/api/health" || fail "health checks do not pass after the update"
echo; echo "upgrade: ok"

echo "== a broken release must be rolled back, with this commit's script"
rm -rf broken && git archive --prefix=broken/ HEAD | tar x
python3 - <<'PY'
p = "broken/panel/app.py"; s = open(p).read()
s = s.replace("def _tick():\n", "def _tick():\n    raise RuntimeError('broken on purpose - CI rollback test')\n", 1)
open(p, "w").write(s)
PY
echo ci-broken > broken/panel/VERSION
docker cp broken vhu:/root/broken
set +e; x "ALLOW_UNSIGNED=1 TEST_SRC_DIR=/root/broken /opt/valheim/panel-update.sh ci-broken" | tail -4; rc=${PIPESTATUS[0]}; set -e
x "grep -q ' rollback ' /opt/valheim/update-result" || fail "the broken release was not rolled back (rc=$rc): $(x cat /opt/valheim/update-result)"
[ "$(x cat /opt/valheim/panel/VERSION)" = "$(cat panel/VERSION)" ] || fail "the rollback did not bring this commit's panel back"
[ "$(snap)" = "$BEFORE" ] || fail "the rollback changed the world or settings"
[ "$(x "systemctl show valheim -p MainPID --value")" = "$PID" ] || fail "the game was restarted"
for _ in $(seq 1 60); do x "curl -sf http://127.0.0.1:2460/api/health" >/dev/null && break; sleep 2; done
x "curl -sf http://127.0.0.1:2460/api/health" >/dev/null || fail "the rolled-back panel is not healthy"
echo "rollback: ok"
