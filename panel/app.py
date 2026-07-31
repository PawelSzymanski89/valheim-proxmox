"""Valheim server admin panel — runs inside the container, next to the game server.

No RCON exists in Valheim, so everything here is what a *server* admin can control:
the systemd unit, the launch arguments, the player lists, the worlds and the backups.
In-game commands (kick, spawn, weather) are done from the F5 console by a player
listed in adminlist.txt — the panel manages that list.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

VH_DIR = os.environ.get("VH_DIR", "/opt/valheim")
VH_DATA = f"{VH_DIR}/data"
VH_WORLDS = f"{VH_DATA}/worlds_local"
VH_BACKUPS = f"{VH_DIR}/backups"
VH_ENV = f"{VH_DIR}/server.env"
VH_PANEL_ENV = f"{VH_DIR}/panel.env"
VH_STORE = Path(os.environ.get("VH_STORE", f"{VH_DIR}/players.json"))
HERE = Path(__file__).resolve().parent

PANEL_DEFAULT_PASS = "valheim123"   # the installer's starting password; the UI nags until changed
VH_LISTS = {"admin": "adminlist.txt", "banned": "bannedlist.txt", "permitted": "permittedlist.txt"}
VH_TIMERS = {"backup": "valheim-backup.timer", "update": "valheim-update.timer"}
# What the game server (0.221) actually accepts. Nothing outside these sets reaches
# server.env — a typo in a modifier makes the server refuse to start, and then the
# panel is the only way back.
VH_PRESETS = ["", "normal", "casual", "easy", "hard", "hardcore", "immersive", "hammer"]
VH_MODIFIERS = {"combat": ["veryeasy", "easy", "hard", "veryhard"],
                "deathpenalty": ["casual", "veryeasy", "easy", "hard", "hardcore"],
                "resources": ["muchless", "less", "more", "muchmore", "most"],
                "raids": ["none", "muchless", "less", "more", "muchmore"],
                "portals": ["casual", "hard", "veryhard"]}
VH_KEYS = ["nobuildcost", "playerevents", "passivemobs", "nomap"]
VH_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")
VH_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
VH_BAK_RE = re.compile(r"^world-\d{8}-\d{6}\.tar\.gz$")
VH_ACTIONS = {"start": ("systemctl start valheim", 60),
              "stop": ("systemctl stop valheim", 180),
              "restart": ("systemctl restart valheim", 180),
              "backup": (f"{VH_DIR}/backup.sh", 120),
              "update": ("systemctl start --no-block valheim-update.service", 30)}


def _env_file(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                try:
                    parts = shlex.split(v)
                except ValueError:
                    parts = [v]
                out[k.strip()] = parts[0] if parts else ""
    except FileNotFoundError:
        pass
    return out


# ---------- auth ----------
# Session cookie + our own login page. HTTP Basic still works so curl and scripts stay
# usable, but nobody has to look at the browser's grey box.
# The cookie is signed with a secret AND the current password hash, so changing the
# password logs every session out — otherwise a stolen cookie would outlive the change.
SESSION_DAYS = 30


def _secret():
    cfg = _env_file(VH_PANEL_ENV)
    s = cfg.get("SESSION_SECRET")
    if not s:
        s = secrets.token_hex(32)
        cfg["SESSION_SECRET"] = s
        _save_panel_env(cfg)
    return s


def _sign(user, exp):
    cfg = _env_file(VH_PANEL_ENV)
    key = (_secret() + hashlib.sha256(cfg.get("PANEL_PASS", "").encode()).hexdigest()).encode()
    return hmac.new(key, f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()


def _check_login(user, password):
    cfg = _env_file(VH_PANEL_ENV)
    want_u, want_p = cfg.get("PANEL_USER", "admin"), cfg.get("PANEL_PASS", "")
    return bool(want_p) and secrets.compare_digest(user or "", want_u) and \
        secrets.compare_digest(password or "", want_p)


def _session_ok(cookie):
    try:
        user, exp, mac = (cookie or "").split("|")
        if int(exp) < int(time.time()):
            return None
        return user if hmac.compare_digest(mac, _sign(user, exp)) else None
    except Exception:
        return None


def _basic(request):
    h = request.headers.get("authorization", "")
    if not h.startswith("Basic "):
        return None
    try:
        u, _, p = base64.b64decode(h[6:]).decode().partition(":")
    except Exception:
        return None
    return u if _check_login(u, p) else None


def _who(request):
    return _session_ok(request.cookies.get("vh_session")) or _basic(request)


app = FastAPI(title="Valheim panel")

# The login screen and the login call are the only things reachable without a session.
OPEN_PATHS = {"/", "/icon.svg", "/api/login", "/api/logout"}


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Anything that reaches here is a panel bug; it belongs in the log next to the action
    # that triggered it, not only in uvicorn's traceback.
    _log("error.unhandled", ok=False, path=request.url.path, error=f"{type(exc).__name__}: {exc}"[:300])
    return JSONResponse({"detail": "Panel error — see the Log tab, panel log"}, status_code=500)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path in OPEN_PATHS or _who(request):
        return await call_next(request)
    return JSONResponse({"detail": "Bad credentials"}, status_code=401,
                        headers={"WWW-Authenticate": "Basic"})


class Login(BaseModel):
    user: str = ""
    password: str = ""


@app.post("/api/login")
def login(l: Login, request: Request, response: Response):
    if not _check_login(l.user, l.password):
        raise HTTPException(401, "Wrong user or password")
    exp = int(time.time()) + SESSION_DAYS * 86400
    response.set_cookie("vh_session", f"{l.user}|{exp}|{_sign(l.user, exp)}",
                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto") == "https")
    return {"ok": True, "user": l.user}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("vh_session")
    return {"ok": True}


def _save_panel_env(cfg):
    _write(VH_PANEL_ENV, "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in cfg.items()),
           mode=0o600, own="root:root")


class Auth(BaseModel):
    user: str = "admin"
    password: str = ""


@app.post("/api/panel/auth")
def panel_auth(a: Auth):
    """Change the panel login. Credentials are read per request, so no restart is needed."""
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", a.user):
        raise HTTPException(400, "User: 3-32 chars, letters, digits, _ . -")
    if len(a.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if a.password == PANEL_DEFAULT_PASS:
        raise HTTPException(400, "That is the default password — pick another one")
    cfg = _env_file(VH_PANEL_ENV)
    cfg["PANEL_USER"], cfg["PANEL_PASS"] = a.user, a.password
    _save_panel_env(cfg)
    _log("panel.login_changed", user=a.user)     # never the password
    return {"ok": True, "user": a.user}


VH_LOG = Path(os.environ.get("VH_LOG", f"{VH_DIR}/panel.log"))
LOG_KEEP = 4000


def _log(action, ok=True, **fields):
    """One JSON line per action the panel takes. This is the file to open when someone
    says "the mod did not install" — uvicorn's journal only shows the HTTP status."""
    try:
        rec = {"ts": int(time.time()), "action": action, "ok": ok}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with VH_LOG.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if VH_LOG.stat().st_size > 2_000_000:
            keep = VH_LOG.read_text(errors="replace").splitlines()[-LOG_KEEP:]
            VH_LOG.write_text("\n".join(keep) + "\n")
    except Exception:
        pass          # a broken log must never break the action it was describing


