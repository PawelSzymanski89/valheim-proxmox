"""Valheim server admin panel — runs inside the container, next to the game server.

No RCON exists in Valheim, so everything here is what a *server* admin can control:
the systemd unit, the launch arguments, the player lists, the worlds and the backups.
In-game commands (kick, spawn, weather) are done from the F5 console by a player
listed in adminlist.txt — the panel manages that list.
"""
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import grp
import pwd
import secrets
import shutil
import tempfile
import threading
import shlex
import socket
import struct
import subprocess
import time
import unicodedata
import urllib.request

import icon_badge
from datetime import datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

VH_DIR = os.environ.get("VH_DIR", "/opt/valheim")
VH_DATA = f"{VH_DIR}/data"
VH_WORLDS = f"{VH_DATA}/worlds_local"
VH_BACKUPS = f"{VH_DIR}/backups"
VH_ENV = f"{VH_DIR}/server.env"
VH_PANEL_ENV = f"{VH_DIR}/panel.env"
VH_STORE = Path(os.environ.get("VH_STORE", f"{VH_DIR}/players.json"))
HERE = Path(__file__).resolve().parent

VH_PASS_RETIRED = Path(f"{VH_DIR}/panel-pass-retired")
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
VH_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,40}\Z")
VH_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
VH_BAK_RE = re.compile(r"^world-\d{8}-\d{6}\.tar\.gz\Z")
VH_ACTIONS = {"start": ("systemctl start valheim", 60),
              "stop": ("systemctl stop valheim", 180),
              "restart": ("systemctl restart valheim", 180),
              # as the game user: root running a script is only safe if nobody else can edit it,
              # and this one only touches the game's files anyway
              "backup": (f"runuser -u valheim -- {VH_DIR}/backup.sh", 120)}


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
        if int(exp) < int(time.time()) or mac in _revoked():
            return None
        return user if hmac.compare_digest(mac, _sign(user, exp)) else None
    except Exception:
        return None


# Signed-out sessions. The cookie is self-contained, so deleting it in the browser used to be
# all a logout did - a copy taken earlier stayed good for its whole 30 days. Its signature is
# kept here until the cookie would have expired anyway.
VH_REVOKED = Path(f"{VH_DIR}/revoked-sessions.json")
_REVOKED = {"at": 0.0, "set": set()}


def _revoked():
    if time.time() - _REVOKED["at"] > 5:
        now = time.time()
        _REVOKED["set"] = {m for m, exp in _load_json(VH_REVOKED, {}).items() if exp > now}
        _REVOKED["at"] = now
    return _REVOKED["set"]


def _revoke(cookie):
    try:
        _user, exp, mac = cookie.split("|")
    except (AttributeError, ValueError):
        return
    now = time.time()
    keep = {m: e for m, e in _load_json(VH_REVOKED, {}).items() if e > now}
    keep[mac] = int(exp)
    _save_json(VH_REVOKED, keep)
    _REVOKED["at"] = 0


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
OPEN_PATHS = {"/", "/icon.svg", "/api/login", "/api/logout", "/api/public"}
# the launcher talks to these without a panel login - see _launcher_cfg for what they expose
OPEN_PREFIXES = ("/api/launcher/",)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Anything that reaches here is a panel bug; it belongs in the log next to the action
    # that triggered it, not only in uvicorn's traceback.
    _log("error.unhandled", ok=False, path=request.url.path, error=f"{type(exc).__name__}: {exc}"[:300])
    return JSONResponse({"detail": "Panel error — see the Log tab, panel log"}, status_code=500)


def _cross_site(request):
    """A state-changing request a browser sent on behalf of another site. SameSite=Lax stops
    other sites but not a sibling subdomain, and a browser that cached Basic credentials sends
    them with any cross-site form. Every current browser labels its requests with
    Sec-Fetch-Site; for one that does not, Origin is compared with Host. A request carrying
    neither - curl, a script, the launcher - is not a browser and not a CSRF vector."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    return bool(origin) and origin.split("://", 1)[-1] != request.headers.get("host", "")


@app.middleware("http")
async def headers(request: Request, call_next):
    """Anything that is not an upload has no business being large: the open login route read
    a body of any size into memory. And no other site may frame the panel - a sibling
    subdomain counts as the same site for the session cookie."""
    size = request.headers.get("content-length")
    upload = "/upload" in request.url.path or request.url.path.endswith(("/map", "/background"))
    if size and size.isdigit() and int(size) > (400 * 2**20 if upload else 2**20):
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.middleware("http")
async def guard(request: Request, call_next):
    # A locked-out address gets nothing checked at all. Checking the Basic header first
    # answered 200 for a right password and 429 for a wrong one - the lockout still leaked
    # which guess was correct.
    if request.headers.get("authorization", "").startswith("Basic "):
        rec = LOGIN_FAILS.get(_lock_key(_client_ip(request)))
        if rec and rec.get("until", 0) > time.time():
            left = int(rec["until"] - time.time())
            return JSONResponse({"detail": f"Too many attempts — try again in {left // 60 + 1} min"},
                                status_code=429, headers={"Retry-After": str(left)})
    if _cross_site(request):
        _log("panel.cross_site_refused", ok=False, path=request.url.path,
             origin=request.headers.get("origin"), site=request.headers.get("sec-fetch-site"))
        return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    if (request.url.path in OPEN_PATHS
            or request.url.path.startswith(OPEN_PREFIXES)
            or _who(request)):
        return await call_next(request)
    # A rejected Basic header is a guess like any other, and until now it was the one door
    # nobody was counting: no rate limit, no log line. A request with no credentials at all
    # is just an unauthenticated browser and is left alone.
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        ip = _client_ip(request)
        rec = LOGIN_FAILS.get(_lock_key(ip))
        if rec and rec.get("until", 0) > time.time():
            left = int(rec["until"] - time.time())
            return JSONResponse({"detail": f"Too many attempts — try again in {left // 60 + 1} min"},
                                status_code=429, headers={"Retry-After": str(left)})
        try:
            u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            u, pw = "", ""
        _login_failed(ip, u, pw, how="basic")
    # The Basic challenge only for clients that are not a browser: a browser answering it
    # pops a password box and then caches what was typed, sending it along with any
    # cross-site form post from then on. The panel's own page has its login form.
    browser = "sec-fetch-mode" in request.headers or "mozilla" in request.headers.get("user-agent", "").lower()
    return JSONResponse({"detail": "Bad credentials"}, status_code=401,
                        headers={} if browser else {"WWW-Authenticate": "Basic"})


# Rate limiting on the login. The panel sits on a public hostname; without this, a script
# can try passwords as fast as the network allows and nothing anywhere notices.
LOGIN_FAILS = {}
LOGIN_MAX = 5           # failures before a lockout
LOGIN_WINDOW = 900      # ...within this many seconds
LOGIN_BLOCK = 600       # lockout length, doubling on repeat


# Only a proxy we run ourselves may say who the client is. A reverse proxy APPENDS the
# address it sees to X-Forwarded-For, so a header the caller sent lands on the LEFT and the
# proxy's own truth on the RIGHT. Reading the leftmost value - which this did until
# 4 August 2026 - let anyone name their own address, and with it their own rate-limit
# bucket: password guessing without any lockout, against a panel that sits on a public
# name, plus a log and phone alerts full of addresses the attacker chose. Verified with
# `curl -H 'X-Forwarded-For: 9.9.9.9'` from another machine: the panel wrote 9.9.9.9.
#
# Set TRUSTED_PROXIES in the panel env to the address of your reverse proxy. Unset means
# only loopback is believed - the panel then logs the proxy's own address instead of the
# player's, which is a worse log, not an open door.
def _trusted_proxies():
    raw = _env_file(VH_PANEL_ENV).get("TRUSTED_PROXIES", "127.0.0.1,::1")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _lock_key(ip):
    """What a lockout is counted against. An IPv6 address is one of 2^64 in the /64 its owner
    holds - counting per address let anyone with IPv6 guess forever - so the /64 counts."""
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(f"{a}/64", strict=False))
        return str(a)
    except ValueError:
        return ip


def _client_ip(request):
    peer = request.client.host if request.client else "?"
    if peer not in _trusted_proxies():
        return peer
    fwd = request.headers.get("x-forwarded-for", "").strip()
    return fwd.split(",")[-1].strip() if fwd else peer


def _login_guard(ip):
    rec = LOGIN_FAILS.get(_lock_key(ip))
    if rec and rec.get("until", 0) > time.time():
        left = int(rec["until"] - time.time())
        raise HTTPException(429, f"Too many attempts — try again in {left // 60 + 1} min",
                            {"Retry-After": str(left)})


def _attempted(user, password):
    """What was typed at a failed login, for the log: the user name, and of the password only
    its length. The most common failed login is the admin's own typo, and a typo of the real
    password is most of the real password - it has no business sitting in a log file."""
    env = _env_file(VH_PANEL_ENV)
    shown = "<the real one>" if password and password == env.get("PANEL_PASS") \
        else (f"<{len(password)} characters>" if password else "<empty>")
    return {"tried_user": (user or "")[:64] or "<empty>", "tried_pass": shown}


def _login_failed(ip, user="", password="", how="form"):
    now = time.time()
    if len(LOGIN_FAILS) > 1000:            # a scan from many addresses must not grow this forever
        for k in [k for k, r in LOGIN_FAILS.items() if r["until"] < now and now - r["first"] > LOGIN_WINDOW]:
            LOGIN_FAILS.pop(k, None)
    rec = LOGIN_FAILS.setdefault(_lock_key(ip), {"count": 0, "first": now, "until": 0, "blocks": 0})
    if now - rec["first"] > LOGIN_WINDOW:
        rec.update({"count": 0, "first": now})
    rec["count"] += 1
    if rec["count"] >= LOGIN_MAX:
        rec["blocks"] += 1
        rec["until"] = now + LOGIN_BLOCK * min(6, rec["blocks"])     # 10, 20, 30 ... up to 60 min
        rec.update({"count": 0, "first": now})
        _log("panel.login_blocked", ok=False, ip=ip, how=how,
             minutes=int((rec["until"] - now) / 60), **_attempted(user, password))
        _notify("panel_login", "Panel locked out an address",
                f"{ip} kept guessing the password — blocked for {int((rec['until'] - now) / 60)} min.",
                priority="high", tags="lock")
    else:
        _log("panel.login_failed", ok=False, ip=ip, attempt=rec["count"], how=how,
             **_attempted(user, password))


class Login(BaseModel):
    user: str = ""
    password: str = ""


@app.post("/api/login")
def login(l: Login, request: Request, response: Response):
    ip = _client_ip(request)
    _login_guard(ip)
    if not _check_login(l.user, l.password):
        _login_failed(ip, l.user, l.password)
        time.sleep(1)          # a scripted guess costs a second; a human never notices
        if l.password == PANEL_DEFAULT_PASS and VH_PASS_RETIRED.exists():
            return JSONResponse({"detail": "The default password was retired", "code": "default_retired"},
                                status_code=401)
        raise HTTPException(401, "Wrong user or password")
    LOGIN_FAILS.pop(_lock_key(ip), None)
    _notify("panel_login", "Panel sign-in", f"{l.user} signed in from {ip}.", tags="key")
    exp = int(time.time()) + SESSION_DAYS * 86400
    response.set_cookie("vh_session", f"{l.user}|{exp}|{_sign(l.user, exp)}",
                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto") == "https")
    return {"ok": True, "user": l.user}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    if _session_ok(request.cookies.get("vh_session")):
        _revoke(request.cookies.get("vh_session"))
    response.delete_cookie("vh_session")
    return {"ok": True}


def _no_newlines(env):
    """Both env files are read line by line. shlex.quote keeps a newline harmless to bash,
    but the panel's own parser would read what follows it as a key of its own - a token
    field carrying "\nTRUSTED_PROXIES=..." wrote exactly that."""
    for k, v in env.items():
        if "\n" in str(v) or "\r" in str(v):
            raise HTTPException(400, f"{k}: no line breaks")


def _save_panel_env(cfg):
    _no_newlines(cfg)
    _write(VH_PANEL_ENV, "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in cfg.items()),
           mode=0o600, own="root:root")


class Auth(BaseModel):
    user: str = "admin"
    password: str = ""
    current: str = ""


@app.post("/api/panel/auth")
def panel_auth(a: Auth):
    """Change the panel login. Credentials are read per request, so no restart is needed.
    Asks for the current password: a session left open on someone else's screen, or a forged
    request, must not be enough to take the panel over. The CLI reset stays for the lost one."""
    if not hmac.compare_digest(a.current.encode(), (_env_file(VH_PANEL_ENV).get("PANEL_PASS") or "").encode()):
        raise HTTPException(403, "The current password is wrong")
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}\Z", a.user):
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


def _in_game_dirs(p):
    """True when a path, links resolved, lies in the game user's part of the install."""
    r = Path(p).resolve()
    return any(r.is_relative_to(Path(d).resolve()) for d in (VH_SERVER, VH_DATA, VH_BACKUPS))


def _ids(own):
    u, _, g = own.partition(":")
    return pwd.getpwnam(u).pw_uid, grp.getgrnam(g or u).gr_gid


