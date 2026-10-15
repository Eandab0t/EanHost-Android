#!/data/data/com.termux/files/usr/bin/python
# runner.py — keeps every bot in bots/ alive: spawns / watches / respawns each
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
#
# Fast Mode (opt-in via runner.json, see below) adds:
#   * a TCP command socket so the panel no longer spawns `adb` per action;
#   * an event that wakes the loop the instant a command arrives (no waiting
#     for CHECK_T), backed by an inotify/timestamp watch on bots.ctl;
#   * optional RAM-backed coordination files (bots.ctl / bots.stopped) so the
#     hot path stops wearing the tablet's flash.
# Everything degrades to the original file/adb behaviour when Fast Mode is
# off, so upgrading the runner is safe before the panel is switched over.
import json
import os
import shlex
import socket
import subprocess
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "bots.json")
RCFG = os.path.join(BASE, "runner.json")
CHECK_T = 10
CRASH_BACKOFF = 60   # wait this long before re-spawning a bot that crashed <20s after start
TAIL_BYTES = 256 * 1024


def load_runner_cfg():
    try:
        with open(RCFG, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


_RC = load_runner_cfg()
FAST = bool(_RC.get("fast"))
TCP_BIND = str(_RC.get("tcp_bind") or "127.0.0.1")
TCP_PORT = int(_RC.get("tcp_port") or 19000)
RAM_DIR = _RC.get("ram_dir") or None
INOTIFY = _RC.get("inotify", True)

# Coordination files. In Fast Mode bots.ctl / bots.stopped can live on a
# RAM-backed dir; bots.status is always written to BASE for the legacy panel.
_ram_ok = False
if RAM_DIR:
    try:
        os.makedirs(RAM_DIR, exist_ok=True)
        _ram_ok = True
    except Exception:
        _ram_ok = False

CTL = os.path.join(RAM_DIR, "bots.ctl") if _ram_ok else os.path.join(BASE, "bots.ctl")
STOPF = os.path.join(RAM_DIR, "bots.stopped") if _ram_ok else os.path.join(BASE, "bots.stopped")
STATUS = os.path.join(BASE, "bots.status")
STATUS_RAM = os.path.join(RAM_DIR, "bots.status") if _ram_ok else None

WAKE = threading.Event()
CTL_LOCK = threading.Lock()
LATEST = {"rows": [], "detail": {}, "bots": {}, "ts": 0}
LATEST_LOCK = threading.Lock()
_LAST_CFG = None


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
    # A truncated or mid-write bots.json must never be read as "no bots":
    # main() kills every bot that is not in cfg, so a transient parse error
    # would take the whole fleet down. Keep the last good config instead.
    global _LAST_CFG
    try:
        with open(CFG, encoding="utf-8") as f:
            bots = json.load(f).get("bots") or []
        cfg = {b.get("name", ""): b for b in bots if b.get("name")}
        _LAST_CFG = cfg
        return cfg
    except Exception:
        return dict(_LAST_CFG) if _LAST_CFG is not None else {}


def _atomic_write(path, text):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        pass


def write_status(rows):
    body = "\n".join(rows) + "\n"
    _atomic_write(STATUS, body)
    if STATUS_RAM:
        _atomic_write(STATUS_RAM, body)


def read_stopped():
    try:
        with open(STOPF, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def write_stopped(names):
    try:
        body = "".join(n + "\n" for n in sorted(names))
        tmp = STOPF + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, STOPF)
    except OSError:
        pass


def enqueue_ctl(action, name):
    """Append a control command and wake the loop immediately."""
    with CTL_LOCK:
        try:
            with open(CTL, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (action, name))
        except OSError:
            return False
    WAKE.set()
    return True


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
    with CTL_LOCK:
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


def tail_file(path, since=None, max_bytes=TAIL_BYTES):
    """Return (content, size, offset, reset) for a log file.

    ``since`` absent -> return the tail (last ``max_bytes``) so a first read
    is never empty. ``since`` out of range (file cleared/truncated) -> return
    the tail again but flag ``reset`` so the client full-replaces.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0, 0, False
    reset = False
    if since is None:
        offset = max(0, size - max_bytes)
    elif isinstance(since, int) and 0 <= since <= size:
        offset = since
    else:
        offset = max(0, size - max_bytes)   # cannot honour: re-send tail
        reset = True
    if size - offset > max_bytes:            # too far behind: cap, flag reset
        offset = size - max_bytes
        reset = True
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return "", size, offset, reset
    return data.decode("utf-8", errors="replace"), size, offset, reset


def _watch_ctl():
    """Wake the loop when bots.ctl is touched by anything outside our socket.

    Uses inotify when available (via ctypes); otherwise falls back to a light
    timestamp poll. Either way the runner stops waiting the full CHECK_T.
    """
    last = None
    while True:
        try:
            m = os.stat(CTL).st_mtime_ns
        except OSError:
            m = None
        if last is not None and m != last:
            WAKE.set()
        last = m
        time.sleep(0.5 if not INOTIFY else 0.5)


# ---- Fast Mode TCP server ------------------------------------------------

def _cmd_status():
    with LATEST_LOCK:
        snap = dict(LATEST)
    return {"ok": True, "ts": snap.get("ts", 0), "rows": snap.get("rows", []),
            "bots": snap.get("bots", {}), "bots_detail": snap.get("detail", {})}


def _cmd_log(req):
    name = str(req.get("name") or "")
    if not name or not all(c.isalnum() or c in "._-" for c in name):
        return {"ok": False, "error": "bad name"}
    since = req.get("since")
    if not isinstance(since, int):
        since = None
    maxb = req.get("tail")
    if not isinstance(maxb, int) or maxb <= 0 or maxb > TAIL_BYTES:
        maxb = TAIL_BYTES
    content, size, _start, reset = tail_file(
        os.path.join(BASE, "run-%s.log" % name), since=since, max_bytes=maxb)
    # ``offset`` is the resume position the client passes back as ``since``:
    # it is the end of the returned content, i.e. the current file size. The
    # client therefore only ever receives bytes it has not seen yet.
    return {"ok": True, "content": content, "size": size,
            "offset": size, "reset": reset}


def _cmd_global(req):
    action = str(req.get("action") or "").lower()
    with LATEST_LOCK:
        names = list(LATEST.get("detail", {}).keys())
    cfg = load_config()
    names = names or list(cfg.keys())
    if action == "clear_logs":
        cleared = 0
        for name in names:
            try:
                open(os.path.join(BASE, "run-%s.log" % name), "w").close()
                cleared += 1
            except OSError:
                pass
        return {"ok": True, "cleared": cleared}
    if action in ("start_all", "stop_all"):
        verb = "start" if action == "start_all" else "stop"
        with LATEST_LOCK:
            detail = dict(LATEST.get("detail", {}))
        sent = 0
        for name in names:
            state = (detail.get(name) or {}).get("state")
            if verb == "stop" and state == "STP":
                continue
            if verb == "start" and state == "UP":
                continue
            if enqueue_ctl(verb, name):
                sent += 1
        return {"ok": True, "sent": sent}
    if action == "update_all":
        updated, errors = [], []
        for name in names:
            d = os.path.join(BASE, cfg.get(name, {}).get("dir", ""))
            if not os.path.isdir(os.path.join(d, ".git")):
                continue
            try:
                r = subprocess.run(["git", "-C", d, "pull", "--ff-only"],
                                   capture_output=True, timeout=120)
                if r.returncode == 0:
                    updated.append(name)
                else:
                    errors.append(name)
            except Exception:
                errors.append(name)
        return {"ok": True, "updated": updated, "errors": errors}
    return {"ok": False, "error": "unknown action"}


def sync_bot(name):
    """Deploy/refresh one bot from its git remote.

    First run (no .git) initialises the repo from the bot's ``repo`` URL;
    afterwards it fetches and hard-resets to the remote HEAD. Runs on the
    device so the panel only receives the result.
    """
    b = load_config().get(name)
    if not b:
        return {"ok": False, "error": "unknown bot"}
    d = os.path.join(BASE, b.get("dir") or "")
    repo = (b.get("repo") or "").strip()
    log = []
    if not os.path.isdir(d):
        if not repo:
            return {"ok": False, "error": "bot dir missing", "log": log}
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": "cannot create dir: %s" % e, "log": log}

    def run(args, timeout=180):
        try:
            r = subprocess.run(["git", "-C", d] + args, capture_output=True,
                               timeout=timeout)
        except FileNotFoundError:
            return 127, "git not installed"
        except Exception as e:
            return 1, str(e)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        log.append("git %s -> %s" % (" ".join(args), r.returncode))
        return r.returncode, out

    if not os.path.isdir(os.path.join(d, ".git")):
        if not repo:
            return {"ok": False, "error": "not a repo and no 'repo' url set"}
        rc, o = run(["init"])
        if rc != 0:
            return {"ok": False, "error": o, "log": log}
        run(["remote", "add", "origin", repo])
    if repo:
        run(["remote", "set-url", "origin", repo])
    rc, o = run(["fetch", "--prune", "origin"])
    if rc != 0:
        return {"ok": False, "error": o, "log": log}
    rc, o = run(["reset", "--hard", "FETCH_HEAD"])
    if rc != 0:
        return {"ok": False, "error": o, "log": log}
    _, rev = run(["rev-parse", "--short", "HEAD"])
    return {"ok": True, "name": name, "rev": rev, "log": log}


def _handle_conn(conn):
    try:
        conn.settimeout(30)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 1024 * 1024:
                return
        req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
        cmd = str(req.get("cmd") or "")
        if cmd == "ping":
            res = {"ok": True, "pong": True, "ts": int(time.time())}
        elif cmd == "status":
            res = _cmd_status()
        elif cmd == "log":
            res = _cmd_log(req)
        elif cmd == "bot":
            action = str(req.get("action") or "").lower()
            name = str(req.get("name") or "")
            valid = bool(name) and all(c.isalnum() or c in "._-" for c in name)
            if action in ("start", "stop", "restart") and valid:
                res = {"ok": enqueue_ctl(action, name)}
            elif action == "sync" and valid:
                res = sync_bot(name)
            else:
                res = {"ok": False, "error": "bad bot"}
        elif cmd == "global":
            res = _cmd_global(req)
        else:
            res = {"ok": False, "error": "unknown"}
        conn.sendall(json.dumps(res).encode("utf-8") + b"\n")
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((TCP_BIND, TCP_PORT))
    except OSError as e:
        print("tcp bind failed %s:%s: %s" % (TCP_BIND, TCP_PORT, e))
        return
    srv.listen(16)
    print("runner tcp on %s:%s" % (TCP_BIND, TCP_PORT))
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=_handle_conn, args=(conn,), daemon=True).start()


def main():
    procs = {}
    started = {}
    if FAST:
        threading.Thread(target=_serve, daemon=True).start()
        if INOTIFY:
            threading.Thread(target=_watch_ctl, daemon=True).start()
        WAKE.set()
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
        detail = {}
        for name, b in cfg.items():
            p = procs.get(name)
            d = os.path.join(BASE, b.get("dir", ""))
            if name in stopped:
                rows.append("%s STP" % name)
                detail[name] = {"state": "STP", "pid": None}
            elif p is not None and p.poll() is None:
                rows.append("%s UP pid=%s" % (name, p.pid))
                detail[name] = {"state": "UP", "pid": str(p.pid)}
            elif os.path.isdir(d):
                rows.append("%s DOWN" % name)
                detail[name] = {"state": "DOWN", "pid": None}
            else:
                rows.append("%s MISSING dir=%s" % (name, b.get("dir", "")))
                detail[name] = {"state": "MISSING", "pid": None}
        write_status(rows)
        with LATEST_LOCK:
            LATEST["rows"] = rows
            LATEST["detail"] = detail
            LATEST["bots"] = {n: v["state"] for n, v in detail.items()}
            LATEST["ts"] = int(now)
        try:
            WAKE.wait(CHECK_T)
            WAKE.clear()
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
