#!/bin/sh
# Runs before systemd. Whatever the operator put in the container environment is written
# where valheim-firstboot.service can read it - PID 1 does not hand its environment to
# the units it starts, and the compose file is the only place a user sets these.
# Root-only: it carries the panel and game passwords, and the game user - which runs every
# mod - could read a 644 file here. firstboot deletes it once it has been read.
umask 077
mkdir -p /run/valheim
env | grep -E '^(SERVER_NAME|WORLD_NAME|SERVER_PASS|GAME_PORT|PANEL_USER|PANEL_PASS|PANEL_PORT|NTFY_SERVER|NTFY_TOPIC)=' \
  > /run/valheim/env || true
exec /sbin/init