def _write(path, text, mode=0o644, own="valheim:valheim"):
    """Atomic and never through a symlink: a temp file next to the target, renamed over it.
    server/, data/ and backups/ belong to the game user, so anything running as that user -
    a mod, say - can plant a link there, and a root panel writing through it would write
    wherever the link points. rename() replaces a link instead of following it, and a crash
    mid-write leaves the old file instead of an empty one."""
    p = Path(path)
    if p.parent.resolve() != Path(VH_DIR).resolve():
        if not _in_game_dirs(p.parent):
            raise HTTPException(400, f"Refusing to write outside the install: {p}")
        # A game folder: written BY the game user, not by root checking and then writing - the
        # check and the write were two steps, and a mod swapping a folder for a link between
        # them had root write wherever it pointed. As the game user, a link leads only where
        # that user could write anyway.
        return _write_as_game(p, text.encode(), mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            os.fchmod(f.fileno(), mode)
            if own:
                os.fchown(f.fileno(), *_ids(own))
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_GAME_WRITE = """
import os, sys, tempfile
p, mode = sys.argv[1], int(sys.argv[2], 8)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix="." + os.path.basename(p) + ".")
try:
    os.write(fd, sys.stdin.buffer.read()); os.fchmod(fd, mode); os.close(fd)
    os.replace(tmp, p)
except BaseException:
    os.unlink(tmp); raise
"""


def _write_as_game(p, data, mode=0o644):
    r = subprocess.run(["runuser", "-u", "valheim", "--", "python3", "-c", _GAME_WRITE, str(p), oct(mode)],
                       input=data, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise HTTPException(502, f"Could not write {p.name}: " + r.stderr.decode(errors="replace").strip()[-200:])


def _game_sh(cmd, timeout=120):
    """A shell command run as the game user. For anything that deletes, moves or reads inside
    its folders: data/worlds_local and everything under server/ can be swapped for a link by
    that user, and root following one would delete or read wherever it led."""
    return _sh_ok(f"runuser -u valheim -- sh -c {shlex.quote(cmd)}", timeout=timeout)


def _read_as_game(p):
    """A file in a game folder, read with the game user's rights - root following a link
    planted there would read, say, panel.env for whoever planted it."""
    r = subprocess.run(["runuser", "-u", "valheim", "--", "cat", "--", str(p)], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise HTTPException(404, f"Could not read {Path(p).name}")
    return r.stdout.decode(errors="replace")


def _save_json(path, obj, **kw):
    """State files, atomically: temp file + rename. Written in place, a reader could catch the
    file empty - _history then read "no history" and wrote that back, and every hour of
    playtime older than the journal was gone. A crash mid-write did the same."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, **kw)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _ts(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except Exception:
        return None


VH_LOG_FILTER = ("grep -E 'Got connection|Got handshake|Closing socket|ZDOID|Connections [0-9]|"
                 "Valheim version|join code'")
# How far back status() reads the journal. The whole of it took 3 s on two months of
# production log and grew by the day - on every tick and every page poll. What a longer
# session needs from before the window (its version and join code) is kept in VH_SESSION.
VH_LOG_DAYS = 7
VH_SESSION = Path(f"{VH_DIR}/session.json")
VH_STATUS_SH = f"""
echo '@state'; systemctl is-active valheim
date +%s
date -d "$(systemctl show valheim -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0
echo '@env'; cat {VH_ENV} 2>/dev/null
echo '@panel'; cat {VH_PANEL_ENV} 2>/dev/null | grep -v PANEL_PASS
echo '@lists'; for f in {' '.join(VH_LISTS.values())}; do echo "#$f"; cat {VH_DATA}/$f 2>/dev/null; done
echo '@backups'; ls -l --time-style=+%s {VH_BACKUPS} 2>/dev/null
echo '@timers'; for t in {' '.join(VH_TIMERS.values())}; do \
  n=$(systemctl list-timers --all --no-pager $t 2>/dev/null | awk 'NR==2 && $1!="-"{{print $1,$2,$3,$4}}'); \
  echo "$t $(systemctl is-enabled $t 2>/dev/null) $(systemctl is-active $t 2>/dev/null) $(date -d "$n" +%s 2>/dev/null || echo 0)"; done
echo '@disk'; df -B1 --output=used,avail {VH_DIR} 2>/dev/null | tail -1
echo '@machine'; cat /proc/loadavg; nproc; awk '/MemTotal|MemAvailable/{{print $2}}' /proc/meminfo; cut -d' ' -f1 /proc/uptime
echo '@log'; journalctl -u valheim -o short-iso --no-pager --since "-{VH_LOG_DAYS} days" | {VH_LOG_FILTER} | tail -n 4000
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
        # A dead player's character id is zeroed, and that is the only trace a death leaves
        # in a vanilla server log — the game itself never writes the word. Checked before
        # the name line below, which otherwise swallows it.
        m = re.search(r"Got character ZDOID from (.+?) : 0:0\s*$", rest)
        if m:
            yield t, "death", m.group(1).strip()
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
            # The server prints both "Got connection" and "Got handshake from client" for
            # the same peer, and with crossplay sometimes only one of the two — so the id
            # is what counts, not the line. Without this guard one player was two or three
            # online, and every join appeared twice in the history.
            if any(x["id"] == val for x in conns):
                continue
            c = {"id": val, "name": None, "since": t}
            conns.append(c)
            hist.append((t, "join", c))
        elif kind == "leave":
            # an id that matches nobody (crossplay peers can close under another id) is only
            # pinned on someone when there is exactly one to pin it on: dropping the wrong
            # player makes the server look empty, and "empty" is what every restart waits for
            c = next((x for x in conns if x["id"] == val), None) or (conns[0] if len(conns) == 1 else None)
            if c:
                conns.remove(c)
                hist.append((t, "leave", c))
        elif kind == "name":
            c = next((x for x in conns if not x["name"]), None)
            if c:
                c["name"] = val
                hist.append((t, "name", c))
        elif kind == "death":
            # the line carries a name, not an id, so it can only be attributed to someone
            # who is connected under that name — an unmatched death is dropped, not guessed
            c = next((x for x in conns if x["name"] == val), None)
            if c:
                hist.append((t, "death", c))
        elif kind == "count":
            count, count_ts = int(val), t
        elif kind == "joincode":
            joincode = val
    return conns, hist, count, count_ts, version, joincode


_HISTORY_LOCK = threading.Lock()


def _history(hist):
    """Persistent login history — the journal rotates, the player list should not. Runs from
    the tick and from every page poll at once, hence the lock."""
    with _HISTORY_LOCK:
        return _history_locked(hist)


def _history_locked(hist):
    try:
        st = json.loads(VH_STORE.read_text())
    except FileNotFoundError:
        st = {}
    except Exception as e:
        # exists but does not parse: never overwrite it with what the journal still holds
        _log("history.unreadable", ok=False, error=f"{type(e).__name__}: {e}"[:120])
        st = None
    if st is None:
        return []
    players, last = st.get("players", {}), st.get("last_ts", 0)
    newest = last
    for t, kind, c in hist:
        newest = max(newest, t)
        if t <= last:
            continue
        p = players.setdefault(c["id"], {"id": c["id"], "name": None, "first": t, "last": t,
                                         "sessions": 0, "total": 0, "log": []})
        if kind == "join":
            p["sessions"] = p.get("sessions", 0) + 1
        if kind == "name" and c["name"]:
            p["name"] = c["name"]
        if kind == "death":
            p["deaths"] = p.get("deaths", 0) + 1
            p["last_death"] = t
        if kind == "leave" and c.get("since"):
            # the only place a session length can be known: the connection carried its start
            dur = max(0, t - c["since"])
            p["total"] = p.get("total", 0) + dur
            p.setdefault("log", []).append({"start": c["since"], "end": t, "seconds": dur})
            p["log"] = p["log"][-60:]
        p["first"] = min(p.get("first", t), t)
        p["last"] = max(p.get("last", t), t)
    if len(players) > 1000:                 # the thousand most recent are plenty of history
        players = dict(sorted(players.items(), key=lambda kv: kv[1]["last"], reverse=True)[:1000])
    try:
        _save_json(VH_STORE, {"players": players, "last_ts": newest})
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
    # the page itself decides: logged in -> panel, otherwise our own login screen. The session
    # only: "/" is an open path, and answering a Basic header here was a password oracle that
    # sat outside the lockout - index.html for a right guess, login.html for a wrong one.
    return (HERE / ("index.html" if _session_ok(request.cookies.get("vh_session")) else "login.html")).read_text()


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

    try:
        worlds = _worlds()
    except FileNotFoundError:
        worlds = []
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
    # the boot line is in the window: remember it; it is not: the session is older than the
    # window, and what was remembered is still its version and join code
    try:
        if version:
            if _load_json(VH_SESSION, {}) != {"version": version, "joincode": joincode}:
                _save_json(VH_SESSION, {"version": version, "joincode": joincode})
        else:
            sess = _load_json(VH_SESSION, {})
            version, joincode = sess.get("version"), sess.get("joincode")
    except Exception:
        pass
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


@app.get("/api/valheim/players/stats")
def player_stats():
    """Playtime and when the server is actually busy — both come out of the session log the
    history store already keeps, so nothing extra runs on the game server."""
    try:
        players = json.loads(VH_STORE.read_text()).get("players", {})
    except Exception:
        players = {}
    by_hour = [0] * 24
    for p in players.values():
        for s in p.get("log", []):
            h = datetime.fromtimestamp(s["start"]).hour
            span = max(1, round(s["seconds"] / 3600))
            for i in range(span):
                by_hour[(h + i) % 24] += 1
    board = sorted(({"id": p["id"], "name": p.get("name"), "total": p.get("total", 0),
                     "sessions": p.get("sessions", 0), "last": p.get("last"),
                     "first": p.get("first"), "deaths": p.get("deaths", 0),
                     "longest": max((s["seconds"] for s in p.get("log", [])), default=0)}
                    for p in players.values()),
                   key=lambda x: x["total"], reverse=True)
    recent = sorted((dict(s, id=p["id"], name=p.get("name"))
                     for p in players.values() for s in p.get("log", [])),
                    key=lambda s: s["start"], reverse=True)[:40]
    return {"players": board, "by_hour": by_hour, "recent": recent,
            "total_seconds": sum(p["total"] for p in board)}


@app.post("/api/valheim/action/{action}")
def action(action: str):
    if action == "update":
        r = _game_update_tick(dict(WATCH["online"]), manual=True)
        if r.get("held"):
            raise HTTPException(409, f"Update {r['latest']} is waiting: {r['held']}")
        return {"ok": True, "out": (f"updated to build {r['latest']}" if r["updated"]
                                    else f"already on build {r['installed']}")}
    cmd = VH_ACTIONS.get(action)
    if not cmd:
        raise HTTPException(400, "Unknown action")
    out = _sh_ok(cmd[0], timeout=cmd[1]).strip()[-400:]
    _log("server." + action, out=out or None)
    # The watcher samples once a minute, so a stop followed by a start half a minute later
    # is invisible to it - both samples say "running". The action itself knows exactly what
    # happened, so it says so, and hands the watcher the new state to stop it reporting the
    # same thing again from behind.
    if action in ("start", "stop", "restart"):
        WATCH["active"] = action != "stop"
        _notify("server_action", {"start": "Server started", "stop": "Server stopped",
                                  "restart": "Server restarted"}[action],
                f"From the panel, at {datetime.now().strftime('%H:%M')}.",
                priority="high" if action == "stop" else "default",
                tags={"start": "green_circle", "stop": "red_circle", "restart": "repeat"}[action])
    return {"ok": True, "out": out}


UPDATE_SH_STUB = """#!/bin/bash
# Since 2026-09-07 the panel installs game updates itself (valheim-update.timer is the
# on/off switch it reads). This script stays so the timer has something to run.
echo "game updates are handled by the panel - see the Log tab"
"""
VH_PANEL_VERSION = Path(f"{VH_DIR}/panel.version")   # written by setup.sh: "v1.20.0 <when>"
VH_AUTO_UPDATE_OFF = Path(f"{VH_DIR}/auto-update.off")  # present = the admin switched it off
_PANEL_LATEST = {"at": 0, "tag": "", "name": "", "url": "", "notes": ""}


def _vtuple(v):
    # searched, not anchored: a test build named "test-v1.24.0-rc2" read as no version at all,
    # and "no version" counts as older - the panel then "updated" itself back to the release
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(map(int, m.groups())) if m else None


def _panel_installed():
    """The release this panel came from. Installs from before releases had a VERSION file
    wrote a commit hash to panel.version - that reads as unknown, and unknown is older."""
    f = HERE / "VERSION"
    return f.read_text().strip() if f.exists() else "unknown"


def _panel_latest():
    """The newest GitHub release, asked once an hour - the panel polls this every page load."""
    if time.time() - _PANEL_LATEST["at"] > 3600:
        _PANEL_LATEST["at"] = time.time()
        try:
            r = _github_json("https://api.github.com/repos/PawelSzymanski89/valheim-proxmox/releases/latest")
            _PANEL_LATEST.update(tag=r["tag_name"], name=r.get("name") or r["tag_name"],
                                 url=r.get("html_url", ""), notes=(r.get("body") or "")[:1500])
        except Exception:
            pass
    return _PANEL_LATEST


def _panel_can_update():
    return Path(f"{VH_DIR}/panel-update.sh").exists() and not Path("/opt/valheim-image").exists()


def _panel_newer():
    """The latest release if it is newer than this panel, else None."""
    latest, mine = _panel_latest(), _vtuple(_panel_installed())
    new = _vtuple(latest["tag"])
    return latest if new and (mine is None or new > mine) else None


@app.get("/api/panel/version")
def panel_version():
    """What is installed, what GitHub has, and whether this install can update itself
    (a Docker install cannot - the image is the unit of update there)."""
    latest = _panel_latest()
    return {"installed": _panel_installed(), "latest": latest["tag"], "name": latest["name"],
            "url": latest["url"], "notes": latest["notes"], "newer": bool(_panel_newer()),
            "auto": not VH_AUTO_UPDATE_OFF.exists(),
            "docker": Path("/opt/valheim-image").exists(), "can_update": _panel_can_update()}


RELEASE_KEY = "WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0="   # same key as panel-update.sh and the launcher


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-proxmox-panel"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _release_signed(data, sig_b64):
    """ed25519 over the file, with the project's release key. The key is made and kept on the
    maintainer's machine, not on GitHub - a release anyone else publishes does not verify.
    The crypto library first; openssl where an old venv has not got it yet."""
    try:
        sig = base64.b64decode(sig_b64.strip(), validate=True)
    except Exception:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        d = Path(tempfile.mkdtemp())
        try:
            der = bytes.fromhex("302a300506032b6570032100") + base64.b64decode(RELEASE_KEY)
            (d / "k.pem").write_text("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode()
                                     + "\n-----END PUBLIC KEY-----\n")
            (d / "f").write_bytes(data)
            (d / "s").write_bytes(sig)
            return subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(d / "k.pem"),
                                   "-rawin", "-in", str(d / "f"), "-sigfile", str(d / "s")],
                                  capture_output=True).returncode == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(RELEASE_KEY)).verify(sig, data)
        return True
    except Exception:
        return False


def _panel_update_start(why):
    """panel-update.sh in its own transient unit - it restarts this very process, so it
    cannot run as our child. The script keeps the previous panel and rolls back by itself."""
    # An install from 2026-09-07 has the old panel-update.sh, which copied the panel files and
    # nothing else - it brings this panel in, but never VERSION or setup.sh. The engine is
    # swapped in first, so the one click on the old button ends on a complete update.
    # The new script comes out of the newest SIGNED release, verified here - it used to be
    # fetched from the main branch, where one push would have run as root everywhere.
    script = Path(f"{VH_DIR}/panel-update.sh")
    if "RELEASE_KEY=" not in script.read_text():
        tag = _panel_latest()["tag"]
        if not tag:
            raise HTTPException(502, "Could not ask GitHub for the latest release")
        base = f"https://github.com/PawelSzymanski89/valheim-proxmox/releases/download/{tag}/valheim-proxmox-{tag}.tar.gz"
        data, sig = _fetch(base, 120), _fetch(base + ".sig", 20)
        if not _release_signed(data, sig):
            raise HTTPException(502, f"{tag}: the release is not signed with the project key - not installing")
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            m = next(x for x in t.getmembers() if x.name.endswith("/panel/panel-update.sh") and x.isfile())
            new = t.extractfile(m).read()
        tmp = script.with_suffix(".new")
        tmp.write_bytes(new)
        tmp.chmod(0o755)
        tmp.replace(script)
        _log("panel.update_engine_installed", tag=tag)
    _log("panel.update", why=why, to=_PANEL_LATEST["tag"])
    return _sh(f"systemd-run --on-active=1 --unit=valheim-panel-update-{int(time.time())} "
               f"{VH_DIR}/panel-update.sh")


@app.post("/api/panel/update")
def panel_update():
    if Path("/opt/valheim-image").exists():
        raise HTTPException(400, "Docker install: rebuild the image (docker compose up -d --build)")
    if not Path(f"{VH_DIR}/panel-update.sh").exists():
        raise HTTPException(400, "panel-update.sh is missing - update once from the terminal, see the README")
    _panel_update_start("button")
    return {"ok": True}


@app.post("/api/panel/auto-update")
def panel_auto_update(body: dict = Body(...)):
    if body.get("on"):
        VH_AUTO_UPDATE_OFF.unlink(missing_ok=True)
    else:
        VH_AUTO_UPDATE_OFF.touch()
    _log("panel.auto_update", on=bool(body.get("on")))
    return {"ok": True, "auto": not VH_AUTO_UPDATE_OFF.exists()}


def _panel_update_tick(now_on):
    """Automatic updates: a newer release, switched on, and nobody playing. The panel is the
    only thing that restarts - the game keeps running - but an update is still best done
    while nobody is watching. One try per release: a rolled-back update waits for the next."""
    new = _panel_newer()
    if not new or VH_AUTO_UPDATE_OFF.exists() or not _panel_can_update() or now_on:
        return
    # an armed or releasing launch is holding the game in a precise state - leave it alone
    if _launch_waiting() or _LAUNCH_BUSY["at"]:
        return
    tried = Path(f"{VH_DIR}/auto-update.tried")
    if tried.exists() and tried.read_text().strip() == new["tag"]:
        return
    # the one try per release is only spent once the update has really been started - a
    # failed GitHub fetch or systemd-run is tried again, but no more than once an hour
    if time.time() - WATCH.get("panel_update_at", 0) < 3600:
        return
    WATCH["panel_update_at"] = time.time()
    if _panel_update_start("auto").returncode == 0:
        tried.write_text(new["tag"])


def _retire_default_password():
    """Until v1.21.0 every install started with the same password, and an update never touched
    panel.env - so upgraded servers still had it: a root panel, on the network, behind a
    password printed in the README. It is replaced by a random one here, once, on every kind
    of install. The admin sets their own on the host (panel-passwd.sh); the login page says
    how when someone tries the old one."""
    cfg = _env_file(VH_PANEL_ENV)
    if cfg.get("PANEL_PASS") != PANEL_DEFAULT_PASS:
        return
    cfg["PANEL_PASS"] = secrets.token_urlsafe(18)
    _save_panel_env(cfg)
    VH_PASS_RETIRED.touch()
    _log("panel.default_password_retired")
    _notify("panel_login", "Panel password replaced",
            "The panel still had the default password everyone knows, so it was replaced with a "
            "random one. Set your own on the Proxmox host: "
            "pct exec <container id> -- /opt/valheim/panel-passwd.sh <new password>",
            priority="high", tags="lock")


def _panel_updated_notice():
    """Runs once at start: tells the admin when this start is the first on a new release."""
    seen = Path(f"{VH_DIR}/panel.seen")
    mine = _panel_installed()
    old = seen.read_text().strip() if seen.exists() else ""
    if old and old != mine and "rollback" not in (VH_PANEL_VERSION.read_text() if VH_PANEL_VERSION.exists() else ""):
        _log("panel.updated", frm=old, to=mine)
        _notify("maintenance", f"Panel updated to {mine}", f"From {old}. World and settings untouched.",
                tags="arrow_up")
    seen.write_text(mine)
    # a rollback leaves the old panel running and would otherwise pass without a word
    ver = VH_PANEL_VERSION.read_text().strip() if VH_PANEL_VERSION.exists() else ""
    told = Path(f"{VH_DIR}/panel.rollback-seen")
    if "rollback" in ver and (not told.exists() or told.read_text().strip() != ver):
        _log("panel.rolled_back", version=ver)
        _notify("mod_failed", "Panel update rolled back",
                f"The new version did not start, so {mine} is running again. Nothing else changed.",
                priority="high", tags="warning")
        told.write_text(ver)


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
    _no_newlines(env)
    _write(VH_ENV, "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()), mode=0o640, own="root:valheim")

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


# Two on-disk formats. Up to 0.221 a world was NAME.db + NAME.fwl. Since 1.0 it is a folder
# NAME/ of map chunks plus numbered saves _main.N.{db2,fwl2,chunks,ok}; N goes up on every
# save, the .ok is written last, and the game loads the highest complete N. The server
# still reads an old pair and converts it on load, so both have to be understood here.
VH_W1_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.(chunk|chunks|db2|fwl2|ok)\Z")
VH_UPLOADS = Path(f"{VH_DIR}/upload")


def _world_files(name):
    """(fwl, db) paths of a world's current save, (None, None) if there is no such world."""
    d = Path(VH_WORLDS) / name
    if d.is_dir():
        saves = sorted((int(p.name.split(".")[1]), p) for p in d.glob("_main.*.ok")
                       if p.name.split(".")[1].isdigit())
        for n, _ in reversed(saves):
            fwl, db = d / f"_main.{n}.fwl2", d / f"_main.{n}.db2"
            if fwl.exists() and db.exists():
                return fwl, db
        # a world that has not been saved yet has only its _main.0.fwl2
        fwls = sorted((int(p.name.split(".")[1]), p) for p in d.glob("_main.*.fwl2")
                      if p.name.split(".")[1].isdigit())
        if fwls:
            return fwls[-1][1], fwls[-1][1].with_suffix(".db2")
    # An old pair. Converting one, the 1.0 server makes the folder at load but fills it only
    # on the first save - until then the empty folder must not hide the pair.
    fwl, db = d.with_name(f"{name}.fwl"), d.with_name(f"{name}.db")
    return (fwl, db) if fwl.exists() else (None, None)


def _worlds():
    out = []
    names = {p.name if p.is_dir() else p.stem for p in Path(VH_WORLDS).glob("*")
             if p.is_dir() or p.suffix == ".fwl"}
    for name in names:
        if not VH_NAME_RE.match(name) or "_backup_auto-" in name:
            continue
        fwl, db = _world_files(name)
        if not fwl:
            continue
        new = fwl.suffix == ".fwl2"
        files = [f.stat() for f in (fwl.parent.iterdir() if new else (fwl, db)) if f.exists()]
        out.append({"name": name, "format": "1.0" if new else "legacy",
                    "size": sum(s.st_size for s in files),
                    "mtime": int(max((s.st_mtime for s in files), default=0))})
    return sorted(out, key=lambda w: w["mtime"], reverse=True)


def _not_active(name):
    if _parse_env(Path(VH_ENV).read_text().splitlines())["world"] == name:
        raise HTTPException(409, "That is the active world — switch to another one first")


class World(BaseModel):
    name: str
    restart: bool = True


@app.post("/api/valheim/worlds/activate")
def world_activate(w: World):
    _world_ok(w.name)
    if not _world_files(w.name)[0]:
        raise HTTPException(404, "No such world")
    cur = _parse_env(Path(VH_ENV).read_text().splitlines())
    cur["world"] = w.name
    cur["restart"] = w.restart
    cur["panel_port"] = int(_env_file(VH_PANEL_ENV).get("PANEL_PORT") or 2460)
    return _save_settings(Settings(**cur))


@app.delete("/api/valheim/worlds/{name}")
def world_delete(name: str):
    q = _world_ok(name)
    _not_active(name)
    # rm -r: since 1.0 the world and each of its automatic copies are folders
    _game_sh(f"cd {VH_WORLDS} && rm -rf {q} {q}.db {q}.fwl {q}.db.old {q}.fwl.old {q}_backup_auto-*")
    _log("world.delete", world=name)
    return {"ok": True}


@app.get("/api/valheim/worlds/{name}/download")
def world_download(name: str):
    q = _world_ok(name)
    fwl, db = _world_files(name)
    if not fwl:
        raise HTTPException(404, "No such world")
    # the folder itself goes in, so the archive unpacks straight into a game's worlds_local
    what = q if fwl.parent.name == name else f"{q}.db {q}.fwl"
    # tar straight to us, as the game user - through "| base64" a failed tar (a folder it may
    # not enter, say) came back as an empty archive instead of an error
    r = subprocess.run(["runuser", "-u", "valheim", "--", "sh", "-c", f"tar czf - -C {shlex.quote(VH_WORLDS)} {what}"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise HTTPException(502, "Could not pack the world: " + r.stderr.decode(errors="replace").strip()[-200:])
    data = r.stdout
    return Response(data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})


@app.post("/api/valheim/worlds/upload/{filename:path}")
def world_upload(filename: str, data: bytes = Body(b""), fresh: bool = False):
    # raw body instead of multipart — keeps python-multipart out of the dependency list
    if len(data) > 300 * 1024 * 1024:
        raise HTTPException(413, "File too large")
    world, sep, fn = filename.partition("/")
    if sep:
        # 1.0: one file of a world folder. Staged outside worlds_local until upload-done,
        # so a half-finished upload never sits where the game or the panel would read it.
        if not VH_NAME_RE.match(world) or not VH_W1_FILE_RE.match(fn):
            raise HTTPException(400, f"Not a file of a Valheim world folder: {filename}")
        _not_active(world)                    # on the first file, not after the whole folder
        dest = VH_UPLOADS / world
        if fresh:
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / fn).write_bytes(data)
        return {"ok": True, "staged": filename}
    base, _, ext = filename.rpartition(".")
    if ext not in ("db", "fwl") or not VH_NAME_RE.match(base):
        raise HTTPException(400, f"Only a world folder, or .db and .fwl files with a plain name: {filename}")
    if not data:
        raise HTTPException(400, "Empty file")
    _not_active(base)
    p = Path(VH_WORLDS) / filename
    if not _in_game_dirs(p.parent):
        raise HTTPException(400, "worlds_local is not where it should be")
    # written by the game user: worlds_local itself can be swapped for a link, which an
    # O_NOFOLLOW on the file name alone does not catch
    _write_as_game(p, data)
    _log("world.upload", file=filename, size=len(data))
    return {"ok": True, "saved": filename}


@app.post("/api/valheim/worlds/upload-done/{name}")
def world_upload_done(name: str):
    """Moves a staged 1.0 world folder into place, replacing a world of the same name."""
    q = _world_ok(name)
    _not_active(name)
    src = VH_UPLOADS / name
    kinds = {p.suffix for p in src.glob("_main.*")} if src.is_dir() else set()
    if not {".db2", ".fwl2", ".ok"} <= kinds:
        shutil.rmtree(src, ignore_errors=True)
        raise HTTPException(400, "Not a Valheim 1.0 world folder — it needs _main.N.db2, .fwl2 and .ok")
    # copied in by the game user from root's staging folder, the old world removed by it too
    _sh_ok(f"chown -R valheim:valheim {shlex.quote(str(src))}")
    try:
        _game_sh(f"cd {VH_WORLDS} && rm -rf {q} && cp -a {shlex.quote(str(src))} {q}", timeout=300)
    finally:
        shutil.rmtree(src, ignore_errors=True)
    _log("world.upload", world=name, files=sum(1 for _ in (Path(VH_WORLDS) / name).iterdir()))
    return {"ok": True, "saved": name}


def _bak_ok(fn):
    if not VH_BAK_RE.match(fn or ""):
        raise HTTPException(400, "Invalid backup name")
    return fn


# ---------- what is actually inside a world file ----------
# Both formats are plain little-endian structs written by C#'s BinaryWriter. Verified
# against this server's own files: .fwl gives the seed, .db opens with the world version
# and the in-game clock. Game time only advances while somebody is connected, which is why
# two backups taken from an empty server are byte-identical.
DAY_SECONDS = 1800
# One in-game hour is 75 real seconds; of the 30-minute cycle, 21 minutes are daylight and
# 9 are night, which puts night at roughly 20:24-03:36. What is *not* documented anywhere is
# the phase - which clock time the saved counter's zero corresponds to. A fresh world starts
# in the morning, so 06:00 is the assumption, and CLOCK_OFFSET_H is the knob to correct it:
# compare the panel against the sky once and shift it by the difference.
CLOCK_OFFSET_H = float(os.environ.get("VH_CLOCK_OFFSET", 6))
NIGHT_FROM, NIGHT_TO = 20 + 24 / 60, 3 + 36 / 60


def _clock(net_seconds):
    """In-game time of day from the world clock, plus how long until it flips."""
    h = ((net_seconds / DAY_SECONDS % 1) * 24 + CLOCK_OFFSET_H) % 24
    night = h >= NIGHT_FROM or h < NIGHT_TO
    nxt = NIGHT_TO if night else NIGHT_FROM
    hours_left = (nxt - h) % 24
    return {"hour": int(h), "minute": int(h % 1 * 60),
            "time": f"{int(h):02d}:{int(h % 1 * 60):02d}",
            "night": night, "changes_in": round(hours_left * (DAY_SECONDS / 24))}


def _cs_string(b, i):
    """C# BinaryWriter string: 7-bit encoded length, then UTF-8."""
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            break
        shift += 7
    return b[i:i + n].decode("utf8", "replace"), i + n


def _read_fwl(data):
    i = 4                                     # length prefix of the package that follows
    ver = struct.unpack_from("<i", data, i)[0]
    i += 4
    name, i = _cs_string(data, i)
    seed_name, i = _cs_string(data, i)
    seed = struct.unpack_from("<i", data, i)[0]
    return {"version": ver, "name": name, "seed_name": seed_name, "seed": seed}


def _read_db_head(head):
    ver = struct.unpack_from("<i", head, 0)[0]
    net = struct.unpack_from("<d", head, 4)[0]
    return {"version": ver, "time": round(net, 1), "day": int(net / DAY_SECONDS) + 1}


def _world_card(world=None):
    """Seed, in-game day and file sizes for one world — everything a restore decision needs."""
    world = world or _parse_env(Path(VH_ENV).read_text().splitlines())["world"]
    fwl, db = _world_files(world)
    card = {"world": world, "db": None, "fwl": None, "error": None}
    try:
        if not fwl:
            raise FileNotFoundError
        card["fwl"] = _read_fwl(fwl.read_bytes())
        with db.open("rb") as f:
            card["db"] = _read_db_head(f.read(12))
        card["db"]["size"] = db.stat().st_size
        card["db"]["saved"] = int(db.stat().st_mtime)
        # The file only moves on save (every 20 minutes), but game time runs at wall-clock
        # rate while somebody is connected - and stands still when nobody is. So: extrapolate
        # from the last save, and only for as long as the server has had players.
        live = card["db"]["time"] + (time.time() - card["db"]["saved"] if WATCH.get("online") else 0)
        card["clock"] = _clock(live)
        card["day"] = int(live / DAY_SECONDS) + 1
    except FileNotFoundError:
        card["error"] = "world files not found — it is created on first start"
    except Exception as e:
        card["error"] = f"{type(e).__name__}: {e}"[:120]
    return card


def _verify_backup(fn):
    """A backup nobody has opened is a guess. `tar tz` walks the whole gzip stream, so a
    truncated or bit-rotted archive fails here instead of on the night you need it."""
    p = Path(VH_BACKUPS) / fn
    out = {"file": fn, "at": int(time.time()), "ok": False, "size": None, "error": None}
    try:
        out["size"] = p.stat().st_size
        # as the game user: the archive sits in its folder, and a tar/gzip parser bug should
        # not be root's problem
        r = _sh(f"runuser -u valheim -- tar tzf {shlex.quote(str(p))}", timeout=180)
        if r.returncode != 0:
            out["error"] = (r.stderr or "tar failed").strip()[:160]
            return out
        # the names are kept exactly as tar stored them ("./Klans.db"), because that is what
        # tar wants back when extracting one — trimming the "./" first finds nothing
        members = [m.strip() for m in r.stdout.splitlines() if m.strip()]
        # .db/.fwl up to 0.221, .db2/.fwl2 inside a world folder since 1.0; the game's own
        # automatic copies are in the archive too, the real world is the one to read
        active = _parse_env(Path(VH_ENV).read_text().splitlines())["world"]
        mset = set(members)

        def rank(m):
            # the active world's newest *complete* save: numbers compared as numbers ("99" sorts
            # after "755" as text), and a save without its .ok is one that was interrupted
            n = re.search(r"_main\.(\d+)\.db2$", m)
            done = not n or m[:-len(".db2")] + ".ok" in mset
            return ("_backup_auto-" in m, not m.startswith((f"./{active}/", f"./{active}.")),
                    not done, -(int(n.group(1)) if n else 0))
        dbs = sorted((m for m in members if m.endswith((".db", ".db2"))), key=rank)
        if not dbs or not any(m.endswith((".fwl", ".fwl2")) for m in members):
            out["error"] = f"no world in the archive ({len(members)} files)"
            return out
        # and prove the world inside is readable, not just that the archive opens
        name = dbs[0]
        head = _sh(f"runuser -u valheim -- tar xzOf {shlex.quote(str(p))} {shlex.quote(name)} 2>/dev/null | head -c 12 | base64",
                   timeout=180).stdout.strip()
        out.update(_read_db_head(base64.b64decode(head)))
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:160]
    return out


