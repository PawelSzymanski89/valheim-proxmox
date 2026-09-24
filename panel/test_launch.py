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

# --- bringing the admin tooling back, without ever leaving a dead server ---------------
# Release dates are useless as a "is it ready for the new game" gate: AviiNL-rcon has not been
# touched since 2024, and a mod that still works never gets a rebuild to prove it. So the panel
# tries and verifies, and the dates only stop it repeating an attempt that already failed.
INSTALLED, CLEARED = [], []
app._sh = lambda c, **k: type("R", (), {"stdout": "active", "returncode": 0})()
app.mods_install = lambda p: (INSTALLED.append(p), {"installed": [], "failed": []})[1]
app.mods_clear = lambda c: CLEARED.append(True)
app._ts_latest = lambda full: {"AviiNL-rcon": "1.0.5",
                               "JereKuusela-Rcon_Commands": "1.2.0",
                               "JereKuusela-Server_devcommands": "1.109.0"}[full]
VERS = {"AviiNL-rcon": "1.0.5", "JereKuusela-Rcon_Commands": "1.2.0",
        "JereKuusela-Server_devcommands": "1.109.0"}


def released(**over):
    cfg = dict(app.LAUNCH_DEFAULT)
    cfg.update(armed=False, released=True, released_at=TARGET, restore_mods=True,
               mods_restored=False, mods_tried=None, mods_checked=0)
    cfg.update(over)
    app._launch_save(cfg)


def mtick(now, healthy=True):
    INSTALLED.clear(); CLEARED.clear()
    app._LAUNCH_BUSY["at"] = 0
    app._server_healthy = lambda wait=0: healthy
    app._mods_restore_tick(now)
    time.sleep(0.3)
    return len(INSTALLED), len(CLEARED)

# nothing before the launch has even happened
released(released=False)
assert mtick(TARGET + 9999) == (0, 0), "restored mods before the launch"

# nothing while the new build is still settling
released()
assert mtick(TARGET + 10) == (0, 0), "did not let the new build settle first"

# switched off by the operator: stays off
released(restore_mods=False)
assert mtick(TARGET + 99999) == (0, 0), "restored mods although the option was off"

# the happy path: install, verify, mark done
released()
assert mtick(TARGET + app.MODS_SETTLE + 60, healthy=True) == (1, 0)
c = app._launch_cfg()
assert c["mods_restored"] is True, c
assert "installed" in c["mods_note"], c["mods_note"]

# ...and once done it never runs again
assert mtick(TARGET + 999999) == (0, 0), "reinstalled mods that were already restored"

# the unhappy path: a mod built for the old game takes the server down -> roll back to vanilla
released()
assert mtick(TARGET + app.MODS_SETTLE + 60, healthy=False) == (1, 1), "did not roll back"
c = app._launch_cfg()
assert c["mods_restored"] is False, "called a failed restore a success"
assert c["mods_tried"] == VERS, c["mods_tried"]
assert "rolled back" in c["mods_note"], c["mods_note"]

# ...and the identical versions are not tried again an hour later
c["mods_checked"] = 0; app._launch_save(c)
assert mtick(TARGET + app.MODS_SETTLE + 7200, healthy=True) == (0, 0), "retried the same broken versions"

# ...but a rebuild is picked up
c = app._launch_cfg(); c["mods_checked"] = 0; app._launch_save(c)
app._ts_latest = lambda full: "9.9.9" if full == "AviiNL-rcon" else VERS[full]
assert mtick(TARGET + app.MODS_SETTLE + 7200, healthy=True)[0] == 1, "ignored a rebuilt mod"
assert app._launch_cfg()["mods_restored"] is True

# Thunderstore down: _ts_latest answers None - nothing is installed, and nothing rolled back
released()
app._ts_latest = lambda full: None
assert mtick(TARGET + app.MODS_SETTLE + 60) == (0, 0), "installed None during an outage"
assert "unreachable" in app._launch_cfg()["mods_note"]
app._ts_latest = lambda full: VERS[full]

