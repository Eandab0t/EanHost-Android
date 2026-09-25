#!/data/data/com.termux/files/usr/bin/bash
D=/sdcard/EanHost/termux
IN=$D/inbox
OUT=$D/outbox
DN=$D/done
LOCK=$D/agent.lock
mkdir -p "$IN" "$OUT" "$DN"
while ! mkdir "$LOCK" 2>/dev/null; do
  lp=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "$lp" ] && ! kill -0 "$lp" 2>/dev/null; then
    rm -rf "$LOCK"
    continue
  fi
  sleep 3
done
echo $$ > "$LOCK/pid"
echo "AGENT $$ $(date +%s) lock-held" >> "$D/agent.log"
i=0
while true; do
  mkdir -p "$IN" "$OUT" "$DN"
  for f in "$IN"/*.sh; do
    [ -e "$f" ] || continue
    n=${f##*/}
    b=${n%.sh}
    [ -z "$b" ] && continue
    echo "run $b $$ $(date +%s)" >> "$D/agent.log"
    bash "$f" > "$OUT/$b.out" 2>&1
    echo $? > "$OUT/$b.exit"
    mv "$f" "$DN/$n" 2>> "$D/agent.log"
    echo "done $b $$ $(date +%s)" >> "$D/agent.log"
  done
  i=$((i + 1))
  echo "$i $$ $(date +%s)" > "$D/agent.heart"
  rp=$(cat "$D/bots/runner.pid" 2>/dev/null)
  if [ -z "$rp" ] || ! kill -0 "$rp" 2>/dev/null; then
    bash "$D/bots/start-bots.sh" >> "$D/bots/run.log" 2>&1
  fi
  sleep 2
done