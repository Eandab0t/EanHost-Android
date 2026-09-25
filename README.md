# EanHost-Android - USB brain-farm for old Android tablets

Turn a handful of old Android tablets into a supervised compute cluster,
controlled from one Linux box over adb. Each tablet runs Termux and an on-device
supervisor; a single Python web panel (stdlib only) manages files, console,
tasks, logs, and ships whole bots to devices as ZIPs. A durable
queue/result contract means nothing runs twice and nothing is lost on crash.

This is the working codebase from a live deployment (a handful of semi-modern
Samsung tablets wired to a Debian host). The spec and runbook are in
[`docs/`](docs/).

## What problem this solves

- Old tablets are constant-power compute: no screen needed, they run Termux, and
  one USB hub + one Linux box controls them all.
- Bots must survive: host reboots, tablet reboots, adb restarts, power cuts.
- Deploying/administering bots should happen from a browser, not a terminal per
  device.

## The two components

| Piece | Where | What it does |
|-------|-------|--------------|
| **Web panel** (`panel/`) | The Linux host | `python3 panel.py --port 8443`. Zero deps (stdlib `http.server` + calls to `adb`). Dashboard per node, file browser, console, tasks, logs, and **EZ install** (upload a bot as ZIP -> pushed, deps installed, .env handled, supervised). |
| **Device payload** (`device-payload/`) | Every tablet | A `sh` **supervisor** (runs dropped tasks, keeps long-lived bots alive) and a **Termux agent** (Termux-uid executor for anything adb's shell uid can't do). Both are heartbeat/lock single instances. |
| **Runner** (`termux-bots/runner.py`) | Termux on a tablet | Keeps one bot project folder alive each (`bots/<name>/`), respawns crashes, writes `bots.status`, consumes `bots.ctl` for stop/start/restart. This is how panel EZ-install bots stay up. |

## Deploy on any server

The host needs only: `adb` (platform-tools), `python3`, `bash`. The tablets need
`adb` (USB debugging enabled) and **Termux** with basic dev tools
(`pkg install python bash` for the python route).

```bash
git clone https://github.com/Eandab0t/EanHost-Android.git && cd EanHost-Android

# 1. Authorize the tablets: accept the RSA dialog on each screen
adb devices

# 2. Onboard each tablet (scaffolds /sdcard/EanHost, pushes payload,
#    registers the node, starts daemons)
./provision-node.sh <adb-serial> <friendly-name>

# 3. Set the panel's admin password
python3 panel/panel.py --set-pass '<your-password>'

# 4. Start the control plane (panel + keep-alive loop); come back after reboots
bash eanhost-up.sh

# 5. Make it survive a reboot or a crash (no root needed)
crontab -e
#   @reboot bash /path/to/EanHost-Android/eanhost-up.sh
#   * * * * * bash /path/to/EanHost-Android/host-watchdog.sh

# 6. Open http://<host-ip>:8443, log in, and use the EZ install tab to ship a
#    bot as a ZIP
```

Details, options, and troubleshooting live in the docs:

- [`docs/NOTES.md`](docs/NOTES.md) - protocol + runbook (verified on live
  hardware): on-device layout, the `ean` CLI, reboot self-heal, the Termux
  agent, constraints, panel usage.
- [`docs/QUEUE-CONTRACT.md`](docs/QUEUE-CONTRACT.md) - the durable-queue spec
  (job/result shape, exactly-once semantics, lanes, liveness) and the
  conformance harness `tests/contract/contract-test.sh`.

## EZ install behavior

- Fresh bot name -> files land in `termux/bots/<name>/`, `.env` kept **from the
  zip**, or created from the token field, or a stub.
- Re-uploading an **existing** name = update: code replaced, live `.env` kept,
  `restart <name>` queued -> bot auto-restarts.
- `requirements.txt` / `package.json` deps are installed on-device via the
  Termux lane; the response reports `updated`, `files`, `env`, `restart_queued`,
  `deps_started`/`deps_exit`.
- Tokens never live in `bots.json`; each bot's `.env` stays inside its own
  folder, and the panel's file browser deliberately doesn't surface `.env`.

## The contract in one paragraph

A **job** is one `*.sh` file dropped into a lane's inbox on the device's shared
storage. A lane's worker runs it, writes `<name>.out` + `<name>.exit` **in that
order** (`.exit` existing = completion), then archives the input to `done/`.
Two lanes: **device** (`/sdcard/EanHost/tasks/`, executed by the on-device
supervisor) and **termux** (`/sdcard/EanHost/termux/inbox/`, executed by the
Termux agent which can run python, npm, etc.). Crash semantics are
**at-least-once** in execution, **exactly-once** in normal operation; jobs must
tolerate re-runs, and writers pick unique names.

## Layout at a glance

```
EanHost-Android/
├── ean                  # CLI: send/fetch/cmd/run/bot/wait/psup/tbot/twait/tstatus/tstart
├── eanwatch.sh          # one loop, all nodes: revive supervisors + agents, panel watchdog
├── eanhost-up.sh        # idempotent bring-up: adb server + panel + eanwatch
├── host-watchdog.sh     # cron safety net: re-runs eanhost-up.sh every minute, trims logs
├── provision-node.sh    # onboard a tablet
├── nodes.py             # parse config.json node list
├── panel/               # web panel: panel.py, start.sh, config.json, static/
├── termux-bots/         # runner.py + bots.json + start-bots.sh (on-device bot supervisor)
├── device-payload/      # what gets pushed to each tablet: svc/ (supervisor) + termux/ (agent)
├── tests/contract/      # contract conformance harness (drives the lanes over adb)
└── docs/                # NOTES.md (runbook), QUEUE-CONTRACT.md (spec)
```

## Config note (secrets)

`panel/config.json` holds a **sha256 of your admin password** plus the node
list. The version in this repo ships with the literal
`sha256_hex_of_your_admin_password` as a placeholder; set your own with
`python3 panel/panel.py --set-pass '<your-password>'`.

## Security posture

- Login -> session token (cookie `ean_sess`, 7-day in-memory sessions).
- Plan for LAN/trusted-network only; anyone who can write an inbox is RCE as
  that lane's uid (see [`docs/QUEUE-CONTRACT.md`](docs/QUEUE-CONTRACT.md)).
- The panel needs the `adb` server running on the host.

## Notes from live use

- Old Android toolboxes (Android 5.1-era) lack `wc`/`pidof`/`tail`; the payload
  scripts stick to `sh` builtins (`grep -c ""`, `ps | grep`, mkdir/cat/echo/
  kill/mv/sleep).
- The Termux agent is tracked by **heartbeat** (`agent.heart` fresh < 60s), not
  `kill -0`, because a shell uid can't see Termux-uid processes.
- Reboot self-heal verified: adb keys survive, supervisor + agent respawn, queued
  tasks complete.

## License

MIT, see [LICENSE](LICENSE).