def _sh(cmd, timeout=60):
    return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)


def _sh_ok(cmd, timeout=60):
    r = _sh(cmd, timeout)
    if r.returncode != 0:
        raise HTTPException(502, ((r.stderr or r.stdout).strip() or "command failed")[:300])
    return r.stdout


def _write(path, text, mode=0o644, own="valheim:valheim"):
    p = Path(path)
    p.write_text(text)
    os.chmod(p, mode)
    if own:
        _sh(f"chown {own} {shlex.quote(str(p))}")


def _ts(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except Exception:
        return None


VH_LOG_FILTER = ("grep -E 'Got connection|Got handshake|Closing socket|ZDOID|Connections [0-9]|"
                 "Valheim version|join code'")
VH_STATUS_SH = f"""
echo '@state'; systemctl is-active valheim
date +%s
date -d "$(systemctl show valheim -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0
echo '@env'; cat {VH_ENV} 2>/dev/null
echo '@panel'; cat {VH_PANEL_ENV} 2>/dev/null | grep -v PANEL_PASS
echo '@lists'; for f in {' '.join(VH_LISTS.values())}; do echo "#$f"; cat {VH_DATA}/$f 2>/dev/null; done
echo '@worlds'; ls -l --time-style=+%s {VH_WORLDS} 2>/dev/null
echo '@backups'; ls -l --time-style=+%s {VH_BACKUPS} 2>/dev/null
echo '@timers'; for t in {' '.join(VH_TIMERS.values())}; do \
  n=$(systemctl list-timers --all --no-pager $t 2>/dev/null | awk 'NR==2 && $1!="-"{{print $1,$2,$3,$4}}'); \
  echo "$t $(systemctl is-enabled $t 2>/dev/null) $(systemctl is-active $t 2>/dev/null) $(date -d "$n" +%s 2>/dev/null || echo 0)"; done
echo '@disk'; df -B1 --output=used,avail {VH_DIR} 2>/dev/null | tail -1
echo '@machine'; cat /proc/loadavg; nproc; awk '/MemTotal|MemAvailable/{{print $2}}' /proc/meminfo; cut -d' ' -f1 /proc/uptime
echo '@log'; journalctl -u valheim -o short-iso --no-pager | {VH_LOG_FILTER} | tail -n 4000
"""


def _sections(out):
    sec, cur = {}, None
    for ln in out.splitlines():
        if ln.startswith("@"):
            cur = ln[1:]
            sec[cur] = []
        elif cur:
            sec[cur].append(ln)
    return sec


def _events(lines):
    """(epoch, kind, value) from the server log."""
    for ln in lines:
        ts, _, rest = ln.partition(" ")
        t = _ts(ts)
        if t is None:
            continue
        m = re.search(r"Valheim version: (\S+)", rest)
        if m:
            yield t, "boot", m.group(1)
            continue
        m = re.search(r"Got (?:connection|handshake from client)\s*(?:SteamID|PlayFabID)?\s*(\S+)", rest)
        if m:
            yield t, "join", m.group(1)
            continue
        m = re.search(r"Closing socket\s*(\S*)", rest)
        if m:
            yield t, "leave", m.group(1)
            continue
        m = re.search(r"Got character ZDOID from (.+?) : ", rest)
        if m:
            yield t, "name", m.group(1).strip()
            continue
        m = re.search(r"Connections (\d+)", rest)
        if m:
            yield t, "count", m.group(1)
            continue
        m = re.search(r"join code (\w+)", rest)
        if m:
            yield t, "joincode", m.group(1)


def _scan(lines):
    """One pass over the log: who is connected now + events for the history store.

    `Connections N` is the authoritative count but the server prints it only every
    ~10 minutes, so the online list is built from events and the counter is reported
    separately — the UI shows the disagreement instead of hiding it.

    The player name is matched to a connection FIFO, because the `Got character ZDOID`
    line carries no player id. Valheim has no RCON; without a server-side mod this is
    as precise as it gets.
    """
    conns, hist, count, count_ts, version, joincode = [], [], None, None, None, None
    for t, kind, val in _events(lines):
        if kind == "boot":
            for c in conns:
                hist.append((t, "leave", c))
            conns, version, joincode = [], val, None
        elif kind == "join":
            c = {"id": val, "name": None, "since": t}
            conns.append(c)
            hist.append((t, "join", c))
        elif kind == "leave":
            c = next((x for x in conns if x["id"] == val), None) or (conns[0] if conns else None)
            if c:
                conns.remove(c)
                hist.append((t, "leave", c))
        elif kind == "name":
            c = next((x for x in conns if not x["name"]), None)
            if c:
                c["name"] = val
                hist.append((t, "name", c))
        elif kind == "count":
            count, count_ts = int(val), t
        elif kind == "joincode":
            joincode = val
    return conns, hist, count, count_ts, version, joincode


def _history(hist):
    """Persistent login history — the journal rotates, the player list should not."""
    try:
        st = json.loads(VH_STORE.read_text())
    except Exception:
        st = {}
    players, last = st.get("players", {}), st.get("last_ts", 0)
    newest = last
    for t, kind, c in hist:
        newest = max(newest, t)
        if t <= last:
            continue
        p = players.setdefault(c["id"], {"id": c["id"], "name": None, "first": t, "last": t, "sessions": 0})
        if kind == "join":
            p["sessions"] = p.get("sessions", 0) + 1
        if kind == "name" and c["name"]:
            p["name"] = c["name"]
        p["first"] = min(p.get("first", t), t)
        p["last"] = max(p.get("last", t), t)
    try:
        VH_STORE.write_text(json.dumps({"players": players, "last_ts": newest}))
    except Exception:
        pass
    return sorted(players.values(), key=lambda p: p["last"], reverse=True)[:200]


def _parse_env(lines):
    env = {}
    for ln in lines:
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            try:
                parts = shlex.split(v)
            except ValueError:
                parts = [v]
            env[k.strip()] = parts[0] if parts else ""
    mods = {}
    for m in (env.get("MODIFIERS") or "").split():
        if ":" in m:
            k, v = m.split(":", 1)
            mods[k] = v
    return {"name": env.get("NAME", ""), "world": env.get("WORLD", ""),
            "password": env.get("PASSWORD", ""), "port": int(env.get("PORT") or 2456),
            "public": env.get("PUBLIC") == "1", "crossplay": env.get("CROSSPLAY") == "1",
            "preset": env.get("PRESET", ""), "modifiers": mods,
            "keys": (env.get("SETKEYS") or "").split()}


def _ls(lines):
    out = []
    for ln in lines:
        f = ln.split(None, 6)
        if len(f) == 7 and f[0].startswith("-"):
            out.append({"name": f[6], "size": int(f[4]), "mtime": int(f[5])})
    return out


@app.get("/icon.svg")
def icon():
    return Response((HERE / "icon.svg").read_bytes(), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # the page itself decides: logged in -> panel, otherwise our own login screen
    return (HERE / ("index.html" if _who(request) else "login.html")).read_text()


@app.get("/api/valheim")
def status():
    sec = _sections(_sh(VH_STATUS_SH, timeout=90).stdout)
    state = sec.get("state", ["", "0", "0"])
    active = state[0].strip() == "active"
    uptime = None
    try:
        # wall clock, not monotonic: inside an LXC /proc/uptime is the container's,
        # while systemd counts from the host boot — the difference came out negative
        now, started = int(state[1]), int(state[2])
        uptime = now - started if active and started else None
    except Exception:
        pass

    lists, cur = {k: [] for k in VH_LISTS}, None
    rev = {v: k for k, v in VH_LISTS.items()}
    for ln in sec.get("lists", []):
        if ln.startswith("#"):
            cur = rev.get(ln[1:].strip())
        elif cur and ln.strip() and not ln.strip().startswith("//"):
            lists[cur].append(ln.strip())

    files = _ls(sec.get("worlds", []))
    dbs = {f["name"][:-3]: f for f in files if f["name"].endswith(".db")}
    worlds = sorted(({"name": f["name"][:-4],
                      "size": dbs.get(f["name"][:-4], {}).get("size", 0),
                      "mtime": max(f["mtime"], dbs.get(f["name"][:-4], {}).get("mtime", 0))}
                     for f in files if f["name"].endswith(".fwl") and "_backup_auto-" not in f["name"]),
                    key=lambda w: w["mtime"], reverse=True)
    backups = sorted((f for f in _ls(sec.get("backups", [])) if VH_BAK_RE.match(f["name"])),
                     key=lambda b: b["mtime"], reverse=True)

    timers = []
    for name, unit in VH_TIMERS.items():
        for ln in sec.get("timers", []):
            f = ln.split()
            if f and f[0] == unit:
                nxt = int(f[3]) if len(f) > 3 and f[3].isdigit() and f[3] != "0" else None
                timers.append({"name": name, "unit": unit, "enabled": len(f) > 1 and f[1] == "enabled",
                               "active": len(f) > 2 and f[2] == "active", "next": nxt})
    disk = (sec.get("disk") or [""])[0].split()
    m = sec.get("machine", [])
    machine = None
    try:
        la = m[0].split()
        machine = {"load": [float(la[0]), float(la[1]), float(la[2])], "procs": la[3],
                   "cores": int(m[1]), "mem_total": int(m[2]) * 1024, "mem_avail": int(m[3]) * 1024,
                   "uptime": int(float(m[4]))}
    except Exception:
        pass
    conns, hist, count, count_ts, version, joincode = _scan(sec.get("log", []))
    settings = _parse_env(sec.get("env", []))
    panel_cfg = _env_file(VH_PANEL_ENV)
    settings["panel_port"] = int(panel_cfg.get("PANEL_PORT") or 2460)
    settings["panel_user"] = panel_cfg.get("PANEL_USER", "admin")  # password never leaves the box
    settings["panel_default_pass"] = panel_cfg.get("PANEL_PASS") == PANEL_DEFAULT_PASS
    return {"active": active, "uptime": uptime, "version": version,
            "players": len(conns), "online": conns, "joincode": joincode,
            "connections": {"count": count, "ts": count_ts},
            "history": _history(hist), "lists": lists, "settings": settings,
            "worlds": worlds, "backups": [b["name"] for b in backups], "backups_full": backups,
            "timers": timers,
            "disk": {"used": int(disk[0]), "avail": int(disk[1])} if len(disk) == 2 else None,
            "machine": machine,
            "options": {"presets": VH_PRESETS, "modifiers": VH_MODIFIERS, "keys": VH_KEYS}}


# ---------- summary / connectivity ----------
# What can honestly be answered from inside the container:
#  - does the game server answer the Steam query protocol at all (A2S on port+1),
#  - what the public IP is, and whether it is one a port forward can ever reach,
#  - whether Steam's master server sees the server at that public IP — that is the
#    same evidence a player on the internet has, and the only external probe available
#    without paying a third party to knock on the port.
# A "port is open" claim based on a local check would be a lie, so it is not made.
def _a2s_info(host, port, timeout=2.0):
    req = b"\xff\xff\xff\xffTSource Engine Query\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(req, (host, port))
        data, _ = s.recvfrom(4096)
        if data[4:5] == b"A":  # challenge — resend with the token
            s.sendto(req + data[5:9], (host, port))
            data, _ = s.recvfrom(4096)
        if data[4:5] != b"I":
            return None
        i = 6  # 4 bytes header + 'I' + protocol byte

        def take(buf, i):
            j = buf.index(b"\x00", i)
            return buf[i:j].decode("utf-8", "replace"), j + 1

        name, i = take(data, i)
        mapname, i = take(data, i)
        _folder, i = take(data, i)
        game, i = take(data, i)
        i += 2  # app id
        return {"name": name, "map": mapname, "game": game,
                "players": data[i], "max": data[i + 1]}
    except Exception:
        return None
    finally:
        s.close()


def _get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ip_kind(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if a.is_private:
        return "private"
    if a in ipaddress.ip_network("100.64.0.0/10"):  # carrier-grade NAT
        return "cgnat"
    return "public"


@app.get("/api/valheim/summary")
def summary():
    env = _parse_env(Path(VH_ENV).read_text().splitlines())
    game, query, panel_port = env["port"], env["port"] + 1, int(_env_file(VH_PANEL_ENV).get("PANEL_PORT") or 2460)
    lan = _sh("hostname -I").stdout.split()
    lan_ip = lan[0] if lan else None
    hostname = _sh("hostname").stdout.strip()
    # the game port only appears once the world has finished loading — on a first start
    # that is a good minute, and calling that "broken" would be wrong
    age = _sh('date +%s; date -d "$(systemctl show valheim -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0').stdout.split()
    starting = False
    try:
        starting = int(age[1]) > 0 and int(age[0]) - int(age[1]) < 120
    except Exception:
        pass
    listen = _sh("ss -ulnH; ss -tlnH").stdout

    a2s = _a2s_info("127.0.0.1", query)
    public_ip = None
    for url in ("https://api.ipify.org?format=json", "https://ipinfo.io/json"):
        try:
            public_ip = _get_json(url).get("ip")
            break
        except Exception:
            continue
    kind = _ip_kind(public_ip) if public_ip else "unknown"

    # Steam knows about a server because the server registered itself over an outbound
    # connection. Measured on a container whose port was NOT forwarded: Steam still listed
    # it. So this says "players can find it in the server list", never "the port is open".
    steam = {"state": "unknown", "detail": ""}
    if not env["public"]:
        steam["detail"] = ("Not listed on purpose (public is off), so Steam has nothing to "
                           "register. Players join by address, not from the server list.")
    elif public_ip:
        try:
            data = _get_json(f"https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/?addr={public_ip}")
            servers = (data.get("response") or {}).get("servers") or []
            mine = [s for s in servers if str(s.get("gameport")) == str(game)]
            steam["state"] = "ok" if mine else "bad"
            steam["detail"] = (f"Steam has it registered at {public_ip}:{game} — it shows up in the "
                               "server list. This does not prove the port is forwarded."
                               if mine else
                               f"Steam has nothing on port {game} at {public_ip}. Registration takes a "
                               "few minutes after a start; if it stays empty the server cannot reach Steam.")
        except Exception as e:
            steam["detail"] = f"Could not ask Steam ({e}). No verdict rather than a guessed one."

    crossplay = env["crossplay"]
    game_bound = f":{game}" in listen
    checks = [
        {"key": "process", "label": "Game server process",
         "state": "ok" if _sh("systemctl is-active valheim").stdout.strip() == "active" else "bad",
         "detail": "systemd unit valheim"},
        {"key": "join_mode", "label": "How players join",
         "state": "unknown" if crossplay else "ok",
         "detail": ("Crossplay is on: the server talks through the PlayFab relay and does not "
                    f"open port {game} at all. Players use the crossplay server list / join code — "
                    "a router forward changes nothing in this mode. Turn crossplay off in Settings "
                    "if you want people to connect by address."
                    if crossplay else
                    f"Crossplay is off: players connect straight to the address on port {game}.")},
        {"key": "a2s", "label": f"Server answers queries on {query}",
         "state": "ok" if a2s else ("unknown" if not env["public"] else "bad"),
         "detail": (f"replied: {a2s['name']} · {a2s['players']}/{a2s['max']} players" if a2s
                    else "the query responder only runs when the server is listed publicly"
                    if not env["public"] else "still starting up" if starting else
                    "no reply — normal for the first ~30 s after a start, otherwise the server is not ready")},
        {"key": "bind", "label": "Ports open inside the container",
         "state": ("ok" if (f":{panel_port}" in listen and (game_bound or crossplay))
                   else "unknown" if starting else "bad"),
         "detail": (("game " + str(game) + " bound · " if game_bound else
                     f"game {game} not bound yet — the world is still loading · " if starting else
                     f"game {game} not bound (expected with crossplay on) · ")
                    + " ".join(sorted({ln.split()[3] for ln in listen.splitlines()
                                       if len(ln.split()) > 3 and str(game) in ln.split()[3]
                                       or len(ln.split()) > 3 and str(panel_port) in ln.split()[3]}))[:200])},
        {"key": "public_ip", "label": "Public address of your connection",
         "state": {"public": "ok", "cgnat": "bad", "private": "bad"}.get(kind, "unknown"),
         "detail": {"public": f"{public_ip} — a forward can reach you here",
                    "cgnat": f"{public_ip} is carrier NAT (100.64/10) — no forward can ever work, "
                             "ask your ISP for a public address or use a VPN/tunnel",
                    "private": f"{public_ip} is a private address — there is another NAT above you",
                    "unknown": "could not determine the public address"}[kind]},
        {"key": "steam", "label": "Listed in the Steam server browser",
         "state": steam["state"], "detail": steam["detail"]},
        {"key": "forward", "label": "Router forward (inbound reachability)",
         "state": "unknown",
         "detail": ("Not applicable while crossplay is on — nothing listens on the game port."
                    if crossplay else
                    f"Cannot be proven from inside this network: every probe from here leaves and "
                    f"comes back through your own NAT. Forward UDP {game}-{game + 2} to {lan_ip} on the "
                    f"router, then have someone outside connect to {public_ip}:{game} — that is the "
                    "only honest confirmation.")},
    ]
    return {
        "join": {"lan": f"{lan_ip}:{game}" if lan_ip else None,
                 "public": f"{public_ip}:{game}" if public_ip else None,
                 "panel": f"http://{lan_ip}:{panel_port}" if lan_ip else None,
                 "password": env["password"]},
        "ports": {"game": [game, game + 1, game + 2], "panel": panel_port, "query": query},
        "public_listing": env["public"], "crossplay": env["crossplay"],
        "a2s": a2s, "checks": checks, "hostname": hostname,
    }


@app.post("/api/valheim/action/{action}")
def action(action: str):
    cmd = VH_ACTIONS.get(action)
    if not cmd:
        raise HTTPException(400, "Unknown action")
    out = _sh_ok(cmd[0], timeout=cmd[1]).strip()[-400:]
    _log("server." + action, out=out or None)
    return {"ok": True, "out": out}


@app.get("/api/panel/log")
def panel_log(n: int = 200, kind: str = "all"):
    n = max(10, min(1000, n))
    try:
        lines = VH_LOG.read_text(errors="replace").splitlines()[-2000:]
    except FileNotFoundError:
        return {"entries": [], "file": str(VH_LOG)}
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if kind == "mods" and not rec.get("action", "").startswith(("mods.", "config.")):
            continue
        if kind == "errors" and rec.get("ok", True):
            continue
        out.append(rec)
    return {"entries": out[-n:][::-1], "file": str(VH_LOG)}


@app.get("/api/valheim/log")
def log(n: int = 200):
    n = max(50, min(1000, n))
    out = _sh_ok(f"journalctl -u valheim -o short-iso --no-pager -n 4000 | grep -vF 'PlayFab reconnect' | tail -n {n}")
    return {"log": out}


class Ids(BaseModel):
    ids: list = []


@app.post("/api/valheim/lists/{kind}")
def list_save(kind: str, body: Ids):
    fn = VH_LISTS.get(kind)
    if not fn:
        raise HTTPException(404, "No such list")
    ids, seen = [], set()
    for i in body.ids:
        i = str(i).strip()
        if not i or i in seen:
            continue
        if not VH_ID_RE.match(i):
            raise HTTPException(400, f"Invalid id: {i[:40]}")
        seen.add(i)
        ids.append(i)
    _write(f"{VH_DATA}/{fn}", f"// {kind} — managed by the Valheim panel\n" + "\n".join(ids) + "\n")
    _log("list.save", kind=kind, count=len(ids), ids=ids[:20])
    return {"ok": True, "count": len(ids)}


class Settings(BaseModel):
    name: str = "Valheim"
    world: str = "Dedicated"
    password: str = ""
    port: int = 2456
    public: bool = False
    crossplay: bool = True
    preset: str = ""
    modifiers: dict = {}
    keys: list = []
    panel_port: int = 2460
    restart: bool = False


def _save_settings(s: Settings):
    if not VH_NAME_RE.match(s.name) or not VH_NAME_RE.match(s.world):
        raise HTTPException(400, "Server and world name: letters, digits, space, _ and - (max 40)")
    if not 1024 <= s.port <= 65530:
        raise HTTPException(400, "Game port out of range 1024-65530")
    if not 1024 <= s.panel_port <= 65535:
        raise HTTPException(400, "Panel port out of range 1024-65535")
    # the game needs three consecutive ports; overlapping the panel would break both
    if s.port <= s.panel_port <= s.port + 2:
        raise HTTPException(400, f"Panel port collides with the game ({s.port}-{s.port + 2})")
    # rules of the game server itself — breaking them stops the server from starting
    if s.password:
        if len(s.password) < 5:
            raise HTTPException(400, "Password must be at least 5 characters")
        if s.world.lower() in s.password.lower() or s.name.lower() in s.password.lower():
            raise HTTPException(400, "Password cannot contain the server or world name (the game rejects it)")
    elif s.public:
        raise HTTPException(400, "A public server must have a password")
    if s.preset not in VH_PRESETS:
        raise HTTPException(400, "Unknown preset")
    mods = []
    for k, v in (s.modifiers or {}).items():
        if not v:
            continue
        if k not in VH_MODIFIERS or v not in VH_MODIFIERS[k]:
            raise HTTPException(400, f"Unknown modifier: {k}={v}")
        mods.append(f"{k}:{v}")
    keys = [k for k in s.keys if k in VH_KEYS]
    env = {"NAME": s.name, "WORLD": s.world, "PASSWORD": s.password, "PORT": str(s.port),
           "PUBLIC": "1" if s.public else "0", "CROSSPLAY": "1" if s.crossplay else "0",
           "PRESET": s.preset, "MODIFIERS": " ".join(sorted(mods)), "SETKEYS": " ".join(keys)}
    _write(VH_ENV, "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()), mode=0o640)

    panel = _env_file(VH_PANEL_ENV)
    port_changed = int(panel.get("PANEL_PORT") or 2460) != s.panel_port
    if port_changed:
        panel["PANEL_PORT"] = str(s.panel_port)
        _save_panel_env(panel)
    if s.restart:
        _sh_ok("systemctl restart valheim", timeout=180)
    if port_changed:
        # restarting our own unit from inside it would kill this request mid-flight,
        # so hand the job to systemd and answer first
        _sh("systemd-run --on-active=2 --unit=valheim-panel-restart systemctl restart valheim-panel")
    _log("settings.save", world=s.world, port=s.port, panel_port=s.panel_port,
         public=s.public, crossplay=s.crossplay, preset=s.preset or None,
         modifiers=mods or None, keys=keys or None, restarted=s.restart)
    return {"ok": True, "restarted": s.restart, "panel_port_changed": port_changed}


@app.post("/api/valheim/settings")
def settings_save(s: Settings):
    return _save_settings(s)


def _world_ok(name):
    if not VH_NAME_RE.match(name or ""):
        raise HTTPException(400, "Invalid world name")
    return shlex.quote(name)


class World(BaseModel):
    name: str
    restart: bool = True


@app.post("/api/valheim/worlds/activate")
def world_activate(w: World):
    q = _world_ok(w.name)
    if _sh(f"test -f {VH_WORLDS}/{q}.fwl").returncode != 0:
        raise HTTPException(404, "No such world")
    cur = _parse_env(Path(VH_ENV).read_text().splitlines())
    cur["world"] = w.name
    cur["restart"] = w.restart
    cur["panel_port"] = int(_env_file(VH_PANEL_ENV).get("PANEL_PORT") or 2460)
    return _save_settings(Settings(**cur))


@app.delete("/api/valheim/worlds/{name}")
def world_delete(name: str):
    q = _world_ok(name)
    cur = _parse_env(Path(VH_ENV).read_text().splitlines())
    if cur["world"] == name:
        raise HTTPException(409, "That is the active world — switch to another one first")
    _sh_ok(f"cd {VH_WORLDS} && rm -f {q}.db {q}.fwl {q}.db.old {q}.fwl.old {q}_backup_auto-*.db {q}_backup_auto-*.fwl")
    _log("world.delete", world=name)
    return {"ok": True}


@app.get("/api/valheim/worlds/{name}/download")
def world_download(name: str):
    q = _world_ok(name)
    data = base64.b64decode(_sh_ok(f"tar czf - -C {VH_WORLDS} {q}.db {q}.fwl | base64", timeout=120))
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})


@app.post("/api/valheim/worlds/upload/{filename}")
async def world_upload(filename: str, data: bytes = Body(...)):
    # raw body instead of multipart — keeps python-multipart out of the dependency list
    base, _, ext = filename.rpartition(".")
    if ext not in ("db", "fwl") or not VH_NAME_RE.match(base):
        raise HTTPException(400, f"Only .db and .fwl files with a plain name: {filename}")
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 300 * 1024 * 1024:
        raise HTTPException(413, "File too large")
    p = Path(VH_WORLDS) / filename
    p.write_bytes(data)
    _sh(f"chown valheim:valheim {shlex.quote(str(p))}")
    _log("world.upload", file=filename, size=len(data))
    return {"ok": True, "saved": filename}


def _bak_ok(fn):
    if not VH_BAK_RE.match(fn or ""):
        raise HTTPException(400, "Invalid backup name")
    return fn


@app.post("/api/valheim/backups/{fn}/restore")
def backup_restore(fn: str):
    _bak_ok(fn)
    # snapshot the current world first — restoring by mistake has to be reversible
    _sh_ok(f"{VH_DIR}/backup.sh", timeout=120)
    _sh_ok(f"systemctl stop valheim && tar xzf {VH_BACKUPS}/{fn} -C {VH_WORLDS} "
           f"&& chown -R valheim:valheim {VH_WORLDS} && systemctl start valheim", timeout=240)
    _log("backup.restore", file=fn)
    return {"ok": True}


@app.delete("/api/valheim/backups/{fn}")
def backup_delete(fn: str):
    _sh_ok(f"rm -f {VH_BACKUPS}/{_bak_ok(fn)}")
    _log("backup.delete", file=fn)
    return {"ok": True}


@app.get("/api/valheim/backups/{fn}/download")
def backup_download(fn: str):
    data = Path(f"{VH_BACKUPS}/{_bak_ok(fn)}").read_bytes()
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/valheim/timers/{name}/{state}")
def timer(name: str, state: str):
    unit = VH_TIMERS.get(name)
    if not unit or state not in ("on", "off"):
        raise HTTPException(400, "Unknown timer or state")
    _sh_ok(f"systemctl {'enable --now' if state == 'on' else 'disable --now'} {unit}")
    _log("timer." + state, timer=name)
    return {"ok": True}


# ---------- mods (Thunderstore) ----------
# The share code from Thunderstore Mod Manager / r2modman is a profile id. Thunderstore
# hands the profile back over the legacyprofile API, so no mod manager is involved here —
# and the versions in the code are exactly the versions the players already run, which is
# the whole point: a mismatched version bounces the player at the door.
TS = "https://thunderstore.io"
VH_SERVER = f"{VH_DIR}/server"
VH_PLUGINS = f"{VH_SERVER}/BepInEx/plugins"
VH_MODS_JSON = Path(f"{VH_DIR}/mods.json")
BEPINEX = ("denikson", "BepInExPack_Valheim")
MOD_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
MOD_VER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _mods_state():
    try:
        return json.loads(VH_MODS_JSON.read_text())
    except Exception:
        return {"profile_code": None, "profile_name": None, "mods": {}}


def _mods_save(st):
    VH_MODS_JSON.write_text(json.dumps(st, indent=1))


def _ts_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-proxmox-panel"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _ts_package(ns, name):
    try:
        return json.loads(_ts_get(f"{TS}/api/experimental/package/{ns}/{name}/"))
    except Exception as e:
        raise HTTPException(404, f"Thunderstore does not know {ns}/{name} ({e})")


def _unpack(data, dest, strip=None):
    """Unpack a Thunderstore zip. `strip` drops a leading folder the package wraps itself in."""
    import zipfile
    import io
    dest = Path(dest)
    skip = {"manifest.json", "icon.png", "readme.md", "changelog.md", "license", "license.txt"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if strip and name.startswith(strip):
                name = name[len(strip):]
            if not name or name.split("/")[-1].lower() in skip and "/" not in name:
                continue
            # a package that ships a plugins/ folder means "put my content there"
            if name.startswith("plugins/"):
                name = name[len("plugins/"):]
            out = dest / name
            if not str(out.resolve()).startswith(str(dest.resolve())):
                raise HTTPException(400, f"Package tries to escape its directory: {info.filename}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(info))


def _install_bepinex():
    pkg = _ts_package(*BEPINEX)
    ver = pkg["latest"]["version_number"]
    data = _ts_get(pkg["latest"]["download_url"], timeout=180)
    _unpack(data, VH_SERVER, strip="BepInExPack_Valheim/")
    _sh(f"chown -R valheim:valheim {VH_SERVER}/BepInEx {VH_SERVER}/doorstop_libs "
        f"{VH_SERVER}/unstripped_corlib 2>/dev/null; chmod -R u+rwX {VH_SERVER}/BepInEx")
    return ver


def _install_mod(ns, name, version=None):
    if not (MOD_NAME_RE.match(ns) and MOD_NAME_RE.match(name)):
        raise HTTPException(400, f"Odd package name: {ns}/{name}")
    # the experimental package endpoint only carries the latest version, and the download
    # URL is a stable pattern — so an explicit version needs no lookup at all
    if version:
        if not MOD_VER_RE.match(version):
            raise HTTPException(400, f"Odd version: {version}")
        url = f"{TS}/package/download/{ns}/{name}/{version}/"
    else:
        latest = _ts_package(ns, name)["latest"]
        version, url = latest["version_number"], latest["download_url"]
    full = f"{ns}-{name}"
    try:
        data = _ts_get(url, timeout=180)
    except Exception as e:
        raise HTTPException(404, f"{ns}/{name} {version} could not be downloaded ({e})")
    target = Path(VH_PLUGINS) / full
    _sh(f"rm -rf {shlex.quote(str(target))}")
    _unpack(data, target)
    _sh(f"chown -R valheim:valheim {shlex.quote(str(target))}")
    return {"full_name": full, "version": version, "size": len(data)}


def _profile(code):
    """Expand a Thunderstore Mod Manager share code into a mod list."""
    import base64 as _b
    import zipfile
    import io
    import yaml
    if not re.match(r"^[A-Za-z0-9-]{8,64}$", code or ""):
        raise HTTPException(400, "That does not look like a share code")
    try:
        raw = _ts_get(f"{TS}/api/experimental/legacyprofile/get/{code}/", timeout=60).decode()
    except Exception as e:
        raise HTTPException(404, f"Thunderstore has no profile under that code ({e})")
    body = raw.split("\n", 1)[1] if raw.startswith("#") else raw
    with zipfile.ZipFile(io.BytesIO(_b.b64decode(body))) as z:
        r2x = yaml.safe_load(z.read("export.r2x"))
    mods = []
    for m in r2x.get("mods") or []:
        v = m.get("versionNumber") or m.get("version") or {}
        mods.append({"full_name": m["name"],
                     "version": f"{v.get('major', 0)}.{v.get('minor', 0)}.{v.get('patch', 0)}",
                     "enabled": m.get("enabled", True)})
    return {"name": r2x.get("profileName") or "profile", "code": code, "mods": mods}


@app.get("/api/mods")
def mods_state():
    st = _mods_state()
    installed = _sh(f"ls -1 {VH_PLUGINS} 2>/dev/null").stdout.split()
    return {"bepinex": _sh(f"test -d {VH_SERVER}/BepInEx && echo yes").stdout.strip() == "yes",
            "bepinex_version": st.get("bepinex_version"),
            "profile_code": st.get("profile_code"), "profile_name": st.get("profile_name"),
            "mods": [{"full_name": d, **st.get("mods", {}).get(d, {})} for d in sorted(installed)]}


@app.get("/api/mods/profile/{code}")
def mods_profile(code: str):
    return _profile(code)


class ModPick(BaseModel):
    code: str = ""
    mods: list = []          # ["ns-Name" ...] or [{"full_name":..,"version":..}]
    restart: bool = True


@app.post("/api/mods/install")
def mods_install(p: ModPick):
    st = _mods_state()
    # mods can corrupt a save for good, so the world goes into a backup before the first one
    if not st.get("mods"):
        _sh_ok(f"{VH_DIR}/backup.sh", timeout=120)
    # The server is stopped for the whole operation: writing into BepInEx/plugins under a
    # running server leaves the old assemblies loaded, which looks exactly like "the mod
    # did not install".
    was_running = _sh("systemctl is-active valheim").stdout.strip() == "active"
    if was_running:
        _sh_ok("systemctl stop valheim", timeout=180)
    report = {"backup": not st.get("mods"), "stopped": was_running, "installed": [], "failed": []}
    if not Path(f"{VH_SERVER}/BepInEx").exists():
        st["bepinex_version"] = _install_bepinex()
        report["bepinex"] = st["bepinex_version"]
    for m in p.mods:
        full, ver = (m, None) if isinstance(m, str) else (m.get("full_name"), m.get("version"))
        ns, _, name = (full or "").partition("-")
        try:
            got = _install_mod(ns, name, ver)
            st.setdefault("mods", {})[got["full_name"]] = {"version": got["version"]}
            report["installed"].append(got)
        except HTTPException as e:
            report["failed"].append({"full_name": full, "error": e.detail})
    if p.code:
        prof = None
        try:
            prof = _profile(p.code)
        except HTTPException:
            pass
        st["profile_code"] = p.code
        st["profile_name"] = prof["name"] if prof else None
    _mods_save(st)
    if p.restart or was_running:
        _sh_ok("systemctl start valheim", timeout=180)
        report["restarted"] = True
    _log("mods.install", ok=not report["failed"], code=p.code or None,
         installed=[m["full_name"] + " " + m["version"] for m in report["installed"]],
         failed=report["failed"] or None, bepinex=report.get("bepinex"))
    return report


class ModClear(BaseModel):
    start: bool = True


@app.post("/api/mods/clear")
def mods_clear(c: ModClear):
    """Back to vanilla: snapshot the world, stop, wipe BepInEx and every plugin."""
    _sh_ok(f"{VH_DIR}/backup.sh", timeout=120)
    _sh_ok("systemctl stop valheim", timeout=180)
    _sh_ok(f"cd {VH_SERVER} && rm -rf BepInEx doorstop_libs unstripped_corlib "
           f"doorstop_config.ini start_game_bepinex.sh start_server_bepinex.sh .doorstop_version")
    st = _mods_state()
    st["mods"], st["profile_code"], st["profile_name"], st["bepinex_version"] = {}, None, None, None
    _mods_save(st)
    if c.start:
        _sh_ok("systemctl start valheim", timeout=180)
    _log("mods.clear", removed=list(st.get("mods", {})) or None, started=c.start)
    return {"ok": True, "started": c.start}


@app.delete("/api/mods/{full_name}")
def mods_remove(full_name: str, restart: bool = True):
    if not re.match(r"^[A-Za-z0-9_-]{1,80}$", full_name):
        raise HTTPException(400, "Odd package name")
    _sh_ok(f"rm -rf {shlex.quote(VH_PLUGINS + '/' + full_name)}")
    st = _mods_state()
    st.get("mods", {}).pop(full_name, None)
    _mods_save(st)
    _log("mods.remove", package=full_name)
    if restart:
        _sh_ok("systemctl restart valheim", timeout=180)
    return {"ok": True}

# ---------- mod configs ----------
# BepInEx writes one .cfg per plugin on first run. Each entry carries its own description,
# type and defaults in comments above it, which is enough to build a form — so the keys are
# never typed by hand and cannot be misspelled. Only values are ever rewritten; comments,
# sections and ordering come back byte for byte.
VH_MODCFG = f"{VH_SERVER}/BepInEx/config"
VH_CFGHIST = f"{VH_MODCFG}/.history"
CFG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.(cfg|json|ya?ml|txt|ini|xml)$")
CFG_MAX = 512 * 1024
CFG_KEEP = 20


def _cfg_path(name, base=VH_MODCFG):
    if not CFG_NAME_RE.match(name or ""):
        raise HTTPException(400, "Not a config file name")
    p = Path(base) / name
    if not str(p.resolve()).startswith(str(Path(base).resolve())):
        raise HTTPException(400, "Path escapes the config directory")
    return p


def _cfg_parse(text):
    """BepInEx .cfg -> [{section, key, value, line, description, type, default, choices, range}]."""
    entries, section, desc, meta = [], "", [], {}
    for n, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if not line:
            desc, meta = [], {}
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            desc, meta = [], {}
            continue
        if line.startswith("##"):
            desc.append(line.lstrip("#").strip())
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            for label, key in (("Setting type:", "type"), ("Default value:", "default"),
                               ("Acceptable values:", "choices"), ("Acceptable value range:", "range")):
                if body.startswith(label):
                    meta[key] = body[len(label):].strip()
                    break
            else:
                desc.append(body)
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            choices = [c.strip() for c in meta.get("choices", "").split(",") if c.strip()]
            rng = None
            m = re.search(r"From\s+(-?[\d.]+)\s+to\s+(-?[\d.]+)", meta.get("range", ""))
            if m:
                rng = [m.group(1), m.group(2)]
            entries.append({"section": section, "key": k.strip(), "value": v.strip(), "line": n,
                            "description": " ".join(desc), "type": meta.get("type", ""),
                            "default": meta.get("default", ""), "choices": choices, "range": rng})
            desc, meta = [], {}
    return entries


def _cfg_apply(text, changes):
    """Rewrite only the value on each entry's own line — everything else is left alone."""
    lines = text.split("\n")
    by_line = {e["line"]: e for e in _cfg_parse(text)}
    for ch in changes:
        ln = ch.get("line")
        e = by_line.get(ln)
        if e is None or e["key"] != ch.get("key") or e["section"] != ch.get("section"):
            raise HTTPException(409, "The file changed on disk since you opened it — reload and try again")
        val = str(ch.get("value", ""))
        if "\n" in val or "\r" in val:
            raise HTTPException(400, f"{e['key']}: a value cannot span lines")
        lines[ln] = f"{e['key']} = {val}"
    return "\n".join(lines)


def _cfg_snapshot(p):
    """Keep every save, newest first, so any of them can be restored with one click."""
    if not p.exists():
        return
    d = Path(VH_CFGHIST) / p.name
    _sh_ok(f"mkdir -p {shlex.quote(str(d))}")
    (d / str(int(time.time()))).write_text(p.read_text(errors="replace"))
    old = sorted(d.iterdir(), key=lambda f: f.name, reverse=True)[CFG_KEEP:]
    for f in old:
        f.unlink(missing_ok=True)


@app.get("/api/mods/configs")
def mod_configs():
    out = _sh(f"ls -l --time-style=+%s {VH_MODCFG} 2>/dev/null").stdout
    files = [f for f in _ls(out.splitlines()) if CFG_NAME_RE.match(f["name"])]
    return {"dir": VH_MODCFG, "files": sorted(files, key=lambda f: f["name"].lower())}


@app.get("/api/mods/configs/{name}")
def mod_config_read(name: str):
    p = _cfg_path(name)
    if not p.exists():
        raise HTTPException(404, "No such config")
    if p.stat().st_size > CFG_MAX:
        raise HTTPException(413, "File too large to edit here")
    text = p.read_text(errors="replace")
    hist = []
    d = Path(VH_CFGHIST) / name
    if d.is_dir():
        hist = sorted((int(f.name) for f in d.iterdir() if f.name.isdigit()), reverse=True)
    return {"name": name, "mtime": int(p.stat().st_mtime), "history": hist,
            "form": _cfg_parse(text) if name.endswith((".cfg", ".ini")) else None,
            "content": text}


class CfgSave(BaseModel):
    changes: list = []      # [{section, key, line, value}]
    content: str = ""       # only used for files with no form (json/yaml)
    restart: bool = False


@app.post("/api/mods/configs/{name}")
def mod_config_write(name: str, body: CfgSave):
    p = _cfg_path(name)
    if not p.exists():
        raise HTTPException(404, "No such config")
    _cfg_snapshot(p)
    if body.changes:
        new = _cfg_apply(p.read_text(errors="replace"), body.changes)
    else:
        if len(body.content.encode()) > CFG_MAX:
            raise HTTPException(413, "File too large")
        new = body.content
    _write(str(p), new)
    _log("config.save", file=name, restarted=body.restart,
         changed=[f"{c.get('key')}={c.get('value')}" for c in body.changes] or None)
    if body.restart:
        _sh_ok("systemctl restart valheim", timeout=180)
    return {"ok": True, "restarted": body.restart}


@app.post("/api/mods/configs/{name}/restore/{stamp}")
def mod_config_restore(name: str, stamp: int, restart: bool = False):
    p = _cfg_path(name)
    src = Path(VH_CFGHIST) / name / str(stamp)
    if not src.exists():
        raise HTTPException(404, "No such version")
    _cfg_snapshot(p)          # the state being replaced is itself worth keeping
    _write(str(p), src.read_text(errors="replace"))
    _log("config.restore", file=name, version=stamp, restarted=restart)
    if restart:
        _sh_ok("systemctl restart valheim", timeout=180)
    return {"ok": True, "restarted": restart}
