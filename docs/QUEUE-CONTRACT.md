# EanHost Durable Queue Contract - v1

One protocol for "submit a unit of work to a tablet / get a result back / know
its status", so that everything (panel, `ean`, watch loops, bots) builds on a
single shape instead of several half-sibling shapes. This is a SPEC: it pins the
decisions the earlier sessions left open. Verified live results appended at the
bottom (§Verification).

## 1. The one abstraction

A **job** is a single shell script file, and a **result** is two files. A job is
dropped into a lane's **inbox** on the shared tablet storage; the lane's worker
executes it, publishes results, and **displaces the input so it is never
executed twice**. That is the entire contract.

Shared storage per tablet: `/sdcard/EanHost` (single sdcardfs tree with the same
view from `adb shell` uid 2000, Termux uid, and the host over adb).

## 2. Lanes

A lane IS the answer to "does the job target the tablet or the host": the lane
decides the **executor**, hence the interpreter, hence the job's powers.

| lane | inbox (read/write) | executor | interpreter | result dir | archive |
|------|--------------------|----------|-------------|-----------|---------|
| `device` | `tasks/` | on-device supervisor (uid 2000/shell) | `/system/bin/sh` | `results/` | `done/` |
| `termux` | `termux/inbox/` | Termux agent (Termux uid) | Termux bash | `termux/outbox/` | `termux/done/` |

Job **target** = job **lane** = executor. There is no other target dimension.

## 3. Job shape

- Input file is named `<name>.sh`, where name is `[A-Za-z0-9._-]+`, **no
  whitespace**.
- Content: UTF-8 shell (shebang optional; the lane's interpreter is fixed).
- **Uniqueness is the writer's job**: name collision is undefined (last writer
  wins). Use `$(date +%s)`, a uuid, or a per-batch tag. Unique names are also
  what make "is this finished" unambiguous (a stale `results/<name>.exit` from
  an earlier run with the same name would fake completion).
- The **legacy `.cmd` naming** from the original test kit is deprecated: the
  kit's `job*.cmd` files are the same *content* shape, but the supported name is
  `*.sh`. (All lanes already execute a file regardless of extension; the
  contract standardizes on `.sh`.)

## 4. Result shape

- `results/<name>.out`  -> stdout+stderr, merged. Created **at run start**
  (may be empty while the job runs).
- `results/<name>.exit` -> integer exit status. Written **last**.
- **`<name>.exit` existing IS the completion signal.** NEVER treat `.out`
  presence as completion (it exists from the start).
- Result dir per lane: `results/` (device), `termux/outbox/` (termux).
- Writers only write inboxes; anyone on the LAN may read results.

## 5. Consume / archive semantics (the "done" decision)

Sequence, per job, per lane, in this exact order:
1. run the input -> capture output to unresolved `.out`
2. write `.exit` (completion)
3. displace the input; **archive** it to `done/<name>.sh`.

**Crash semantics - read this twice.** Displacement happens strictly AFTER
execution. If the worker dies between step 2 and step 3 (or mid-step 1), the
input is still in the inbox and is re-run when the worker returns. Therefore:

> Results are **at-least-once** under crashes, **exactly-once** in normal
> operation. Jobs MUST tolerate re-execution: be idempotent, or self-guard by
> touching a unique marker at start and skipping if the marker exists.

This is a contract requirement on the job author, not a bug in the workers.

**One worker per lane** - exactly one on-device supervisor (enforced by the
`sup.lock` mkdir-singleton in `svc/supervisor.sh`), one Termux agent (its own
`termux/agent.lock`). Two workers in one lane = duplicate execution; the
supervisor lock exists precisely to keep that impossible.

## 6. Status signals (queryable; no hand-polling of scripts)

- **Job completion:** `results/<name>.exit` exists (any lane).
- **Job progress:** `<name>.out` content / mtime.
- **Worker liveness:**
  - device supervisor: `sup.pid` = `SUP <pid> <epoch>`, plus the lock owner
    `sup.lock/pid`; both are same-uid, so `kill -0` is authoritative.
  - termux agent: `termux/agent.heart` = `iter pid epoch`; fresh iff
    `(device now) - epoch < 60`. `kill -0` is NOT usable cross-uid from adb
    (EPERM); the heartbeat is the authoritative probe.
- **Events:** `svc.log` (device lanes), `termux/agent.log` (termux)
  (*"run b=[...]"* before, *"done b=[...]"* after; bot up/down for `bots/`).

## 7. Long-lived work (the bots lane)

`bots/*.sh` is the same *shape* (script on shared storage) but a different
*disposition*: the device supervisor keeps ONE instance alive (pid in
`bots/id.<name>.pid`, log `bots/log.<name>`), respawns it on death, and kills it
when the file is deleted. It consumes no `.exit`/`.out` protocol; its status is
`bots/id.<name>.pid` + `kill -0` + `svc.log`. (Python/Discord bots run inside
Termux instead; see `termux/bots/`; adb uid 2000 cannot exec Termux binaries.)
Keep this separate from the run-once contract; it is not a queue.

## 8. Retention

No automatic GC. `results/`, `done/`, `termux/outbox/`, `termux/done/`
accumulate; the operator clears them (delete = `rm results/<name>.*` etc.).
Chosen deliberately over auto-GC to keep on-device workers tiny. Revisit if a
small tablet fills.

## 9. Mapping to existing systems

- Panel **Tasks** tab = lanes (mode `supervisor` -> `tasks/`, mode `termux` ->
  `termux/inbox/`); panel **Files/Console** = transport over adb, NOT the queue
  contract.
- `ean send/fetch/cmd/run/bot/tbot` = writers/readers of the same shapes
  (direct adb).
- `svc/supervisor.sh` and `termux/agent.sh` are the two reference workers for
  lanes `device` and `termux`.

## 10. Trust note

Anyone who can write an inbox is remote-code-execution as the lane's uid, on
the LAN. Intended; LAN-only.

---

## Verification (contract conformance, live hardware)

Harness: `tests/contract/contract-test.sh` (runs on the host, drives the
tablets over adb, unique per-run tag, self-cleaning).

Verified on the known devices (each: device-lane worker alive and
`sup.lock`-consistent, burst of 4 jobs completing with exit codes preserved,
stdout captured, progress signal present, archive `tasks/` -> `done/` correct,
exactly-once `run b=[name]` markers in `svc.log`, md5-identical file
round-trip, job-internal `cp`+`mv`, and the termux lane conforming
inbox -> outbox `.out`/`.exit` -> `termux/done/` with exit 0).

Contract clauses 1-10 hold on both review-known devices as written. No
production code was changed to pass; the harness only exercised the lanes as
deployed.