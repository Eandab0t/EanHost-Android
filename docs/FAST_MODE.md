# EanHost Fast Mode — Migration Guide

Fast Mode replaces the per-action `adb shell` spawn and `/sdcard` polling with a
persistent TCP link to a device-side runner socket, an event-driven supervisor
loop, and RAM-backed coordination files. It is **opt-in** and **fully
reversible**: with Fast Mode off, every code path falls straight through to the
original adb/file behaviour.

Nothing here requires downtime. The panel and runner are both written so the
new code can be deployed *before* it is switched on.

---

## 1. Architecture

```
 panel host (python3)                         tablet (termux)
 ----------------------                       -------------------------
 HTTP request
   -> fast_call(serial, payload)  --- TCP -->  runner.py TCP server
        |  adb forward tcp:P tcp:P                 |  handle cmd
        |  (once per node)                        |  ping / status / log
   <- JSON line reply <--------------------------+  bot / global
   |
   +-- on any error/timeout/None --> legacy adb shell + /sdcard files
```

- **Host** (`panel/panel.py`):
  `TCPClient` (one reused socket per endpoint, per-socket lock),
  `ensure_forward()` (idempotent `adb -s <serial> forward tcp:P tcp:P`),
  `fast_enabled()`, `tcp_endpoint()`, `fast_call()`. `fast_call()` returns
  `None` on *any* failure so callers fall back to adb transparently
  (`bots_status`, `bot_states`, `api_node_post`, `read_log`, `api_global`,
  `node_latency`).
- **Device** (`termux-bots/runner.py`):
  Fast Mode starts a TCP command server, a `threading.Event` (`WAKE`) so a
  command is applied immediately instead of after `CHECK_T`, and an
  inotify/mtime watcher on `bots.ctl`. `bots.status` is **always** written to
  disk so the legacy panel can still read it if Fast Mode is off.
  TCP is only started when `runner.json` has `"fast": true`.

---

## 2. Configuration

### Host — `panel/config.json`
Merged over `FAST_DEFAULTS` in `panel.py`:

| key | default | meaning |
| --- | --- | --- |
| `fast_mode` | `false` | master switch. off = pure legacy. |
| `fast_nodes` | `[]` | nodes to run in Fast Mode; empty + `fast_mode` = all |
| `tcp_forward_base` | `19000` | host port for node 0; node N = base+N |
| `tcp_ports` | `{}` | per-serial port override `{serial: port}` |
| `tcp_hosts` | `{}` | per-serial host override, default `127.0.0.1` |
| `adb_timeout` | `30` | adb fallback timeout (s) |
| `inotify_enabled` | `true` | host-side hint (watcher lives on device) |
| `ram_base` | `/data/data/com.termux/files/home/eanhost/ram` | documented device RAM dir |
| `fallback_to_files` | `true` | keep adb fallback enabled |
| `global_actions_enabled` | `true` | show/hide the fleet action bar |
| `log_poll_ms` | `2000` | UI log poll hint |

`nodes` and `password_hash` are the pre-existing keys and are unchanged.

### Device — `termux-bots/runner.json` (next to `runner.py`)

| key | default | meaning |
| --- | --- | --- |
| `fast` | `false` | start the TCP server + WAKE + watcher |
| `tcp_bind` | `127.0.0.1` | bind address; keep loopback + adb forward |
| `tcp_port` | `19000` | must equal the host's port for this node |
| `ram_dir` | `null` | optional tmpfs/RAM dir for `bots.ctl`/`bots.stopped` |
| `inotify` | `true` | watch `bots.ctl` for instant command pickup |

**Ports must match.** With defaults, the 1st node in `config.json` `nodes`
order uses 19000, the 2nd 19001, the 3rd 19002, …  Set `tcp_ports` on the host
and `tcp_port` on the device explicitly if you prefer fixed numbers.

---

## 3. Deploy (already done for the host)

1. **Host panel + runner (done):**
   ```
   scp panel/panel.py   ean@host:/home/ean/t337t/eanhost/panel/panel.py
   scp termux-bots/runner.py ean@host:/home/ean/t337t/eanhost/termux-bots/runner.py
   scp panel/static/index.html ean@host:/home/ean/t337t/eanhost/panel/static/index.html
   python3 -m py_compile panel/panel.py termux-bots/runner.py   # sanity
   ```
   The panel is supervised by `panel/start.sh` (called by `eanhost-up.sh`, which
   cron runs every minute via `host-watchdog.sh`). Restart it with:
   ```
   bash /home/ean/t337t/eanhost/panel/start.sh
   ```
   (If it says "already running", `pkill -f 'panel.py --port 8443'` first, or
   just let the next cron tick pick it up.)

2. **Device runner:** copy the new `runner.py` to each tablet's
   `termux/bots/` dir (same place as the existing runner) **without** a
   `runner.json` yet — this alone changes nothing, the loop behaves exactly like
   the legacy one plus the disk `bots.status` mirror.

3. **Enable per node** once you're ready (stage 2 below).

---

## 4. Staged rollout

Static port map (`tcp_forward_base` 19000 + node index). **No collisions** with
the existing forwards, which stop at 18999.

| index | node | serial | fast | tcp_port |
| --- | --- | --- | --- | --- |
| 0 | A10e | `RF8MB2B74QR` | **yes** | 19000 |
| 1 | T337T | `d45f315a` | no | 19001 |
| 2 | T380 | `b336faa7e413ae41` | **yes** | 19002 |
| 3 | T380-2 | `755b468fb9ea1b30` | no | 19003 |

- **Stage 0:** new code deployed, `fast_mode:false`, no `runner.json`. Zero
  behaviour change.