# somebody playing: installing restarts the server, so it waits
released()
app.WATCH["online"] = {"p1": "Eir"}
assert mtick(TARGET + app.MODS_SETTLE + 60) == (0, 0), "restarted the server under a player"
app.WATCH["online"] = {}

# the launch kept the players' modpack: a rollback takes only the three admin mods out
REMOVED = []
app.mods_remove = lambda full, restart=True: REMOVED.append(full)
app._mods_state = lambda: {"mods": {"Azumatt-AzuAutoStore": {}, "AviiNL-rcon": {}}}
released()
assert mtick(TARGET + app.MODS_SETTLE + 60, healthy=False) == (1, 0), "wiped the modpack on rollback"
assert REMOVED == app.LAUNCH_MODS, REMOVED

# --- the placeholder is up exactly while armed and unreleased --------------------------
assert app._launch_waiting({"armed": True, "released": False}) is True
assert app._launch_waiting({"armed": True, "released": True}) is False
assert app._launch_waiting({"armed": False, "released": False}) is False

# --- stood down: the automatic restarts (05:00 window, memory guard) must ask this first ---
# 2026-09-06 05:00 the window restarted a game the launch had switched off; the old world
# ran for a day behind the countdown page.
assert app._launch_stood_down({"armed": True, "released": False, "stop_server": True}) is True
assert app._launch_stood_down({"armed": True, "released": False}) is True, "stop_server defaults to on"
assert app._launch_stood_down({"armed": True, "released": False, "stop_server": False}) is False
assert app._launch_stood_down({"armed": True, "released": True, "stop_server": True}) is False, "released = ordinary day again"
assert app._launch_stood_down({"armed": False, "released": False, "stop_server": True}) is False

# --- the gate every automatic restart asks (window, memory guard, game update) ---------
arm(stop_server=True)
assert app._game_may_restart({}) is False, "a stood-down launch must block every restart"
assert app._game_may_restart({}, need_empty=False) is False
arm(stop_server=False)
assert app._game_may_restart({}) is True
assert app._game_may_restart({"p1": "Eir"}) is False, "someone playing blocks a restart"
assert app._game_may_restart({"p1": "Eir"}, need_empty=False) is True, "the window has its own player rule"
app._LAUNCH_BUSY["at"] = 1
assert app._game_may_restart({}) is False, "a release in progress blocks a restart"
app._LAUNCH_BUSY["at"] = 0

# the panel does not update itself while a launch is holding the game in place
STARTED = []
RC = {"v": 0}
app._panel_update_start = lambda why: (STARTED.append(why), type("R", (), {"returncode": RC["v"]})())[1]
app._panel_newer = lambda: {"tag": "v9.9.9"}
app._panel_can_update = lambda: True
app.VH_AUTO_UPDATE_OFF = pathlib.Path(tempfile.mkdtemp()) / "off"
app.VH_DIR = tempfile.mkdtemp()
cfg = dict(app.LAUNCH_DEFAULT); cfg.update(armed=True, released=False); app._launch_save(cfg)
app._LAUNCH_BUSY["at"] = 0
app._panel_update_tick({})
assert STARTED == [], "self-updated during an armed launch"
cfg.update(armed=False); app._launch_save(cfg)
app._panel_update_tick({"p1": "Eir"})
assert STARTED == [], "self-updated with someone playing"
app._panel_update_tick({})
assert STARTED == ["auto"], STARTED
app._panel_update_tick({})
assert STARTED == ["auto"], "tried the same release twice"
# a start that failed (GitHub down, systemd-run refused) is retried - but not every minute
app.VH_DIR = tempfile.mkdtemp(); app.WATCH["panel_update_at"] = 0; STARTED.clear(); RC["v"] = 1
app._panel_update_tick({}); app._panel_update_tick({})
assert STARTED == ["auto"], "retried a failed start within the hour"
app.WATCH["panel_update_at"] = 0; RC["v"] = 0
app._panel_update_tick({})
assert STARTED == ["auto", "auto"], "never retried a failed start"

