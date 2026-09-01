"""Runnable check for the launch-mode decision: release, or keep waiting.

This is the one piece of the panel that cannot be tried out safely on a live server —
getting it wrong wipes a world on the wrong day. Run it with the panel's interpreter:

    /opt/valheim/panel/.venv/bin/python test_launch.py
"""
import json
import pathlib
import tempfile
import time

import app

DAY = 86400
TARGET = 2000 * DAY          # some fixed moment; only the ordering matters
RELEASED = []

app.VH_LAUNCH = pathlib.Path(tempfile.mkdtemp()) / "launch.json"
app._log = lambda *a, **k: None
app._notify = lambda *a, **k: False
app._launch_release = lambda cfg, latest: RELEASED.append(latest)


def steam(latest, installed="100"):
    app._steam_latest_build = lambda: (installed, latest)


def arm(**over):
    cfg = dict(app.LAUNCH_DEFAULT)
    cfg.update(armed=True, released=False, baseline_build="100", checked=0,
               target=app.datetime.fromtimestamp(TARGET).isoformat())
    cfg.update(over)
    app._launch_save(cfg)


def tick(now):
    RELEASED.clear()
    app._LAUNCH_BUSY["at"] = 0
    app._launch_tick(now)
    time.sleep(0.3)                      # the release runs in a thread
    return list(RELEASED)


# --- not armed: the panel must not touch anything, whatever Steam says -----------------
steam("999")
arm(armed=False)
assert tick(TARGET + 60) == [], "disarmed panel released anyway"

# --- armed, timer still running: no release, and the baseline follows the live build ---
# A patch shipped in the days before a launch has to be absorbed into the baseline. If it
# were not, it would still read as "a newer build exists" the moment the timer expired.
steam("101")
arm()
assert tick(TARGET - 2 * DAY) == [], "released before the timer ran out"
assert app._launch_cfg()["baseline_build"] == "101", "baseline did not follow the pre-launch patch"

# ...and at the target, that same patch build must NOT count as the launch
assert tick(TARGET + 60) == [], "released on a pre-launch patch"

# --- armed, timer up, server build still not out: the case this exists for -------------
# On a Valheim launch day the game client updates before the dedicated server does. A panel
# that started the server on the clock would start it on a build that does not exist yet.
steam("100")
arm()
assert tick(TARGET + 3600) == [], "released while Steam still had the old server build"
assert "waiting for the server build" in app._launch_cfg()["note"], app._launch_cfg()["note"]

# --- armed, timer up, the server build lands: release ----------------------------------
steam("200")
arm()
assert tick(TARGET + 3600) == ["200"], "did not release once the new server build appeared"

# --- already released: it happens once, not every minute -------------------------------
steam("300")
arm(released=True, armed=False)
assert tick(TARGET + DAY) == [], "released twice"

# --- no target set: nothing to count down to, so nothing fires -------------------------
steam("999")
arm(target="")
assert tick(TARGET + DAY) == [], "released without a target date"

# --- the pacing: a check costs a steamcmd round trip, so it must not run every tick -----
steam("200")
arm()
assert tick(TARGET + 3600) == ["200"], "setup"
cfg = app._launch_cfg()
cfg.update(armed=True, released=False, baseline_build="100", checked=TARGET + 3600)
app._launch_save(cfg)
calls = []
app._steam_latest_build = lambda: (calls.append(1), ("100", "200"))[1]
app._launch_tick(TARGET + 3600 + 60)     # a minute later — too soon to look again
assert calls == [], "hit Steam again a minute after the last check"
app._launch_tick(TARGET + 3600 + app.LAUNCH_POLL + 1)
assert calls == [1], "did not look again after the poll interval"

# --- what is switched off must stay off across a reboot --------------------------------
# The bug this locks down: the first armed window ended eighteen minutes in, when the
# container rebooted and the enabled game service brought the old world back up behind a
# page still counting down to its replacement. "stop" is not enough; "disable --now" is.
cmds = []
app._sh = lambda c, **k: (cmds.append(c), type("R", (), {"stdout": "enabled", "returncode": 0})())[1]
app._game_service(False)
assert any("disable" in c and "valheim" in c for c in cmds), cmds
assert not any(c.strip() == "systemctl stop valheim" for c in cmds), "stop alone does not survive a reboot"

cmds.clear()
app._update_timer(False)
assert any("disable" in c and "valheim-update.timer" in c for c in cmds), cmds

# ...and comes back only as it was found: autostart the operator had switched off stays off
cmds.clear(); app._game_service(True, "disabled")
assert not any("enable" in c for c in cmds), cmds
cmds.clear(); app._game_service(True, "enabled")
assert any("enable" in c for c in cmds), cmds

# --- the target is an absolute moment, not a bare wall-clock time ----------------------
# The panel container runs on UTC. A naive "09:00" sent from a browser in Warsaw used to be
# read as 09:00 UTC, putting the launch two hours after the hour the operator typed.
utc = app._launch_target_ts({"target": "2026-09-09T07:00:00+00:00"})
cest = app._launch_target_ts({"target": "2026-09-09T09:00:00+02:00"})
assert utc == cest == 1788937200, (utc, cest)
assert app._launch_target_ts({"target": ""}) == 0
assert app._launch_target_ts({"target": "nonsense"}) == 0

# --- the placeholder is up exactly while armed and unreleased --------------------------
assert app._launch_waiting({"armed": True, "released": False}) is True
assert app._launch_waiting({"armed": True, "released": True}) is False
assert app._launch_waiting({"armed": False, "released": False}) is False

print("OK — releases on the server build, survives a reboot, honours the timezone")
