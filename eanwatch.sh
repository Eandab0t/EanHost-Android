#!/usr/bin/env bash
# eanwatch.sh - single loop keeping every node's supervisor + termux agent alive.
# Every CHECK_T seconds: for each node in panel/config.json, if the node is
# present: make sure the on-device supervisor is alive and the termux agent is
# heartbeating; revive either otherwise.
set -u
cd "$(dirname "$0")"
CFG="$PWD/panel/config.json"
LOG=/tmp/eanhost-eanwatch.log
PF=/tmp/eanhost-eanwatch.pid
CHECK_T=30

reads_nodes() {
  local out
  out=$(python3 "$PWD/nodes.py" "$CFG" 2>/dev/null)
  if [ -n "$out" ]; then
    echo "$out" | while IFS=$'\t' read -r name serial; do
      [ -n "$serial" ] && echo "$serial"
    done
  else
    printf 'SERIAL1\nSERIAL2\n'
  fi
}

solo() {
  local f=$1 opid
  if [ -f "$f" ]; then
    opid=$(cat "$f" 2>/dev/null)
    # Match the cmdline, not just kill -0: after an unclean shutdown the
    # recorded pid can be recycled by an unrelated process, and a bare
    # kill -0 would then report "already running" forever, leaving every node
    # unsupervised with nothing able to restart this loop.
    if [ -n "$opid" ] && [ -r "/proc/$opid/cmdline" ] &&
       tr '\0' ' ' <"/proc/$opid/cmdline" 2>/dev/null | grep -q 'eanwatch\.sh'; then
      echo "eanwatch.sh already running (pid $opid)" >&2
      exit 0
    fi
  fi
  echo $$ > "$f"
}

solo "$PF"
echo "eanwatch running (pid $$): keeps supervisors+agents alive every ${CHECK_T}s"
while true; do
  if ! pgrep -f "panel[.]py --port 8443" >/dev/null; then
    echo "$(date +%s): panel down -> start.sh" >>"$LOG"
    bash "$PWD/panel/start.sh" >>"$LOG" 2>&1
  fi
  for serial in $(reads_nodes); do
    if ! adb devices 2>/dev/null | awk '{print $1}' | grep -qx "$serial"; then
      echo "$(date +%s): $serial not present, skipping" >>"$LOG"
      continue
    fi
    spid=$(EAN_DEV=$serial "$PWD/ean" psup status 2>/dev/null | sed -n 's/.*alive=//p')
    if [ "$spid" != "yes" ]; then
      echo "$(date +%s): $serial supervisor missing/dead -> sstart" >>"$LOG"
      adb -s "$serial" shell "sh /sdcard/EanHost/svc/sstart.sh" >>"$LOG" 2>&1
      sleep 2
    fi
    if ! EAN_DEV=$serial "$PWD/ean" tstatus 2>/dev/null | grep -q ALIVE; then
      bf=/tmp/eanhost-last-tstart-$serial
      last=$(cat "$bf" 2>/dev/null || echo 0)
      now=$(date +%s)
      if [ $((now - last)) -ge 300 ]; then
        echo "$now: $serial agent stale -> tstart" >>"$LOG"
        EAN_DEV=$serial "$PWD/ean" tstart >>"$LOG" 2>&1
        echo "$now" > "$bf"
      else
        echo "$now: $serial agent stale, tstart backoff" >>"$LOG"
      fi
    fi
  done
  sleep $CHECK_T
done
