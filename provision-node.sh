#!/usr/bin/env bash
# provision-node.sh <serial> [name] [--wireless <ip>]
# Onboard a new Android tablet into the EanHost control plane.
#   <serial>   adb serial (from `adb devices`; e.g. XYZ170A or <ip>:5555)
#   [name]     display name (default: serial)
#   --wireless <ip>  enable wireless adb (adb tcpip 5555 + connect ip:5555) and
#                    register the node by its "ip:5555" serial
#
# Steps: authorize adb, scaffold /sdcard/EanHost, push payload scripts,
# register node in panel/config.json, restart panel, start the watchdog
# (eanwatch), start on-device supervisor, verify via API.
# Idempotent: re-running updates name / resurrects daemons.
set -u
cd "$(dirname "$0")"
PAYLOAD="$PWD/device-payload"
CFG="$PWD/panel/config.json"
ROOT=/sdcard/EanHost
SERIAL="${1:-}"; NAME="${2:-$SERIAL}"; WIRELESS=""; IP=""
REG_SERIAL="$SERIAL"

while [ $# -gt 0 ]; do
  case "$1" in
    --wireless) WIRELESS=1; IP="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

if [ -z "$SERIAL" ] || [ -n "$WIRELESS" ] && [ -z "$IP" ]; then
  echo "usage: provision-node.sh <serial> [name] [--wireless <ip>]"; exit 1
fi

adbd() { adb devices | awk '{print $1}' | grep -qx "$1"; }

echo "==> 1/8 target: $SERIAL (${NAME})"
if [ -n "$WIRELESS" ]; then
  if ! adbd "$SERIAL"; then
    echo "    USB serial $SERIAL not visible; plug it in first (enable USB debugging)." >&2
    exit 1
  fi
  echo "    enabling wireless adb..."
  adb -s "$SERIAL" tcpip 5555 >/dev/null || { echo "    tcpip failed" >&2; exit 1; }
  sleep 2
  adb connect "$IP:5555" >/dev/null || { echo "    connect $IP:5555 failed" >&2; exit 1; }
  REG_SERIAL="$IP:5555"
  SERIAL="$IP:5555"
fi
if ! adbd "$SERIAL"; then
  echo "    target serial not present in: `adb devices`" >&2
  echo "    (accepted the RSA authorization on the device?)" >&2
  exit 1
fi

echo "==> 2/8 scaffolding $ROOT"
for d in tasks results done files svc bots termux/inbox termux/outbox termux/done; do
  adb -s "$SERIAL" shell mkdir -p "$ROOT/$d" >/dev/null 2>&1
done

echo "==> 3/8 pushing payload scripts"
adb -s "$SERIAL" push "$PAYLOAD/svc/supervisor.sh" "$ROOT/svc/supervisor.sh" >/dev/null || exit 1
adb -s "$SERIAL" push "$PAYLOAD/svc/sstart.sh"     "$ROOT/svc/sstart.sh"     >/dev/null || exit 1
adb -s "$SERIAL" push "$PAYLOAD/termux/agent.sh"   "$ROOT/termux/agent.sh"   >/dev/null || exit 1
adb -s "$SERIAL" push "$PAYLOAD/termux/agentonce.sh" "$ROOT/termux/agentonce.sh" >/dev/null || exit 1

echo "==> 4/8 registering $REG_SERIAL -> ${NAME} in config"
python3 - "$CFG" "$REG_SERIAL" "$NAME" <<'PY' || { echo "    register failed" >&2; exit 1; }
import json, sys
cfg_path, serial, name = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(cfg_path, encoding="utf-8"))
nodes = cfg.get("nodes")
if not isinstance(nodes, list):
    nodes = []
pos = next((i for i, n in enumerate(nodes) if n and n[0] == serial), None)
if pos is None:
    nodes.append([serial, name])
else:
    nodes[pos][1] = name
cfg["nodes"] = nodes
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=2)
print("    nodes now: %s" % ", ".join("%s=%s" % (s, n) for s, n in nodes))
PY

echo "==> 5/8 restarting panel (picks up new node list)"
if pgrep -f "panel.py --port 8443" >/dev/null; then
  pkill -f "panel.py --port 8443" && sleep 1
fi
bash "$PWD/panel/start.sh"

echo "==> 6/8 starting host-side daemons (panel + eanwatch)"
bash "$PWD/eanhost-up.sh" || true

echo "==> 7/8 starting on-device supervisor"
adb -s "$SERIAL" shell "sh $ROOT/svc/sstart.sh" >/dev/null 2>&1 && echo "    sstart ok" || echo "    (supervisor start deferred; eanwatch/panel will retry)"

echo "==> 8/8 verifying: panel up + node in frontend SERIALS"
ok="no"
for i in 1 2 3 4 5; do
  if curl -sf -m 5 -o /dev/null "http://127.0.0.1:8443/"; then ok="yes"; break; fi
  sleep 1
done
if [ "$ok" = yes ]; then
  echo "    panel up"
  curl -s -m 5 "http://127.0.0.1:8443/" | grep -c "const SERIALS=\[\[" | sed 's/^/    frontend SERIALS line present: /'
  if curl -s -m 5 "http://127.0.0.1:8443/" | grep -qF "\"$REG_SERIAL\""; then
    echo "    node $REG_SERIAL reachable in served index"
  else
    echo "    WARN: $REG_SERIAL not in served index yet" >&2
  fi
else
  echo "    WARN: panel did not answer on :8443" >&2
fi

echo "==> done: $REG_SERIAL registered. Termux agent needs 'ean tstart $REG_SERIAL' once Termux is ready."
exit 0