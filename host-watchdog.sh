#!/usr/bin/env bash
# host-watchdog.sh - outer safety net for the EanHost control plane.
# Cron runs this every minute. It re-runs the idempotent eanhost-up.sh, so
# anything that died (web panel, eanwatch loop, adb server) is back within a
# minute, with no root and no systemd dependency. eanwatch keeps the nodes
# alive; this keeps eanwatch alive.
set -u
cd "$(dirname "$0")"
LOG=/tmp/eanhost-watchdog.log
LOCK=/tmp/eanhost-watchdog.lock
BEAT=/tmp/eanhost-watchdog.last
MAXLOG_KB=512   # /tmp is usually tmpfs, so these logs eat RAM

# Never overlap: a hung adb call must not stack watchdogs, and the boot
# @reboot run may collide with the first cron tick. flock -o holds the lock in
# the flock process and closes the fd in the child, so the panel and the adb
# server this script starts cannot inherit the lock and wedge it forever.
if [ "${EANHOST_WD_LOCK:-0}" != 1 ]; then
  flock -n -o "$LOCK" env EANHOST_WD_LOCK=1 bash "$PWD/host-watchdog.sh" "$@" || exit 0
  exit 0
fi

note() { echo "$(date +%s): $*" >>"$LOG"; }

# Heartbeat: proof of life for the cron tick itself, and the only way to tell
# "cron is not firing this" from "cron fired and everything was healthy".
date +%s >"$BEAT" 2>/dev/null || true

# 1. adb server. After a host reboot it is gone. The adb client respawns it on
#    first use, but do it explicitly so the panel and eanwatch never race to
#    spawn it and so a dead server is diagnosed here rather than in a request.
#    start-server is a cheap no-op when the server is already up. Never use
#    adb kill-server: that would need the devices re-authorized.
if ! adb start-server >/dev/null 2>&1; then
  note "adb start-server failed, retrying once"
  sleep 2
  adb start-server >>"$LOG" 2>&1 || note "adb server still not up"
fi

# 2. The control plane itself. eanhost-up.sh only starts what is missing, so
#    this is a no-op on a healthy box and a full revive otherwise.
if out=$(bash "$PWD/eanhost-up.sh" 2>&1); then
  # Stay quiet only when nothing was started. Matching on the whole output
  # would hide a partial revive, because "already running" for the panel
  # appears in the same lines as "started" for eanwatch.
  case "$out" in
    *started*) note "revived: $(printf '%s' "$out" | tr '\n' ';')" ;;
  esac
else
  note "eanhost-up.sh failed: $(printf '%s' "$out" | tr '\n' ';')"
fi

# 3. Keep the RAM-backed logs bounded so a runaway log cannot OOM the host.
cap() { # file
  local f=$1 kb
  [ -f "$f" ] || return 0
  kb=$(( $(wc -c <"$f") / 1024 ))
  if [ "$kb" -gt "$MAXLOG_KB" ]; then
    if tail -c $(( MAXLOG_KB * 512 )) "$f" > "$f.trim" 2>/dev/null; then
      mv "$f.trim" "$f"
      note "trimmed $f (${kb}KiB > ${MAXLOG_KB}KiB)"
    else
      rm -f "$f.trim"
    fi
  fi
}
for f in /tmp/eanhost-*.log; do cap "$f"; done
cap "$LOG"
