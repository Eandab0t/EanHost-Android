# EanHost Android

<p align="center">
  <strong>Give a fleet of Android tablets one reliable control plane.</strong><br>
  Provision devices, run durable jobs, manage bots, and monitor everything from a browser.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-00b4d8.svg"></a>
  <img alt="Host: Linux" src="https://img.shields.io/badge/host-Linux-222633.svg">
  <img alt="Python: 3" src="https://img.shields.io/badge/Python-3-222633.svg">
  <img alt="Dependencies: Python standard library" src="https://img.shields.io/badge/panel-stdlib%20only-222633.svg">
</p>

EanHost turns Android tablets running Termux into a supervised, remotely managed
bot and task fleet. A Linux host provides the web panel and talks to devices over
ADB; on each tablet, lightweight supervisors keep jobs and bots running through
disconnects and restarts.

> **Built for real devices:** the shell payload avoids relying on newer Android
> command-line tools, and the control plane can fall back to its original ADB
> workflow whenever Fast Mode is unavailable.

## At a glance

| | |
|---|---|
| **Control plane** | Browser dashboard for device health, files, console, tasks, logs, bot management, and fleet-wide actions |
| **Device workers** | Android shell supervisor for durable device-lane jobs; Termux agent for Python, npm, and other Termux commands |
| **Bot runner** | Keeps projects under `termux/bots/<name>/` running, restarts crashed processes, and supports start/stop/restart controls |
| **Fast Mode** | Optional persistent TCP connection for quicker status, bot controls, and incremental logs—with automatic ADB/file fallback |
| **Dependencies** | Python standard library on the host panel; `adb` and Bash are also required |

## How it fits together

```text
 Browser
    │
    ▼
 Linux host ── EanHost panel + eanwatch ── ADB ── Android tablet
                                                   ├─ device supervisor
                                                   ├─ Termux agent
                                                   └─ Termux bot runner
```

The panel gives you one place to work across devices. Jobs are queued into one
of two lanes: the **device lane** runs with Android's shell UID, while the
**Termux lane** runs as the Termux user and can use its installed runtime and
packages. The on-device workers continue processing already-queued work if the
host disconnects.

## Get started

### Requirements

- **Host:** Linux, Python 3, Bash, and Android platform-tools (`adb`).
- **Each tablet:** USB debugging enabled and authorized for the host. Install
  and open Termux, then install its basic tools with `pkg install python bash`.
- Keep the host and devices on a **trusted network**; the panel uses HTTP.

### Install and onboard

```bash
git clone https://github.com/Eandab0t/EanHost-Android.git
cd EanHost-Android

# Connect each tablet and accept its Android debugging authorization prompt.
adb devices

# Provision a device, push its worker scripts, and register it with the panel.
./provision-node.sh <adb-serial> <friendly-name>

# Set the panel administrator password.
python3 panel/panel.py --set-pass '<your-password>'

# Start ADB, the web panel, and the fleet keep-alive loop.
bash eanhost-up.sh
```

Open `http://<host-ip>:8080` in a browser and sign in with the password you set.
Provisioning is safe to repeat; it updates the node's display name and
re-establishes its on-device services.

To make the host control plane recover after a reboot or process failure, add
these entries to the host user's crontab, replacing the path with your clone's
absolute path:

```cron
@reboot bash /path/to/EanHost-Android/eanhost-up.sh
* * * * * bash /path/to/EanHost-Android/host-watchdog.sh
```

`eanhost-up.sh` and the watchdog are idempotent. The first starts the panel and
device keep-alive loop; the second periodically brings them back if needed.

## What you can do

- **Monitor the fleet:** see online state, battery, worker/bot status, and
  connection latency; run supported actions across online nodes.
- **Manage files and commands:** browse device storage, use the console, and
  queue work in the device or Termux task lane.
- **Watch logs:** inspect service and bot output. In Fast Mode, logs are fetched
  incrementally instead of replacing the whole log on every poll.
- **Deploy bots from the browser:** upload a bot ZIP, install supported Python
  or npm dependencies on the device, and start or update the bot.
- **Recover automatically:** host and device watchdogs revive their workers;
  the Termux agent is monitored by heartbeat rather than a cross-user process
  check.

### EZ install

