#!/bin/sh
# Runnable check for offsite-sink.sh: sh scripts/test-offsite-sink.sh
set -eu
S=$(cd "$(dirname "$0")" && pwd)/offsite-sink.sh
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
run() { SSH_ORIGINAL_COMMAND="$1" sh "$S" "$D/store"; }
fail() { echo "FAIL: $*"; exit 1; }
echo data | run "put world-20260924-120000.tar.gz" | grep -q "stored world-20260924-120000.tar.gz 5" || fail put
for n in 1 2 3; do echo x | run "put world-2026092$n-000000.tar.gz" >/dev/null; done
[ "$(run list | wc -l | tr -d ' ')" = 4 ] || fail list
run "prune 2" >/dev/null; [ "$(run list | wc -l | tr -d ' ')" = 2 ] || fail prune
run list | grep -q "world-20260924-120000" || fail "prune removed the newest"
echo x | run "put ../../etc/passwd" 2>/dev/null && fail "accepted a path"
echo x | run "put world-2026-evil.tar.gz" 2>/dev/null && fail "accepted a loose name"
run "prune 0" 2>/dev/null && fail "accepted prune 0"
run "rm -rf /" 2>/dev/null && fail "ran an arbitrary command"
run "" 2>/dev/null && fail "an empty command did something"
run test | grep -qE "^ok [0-9]+" || fail test
[ ! -e "$D/etc" ] && [ -z "$(ls -A "$D/store" | grep -v '^world-')" ] || fail "something was written outside the rules"
echo "OK - offsite sink: put, list, prune, test, and every other command refused"