- **Stage 1 — DONE:** A10e. Device `runner.json`
  `{"fast": true, "tcp_bind": "127.0.0.1", "tcp_port": 19000, "inotify": true}`.
  Host `"fast_mode": true, "fast_nodes": ["RF8MB2B74QR"]`. Verified: latency
  `adb → tcp` (~7 ms), fallback proven (remove forward → silently adb, restore
  → tcp).
- **Stage 2 — DONE:** T380 (`tcp_port` 19002, live `verify` bot adopted by the
  restarted runner, logs stream, scoped `clear_logs` over tcp).
- **Stage 3 — all:** set `"fast_nodes": []` (all nodes) or leave `fast_mode`
  true. Watch `latency_via` on the dashboard turn from `adb` to `tcp`.
  (T337T and T380-2 still adb.)

### Enabling a node (repeatable recipe)

1. Device: back up `bots/runner.py`, push the new `runner.py`, push
   `runner.json` with the node's `tcp_port`.
2. Restart the runner. **If the node's `agent.sh` has the revive loop** (A10e),
   push an inbox script that only *kills* `runner.pid`; the agent revives it
   within ~2 s. **If not** (T380), push an inbox script that kills **and**
   re-runs `bots/start-bots.sh` in one go (see gotchas).
3. Host: append the serial to `fast_nodes` in `panel/config.json`. `cfg_load()`
   reads the file per request, so **no panel restart is needed**.
4. Verify `latency_via:"tcp"` (wait one `STATUS_T`=30 s cache cycle).

---

## 5. Verify

```bash
# token
TOK=$(curl -s -X POST http://localhost:8443/api/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<pw>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# Fast Mode view
curl -s -H "X-Token: $TOK" http://localhost:8443/api/config
#   -> {"fast_mode":false,"nodes":[{"serial":"...","fast":false}, ...]}

# live status: latency_via flips adb -> tcp and latency_ms drops
curl -s -H "X-Token: $TOK" http://localhost:8443/api/status | python3 -m json.tool | head -30

# incremental log (Fast Mode only): the first call (no since) returns the tail
#   plus offset == current file size. Pass that offset back as since= to receive
#   only the bytes written afterwards. reset:true means the log rotated or was
#   truncated (cleared) -> the client full-replaces.
curl -s -H "X-Token: $TOK" \
  "http://localhost:8443/api/node/<serial>/logs?name=botrun&bot=<name>"
```

On the device:
```bash
ss -ltnp 2>/dev/null | grep 19000      # runner listening (Fast Mode)
echo '{"cmd":"ping"}' | timeout 3 nc 127.0.0.1 19000   # {"ok":true,...}
```

Success looks like: `/api/config` shows `fast:true` for the node,
`latency_via:"tcp"`, bot start/stop applies instantly, logs stream with
`size`/`offset`.

---

## 6. Rollback

Instant, no restart required for the host:
- host: set `"fast_mode": false` in `panel/config.json` (or remove the serial
  from `fast_nodes`). The next request uses adb. `ensure_forward` results are
  per-process and harmless.
- device: set `"fast": false` in `runner.json` (or delete the file) and restart
  the runner. It returns to the legacy file/adb loop; `bots.status` keeps being
  written either way.

`adb forward … --remove-all` clears any tunnels if you want a clean slate.

---

## 7. Fallback guarantees

- Every `fast_call()` failure (no tunnel, connection refused, read timeout,
  malformed JSON, oversize reply, `{"ok":false,"error":"unknown"}`) returns
  `None`; the caller then runs the original adb/file code.
- adb is always wrapped in `subprocess.run(..., timeout=…)` inside
  try/except; timeouts return `None`/`124` rather than raising.
- Legacy `/sdcard/EanHost/termux/bots/bots.status` is still produced by the
  runner, so a panel running with Fast Mode off reads the same fleet.
- Nothing in the legacy path was deleted.

---

## 8. Gotchas

- **Port collisions:** `tcp_forward_base + index` must not clash with other
  adb forwards. Override with `tcp_ports`.
- **adb tunnel is per-host-process for the "already forwarded" cache**
  (`_FWD_DONE`); after killing the panel, the forward may already exist — that's
  fine, `adb forward` is idempotent.
- **Direct device IP:** set `tcp_hosts[serial]` to the device's LAN IP and skip
  the adb hop entirely (the device must reach the host network).
- **RAM dir** must be writable by the Termux user; if `makedirs` fails the
  runner silently keeps `bots.ctl`/`bots.stopped` on flash.
- **`bots.status` never moves to RAM** — the legacy panel depends on it.
- **Some nodes have no runner-revive block in `agent.sh`.** A10e's agent watches
  `bots/runner.pid` and restarts the runner on death; T380's does **not** (it
  only drains `inbox/*.sh`). On such a node, killing the runner leaves the bots
  unsupervised until you re-run `bots/start-bots.sh` via an inbox script. To
  give a node parity, replace its `agent.sh` with A10e's and bounce the agent
  (the host `eanwatch.sh` revives it).
- **`bots.json` must be replaced atomically.** `load_config()` returning `{}`
  (e.g. a truncated read during a non-atomic `adb push`) would make the runner
  kill every bot. The runner now keeps the last good config on a parse error;
  still, push to `bots.json.new` then `mv` over the live file (rename is atomic
  on `/sdcard`).
- **Clearing a running bot's log is safe:** the runner opens `run-<name>.log`
  with mode `"a"` (O_APPEND), so a truncate (`clear_logs`) does not create a
  sparse file — the next write resumes at offset 0.
