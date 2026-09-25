#!/system/bin/sh
# EanHost on-device supervisor: runs dropped tasks AND keeps long-running bots alive.
#   tasks/*.sh  -> run once, result to results/<name>.out + .exit, archive to done/
#   bots/*.sh   -> long-running foreground scripts; respawned if dead (per-bot pid in bots/id.<name>.pid)
#                  delete bots/<name>.sh to make the supervisor kill the instance.
# NOTE: sh-builtins + mkdir/cat/echo/kill/mv/sleep only (old toolboxes lack awk/tr/cut).
D=/sdcard/EanHost
T=$D/tasks
R=$D/results
DD=$D/done
B=$D/bots
mkdir -p $T $R $DD $B
while ! mkdir $D/sup.lock 2>/dev/null; do
  lpid=; read lpid rest < $D/sup.lock/pid 2>/dev/null
  if [ -n "$lpid" ] && kill -0 "$lpid" 2>/dev/null; then
    exit 0
  fi
  rm -rf $D/sup.lock
done
echo $$ > $D/sup.lock/pid
echo "SUP $$ $(date +%s)" > $D/sup.pid
while true; do
  for pf in $B/id.*.pid; do
    [ -e "$pf" ] || continue
    base=${pf##*/id.}
    base=${base%.pid}
    [ -e "$B/$base.sh" ] && continue
    pid=; read pid rest < "$pf" 2>/dev/null
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    echo "bot down $base" >> $D/svc.log
    rm -f "$pf" "$B/log.$base"
  done
  for b in $B/*.sh; do
    n=${b##*/}
    [ "$n" = "*.sh" ] && continue
    base=${n%.sh}
    pf=$B/id.$base.pid
    pid=; read pid rest < "$pf" 2>/dev/null
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    rm -f "$pf"
    echo "bot up $base" >> $D/svc.log
    ( sh "$b" > "$B/log.$base" 2>&1 & echo $! > "$pf" )
  done
  for f in $T/*.sh; do
    n=${f##*/}
    [ "$n" = "*.sh" ] && continue
    [ -e "$f" ] || continue
    b=${n%.sh}
    [ -z "$b" ] && b=task-$(date +%s)
    echo "run b=[$b]" >> $D/svc.log
    sh "$f" > "$R/$b.out" 2>&1
    echo $? > "$R/$b.exit"
    mv "$f" "$DD/$b.sh" 2>>$D/svc.log
    echo "done b=[$b]" >> $D/svc.log
  done
  sleep 2
done