# ---------- admin-only tools: the panel talking to the running game ----------
# Vanilla Valheim has no way in - no RCON, no console socket, nothing. Three server-side
# mods provide one, and none of them touches the players: rcon carries the protocol,
# Rcon_Commands exposes the server console over it, Server_devcommands adds the commands
# worth sending. Everything below is dead until they are installed.
ADMIN_TOOLS = ["AviiNL-rcon", "JereKuusela-Rcon_Commands", "JereKuusela-Server_devcommands"]
SAY_KEEP = 200


def _rcon(command, timeout=6):
    """Source RCON: 4-byte length, request id, type, body, two nulls. Type 3 authenticates,
    type 2 runs. Loopback only - the port is not meant to leave this container."""
    env = {**_env_file(VH_PANEL_ENV), **_env_file(VH_RCON_ENV)}
    port, pw = int(env.get("RCON_PORT") or 0), env.get("RCON_PASS") or ""
    if not port or not pw:
        raise HTTPException(503, "RCON is not configured — install the admin tools first")

    def pkt(i, t, body):
        data = struct.pack("<ii", i, t) + body.encode("utf8") + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def read(sock):
        head = sock.recv(4)
        if len(head) < 4:
            raise OSError("short read")
        n = struct.unpack("<i", head)[0]
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        rid, _t = struct.unpack_from("<ii", buf)
        return rid, buf[8:-2].decode("utf8", "replace")

    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sk:
        sk.settimeout(timeout)
        sk.sendall(pkt(1, 3, pw))
        rid, _ = read(sk)
        if rid == -1:
            raise HTTPException(502, "RCON refused the password")
        sk.sendall(pkt(2, 2, command))
        _rid, out = read(sk)
        return out


VH_CHAR_NAME_RE = re.compile(r"[^\W_][\w '-]{0,23}")   # unicode letters: Michał stays Michał


def _ingame(text):
    """Valheim's font has no Polish letters - they arrive as question marks - so anything
    headed for a player's screen is folded to ASCII first. Only the message text: a player
    named Michał has to stay Michał or the command finds nobody. The panel's own history
    keeps the original, because that one is read in a browser."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    for a, b in (("—", "-"), ("–", "-"), ("„", '"'), ("”", '"'), ("’", "'"), ("…", "...")):
        text = text.replace(a, b)
    return text


def _admin_tools_state():
    have = set((_mods_state().get("mods") or {}).keys())
    env = {**_env_file(VH_PANEL_ENV), **_env_file(VH_RCON_ENV)}
    missing = [m for m in ADMIN_TOOLS if m not in have]
    st = {"packages": ADMIN_TOOLS, "missing": missing,
          "configured": bool(env.get("RCON_PORT") and env.get("RCON_PASS")),
          "ready": False, "error": None}
    if not missing and st["configured"]:
        try:
            _rcon("help", timeout=4)
            st["ready"] = True
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"[:120]
    return st


RCON_CFG = f"{VH_DIR}/server/BepInEx/config/nl.avii.plugins.rcon.cfg"


@app.post("/api/valheim/admin-tools/setup")
def admin_tools_setup():
    """Turn RCON on with a generated password and a port outside the forwarded game range.
    The default is 2458, which sits inside 2456-2458 - the range a router forward points at.
    It is TCP so a UDP forward cannot reach it, but a port nobody has to reason about is
    better than one that needs the explanation."""
    cfg = Path(RCON_CFG)
    if not cfg.exists():
        raise HTTPException(409, "The rcon mod has not written its config yet — start the server once")
    env = {**_env_file(VH_PANEL_ENV), **_env_file(VH_RCON_ENV)}
    port = env.get("RCON_PORT") or "2465"
    pw = env.get("RCON_PASS") or secrets.token_urlsafe(18)
    text = re.sub(r"(?m)^enabled = .*$", "enabled = true", cfg.read_text())
    text = re.sub(r"(?m)^port = .*$", f"port = {port}", text)
    text = re.sub(r"(?m)^password = .*$", f"password = {pw}", text)
    _write(cfg, text)
    # its own file, owned by the game user: backup.sh runs as valheim and needs the password
    # to ask for a save before it copies the world, and it has no business reading panel.env
    Path(VH_RCON_ENV).write_text(f"RCON_PORT='{port}'\nRCON_PASS='{pw}'\n")
    Path(VH_RCON_ENV).chmod(0o600)
    _sh(f"chown valheim:valheim {VH_RCON_ENV}")
    os.environ["RCON_PORT"], os.environ["RCON_PASS"] = str(port), pw
    _sh_ok("systemctl restart valheim", timeout=240)
    _log("admin_tools.setup", port=port)
    return {"ok": True, "port": int(port)}


@app.get("/api/valheim/admin-tools")
def admin_tools():
    return _admin_tools_state()


class Say(BaseModel):
    text: str
    where: str = "center"        # center or side, the two vanilla message positions
    players: str = ""            # empty = everyone


@app.post("/api/valheim/say")
def say(p: Say):
    text = (p.text or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to say")
    if len(text) > 200:
        raise HTTPException(400, "Keep it under 200 characters")
    where = p.where if p.where in ("center", "side") else "center"
    if p.players.strip():
        out = _rcon(f'message {p.players.strip()} {where} {_ingame(_signed(text))}')
    else:
        out = _rcon(f"broadcast {where} {_ingame(_signed(text))}")
    _say_log({"t": int(time.time()), "text": text, "where": where,
              "to": p.players.strip() or "all", "by": "panel"})
    _log("say", where=where, to=p.players.strip() or "all", chars=len(text))
    return {"ok": True, "out": out}


def _say_log(entry):
    try:
        hist = json.loads(VH_SAY.read_text())
    except Exception:
        hist = []
    hist.append(entry)
    try:
        _save_json(VH_SAY, hist[-SAY_KEEP:])
    except Exception:
        pass


# ---------- scheduled messages ----------
# The mod that used to do this on Thunderstore threw 1797 null references a minute and never
# wrote its config, so the panel does it: it already knows the in-game clock, who is online
# and how to reach the server. Rules fire at most once per in-game day, and never into an
# empty world - a message nobody reads is just noise in the log.
VH_RULES = Path(f"{VH_DIR}/schedule.json")
RULE_FIRED = {}


def _rules():
    try:
        return json.loads(VH_RULES.read_text())
    except Exception:
        return DEFAULT_RULES


DEFAULT_RULES = [
    {"id": "dusk", "text": "Zaraz będzie ciemno!", "where": "center",
     "when": "before_night", "value": 1, "enabled": True},
]


@app.get("/api/valheim/schedule")
def rules_get():
    return {"rules": _rules(), "fired": RULE_FIRED}


@app.post("/api/valheim/schedule")
def rules_set(body: dict = Body(...)):
    out = []
    for r in (body.get("rules") or [])[:20]:
        text = str(r.get("text") or "").strip()[:200]
        if not text:
            continue
        when = r.get("when") if r.get("when") in \
            ("before_night", "ingame_at", "every", "on_join", "after_join") else "before_night"
        out.append({"id": str(r.get("id") or secrets.token_hex(4))[:16], "text": text,
                    "where": "side" if r.get("where") == "side" else "center",
                    "when": when, "value": max(0, float(r.get("value") or 0)),
                    "enabled": bool(r.get("enabled", True))})
    _save_json(VH_RULES, out, ensure_ascii=False, indent=1)
    _log("schedule.save", rules=len(out))
    return {"ok": True, "rules": out}


GREETED = {}
VH_LANG = Path(f"{VH_DIR}/panel-lang")     # the panel is a browser tab; greetings are not
GREETINGS = HERE / "greetings.json"
JOKES = HERE / "jokes.json"
VH_SAYCFG = Path(f"{VH_DIR}/say.json")
JOKED = {}


def _say_cfg():
    cfg = {"nick": "Odyn"}
    try:
        cfg.update(json.loads(VH_SAYCFG.read_text()))
    except Exception:
        pass
    return cfg


@app.get("/api/valheim/say/settings")
def say_cfg_get():
    return _say_cfg()


@app.post("/api/valheim/say/settings")
def say_cfg_set(body: dict = Body(...)):
    nick = str(body.get("nick") or "").strip()[:24]
    _save_json(VH_SAYCFG, {"nick": nick})
    _log("say.settings", nick=nick or "<none>")
    return {"ok": True, "nick": nick}


def _signed(text):
    """Screen messages carry no sender, so the name goes in front of the line. An empty
    nick leaves the message bare - some people want the server to sound like the world
    itself rather than like somebody typing."""
    nick = _say_cfg().get("nick", "").strip()
    return f"{nick}: {text}" if nick else text


def _joke():
    try:
        pool = json.loads(JOKES.read_text())
    except Exception:
        return None
    if isinstance(pool, dict):                    # room for an English set later
        pool = pool.get(_lang()) or pool.get("pl") or []
    if not pool:
        return None
    pick = secrets.choice(pool)
    if len(pool) > 1 and pick == JOKED.get("last"):
        pick = secrets.choice([j for j in pool if j != pick])
    JOKED["last"] = pick
    return pick


def _lang():
    try:
        v = VH_LANG.read_text().strip()
        return v if v in ("pl", "en") else "en"
    except Exception:
        return "en"


@app.get("/api/panel/lang")
def lang_get():
    return {"lang": _lang()}


@app.post("/api/panel/lang")
def lang_set(body: dict = Body(...)):
    v = body.get("lang")
    if v not in ("pl", "en"):
        raise HTTPException(400, "pl or en")
    VH_LANG.write_text(v)
    return {"ok": True, "lang": v}


def _greeting(name):
    """A line from the pool in the panel's own language. The language lives in the browser,
    so the panel posts it here - a greeting is written by the server, hours after anyone had
    a tab open. Never the same line twice in a row for the same player."""
    try:
        pool = json.loads(GREETINGS.read_text())[_lang()]
    except Exception:
        return None
    last = GREETED.get("last")
    pick = secrets.choice(pool)
    if len(pool) > 1 and pick == last:
        pick = secrets.choice([g for g in pool if g != last])
    GREETED["last"] = pick
    return pick.replace("{name}", name)


def _greet_tick():
    """Greet by name, which needs two things the joining line alone does not give.

    The name arrives seconds after the connection - the server logs the id first and the
    character second - so a greeting has to wait for it. And it has to be quick, or it lands
    when the player is already off the boat, which is why this reads the tail of the journal
    every ten seconds instead of waiting for the once-a-minute pass over the whole thing.
    """
    rules = [r for r in _rules() if r.get("enabled") and r.get("when") in ("on_join", "after_join")]
    if not rules:
        return
    out = _sh(f"journalctl -u valheim -o short-iso --no-pager -n 400 | {VH_LOG_FILTER}", timeout=20)
    conns, *_ = _scan(out.stdout.splitlines())
    here = {c["id"]: c for c in conns}
    for pid in list(GREETED):
        if pid != "last" and pid not in here:     # "last" is the previous greeting, not a player
            GREETED.pop(pid, None)          # left - greet them again next time they come back
    for key in [k for k in JOKED if k != "last" and k.split("|")[0] not in here]:
        JOKED.pop(key, None)                # same for the delayed lines
    now = int(time.time())
    for pid, c in here.items():
        name = c.get("name")
        if not name:
            continue                        # the character line has not arrived yet
        # The name comes from the player's own client and goes into a console command. Only
        # what a character name can honestly be gets through: a modified client naming itself
        # "x;kick Bob" would otherwise have the greeting run a second command.
        if not VH_CHAR_NAME_RE.fullmatch(name):
            _log("say.odd_name", ok=False, player=name[:40])
            continue
        for r in rules:
            if r["when"] == "on_join":
                if pid in GREETED:
                    continue
                GREETED[pid] = now
            else:                           # after_join: value is minutes since they joined
                key = f"{pid}|{r['id']}"
                if key in JOKED or now - (c.get("since") or now) < r["value"] * 60:
                    continue
                JOKED[key] = now
            body = r["text"].strip()
            text = _greeting(name) if body == "{random}" else \
                _joke() if body == "{joke}" else body.replace("{name}", name)
            if not text:
                continue
            try:
                _rcon(f"message {name} {r['where']} {_ingame(_signed(text))}")
                _say_log({"t": now, "text": text, "where": r["where"], "to": name, "by": r["id"]})
                _log("say.player", player=name, rule=r["id"], when=r["when"])
            except Exception as e:
                _log("say.failed", ok=False, rule=r["id"], error=f"{type(e).__name__}: {e}"[:120])


def _rules_tick():
    """Called from the ten-second sampler: fine enough for a clock where an in-game hour is
    75 real seconds, and cheap because it only reads two small files."""
    if not WATCH.get("online"):
        return                                    # nobody to read it
    # on_join and after_join are addressed to one player and are sent by the watcher that
    # knows who joined and when. They have no place here: this loop broadcasts, and its
    # catch-all "every N minutes" branch was firing them at everyone - with {joke} and
    # {random} left as literal text, because only the per-player path resolves those.
    rules = [r for r in _rules()
             if r.get("enabled") and r.get("when") not in ("on_join", "after_join")]
    if not rules:
        return
    card = _world_card()
    clock, day = card.get("clock"), card.get("day")
    if not clock:
        return
    now = int(time.time())
    for r in rules:
        key, fire = r["id"], False
        if r["when"] == "before_night":
            # value is in in-game hours before nightfall; one of them is 75 real seconds
            fire = not clock["night"] and clock["changes_in"] <= r["value"] * (DAY_SECONDS / 24)
            stamp = f"day{day}"
        elif r["when"] == "ingame_at":
            target = r["value"]                   # in-game hour, 0-23.99
            fire = abs((clock["hour"] + clock["minute"] / 60) - target) < 0.4
            stamp = f"day{day}"
        else:                                     # every N real minutes
            fire = now - RULE_FIRED.get(key, 0) >= max(1, r["value"]) * 60
            stamp = str(now)
        if not fire or RULE_FIRED.get(key + ":stamp") == stamp:
            continue
        RULE_FIRED[key + ":stamp"], RULE_FIRED[key] = stamp, now
        # A timed rule may ask for a random joke too - resolve it here as well, otherwise
        # the placeholder goes out verbatim.
        text = _joke() if r["text"].strip() == "{joke}" else r["text"]
        if not text:
            continue
        try:
            _rcon(f"broadcast {r['where']} {_ingame(_signed(text))}")
            _say_log({"t": now, "text": text, "where": r["where"], "to": "all", "by": r["id"]})
            _log("say.scheduled", rule=r["id"], when=r["when"])
        except Exception as e:
            _log("say.failed", ok=False, rule=r["id"], error=f"{type(e).__name__}: {e}"[:120])


@app.get("/api/valheim/say/history")
def say_history():
    try:
        return {"messages": json.loads(VH_SAY.read_text())[::-1]}
    except Exception:
        return {"messages": []}


# The world is a disc a little over 10 km across; the edge is where the sea stops being
# survivable. Coordinates come back as x, z, y - z is the north-south axis and y is height,
# which is not the order anyone expects.
WORLD_RADIUS = 10500
PLAYER_POS_RE = re.compile(r"(\S+)/(.+?)/(\S+)\s+\((-?\d+),\s*(-?\d+),\s*(-?\d+)\)")


# valheim-map.world renders in the browser and has no API, so nothing here pretends to
# talk to it. The admin does the two clicks that site is good at - download the rendered
# map, create a lobby for shared pins - and the panel keeps the results.
VH_MAPIMG = Path(f"{VH_DIR}/worldmap.png")
VH_WORLDCFG = Path(f"{VH_DIR}/worldmap.json")


def _world_cfg():
    cfg = {"lobby_view": "", "lobby_edit": "", "map_seed": "", "map_at": 0}
    try:
        cfg.update(json.loads(VH_WORLDCFG.read_text()))
    except Exception:
        pass
    cfg["map"] = VH_MAPIMG.exists()
    return cfg


@app.get("/api/valheim/world/links")
def world_links():
    return _world_cfg()


@app.post("/api/valheim/world/links")
def world_links_set(body: dict = Body(...)):
    cfg = _world_cfg()
    for k in ("lobby_view", "lobby_edit"):
        if k in body:
            v = str(body[k] or "").strip()[:300]
            # only that site, and only https - this link is handed to players
            if v and not v.startswith("https://valheim-map.world/"):
                raise HTTPException(400, "Expecting a https://valheim-map.world/ link")
            cfg[k] = v
    _save_json(VH_WORLDCFG, {k: cfg[k] for k in ("lobby_view", "lobby_edit", "map_seed", "map_at")})
    _log("world.links", view=bool(cfg["lobby_view"]), edit=bool(cfg["lobby_edit"]))
    return _world_cfg()


@app.post("/api/valheim/world/map")
def world_map_upload(data: bytes = Body(...)):
    if not data or len(data) > 40 * 1024 * 1024:
        raise HTTPException(400, "Expecting an image under 40 MB")
    if not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
        raise HTTPException(400, "PNG or JPEG only")
    VH_MAPIMG.write_bytes(data)
    cfg = _world_cfg()
    cfg["map_seed"] = (_world_card().get("fwl") or {}).get("seed_name") or ""
    cfg["map_at"] = int(time.time())
    _save_json(VH_WORLDCFG, {k: cfg[k] for k in ("lobby_view", "lobby_edit", "map_seed", "map_at")})
    _log("world.map_upload", bytes=len(data), seed=cfg["map_seed"])
    return _world_cfg()


@app.delete("/api/valheim/world/map")
def world_map_delete():
    VH_MAPIMG.unlink(missing_ok=True)
    _log("world.map_delete")
    return _world_cfg()


@app.get("/api/valheim/world/map")
def world_map():
    if not VH_MAPIMG.exists():
        raise HTTPException(404, "No map uploaded")
    head = VH_MAPIMG.read_bytes()
    return Response(head, media_type="image/png" if head.startswith(b"\x89PNG") else "image/jpeg")


@app.get("/api/valheim/players/positions")
def player_positions():
    """Where everyone is standing, straight from the game rather than from the log.

    Admin side only, and deliberately not on the public page: a live position feed is the
    one thing on this server that could get somebody raided in their sleep.
    """
    out = {"radius": WORLD_RADIUS, "players": [], "seed": None, "error": None}
    try:
        card = _world_card()
        out["seed"] = (card.get("fwl") or {}).get("seed_name")
        raw = _rcon("playerlist")
        for m in PLAYER_POS_RE.finditer(raw):
            pid, name, _cid, x, z, y = m.groups()
            out["players"].append({"id": pid, "name": name.strip(),
                                   "x": int(x), "z": int(z), "y": int(y)})
    except HTTPException:
        out["error"] = "admin tools are not installed"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:120]
    return out


def _wipe_world(world, wipe_mods=False):
    """Back up, stop, and erase the world together with everything counted about it.

    The order matters and every step here was learned the hard way. The backup goes first
    and it is the only way back. The login history is not deleted but *watermarked* - the
    panel rebuilds it from the journal, so an empty file simply fills up again with the
    players who were here yesterday.

    The server is deliberately left stopped: the caller decides what happens next. A manual
    reset starts it again immediately, while the launch installs the new game build first -
    starting in between would put the old world back up on the old build for as long as the
    download takes.
    """
    backup = _sh_ok(f"runuser -u valheim -- {VH_DIR}/backup.sh", timeout=180).strip()[-120:]
    _sh_ok("systemctl stop valheim", timeout=180)

    # Mods are optional here on purpose. A fresh world with the same mod set is a normal
    # thing to want; so is going back to vanilla. Wiping them also drops the share code,
    # so the players have to be told either way.
    if wipe_mods:
        _game_sh(f"cd {VH_SERVER} && rm -rf BepInEx doorstop_libs unstripped_corlib "
               f"doorstop_config.ini start_game_bepinex.sh start_server_bepinex.sh .doorstop_version")
        st = _mods_state()
        st["mods"], st["profile_code"], st["profile_name"], st["bepinex_version"] = {}, None, None, None
        _mods_save(st)

    removed = []
    for p in Path(VH_WORLDS).glob(f"{world}*"):
        # exact name only - a glob on "Klans" would also take the world "Klans2"
        if p.name != world and not p.name.startswith((f"{world}.", f"{world}_backup_auto-")):
            continue
        # 1.0 keeps folders, older builds files; either way removed by the game user
        if p.is_dir() or p.suffix in (".db", ".fwl", ".old") or ".db" in p.name or ".fwl" in p.name:
            removed.append(p.name)
    if removed:
        _game_sh(f"cd {shlex.quote(VH_WORLDS)} && rm -rf -- " + " ".join(shlex.quote(n) for n in removed))
    for f in (VH_SAY, VH_METRICS, VH_LIFE):
        f.unlink(missing_ok=True)
    _save_json(VH_STORE, {"players": {}, "last_ts": int(time.time())})
    _sh(f"chown valheim:valheim {VH_STORE}")
    GREETED.clear()
    JOKED.clear()
    WATCH["online"], WATCH["death_ts"] = {}, 0
    return backup, removed


@app.post("/api/valheim/world/reset")
def world_reset(body: dict = Body(default={})):
    """Start over: new world, empty statistics, counters from zero.

    The world file only appears on the first save, so the public page shows no day or
    clock until the server has written one - up to an autosave away.
    """
    world = _parse_env(Path(VH_ENV).read_text().splitlines())["world"]
    if body.get("confirm") != world:
        raise HTTPException(400, "Confirm with the world name")

    wipe_mods = bool(body.get("mods"))
    backup, removed = _wipe_world(world, wipe_mods)

    _sh_ok("systemctl start valheim", timeout=180)
    _log("world.reset", world=world, files=len(removed), mods_wiped=wipe_mods, backup=backup)
    _notify("maintenance", "World reset",
            f"{world} started over. The old one is in {backup or 'the backups'}.", tags="new")
    return {"ok": True, "world": world, "removed": removed, "mods_wiped": wipe_mods, "backup": backup}


# ---------- launch mode: the countdown, and the release that ends it ----------
# Written for the 1.0 release, and general enough for any release after it. While it is armed
# the public page is a placeholder with a timer, the server stays down, and the panel waits.
#
# What it waits for is the point. On a Valheim release day the game client updates before the
# dedicated server does - the version-mismatch window is a known part of every one of them -
# so a countdown that started the server when the clock ran out would start it on a build that
# does not exist yet, and the players would get "failed to connect" instead of a launch. So the
# clock only decides when to *start looking*: what actually triggers the release is Steam's
# build id for the dedicated server app itself moving off the one installed here. When that
# number changes the new server build is real and downloadable, and only then does anything
# happen. A late release is therefore handled by doing nothing, which is the correct response.
STEAM_APP = 896660                # Valheim Dedicated Server; the game client is 892970
VH_LAUNCH = Path(f"{VH_DIR}/launch.json")
LAUNCH_POLL = 300                 # a steamcmd round trip every five minutes while waiting
LAUNCH_DEFAULT = {
    "armed": False,               # placeholder up, build being watched
    "target": "",                 # ISO 8601 local time the timer counts down to
    "title": "",                  # headline on the placeholder; blank uses a default
    "message": "",                # a line under it - what the players should expect
    "wipe_world": True,           # bring the new build up on a fresh map
    "wipe_mods": False,           # and, if asked, without the mods the old one carried
    "stop_server": True,          # keep the server down until the build lands
    "baseline_build": "",         # build id at the moment of arming; anything else is the release
    "timer_was": "",              # is-enabled before arming, so it goes back exactly as found
    "game_was": "",               # ditto for the game service itself
    "restore_mods": True,         # bring the admin tooling back once the launch is done
    "mods_restored": False,
    "mods_tried": None,           # the version set that took the server down, if one did
    "mods_checked": 0,
    "mods_note": "",
    "released": False,            # the build landed, the release ran, the placeholder is gone
    "released_at": 0,
    "released_build": "",
    "checked": 0,                 # last Steam round trip, so the tick can pace itself
    "note": "",                   # what the last check or release had to say
}
_LAUNCH_BUSY = {"at": 0}          # a release runs in a thread and takes minutes; never twice


def _launch_cfg():
    cfg = dict(LAUNCH_DEFAULT)
    try:
        cfg.update(json.loads(VH_LAUNCH.read_text()))
    except Exception:
        pass
    return cfg


def _launch_save(cfg):
    _save_json(VH_LAUNCH, cfg, indent=1)
    return cfg


def _launch_waiting(cfg=None):
    """Armed and not yet released - the state in which the public page is a placeholder."""
    cfg = cfg or _launch_cfg()
    return bool(cfg.get("armed")) and not cfg.get("released")


def _launch_stood_down(cfg=None):
    """Armed, not released, and the game is meant to stay down until the new build lands.
    Everything that restarts the server on its own has to ask this first - the 05:00
    window did not, and on 2026-09-06 it brought the old world back up behind a page that
    was still counting down to its replacement."""
    cfg = cfg or _launch_cfg()
    return _launch_waiting(cfg) and bool(cfg.get("stop_server", True))


_GAME_UPDATE_LOCK = threading.Lock()
_UPDATE_BUSY = {"at": 0}


def _game_may_restart(now_on, need_empty=True):
    """The one question every automatic restart asks before touching the game. A launch that
    keeps the game down wins over everything, a release in progress too, and - unless the
    caller has its own rule for that - so does anyone playing. Until 2026-09-07 the
    maintenance window, the memory guard and update.sh each asked something slightly
    different, and the window won against the launch."""
    if _launch_stood_down() or _LAUNCH_BUSY["at"] or _UPDATE_BUSY["at"]:
        return False
    return not (need_empty and now_on)


def _steam_install():
    """steamcmd, straight: the caller decides what to stop first and what to start after."""
    return _sh(f"runuser -u valheim -- env HOME={VH_DIR} {VH_DIR}/steamcmd/steamcmd.sh "
               f"+force_install_dir {VH_SERVER} +login anonymous "
               f"+app_update {STEAM_APP} validate +quit", timeout=3600)


def _launch_target_ts(cfg):
    """The timer's target as a unix timestamp, 0 if it was never set or does not parse."""
    try:
        return int(datetime.fromisoformat(cfg["target"]).timestamp())
    except Exception:
        return 0


