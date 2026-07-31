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

conns, hist, count, count_ts, version = app._scan(LOG)
assert version == "l-0.221.12", version
assert count == 2, count
assert [c["id"] for c in conns] == ["76561198000000002"], conns
assert conns[0]["name"] == "Hilda", conns
assert conns[0]["since"] == app._ts("2026-07-31T10:02:00+0000"), conns

# a server restart drops everyone, even without a "Closing socket" line
restarted, *_ = app._scan(LOG + [L.format("11:00:00", "11:00:00", "Valheim version: l-0.221.12")])
assert restarted == [], restarted

app.VH_STORE = pathlib.Path(tempfile.mkdtemp()) / "players.json"
by_id = {p["id"]: p for p in app._history(hist)}
assert by_id["76561198000000001"]["name"] == "Skjor", by_id
assert by_id["76561198000000002"]["name"] == "Hilda", by_id
assert by_id["76561198000000001"]["sessions"] == 1, by_id

# The panel re-reads the whole journal every poll, so replaying the same events must
# not inflate the join counts.
again = {p["id"]: p["sessions"] for p in app._history(hist)}
assert again == {i: 1 for i in by_id}, again

print("OK — log parser and login history")
