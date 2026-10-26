#!/usr/bin/env bash
# Start the EanHost web panel (port 8443). Idempotent.
cd "$(dirname "$0")"
if pgrep -f "panel.py --port 8443" >/dev/null; then
  echo "panel: already running (pid $(pgrep -f 'panel.py --port 8443'))"
  exit 0
fi
setsid nohup python3 panel.py --port 8443 --bind 0.0.0.0 </dev/null >>/tmp/eanhost-panel.log 2>&1 &
disown
echo "panel: started (pid $!)"