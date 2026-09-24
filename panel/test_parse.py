"""Runnable check for the log parser and the history store.

This is the one path a live server cannot exercise on demand — it needs real players
joining and leaving. Run it with the panel's own interpreter:

    /opt/valheim/panel/.venv/bin/python test_parse.py
"""
import pathlib
import tempfile

import app

L = "2026-07-31T{}+0000 host start.sh[1]: 07/31/2026 {}: {}"
LOG = [
    L.format("10:00:00", "10:00:00", "Valheim version: l-0.221.12 (network version 36)"),
    L.format("10:01:00", "10:01:00", "Got connection SteamID 76561198000000001"),
    L.format("10:01:05", "10:01:05", "Got character ZDOID from Skjor : 123:1"),
    L.format("10:02:00", "10:02:00", "Got connection SteamID 76561198000000002"),
    L.format("10:02:07", "10:02:07", "Got character ZDOID from Hilda : 456:1"),
    L.format("10:05:00", "10:05:00", " Connections 2 ZDOS:81  sent:0 recv:0"),
    L.format("10:09:00", "10:09:00", "Closing socket 76561198000000001"),
]

conns, hist, count, count_ts, version, code = app._scan(LOG)
assert version == "l-0.221.12", version
assert count == 2, count
assert [c["id"] for c in conns] == ["76561198000000002"], conns
assert conns[0]["name"] == "Hilda", conns
assert conns[0]["since"] == app._ts("2026-07-31T10:02:00+0000"), conns

assert code is None, code  # no crossplay line in this log

with_code = LOG + [L.format("10:10:00", "10:10:00", 'Session "X" registered with join code 458673')]
assert app._scan(with_code)[5] == "458673", app._scan(with_code)[5]
# a restart starts a new session, so the old code must not linger
assert app._scan(with_code + [L.format("11:00:00", "11:00:00", "Valheim version: l-0.221.12")])[5] is None

# a server restart drops everyone, even without a "Closing socket" line
restarted, *_ = app._scan(LOG + [L.format("11:00:00", "11:00:00", "Valheim version: l-0.221.12")])
assert restarted == [], restarted

# Straight from a production log: the server prints "Got connection" AND "Got handshake"
# for one peer, and a reconnect prints the pair again. This read as three players online.
DUP = [
    L.format("10:00:00", "10:00:00", "Valheim version: l-0.221.12 (network version 36)"),
    L.format("11:40:52", "11:40:52", "Got connection SteamID 76561197992106139"),
    L.format("11:40:52", "11:40:52", "Got handshake from client 76561197992106139"),
    L.format("11:41:00", "11:41:00", "Closing socket 76561197992106139"),
    L.format("11:41:21", "11:41:21", "Got connection SteamID 76561197992106139"),
    L.format("11:41:21", "11:41:21", "Got handshake from client 76561197992106139"),
    L.format("11:41:36", "11:41:36", "Got character ZDOID from Torvald : 487959370:3"),
]
dup_conns, dup_hist, *_ = app._scan(DUP)
assert len(dup_conns) == 1, dup_conns
assert dup_conns[0]["name"] == "Torvald", dup_conns
assert sum(1 for _t, kind, _c in dup_hist if kind == "join") == 2, dup_hist  # two real sessions

# Deaths: the same ZDOID line the name comes from, with the character id zeroed. The name
# line must keep working, and a death by somebody who is not connected is dropped, not
# guessed onto whoever happens to be online.
DEATHS = LOG[:5] + [
    L.format("10:06:00", "10:06:00", "Got character ZDOID from Skjor : 0:0"),
    L.format("10:07:00", "10:07:00", "Got character ZDOID from Ghost : 0:0"),
    L.format("10:08:00", "10:08:00", "Got character ZDOID from Skjor : 0:0"),
]
d_conns, d_hist, *_ = app._scan(DEATHS)
assert [c["name"] for c in d_conns] == ["Skjor", "Hilda"], d_conns
assert [c["name"] for _t, k, c in d_hist if k == "death"] == ["Skjor", "Skjor"], d_hist

app.VH_STORE = pathlib.Path(tempfile.mkdtemp()) / "players.json"
by_death = {p["id"]: p for p in app._history(d_hist)}
assert by_death["76561198000000001"]["deaths"] == 2, by_death

