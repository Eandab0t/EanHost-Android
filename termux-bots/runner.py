#!/data/data/com.termux/files/usr/bin/python
# runner.py - keeps every bot in bots/ alive: spawns / watches / respawns each
# entry (dir=<bot project folder under bots/>, cwd = that folder) and writes
# bots.status every CHECK_T seconds for the panel to read. No discord import:
# each bot is its own process and keeps its own token in its own .env.
#
# Manual controls via bots.ctl (one line per command, consumed each poll):
#   stop NAME    -> mark stopped (bots.stopped), kill the bot
#   start NAME   -> unmark stopped, bot respawns
#   restart NAME -> kill the bot now; runner respawns immediately
#
# Each bot's pid is kept in run-<name>.pid so the runner can adopt a bot that
# survived a runner restart (avoids spawning a duplicate).
import json
import os
import shlex
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "bots.json")
STATUS = os.path.join(BASE, "bots.status")
CTL = os.path.join(BASE, "bots.ctl")
STOPF = os.path.join(BASE, "bots.stopped")
CHECK_T = 10
CRASH_BACKOFF = 60   # wait this long before re-spawning a bot that crashed <20s after start


class Fake(object):
    """Minimal Popen stand-in for an adopted (already-running) bot process."""
    __slots__ = ("pid",)

    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            return self.pid

    def terminate(self):
        try:
            os.kill(self.pid, 15)
        except OSError:
            pass


def load_config():
    try:
        with open(CFG, encoding="utf-8") as f:
            bots = json.load(f).get("bots") or []
    except Exception:
        bots = []
    return {b.get("name", ""): b for b in bots if b.get("name")}


def write_status(rows):
    try:
        with open(STATUS, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    except Exception:
        pass


def read_stopped():
    try:
        with open(STOPF, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def write_stopped(names):
    try:
        with open(STOPF, "w", encoding="utf-8") as f:
            f.write("".join(n + "\n" for n in sorted(names)))
    except OSError:
        pass


def pidfile(name):
    return os.path.join(BASE, "run-%s.pid" % name)


def read_pid(name):
    try:
        with open(pidfile(name), encoding="utf-8") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def rm_pidfile(name):
    try:
        os.unlink(pidfile(name))
    except OSError:
        pass


def kill_if_running(procs, started, name):
    p = procs.get(name)
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except OSError:
            pass
    procs.pop(name, None)
    started.pop(name, None)
    rm_pidfile(name)


def apply_ctl(procs, started, stopped):
    try:
        with open(CTL, encoding="utf-8") as f:
            lines = [l.split() for l in f if l.strip()]
    except OSError:
        return
    try:
        open(CTL, "w").close()
    except OSError:
        pass
    for parts in lines:
        if len(parts) < 2:
            continue
        action, name = parts[0].lower(), parts[1]
        if action == "stop":
            stopped.add(name)
        else:                       # start / restart
            stopped.discard(name)
        write_stopped(stopped)
        kill_if_running(procs, started, name)


def main():
    procs = {}
    started = {}
    while True:
        cfg = load_config()
        now = time.time()
        stopped = read_stopped()
        apply_ctl(procs, started, stopped)
        for name in list(procs):
            if name not in cfg:
                kill_if_running(procs, started, name)
        for name, b in cfg.items():
            if name in stopped:
                kill_if_running(procs, started, name)
                continue
            d = os.path.join(BASE, b.get("dir", ""))
            cmd = shlex.split(b.get("cmd") or "python bot.py")
            p = procs.get(name)
            if p is not None and p.poll() is None:
                continue
            if p is not None and now - started.get(name, 0) < CRASH_BACKOFF:
                continue
            if p is None:
                adopted = read_pid(name)
                if adopted is not None and alive(adopted):
                    procs[name] = Fake(adopted)
                    continue
            if not os.path.isdir(d):
                continue
            log = open(os.path.join(BASE, "run-%s.log" % name), "a")
            log.write("== start %d %s\n" % (int(now), " ".join(cmd)))
            log.flush()
            procs[name] = subprocess.Popen(
                cmd, cwd=d, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT)
            started[name] = now
            try:
                with open(pidfile(name), "w", encoding="utf-8") as f:
                    f.write(str(procs[name].pid))
            except Exception:
                pass
        rows = []
        for name, b in cfg.items():
            p = procs.get(name)
            d = os.path.join(BASE, b.get("dir", ""))
            if name in stopped:
                rows.append("%s STP" % name)
            elif p is not None and p.poll() is None:
                rows.append("%s UP pid=%s" % (name, p.pid))
            elif os.path.isdir(d):
                rows.append("%s DOWN" % name)
            else:
                rows.append("%s MISSING dir=%s" % (name, b.get("dir", "")))
        write_status(rows)
        try:
            time.sleep(CHECK_T)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()