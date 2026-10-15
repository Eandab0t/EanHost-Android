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

The web panel listens on port `8080` by default. Fast Mode uses a separate
loopback TCP-forward range beginning at `19000`; keep these ports distinct.

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

## 3. Deploy

Use your own deployment host and installation directory in place of the
placeholders below:

1. **Deploy the host panel and runner:**
   ```
   scp panel/panel.py <user>@<host>:/path/to/install/panel/panel.py
   scp termux-bots/runner.py <user>@<host>:/path/to/install/termux-bots/runner.py
   scp panel/static/index.html <user>@<host>:/path/to/install/panel/static/index.html
   python3 -m py_compile panel/panel.py termux-bots/runner.py
   ```
   The panel is supervised by `panel/start.sh` and started by `eanhost-up.sh`.
   Restart it from the installation directory with:
   ```
   bash /path/to/install/panel/start.sh
   ```

2. **Device runner:** copy the new `runner.py` to each device's existing
   `termux/bots/` directory. Leave `runner.json` absent or set `"fast": false`
   until you are ready to enable Fast Mode; deploying the runner alone keeps
   the legacy behavior.

3. **Enable Fast Mode per device** using the staged rollout below.

---

## 4. Staged rollout

Use a unique device name and ADB serial for each node. These values are
placeholders, not real device identifiers. The default TCP-forward port is
`19000 + node index`; the panel itself uses port `8080`.

| index | node | serial | fast | tcp_port |
| --- | --- | --- | --- | --- |
| 0 | `<device-name-1>` | `<adb-serial-1>` | no | 19000 |
| 1 | `<device-name-2>` | `<adb-serial-2>` | no | 19001 |
| 2 | `<device-name-3>` | `<adb-serial-3>` | no | 19002 |

- **Stage 0:** deploy the updated code with `fast_mode` disabled and no
  device-side Fast Mode configuration. Existing behavior is unchanged.
- **Stage 1:** enable one test device by setting its `runner.json` to
  `{"fast": true, "tcp_bind": "127.0.0.1", "tcp_port": 19000, "inotify": true}`,
  then add its ADB serial to the host's `fast_nodes`.
- **Stage 2:** verify status, bot controls, and log streaming for that device.
  Confirm that an unavailable TCP connection falls back to ADB.
- **Stage 3:** repeat for additional devices, assigning each a unique TCP port.
  Set `fast_nodes` to `[]` to include every configured node when `fast_mode` is
  enabled.

### Enabling a node (repeatable recipe)

1. Back up the device's existing `runner.py`, deploy the updated runner, and
   add a `runner.json` containing that device's assigned `tcp_port`.
2. Restart the runner using the device's existing supported procedure. If its
   Termux agent does not automatically revive the runner, start it explicitly
   after stopping the old process.
3. Add the device's ADB serial to `fast_nodes` in `panel/config.json`.
   `cfg_load()` reads the file per request, so a panel restart is not needed.
4. Verify `latency_via:"tcp"` (wait one `STATUS_T`=30 s cache cycle).

---

## 5. Verify

```bash
# token
TOK=$(curl -s -X POST http://localhost:8080/api/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<pw>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# Fast Mode view
curl -s -H "X-Token: $TOK" http://localhost:8080/api/config
#   -> {"fast_mode":false,"nodes":[{"serial":"...","fast":false}, ...]}

# live status: latency_via flips adb -> tcp and latency_ms drops
curl -s -H "X-Token: $TOK" http://localhost:8080/api/status | python3 -m json.tool | head -30

# incremental log (Fast Mode only): the first call (no since) returns the tail
#   plus offset == current file size. Pass that offset back as since= to receive
#   only the bytes written afterwards. reset:true means the log rotated or was
#   truncated (cleared) -> the client full-replaces.
curl -s -H "X-Token: $TOK" \
  "http://localhost:8080/api/node/<adb-serial>/logs?name=<log-name>&bot=<bot-name>"
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
- **Runner recovery depends on the installed Termux agent.** Some agent
  versions watch `bots/runner.pid` and restart the runner; others only drain
  `inbox/*.sh`. If the installed agent does not revive the runner, start it
  explicitly after a restart or deploy an agent version that supports runner
  recovery. The host `eanwatch.sh` can revive the agent itself.
- **`bots.json` must be replaced atomically.** `load_config()` returning `{}`
  (e.g. a truncated read during a non-atomic `adb push`) would make the runner
  kill every bot. The runner now keeps the last good config on a parse error;
  still, push to `bots.json.new` then `mv` over the live file (rename is atomic
  on `/sdcard`).
- **Clearing a running bot's log is safe:** the runner opens `run-<name>.log`
  with mode `"a"` (O_APPEND), so a truncate (`clear_logs`) does not create a
  sparse file — the next write resumes at offset 0.
