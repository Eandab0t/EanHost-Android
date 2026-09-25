#!/system/bin/sh
# Start the device supervisor if not already running (singleton via sup.pid).
# Bugs avoided: no awk/tr/cut (absent on Android 5.1 toolboxes) - uses `read`.
D=/sdcard/EanHost
pid=; read _ pid rest < $D/sup.pid 2>/dev/null
if [ -n "$pid" ]; then
  kill -0 "$pid" 2>/dev/null && exit 0
fi
( sh $D/svc/supervisor.sh </dev/null >/dev/null 2>&1 & )