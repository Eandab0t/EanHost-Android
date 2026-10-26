# EanHost transport -- NOTES (protocol + runbook, verified 2026-09-07)

> **OPTIMIZATION pass 2026-09-09.** The `queue/` + `watch.sh` x58 lane is
> REMOVED. `ean send/fetch/cmd` now call adb directly (no queue round-trip, no
> 2s poller). The old per-device `twatch`+`swatch` daemons are replaced by one
> `eanwatch.sh` loop covering all nodes. The web panel caches node status
> (30s background refresh) and dropped the SSE console (single-shot `POST
> /cmd`). Sections below that describe watch.sh / queue / twatch/swatch /
> cmdstream are HISTORY, kept for context — the live layout, contract lanes
> (device `tasks/`, termux `termux/inbox/`), and on-device caveats still apply.

Shared root on the device: `/sdcard/EanHost` (single sdcardfs tree, same view for
`adb shell`, Termux apps, and the X58 over adb). Everything below is tested and
holds for BOTH tablets (T337T `d45f315a` and A10e `RF8MB2B74QR`) -- the layout
below is per-device (each tablet's `/sdcard` is its own storage).

## Layout (device)
```
/sdcard/EanHost/queue/    jobs for the X58 (dropped by anyone who can write shared storage)
/sdcard/EanHost/results/  outputs: <job>.out (stdout+stderr), <job>.exit (exit code)
                        ALSO used by the supervisor for <task>.out / <task>.exit
/sdcard/EanHost/files/    staging area for file send/fetch
/sdcard/EanHost/tasks/    tasks for the device supervisor (bots)
/sdcard/EanHost/done/     completed tasks (moved here after execution)
/sdcard/EanHost/svc/      supervisor scripts (supervisor.sh, sstart.sh)
/sdcard/EanHost/sup.pid   "SUP <pid> <epoch>" heartbeat of the supervisor
/sdcard/EanHost/svc.log   supervisor event log
```

## Job format
A job is a file in `queue/` whose FIRST line is a directive; running it removes
the job file. Directives:

```
@adb  <serial> <command...>   run shell command on the device
@send <serial> <local> <remote>   push local X58 file -> device path
@fetch <serial> <remote> <local>  pull device file -> local X58 path
(default)                       run the file as a bash script on the X58
```

Result: `<job>.out` = output, `<job>.exit` = exit status, both in `results/`.

## Primitive commands (X58 CLI)
`~/t337t/eanhost/ean` (add to PATH if desired). For send/fetch/cmd/run, watch.sh
must be running on the X58 as user `ean`:

```bash
nohup bash ~/t337t/eanhost/watch.sh >/tmp/eanhost-daemon.log 2>&1 &

ean send  /tmp/payload.bin /sdcard/EanHost/files/payload.bin
ean fetch /sdcard/EanHost/files/payload.bin /tmp/back.bin
ean run   /tmp/somescript.sh          # script executes ON THE X58
ean cmd   "ls -l /sdcard/EanHost/files"
```

`ean` pushes a job file into the queue, then polls `results/` up to EAN_TIMEOUT.
Other serials (e.g. the A10e, RF8MB2B74QR): `EAN_DEV=RF8MB2B74QR ean cmd "id"`
(NULL/`@adb` carries the serial, so a single watcher serves every device).

**Run exactly ONE watch.sh** (default serial). The queue dir is the SAME storage
as seen from every device, so a second watcher just races the same files.
Start it after the X58 boots:
```bash
nohup bash ~/t337t/eanhost/watch.sh >/tmp/eanhost-daemon.log 2>&1 &
```

## Runbook (X58-side control plane)
Everything on the X58 side is conventional bash daemons; bring them all back
after an X58 reboot with one idempotent command:

```bash
bash ~/t337t/eanhost/eanhost-up.sh
```

It ensures one instance each of: `watch.sh` (queue), and per device
`twatch` + `swatch` (termux-agent watchdog + supervisor watchdog):

```
watch.sh                         queue daemon (EAN_DEV=enqueuing device)
ean twatch  [serial]             revive the termux agent every 30s
ean swatch  [serial]             restart the device supervisor if dead
```

Each daemon singles itself out with a host pidfile
(`/tmp/eanhost-<role>-<serial>.pid`) + `kill -0`; a duplicate start exits
immediately instead of running concurrently. Logs: `/tmp/eanhost-daemon.log`,
`-twatch.log`, `-swatch.log`, `-twatch-a10e.log`, `-swatch-a10e.log`.

## Reboot self-heal (VERIFIED, no human on the tablet)
- ADB authorization persists across reboot (keys at `/home/ean/.android`, backup
  `/home/ean/.android-backup/`).
- Supervisor: `swatch` sees the stale `sup.pid` and calls `sstart`; the guarded
  `sstart.sh` checks `kill -0` first, so duplicate/racing starts can't stack
  supervisors.
- Termux agent: `twatch` auto-revives; `tstart` includes keyguard dismissal
  (`input keyevent 224` + swipe) so it works the instant the screen is locked
  post-boot.
- Clean-reboot run: pre-cleared markers, tablet rebooted; ~60s later the
  supervisor respawned (pid 5746) AND a fresh Termux agent (pid 6018) rose on
  its own; freshly-queued unique tasks (supervisor + termux) completed exit 0.
- Watch out: after reboot, `agent.heart`/`sup.pid` from before the reboot look
  "fresh" for up to 60s; that and stale `results/*` can fake a health check.
  Clear markers (or use unique task names) when testing reboot recovery.

## Device supervisor (bots that survive X58 shutdown)
A daemon runs ON THE DEVICE (detached from adb, reparented to init). It polls
`tasks/` every 2s, runs each `*.sh` as `sh`, writes `results/<task>.out` (and
`.exit`), moves the task to `done/`. Once running it needs NOTHING on the X58:
the server can power off mid-task and the task still finishes (verified: a 30s
task ran to completion across `adb kill-server` + restart; auth survived both a
kill-server and a tablet reboot because keys now persist).

```bash
ean psup status              # is the supervisor alive?
ean psup start               # (idempotent enough; start it after tablet reboot)
ean psup stop

ean bot  /path/to/task.sh [name]   # queue one task on the device; returns immediately
ean wait taskname                  # poll results/<taskname>.out + .exit
```

`ean wait` (and `ean send/fetch/cmd/run`) poll for `results/<name>.exit`,
i.e. true completion -- `<name>.out` is created empty at task START, so polling
on .out alone returns before a long task finishes. Task script runs as uid 2000
(shell) with sdcard_rw; it may write anywhere in `/sdcard/EanHost`. Device reboot
stops the supervisor (no autostart yet; run `ean psup start`).

## Talking to the X58 (an X58-issued command executes on the X58)
```bash
ean run - <<'EOF'
echo "host says $(hostname)"
EOF
```

## On-device (Termux) side: the driving direction
Termux cannot be started headless: `com.termux/.app.RunCommandService` exists but
enforces `com.termux.permission.RUN_COMMAND`, which ADB (uid 2000) is denied;
`run-as` is broken ("package unknown"); no `su`. So commands are STARTED by the
X58 (watcher + `ean`), executing either on the X58 or on the device via
`adb shell` (`@adb`). A Termux-side writer works when a human runs it; the
transport tolerates both origins.

## Termux agent (fully headless executor + self-heal)
A Termux-UID daemon (`termux/agent.sh`, runs as u0_a159 with Termux env,
`PREFIX=/data/data/com.termux/files/usr`) processes `termux/inbox/*.sh`, writes
`termux/outbox/<task>.out` + `.exit`, moves the task to `termux/done/`. It is
launched WITHOUT a human:

1. `adb shell am start -n com.termux/.app.TermuxActivity` (wake + focus)
2. `input tap 400 400` (put focus on the terminal)
3. `input text` a **plain, typeable command** `sh /sdcard/EanHost/termux/agentonce.sh`
   + `input keyevent 66`. PITFALL: `input text` cannot type `( ) & > !` etc. --
   the launcher MUST be pushed as a file (`agentonce.sh`); the injected line is
   only `sh .../agentonce.sh` (alnum, `/`, `.`, spaces -> `%s`).
4. `agentonce.sh` runs `( sh agent.sh > launch.log 2>&1 & )` -- detaches and
   reparents to init, so it survives Termination of TermuxActivity.

Liveness is HEARTBEAT only: `agent.heart` = `iter pid epoch` written every 2s;
fresh iff `(device date +%s) - epoch < 60s`. Shell `kill -0` CANNOT probe a
Termux-UID process (adb uid 2000, EPERM); matching by pid in `ps` also lies.

```bash
ean tstatus                 # ALIVE iff heart age < 60s
ean tstart                  # idempotent launch (wake, focus, inject, wait 15s)
ean tbot task.sh [name]     # queue a task for the agent (inbox/)
ean twait taskname          # poll termux/outbox/<task>.out + .exit
ean twatch                  # watchdog loop: every 30s, tstart if stale. Run THIS on the X58:
  #   nohup bash -c "exec ~/t337t/eanhost/ean twatch >> /tmp/eanhost-twatch.log 2>&1" &
```

Things that are VERIFIED:
- Tasks run while Termux stays in the BACKGROUND (UI on Home) -- headless.
- `am force-stop com.termux` (or app kill) freezes the heart; the next twatch
  cycle sees it stale and relaunches a NEW agent; queued tasks ride through.
- Agent self-singleton via `mkdir termux/agent.lock` (pid inside): overlapping
  `tstart`/twatch injections can't produce two live agents.
- Reboot: on cold boot the agent is gone until twatch (or a manual `tstart`)
  notices; the stale lock (dead pid) is reaped by the lock keep-alive check.

## Constraints / gotchas
- Two devices share the USB: every adb call must use `-s <serial>`.
- Android 5.1 toybox lacks `wc`, `pidof`, `tail`: use `grep -c ""` for counting,
  `ps | grep` for pids, `grep ""`/`head -n -1` instead of tail.
- `@adb` commands are passed as a single shell string (quotes not preserved);
  use `ean run` with a script for anything quoting-sensitive.
- Multi-bot / file-mgmt feasibility verified: push/pull md5-identical, device
  `cp/mv` fine, 3 concurrent device writers overlap cleanly, queue handles
  bursts.
- **ONE supervisor per device.** Two supervisors racing `tasks/` corrupt each
  other's bookkeeping (ran the same task twice, empty names, failed mv). Never
  `sstart` again while one is alive; `sstart.sh` now guards itself
  (`kill -0` on `sup.pid` before spawning), so `ean psup start` / `swatch`
  cannot stack supervisors anymore. Same discipline via pidfiles:
  one `watch.sh`, one `twatch`+`swatch` per device.
- **Never `adb kill-server` on the X58.** It regenerates the keypair and both
  devices drop to `unauthorized` (recovery = tap "Allow USB debugging" again on
  each tablet). Keys now live at `/home/ean/.android/` and are backed up to
  `/home/ean/.android-backup/`; a restart reuses them (verified: survived
  kill-server AND reboot). If keys are ever lost, restore from the backup.
- Security: the watcher executes queue files as user `ean` on the X58, and the
  supervisor executes task files as shell uid on the device. Anything that can
  write to `queue/` or `tasks/` on the LAN device is remote-code. Intended, but
  only trust the LAN.
## Web panel (EanHost Control)
- URL: `http://<x58-lan-ip>:8443` (typical http://192.168.1.150:8443), HTTP from LAN.
- Single admin password: sha256 in `panel/config.json`. Change it:
  `python3 /home/ean/t337t/eanhost/panel/panel.py --set-pass '<new>'`
  (writes config.json atomically; no restart needed - read per request).
- Start/stop (also wired into `eanhost-up.sh` at the end, so it comes back on
  X58 reboot via the existing cron @reboot line):
  `bash /home/ean/t337t/eanhost/panel/start.sh`  (idempotent, single instance)
  `pkill -f 'panel.py --port 8443'`
- Runs as user `ean`, stdlib only (http.server + adb), binds 0.0.0.0:8443.
  Adb server must be running for the panel (it shells out `adb -s <serial>`).
- Features: dashboard (battery/disk/sup/agent/uptime per node), per-node tabs
  Files / Console / Tasks / Logs. Files root is `/sdcard` (pull/push via adb).
  Console = SSE stream (`/api/node/<serial>/cmdstream?cmd=..&t=<token>`), adb
  shell, exit code via `__EAN_EXIT__`. Tasks = supervisor (tasks/) or termux
  (inbox/) executors, polls for output. Logs = svc.log / agent.log / heart /
  launch.log.
- Auth: login returns token; cookie `ean_sess` (non-HttpOnly so JS/EventSource
  can use it), token also accepted as `?t=` for SSE. Server sessions in memory
  (cleared on panel restart), 7-day expiry.
- Firewall: X58 nftables only allows 22/80/2022/8080 from LAN. 8443 was opened
  via: `nft insert rule inet host_filter input ip saddr 192.168.1.0/24 tcp dport 8443 accept`
  and persisted in `/etc/nftables.conf` (root). If it gets dropped again, re-add.
- A10e queue note: the shared `watch.sh` only polls the T337T queue. A10e
  cmd/send/fetch need `EAN_DEV=RF8MB2B74QR` (bot/tbot) or a second watch.sh.
  The panel talks to either device directly over adb, so no such limitation.
