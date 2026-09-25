#!/usr/bin/env bash
# contract-test.sh - EanHost Durable Queue Contract v1 conformance harness.
# Host: bash on any host. Drives live tablets over adb and asserts every contract clause.
# Usage: bash contract-test.sh            # both review-known devices
#        bash contract-test.sh SERIAL1   # one device only
# Reference: QUEUE-CONTRACT.md (../../docs/) §Verification.
# Device-toolbox-safe: only sh + ls/cat/grep -c/date/dd/cp/mv/rm/sleep on-device.
set -u
R=/sdcard/EanHost
LD=/tmp/eanhost-contract
TAG="ct$(date +%s)"
mkdir -p "$LD"
PASS=0; FAIL=0; SKIP=0
DEVICES="${1:-SERIAL1 SERIAL2}"

report()  { printf '%-14s %-24s %-5s %s\n' "$1" "$2" "$3" "${4:-}"; }
done_sum() { PASS=$((PASS+1)); report "$1" "$2" PASS "$3"; }
nope()     { FAIL=$((FAIL+1)); report "$1" "$2" FAIL "$3"; }
skip()     { SKIP=$((SKIP+1)); report "$1" "$2" SKIP "${3:-not applicable}"; }

dev_cat() { adb -s "$1" shell cat "$2" 2>/dev/null | tr -d '\r'; }

dev_count() { # serial, glob-path (already quoted) -> count of matches on device
  adb -s "$1" shell "ls $2 2>/dev/null | grep -c ''" 2>/dev/null | tr -d '\r'
}

alive_on_dev() { # serial, pid -> ALIVE/DEAD (kill -0 works: same uid 2000)
  adb -s "$1" shell "kill -0 $2 2>/dev/null && echo ALIVE || echo DEAD" 2>/dev/null | tr -d '\r'
}