def _update_timer(on, was="enabled"):
    """The stock update timer installs a new build and starts the server the moment it is
    done. That is right on any ordinary day and wrong on this one: it would bring the old
    world up on the new build, for however long it takes the tick to notice and wipe it out
    from under whoever had already joined. So it is stood down while the launch is armed.

    disable, not stop: the wait is measured in days, and a container that reboots in the
    middle of one would otherwise come back with the timer running and the launch quietly
    sabotaged. What it was before is remembered, so putting it back cannot switch on a timer
    the operator had deliberately switched off.
    """
    if not on:
        return _sh("systemctl disable --now valheim-update.timer", timeout=30)
    if was == "enabled":
        return _sh("systemctl enable --now valheim-update.timer", timeout=30)
    return None


def _game_service(on, was="enabled"):
    """Stopping the game is not enough to keep it stopped, and this was learned in the worst
    possible way: eighteen minutes into the first armed window CT 108 rebooted, and the game
    service - being enabled - brought the old world straight back up behind a page that was
    still counting down to its replacement. A launch waits for days, so anything it switches
    off has to stay off across a reboot.
    """
    if not on:
        return _sh("systemctl disable --now valheim", timeout=180)
    _sh("systemctl start valheim", timeout=180)
    if was != "disabled":          # never switch on autostart the operator had switched off
        _sh("systemctl enable valheim", timeout=30)
    return None


def _launch_release(cfg, latest):
    """The new server build exists: install it, optionally start over, take the page down."""
    world = _parse_env(Path(VH_ENV).read_text().splitlines())["world"]
    steps, backup, removed = [], "", []
    try:
        if cfg.get("wipe_world"):
            backup, removed = _wipe_world(world, bool(cfg.get("wipe_mods")))
            steps.append(f"wiped {world} ({len(removed)} files)")
        else:
            backup = _sh_ok(f"runuser -u valheim -- {VH_DIR}/backup.sh", timeout=180).strip()[-120:]
            _sh_ok("systemctl stop valheim", timeout=180)
            steps.append("kept the world")

        up = _steam_install()
        if up.returncode != 0:
            # Not fatal, and deliberately so. A server that is up on the old build is a
            # server people can play on; one left stopped because a download failed at 98%
            # is an outage nobody is awake to fix. The panel says what happened either way.
            steps.append(f"steamcmd failed (rc={up.returncode})")
        else:
            steps.append(f"installed build {latest}")
    finally:
        _game_service(True, cfg.get("game_was"))
        _update_timer(True, cfg.get("timer_was"))

    cfg.update(armed=False, released=True, released_at=int(time.time()),
               released_build=latest, note="; ".join(steps))
    _launch_save(cfg)
    _log("launch.released", build=latest, world=world, wiped=bool(cfg.get("wipe_world")),
         files=len(removed), backup=backup, steps=steps)
    _notify("maintenance", "Launch — server is up",
            f"Build {latest} is installed and {world} is live. " +
            (f"Fresh world; the old one is in {backup or 'the backups'}." if removed
             else "The world carried over."),
            priority="high", tags="rocket")
    return cfg


# Narzedzia admina, nie mody gracza: powitania na ekranie, mapa z pozycjami i „zapisz swiat
# przed kopia" mowia przez RCON, a RCON jest tu modem. Premiera nie konczy sie wiec w chwili,
# gdy serwer wstanie — konczy sie, gdy to wroci.
LAUNCH_MODS = ["AviiNL-rcon", "JereKuusela-Rcon_Commands", "JereKuusela-Server_devcommands"]
MODS_POLL = 3600
MODS_SETTLE = 600            # niech nowy build sie ustoi, zanim cokolwiek do niego dokladamy


def _server_healthy(wait=150):
    """Czy wstal i *zostal* wstany.

    Odczyt zaraz po `systemctl start` nic nie znaczy: usluga melduje sie jako active w chwili,
    gdy proces ruszyl, a mod zbudowany pod poprzednia wersje gry kladzie ja kilka sekund pozniej.
    Stad odczekanie, a do tego dowod z samej gry — linia z wersja pada dopiero, gdy silnik
    faktycznie doszedl do startu, wiec proces zywy, ale wiszacy, nie przejdzie.
    """
    time.sleep(wait)
    if _sh("systemctl is-active valheim").stdout.strip() != "active":
        return False
    since = _sh('systemctl show valheim -p ActiveEnterTimestamp --value').stdout.strip()
    log = _sh(f'journalctl -u valheim --since "{since}" --no-pager -o cat 2>/dev/null'
              ' | grep -c "Valheim version:"', timeout=60).stdout.strip()
    return log.isdigit() and int(log) > 0


def _mods_restore_tick(now):
    """Po premierze oddaj adminowi jego narzedzia — sam, ale nigdy kosztem zywego serwera.

    Daty wydania na Thunderstore NIE nadaja sie na bramke „czy juz pod 1.0": `AviiNL-rcon` nie
    byl ruszany od 2024 roku, a mod, ktory dalej dziala, nie dostanie nowej wersji tylko po to,
    zeby nam cos udowodnic. Czekanie na przebudowe czekaloby wiec w nieskonczonosc. Jedyna
    uczciwa odpowiedz na „czy te mody dzialaja z nowa gra" to zainstalowac je i zobaczyc —
    co jest bezpieczne wylacznie dlatego, ze nieudana proba sie wycofuje.

    Daty sluza do czegos innego: zeby nie powtarzac w kolko tej samej nieudanej proby. Po
    wpadce panel czeka, az na Thunderstore pojawi sie INNY zestaw wersji niz ten, ktory polegl.
    """
    cfg = _launch_cfg()
    if not (cfg.get("released") and cfg.get("restore_mods")) or cfg.get("mods_restored"):
        return
    if _LAUNCH_BUSY["at"] or now - cfg.get("mods_checked", 0) < MODS_POLL:
        return
    if now - (cfg.get("released_at") or 0) < MODS_SETTLE:
        return
    cfg["mods_checked"] = now

    # _ts_latest answers None when Thunderstore does not (it used to be shadowed by a raising
    # twin, so this branch never ran and an outage went on to "install" None and roll back)
    avail = {full: _ts_latest(full) for full in LAUNCH_MODS}
    if not all(avail.values()):
        cfg["mods_note"] = "Thunderstore unreachable - trying again later"
        _launch_save(cfg)
        return
    # installing restarts the server, twice if it rolls back - never with people on
    if not _game_may_restart(WATCH["online"]):
        cfg["mods_note"] = "waiting for an empty server"
        _launch_save(cfg)
        return
    if avail == cfg.get("mods_tried"):
        cfg["mods_note"] = ("these exact versions already took the server down; waiting for a "
                            "rebuild of " + ", ".join(f"{k} {v}" for k, v in avail.items()))
        _launch_save(cfg)
        return

    _LAUNCH_BUSY["at"] = now
    # the players' own modpack, if the launch kept it - a rollback must not take it too
    kept = set(_mods_state().get("mods", {})) - set(LAUNCH_MODS)

    def run():
        c = _launch_cfg()
        try:
            rep = mods_install(ModPick(mods=[{"full_name": k, "version": v}
                                             for k, v in avail.items()], restart=True))
            ok = not rep.get("failed") and _server_healthy()
            if ok:
                c.update(mods_restored=True, mods_tried=None,
                         mods_note="installed " + ", ".join(f"{k} {v}" for k, v in avail.items()))
                _notify("maintenance", "Admin tooling is back",
                        "RCON and the server console are in. In-game messages, the player map "
                        "and the world save before each backup work again.", tags="wrench")
            else:
                # Wycofanie, a nie zostawienie trupa: to chodzi bez nadzoru, a mod zbudowany pod
                # poprzednia wersje gry zabiera ze soba caly serwer. Lepszy waniliowy i zywy.
                if kept:
                    for full in LAUNCH_MODS:
                        mods_remove(full, restart=False)
                    _sh("systemctl restart valheim", timeout=180)
                else:
                    mods_clear(ModClear(start=True))
                c.update(mods_tried=avail, mods_checked=int(time.time()),
                         mods_note="rolled back — the server did not stay up with them")
                _notify("mod_failed", "Admin tooling rolled back",
                        "The mods installed but the server did not stay up, so they were removed "
                        "and it is running clean again. Will retry when they are rebuilt.",
                        priority="high", tags="warning")
            _launch_save(c)
            _log("launch.mods_restore", ok=ok, versions=avail, note=c["mods_note"])
        except Exception as e:
            c = _launch_cfg()
            c.update(mods_tried=avail,
                     mods_note=f"restore failed: {type(e).__name__}: {e}"[:200])
            _launch_save(c)
            _log("launch.mods_restore", ok=False, error=c["mods_note"])
        finally:
            _LAUNCH_BUSY["at"] = 0

    import threading
    threading.Thread(target=run, daemon=True).start()


