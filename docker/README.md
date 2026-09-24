# Docker

The same server and panel as the LXC and bare-Debian installs, in a container that boots
systemd. The panel drives the game through systemd - units, timers, the journal - so the
container runs systemd as PID 1 rather than rewriting the panel around a process supervisor.
The price is `privileged: true` and host networking; the gain is one code path for all
three ways in.

**Know what that price means.** A privileged container with the host's cgroups and network is
not a security boundary: whoever gets root in it — through a panel bug, a malicious mod that
escalates, a poisoned dependency — is root on the Docker host. And with host networking the
panel listens on every interface of the host, which on a VPS is the internet. Use this path
on a machine that does nothing else important, keep port 2460 closed to the internet (firewall,
or a reverse proxy with its own authentication on a private address), and prefer the LXC
installer on Proxmox, where the container is unprivileged and gets firewall rules.

```bash
git clone https://github.com/PawelSzymanski89/valheim-proxmox && cd valheim-proxmox/docker
cp .env.example .env    # optional: ports, names, passwords
docker compose up -d --build
docker compose logs -f  # the first start downloads ~1.5 GB from Steam
```

Panel: `http://HOST:2460` (login `admin`, the password is generated on first boot: `docker compose logs | grep "panel login"`). Game: `HOST:2456/udp`.

## Updating

The image is the unit of update — the panel cannot replace its own code inside it, so it shows a
banner with the exact commands instead. From this folder:

```bash
git fetch --tags && git checkout vX.Y.Z   # the release the banner names
docker compose up -d --build
```

A release tag rather than `main`: release tags on GitHub cannot be moved or deleted, so what you
build is the published, signed release. The world, settings, mods and the panel login live in
the volume and survive the rebuild.

## What is where

| Path in the container | What |
|---|---|
| `/opt/valheim` (volume `valheim`) | worlds, backups, mods, `server.env`, `panel.env`, panel state |
| `/opt/valheim-image` (image) | the pristine install; `valheim-firstboot.service` copies it into the volume on every boot, overwriting only the code (panel, scripts) |

Rebuilding the image updates the panel; the volume keeps everything else. To start over:
`docker compose down -v`.

## Ports

`GAME_PORT` and `PANEL_PORT` in `.env` are read once, on the first start. With host
networking there is nothing to map: the game listens on `GAME_PORT` and `GAME_PORT+1` (udp),
the panel on `PANEL_PORT` (tcp), directly on the host. Changing the game port later is done
in the panel, as on any install. Two servers on one host = two directories with two `.env`
files, different `CONTAINER_NAME` and ports.

## Without compose

```bash
docker build -t valheim-proxmox -f docker/Dockerfile .
docker run -d --name valheim --privileged --cgroupns=host --network host \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v valheim:/opt/valheim -e GAME_PORT=2456 -e PANEL_PORT=2460 -e SERVER_PASS=secret \
  --stop-signal SIGRTMIN+3 --stop-timeout 100 --restart unless-stopped valheim-proxmox
```

## Not for

Docker Desktop on macOS or Windows: no host networking and no systemd-friendly cgroups
there. This path is for a Linux host - a VPS, a NAS, a machine that already runs Docker.