# releases come in waves: "stable" waits ROLLOUT_HOURS, "early" does not, "[hold]" stops both
def wave(published, channel, hold=False):
    STARTED.clear(); RC["v"] = 0
    d = tempfile.mkdtemp()
    app.VH_DIR, app.VH_CHANNEL = d, pathlib.Path(d) / "update-channel"
    if channel == "early":
        app.VH_CHANNEL.write_text("early")
    app.WATCH["panel_update_at"] = 0
    app._panel_newer = lambda: {"tag": "v9.9.9", "published": published, "hold": hold}
    app._panel_update_tick({})
    return bool(STARTED)

now = time.time()
assert not wave(now - 3600, "stable"), "stable took a release an hour old"
assert wave(now - 49 * 3600, "stable"), "stable never took a release two days old"
assert wave(now - 60, "early"), "early did not take a fresh release"
assert not wave(now - 99 * 3600, "early", hold=True), "a held release installed itself"
assert not wave(now - 99 * 3600, "stable", hold=True), "a held release installed itself"
assert wave(0, "stable"), "a release with no date never installed"     # unknown date: no wave to wait for
assert app._rollout_at({"published": 1000, "hold": False}) == 1000 + app.ROLLOUT_HOURS * 3600
assert app._iso_ts("2026-09-24T07:42:22Z") == 1790235742
# the marker is a line of its own - notes that merely mention it (as v1.25.0's did) hold nothing
assert app._held("fixes\n[hold]\nmore") and app._held("  [HOLD]  ")
assert not app._held("a line reading `[hold]` stops a release") and not app._held("")

# a try that never reached GitHub is tried again (hourly); any other outcome is final
def retry(result_line):
    STARTED.clear(); RC["v"] = 0
    d = tempfile.mkdtemp()
    app.VH_DIR, app.VH_CHANNEL = d, pathlib.Path(d) / "update-channel"
    app.VH_CHANNEL.write_text("early")
    app.VH_UPDATE_RESULT = pathlib.Path(d) / "update-result"
    pathlib.Path(d, "auto-update.tried").write_text("v9.9.9")
    if result_line:
        app.VH_UPDATE_RESULT.write_text(result_line)
    app.WATCH["panel_update_at"] = 0
    app._panel_newer = lambda: {"tag": "v9.9.9", "published": 0, "hold": False}
    app._panel_update_tick({})
    return bool(STARTED)

assert retry("v9.9.9 network 1"), "a network failure was not retried"
assert retry("unknown network 1"), "a failed release lookup was not retried"
assert not retry("v9.9.9 rollback 1"), "a rolled-back release was retried"
assert not retry("v9.9.9 refused 1"), "a refused release was retried"
assert not retry("v9.9.8 network 1"), "an older release's network failure retried this one"
assert not retry(""), "a tried release with no result was retried"

# a game update in progress: nothing else restarts the game, and a second one does not start
import threading
cfg = dict(app.LAUNCH_DEFAULT); cfg.update(armed=False); app._launch_save(cfg)
app._LAUNCH_BUSY["at"] = 0
app._steam_latest_build = lambda: ("100", "200")
app._update_timer_on = lambda: True
app._sh = lambda c, **k: type("R", (), {"stdout": "", "returncode": 0})()
gate = threading.Event()
app._steam_install = lambda: (gate.wait(5), type("R", (), {"returncode": 0})())[1]
t = threading.Thread(target=app._game_update_tick, args=({},)); t.start(); time.sleep(0.2)
assert app._game_may_restart({}) is False, "allowed a restart in the middle of a game update"
assert app._game_update_tick({}, manual=True).get("held"), "started a second steamcmd"
gate.set(); t.join()
assert app._game_may_restart({}) is True, "the update never let go"

print("OK — release, reboot, timezone, and a mod restore that rolls itself back")
