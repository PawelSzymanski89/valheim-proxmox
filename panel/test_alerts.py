"""Runnable check for the "only while someone is playing" alerts. Everything lives in a
temporary VH_DIR, so nothing on the machine is touched:

    /opt/valheim/panel/.venv/bin/python test_alerts.py
"""
import json
import os
import tempfile
import types

os.environ["VH_DIR"] = tempfile.mkdtemp()
os.environ["VH_ALERTS"] = os.environ["VH_DIR"] + "/alerts.json"
open(os.environ["VH_DIR"] + "/panel.env", "w").write("NTFY_TOPIC=test\n")

import app  # noqa: E402

SENT, PINGS, PLAYERS = [], [], []
app._log = lambda *a, **k: None
app._notify = lambda ev, *a, **k: SENT.append(ev) or True
app._save_panel_env = lambda env: None           # writes root:root, needs root
app._vh_log = lambda: [f"2026-01-01T00:00:00+0000 Got connection SteamID {p}" for p in PLAYERS]


def save(**over):
    cfg = json.loads(json.dumps(app.ALERTS_DEFAULT))
    cfg.update(over)
    app.VH_ALERTS.write_text(json.dumps(cfg))


# --- U5: a null / list playing_only, in the file or in the body, is "not set" -------------
app.VH_ALERTS.write_text(json.dumps({"playing_only": None}))
for po in (None, [], [1], {"load_high": True}):
    assert app.alerts_set({"playing_only": po}) == {"ok": True}, po
assert app._alerts_cfg()["playing_only"]["load_high"] is True

# --- U3 + U4: the load alert, held while nobody plays ---------------------------------------
save(playing_only={"load_high": True, "link_bad": False, "disk_low": False})
app.LIVE.update(cpu=99, mem=10)
app.WATCH["online"] = {"stale": "x"}              # the minute-old view says someone is on


def load(t):
    SENT.clear()
    app.LIVE["t"] = t
    app._load_watch()
    return list(SENT)


assert load(1000) == [] and load(5000) == [], "alerted while the fresh log shows nobody"
PLAYERS.append("p1")
app.WATCH["online"] = {}                          # ...and now it lags the other way
assert load(5010) == [], "an hour held counted as an hour hot"
assert load(5080) == ["load_high"], "no alert after a minute hot with a player on"

# --- U2: ping_when_empty must not starve a playing-only link alert ---------------------------
app.status = lambda: {"active": True, "online": [{"id": "p1", "name": "A"}],
                      "settings": {"world": "w", "name": "n"}, "history": [], "disk": {}}
app._sh = lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0)
app._ping = lambda *a, **k: PINGS.append(1) or {"avg": 400, "loss": 0}
app._speedtest = lambda: None
for fn in ("_launch_tick", "_mods_restore_tick", "_crash_watch", "_offsite_tick",
           "_panel_update_tick", "_game_update_tick"):
    setattr(app, fn, lambda *a, **k: None)
app.WATCH.update(active=True, online={"p1": "A"}, build_checked=10**12)
save(playing_only={"load_high": False, "link_bad": True, "disk_low": False},
     schedule=dict(app.ALERTS_DEFAULT["schedule"], ping_when_empty=True))
SENT.clear()
app._tick()
assert PINGS and "link_bad" in SENT, "link alert can never fire with ping_when_empty"

print("OK — playing-only: null settings, held load counter, fresh players, ping not starved")
