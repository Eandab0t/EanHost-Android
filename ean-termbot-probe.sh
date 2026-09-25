#!/system/bin/sh
H=/sdcard/EanHost/termux/bots
echo "###TREE"
ls -la "$H" 2>/dev/null || echo "NO_DIR"
echo "###STATUS"
cat "$H/bots.status" 2>/dev/null || echo "NO_STATUS"
echo "###PID"
pgrep -a -f "runner.py" 2>/dev/null || echo "NO_PID"
echo "###HEART"
cat /sdcard/EanHost/termux/agent.heart 2>/dev/null || echo "NO_HEART"
echo "###LAUNCH"
head -n 30 /sdcard/EanHost/termux/launch.log 2>/dev/null || echo "NO_LAUNCH"
