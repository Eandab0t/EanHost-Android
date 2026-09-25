# EanHost NOTES (protocol + runbook)

Shared root on the device: `/sdcard/EanHost` (single sdcardfs tree, same view for
`adb shell`, Termux apps, and the host over adb). Everything below holds for any
provisioned device; each tablet's `/sdcard` is its own storage.

## Layout (device)

```
/sdcard/EanHost/files/        staging area for file send/fetch
/sdcard/EanHost/tasks/        tasks for the device supervisor (bots)
/sdcard/EanHost/results/      outputs: <job>.out (stdout+stderr), <job>.exit (exit code)
/sdcard/EanHost/done/         completed tasks (moved here after execution)
/sdcard/EanHost/svc/          supervisor scripts (supervisor.sh, sstart.sh)
/sdcard/EanHost/sup.pid       "SUP <pid> <epoch>" heartbeat of the supervisor
/sdcard/EanHost/svc.log       supervisor event log
/sdcard/EanHost/bots/         long-running shell bots, one per *.sh (supervisor-owned)
/sdcard/EanHost/termux/       Termux lane (inbox/outbox/done, agent.sh, bots/)
```

## Primitive commands (host CLI)

`./ean` in this repo (add to PATH if desired). Every command talks to one device
directly over adb; target device defaults to `EAN_DEV` (otherwise the `SERIAL1`
placeholder in the script, which you must replace with a real serial from
`adb devices`), override with `EAN_DEV=<serial> ean <cmd>`.

```bash
ean send  /tmp/payload.bin /sdcard/EanHost/files/payload.bin
ean fetch /sdcard/EanHost/files/payload.bin /tmp/back.bin
ean run   /tmp/somescript.sh          # script executes ON THE HOST
ean cmd   "ls -l /sdcard/EanHost/files"
```

`ean send/fetch/cmd/run` go straight to adb (no queue round-trip). `bot`/`wait`
use the on-device supervisor; `tbot`/`twait`/`tstatus`/`tstart` use the Termux
agent (see below).

## Runbook (host-side control plane)

Everything on the host is conventional bash daemons; bring everything back after
a host reboot with one idempotent command:

```bash
bash eanhost-up.sh
```