Upload a ZIP containing a supported bot project from the panel's install view.
The project is placed under `termux/bots/<name>/`; the runner starts it and
restarts it if it exits unexpectedly. Python requirements and npm package
dependencies can be installed on-device when supplied by the project.

Re-uploading a bot with the same name updates its code and queues a restart,
while preserving the bot's existing `.env`. A new bot can use the `.env` from
the ZIP or the install form's token input. Secrets remain in the individual
bot's `.env`, not in `bots.json`; the panel file browser does not display
`.env` files.

### Fast Mode (optional)

Fast Mode replaces repeated ADB calls for supported panel operations with a
persistent, loopback TCP connection to the Termux bot runner. It can make status
checks and bot controls more responsive, wake the runner immediately for
commands, and stream log changes incrementally. Optional RAM-backed
coordination files reduce writes to device storage.

Fast Mode is **off by default**. It requires matching host and device settings
and uses ADB port forwarding; if the TCP path fails, supported operations fall
back to the existing ADB/file workflow. See the
[Fast Mode migration and configuration guide](docs/FAST_MODE.md) before
enabling it.

## Durable task contract

A job is a uniquely named `*.sh` file placed in a lane's inbox. Its worker
writes `<name>.out` and then `<name>.exit` to signal completion, and archives
the input in `done/`.

| Lane | Inbox | Runs as | Use it for |
|---|---|---|---|
| **Device** | `/sdcard/EanHost/tasks/` | Android shell UID | Shell scripts and shared-storage tasks |
| **Termux** | `/sdcard/EanHost/termux/inbox/` | Termux UID | Python, npm, and Termux-installed tools |

Execution is **at least once** across crashes, so make jobs safe to retry and
use unique names. In normal operation, the completion marker prevents a
finished job from being treated as pending. The full job/result format and
failure semantics are documented in the
[queue contract](docs/QUEUE-CONTRACT.md).

## Repository map

```text
EanHost-Android/
├── ean                    # Host CLI for device and Termux operations
├── eanhost-up.sh           # Idempotent control-plane startup
├── eanwatch.sh             # Keeps the panel and device workers alive
├── host-watchdog.sh        # Cron safety net and log maintenance
├── provision-node.sh       # Onboard a tablet and register it
├── nodes.py                # Read the configured node list
├── panel/
│   ├── panel.py            # Python HTTP panel, APIs, ADB and Fast Mode
│   ├── start.sh            # Idempotent panel launcher
│   └── static/index.html   # Responsive dashboard and interface
├── device-payload/
│   ├── svc/                # On-device task supervisor
│   └── termux/             # Termux task agent and launcher
├── termux-bots/
│   ├── runner.py           # Bot supervisor and optional TCP server
│   ├── runner.json         # Optional device-side Fast Mode settings
│   └── start-bots.sh       # Runner launcher
├── tests/contract/         # Queue-contract conformance harness
└── docs/
    ├── NOTES.md            # Protocol details, runbook, and troubleshooting
    ├── QUEUE-CONTRACT.md   # Durable queue specification
    └── FAST_MODE.md        # Fast Mode setup, rollout, and rollback
```

## Configuration and security

- Set the administrator password with
  `python3 panel/panel.py --set-pass '<your-password>'`. The panel stores its
  SHA-256 hash in `panel/config.json`; keep that file private and do not commit
  real credentials or deployment-specific node data.
- The panel listens on `0.0.0.0:8080` by default and serves **HTTP, not HTTPS**.
  Restrict access to a trusted LAN or put it behind an appropriately secured
  reverse proxy; do not expose it directly to the public internet.
- Treat both task inboxes as code-execution interfaces: anyone who can write
  there can run commands with that lane's privileges. See the
  [queue contract security notes](docs/QUEUE-CONTRACT.md).
- Panel sessions are held in memory and expire after seven days; restarting
  the panel clears them. ADB must be available on the host.

## Documentation

- [Runbook and protocol notes](docs/NOTES.md) — device layout, CLI, reboot
  recovery, panel usage, and troubleshooting.
- [Queue contract](docs/QUEUE-CONTRACT.md) — job/result schema, lanes,
  completion semantics, and conformance tests.
- [Fast Mode guide](docs/FAST_MODE.md) — configuration, rollout, verification,
  fallback, and rollback.

## License

[MIT](LICENSE)