app.VH_STORE = pathlib.Path(tempfile.mkdtemp()) / "players.json"
by_id = {p["id"]: p for p in app._history(hist)}
assert by_id["76561198000000001"]["name"] == "Skjor", by_id
assert by_id["76561198000000002"]["name"] == "Hilda", by_id
assert by_id["76561198000000001"]["sessions"] == 1, by_id

# The panel re-reads the whole journal every poll, so replaying the same events must
# not inflate the join counts.
again = {p["id"]: p["sessions"] for p in app._history(hist)}
assert again == {i: 1 for i in by_id}, again

# Skjor joined 10:01 and left 10:09 — eight minutes, recorded once
skjor = by_id["76561198000000001"]
assert skjor["total"] == 8 * 60, skjor
assert len(skjor["log"]) == 1 and skjor["log"][0]["seconds"] == 8 * 60, skjor
# Hilda never left in this log, so she has no closed session yet
assert by_id["76561198000000002"]["total"] == 0, by_id["76561198000000002"]

# The crash watcher, without crashing a live server: systemd's own counter is what it
# reads, so a canned `systemctl show` is a faithful stand-in.
class _Fake:
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


def _systemctl(n, result="signal", status="9"):
    return lambda cmd, **kw: _Fake(f"NRestarts={n}\nResult={result}\n"
                                   f"ExecMainStatus={status}\nExecMainCode=2\n")


app.VH_HEALTH = pathlib.Path(tempfile.mkdtemp()) / "health.json"
app._notify = lambda *a, **k: None
app._log = lambda *a, **k: None
app.WATCH["restarts"] = None

app._sh = _systemctl(3)
assert app._crash_watch(1000) is None, "first pass only takes a watermark"
app._sh = _systemctl(3)
assert app._crash_watch(1001) is None, "no new restarts, no crash"
app._sh = _systemctl(4, "oom-kill", "0")
c = app._crash_watch(1002)
assert c and c["result"] == "oom-kill", c
assert [x["result"] for x in app._health_state()["crashes"]] == ["oom-kill"], app._health_state()

# world files: a 1.0 folder loads its highest *complete* save, an old pair still counts
import struct
w = pathlib.Path(tempfile.mkdtemp())
app.VH_WORLDS = str(w)
cs = lambda s: bytes([len(s)]) + s.encode()
fwl = lambda n: struct.pack("<ii", 0, 41) + cs(n) + cs("SEEDNAME") + struct.pack("<i", 7)
(w / "New").mkdir()
for n in (3, 4):
    (w / "New" / f"_main.{n}.fwl2").write_bytes(fwl("New"))
    (w / "New" / f"_main.{n}.db2").write_bytes(struct.pack("<id", 41, 1800.0 * n))
(w / "New" / "_main.3.ok").write_bytes(b"x")          # save 4 has no .ok: an interrupted write
(w / "New_backup_auto-20260101-000000").mkdir()
(w / "Old.fwl").write_bytes(fwl("Old"))
(w / "Old.db").write_bytes(struct.pack("<id", 37, 0.0))
assert app._world_files("New")[1].name == "_main.3.db2", app._world_files("New")
assert sorted((x["name"], x["format"]) for x in app._worlds()) == [("New", "1.0"), ("Old", "legacy")], app._worlds()
card = app._world_card("New")
assert card["fwl"]["seed_name"] == "SEEDNAME" and card["db"]["day"] == 4, card
assert app._world_files("Nope") == (None, None)
(w / "Fresh").mkdir()                                   # generated, never saved yet
(w / "Fresh" / "_main.0.fwl2").write_bytes(fwl("Fresh"))
assert app._world_files("Fresh")[0].name == "_main.0.fwl2"
assert "Fresh" in [x["name"] for x in app._worlds()]
(w / "Old").mkdir()                                     # the 1.0 server converting Old.db
assert app._world_files("Old")[0].name == "Old.fwl"
assert ("Old", "legacy") in [(x["name"], x["format"]) for x in app._worlds()]

# a close with an id nobody has: guessed onto someone only when there is one to guess
two = LOG[:5] + [L.format("10:06:00", "10:06:00", "Closing socket 999")]
assert len(app._scan(two)[0]) == 2, "dropped a player on an unmatched close"
one = LOG[:3] + [L.format("10:06:00", "10:06:00", "Closing socket 999")]
assert len(app._scan(one)[0]) == 0