def _launch_tick(now):
    """Called once a minute. Cheap unless it is actually waiting for something."""
    cfg = _launch_cfg()
    if not _launch_waiting(cfg) or _LAUNCH_BUSY["at"]:
        return
    target = _launch_target_ts(cfg)
    if not target:
        return
    # Two speeds, for two different jobs. Before the timer runs out nothing can be released,
    # so the hourly check exists only to keep the baseline current: Iron Gate can ship an
    # ordinary patch in the days before a launch, and a stale baseline would still be reading
    # "there is a newer build" when the timer expired - releasing on the patch instead of on
    # the launch, and wiping the world for it. After the target the check is frequent, because
    # that window is the entire reason this exists.
    before = now < target
    if now - cfg.get("checked", 0) < (3600 if before else LAUNCH_POLL):
        return
    cfg["checked"] = now
    try:
        installed, latest = _steam_latest_build()
    except Exception as e:
        cfg["note"] = f"Steam check failed: {type(e).__name__}"
        _launch_save(cfg)
        return
    if not latest:
        cfg["note"] = "Steam did not answer with a build id"
        _launch_save(cfg)
        return
    if before:
        cfg["baseline_build"] = latest
        cfg["note"] = f"waiting for the timer — current server build {latest}"
        _launch_save(cfg)
        return
    base = cfg.get("baseline_build") or installed
    if latest == base:
        cfg["note"] = f"timer is up, Steam still has build {latest} — waiting for the server build"
        _launch_save(cfg)
        return

    _LAUNCH_BUSY["at"] = now
    _log("launch.detected", baseline=base, latest=latest)
    _notify("update_available", "Launch — new server build",
            f"Steam has build {latest} (was {base}). Installing it now.", tags="rocket")

    def run():
        try:
            _launch_release(_launch_cfg(), latest)
        except Exception as e:
            c = _launch_cfg()
            c["note"] = f"release failed: {type(e).__name__}: {e}"[:200]
            _launch_save(c)
            _log("launch.failed", ok=False, error=c["note"])
            _notify("maintenance", "Launch failed", c["note"], priority="urgent", tags="warning")
        finally:
            _LAUNCH_BUSY["at"] = 0

    # In a thread because steamcmd downloads the whole game and _tick blocks the event
    # loop while it runs - a panel frozen for twenty minutes is how you end up rebooting
    # the box during the one window you cannot afford to.
    import threading
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/launch")
def launch_get():
    cfg = _launch_cfg()
    cfg["target_ts"] = _launch_target_ts(cfg)
    cfg["waiting"] = _launch_waiting(cfg)
    cfg["busy"] = bool(_LAUNCH_BUSY["at"])
    cfg["world"] = _parse_env(Path(VH_ENV).read_text().splitlines())["world"]
    return cfg


@app.post("/api/launch")
def launch_set(body: dict = Body(...)):
    cfg = _launch_cfg()
    was = _launch_waiting(cfg)
    for k in ("wipe_world", "wipe_mods", "stop_server", "restore_mods"):
        if k in body:
            cfg[k] = bool(body[k])
    for k, n in (("target", 40), ("title", 120), ("message", 280)):
        if k in body:
            cfg[k] = str(body[k])[:n]
    if "armed" in body:
        cfg["armed"] = bool(body["armed"])

    if cfg["armed"] and not was:
        if not _launch_target_ts(cfg):
            raise HTTPException(400, "Set a target date the timer can count down to")
        # Snapshot the build being replaced. Read now rather than at release time, because
        # by then the thing we would be comparing against is the answer itself.
        try:
            installed, _ = _steam_latest_build()
        except Exception:
            installed = ""
        cfg["baseline_build"] = installed
        cfg["timer_was"] = _sh("systemctl is-enabled valheim-update.timer").stdout.strip()
        cfg.update(released=False, released_at=0, released_build="", checked=0,
                   note=f"waiting for a build newer than {installed or 'the one installed'}")
        _update_timer(False)
        cfg["game_was"] = _sh("systemctl is-enabled valheim").stdout.strip()
        if cfg.get("stop_server"):
            _game_service(False)
    elif was and not cfg["armed"]:
        _update_timer(True, cfg.get("timer_was"))
        if cfg.get("stop_server"):
            _game_service(True, cfg.get("game_was"))
        cfg["note"] = "disarmed"

    _launch_save(cfg)
    _log("launch.config", armed=cfg["armed"], target=cfg["target"],
         wipe_world=cfg["wipe_world"], baseline=cfg["baseline_build"])
    return launch_get()


@app.post("/api/launch/now")
def launch_now():
    """Release by hand: the build is out but the panel has not got there yet, or the timer
    was set to the wrong hour. Same path as the automatic one, so it wipes and installs
    exactly the same way."""
    cfg = _launch_cfg()
    if not _launch_waiting(cfg):
        raise HTTPException(400, "Launch mode is not armed")
    if _LAUNCH_BUSY["at"]:
        raise HTTPException(409, "A release is already running")
    _LAUNCH_BUSY["at"] = int(time.time())
    try:
        _, latest = _steam_latest_build()
    except Exception:
        latest = ""

    def run():
        try:
            _launch_release(_launch_cfg(), latest or "manual")
        except Exception as e:
            c = _launch_cfg()
            c["note"] = f"release failed: {type(e).__name__}: {e}"[:200]
            _launch_save(c)
            _log("launch.failed", ok=False, error=c["note"])
        finally:
            _LAUNCH_BUSY["at"] = 0

    import threading
    threading.Thread(target=run, daemon=True).start()
    _log("launch.manual", build=latest)
    return {"ok": True, "build": latest}


# ---------- launcher for players ----------
# The upstream generator hands every player an FTP account baked into the exe, and FTP is
# plaintext. Here the launcher pulls the same things over the panel's own HTTPS: a manifest
# of mod files with their hashes, the files themselves, and the background. Nothing here is
# a secret - it is the mod list this server already publishes - so these routes are open,
# and they answer 404 the moment the launcher is switched off.
def _launcher_cfg():
    # address is the one thing a launcher cannot work out for itself and the one thing that
    # moves: a home connection changes IP, DDNS follows it, and an IP baked into an exe is
    # wrong by morning. So it lives here and travels in the manifest, which means the only
    # constant inside the exe is where the panel is.
    cfg = {"enabled": False, "note": "", "bg_at": 0, "address": ""}
    try:
        cfg.update(json.loads(VH_LAUNCHER.read_text()))
    except Exception:
        pass
    cfg["background"] = VH_LAUNCHER_BG.exists()
    cfg["repo"] = LAUNCHER_REPO
    return cfg


# Mods that exist to run the server, not to play on it: RCON, admin consoles and the
# like. Sending them to players installs an admin toolkit on their machine and, worse,
# drags the mod's config along - which is where the RCON password lives.
_SERVER_ONLY = re.compile(r"rcon|server_devcommands|servercommands|admin", re.I)

# A config line that hands out a secret. Any file carrying one never leaves this box,
# whatever mod it belongs to - config file names cannot be mapped back to packages
# reliably, so this is checked on content rather than on the name.
# A config line whose KEY mentions a secret anywhere - "Admin Password", "RconPassword",
# "Discord Webhook", "Api Key" - with a value. The old pattern only caught keys that began
# with the word, and handed the rest to anyone who asked the launcher.
_SECRET_LINE = re.compile(rb"^[ \t]*[^#;\r\n=]*(pass(word|wd|phrase)|secret|token|webhook|api[ _-]?key|auth[ _-]?(key|code)|credential)[^=\r\n]*=[ \t]*\S",
                          re.I | re.M)
# the admin tooling runs on the server only; players have no use for its settings
_ADMIN_CFG = re.compile(r"rcon|devcommands", re.I)


def _client_mods():
    """Which installed mods a player actually needs. Server-only packages are excluded
    by default; the admin can override either way in the Launcher tab."""
    st = _mods_state()
    picks = st.get("client_mods")
    out = {}
    for full_name in (st.get("mods") or {}):
        if isinstance(picks, dict) and full_name in picks:
            out[full_name] = bool(picks[full_name])
        else:
            out[full_name] = not _SERVER_ONLY.search(full_name)
    return out


