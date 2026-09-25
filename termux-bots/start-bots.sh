#!/data/data/com.termux/files/usr/bin/bash
# Run inside Termux (or via panel Files editor "Run" won't work; use Termux app):
#   bash /sdcard/EanHost/termux/bots/start-bots.sh
cd /sdcard/EanHost/termux/bots
OLD=runner.pid
p=; [ -f "$OLD" ] && read p < "$OLD"
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
  echo "runner already running (pid $p)"
  exit 0
fi
nohup python runner.py >> run.log 2>&1 &
echo $! > "$OLD"
sleep 1
echo "started runner pid $(cat "$OLD")"