# a players.json that exists but does not parse is left alone, not replaced by the journal
app.VH_STORE = pathlib.Path(tempfile.mkdtemp()) / "players.json"
app.VH_STORE.write_text('{"players": {"x": ')
assert app._history(hist) == []
assert app.VH_STORE.read_text() == '{"players": {"x": ', "overwrote an unreadable history"

# CSRF: a browser request from another site is refused, a script and the page itself are not
class _Req:
    def __init__(self, method="POST", **h):
        self.method, self.headers = method, {k.replace("_", "-"): v for k, v in h.items()}
assert app._cross_site(_Req(sec_fetch_site="cross-site"))
assert app._cross_site(_Req(sec_fetch_site="same-site"))
assert not app._cross_site(_Req(sec_fetch_site="same-origin"))
assert not app._cross_site(_Req())                                   # curl, the launcher
assert not app._cross_site(_Req("GET", sec_fetch_site="cross-site"))
assert app._cross_site(_Req(origin="https://evil.example", host="panel.example"))
assert not app._cross_site(_Req(origin="https://panel.example", host="panel.example"))

# a value with a line break never reaches an env file
try:
    app._no_newlines({"NTFY_TOKEN": "x\nTRUSTED_PROXIES=1.2.3.4"})
    raise AssertionError("let a newline into an env file")
except app.HTTPException:
    pass

# character names that may go into a console command, and the ones that may not
for ok in ("Michał", "Skjor", "Eir the Red", "O'Hara", "Jan-Olof"):
    assert app.VH_CHAR_NAME_RE.fullmatch(ok), ok
for bad in ("x;kick Bob", "a\nb", "", " lead", "x" * 30, "<b>", "a|b"):
    assert not app.VH_CHAR_NAME_RE.fullmatch(bad), bad

# config lines that carry a secret, and the ones that only look like it
for sec in (b"password = x", b"Admin Password = x", b"RconPassword=x", b"Discord Webhook = h", b"Api Key = k"):
    assert app._SECRET_LINE.search(sec), sec
for plain in (b"Passive Mobs = true", b"Author = me", b"password = ", b"# Password: shown\nSpeed = 1"):
    assert not app._SECRET_LINE.search(plain), plain

# the manifest signature verifies with the key the launcher is given, and nothing else does
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
app.VH_MANIFEST_KEY = pathlib.Path(tempfile.mkdtemp()) / "manifest.key"
body = b'{"files":[]}'
sig = app._manifest_key().sign(body)
pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(app._manifest_pub()))
pub.verify(sig, body)
try:
    pub.verify(sig, body + b" ")
    raise AssertionError("a changed manifest verified")
except Exception as e:
    assert type(e).__name__ == "InvalidSignature", e
assert oct(app.VH_MANIFEST_KEY.stat().st_mode & 0o777) == "0o600"

# the default password is replaced once, and the marker explains a later try of it
d = pathlib.Path(tempfile.mkdtemp())
app.VH_PANEL_ENV, app.VH_PASS_RETIRED, app.VH_DIR = str(d / "panel.env"), d / "retired", str(d)
(d / "panel.env").write_text("PANEL_USER='admin'\nPANEL_PASS='valheim123'\nNTFY_TOPIC='t'\n")
app._write = lambda path, text, **k: pathlib.Path(path).write_text(text)
app._retire_default_password()
env = app._env_file(app.VH_PANEL_ENV)
assert env["PANEL_PASS"] != "valheim123" and len(env["PANEL_PASS"]) >= 20 and env["NTFY_TOPIC"] == "t", env
assert app.VH_PASS_RETIRED.exists()
before = env["PANEL_PASS"]; app._retire_default_password()
assert app._env_file(app.VH_PANEL_ENV)["PANEL_PASS"] == before, "rotated a password that was not the default"

# release signatures: a file signed with the project key verifies, anything else does not
import os, subprocess as sp
S = os.environ.get("SIGNED_FIXTURE")        # set by the release checklist: FILE with FILE.sig next to it
if S:
    data, sig = open(S, "rb").read(), open(S + ".sig", "rb").read()
    assert app._release_signed(data, sig), "a correctly signed release did not verify"
    assert not app._release_signed(data + b"x", sig), "a changed release verified"
assert not app._release_signed(b"anything", b"bm90IGEgc2lnbmF0dXJl"), "garbage verified"
assert not app._release_signed(b"anything", b"%%%"), "a broken .sig verified"

print("OK — log parser, login history, the crash watcher and both world formats")
