#!/usr/bin/env bash
# eanhost-up.sh - start the whole control plane. Idempotent; safe after
# every host reboot. Starts exactly two things: the web panel and one eanwatch
# loop (which keeps every node's supervisor + termux agent alive).
cd "$(dirname "$0")"
LOG=/tmp/eanhost-up.log

# Serialize with the watchdog and the boot @reboot run, which can collide.
# flock -o holds the lock in the flock process and closes the fd in the child:
# a plain "flock -n 8" would leak the lock fd into every process we spawn (the
# panel, the adb server, sleep), and the lock would then stay held for the life
# of the panel, so no later run could ever restart the control plane.
if [ "${EANHOST_UP_LOCK:-0}" != 1 ]; then
  flock -n -o /tmp/eanhost-up.lock env EANHOST_UP_LOCK=1 bash "$PWD/eanhost-up.sh" "$@" || exit 0
  exit 0
fi

# A stale pidfile must never be able to fake "already running": on an unclean
# shutdown the recorded pid can be recycled by an unrelated process, which
# would leave the control plane permanently down with nothing to retry it.
alive() { # pid needle
  local p=$1 needle=$2
  [ -n "$p" ] && [ -r "/proc/$p/cmdline" ] || return 1
  tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -q -- "$needle"
}

start_one() { # tag cmd...
  local tag=$1; shift
  local f=/tmp/eanhost-up-$tag.pid opid
  if [ -f "$f" ]; then
    opid=$(cat "$f" 2>/dev/null)
    if alive "$opid" "$tag"; then
      echo "$tag: already running (pid $opid)"
      return 0
    fi
  fi
  setsid nohup "$@" >>"$LOG" 2>&1 </dev/null &
  echo $! > "$f"
  echo "$tag: started (pid $!)"
}

# adb server first: the panel and eanwatch both shell out to adb, and after a
# reboot it is gone. Cheap and idempotent when it is already up.
adb start-server >/dev/null 2>&1 || adb start-server >>"$LOG" 2>&1

start_one "eanwatch" bash "$PWD/eanwatch.sh"
if [ -f "$PWD/panel/start.sh" ]; then
  bash "$PWD/panel/start.sh"
fi