It ensures one instance each of: the web panel (`panel/start.sh`) and
`eanwatch.sh` (one loop keeping every node's supervisor + Termux agent alive).

## Surviving a host outage

Three layers, so any single failure is repaired without a human:

1. **Boot**: a cron `@reboot` entry runs `eanhost-up.sh`, which also makes sure
   the adb server is up (it is gone after a reboot) before starting the daemons.
2. **Every minute**: `host-watchdog.sh` runs from cron and re-runs the
   idempotent `eanhost-up.sh`, reviving the panel, the eanwatch loop, or the
   adb server if any of them died. It also trims the `/tmp` logs, which are
   RAM-backed and would otherwise be able to OOM the host. Its
   `/tmp/eanhost-watchdog.last` heartbeat proves the cron tick is firing;
   `/tmp/eanhost-watchdog.log` only records actual revives, so silence means
   healthy.
3. **Every 30s**: `eanwatch.sh` itself revives the web panel if it is down, and
   each node's device supervisor and Termux agent.

Two details that are load-bearing, both learned the hard way:

- **Singleton checks must match the process command line, not just `kill -0`.**
  After an unclean shutdown the recorded pid can be recycled by an unrelated
  process; a bare `kill -0` then reports "already running" forever and the
  control plane stays down with nothing able to restart it. So `eanhost-up.sh`
  and `eanwatch.sh` read `/proc/<pid>/cmdline` before believing a pidfile.
- **Lock with `flock -o`, never a bare `flock -n 8`.** A lock held on an
  inherited file descriptor is inherited by every process the script spawns
  (the panel, the adb server), so the lock stays held for the life of the panel
  and every later run exits immediately. `flock -o` holds the lock in the parent
  and closes the fd in the child. Symptom if you get this wrong: the watchdog
  log is empty and nothing ever revives, while `fuser` shows the panel holding
  the lock file.

No root is required (cron is already present, and this is not a systemd
deployment). To upgrade to real systemd user units instead, the box would need
`loginctl enable-linger ean` once as root; without linger, user units do not
start at boot.

## Reboot self-heal (no human on the tablet)

- ADB authorization persists across reboot (keys live in `~/.android`; back them
  up).
- Supervisor: `eanwatch.sh` checks each node via `ean psup status`; if the
  supervisor is dead it calls `sstart` on the device. The guarded `sstart.sh`
  checks `kill -0` first, so duplicate/racing starts cannot stack supervisors.
- Termux agent: `eanwatch.sh` calls `ean tstart` when the heartbeat is stale;
  `tstart` includes keyguard dismissal (`input keyevent 224` + swipe) so it
  works the instant the screen is locked post-boot.
- Watch out: after reboot, `agent.heart`/`sup.pid` from before the reboot look
  "fresh" for up to 60s; stale `results/*` can fake a health check. Clear
  markers (or use unique task names) when testing reboot recovery.

## Device supervisor (bots that survive host shutdown)

A daemon runs ON THE DEVICE (detached from adb, reparented to init). It polls
`tasks/` every 2s, runs each `*.sh` as `sh`, writes `results/<task>.out` (and
`.exit`), moves the task to `done/`. Also keeps long-running shell bots in
`bots/` alive (respawned on death; delete the `.sh` to stop it). Once running it
needs NOTHING on the host: the server can power off mid-task and the task still
finishes.

```bash
ean psup status              # is the supervisor alive?
ean psup start               # start it after tablet reboot
ean psup stop

ean bot  /path/to/task.sh [name]   # queue one task on the device; returns immediately
ean wait taskname                  # poll results/<taskname>.out + .exit
```

`ean wait` (and `ean send/fetch/cmd/run`) poll for `results/<name>.exit`,
i.e. true completion; `<name>.out` is created empty at task START, so polling on
`.out` alone returns before a long task finishes. Task script runs as uid 2000
(shell) with sdcard_rw; it may write anywhere in `/sdcard/EanHost`. Device reboot
stops the supervisor (no autostart yet; run `ean psup start`).

## Termux agent (fully headless executor + self-heal)

A Termux-UID daemon (`termux/agent.sh`, runs as a Termux uid with Termux env,
`PREFIX=/data/data/com.termux/files/usr`) processes `termux/inbox/*.sh`, writes
`termux/outbox/<task>.out` + `.exit`, moves the task to `termux/done/`. It is
launched WITHOUT a human:

1. `adb shell am start -n com.termux/.app.TermuxActivity` (wake + focus)
2. `input tap 400 400` (put focus on the terminal)
3. `input text` a **plain, typeable command** `sh /sdcard/EanHost/termux/agentonce.sh`
   + `input keyevent 66`. PITFALL: `input text` cannot type `( ) & > !` etc.;
   the launcher MUST be pushed as a file (`agentonce.sh`); the injected line is
   only `sh .../agentonce.sh` (alnum, `/`, `.`, spaces).
4. `agentonce.sh` runs `( sh agent.sh > launch.log 2>&1 & )`; it detaches and
   reparents to init, so it survives Termination of TermuxActivity.

Liveness is HEARTBEAT only: `agent.heart` = `iter pid epoch` written every 2s;
fresh iff `(device date +%s) - epoch < 60s`. Shell `kill -0` CANNOT probe a
Termux-UID process (adb uid 2000, EPERM); matching by pid in `ps` also lies.

```bash
ean tstatus                 # ALIVE iff heart age < 60s
ean tstart                  # idempotent launch (wake, focus, inject, wait 15s)
ean tbot task.sh [name]     # queue a task for the agent (inbox/)
ean twait taskname          # poll termux/outbox/<task>.out + .exit
```

Things that are VERIFIED:

- Tasks run while Termux stays in the BACKGROUND (UI on Home); fully headless.
- `am force-stop com.termux` (or app kill) freezes the heart; the next watchdog
  cycle sees it stale and relaunches a NEW agent; queued tasks ride through.
- Agent self-singleton via `mkdir termux/agent.lock` (pid inside): overlapping
  `tstart`/watchdog injections cannot produce two live agents.
- Reboot: on cold boot the agent is gone until a watchdog (or a manual `tstart`)
  notices; the stale lock (dead pid) is reaped by the lock keep-alive check.
- Python/Discord bots under Termux are supervised by `termux-bots/runner.py`
  (spawns/respawns each entry in `bots.json`, writes `bots.status`).

## Constraints / gotchas

- Every adb call must use `-s <serial>` when more than one device is attached.
- Old Android toolboxes (Android 5.1-era) lack `wc`, `pidof`, `tail`: use
  `grep -c ""` for counting, `ps | grep` for pids, `grep ""`/`head -n -1`
  instead of tail.
- Multi-bot / file-mgmt feasibility verified: push/pull md5-identical, device
  `cp/mv` fine, concurrent device writers overlap cleanly, bursts handled.
- **ONE supervisor per device.** Two supervisors racing `tasks/` corrupt each
  other's bookkeeping (same task run twice, empty names, failed mv). `sstart.sh`
  guards itself (`kill -0` on `sup.pid` before spawning), so `ean psup start`
  / watchdog cannot stack supervisors. Same discipline via host pidfiles.
- **Never `adb kill-server` on the host if you rely on the same ADB key.**
  Restarting the adb server is fine; regenerating keys drops every device to
  `unauthorized` (recovery = tap "Allow USB debugging" again on each tablet).
  Back up `~/.android`.
- Security: anyone who can write `tasks/`, `termux/inbox/`, or `bots/` on a LAN
  device is remote code as that lane's uid. Intended, LAN/trusted-network only.

## Web panel (EanHost Control)

- URL: `http://<host-ip>:8443` (HTTP, from LAN). Panel binds 0.0.0.0:8443,
  stdlib only (`http.server` + calls to adb), runs as whatever user starts it.
  The adb server must be running for the panel (`adb -L tcp:5037 fork-server`).
- Single admin password: sha256 in `panel/config.json`. Change it:
  `python3 panel/panel.py --set-pass '<new>'` (writes config.json atomically; no
  restart needed; read per request).
- Start/stop (also wired into `eanhost-up.sh`):
  `bash panel/start.sh`  (idempotent, single instance)
  `pkill -f 'panel.py --port 8443'`
- Features: dashboard (battery/supervisor/agent/bots per node), per-node tabs
  Files / Console / Tasks / Logs. Files root is `/sdcard` (pull/push via adb).
  Tasks = supervisor (`tasks/`) or termux (`inbox/`) executors, polls for
  output. Logs = svc.log / agent.log / heart / launch.log. EZ install ships a
  bot ZIP to `termux/bots/<name>/`, installs deps, and registers it in
  `bots.json` (tokens stay in the bot's own `.env`; never in `bots.json`).
- Auth: login returns token; cookie `ean_sess` (non-HttpOnly so JS can use it),
  token also accepted as `?t=`. Server sessions in memory (cleared on panel
  restart), 7-day expiry.