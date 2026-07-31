# valheim-proxmox

One command on a Proxmox host gives you a Valheim dedicated server in its own LXC,
plus a web panel to run it. No RCON gymnastics, no Docker, no game panel to babysit.

🇵🇱 **[Polska wersja tego pliku → README.pl.md](README.pl.md)**

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/install.sh)"
```

Run it **on the Proxmox VE host** (as root). It creates an unprivileged Debian 12
container, installs SteamCMD and the Valheim dedicated server, sets up systemd units and
timers, installs the admin panel, and prints the address, the login and the generated
password at the end.

Takes a few minutes — most of it is Steam pulling ~1.5 GB.

## What you get

| | |
|---|---|
| **Game server** | Valheim dedicated, systemd unit with a clean stop (`SIGINT`, so the world is saved) |
| **Panel** | web UI on port **2460**, HTTP Basic auth, password generated at install |
| **Backups** | world snapshot every 2 h, 30 kept, restore with one click |
| **Updates** | checks Steam every 2 h and restarts **only** when there is a new build |
| **Defaults** | 4 cores, 6 GB RAM, 30 GB disk, container starts on boot |

## The panel

| Tab | What it does |
|---|---|
| **Summary** | join addresses for LAN and for the internet (with copy buttons), live load / RAM / disk of the container, and connectivity checks that say what they can and cannot prove |
| **Players** | who is online right now — name, id, **live session timer** — and a persistent login history (first seen / last seen / number of joins) |
| **Access & bans** | admin list, ban list, allowlist; ban straight from the online list or the history. **An allowlist that is not empty locks everyone else out** — that is Valheim's own rule, not the panel's |
| **World** | list worlds, switch the active one, download, delete, upload a `.db` + `.fwl` pair |
| **Backups** | restore, download, delete; toggles for the auto-backup and auto-update timers |
| **Settings** | server name, world, password, **game port**, **panel port**, public listing, crossplay, world preset and modifiers (combat, death penalty, resources, raids, portals) and the world toggles (`nobuildcost`, `playerevents`, `passivemobs`, `nomap`) |
| **Mods** | paste a Thunderstore Mod Manager / r2modman **share code**: the panel expands it, shows what is inside, and installs the picked packages (BepInEx included, world backed up first). Also installs single packages by name |
| **Log** | server events, with the PlayFab keepalive noise filtered out |

Plus Start / Stop / Restart / Back up now / Check update.

### Login

The installer generates a random 16-character password and prints it **once**. There is
no fixed default on purpose — a shipped default password would be identical on every
install of this repo. Change the user and password in **Settings → Panel login**;
credentials are read per request, so the change is immediate, no restart.

They live in `/opt/valheim/panel.env` (mode 600, root-owned).

### Ports

| Port | What |
|---|---|
| `2456-2458/udp` | the game (Valheim always uses three consecutive ports starting at the one you set) |
| `2460/tcp` | the panel — picked to sit right next to the game ports so it is easy to remember, and clear of the usual suspects (8080, 8000, 9000, 8006…) |

Both are changeable in **Settings**. Changing the panel port restarts the panel through
`systemd-run`, so the request that changed it still gets an answer. The panel refuses a
port that would land inside the game's three-port range.

## Playing from the internet

Forward **UDP 2456-2458** to the container on your router. That is all the game needs —
Valheim is raw UDP, it does not go through a reverse proxy and does not need a certificate.

### Crossplay changes what "port" means

**Measured, not assumed:** with crossplay on the server talks through the PlayFab relay and
**never binds the game port** — `ss -uln` shows only the query port. Players join from the
crossplay server list with a join code, and a router forward does exactly nothing.

With crossplay off the server binds `2456` and people connect by address, which is what the
port forwarding above is for. The installer therefore leaves crossplay **off**; flip it in
Settings if you would rather have Xbox/Game Pass players and no direct address joins.
**Crossplay needs `libpulse-mainloop-glib0`.** Without it PlayFab Party never initialises,
the log repeats `begin PlayFab create and join network` every 30 s and the join code comes
out empty — a server that nobody can reach by any route. The installer pulls it in; the
diagnosis was `ldd libparty.so`. With crossplay working the panel reads the **join code**
out of the log and shows it on Summary (it changes on every restart).


**Keep the panel off the internet.** It can delete worlds and hand out world downloads.
LAN or VPN only. If you must expose it, put it behind a reverse proxy with its own auth.

## Mods, from a share code

Export a profile in Thunderstore Mod Manager or r2modman (**Settings → Export profile → as a
code**) and paste that code into the **Mods** tab. The panel pulls the profile from
Thunderstore, lists the packages with their exact versions, and installs the ones you keep
ticked — together with BepInEx if it is not there yet.

The point of going through the code rather than picking mods by hand: the versions in it are
the versions your players already run, and a version mismatch is what bounces people at the
door. The code is shown on the **Summary** tab afterwards, so you can hand it back to anyone
who needs to catch up.

Profiles carry client-side mods too (UI, maps, sounds). They are usually harmless on a server
but a few throw on load, so untick what the server has no use for. The world is backed up
before the first mod is ever installed — mods can wreck a save for good.

Installed mods live in `server/BepInEx/plugins/<author>-<Package>/`, and `start.sh` turns on
the doorstop loader by itself once `BepInEx/` exists.

## Options

Every value can be overridden with an environment variable:

```bash
CTID=250 RAM=8192 CORES=6 DISK=40 GAME_PORT=2456 PANEL_PORT=2460 \
SERVER_NAME="Klans" WORLD_NAME="Midgard" SERVER_PASS="letmein42" \
bash -c "$(curl -fsSL .../install.sh)"
```

| Variable | Default | |
|---|---|---|
| `CTID` | next free id | container id |
| `HOSTNAME_` | `valheim` | container hostname |
| `CORES` / `RAM` / `DISK` | `4` / `6144` / `30` | cores / MB / GB |
| `STORAGE` | first storage that takes a rootfs | where the container disk goes |
| `IP` / `GW` | `dhcp` | fixed address instead: `IP=192.168.89.21/24 GW=192.168.89.1` — worth it when DNS or a port forward already points at that address |
| `BRIDGE` | `vmbr0` | network bridge |
| `GAME_PORT` / `PANEL_PORT` | `2456` / `2460` | |
| `SERVER_NAME` / `WORLD_NAME` | `Valheim` / `Dedicated` | |
| `SERVER_PASS` | random 10 chars | game password (5+ chars, must not contain the server or world name — the game rejects that) |

## Installing without Proxmox

`setup.sh` works on any Debian 12 machine on its own:

```bash
curl -fsSL .../setup.sh -o setup.sh && bash setup.sh
```

## Layout

```
/opt/valheim/
├── server/            game files (SteamCMD)
├── data/              savedir: worlds_local/, adminlist.txt, bannedlist.txt, permittedlist.txt
├── backups/           world-YYYYMMDD-HHMMSS.tar.gz, 30 kept
├── server.env         launch settings — this is what the panel edits
├── panel.env          panel user, password, port (600)
├── players.json       login history (the journal rotates, this does not)
├── start.sh           assembles the launch arguments from server.env
├── backup.sh          world snapshot + retention
├── update.sh          Steam build check, restarts only when there is a new build
└── panel/             app.py, index.html, .venv
```

systemd: `valheim`, `valheim-panel`, `valheim-backup.timer`, `valheim-update.timer`.

## Honest limitations

- **Valheim has no RCON.** In-game commands (kick, spawn, weather, god mode) are typed in
  the F5 console by a player whose id is in `adminlist.txt`. The panel manages that list —
  it cannot type into the game for you. A "kick" here is a ban followed by an unban.
- **The online player list is a heuristic.** The log line that carries the player name
  (`Got character ZDOID from …`) does not carry the player id, so names are matched to
  connections in order of arrival. The authoritative counter (`Connections N`) is printed
  only every ~10 minutes; when the two disagree the panel says so rather than hiding it.
- **The panel runs as root** in its own container. It calls `systemctl` and writes into
  `/opt/valheim`. That is why it is a dedicated container and why it should not face the internet.

## Verified on

Proxmox VE 8.4, `debian-12-standard` template, Valheim dedicated `l-0.221.12`.
Every panel action was exercised against a real container: settings propagation down to the
running process arguments, world switch/upload/delete, backup restore (checksum matched
before and after), timers, panel port change, login change. The player list and the login
history are covered by `panel/test_parse.py`, since they need real players joining.

## License

MIT — see [LICENSE](LICENSE).
