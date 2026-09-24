#!/bin/sh
# Receives world backups from a valheim-proxmox panel over SSH - and does nothing else.
#
# Install on the machine that keeps the copies (a NAS, any Linux box with SSH):
#   1. copy this file somewhere, e.g. ~/bin/valheim-sink, and chmod 755 it
#   2. put the panel's public key (Backups tab -> Offsite copy) in ~/.ssh/authorized_keys as:
#      command="$HOME/bin/valheim-sink /path/to/valheim-backups",no-port-forwarding,no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... valheim-panel
#
# The forced command means that key can only run this script, whatever it asks for: store a
# backup under a strict name, list the backups, drop the oldest. No shell, no other files.
set -eu
DIR=${1:?usage: valheim-sink /path/to/backups}
mkdir -p "$DIR"
set -- ${SSH_ORIGINAL_COMMAND:-}
case "${1:-}" in
  put)
    name=${2:-}
    echo "$name" | grep -qE '^world-[0-9]{8}-[0-9]{6}\.tar\.gz$' || { echo "refused: odd name" >&2; exit 2; }
    cat > "$DIR/.$name.part"
    mv -f "$DIR/.$name.part" "$DIR/$name"
    echo "stored $name $(wc -c < "$DIR/$name" | tr -d ' ')"
    ;;
  list)
    for f in "$DIR"/world-*.tar.gz; do [ -f "$f" ] && echo "$(basename "$f") $(wc -c < "$f" | tr -d ' ')"; done
    exit 0
    ;;
  prune)
    keep=${2:-}
    echo "$keep" | grep -qE '^[0-9]{1,4}$' && [ "$keep" -ge 1 ] || { echo "refused: keep must be 1-9999" >&2; exit 2; }
    ls "$DIR" | grep -E '^world-[0-9]{8}-[0-9]{6}\.tar\.gz$' | sort -r | tail -n +$((keep + 1)) | while read -r f; do rm -f "$DIR/$f"; echo "removed $f"; done
    ;;
  test)
    echo "ok $(df -k "$DIR" | awk 'NR==2{print $4*1024}')"
    ;;
  *)
    echo "refused: this key only stores valheim backups (put NAME | list | prune N | test)" >&2
    exit 2
    ;;
esac