wait_exits() { # serial, tag, count, timeout_s
  local s=$1 tag=$2 want=$3 tmo=$4 got=0 i=0
  while [ $i -lt $tmo ]; do
    got=$(dev_count "$s" "$R/results/$tag-*.exit")
    [ "$got" = "$want" ] && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

cleanup_lane() { # serial, tag
  adb -s "$1" shell "rm -f $R/results/$2-*.out $R/results/$2-*.exit $R/done/$2-*.sh $R/files/$2-*.dat" >/dev/null 2>&1
  adb -s "$1" shell "rm -f $R/termux/outbox/$2-*.out $R/termux/outbox/$2-*.exit $R/termux/done/$2-*.sh" >/dev/null 2>&1
  adb -s "$1" shell "rm -f $R/results/$2-*.out $R/results/$2-*.exit" >/dev/null 2>&1
  : > "$LD/ok.txt"; : > "$LD/err.txt"; : > "$LD/wait.txt"; : > "$LD/fm.txt"
}

push_job() { # serial, tag, name, local-file
  adb -s "$serial" push "$4" "$R/tasks/$3.sh" >/dev/null 2>&1 || printf 'push fail for %s\n' "$3"
}

echo "== EanHost Durable Queue Contract v1 conformance =="
echo "tag=$TAG  devices=$DEVICES"

if ! adb devices 2>/dev/null | grep -q "device$"; then
  echo "ABORT: no adb devices in 'device' state (check USB / auth)"; exit 2
fi

for serial in $DEVICES; do
  echo "--- device $serial"
  s="$serial"

  # 0. Worker liveness clause (§5/§6): one supervisor, alive, lock consistent.
  sup=$(dev_cat "$s" "$R/sup.pid")
  read _ sup_pid _ <<<"$sup"
  lock_pid=$(dev_cat "$s" "$R/sup.lock/pid")
  if [ -n "$sup_pid" ] && [ "$sup_pid" = "$lock_pid" ] && [ "$(alive_on_dev "$s" "$sup_pid")" = "ALIVE" ]; then
    done_sum "$s" "device-lane worker alive+lock" "sup $sup_pid"
  else
    nope "$s" "device-lane worker alive+lock" "sup='$sup' lock='$lock_pid'"; continue
  fi

  # 1. Burst job submission (§3/§4).
  cat > "$LD/ok.sh"   <<EOF
echo hello-$TAG
exit 0
EOF
  cat > "$LD/err.sh"  <<EOF
echo failing-$TAG
exit 7
EOF
  cat > "$LD/wait.sh"  <<EOF
sleep 12
echo slow-done-$TAG
EOF
  cat > "$LD/fm.sh"    <<EOF
dd if=/dev/urandom of=$R/files/$TAG.dat bs=1024 count=4 2>/dev/null
cp $R/files/$TAG.dat $R/files/$TAG-copy.dat
mv $R/files/$TAG-copy.dat $R/files/$TAG-mv.dat
ls -l $R/files/$TAG-mv.dat
EOF
  push_job "$s" "$TAG" "$TAG-ok"   "$LD/ok.sh"
  push_job "$s" "$TAG" "$TAG-err"  "$LD/err.sh"
  push_job "$s" "$TAG" "$TAG-wait" "$LD/wait.sh"
  push_job "$s" "$TAG" "$TAG-fm"   "$LD/fm.sh"

  # 2. Progress signal (§4): before ANY exit appears, .out exists for the long job.
  sleep 3
  wait_out=$(dev_count "$s" "$R/results/$TAG-wait.out")
  wait_ex=$(dev_count "$s" "$R/results/$TAG-wait.exit")
  if [ "$wait_ex" = "0" ] && [ "$wait_out" = "1" ]; then
    done_sum "$s" "progress: out-then-exit" "out=1 exit=0"
  else
    nope "$s" "progress: out-then-exit" "out=$wait_out exit=$wait_ex (soft: worker may not have started yet)"
  fi

  # 3. Completion signal (§4): all four .exit appear.
  if wait_exits "$s" "$TAG" 4 45; then
    done_sum "$s" "completion signal x4" "exit files present"
  else
    nope "$s" "completion signal x4" "$(dev_count "$s" "$R/results/$TAG-*.exit")/4 within 45s"
  fi

  # 4. Exit codes (ok=0 err=7 wait=0 fm=0).
  ok_e=$(dev_cat "$s" "$R/results/$TAG-ok.exit");   _=$ok_e
  er_e=$(dev_cat "$s" "$R/results/$TAG-err.exit");  _=$er_e
  wt_e=$(dev_cat "$s" "$R/results/$TAG-wait.exit"); _=$wt_e
  fm_e=$(dev_cat "$s" "$R/results/$TAG-fm.exit");   _=$fm_e
  if [ "$ok_e" = "0" ] && [ "$er_e" = "7" ] && [ "$wt_e" = "0" ] && [ "$fm_e" = "0" ]; then
    done_sum "$s" "exit codes preserved" "0/7/0/0"
  else
    nope "$s" "exit codes preserved" "ok=$ok_e err=$er_e wait=$wt_e fm=$fm_e"
  fi

  # 5. Output content (§4).
  ok_o=$(dev_cat "$s" "$R/results/$TAG-ok.out")
  wt_o=$(dev_cat "$s" "$R/results/$TAG-wait.out")
  if [ "$ok_o" = "hello-$TAG" ] && case "$wt_o" in *"slow-done-$TAG"*) true;; *) false;; esac; then
    done_sum "$s" "stdout captured" "hello/slow-done present"
  else
    nope "$s" "stdout captured" "ok='$ok_o' wait='$wt_o'"
  fi

  # 6. Archive semantics (§5): inputs displaced from inbox, archived in done/.
  t_in=$(dev_count "$s" "$R/tasks/$TAG-*.sh")
  d_in=$(dev_count "$s" "$R/done/$TAG-*.sh")
  if [ "$t_in" = "0" ] && [ "$d_in" = "4" ]; then
    done_sum "$s" "archive: inbox->done/" "tasks=0 done=4"
  else
    nope "$s" "archive: inbox->done/" "tasks=$t_in done=$d_in"
  fi

  # 7. Idempotence/archive marker (§5/§6): exactly one run+done per job in svc.log.
  adb -s "$s" pull "$R/svc.log" "$LD/svc-$s.log" >/dev/null 2>&1
  uniq_ok=1
  for n in ok err wait fm; do
    c=$(grep -c "run b=\[$TAG-$n\]" "$LD/svc-$s.log" || true)
    [ "$c" = "1" ] || uniq_ok=0
  done
  if [ "$uniq_ok" = "1" ]; then
    done_sum "$s" "exactly-once run markers" "1 run b= per job"
  else
    nope "$s" "exactly-once run markers" "grep counts: $(for n in ok err wait fm; do echo -n "$TAG-$n="; grep -c "run b=\[$TAG-$n\]" "$LD/svc-$s.log" || true; echo -n ' '; done)"
  fi

  # 8. File round-trip THROUGH the queue (§5 load-bearing path): md5 identical, cp+mv worked.
  adb -s "$s" pull "$R/files/$TAG.dat"     "$LD/$TAG-a.dat" >/dev/null 2>&1
  adb -s "$s" pull "$R/files/$TAG-mv.dat"  "$LD/$TAG-b.dat" >/dev/null 2>&1
  if [ -f "$LD/$TAG-a.dat" ] && [ -f "$LD/$TAG-b.dat" ]; then
    ma=$(md5sum "$LD/$TAG-a.dat" | cut -d' ' -f1)
    mb=$(md5sum "$LD/$TAG-b.dat" | cut -d' ' -f1)
    if [ "$ma" = "$mb" ] && [ -n "$ma" ]; then
      done_sum "$s" "file rt md5 round-trip" "a=b ($ma)"
    else
      nope "$s" "file rt md5 round-trip" "a=$ma b=$mb"
    fi
  else
    nope "$s" "file rt md5 round-trip" "pull failed (files missing?)"
  fi
  c_present=$(dev_count "$s" "$R/files/$TAG-copy.dat")
  mv_present=$(dev_count "$s" "$R/files/$TAG-mv.dat")
  if [ "$c_present" = "0" ] && [ "$mv_present" = "1" ]; then
    done_sum "$s" "device cp+mv in job" "copy gone, mv present"
  else
    nope "$s" "device cp+mv in job" "copy=$c_present mv=$mv_present"
  fi

  # 9. Termux lane (§2): inbox/outbox/done, .exit completion, heartbeat-eligible only.
  heart=$(dev_cat "$s" "$R/termux/agent.heart")
  read i tp te <<<"$heart"  # i pid epoch
  now=$(adb -s "$s" shell date +%s 2>/dev/null | tr -d '\r')
  if [ -n "$te" ] && [ $((now - te)) -lt 60 ]; then
    printf 'echo termux-%s\n' "$TAG" > "$LD/tx.sh"
    adb -s "$s" push "$LD/tx.sh" "$R/termux/inbox/$TAG-tx.sh" >/dev/null 2>&1
    tx=0; i=0
    while [ $i -lt 30 ]; do
      x=$(dev_count "$s" "$R/termux/outbox/$TAG-tx.exit")
      [ "$x" = "1" ] && { tx=1; break; }
      sleep 1; i=$((i+1))
    done
    if [ "$tx" = "1" ]; then
      txe=$(dev_cat "$s" "$R/termux/outbox/$TAG-tx.exit")
      txo=$(dev_cat "$s" "$R/termux/outbox/$TAG-tx.out")
      txd=$(dev_count "$s" "$R/termux/done/$TAG-tx.sh")
      txl=$(dev_count "$s" "$R/termux/inbox/$TAG-tx.sh")
      if [ "$txe" = "0" ] && [ "$txo" = "termux-$TAG" ] && [ "$txd" = "1" ] && [ "$txl" = "0" ]; then
        done_sum "$s" "termux lane conforms" "out/exit/done ok"
      else
        nope "$s" "termux lane conforms" "exit=$txe out='$txo' done=$txd in=$txl"
      fi
    else
      nope "$s" "termux lane conforms" "no .exit within 30s (heart fresh $te)"
    fi
    c_lane=1
  else
    skip "$s" "termux lane" "heart stale/missing (te=$te, now=$now)"
    c_lane=0
  fi

  # 10. done.

  cleanup_lane "$s" "$TAG"
done

echo "== summary: PASS=$PASS FAIL=$FAIL SKIP=$SKIP =="
[ "$FAIL" = "0" ] && exit 0
exit 1