def _lan_ip():
    """This machine's address on the LAN. The game runs in this very container, so the
    address players on the LAN must use is simply ours."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))     # documentation range: routed, never answered
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def _join_address(request, cfg):
    """Where this particular player should point the game.

    The public name resolves to the reverse proxy, not to this container - that is how the
    panel is reachable over HTTPS at all. A player on the LAN who follows it lands on the
    proxy, which has nothing listening on the game port, and the game hangs on a server
    that looks dead. So: a request that came from a private address gets our LAN address,
    everyone else gets the public name."""
    try:
        addr = ipaddress.ip_address(_client_ip(request))
        if addr.is_private or addr.is_loopback:
            return _lan_ip() or cfg.get("address") or None
    except Exception:
        pass
    return cfg.get("address") or None


_MOD_FILES_CACHE = {"sig": None, "data": [], "at": 0.0}


def _mod_files():
    """Cached in front of the real work below. Hashing every mod on every call is fine for
    three admin plugins and ruinous for a seventy-mod pack — and the manifest is now the
    allow-list for downloads, so it is consulted once per file a player fetches. The cache
    key is a cheap stat sweep: a changed size or mtime anywhere re-hashes, nothing else
    does. Even that sweep is skipped for a few seconds at a time, because a player pulling
    a big pack asks hundreds of times a minute and mods do not change mid-download."""
    now = time.time()
    if _MOD_FILES_CACHE["sig"] is not None and now - _MOD_FILES_CACHE["at"] < 5:
        return _MOD_FILES_CACHE["data"]
    root = Path(VH_SERVER) / "BepInEx"
    try:
        sig = (tuple(sorted((p.as_posix(), s.st_size, int(s.st_mtime))
                            for p in root.rglob("*")
                            if p.is_file() for s in (p.stat(),))),
               tuple(sorted(_client_mods().items())))
    except Exception:
        sig = None
    if sig is not None and sig == _MOD_FILES_CACHE["sig"]:
        _MOD_FILES_CACHE["at"] = now
        return _MOD_FILES_CACHE["data"]
    data = _mod_files_uncached()
    _MOD_FILES_CACHE.update(sig=sig, data=data, at=now)
    return data


def _mod_files_uncached():
    """Every file a client needs, with a hash so the launcher can tell what changed.

    Nothing is shipped when no mod is meant for players: a server whose only mods are
    admin tools expects players to run vanilla, and handing them BepInEx anyway would
    mean the launcher installs a loader nobody asked for.

    Config files ride along on purpose - a server that tunes a mod expects the players to
    run the same numbers - except the ones carrying a secret."""
    root = Path(VH_SERVER) / "BepInEx"
    out = []
    if not root.exists():
        return out
    wanted = {m for m, on in _client_mods().items() if on}
    if not wanted:
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith("cache/") or rel.endswith(".log") or "/logs/" in rel:
            continue
        if rel.startswith("plugins/") and rel.split("/")[1] not in wanted:
            continue
        if rel.startswith("config/"):
            if _ADMIN_CFG.search(rel):
                continue
            try:
                if _SECRET_LINE.search(p.read_bytes()):
                    continue
            except Exception:
                continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out.append({"path": rel, "size": p.stat().st_size, "sha256": h.hexdigest()})
    return out


# The manifest is signed. Players reach the panel over plain http more often than not, and
# the manifest decides which DLLs land in their game - anyone on the path could otherwise
# swap it (and the hashes in it) and run code on every player. The key is made on first use
# and its public half goes into every launcher this panel builds (panel_config.json), so a
# launcher trusts exactly the panel it came from.
VH_MANIFEST_KEY = Path(f"{VH_DIR}/manifest.key")


def _manifest_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    if not VH_MANIFEST_KEY.exists():
        raw = Ed25519PrivateKey.generate().private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw,
                                                         ser.NoEncryption())
        fd = os.open(VH_MANIFEST_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    return Ed25519PrivateKey.from_private_bytes(VH_MANIFEST_KEY.read_bytes())


def _manifest_pub():
    from cryptography.hazmat.primitives import serialization as ser
    return base64.b64encode(_manifest_key().public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()


@app.get("/api/launcher/manifest")
def launcher_manifest(request: Request):
    body = json.dumps(_launcher_manifest(request), separators=(",", ":")).encode()
    return Response(body, media_type="application/json",
                    headers={"X-Manifest-Signature": base64.b64encode(_manifest_key().sign(body)).decode(),
                             "X-Manifest-Key": _manifest_pub()})


def _launcher_manifest(request):
    cfg = _launcher_cfg()
    if not cfg.get("enabled"):
        raise HTTPException(404, "Launcher is off")
    env = _parse_env(Path(VH_ENV).read_text().splitlines())
    st = _mods_state()
    files = _mod_files()
    return {"server": {"name": env["name"], "port": env["port"],
                       "address": _join_address(request, cfg),
                       "password_required": bool(env["password"]),
                       "crossplay": env["crossplay"]},
            # the admin tooling is the server's business, and listing it told the internet
            # that an RCON port was there to knock on
            "mods": [{"full_name": k, "version": v.get("version"), "name": v.get("name")}
                     for k, v in sorted(st.get("mods", {}).items()) if k not in ADMIN_TOOLS],
            "profile_code": st.get("profile_code"),
            "files": files,
            "bytes": sum(f["size"] for f in files),
            "background": ("/api/launcher/background?v=%s" % cfg.get("bg_at")) if cfg["background"] else None,
            "engine": {"repo": LAUNCHER_REPO,
                       "releases": f"https://api.github.com/repos/{LAUNCHER_REPO}/releases/latest"},
            "note": cfg.get("note") or None}


@app.get("/api/launcher/files/{path:path}")
def launcher_file(path: str):
    if not _launcher_cfg().get("enabled"):
        raise HTTPException(404, "Launcher is off")
    # Only what the manifest advertises. Serving anything under BepInEx looked equivalent
    # and was not: it handed out the admin mods' configs, and one of those carries the
    # RCON password. The manifest is the allow-list, so the two can never drift apart.
    if path.replace("\\", "/").lstrip("/") not in {f["path"] for f in _mod_files()}:
        raise HTTPException(404, "No such file")
    root = (Path(VH_SERVER) / "BepInEx").resolve()
    target = (root / path).resolve()
    # unauthenticated route: a link planted under BepInEx must not hand out panel.env
    if not target.is_relative_to(root) or (root / path).is_symlink():
        raise HTTPException(404, "No such file")
    return Response(target.read_bytes(), media_type="application/octet-stream")


@app.get("/api/launcher/background")
def launcher_background():
    if not _launcher_cfg().get("enabled") or not VH_LAUNCHER_BG.exists():
        raise HTTPException(404, "No background")
    head = VH_LAUNCHER_BG.read_bytes()
    kind = "image/png" if head[:4] == b"\x89PNG" else (
        "image/jpeg" if head[:2] == b"\xff\xd8" else "video/mp4")
    return Response(head, media_type=kind,
                    headers={"Cache-Control": "public, max-age=604800"})


# The launcher polls this every few seconds and measures the round-trip as the
# player's "ping" - so the answer must come from memory, always. The status
# script costs real seconds; it runs in a background thread when the cache goes
# stale, and the request being served never waits for it.
_LAUNCHER_STATUS = {"t": 0.0, "data": None, "busy": False}


def _launcher_status_refresh():
    try:
        env = _parse_env(Path(VH_ENV).read_text().splitlines())
        active = _sh("systemctl is-active valheim").stdout.strip() == "active"
        started = _sh('date -d "$(systemctl show valheim -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0').stdout.strip()
        conns = []
        if active:
            try:
                conns, _h, _c, _cts, _v, _jc = _scan(_sections(_sh(VH_STATUS_SH, timeout=90).stdout).get("log", []))
            except Exception:
                conns = []
        out = {"name": env["name"], "online": active,
               "uptime": (int(time.time()) - int(started)) if active and started.isdigit() and int(started) else None,
               "players": len(conns),
               "names": [c.get("name") for c in conns if c.get("name")]}
        _LAUNCHER_STATUS.update(t=time.time(), data=out)
        return out
    finally:
        _LAUNCHER_STATUS["busy"] = False


@app.get("/api/launcher/status")
def launcher_status():
    """Open on purpose: the launcher shows the player whether it is worth
    pressing Play before they join. The names are what they would see in-game
    anyway, and the admin opted into all of this by switching the launcher on."""
    if not _launcher_cfg().get("enabled"):
        raise HTTPException(404, "Launcher is off")
    cached = _LAUNCHER_STATUS["data"]
    if cached is not None:
        if time.time() - _LAUNCHER_STATUS["t"] >= 10 and not _LAUNCHER_STATUS["busy"]:
            _LAUNCHER_STATUS["busy"] = True
            import threading
            threading.Thread(target=_launcher_status_refresh, daemon=True).start()
        return cached
    # very first call since the panel started - nothing to serve yet
    _LAUNCHER_STATUS["busy"] = True
    return _launcher_status_refresh()


@app.get("/api/launcher/download")
def launcher_download(request: Request, platform: str = ""):
    """The engine release on GitHub is neutral: it points at no server at all.
    This route turns it into THIS server's launcher - the panel writes its own
    address into panel_config.json BESIDE the program (inside a signed macOS
    bundle it would break the signature) and hands the player a ready build.
    Cached per engine tag and platform, so one download from GitHub serves
    everyone until a new engine is out."""
    if not _launcher_cfg().get("enabled"):
        raise HTTPException(404, "Launcher is off")
    plat = _platform_for(platform, request.headers.get("user-agent", ""))
    # cached: this route is open to anyone, and each uncached call spent one of the 60 GitHub
    # API requests an hour this address gets - the same budget the panel's own update check uses
    if time.time() - _ENGINE_REL["at"] > 600 or not _ENGINE_REL["rel"]:
        _ENGINE_REL.update(rel=_github_json(f"https://api.github.com/repos/{LAUNCHER_REPO}/releases/latest"),
                           at=time.time())
    rel = _ENGINE_REL["rel"]
    tag = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", [])
                  if a.get("name") == f"launcher-{plat}.zip"), None)
    if not tag or not asset:
        raise HTTPException(503, f"No {plat} engine release on GitHub yet")
    # The panel's public address is whatever name this request came in on -
    # zero configuration, and it is right for LAN and for the internet alike.
    # This route is open to anyone, and the host goes into the build and its cache key -
    # so every made-up Host header used to cost a full rebuild. Forwarded headers count
    # only from our own proxy, the name must look like one, and a build for a new name
    # waits its turn: one at a time, at most one every half minute.
    proxied = (request.client.host if request.client else "") in _trusted_proxies()
    host = (request.headers.get("x-forwarded-host") if proxied else None) or request.headers.get("host", "")
    proto = (request.headers.get("x-forwarded-proto") if proxied else None) or "http"
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}(:\d{1,5})?", host) or proto not in ("http", "https"):
        raise HTTPException(400, "Odd host name")
    env = _parse_env(Path(VH_ENV).read_text().splitlines())
    config = json.dumps({"serverName": env["name"], "panelUrl": f"{proto}://{host}",
                         "engineRepo": LAUNCHER_REPO, "manifestKey": _manifest_pub()})
    base = _exe_base(env["name"])
    # The name of the program is part of what makes this build this server's, so it
    # belongs in the cache key - renaming the server must not serve the old name.
    key = hashlib.sha256(f"{tag}|{config}|{base}|{plat}".encode()).hexdigest()[:12]
    dist = Path(VH_DIR) / "launcher-dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"launcher-{plat}-{tag}-{key}.zip"
    if not out.exists():
        if not _LAUNCHER_BUILD.acquire(timeout=120):
            raise HTTPException(503, "Another launcher build is running - try again in a minute")
        try:
            _launcher_build(out, dist, plat, tag, asset, config, env, base)
        finally:
            _LAUNCHER_BUILD.release()
    return FileResponse(out, filename=f"{base} Launcher ({plat}).zip",
                        media_type="application/zip")


_ENGINE_REL = {"at": 0.0, "rel": None}
_LAUNCHER_BUILD = threading.Lock()
_LAUNCHER_LAST_BUILD = {}                # platform -> when a build for it last started


def _launcher_build(out, dist, plat, tag, asset, config, env, base):
    if not out.exists():                  # re-checked: the build may have happened while waiting
        if time.time() - _LAUNCHER_LAST_BUILD.get(plat, 0) < 30:
            raise HTTPException(429, "A launcher was just built - try again in half a minute",
                                {"Retry-After": "30"})
        _LAUNCHER_LAST_BUILD[plat] = time.time()
        import shutil
        import zipfile
        raw = dist / f"engine-{plat}-{tag}.zip"
        if not raw.exists():
            tmp = raw.with_suffix(".part")
            req = urllib.request.Request(asset["browser_download_url"],
                                         headers={"User-Agent": "valheim-proxmox-panel"})
            with urllib.request.urlopen(req, timeout=600) as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f)
            tmp.rename(raw)
            for old in dist.glob(f"engine-{plat}-*.zip"):
                if old != raw:
                    old.unlink(missing_ok=True)
        # The panel hands this to every player, so it is held to the project's release key
        # like its own updates - checked on every build, the cached copy included: a release
        # someone else put on GitHub is never served.
        try:
            sig = _fetch(asset["browser_download_url"] + ".sig", 30)
        except Exception:
            sig = b""
        if not _release_signed(raw.read_bytes(), sig):
            raw.unlink(missing_ok=True)
            raise HTTPException(503, f"The launcher release {tag} is not signed with the project key - not serving it")
        # What the player double-clicks, named after the server. Renaming a Flutter
        # build is safe: it finds its data next to itself, by position, not by name.
        rename = {"windows": ("server_launcher.exe", f"{base} Launcher.exe"),
                  "linux": ("server_launcher", f"{base} Launcher"),
                  "macos": ("server_launcher.app", f"{base} Launcher.app")}[plat]
        part = out.with_suffix(".part")
        with zipfile.ZipFile(raw) as src, \
                zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                name = item.filename
                if name.endswith("flutter_assets/assets/panel_config.json"):
                    continue                       # the placeholder from the build
                if name == rename[0] or name.startswith(rename[0] + "/"):
                    name = rename[1] + name[len(rename[0]):]
                blob = src.read(item.filename)
                # The icon Windows shows in Explorer lives inside the exe, so it
                # cannot be set at runtime like the window icon - the server's
                # initials go into the resource here, while the file is ours to
                # rewrite. macOS is left alone on purpose: editing anything inside
                # a signed .app breaks its seal.
                if plat == "windows" and item.filename == "server_launcher.exe":
                    blob = icon_badge.patch_exe_icon(blob, icon_badge.badge_png(env["name"]))
                info = zipfile.ZipInfo(name, date_time=item.date_time)
                # Carry permissions over: the launcher and the macOS bundle's inner
                # binary have to stay executable, and a plain writestr would drop that.
                info.external_attr = item.external_attr
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = item.create_system
                dst.writestr(info, blob)
            dst.writestr("panel_config.json", config)
        part.rename(out)
        # the four newest stay - LAN and public name both, and a download still streaming
        # an older one is not cut off under the player
        for old in sorted(dist.glob(f"launcher-{plat}-*.zip"), key=lambda f: f.stat().st_mtime)[:-4]:
            old.unlink(missing_ok=True)


def _exe_base(server_name):
    """A server name trimmed down to what a file name may contain."""
    return re.sub(r"[^A-Za-z0-9._ -]", "", server_name).strip() or "Valheim"


def _platform_for(asked, user_agent):
    """Which build to hand over. An explicit ?platform= wins; otherwise the
    browser's own user agent decides, because a player clicking Download on the
    public page should not have to know what to pick."""
    asked = (asked or "").strip().lower()
    if asked in ("windows", "macos", "linux"):
        return asked
    ua = user_agent.lower()
    if "mac os x" in ua or "macintosh" in ua:
        return "macos"
    if "linux" in ua and "android" not in ua:
        return "linux"
    return "windows"


def _github_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-proxmox-panel",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _mem_limit(cfg=None):
    """Where memory stops being normal for THIS server. A bare server rests near
    12%, a seventy-mod one near 41% - one fixed number would either loop on the
    modded install or never fire on the bare one, so the allowance grows with the
    mod list. Returns 0 when the guard is switched off."""
    sched = (cfg or _alerts_cfg())["schedule"]
    base = float(sched.get("restart_mem_base", 45) or 0)
    if base <= 0:
        return 0
    per = float(sched.get("restart_mem_per_mod", 0.5) or 0)
    mods = len((_mods_state().get("mods") or {}))
    return round(min(95.0, base + per * mods), 1)


@app.get("/api/valheim/launcher")
def launcher_get():
    cfg = _launcher_cfg()
    cfg["files"] = len(_mod_files())
    cfg["client_mods"] = _client_mods()
    return cfg


@app.post("/api/valheim/launcher/mods")
def launcher_mods_set(body: dict = Body(...)):
    """Which mods the launcher hands to players. Server-only packages start off; an admin
    who knows better can flip any of them."""
    st = _mods_state()
    picks = dict(st.get("client_mods") or {})
    for name, on in (body.get("mods") or {}).items():
        if name in (st.get("mods") or {}):
            picks[name] = bool(on)
    st["client_mods"] = picks
    _mods_save(st)
    _log("launcher.client_mods", **{k: v for k, v in picks.items()})
    return {"client_mods": _client_mods(), "files": len(_mod_files())}


@app.post("/api/valheim/launcher")
def launcher_set(body: dict = Body(...)):
    cfg = _launcher_cfg()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "note" in body:
        cfg["note"] = str(body["note"] or "")[:200]
    if "address" in body:
        cfg["address"] = str(body["address"] or "").strip()[:120]
    _save_json(VH_LAUNCHER, {k: cfg[k] for k in ("enabled", "note", "bg_at", "address")})
    _log("launcher.config", enabled=cfg["enabled"])
    return _launcher_cfg()


@app.post("/api/valheim/launcher/background")
def launcher_bg_upload(data: bytes = Body(...)):
    if not data or len(data) > 60 * 1024 * 1024:
        raise HTTPException(400, "Expecting a file under 60 MB")
    if not (data[:4] == b"\x89PNG" or data[:2] == b"\xff\xd8" or b"ftyp" in data[:32]):
        raise HTTPException(400, "PNG, JPEG or MP4 only")
    VH_LAUNCHER_BG.write_bytes(data)
    cfg = _launcher_cfg()
    # the launcher caches the background and only refetches when this stamp changes
    _save_json(VH_LAUNCHER, {"enabled": cfg["enabled"], "note": cfg.get("note", ""),
                             "address": cfg.get("address", ""), "bg_at": int(time.time())})
    _log("launcher.background", bytes=len(data))
    return _launcher_cfg()


@app.delete("/api/valheim/launcher/background")
def launcher_bg_delete():
    VH_LAUNCHER_BG.unlink(missing_ok=True)
    _log("launcher.background_removed")
    return _launcher_cfg()


@app.get("/api/valheim/world/card")
def world_card(world: str = ""):
    if world and not VH_NAME_RE.match(world):
        raise HTTPException(400, "Invalid world name")
    return _world_card(world or None)


@app.get("/api/valheim/health")
def health():
    try:
        return json.loads(VH_HEALTH.read_text())
    except Exception:
        return {"crashes": [], "backup": None}


@app.post("/api/valheim/backups/verify")
def backup_verify(body: dict = Body(default={})):
    fn = body.get("file")
    if not fn:
        names = sorted(_ls(_sh(f"ls -l --time-style=+%s {VH_BACKUPS}").stdout.splitlines()),
                       key=lambda b: b["mtime"], reverse=True)
        if not names:
            raise HTTPException(404, "No backups yet")
        fn = names[0]["name"]
    res = _verify_backup(_bak_ok(fn))
    st = _health_state()
    st["backup"] = res
    _write_health(st)
    _log("backup.verify", file=fn, ok=res["ok"], error=res.get("error"))
    return res


@app.post("/api/valheim/backups/{fn}/restore")
def backup_restore(fn: str):
    _bak_ok(fn)
    # snapshot the current world first — restoring by mistake has to be reversible
    _sh_ok(f"runuser -u valheim -- {VH_DIR}/backup.sh", timeout=120)
    # Unpacked into an empty folder, not over the live one: a 1.0 world keeps numbered saves
    # and loads the highest, so the newer save left in place would win over the restored
    # one. What was there stays in worlds_local.prev until the next restore.
    # Step by step, so a failure anywhere puts the world that was there back and the game up
    # on it - one long command chain used to die on its timeout with the game stopped and
    # the folder half unpacked.
    # all of it as the game user: worlds_local and its neighbours live in that user's folder,
    # and so does the archive
    w = shlex.quote(VH_WORLDS)
    _sh_ok("systemctl stop valheim", timeout=180)
    try:
        _game_sh(f"rm -rf {w}.prev && mv {w} {w}.prev", timeout=300)
    except Exception:
        _sh("systemctl start valheim", timeout=180)
        raise
    try:
        _game_sh(f"mkdir {w} && tar xzf {VH_BACKUPS}/{shlex.quote(fn)} -C {w}", timeout=900)
    except Exception:
        _sh(f"runuser -u valheim -- sh -c {shlex.quote(f'rm -rf {w} && mv {w}.prev {w}')}", timeout=300)
        _sh("systemctl start valheim", timeout=180)
        raise
    _sh_ok("systemctl start valheim", timeout=180)
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
MOD_NAME_RE = re.compile(r"^[A-Za-z0-9_]+\Z")
MOD_VER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\Z")


def _mods_state():
    try:
        return json.loads(VH_MODS_JSON.read_text())
    except Exception:
        return {"profile_code": None, "profile_name": None, "mods": {}}


def _mods_save(st):
    _save_json(VH_MODS_JSON, st, indent=1)


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
    """Unpack a Thunderstore zip. `strip` drops a leading folder the package wraps itself in.
    Into a folder only root can touch, then copied into place by the game user: unpacking
    straight into server/ had root check a path and then write it, with a mod able to swap a
    folder for a link in between."""
    final = Path(dest)
    stage = Path(tempfile.mkdtemp(dir=VH_DIR, prefix=".unpack-"))
    try:
        _unpack_into(data, stage, strip)
        _sh_ok(f"chown -hR valheim:valheim {shlex.quote(str(stage))} && chmod 755 {shlex.quote(str(stage))}")
        _sh_ok(f"runuser -u valheim -- mkdir -p {shlex.quote(str(final))} && "
               f"runuser -u valheim -- cp -a {shlex.quote(str(stage))}/. {shlex.quote(str(final))}/", timeout=300)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _unpack_into(data, dest, strip=None):
    import zipfile
    import io
    dest = Path(dest)
    skip = {"icon.png", "readme.md", "changelog.md", "license", "license.txt"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            # Some packages are zipped on Windows and carry backslashes as separators. Both
            # checks below must run on the normalised name: is_dir() looks for a trailing "/",
            # so "plugins\\Translations\\" would otherwise land as an empty *file* named
            # Translations, and every real file under it then fails with "not a directory".
            name = info.filename.replace("\\", "/")
            if name.endswith("/") or info.is_dir():
                continue
            if strip and name.startswith(strip):
                name = name[len(strip):]
            if not name or name.split("/")[-1].lower() in skip and "/" not in name:
                continue
            # a package that ships a plugins/ folder means "put my content there"
            if name.startswith("plugins/"):
                name = name[len("plugins/"):]
            out = dest / name
            # is_relative_to, not a string prefix: "../ns-NameX" passed a startswith check, and
            # resolve() also catches a link planted inside the folder pointing out of it
            if not out.resolve().is_relative_to(dest.resolve()):
                raise HTTPException(400, f"Package tries to escape its directory: {info.filename}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.unlink(missing_ok=True)          # never write through an existing link
            out.write_bytes(z.read(info))


def _install_bepinex():
    pkg = _ts_package(*BEPINEX)
    ver = pkg["latest"]["version_number"]
    data = _ts_get(pkg["latest"]["download_url"], timeout=180)
    _unpack(data, VH_SERVER, strip="BepInExPack_Valheim/")
    _game_sh(f"chmod -R u+rwX {VH_SERVER}/BepInEx")    # unpacked by the game user already
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
    _game_sh(f"rm -rf {shlex.quote(str(target))}")
    _unpack(data, target)
    meta = {}
    try:
        mf = json.loads((target / "manifest.json").read_text())
        meta = {"name": mf.get("name"), "description": (mf.get("description") or "")[:200],
                "website": mf.get("website_url") or None}
    except Exception:
        pass
    return {"full_name": full, "version": version, "size": len(data), **meta}


def _profile(code):
    """Expand a Thunderstore Mod Manager share code into a mod list."""
    import base64 as _b
    import zipfile
    import io
    import yaml
    if not re.match(r"^[A-Za-z0-9-]{8,64}\Z", code or ""):
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


TS_CACHE = {}          # full_name -> (checked_at, latest_version)


def _ts_latest(full_name, max_age=3600):
    """Newest version on Thunderstore, cached — the index is huge and this runs per package.
    Also fills in the display name and description for packages installed before manifests
    were kept, so nobody has to re-download 1.4 GB just to get pretty names."""
    hit = TS_CACHE.get(full_name)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    ns, _, name = full_name.partition("-")
    ver, meta = None, {}
    try:
        pkg = _ts_package(ns, name)
        ver = pkg["latest"]["version_number"]
        meta = {"name": pkg.get("name") or name,
                "description": (pkg["latest"].get("description") or "")[:200],
                "website": pkg.get("package_url")}
    except Exception:
        pass
    TS_CACHE[full_name] = (time.time(), ver)
    if meta:
        st = _mods_state()
        if full_name in st.get("mods", {}):
            st["mods"][full_name].update({k: v for k, v in meta.items() if v})
            _mods_save(st)
    return ver


@app.get("/api/mods")
def mods_state(check: bool = False):
    st = _mods_state()
    installed = _sh(f"ls -1 {VH_PLUGINS} 2>/dev/null").stdout.split()
    mods = []
    for d in sorted(installed):
        rec = dict(st.get("mods", {}).get(d, {}))
        rec["full_name"] = d
        if not rec.get("name"):
            try:
                mf = json.loads((Path(VH_PLUGINS) / d / "manifest.json").read_text())
                rec["name"] = mf.get("name")
                rec["description"] = (mf.get("description") or "")[:200]
                rec.setdefault("version", mf.get("version_number"))
            except Exception:
                rec["name"] = d.partition("-")[2] or d
        rec["author"] = d.partition("-")[0]
        rec["pinned"] = bool(rec.get("pinned"))
        if check:
            rec["latest"] = _ts_latest(d)
            rec["outdated"] = bool(rec.get("latest") and rec.get("version") and
                                   rec["latest"] != rec["version"] and not rec["pinned"])
        mods.append(rec)
    return {"bepinex": _sh(f"test -d {VH_SERVER}/BepInEx && echo yes").stdout.strip() == "yes",
            "bepinex_version": st.get("bepinex_version"),
            "profile_code": st.get("profile_code"), "profile_name": st.get("profile_name"),
            "checked": check, "mods": mods}


class Pin(BaseModel):
    pinned: bool = True


@app.post("/api/mods/pin/{full_name}")
def mods_pin(full_name: str, p: Pin):
    """Pinned means: never offered for update. The versions your players run are the ones that
    matter, and a server that quietly moves ahead of them bounces everyone at the door."""
    st = _mods_state()
    if full_name not in st.get("mods", {}):
        raise HTTPException(404, "Not installed")
    st["mods"][full_name]["pinned"] = p.pinned
    _mods_save(st)
    _log("mods.pin", package=full_name, pinned=p.pinned)
    return {"ok": True}


@app.post("/api/mods/export")
def mods_export():
    """Turn what is installed into a Thunderstore share code, so players import one code
    instead of a list of names and versions."""
    import base64 as _b
    import io
    import zipfile
    st = _mods_state()
    mods = st.get("mods", {})
    if not mods:
        raise HTTPException(400, "Nothing installed to export")
    lines = ["profileName: " + (st.get("profile_name") or "Server"), "mods:"]
    for full, rec in sorted(mods.items()):
        maj, _, rest = (rec.get("version") or "0.0.0").partition(".")
        minor, _, patch = rest.partition(".")
        lines += [f"  - name: {full}",
                  f"    versionNumber: {{major: {maj or 0}, minor: {minor or 0}, patch: {patch or 0}}}",
                  "    enabled: true"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("export.r2x", "\n".join(lines) + "\n")
    body = ("#r2modman\n" + _b.b64encode(buf.getvalue()).decode()).encode()
    req = urllib.request.Request(f"{TS}/api/experimental/legacyprofile/create/", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/octet-stream",
                                          "User-Agent": "valheim-proxmox-panel"})
    try:
        code = json.loads(urllib.request.urlopen(req, timeout=30).read())["key"]
    except Exception as e:
        raise HTTPException(502, f"Thunderstore refused the profile ({e})")
    st["profile_code"] = code
    _mods_save(st)
    _log("mods.export", code=code, count=len(mods))
    return {"ok": True, "code": code, "count": len(mods)}


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
        _sh_ok(f"runuser -u valheim -- {VH_DIR}/backup.sh", timeout=120)
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
            st.setdefault("mods", {})[got["full_name"]] = {
                k: v for k, v in got.items() if k in ("version", "name", "description", "website")}
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
    if report["failed"]:
        _notify("mod_failed", "Mod install failed",
                ", ".join(f["full_name"] for f in report["failed"]), priority="high", tags="x")
    _log("mods.install", ok=not report["failed"], code=p.code or None,
         installed=[m["full_name"] + " " + m["version"] for m in report["installed"]],
         failed=report["failed"] or None, bepinex=report.get("bepinex"))
    return report


class ModUpdate(BaseModel):
    mods: list = []
    restart: bool = True


@app.post("/api/mods/update")
def mods_update(u: ModUpdate):
    st = _mods_state()
    was_running = _sh("systemctl is-active valheim").stdout.strip() == "active"
    if was_running:
        _sh_ok("systemctl stop valheim", timeout=180)
    done, failed = [], []
    for full in u.mods:
        if st.get("mods", {}).get(full, {}).get("pinned"):
            failed.append({"full_name": full, "error": "pinned"})
            continue
        ns, _, name = full.partition("-")
        try:
            got = _install_mod(ns, name)          # no version = latest
            st["mods"][full] = {**st["mods"].get(full, {}),
                                **{k: v for k, v in got.items()
                                   if k in ("version", "name", "description", "website")}}
            done.append(got)
        except HTTPException as e:
            failed.append({"full_name": full, "error": e.detail})
    _mods_save(st)
    if u.restart or was_running:
        _sh_ok("systemctl start valheim", timeout=180)
    if failed:
        _notify("mod_failed", "Mod update failed", ", ".join(f["full_name"] for f in failed),
                priority="high", tags="x")
    _log("mods.update", ok=not failed, updated=[m["full_name"] + " " + m["version"] for m in done],
         failed=failed or None)
    return {"ok": True, "updated": done, "failed": failed}


class ModClear(BaseModel):
    start: bool = True


@app.post("/api/mods/clear")
def mods_clear(c: ModClear):
    """Back to vanilla: snapshot the world, stop, wipe BepInEx and every plugin."""
    _sh_ok(f"runuser -u valheim -- {VH_DIR}/backup.sh", timeout=120)
    _sh_ok("systemctl stop valheim", timeout=180)
    _game_sh(f"cd {VH_SERVER} && rm -rf BepInEx doorstop_libs unstripped_corlib "
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
    if not re.match(r"^[A-Za-z0-9_-]{1,80}\Z", full_name):
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
CFG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.(cfg|json|ya?ml|txt|ini|xml)\Z")
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
    _game_sh(f"mkdir -p {shlex.quote(str(d))}")
    _write(d / str(int(time.time())), _read_as_game(p))
    old = sorted(d.iterdir(), key=lambda f: f.name, reverse=True)[CFG_KEEP:]
    if old:
        _game_sh("rm -f " + " ".join(shlex.quote(str(f)) for f in old))


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
    _write(str(p), _read_as_game(src))
    _log("config.restore", file=name, version=stamp, restarted=restart)
    if restart:
        _sh_ok("systemctl restart valheim", timeout=180)
    return {"ok": True, "restarted": restart}


# ---------- alerts (ntfy) + maintenance window ----------
# Until now the panel only looked at the server when someone opened the page. This is the
# background watcher: it polls once a minute, pushes what changed to ntfy, and owns the
# "restart only when nobody is playing" window.
VH_ALERTS = Path(os.environ.get("VH_ALERTS", f"{VH_DIR}/alerts.json"))
VH_PUBLIC = Path(f"{VH_DIR}/public.json")
VH_METRICS = Path(f"{VH_DIR}/metrics.json")
VH_LINK = Path(f"{VH_DIR}/link.json")
VH_HEALTH = Path(f"{VH_DIR}/health.json")   # crashes and the last backup verification
VH_SAY = Path(f"{VH_DIR}/messages.json")   # what the panel has said in game, and when
VH_LIFE = Path(f"{VH_DIR}/uptime.json")   # availability since this world began
VH_LAUNCHER = Path(f"{VH_DIR}/launcher.json")
VH_LAUNCHER_BG = Path(f"{VH_DIR}/launcher-bg")   # image or video the launcher shows
LAUNCHER_REPO = "PawelSzymanski89/valheim_launcher_proxmox"
VH_RESTART_DONE = Path(f"{VH_DIR}/restart-done.json")  # last nightly restart, across panel restarts
VH_MEM_GUARD = Path(f"{VH_DIR}/mem-guard.json")  # cooldown that survives a panel restart
VH_RCON_ENV = f"{VH_DIR}/rcon.env"        # readable by the game user, unlike panel.env
HEALTH_KEEP = 50


def _health_state():
    try:
        return json.loads(VH_HEALTH.read_text())
    except Exception:
        return {"crashes": [], "backup": None}


def _write_health(st):
    st["crashes"] = (st.get("crashes") or [])[-HEALTH_KEEP:]
    try:
        _save_json(VH_HEALTH, st)
    except Exception:
        pass


LINK_KEEP = 1008          # a ping every 10 min = a week of history
METRIC_POINTS = 1440          # one a minute = last 24 h
# Minimal by default. Everything here is visible to the whole internet, so each extra field
# is opt-in: a version number narrows down what to try against the server, a mod list and a
# load chart tell a stranger what runs there and when nobody is watching.
PUBLIC_DEFAULT = {"enabled": True, "show_players": True, "show_names": False,
                  "show_mods": False, "show_metrics": False, "show_version": False,
                  "show_board": False,
                  "show_address": False, "show_specs": False, "show_link": False, "note": ""}
VH_COUNT = Path(f"{VH_DIR}/players.count")   # read by update.sh so it can hold off too
ALERT_EVENTS = {
    "link_bad": "Connection degraded",
    "server_down": "Server stopped",
    "server_up": "Server is up",
    "player_join": "Player joined",
    "player_leave": "Player left",
    "player_death": "Player died",
    "server_crash": "Server crashed or was killed",
    "server_action": "Started or stopped from the panel",
    "load_high": "CPU or memory pegged",
    "backup_failed": "Backup failed",
    "disk_low": "Disk almost full",
    "update_available": "Game update available",
    "panel_login": "Someone signed in to the panel",
    "mod_failed": "Mod install failed",
    "maintenance": "Scheduled restart",
}
ALERTS_DEFAULT = {
    "enabled": True,
    "events": {k: k not in ("player_leave", "player_death") for k in ALERT_EVENTS},
    "schedule": {"restart_at": "", "only_when_empty": True, "defer_minutes": 30,
                 "update_when_empty": True, "disk_warn_gb": 3,
                 "link_minutes": 10, "link_speed_hours": 6,
                 "speed_when_empty": True, "ping_when_empty": False,
                 "load_pct": 95, "load_minutes": 1,
                 # The game server grows by a couple of hundred megabytes an hour
                 # even with nobody playing - Unity never hands memory back. The
                 # nightly window normally keeps that in check, but a server that
                 # is busy every night would never get restarted, so this is the
                 # floor under it: restart on memory, only ever on an empty server.
                 #
                 # The threshold cannot be one number, because what a server uses
                 # at rest depends almost entirely on how many mods it loads: this
                 # one sits near 12% bare and at 41% with seventy-two, reached
                 # within minutes of starting. So it is computed - a base for the
                 # game itself plus an allowance per installed mod - and only the
                 # slow climb above that counts as memory running away.
                 # A base of 0 turns the whole thing off.
                 "restart_mem_base": 45, "restart_mem_per_mod": 0.5},
}


def _ensure_topic():
    """ntfy needs no account — publishing to a name creates it. So the panel picks an
    unguessable one at install time and you only subscribe to it in the app. Anyone who
    learns the name receives the alerts, which is exactly why it is random and not "valheim"."""
    env = _env_file(VH_PANEL_ENV)
    if env.get("NTFY_TOPIC"):
        return env["NTFY_TOPIC"]
    topic = "valheim-" + secrets.token_hex(5)
    env["NTFY_TOPIC"] = topic
    _save_panel_env(env)
    _log("alerts.topic_generated", topic=topic)
    return topic


def _alerts_cfg():
    """Same split as the faktury app on this homelab: the topic and server are secrets and
    live in panel.env (600), while what to send and when lives in alerts.json."""
    cfg = json.loads(json.dumps(ALERTS_DEFAULT))
    try:
        saved = json.loads(VH_ALERTS.read_text())
        for k, v in saved.items():
            if isinstance(v, dict) and k in cfg:
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    env = _env_file(VH_PANEL_ENV)
    cfg["ntfy"] = {"server": env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
                   "topic": env.get("NTFY_TOPIC") or _ensure_topic(), "token": env.get("NTFY_TOKEN", "")}
    return cfg


def _notify(event, title, message, priority="default", tags=""):
    """ntfy, published as JSON rather than through HTTP headers.

    notify.py in the faktury app puts the title into the body precisely because HTTP headers
    cannot carry UTF-8 and Polish text came out mangled. The JSON endpoint fixes the same
    problem without giving up the title, priority and tags, so the phone still shows a proper
    notification headline instead of two lines of body text.
    """
    cfg = _alerts_cfg()
    if not cfg["ntfy"]["topic"] or (event != "__test__" and
                                    (not cfg.get("enabled") or not cfg["events"].get(event, False))):
        return False
    # Every push names the game, because a phone that also gets notifications from other
    # boxes only ever sees the title. Just the game: the server name added nothing a
    # single-server owner did not already know, and ate room the actual message needed.
    title = f"Valheim — {title}"
    payload = {"topic": cfg["ntfy"]["topic"], "title": title, "message": message,
               "priority": {"default": 3, "high": 4, "urgent": 5, "low": 2}.get(priority, 3)}
    if tags:
        payload["tags"] = [t for t in tags.split(",") if t]
    req = urllib.request.Request(cfg["ntfy"]["server"], method="POST",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    if cfg["ntfy"]["token"]:
        req.add_header("Authorization", "Bearer " + cfg["ntfy"]["token"])
    try:
        urllib.request.urlopen(req, timeout=8).read()
        _log("alert.sent", event=event, title=title)
        return True
    except Exception as e:
        _log("alert.failed", ok=False, event=event, error=str(e)[:200])
        return False


@app.post("/api/alerts/topic")
def alerts_new_topic():
    """New name, e.g. after sharing the old one by accident."""
    env = _env_file(VH_PANEL_ENV)
    env["NTFY_TOPIC"] = "valheim-" + secrets.token_hex(5)
    _save_panel_env(env)
    _log("alerts.topic_generated", topic=env["NTFY_TOPIC"])
    return {"ok": True, "topic": env["NTFY_TOPIC"]}


def _public_cfg():
    cfg = dict(PUBLIC_DEFAULT)
    try:
        cfg.update(json.loads(VH_PUBLIC.read_text()))
    except Exception:
        pass
    return cfg


_PUBLIC_CACHE = {"at": 0.0, "data": None, "lock": threading.Lock()}


@app.get("/api/public")
def public_status():
    """Cached for ten seconds: it is open to anyone, and one uncached answer costs five
    processes and a week of journal - a loop of requests used to take the panel down and
    load the machine the game runs on. One request builds, the rest wait for it."""
    if time.time() - _PUBLIC_CACHE["at"] < 10 and _PUBLIC_CACHE["data"] is not None:
        return _PUBLIC_CACHE["data"]
    with _PUBLIC_CACHE["lock"]:
        if time.time() - _PUBLIC_CACHE["at"] < 10 and _PUBLIC_CACHE["data"] is not None:
            return _PUBLIC_CACHE["data"]
        data = _public_status()
        _PUBLIC_CACHE.update(at=time.time(), data=data)
        return data


def _public_status():
    """The only endpoint reachable without logging in. Everything here is deliberate: the
    address and port are already public DNS, the mod list and share code are what a player
    needs before joining. The game password, disk, logs and checks never appear."""
    cfg = _public_cfg()
    if not cfg.get("enabled"):
        raise HTTPException(404, "Not enabled")

    # Waiting for a release: the page is a placeholder and a timer, and nothing else. This
    # returns early rather than adding a flag to the full payload on purpose - a server that
    # is deliberately down would otherwise publish an empty player count, a flat load chart
    # and a stale day number, all of which read as "broken" rather than "not started yet".
    lch = _launch_cfg()
    if _launch_waiting(lch):
        env = _parse_env(Path(VH_ENV).read_text().splitlines())
        return {"name": env["name"], "launch": {
            "target": lch["target"], "target_ts": _launch_target_ts(lch),
            "title": lch.get("title") or None, "message": lch.get("message") or None,
            "now": int(time.time())}}

    env = _parse_env(Path(VH_ENV).read_text().splitlines())
    st = _mods_state()
    active = _sh("systemctl is-active valheim").stdout.strip() == "active"
    started = _sh('date -d "$(systemctl show valheim -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0').stdout.strip()
    ver = _sh("journalctl -u valheim -n 2000 --no-pager -o cat | grep -m1 -oP 'Valheim version: \\K\\S+' | tail -1").stdout.strip()
    conns, _h, _c, _cts, _v, _jc = _scan(_sections(_sh(VH_STATUS_SH, timeout=90).stdout).get("log", []))
    out = {"name": env["name"], "world": env["world"], "active": active,
           "crossplay": env["crossplay"], "note": cfg.get("note") or None,
           "uptime": (int(time.time()) - int(started)) if active and started.isdigit() and int(started) else None}
    if cfg.get("show_version"):
        out["version"] = ver or None
    # The world name is on this page anyway, and the day it is on says something a stranger
    # cannot misuse - how far along the server is. Rides with the name, no separate switch.
    try:
        card = _world_card()
        out["day"] = card.get("day")
        out["clock"] = card.get("clock")
    except Exception:
        pass
    if cfg.get("show_address"):
        out["port"] = env["port"]
        out["password_required"] = bool(env["password"])
    if cfg.get("show_players"):
        out["players"] = len(conns)
        if cfg.get("show_names"):
            out["names"] = [c.get("name") for c in conns if c.get("name")]
    # Dostepnosc liczona z tych samych probek, ktore rysuja wykresy - jedna na minute,
    # 1440 punktow, czyli dokladnie doba. Nie deklaruje wiecej, niz ma w danych.
    try:
        pts = json.loads(VH_METRICS.read_text())[-1440:]
        if len(pts) >= 10:
            out["uptime24"] = {"pct": round(100 * sum(1 for p in pts if p.get("up")) / len(pts), 2),
                               "samples": len(pts), "minutes": len(pts)}
    except Exception:
        pass
    try:
        life = json.loads(VH_LIFE.read_text())
        if life.get("total", 0) >= 5:
            out["uptime_life"] = {"pct": round(100 * life["up"] / life["total"], 2),
                                  "since": life.get("since")}
    except Exception:
        pass
    if cfg.get("show_board"):
        # Names only. The ids are what a stranger could use to look someone up, so they stay
        # on this side of the wall even when the ranking is switched on.
        try:
            ps = json.loads(VH_STORE.read_text()).get("players", {}).values()
        except Exception:
            ps = []
        out["board"] = sorted(({"name": p["name"], "total": p.get("total", 0),
                                "sessions": p.get("sessions", 0), "deaths": p.get("deaths", 0)}
                               for p in ps if p.get("name")),
                              key=lambda x: x["total"], reverse=True)[:20]

    # launcher for players - only advertised when it is actually switched on
    lch = _launcher_cfg()
    if lch.get("enabled"):
        # download points at THIS panel, which injects its own address into the
        # neutral engine - the GitHub release alone would not know the server.
        out["launcher"] = {"repo": lch["repo"],
                           "download": "/api/launcher/download",
                           "releases": f"https://github.com/{lch['repo']}/releases/latest",
                           "note": lch.get("note") or None}
    if cfg.get("show_mods"):
        out["mods"] = [{"full_name": k, "version": v.get("version"), "name": v.get("name")}
                       for k, v in sorted(st.get("mods", {}).items(),
                                          key=lambda kv: (kv[1].get("name") or kv[0]).lower())]
        out["profile_code"] = st.get("profile_code")
    if cfg.get("show_specs"):
        m = (_sections(_sh("echo '@machine'; cat /proc/loadavg; nproc; "
                           "awk '/MemTotal|MemAvailable/{print $2}' /proc/meminfo",
                           timeout=20).stdout).get("machine") or [])
        try:
            la = m[0].split()
            cores, total, avail = int(m[1]), int(m[2]) * 1024, int(m[3]) * 1024
            disk = _sh(f"df -B1 --output=used,avail {VH_DIR} | tail -1").stdout.split()
            out["specs"] = {"cores": cores, "ram": total,
                            "disk": (int(disk[0]) + int(disk[1])) if len(disk) == 2 else None}
            # the ten-second sampler if it has run, the one-minute average as a fallback
            fresh = time.time() - LIVE["t"] < 30
            out["load"] = {
                "cpu": LIVE["cpu"] if fresh and LIVE["cpu"] is not None else round(100 * float(la[0]) / cores, 1),
                "mem": LIVE["mem"] if fresh and LIVE["mem"] is not None else round(100 * (1 - avail / total), 1)}
        except Exception:
            pass
    if cfg.get("show_link"):
        link = _link_state()
        live_ping = LIVE["ping"] if time.time() - LIVE["t"] < 30 else None
        out["link"] = {"ping": live_ping or link.get("ping"), "speed": link.get("speed"),
                       "checked": (LIVE["t"] if live_ping else link.get("last_ping")) or None}
        if cfg.get("show_metrics"):
            out["link"]["history"] = [{k: v for k, v in h.items()
                                       if k in ("t", "avg", "loss", "down_mbit", "up_mbit")}
                                      for h in (link.get("history") or [])[-1008:]]
    if cfg.get("show_metrics"):
        try:
            out["metrics"] = json.loads(VH_METRICS.read_text())[-720:]
        except Exception:
            out["metrics"] = []
    return out


@app.get("/api/public/config")
def public_cfg_get():
    return _public_cfg()


@app.post("/api/public/config")
def public_cfg_set(body: dict = Body(...)):
    cfg = _public_cfg()
    for k in ("enabled", "show_players", "show_names", "show_mods", "show_metrics",
              "show_version", "show_address", "show_specs", "show_link", "show_board"):
        if k in body:
            cfg[k] = bool(body[k])
    if "note" in body:
        cfg["note"] = str(body["note"])[:280]
    _save_json(VH_PUBLIC, cfg, indent=1)
    _PUBLIC_CACHE["at"] = 0
    _log("public.config", **{k: v for k, v in cfg.items() if k != "note"})
    return {"ok": True}


@app.get("/api/alerts")
def alerts_get():
    cfg = _alerts_cfg()
    cfg["subscribe_url"] = f"{cfg['ntfy']['server']}/{cfg['ntfy']['topic']}"
    cfg["ntfy"]["token"] = "***" if cfg["ntfy"].get("token") else ""     # never echoed back
    cfg["labels"] = ALERT_EVENTS
    # Wyliczony próg pamięci — admin ma zobaczyć konkretną liczbę dla SWOJEGO
    # serwera, a nie samemu mnożyć bazę przez liczbę modów.
    cfg["mem_limit"] = _mem_limit(cfg)
    cfg["mem_mods"] = len((_mods_state().get("mods") or {}))
    return cfg


@app.post("/api/alerts")
def alerts_set(body: dict = Body(...)):
    cfg = json.loads(json.dumps(ALERTS_DEFAULT))
    try:
        cfg.update({k: v for k, v in json.loads(VH_ALERTS.read_text()).items() if k in cfg})
    except Exception:
        pass
    n = body.get("ntfy") or {}
    env = _env_file(VH_PANEL_ENV)
    if n.get("server"):
        if not re.match(r"^https?://[\w.-]+(:\d+)?/?\Z", n["server"]):
            raise HTTPException(400, "ntfy server must be a plain http(s) URL, no path")
        env["NTFY_SERVER"] = n["server"].rstrip("/")
    if "topic" in n:
        if n["topic"] and not re.match(r"^[\w.-]{1,64}\Z", n["topic"]):
            raise HTTPException(400, "Topic: letters, digits, _ . - only")
        env["NTFY_TOPIC"] = n["topic"]
    if n.get("token") and n["token"] != "***":
        env["NTFY_TOKEN"] = n["token"]
    _save_panel_env(env)

    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    for k, v in (body.get("events") or {}).items():
        if k in ALERT_EVENTS:
            cfg["events"][k] = bool(v)
    s = body.get("schedule") or {}
    if "restart_at" in s:
        if s["restart_at"] and not re.match(r"^([01]\d|2[0-3]):[0-5]\d\Z", s["restart_at"]):
            raise HTTPException(400, "Restart time must be HH:MM")
        cfg["schedule"]["restart_at"] = s["restart_at"]
    for k in ("only_when_empty", "update_when_empty", "speed_when_empty", "ping_when_empty"):
        if k in s:
            cfg["schedule"][k] = bool(s[k])
    for k in ("defer_minutes", "disk_warn_gb", "link_minutes", "link_speed_hours"):
        if k in s:
            cfg["schedule"][k] = max(1, min(720, int(s[k])))
    if "restart_mem_base" in s:
        # 0 switches the guard off. The floor of 20 exists because anything lower
        # is below what the game uses with no mods at all.
        v = float(s["restart_mem_base"] or 0)
        cfg["schedule"]["restart_mem_base"] = 0 if v <= 0 else max(20.0, min(95.0, v))
    if "restart_mem_per_mod" in s:
        cfg["schedule"]["restart_mem_per_mod"] = max(
            0.0, min(3.0, float(s["restart_mem_per_mod"] or 0)))
    _save_json(VH_ALERTS, cfg, indent=1)
    _log("alerts.save", topic=env.get("NTFY_TOPIC") or None, enabled=cfg["enabled"],
         on=[k for k, v in cfg["events"].items() if v], schedule=cfg["schedule"])
    return {"ok": True}


@app.post("/api/alerts/test")
def alerts_test():
    cfg = _alerts_cfg()
    if not cfg["ntfy"].get("topic"):
        raise HTTPException(400, "Set a topic first")
    ok = _notify("__test__", "Valheim — panel serwera",
                 "Testowe powiadomienie. Jeśli to widzisz, alerty działają. ąćęłńóśżź",
                 tags="white_check_mark")
    if not ok:
        raise HTTPException(502, "ntfy did not accept the message — check the server, topic and token")
    return {"ok": True}


# ---------- link quality ----------
# For a game server the line matters more than the CPU: Valheim is UDP, so latency and packet
# loss decide how it feels. Those are cheap, so they run hourly. Throughput is not cheap — it
# means actually moving tens of megabytes — so it runs rarely and never while people play.
def _ping(host="1.1.1.1", count=5):
    out = _sh(f"ping -c {count} -q -w 15 {host}", timeout=30).stdout
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
    if not rtt:
        return None
    return {"host": host, "loss": float(loss.group(1)) if loss else None,
            "min": float(rtt.group(1)), "avg": float(rtt.group(2)),
            "max": float(rtt.group(3)), "jitter": float(rtt.group(4))}


def _speedtest(streams=4, chunk_mb=25):
    """Four parallel streams, summed — one is not enough.

    Measured on a 1000/600 line: a single 25 MB download reported 551 Mbit/s and a single
    100 MB upload 492, because on a 10 ms path most of a short transfer is TLS and TCP
    ramp-up. Four streams of the same size reported 923 down and 637 up, which is the line.
    25 MB per stream is also the ceiling that matters: Cloudflare answers 403 above 100 MB.
    """
    def _sum(cmd, key):
        tmp = "/tmp/.vh-speed"
        out = _sh(f"for i in $(seq {streams}); do ( {cmd} ) & done > {tmp}; wait; "
                  f"awk '{{s+=$1}} END{{printf \"%.0f\", s}}' {tmp}; rm -f {tmp}",
                  timeout=180).stdout.strip()
        return float(out) if out else 0.0

    bytes_ = chunk_mb * 1000000
    down = _sum(f"curl -s -o /dev/null -w '%{{speed_download}}\n' --max-time 60 "
                f"'https://speed.cloudflare.com/__down?bytes={bytes_}'", "down")
    up = _sum(f"head -c {bytes_} /dev/zero | curl -s -o /dev/null -X POST --data-binary @- "
              f"-w '%{{speed_upload}}\n' --max-time 60 https://speed.cloudflare.com/__up", "up")
    if not down and not up:
        return None
    return {"down_mbit": round(down * 8 / 1e6), "up_mbit": round(up * 8 / 1e6),
            "streams": streams, "moved_mb": streams * chunk_mb * 2}


def _link_state():
    try:
        return json.loads(VH_LINK.read_text())
    except Exception:
        return {"last_ping": 0, "last_speed": 0, "ping": None, "speed": None, "history": []}


@app.get("/api/valheim/link")
def link_status():
    return _link_state()


@app.post("/api/valheim/link/test")
def link_test(force: bool = False):
    """Measure now, regardless of the schedule — the button in the panel. Still refuses while
    people are playing unless you say force, because it pushes 200 MB through their line."""
    if not force:
        sec = _sections(_sh(VH_STATUS_SH, timeout=90).stdout)
        conns, *_ = _scan(sec.get("log", []))
        if conns:
            raise HTTPException(409, f"{len(conns)} playing — a throughput test moves 200 MB "
                                     "through the same line. Retry when the server is empty, "
                                     "or force it if you know what you are doing.")
    st = _link_state()
    st["ping"], st["last_ping"] = _ping(), int(time.time())
    st["speed"], st["last_speed"] = _speedtest(), int(time.time())
    st["history"] = (st.get("history") or [])[-167:] + [{"t": st["last_ping"], **(st["ping"] or {}),
                                                         **(st["speed"] or {})}]
    _save_json(VH_LINK, st)
    _log("link.test", ping=st["ping"], speed=st["speed"])
    return st


# ---------- background watcher ----------
WATCH = {"active": None, "online": {}, "disk_warned": 0, "build_checked": 0,
         "restart_done": "", "deferred_until": 0, "death_ts": 0, "restarts": None,
         "hot": {}, "hot_warned": {}}


def _steam_latest_build():
    out = _sh("runuser -u valheim -- env HOME=/opt/valheim /opt/valheim/steamcmd/steamcmd.sh "
              "+login anonymous +app_info_update 1 +app_info_print 896660 +quit 2>/dev/null | "
              "sed -n '/\"branches\"/,/^}/p' | sed -n '/\"public\"/,/}/p' | grep -m1 '\"buildid\"' | "
              "grep -oE '[0-9]+'", timeout=120).stdout.strip()
    installed = _sh("awk -F'\"' '/\"buildid\"/{print $4; exit}' "
                    "/opt/valheim/server/steamapps/appmanifest_896660.acf 2>/dev/null").stdout.strip()
    return installed, out


# Live figures for the public page. Neither of the existing sources moves at the rate the
# page refreshes: loadavg is a one-minute average, and the link ping runs on a ten-minute
# schedule because its history is a chart. These three are cheap enough to take every ten
# seconds - two /proc reads and three packets - and they live in memory only, so there is
# nothing to retain or clean up.
LIVE = {"t": 0, "cpu": None, "mem": None, "ping": None}
_CPU_PREV = {}


def _cpu_pct():
    """Utilisation between two samples, which is what "CPU now" should mean."""
    v = [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
    total, idle = sum(v), v[3] + (v[4] if len(v) > 4 else 0)   # idle + iowait
    prev, _CPU_PREV["v"] = _CPU_PREV.get("v"), (total, idle)
    if not prev or total <= prev[0]:
        return None
    dt, di = total - prev[0], idle - prev[1]
    return round(100 * (dt - di) / dt, 1)


def _live_tick():
    LIVE["cpu"] = _cpu_pct()
    try:
        mem = {k: int(v.split()[0]) for k, _, v in
               (l.partition(":") for l in Path("/proc/meminfo").read_text().splitlines())
               if k in ("MemTotal", "MemAvailable")}
        LIVE["mem"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
    except Exception:
        LIVE["mem"] = None
    LIVE["ping"] = _ping(count=3) or LIVE["ping"]
    LIVE["t"] = int(time.time())
    _load_watch()
    for fn in (_rules_tick, _greet_tick):
        try:
            fn()
        except Exception as e:
            _log("schedule.error", ok=False, error=f"{fn.__name__}: {type(e).__name__}: {e}"[:160])


def _load_watch():
    """A spike is a chunk loading; a minute pinned at the ceiling is the server struggling,
    which players feel as rubber-banding long before anything crashes. Ten-second samples
    make the difference visible, so the alert waits for the run rather than the spike."""
    try:
        sch = _alerts_cfg()["schedule"]
    except Exception:
        return
    lim, need = sch.get("load_pct", 95), max(10, int(float(sch.get("load_minutes", 1)) * 60))
    for key, label in (("cpu", "CPU"), ("mem", "Memory")):
        v, now = LIVE.get(key), LIVE["t"]
        if v is None or v < lim:
            WATCH["hot"][key] = None
            continue
        since = WATCH["hot"].get(key)
        if since is None:
            WATCH["hot"][key] = now
        elif now - since >= need and now - WATCH["hot_warned"].get(key, 0) > 1800:
            WATCH["hot_warned"][key] = now
            _notify("load_high", f"{label} at {v}%",
                    f"{label} has stayed at or above {lim}% for {max(1, round((now - since) / 60))} min. "
                    "Players will feel this as lag.", priority="high", tags="fire")


def _crash_watch(now):
    """systemd counts its own restarts and names the reason, which is the one place a
    container can learn the kernel did the killing: Result=oom-kill. Without it a crash
    loop and a clean restart look identical in the log. Returns the crash it recorded."""
    try:
        f = dict(l.split("=", 1) for l in _sh(
            "systemctl show valheim -p NRestarts -p Result -p ExecMainStatus -p ExecMainCode"
        ).stdout.splitlines() if "=" in l)
        n = int(f.get("NRestarts") or 0)
    except Exception:
        return None
    prev, WATCH["restarts"] = WATCH.get("restarts"), n
    if prev is None or n <= prev:        # first pass only takes a watermark
        return None
    # Result describes the unit's *last* outcome, so after a successful restart it reads
    # "success" - which says nothing about the stop that caused it. Name that case honestly.
    why = f.get("Result") or "unknown"
    if why == "success":
        why = "stopped unexpectedly"
    code = f.get("ExecMainStatus") or "0"
    crash = {"t": now, "result": why, "status": code, "n": n}
    st = _health_state()
    st["crashes"] = (st.get("crashes") or []) + [crash]
    _write_health(st)
    _log("server.crash", result=why, status=code, restarts=n)
    oom = why == "oom-kill"
    _notify("server_crash",
            "Server was killed by the kernel (out of memory)" if oom else "Server crashed",
            f"systemd restarted it. Reason: {why}"
            + (f", exit {code}" if code not in ("0", "") else "")
            + (". Give the container more RAM or it will happen again." if oom else "."),
            priority="high", tags="skull_and_crossbones" if oom else "warning")
    return crash


def _update_timer_on():
    return _sh("systemctl is-enabled valheim-update.timer").stdout.strip() == "enabled"


def _game_update_tick(now_on, manual=False):
    """Install a newer server build when there is one and nothing forbids a restart.
    `manual` is the panel button: it still asks the gate, but ignores the timer switch."""
    installed, latest = _steam_latest_build()
    if not latest or not installed or installed == latest:
        return {"updated": False, "installed": installed, "latest": latest}
    if not manual and not _update_timer_on():
        if WATCH.get("update_notified") != latest:
            WATCH["update_notified"] = latest
            _notify("update_available", "Valheim update available",
                    f"Steam has build {latest}, the server runs {installed}. "
                    "Automatic updates are off - the Update button installs it.", tags="arrow_up")
        return {"updated": False, "installed": installed, "latest": latest, "held": "timer off"}
    if not _game_may_restart(now_on):
        why = "launch" if _launch_stood_down() or _LAUNCH_BUSY["at"] else f"{len(now_on)} playing"
        if WATCH.get("update_notified") != latest:
            WATCH["update_notified"] = latest
            _notify("update_available", "Valheim update waiting",
                    f"Build {latest} is out; installing when the server is free ({why}).", tags="hourglass")
        _log("update.held", latest=latest, why=why)
        return {"updated": False, "installed": installed, "latest": latest, "held": why}
    # One install at a time, and while it runs nothing else restarts the game: the button runs
    # in a request thread, the tick in its own, and the memory guard or the nightly window
    # used to be able to start the server in the middle of a steamcmd install.
    if not _GAME_UPDATE_LOCK.acquire(blocking=False):
        return {"updated": False, "installed": installed, "latest": latest, "held": "an update is already running"}
    try:
        _UPDATE_BUSY["at"] = time.time()
        _log("update.start", installed=installed, latest=latest, manual=manual)
        _sh("systemctl stop valheim", timeout=180)
        up = _steam_install()
        _sh("systemctl start valheim", timeout=180)
    finally:
        _UPDATE_BUSY["at"] = 0
        _GAME_UPDATE_LOCK.release()
    WATCH["active"] = True
    ok = up.returncode == 0
    _log("update.done", ok=ok, latest=latest, rc=up.returncode)
    _notify("maintenance", "Valheim updated" if ok else "Valheim update failed",
            (f"Build {installed} → {latest}, server restarted." if ok
             else f"steamcmd returned {up.returncode}; the server is back on build {installed}."),
            tags="arrow_up" if ok else "warning")
    return {"updated": ok, "installed": installed, "latest": latest}


def _tick():
    cfg = _alerts_cfg()
    s = status()
    now = int(time.time())

    # Isolated, because everything below it is the monitoring this server runs on every day
    # and the launch is a thing that happens once. A bug in the new code should not be able
    # to take the crash watch and the backup verification down with it.
    try:
        _launch_tick(now)
        _mods_restore_tick(now)
    except Exception as e:
        _log("launch.tick_error", ok=False, error=f"{type(e).__name__}: {e}"[:200])

    # While the launch is armed the game is stood down on purpose (see _game_service). If it is
    # up anyway - a maintenance restart, a reboot, a hand on the start button - it goes back
    # down, and the two automatic restarts below sit this one out. The launch itself starts
    # it, on the new build, with the fresh world.
    stood_down = _launch_stood_down()
    if stood_down and s["active"] and not _LAUNCH_BUSY["at"]:
        _log("launch.stood_down_again", players=len(s["online"]))
        _notify("maintenance", "Launch — server put back down",
                "The game came up while the launch is armed; stopping it until the new build.",
                tags="hourglass")
        _game_service(False)
        s = status()

    # the game server going away, and coming back
    if WATCH["active"] is not None and s["active"] != WATCH["active"]:
        if s["active"]:
            _notify("server_up", "Valheim is up", f"World {s['settings']['world']} is running again.",
                    tags="green_circle")
        else:
            _notify("server_down", "Valheim stopped", "The game server is not running.",
                    priority="high", tags="red_circle")
    WATCH["active"] = s["active"]

    # who came and went since the last look
    now_on = {c["id"]: (c.get("name") or c["id"]) for c in s["online"]}
    for pid, name in now_on.items():
        if pid not in WATCH["online"]:
            _notify("player_join", "Player joined", f"{name} is on {s['settings']['name']}.",
                    tags="video_game")
    for pid, name in WATCH["online"].items():
        if pid not in now_on:
            _notify("player_leave", "Player left", f"{name} left.", tags="wave")
    WATCH["online"] = now_on

    # deaths, off the history the status pass has just refreshed. The first pass only takes
    # a watermark — otherwise a panel restart would replay every death ever recorded.
    seen = WATCH["death_ts"]
    newest = seen
    for p in s.get("history") or []:
        d = p.get("last_death") or 0
        newest = max(newest, d)
        if seen and d > seen:
            _notify("player_death", "Player died", f"{p.get('name') or p['id']} died.", tags="skull")
    WATCH["death_ts"] = newest or int(time.time())

    try:
        VH_COUNT.write_text(str(len(now_on)))     # update.sh reads this to hold off
    except Exception:
        pass

    # Availability for as long as this world has existed. The rolling series only keeps a
    # day, and a counter is the honest way to answer "how often is it up" - two integers
    # and the date they started, rather than a guess extrapolated from the last 24 hours.
    try:
        life = {"since": now, "up": 0, "total": 0}
        try:
            life.update(json.loads(VH_LIFE.read_text()))
        except Exception:
            pass
        life["total"] += 1
        life["up"] += 1 if s["active"] else 0
        _save_json(VH_LIFE, life)
    except Exception:
        pass

    # rolling series for the public page charts
    try:
        m = s.get("machine") or {}
        pts = []
        try:
            pts = json.loads(VH_METRICS.read_text())
        except Exception:
            pass
        pts.append({"t": now, "p": len(now_on), "up": 1 if s["active"] else 0,
                    "cpu": round(100 * m["load"][0] / m["cores"], 1) if m.get("cores") else None,
                    "mem": round(100 * (1 - m["mem_avail"] / m["mem_total"]), 1) if m.get("mem_total") else None})
        _save_json(VH_METRICS, pts[-METRIC_POINTS:])
    except Exception:
        pass

    # Two different costs, so two different rules. A ping is five packets over four seconds and
    # keeps running while people play — that is exactly when you want the evidence, because
    # "it was lagging last night" is unanswerable without it. Throughput moves 200 MB through
    # the same line, so it waits for an empty server. Both are settings.
    try:
        link = _link_state()
        sch = cfg["schedule"]
        ping_blocked = bool(now_on) and sch.get("ping_when_empty", False)
        if not ping_blocked and now - link.get("last_ping", 0) >= sch.get("link_minutes", 60) * 60:
            p = _ping()
            link["ping"], link["last_ping"] = p, now
            if p and ((p.get("loss") or 0) > 10 or p["avg"] > 150):
                _notify("link_bad", "Connection degraded",
                        f"{p['avg']} ms, {p.get('loss')}% packet loss — players will feel this.",
                        priority="high", tags="signal_strength")
            entry = {"t": now, **(p or {})}
            speed_ok = not (now_on and sch.get("speed_when_empty", True))
            if speed_ok and now - link.get("last_speed", 0) >= sch.get("link_speed_hours", 6) * 3600:
                s2 = _speedtest()
                link["speed"], link["last_speed"] = s2, now
                entry.update(s2 or {})
            link["history"] = (link.get("history") or [])[-(LINK_KEEP - 1):] + [entry]
            _save_json(VH_LINK, link)
    except Exception:
        pass

    _crash_watch(now)

    # One backup a day gets opened and read. Cheap on a schedule, priceless the one time it
    # comes back broken — an unverified backup is a guess, not a backup.
    try:
        st = _health_state()
        last = (st.get("backup") or {}).get("at", 0)
        if now - last >= 24 * 3600:
            names = sorted(_ls(_sh(f"ls -l --time-style=+%s {VH_BACKUPS}").stdout.splitlines()),
                           key=lambda b: b["mtime"], reverse=True)
            if names:
                res = _verify_backup(names[0]["name"])
                st["backup"] = res
                _write_health(st)
                if not res["ok"]:
                    _notify("backup_failed", "Backup did not verify",
                            f"{res['file']}: {res.get('error')}", priority="high", tags="warning")
    except Exception:
        pass

    # disk, warned once a day at most
    d = s.get("disk") or {}
    warn_gb = cfg["schedule"].get("disk_warn_gb", 3)
    if d and d["avail"] < warn_gb * 2**30 and now - WATCH["disk_warned"] > 86400:
        WATCH["disk_warned"] = now
        _notify("disk_low", "Disk almost full",
                f"{round(d['avail'] / 2**30, 1)} GB left. Backups stop being written long before it hits zero.",
                priority="high", tags="warning")

    # Game updates, every two hours - a steamcmd round trip. The install happens here, not
    # in update.sh: one place asks _game_may_restart, so an update can no longer slip past
    # the launch or a full server. valheim-update.timer stays the operator's on/off switch.
    try:
        _panel_update_tick(now_on)
    except Exception as e:
        _log("panel.update_error", ok=False, error=f"{type(e).__name__}: {e}"[:200])

    if now - WATCH["build_checked"] > 2 * 3600:
        WATCH["build_checked"] = now
        try:
            _game_update_tick(now_on)
        except Exception as e:
            _log("update.error", ok=False, error=f"{type(e).__name__}: {e}"[:200])

    # memory safety net - see restart_mem_base in ALERTS_DEFAULT for why it exists.
    # Never touches a server with people on it: growing memory is a slow problem and
    # kicking players out of a raid is a fast one.
    mem_pct = _mem_limit(cfg)
    if mem_pct and LIVE["mem"] is not None and LIVE["mem"] >= mem_pct and _game_may_restart(now_on):
        # Two brakes, both learned the hard way. The server must have been up for
        # a while: a fresh one climbs to its resting level in minutes, and without
        # this it would restart into the same reading over and over. And the
        # cooldown lives on disk, because it used to live in memory - where a panel
        # restart wiped it and the "one per hour" rule quietly stopped applying.
        started = _sh('date -d "$(systemctl show valheim -p ActiveEnterTimestamp '
                     '--value)" +%s 2>/dev/null || echo 0').stdout.strip()
        up_for = now - int(started) if started.isdigit() and int(started) else 0
        last = 0
        try:
            last = int(json.loads(VH_MEM_GUARD.read_text()).get("last", 0))
        except Exception:
            pass
        if up_for > 2 * 3600 and now - last > 3 * 3600:
            _save_json(VH_MEM_GUARD, {"last": now, "mem": LIVE["mem"]})
            _notify("maintenance", "Restart na pamięci",
                    f"Pamięć {LIVE['mem']}% przy progu {mem_pct}% "
                    f"({len((_mods_state().get('mods') or {}))} modów), nikt nie grał, "
                    f"serwer działał {round(up_for / 3600, 1)} h — restartuję.",
                    tags="repeat")
            _log("maintenance.restart_mem", mem=LIVE["mem"], limit=mem_pct,
                 up_hours=round(up_for / 3600, 1))
            _sh("systemctl restart valheim", timeout=180)

    # maintenance window - it has its own rule for players (defer and retry), so it asks
    # the gate only about the launch
    at = cfg["schedule"].get("restart_at")
    if at and _game_may_restart(now_on, need_empty=False):
        stamp = datetime.now().strftime("%Y-%m-%d") + " " + at
        # A ten-minute window, not the exact minute: a tick is 60 s plus however long the tick
        # takes, so some minutes are never sampled and that night's restart silently did not
        # happen. What was done is kept on disk, so a panel restart inside the window does
        # not restart the game a second time.
        if not WATCH["restart_done"]:
            WATCH["restart_done"] = _load_json(VH_RESTART_DONE, {}).get("stamp", "")
        try:
            h, mnt = map(int, at.split(":"))
            since = (datetime.now() - datetime.now().replace(hour=h, minute=mnt, second=0,
                                                            microsecond=0)).total_seconds()
        except ValueError:
            since = -1
        due = 0 <= since < 600 and WATCH["restart_done"] != stamp
        deferred_due = WATCH["deferred_until"] and now >= WATCH["deferred_until"]
        if due or deferred_due:
            empty = len(now_on) == 0
            if empty or not cfg["schedule"].get("only_when_empty", True):
                WATCH["restart_done"], WATCH["deferred_until"] = stamp, 0
                _save_json(VH_RESTART_DONE, {"stamp": stamp})
                _notify("maintenance", "Scheduled restart", "Restarting the server now.", tags="repeat")
                _log("maintenance.restart", players=len(now_on))
                _sh("systemctl restart valheim", timeout=180)
            else:
                mins = cfg["schedule"].get("defer_minutes", 30)
                WATCH["deferred_until"] = now + mins * 60
                if due:
                    WATCH["restart_done"] = stamp
                    _save_json(VH_RESTART_DONE, {"stamp": stamp})
                    _notify("maintenance", "Restart put off",
                            f"{len(now_on)} playing, trying again in {mins} min.", tags="hourglass")
                _log("maintenance.deferred", players=len(now_on), minutes=mins)


@app.on_event("startup")
async def _start_watcher():
    async def run():
        import asyncio
        while True:
            try:
                # in a thread: a game update inside it runs steamcmd for up to an hour, and on
                # the event loop that froze every route - the public page and the launcher too
                await asyncio.to_thread(_tick)
            except Exception as e:
                _log("watch.error", ok=False, error=f"{type(e).__name__}: {e}"[:200])
            await asyncio.sleep(60)

    # Separate loop on purpose: _tick reads the whole journal and shells out a dozen times,
    # which has no business running six times a minute. The live sampler touches two files
    # and sends three packets.
    async def live():
        import asyncio
        while True:
            try:
                await asyncio.to_thread(_live_tick)
            except Exception as e:
                _log("live.error", ok=False, error=f"{type(e).__name__}: {e}"[:200])
            await asyncio.sleep(10)
    try:
        _retire_default_password()
    except Exception as e:
        _log("panel.retire_error", ok=False, error=str(e)[:120])
    try:
        _panel_updated_notice()
    except Exception as e:
        _log("panel.notice_error", ok=False, error=str(e)[:120])
    # One-time migration for installs older than 2026-09-07: their update.sh still stops,
    # installs and starts the game on its own - a second updater next to the panel's, and
    # one that does not ask the gate. It becomes the stub setup.sh now writes.
    try:
        up = Path(f"{VH_DIR}/update.sh")
        if up.exists() and "app_update" in up.read_text():
            up.write_text(UPDATE_SH_STUB)
            _log("update.sh_retired")
    except Exception as e:
        _log("update.sh_retire_error", ok=False, error=str(e)[:120])
    import asyncio
    asyncio.create_task(run())
    asyncio.create_task(live())
