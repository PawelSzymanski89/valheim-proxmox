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

print("OK — log parser and login history")
