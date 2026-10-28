#!/usr/bin/env python3
"""EanHost web panel - manage both tablets (files, console, tasks, logs) over adb."""
import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import sys
import subprocess
import io
import posixpath
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT_BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT_BASE, "config.json")
STATIC = os.path.join(ROOT_BASE, "static", "index.html")

ROOT = "/sdcard"
TX = "/sdcard/EanHost/termux"
SUPID = "/sdcard/EanHost/sup.pid"

BOTS_DIR = TX + "/bots"
BOTS_JSON = BOTS_DIR + "/bots.json"
AGENT_IN = TX + "/inbox"
AGENT_OUT = TX + "/outbox"

EZ_LIMIT = 128 * 1024 * 1024
EZ_MEMBERS = 4000
EZ_ENTRY_PY = ("bot.py", "main.py", "run.py", "app.py", "index.py", "bot_cog.py")
_EZ_PY_DISCORD = re.compile(rb"\b(import|from)\s+(discord|disnake|nextcord)\b")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
EZ_ENVKEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SESSIONS = {}
LOCK = threading.Lock()
CFG_LOCK = threading.Lock()

# Fast Mode keeps opt-in so live bots on the legacy adb/file path are never
# disturbed: with fast_mode False every helper below falls straight through to
# the original adb implementation.
FAST_DEFAULTS = {
    "fast_mode": False,
    "fast_nodes": [],          # empty = every node when fast_mode is on
    "tcp_forward_base": 19000,  # host localhost port for node 0 (node N = base+N)
    "tcp_ports": {},            # per-serial override {serial: port}
    "tcp_hosts": {},            # per-serial override {serial: host}, default 127.0.0.1
    "adb_timeout": 30,
    "inotify_enabled": True,
    "ram_base": "/data/data/com.termux/files/home/eanhost/ram",
    "fallback_to_files": True,
    "global_actions_enabled": True,
    "log_poll_ms": 2000,
}


def cfg_load():
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG) as f:
                cfg = json.load(f) or {}
        except Exception:
            cfg = {}
    merged = dict(FAST_DEFAULTS)
    merged.update(cfg)
    return merged


def cfg_update(**updates):
    """Merge keys into config.json atomically, keeping unrelated keys intact."""
    with CFG_LOCK:
        cfg = {}
        if os.path.exists(CONFIG):
            try:
                with open(CONFIG) as f:
                    cfg = json.load(f) or {}
            except Exception:
                cfg = {}
        cfg.update(updates)
        try:
            mode = os.stat(CONFIG).st_mode & 0o777
        except OSError:
            mode = 0o664
        tmp = CONFIG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, CONFIG)
        return cfg


def set_node_fast(serial, enabled):
    """Turn Fast Mode on/off for one node and return the saved config.

    An empty fast_nodes list means "every node", so disabling the last node
    also clears the master fast_mode flag instead of re-enabling everyone.
    """
    known = [s for s, _ in SERIALS]
    if serial not in known:
        return None
    cfg = cfg_load()
    global_on = bool(cfg.get("fast_mode"))
    nodes = cfg.get("fast_nodes")
    nodes = [s for s in nodes if s in known] if isinstance(nodes, list) else []
    if not global_on:
        current = []
    elif nodes:
        current = list(nodes)
    else:
        current = list(known)  # empty list = all nodes
    if enabled:
        if serial not in current:
            current.append(serial)
        global_on = True
    else:
        current = [s for s in current if s != serial]
    if not current:
        return cfg_update(fast_mode=False, fast_nodes=[])
    if set(current) >= set(known):
        return cfg_update(fast_mode=True, fast_nodes=[])
    ordered = [s for s in known if s in current]
    return cfg_update(fast_mode=True, fast_nodes=ordered)


def nodes_from_cfg(cfg):
    nodes = cfg.get("nodes")
    if isinstance(nodes, list) and all(
        isinstance(n, list) and len(n) == 2 and n[0] and n[1] for n in nodes
    ):
        return [(serial, name) for serial, name in nodes]
    return [("SERIAL1", "Tablet-1"), ("SERIAL2", "Tablet-2")]


SERIALS = nodes_from_cfg(cfg_load())



# ---- Fast Mode: persistent TCP bridge to device-side runner --------------
TCP_CONN_TIMEOUT = 5.0
TCP_READ_TIMEOUT = 30.0


class TCPClient(object):
    """Connection-reusing JSON-line client, one socket per (host, port).

    Each socket gets its own lock so concurrent panel threads can queue on a
    shared connection instead of interleaving request/response frames.
    """

    def __init__(self):
        self._conns = {}
        self._lock = threading.Lock()

    def drop(self, key):
        with self._lock:
            conn = self._conns.pop(key, None)
        if conn is not None:
            try:
                conn["sock"].close()
            except Exception:
                pass

    def _conn(self, addr, port):
        key = (addr, int(port))
        with self._lock:
            conn = self._conns.get(key)
            if conn is not None:
                return conn
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(TCP_CONN_TIMEOUT)
            s.connect((addr, int(port)))
            s.settimeout(TCP_READ_TIMEOUT)
            conn = {"sock": s, "lock": threading.Lock()}
            self._conns[key] = conn
            return conn

    def call(self, addr, port, payload, timeout=TCP_READ_TIMEOUT):
        """Send one JSON object, read one JSON reply line. Returns (obj, err)."""
        key = (addr, int(port))
        try:
            conn = self._conn(addr, port)
            # A half-open socket only surfaces on write; drop and retry once.
            for attempt in (0, 1):
                try:
                    with conn["lock"]:
                        sock = conn["sock"]
                        sock.settimeout(timeout)
                        sock.sendall(
                            json.dumps(payload).encode("utf-8") + b"\n")
                        buf = b""
                        while b"\n" not in buf:
                            chunk = sock.recv(4096)
                            if not chunk:
                                raise EOFError("connection closed")
                            buf += chunk
                            if len(buf) > 1024 * 1024:
                                raise ValueError("reply too large")
                    line = buf.split(b"\n", 1)[0]
                    return json.loads(
                        line.decode("utf-8", errors="replace")), None
                except (EOFError, ConnectionError, OSError):
                    self.drop(key)
                    if attempt == 1:
                        raise
                    conn = self._conn(addr, port)
        except Exception as e:
            self.drop(key)
            return None, str(e)


tcp_pool = TCPClient()

_FWD_LOCK = threading.Lock()
_FWD_DONE = set()


def ensure_forward(serial):
    """Idempotently map host 127.0.0.1:<port> -> device <port> over adb.

    Termux cannot expose a public socket on most setups, so the panel reaches
    the runner through an adb reverse/forward tunnel. This is a no-op once the
    forward exists for the process lifetime.
    """
    if not fast_enabled(serial):
        return False
    if serial in _FWD_DONE:
        return True
    host, port = tcp_endpoint(serial)
    if host not in ("127.0.0.1", "localhost"):
        # Direct TCP to the device's own IP: no adb hop needed.
        with _FWD_LOCK:
            _FWD_DONE.add(serial)
        return True
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "forward", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, timeout=15)
        if r.returncode != 0:
            return False
        with _FWD_LOCK:
            _FWD_DONE.add(serial)
        return True
    except Exception:
        return False


def fast_enabled(serial=None):
    cfg = cfg_load()
    if not cfg.get("fast_mode"):
        return False
    if serial is None:
        return True
    nodes = cfg.get("fast_nodes")
    if isinstance(nodes, list) and nodes:
        return serial in nodes
    return True


def tcp_endpoint(serial):
    """Resolve (host, port) for a device's runner socket from config."""
    cfg = cfg_load()
    hosts = cfg.get("tcp_hosts") or {}
    base = int(cfg.get("tcp_forward_base", 19000))
    ports = cfg.get("tcp_ports") or {}
    order = [s for s, _ in SERIALS]
    try:
        idx = order.index(serial)
    except ValueError:
        idx = 0
    host = hosts.get(serial) or "127.0.0.1"
    port = int(ports.get(serial, base + idx))
    return host, port


def fast_call(serial, payload, timeout=TCP_READ_TIMEOUT):
    """Try the TCP path. Returns dict on success, None to signal fallback.

    Any failure (no tunnel, refused, timeout, malformed reply) returns None so
    the caller transparently uses the legacy adb implementation.
    """
    if not fast_enabled(serial):
        return None
    if not ensure_forward(serial):
        return None
    host, port = tcp_endpoint(serial)
    res, err = tcp_pool.call(host, port, payload, timeout=timeout)
    if err is not None or not isinstance(res, dict):
        # The adb forward dies with a device reconnect or adb-server restart.
        # Forget it so the next request re-establishes the tunnel.
        with _FWD_LOCK:
            _FWD_DONE.discard(serial)
        return None
    if res.get("ok") is False and res.get("error") == "unknown":
        return None
    return res


def hash_pw(pw):
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def adb(serial, cmd, timeout=90):
    try:
        return subprocess.run(["adb", "-s", serial, "shell", cmd],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def out(r):
    if r is None:
        return ""
    return r.stdout.decode(errors="replace").replace("\r", "")


def ret(r):
    if r is None:
        return 124
    return r.returncode


def adb_pull(serial, remote, local, timeout=120):
    try:
        return subprocess.run(["adb", "-s", serial, "pull", remote, local],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def adb_push(local, serial, remote, timeout=120):
    try:
        return subprocess.run(["adb", "-s", serial, "push", local, remote],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def dev_present(serial):
    r = subprocess.run(["adb", "devices"], capture_output=True, timeout=15)
    return r.returncode == 0 and any(
        line.startswith(serial) and ("device" in line)
        for line in r.stdout.decode(errors="replace").splitlines())


def read_remote(serial, path, cap=4 * 1024 * 1024, tail=False):
    """Pull a remote file and return its bytes.

    Default: the first cap+1 bytes (callers rely on this for bots.json,
    downloads and task output). tail=True returns the LAST cap bytes instead,
    starting on a line boundary when the file was cut, for log viewing.
    """
    fd, tmp = tempfile.mkstemp(prefix="eanpanel-")
    os.close(fd)
    try:
        r = adb_pull(serial, path, tmp)
        if r is None or r.returncode != 0:
            return None
        with open(tmp, "rb") as f:
            if not tail:
                return f.read(cap + 1)
            size = os.fstat(f.fileno()).st_size
            start = max(0, size - cap)
            # Only discard leading partial line if window starts mid-line.
            f.seek(start)
            data = f.read(cap)
            if start > 0:
                f.seek(start - 1)
                if f.read(1) != b"\n":
                    pos = data.find(b"\n")
                    if pos >= 0:
                        data = data[pos + 1:]
            return data
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def write_remote(serial, path, data):
    fd, tmp = tempfile.mkstemp(prefix="eanpanel-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        r = adb_push(tmp, serial, path)
        return r is not None and r.returncode == 0
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def battery(serial):
    text = out(adb(serial, "dumpsys battery", timeout=15))
    level, status, ac, usb = None, None, False, False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("level:"):
            try:
                level = int(line.split(":")[1].strip())
            except ValueError:
                pass
        elif line.startswith("status:"):
            try:
                status = int(line.split(":")[1].strip())
            except ValueError:
                pass
        elif line.startswith("AC powered:"):
            ac = "true" in line.lower()
        elif line.startswith("USB powered:"):
            usb = "true" in line.lower()
    charging = ac or usb or status in (2, 5)
    return {"level": level, "charging": charging, "status": status}


def supervisor_status(serial):
    text = out(adb(serial, f"cat {SUPID} 2>/dev/null", timeout=10))
    m = re.search(r"^\s*SUP\s+(\d+)", text, re.M)
    if not m:
        return None
    pid = int(m.group(1))
    alive = "ok" in out(adb(serial, f"kill -0 {pid} 2>/dev/null && echo ok", timeout=8)).strip()
    return {"pid": pid, "alive": alive}


def agent_status(serial):
    text = out(adb(serial, f"cat {TX}/agent.heart 2>/dev/null; echo __NOW__; date +%s", timeout=15))
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    now = None
    for i, ln in enumerate(lines):
        if ln.strip() == "__NOW__":
            try:
                now = int(lines[i + 1].strip())
            except (IndexError, ValueError):
                pass
            break
    m = re.match(r"^(\d+)\s+(\d+)\s+(\d+)$", first)
    if not m or now is None:
        return None, None, None
    it, pid, ts = int(m.group(1)), int(m.group(2)), int(m.group(3))
    age = now - ts
    alive = 0 <= age < 60
    return {"iter": it, "pid": pid, "age": age}, None if alive else age, alive


def bots_status(serial):
    res = fast_call(serial, {"cmd": "status"}, timeout=5.0)
    if res is not None and isinstance(res.get("bots"), dict):
        return res["bots"] or None
    text = out(adb(serial, f"cat {TX}/bots/bots.status 2>/dev/null", timeout=10))
    bots = {}
    for ln in text.splitlines():
        p = ln.strip().split()
        if len(p) >= 2:
            bots[p[0]] = p[1]
    return bots or None


def node_latency(serial, timeout=4.0):
    """Round-trip time to the node, in ms. Fast Mode pings the runner socket;
    the legacy path times a trivial adb shell round-trip. Returns (ms, via)."""
    t0 = time.time()
    if fast_enabled(serial):
        r = fast_call(serial, {"cmd": "ping"}, timeout=timeout)
        if r is not None and r.get("ok"):
            return int((time.time() - t0) * 1000), "tcp"
    r = adb(serial, "echo .", timeout=timeout)
    if r is None:
        return None, "adb"
    return int((time.time() - t0) * 1000), "adb"


def status_light(serial):
    p = dev_present(serial)
    if not p:
        return {"present": False}
    b = battery(serial)
    sup = supervisor_status(serial)
    ag, ag_age, ag_alive = agent_status(serial)
    lat, lat_via = node_latency(serial)
    return {
        "present": True,
        "battery": b,
        "latency_ms": lat,
        "latency_via": lat_via,
        "supervisor": sup,
        "agent": ag,
        "agent_alive": ag_alive,
        "agent_age": ag_age,
        "bots": bots_status(serial),
    }


# Status is expensive (one adb spawn per metric), so refresh it in one
# background thread every STATUS_T seconds; /api/status serves the cache.
STATUS_T = 30
CACHE = {}
CACHE_LOCK = threading.Lock()


def refresh_status_loop():
    while True:
        results = {}

        def collect(serial):
            try:
                results[serial] = status_light(serial)
            except Exception:
                results[serial] = {"present": False}

        started = time.time()
        tds = [threading.Thread(target=collect, args=(s,), daemon=True)
               for s, _ in SERIALS]
        for t in tds:
            t.start()
        for t in tds:
            t.join(timeout=max(1, STATUS_T - 3))
        with CACHE_LOCK:
            for serial, _ in SERIALS:
                CACHE[serial] = {
                    "ts": int(started),
                    **results.get(serial, {"present": False}),
                }
        time.sleep(STATUS_T)


def status_all():
    with CACHE_LOCK:
        snap = dict(CACHE)
    nodes = []
    for serial, name in SERIALS:
        s = dict(snap.get(serial, {"present": False}))
        s["serial"] = serial
        s["name"] = name
        nodes.append(s)
    return {"nodes": nodes}


# ---- resource stats (cpu / ram / disk) -----------------------------------
# One adb roundtrip per node instead of three. The combined command prints
# three delimited blocks; parsing is per-block so a missing block (older
# Android, missing toybox) degrades to None instead of failing the node.
STAT_CMD = (
    "cat /proc/meminfo 2>/dev/null; "
    "echo __CPU__; cat /proc/stat 2>/dev/null; "
    "echo __CPU2__; sleep 1; cat /proc/stat 2>/dev/null; "
    "echo __DISK__; df -k /data 2>/dev/null; df -k /sdcard 2>/dev/null; true"
)

_MEM_KB = re.compile(r"^(\w+):\s+(\d+)")
# standard toybox/bsd: "/dev/block/dm-5  117618516  52345678  61234567  47% /data"
_DF_PCT = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%")
# older toybox: "/data  12234036.0K  1611628.0K  10622408.0K  4.0K"
_DF_K = re.compile(r"^(\S+)\s+([\d.]+)K\s+([\d.]+)K\s+([\d.]+)K")


def _empty_stats(online):
    return {"online": online, "cpu": None, "ram": None, "ramTotal": None,
            "ramPct": None, "disk": None, "diskTotal": None, "diskPct": None}


def _parse_mem(block):
    vals = {}
    for line in block:
        m = _MEM_KB.match(line.strip())
        if m:
            vals[m.group(1)] = int(m.group(2))
    total = vals.get("MemTotal")
    if not total:
        return None, None, None
    # MemAvailable is preferred; fall back to the classic free/total/buffers set.
    avail = vals.get("MemAvailable")
    if avail is None:
        free = vals.get("MemFree", 0)
        avail = free + vals.get("Buffers", 0) + vals.get("Cached", 0)
    used = max(0, total - avail)
    return used // 1024, total // 1024, int(100 * used / total)


def _cpu_counters(block):
    """Return (idle_jiffies, total_jiffies) for the aggregate cpu row."""
    for line in block:
        parts = line.split()
        # /proc/stat lists cpu0, cpu1, ... first; only the aggregate "cpu" row
        # is meaningful for a whole-device figure.
        if len(parts) >= 5 and parts[0] == "cpu":
            try:
                vals = [int(p) for p in parts[1:5]]
            except ValueError:
                return None
            return vals[3], sum(vals)
    return None


def _parse_cpu(block):
    """Single-sample fallback: lifetime-average CPU (used if no delta)."""
    c = _cpu_counters(block)
    if not c:
        return None
    idle, total = c
    if total <= 0:
        return None
    return int(100 * (total - idle) / total)


def _cpu_delta(a, b):
    """Instantaneous CPU% from two /proc/stat samples taken ~1s apart."""
    if not a or not b:
        return None
    idle_d, total_d = b[0] - a[0], b[1] - a[1]
    if total_d <= 0:
        return None
    pct = 100.0 * (1.0 - idle_d / total_d)
    return max(0, min(100, int(round(pct))))


def _parse_disk(block):
    best = None
    for line in block:
        s = line.strip()
        # skip the "Filesystem 1K-blocks ..." / "Filesystem Size Used ..." header
        if not s.startswith("/"):
            continue
        m = _DF_PCT.match(s)
        if m:
            total_k, used_k, pct = int(m.group(2)), int(m.group(3)), int(m.group(5))
            if total_k > 0:
                return (used_k // (1024 * 1024), total_k // (1024 * 1024), pct)
        m = _DF_K.match(s)
        if m:
            total_k = int(float(m.group(2)))
            used_k = int(float(m.group(3)))
            if total_k > 0:
                pct = int(100 * used_k / total_k)
                return (used_k // (1024 * 1024), total_k // (1024 * 1024), pct)
    return best or (None, None, None)


def _split_blocks(text):
    blocks, cur = {}, []
    key = "mem"
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.strip() == "__CPU__":
            blocks[key] = cur
            key, cur = "cpu", []
            continue
        if line.strip() == "__CPU2__":
            blocks[key] = cur
            key, cur = "cpu2", []
            continue
        if line.strip() == "__DISK__":
            blocks[key] = cur
            key, cur = "disk", []
            continue
        cur.append(line)
    blocks[key] = cur
    return blocks


def node_stats(serial):
    """Rough cpu/ram/disk for one node. Never raises."""
    stats = _empty_stats(False)
    try:
        if not dev_present(serial):
            return stats
        text = out(adb(serial, STAT_CMD, timeout=20))
        if not text.strip():
            return stats
        stats["online"] = True
        blocks = _split_blocks(text)
        ram, ram_total, ram_pct = _parse_mem(blocks.get("mem", []))
        stats["ram"], stats["ramTotal"], stats["ramPct"] = ram, ram_total, ram_pct
        disk, disk_total, disk_pct = _parse_disk(blocks.get("disk", []))
        stats["disk"], stats["diskTotal"], stats["diskPct"] = disk, disk_total, disk_pct
        # instantaneous CPU from two on-device samples; single-sample fallback.
        cpu = _cpu_delta(_cpu_counters(blocks.get("cpu", [])),
                         _cpu_counters(blocks.get("cpu2", [])))
        stats["cpu"] = cpu if cpu is not None else _parse_cpu(blocks.get("cpu", []))
    except Exception:
        return _empty_stats(True)
    return stats


STATS_T = 10
STATS_CACHE = {}
STATS_LOCK = threading.Lock()
STATS_SIG = None


def stats_all():
    """Cached per-node stats, refreshed at most every STATS_T seconds."""
    global STATS_SIG
    with STATS_LOCK:
        sig = STATS_SIG
        fresh = sig is not None and (time.time() - sig) < STATS_T
        if fresh:
            snap = dict(STATS_CACHE)
        else:
            snap = None
    if snap is not None:
        return snap
    results = {}

    def collect(serial):
        try:
            results[serial] = node_stats(serial)
        except Exception:
            results[serial] = _empty_stats(False)

    tds = [threading.Thread(target=collect, args=(s,), daemon=True)
           for s, _ in SERIALS]
    for t in tds:
        t.start()
    for t in tds:
        t.join(timeout=12)
    now = time.time()
    out_map = {}
    for serial, name in SERIALS:
        s = results.get(serial) or _empty_stats(False)
        s["name"] = name
        s["ts"] = int(now)
        out_map[serial] = s
    with STATS_LOCK:
        STATS_CACHE.clear()
        STATS_CACHE.update(out_map)
        STATS_SIG = now
    return out_map


# Android's toybox and older busybox builds disagree on `ls -l`: some omit the
# link-count column entirely and some omit the size. Both columns are optional
# so a real entry (permissions, owner, group, date, name) parses either way.
LS_RE = re.compile(
    r"^([dl-][rwxsStT-]{9})\s+"
    r"(?:\d+\s+)?(\S+)\s+(\S+)\s+"
    r"(?:(\d+)\s+)?(\d{4}-\d{2}-\d{2})"
    r"(?:\s+(\d{2}:\d{2}))?\s+(.+)$")


def parse_type(perms):
    if perms.startswith("d"):
        return "dir"
    if perms.startswith("l"):
        return "link"
    return "file"


def parse_ls_text(text):
    """Parse `ls -l` output into entries, tolerating optional columns.

    Handles the two device layouts seen in the fleet: with a link count and
    with none, CRLF line endings, and directory/symlink type detection.
    """
    entries = []
    for line in text.replace("\r", "").splitlines():
        if not line.strip() or line.startswith("total"):
            continue
        m = LS_RE.match(line)
        if m:
            perms, _, _, size, date, t, name = m.groups()
            name = name.strip()
            if name in (".", ".."):
                continue
            if perms.startswith("l") and " -> " in name:
                name = name.split(" -> ", 1)[0]
            entries.append({"name": name, "type": parse_type(perms),
                            "size": int(size) if size else 0,
                            "mtime": (date + " " + t) if t else date})
        else:
            s = line.strip()
            if s and s not in (".", ".."):
                entries.append({"name": s, "type": "file", "size": 0,
                                "mtime": ""})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def list_dir(serial, path):
    lspath = path if path.endswith("/") else path + "/"
    text = out(adb(serial, "ls -l " + shlex.quote(lspath), timeout=60))
    return parse_ls_text(text)


def safe_path(path):
    p = urllib.parse.unquote(path).strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


# ---- EZ bot install -------------------------------------------------------

def _ez_normalize(tree):
    """Strip a GitHub-style wrapper dir when every entry shares one top folder."""
    firsts = {n.split("/", 1)[0] for n in tree}
    if len(firsts) == 1 and "/" in next(iter(tree)):
        prefix = next(iter(firsts))
        return {n[len(prefix):].lstrip("/"): b for n, b in tree.items()
                if n[len(prefix):].lstrip("/")}
    return dict(tree)


def _ez_detect(tree):
    """Pick an entry point + start command for an uploaded bot project."""
    files = {p.lower(): p for p in tree}
    if "package.json" in files:
        raw = tree[files["package.json"]]
        try:
            pkg = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            pkg = None
        pkg_obj = pkg if isinstance(pkg, dict) else {}
        main = str(pkg_obj.get("main") or "index.js")
        start = ((pkg_obj.get("scripts") or {}).get("start") or "").strip()
        has_deps = bool(isinstance(pkg_obj.get("dependencies"), dict) and
                        pkg_obj.get("dependencies"))
        return {"kind": "node", "entry": main,
                "cmd": "npm start" if start else "node " + main,
                "pip": False, "npm": has_deps}
    for cand in EZ_ENTRY_PY:
        if cand in files and files[cand] in tree:
            if _EZ_PY_DISCORD.search(tree[files[cand]][:8192]):
                return {"kind": "python", "entry": files[cand],
                        "cmd": "python " + files[cand],
                        "pip": "requirements.txt" in files, "npm": False}
    for p in sorted(t for t in tree if t.endswith(".py")):
        if "/" not in p and _EZ_PY_DISCORD.search(tree[p][:8192]):
            return {"kind": "python", "entry": p, "cmd": "python " + p,
                    "pip": "requirements.txt" in files, "npm": False}
    for p in sorted(t for t in tree if t.endswith(".py")):
        return {"kind": "python", "entry": p, "cmd": "python " + p,
                "pip": "requirements.txt" in files, "npm": False}
    if "index.js" in files:
        return {"kind": "node", "entry": "index.js", "cmd": "node index.js",
                "pip": False, "npm": False}
    return None


def _ez_install(serial, name, data, token=None, env=None):
    """Deploy an uploaded zip as a supervised bot on one device."""
    if not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError("bad bot name (letters, digits, _ . - only)")
    if len(data) > EZ_LIMIT:
        raise ValueError("zip too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        tree, total = {}, 0
        for item in zf.infolist():
            if item.is_dir():
                continue
            n = item.filename.replace("\\", "/")
            parts = [p for p in n.split("/") if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                zf.close()
                raise ValueError("unsafe path in zip: " + n)
            total += item.file_size
            if total > EZ_LIMIT:
                zf.close()
                raise ValueError("zip expands too large")
            tree[posixpath.normpath("/".join(parts))] = zf.read(item)
        zf.close()
    except zipfile.BadZipFile:
        raise ValueError("not a zip file")
    if not tree:
        raise ValueError("empty zip")
    tree = _ez_normalize(tree)
    files = {p.lower(): p for p in tree}
    det = _ez_detect(tree)
    if det is None:
        raise ValueError("no bot entry point found "
                         "(expected bot.py / main.py / index.js / package.json)")
    # read current registration first (we need existing-or-new before touching .env)
    raw = read_remote(serial, BOTS_JSON)
    if raw is None:
        r = adb(serial, "[ -f %s ] && echo Y || echo N" % shlex.quote(BOTS_JSON))
        if out(r).strip() == "Y":
            raise ValueError("could not read device bots.json, nothing installed")
        conf = {"bots": []}
    else:
        conf = {"bots": []}
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(j, dict) and isinstance(j.get("bots"), list):
                conf = j
        except Exception:
            pass
    existing = any(b.get("name") == name for b in conf["bots"])
    # env file handling: an update always keeps the live .env; a fresh install
    # uses the zip's .env, else one built from the supplied env vars, else a
    # stub the user fills in.
    env_stat = "kept (existing)"
    if existing:
        tree.pop(".env", None)
    elif ".env" not in files:
        pairs = []
        seen = set()
        for k, v in (env or {}).items():
            k = str(k)
            if not EZ_ENVKEY.match(k):
                continue
            v = str(v).replace("\r", "").replace("\n", "")
            if v != "" and k not in seen:
                seen.add(k)
                pairs.append("%s=%s" % (k, v))
        if token and "DISCORD_TOKEN" not in seen:
            for k in ("TOKEN", "DISCORD_TOKEN"):
                if k not in seen:
                    seen.add(k)
                    pairs.append("%s=%s" % (k, token))
        if pairs:
            tree[".env"] = ("\n".join(pairs) + "\n").encode()
            env_stat = "created (%d var%s)" % (len(pairs),
                                               "" if len(pairs) == 1 else "s")
        else:
            tree[".env"] = b"# paste your bot token here, then restart in the panel\nTOKEN=\nDISCORD_TOKEN=\n"
            env_stat = "placeholder"
    else:
        env_stat = "kept (from zip)"
    # push files
    base = BOTS_DIR + "/" + name
    adb(serial, "mkdir -p " + shlex.quote(base))
    pushed, ok = [], 0
    for rel, blob in tree.items():
        if len(pushed) >= EZ_MEMBERS:
            raise ValueError("zip has too many files")
        dst = base + "/" + rel
        if write_remote(serial, dst, blob):
            ok += 1
            pushed.append(rel)
    if ok == 0:
        raise ValueError("could not push files to device")
    adb(serial, "cp %s %s 2>/dev/null; true"
        % (shlex.quote(BOTS_JSON), shlex.quote(BOTS_JSON + ".bak")), timeout=30)
    for b in conf["bots"]:
        if b.get("name") == name:
            b["dir"], b["cmd"] = name, det["cmd"]
            break
    else:
        conf["bots"].append({"name": name, "dir": name, "cmd": det["cmd"]})
    write_remote(serial, BOTS_JSON, json.dumps(conf, indent=2).encode())
    res = {"ok": True, "name": name, "kind": det["kind"],
           "entry": det["entry"], "cmd": det["cmd"], "files": ok,
           "env": env_stat, "pip": det["pip"], "npm": det["npm"],
           "updated": existing}
    if existing:
        # restart so the new code takes effect (runner consumes the ctl line once)
        write_remote(serial, BOTS_DIR + "/bots.ctl",
                     ("restart %s\n" % name).encode())
        res["restart_queued"] = True
    # install deps via the termux agent inbox (runs on-device in Termux)
    if det["pip"] or det["npm"]:
        tag = "ezdeps-" + name
        for suffix in (".out", ".exit"):
            adb(serial, "rm -f " + shlex.quote(AGENT_OUT + "/" + tag + suffix))
        cmds = ["cd " + shlex.quote(base)]
        if det["pip"]:
            cmds.append("python -m pip install -r requirements.txt")
        if det["npm"]:
            cmds.append("npm install --no-audit --no-fund")
        write_remote(serial, AGENT_IN + "/" + tag + ".sh",
                     ("#!/data/data/com.termux/files/usr/bin/bash\n" +
                      "\n".join(cmds) + "\n").encode())
        res["deps_started"] = True
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(2)
            rc = read_remote(serial, AGENT_OUT + "/" + tag + ".exit", cap=16)
            if rc is not None:
                try:
                    res["deps_exit"] = int(rc.strip() or 0)
                except ValueError:
                    res["deps_exit"] = None
                outraw = read_remote(serial, AGENT_OUT + "/" + tag + ".out",
                                     cap=32768)
                res["deps_log_tail"] = (outraw or b"").decode("utf-8",
                                                               "replace")[-3000:]
                break
        res.setdefault("deps_exit", None)
        res.setdefault("deps_log_tail", "")
        res["deps_pending"] = res["deps_exit"] is None
    return res


# ---- auth ---------------------------------------------------------------

def check_session(token):
    with LOCK:
        entry = SESSIONS.get(token)
    if not entry:
        return False
    if entry["exp"] < time.time():
        with LOCK:
            SESSIONS.pop(token, None)
        return False
    return True


def new_session():
    tok = secrets.token_hex(16)
    with LOCK:
        SESSIONS[tok] = {"exp": time.time() + 86400 * 7}
    return tok


def drop_session(token):
    if not token:
        return False
    with LOCK:
        return SESSIONS.pop(token, None) is not None


# ---- http --------------------------------------------------------------

def bot_states(serial, timeout=10):
    """Parse bots.status into {name: {"state": ..., "pid": ...}}.

    Fast Mode asks the runner directly over TCP, which bypasses both the ~10s
    on-disk refresh cadence and the adb round-trip.
    """
    res = fast_call(serial, {"cmd": "status"}, timeout=min(5.0, timeout))
    if res is not None and isinstance(res.get("bots_detail"), dict):
        return res["bots_detail"]
    text = out(adb(serial,
                   "cat %s/bots/bots.status 2>/dev/null" % TX,
                   timeout=timeout))
    result = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        pid = None
        for tok in f[2:]:
            if tok.startswith("pid="):
                pid = tok[4:]
        result[f[0]] = {"state": f[1], "pid": pid}
    return result


def wait_bot_state(serial, name, action, before_pid, before_state,
                   timeout=30):
    """Block until bots.status shows the action actually took effect.

    The runner only drains bots.ctl every ~10s, so replying straight away
    left the UI displaying a stale PID and made working buttons look dead.
    """
    deadline = time.time() + timeout
    while True:
        cur = bot_states(serial).get(name) or {"state": "STP", "pid": None}
        st = cur.get("state")
        pid = cur.get("pid")
        if action == "stop":
            if st == "STP" or st is None:
                return True, cur
        elif action == "start":
            if st == "UP" and (before_state == "UP" or pid != before_pid):
                return True, cur
        elif action == "restart":
            if st == "UP" and pid and pid != before_pid:
                return True, cur
        if time.time() >= deadline:
            return False, cur
        time.sleep(1.5)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EanHostPanel/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, **headers):
        body = json.dumps(obj).encode()
        headers.setdefault("Cache-Control", "no-store")
        self._send(code, body, "application/json", headers)

    def _request_token(self):
        tok = None
        q = urllib.parse.urlparse(self.path)
        if q.query:
            qs = urllib.parse.parse_qs(q.query)
            tok = qs.get("t", [None])[0]
        if not tok:
            tok = self.headers.get("X-Token")
        if not tok:
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"ean_sess=([0-9a-f]+)", cookie)
            if m:
                tok = m.group(1)
        return tok

    def _auth(self):
        tok = self._request_token()
        return check_session(tok) if tok else False

    # -- GET endpoints -------------------------------------------------

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        route = u.path
        if route == "/":
            return self.serve_index()
        if route == "/api/status":
            if not self._auth():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, status_all())
        if route == "/api/stats":
            if not self._auth():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, stats_all())
        if route == "/api/config":
            if not self._auth():
                return self._json(401, {"error": "unauthorized"})
            cfg = cfg_load()
            return self._json(200, {
                "fast_mode": bool(cfg.get("fast_mode")),
                "fast_nodes": cfg.get("fast_nodes") or [],
                "global_actions_enabled": bool(
                    cfg.get("global_actions_enabled", True)),
                "log_poll_ms": int(cfg.get("log_poll_ms", 2000)),
                "nodes": [
                    {"serial": s, "name": n,
                     "fast": fast_enabled(s)}
                    for s, n in SERIALS
                ],
            })
        if route.startswith("/api/node/"):
            return self.api_node_get(u)
        return self._json(404, {"error": "not found"})

    def serve_index(self):
        try:
            with open(STATIC, "rb") as f:
                body = f.read()
        except OSError:
            body = b"missing index.html"
        src = body.decode("utf-8", "replace")
        nodes_js = "[" + ",".join(
            json.dumps([serial, name]) for serial, name in SERIALS
        ) + "]"
        src = re.sub(
            r"const SERIALS=\[.*?\];",
            "const SERIALS=%s;" % nodes_js, src, count=1)
        body = src.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store, must-revalidate"})

    def api_node_get(self, u):
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        parts = u.path.split("/")
        # /api/node/<serial>/...
        if len(parts) < 4:
            return self._json(400, {"error": "bad path"})
        serial = parts[3]
        qs = urllib.parse.parse_qs(u.query)
        if not any(s == serial for s, _ in SERIALS):
            return self._json(400, {"error": "unknown device"})
        sub = parts[4] if len(parts) > 4 else ""
        if sub == "status":
            return self._json(200, status_light(serial))
        if sub == "files":
            path = safe_path(qs.get("path", [ROOT])[0])
            return self._json(200, {"path": path, "entries": list_dir(serial, path)})
        if sub == "download":
            path = qs.get("path", [None])[0]
            if not path:
                return self._json(400, {"error": "no path"})
            data = read_remote(serial, path)
            if data is None:
                return self._json(404, {"error": "pull failed"})
            fname = re.sub(r'[\r\n"\\]', "_", urllib.parse.unquote(os.path.basename(path)))
            cd = f"attachment; filename=\"{fname}\""
            self._send(200, data, "application/octet-stream",
                       {"Content-Disposition": cd})
            return
        if sub == "logs":
            which = qs.get("name", ["svc"])[0]
            res = self.read_log(serial, which, qs.get("bot", [None])[0],
                                since=qs.get("since", [None])[0])
            return self._json(200, res)
        if sub == "task":
            tname = qs.get("name", [None])[0]
            mode = qs.get("mode", ["supervisor"])[0]
            if not tname:
                return self._json(400, {"error": "no task name"})
            return self._json(200, self.poll_task(serial, mode, tname))
        return self._json(404, {"error": "not found"})

    def read_log(self, serial, which, bot=None, since=None):
        """Return {"log","content","size"?, "fast"?}.

        In Fast Mode a botrun log is fetched over TCP and supports incremental
        reads: the caller passes the last ``size`` it saw and only the new
        bytes come back. Legacy adb reads always return the tail with no size,
        which the UI treats as a full replace.
        """
        if which == "botrun" and bot:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "", bot)
            req = {"cmd": "log", "name": safe, "tail": 65536}
            if since is not None and str(since) != "":
                try:
                    req["since"] = int(since)
                except (TypeError, ValueError):
                    pass
            r = fast_call(serial, req, timeout=8.0)
            if r is not None and isinstance(r.get("content"), str):
                out = {"log": which, "content": r["content"],
                       "size": int(r.get("size", 0) or 0),
                       "offset": int(r.get("offset", 0) or 0),
                       "fast": True}
                if r.get("reset"):
                    out["reset"] = True
                return out
        fn = {"svc": "/sdcard/EanHost/svc.log",
              "agent": TX + "/agent.log",
              "heart": TX + "/agent.heart",
              "launch": TX + "/launch.log",
              "runner": TX + "/bots/run.log"}.get(which)
        if which == "botrun":
            fn = TX + "/bots/run-" + re.sub(r"[^A-Za-z0-9_.-]", "", bot or "") + ".log"
        if not fn:
            return {"log": which, "content": "unknown log"}
        data = read_remote(serial, fn, cap=64 * 1024, tail=True)
        if data in (None, b""):
            return {"log": which, "content": ""}
        try:
            s = data.decode(errors="replace")
        except Exception:
            s = repr(data)[:65536]
        lines = s.splitlines()
        return {"log": which, "content": "\n".join(lines[-1500:])}

    def poll_task(self, serial, mode, name):
        if mode == "termux":
            base = f"{TX}/outbox/{name}"
        else:
            base = f"/sdcard/EanHost/results/{(name or '').rstrip('.sh')}"
        e = out(adb(serial, f"cat {base}.exit 2>/dev/null"))
        o = out(adb(serial, f"cat {base}.out 2>/dev/null"))
        done = bool(e.strip())
        return {"done": done, "exit": e.strip() or None, "output": o[-40000:]}

    # -- POST endpoints -------------------------------------------------

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        route = u.path
        if route == "/api/login":
            return self.api_login()
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        if route == "/api/logout":
            drop_session(self._request_token())
            return self._json(
                200, {"ok": True},
                **{"Set-Cookie": "ean_sess=; Path=/; Max-Age=0; "
                                  "Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
                                  "SameSite=Lax"})
        if route == "/api/config":
            return self.api_config_post()
        if route == "/api/global":
            return self.api_global(u)
        if route.startswith("/api/node/"):
            return self.api_node_post(u)
        return self._json(404, {"error": "not found"})

    def api_global(self, u):
        body = self.read_json() or {}
        action = str(body.get("action") or "").strip().lower()
        one = str(body.get("serial") or "").strip()
        if action not in ("start_all", "stop_all", "update_all",
                          "clear_logs"):
            return self._json(400, {"error": "bad global action"})
        if not cfg_load().get("global_actions_enabled", True):
            return self._json(403, {"error": "global actions disabled"})
        known = [s for s, _ in SERIALS]
        targets = [one] if one in known else known
        results = {}
        for serial in targets:
            r = fast_call(serial, {"cmd": "global", "action": action},
                          timeout=25.0)
            if r is not None and r.get("ok"):
                results[serial] = {"via": "tcp",
                                   "detail": {k: r.get(k) for k in
                                              ("sent", "started", "stopped",
                                               "cleared", "updated", "errors")
                                              if k in r}}
                continue
            results[serial] = self._global_legacy(serial, action)
        return self._json(200, {"ok": True, "action": action,
                                "results": results})

    def _global_legacy(self, serial, action):
        states = bot_states(serial)
        if action in ("start_all", "stop_all"):
            verb = "start" if action == "start_all" else "stop"
            lines = []
            for name, st in states.items():
                state = (st or {}).get("state")
                if verb == "stop" and state == "STP":
                    continue
                if verb == "start" and state == "UP":
                    continue
                lines.append("%s %s" % (verb, name))
            if not lines:
                return {"via": "adb", "sent": 0, "ok": True}
            ok = write_remote(serial, TX + "/bots/bots.ctl",
                              ("\n".join(lines) + "\n").encode())
            return {"via": "adb", "sent": len(lines), "ok": ok}
        if action == "clear_logs":
            r = adb(serial,
                    "for f in %s/bots/run-*.log; do : > \"$f\"; done" % TX)
            return {"via": "adb", "ok": ret(r) == 0}
        if action == "update_all":
            return {"via": "adb", "ok": False,
                    "error": "update_all needs Fast Mode (TCP) enabled"}
        return {"error": "unknown action"}

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8 * 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return None

    def api_config_post(self):
        body = self.read_json() or {}
        serial = str(body.get("serial") or "").strip()
        if "fast" not in body:
            return self._json(400, {"error": "missing fast"})
        if not any(s == serial for s, _ in SERIALS):
            return self._json(400, {"error": "unknown device"})
        saved = set_node_fast(serial, bool(body.get("fast")))
        if saved is None:
            return self._json(400, {"error": "unknown device"})
        return self._json(200, {
            "ok": True,
            "serial": serial,
            "fast": fast_enabled(serial),
            "fast_mode": bool(saved.get("fast_mode")),
            "fast_nodes": saved.get("fast_nodes") or [],
        })

    def api_login(self):
        body = self.read_json()
        pw = (body or {}).get("password", "")
        if hash_pw(str(pw)) != cfg_load().get("password_hash"):
            return self._json(401, {"error": "wrong password"})
        tok = new_session()
        self._json(200, {"ok": True, "token": tok},
                   **{"Set-Cookie": f"ean_sess={tok}; Path=/; SameSite=Lax"})
        return

    def api_node_post(self, u):
        parts = u.path.split("/")
        serial = parts[3]
        sub = parts[4] if len(parts) > 4 else ""
        qs = urllib.parse.parse_qs(u.query)
        if not any(s == serial for s, _ in SERIALS):
            return self._json(400, {"error": "unknown device"})
        if sub == "bot":
            body = self.read_json() or {}
            name = str(body.get("name") or "").strip()
            action = str(body.get("action") or "").strip().lower()
            if not re.match(r"^[A-Za-z0-9_.-]+$", name) or \
                    action not in ("start", "stop", "restart", "sync"):
                return self._json(400, {"error": "bad bot action"})
            if action == "sync":
                # git lives in the Termux env, so this is TCP-only (no adb path).
                res = fast_call(serial, {"cmd": "bot", "action": "sync",
                                         "name": name}, timeout=150.0)
                if res is None:
                    return self._json(503, {"ok": False, "name": name,
                                            "action": "sync",
                                            "error": "sync needs Fast Mode (TCP)"})
                return self._json(200 if res.get("ok") else 500,
                                  {"ok": bool(res.get("ok")), "name": name,
                                   "action": "sync", "via": "tcp",
                                   "rev": res.get("rev"),
                                   "error": res.get("error"),
                                   "log": res.get("log")})
            before = bot_states(serial).get(name) or {}
            ok = False
            via = "adb"
            res = fast_call(serial, {"cmd": "bot", "action": action,
                                     "name": name}, timeout=8.0)
            if res is not None and res.get("ok"):
                ok = True
                via = "tcp"
            if not ok:
                ok = write_remote(serial, TX + "/bots/bots.ctl",
                                  ("%s %s\n" % (action, name)).encode())
            if not ok:
                return self._json(500, {"ok": False, "name": name,
                                        "action": action,
                                        "error": "device unreachable"})
            done, after = wait_bot_state(
                serial, name, action,
                before.get("pid"), before.get("state"), timeout=30)
            return self._json(200 if done else 202,
                              {"ok": done, "name": name,
                               "action": action, "via": via,
                               "state": after.get("state"),
                               "pid": after.get("pid"),
                               "error": None if done else
                                        "runner did not confirm in 30s"})
        if sub == "cmd":
            body = self.read_json()
            cmd = (body or {}).get("cmd", "")
            if not cmd:
                return self._json(400, {"error": "empty command"})
            r = adb(serial, cmd)
            return self._json(200, {"exit": ret(r), "output": out(r)[-40000:]})
        if sub == "mkdir":
            body = self.read_json() or {}
            p = safe_path((body or {}).get("path", ""))
            r = adb(serial, "mkdir -p " + shlex.quote(p))
            return self._json(200, {"exit": ret(r)})
        if sub == "delete":
            body = self.read_json() or {}
            p = safe_path(body.get("path", ""))
            r = adb(serial, "rm -rf " + shlex.quote(p), timeout=120)
            return self._json(200, {"exit": ret(r)})
        if sub == "rename":
            body = self.read_json() or {}
            p = safe_path(body.get("path", ""))
            newname = str(body.get("newname", "")).strip()
            if not p or not newname or "/" in newname:
                return self._json(400, {"error": "bad rename"})
            dst = p.rsplit("/", 1)[0] + "/" + newname
            r = adb(serial, "mv " + shlex.quote(p) + " " + shlex.quote(dst))
            return self._json(200, {"exit": ret(r)})
        if sub == "writetext":
            body = self.read_json() or {}
            p = safe_path(body.get("path", ""))
            content = (body.get("content") or "").encode("utf-8")
            ok = write_remote(serial, p, content)
            return self._json(200 if ok else 500, {"ok": ok})
        if sub == "upload":
            path = safe_path(qs.get("path", [None])[0] or "")
            if not path:
                return self._json(400, {"error": "no destination"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256 * 1024 * 1024:
                return self._json(413, {"error": "too large"})
            data = self.rfile.read(n)
            ok = write_remote(serial, path, data)
            return self._json(200 if ok else 500, {"ok": ok, "bytes": len(data)})
        if sub == "broadcast":
            path = qs.get("path", [None])[0]
            if not path:
                return self._json(400, {"error": "no path"})
            data = read_remote(serial, path)
            if data is None:
                return self._json(404, {"error": "pull failed"})
            results = {}
            for serial2, name2 in SERIALS:
                if serial2 == serial:
                    continue
                results[name2] = bool(write_remote(serial2, path, data))
            return self._json(200, {"sent": path, "bytes": len(data),
                                    "results": results})
        if sub == "task":
            body = self.read_json() or {}
            mode = body.get("mode", "supervisor")
            name = str(body.get("name") or "").strip() or (f"task-{int(time.time())}")
            script = body.get("script") or ""
            if mode == "termux":
                dst = f"{TX}/inbox/{(name or '').rstrip('.sh')}.sh"
                results_base = "termux"
            else:
                dst = f"/sdcard/EanHost/tasks/{(name or '').rstrip('.sh')}.sh"
                results_base = "supervisor"
            if not script.strip():
                return self._json(400, {"error": "empty script"})
            fd, tmp = tempfile.mkstemp(prefix="eanpanel-task-")
            os.close(fd)
            try:
                with open(tmp, "w") as f:
                    f.write(script)
                r = adb_push(tmp, serial, dst)
                ok = r is not None and r.returncode == 0
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return self._json(200 if ok else 500,
                              {"ok": ok, "name": name, "mode": results_base})
        if sub == "ezinstall":
            try:
                qs2 = urllib.parse.parse_qs(u.query)
                name = str(qs2.get("name", [None])[0] or "").strip()
                token = str(qs2.get("token", [None])[0] or "").strip()
                env = {}
                raw_env = str(qs2.get("env", [None])[0] or "").strip()
                if raw_env:
                    try:
                        parsed = json.loads(raw_env)
                    except Exception:
                        raise ValueError("env must be a JSON object")
                    if not isinstance(parsed, dict):
                        raise ValueError("env must be a JSON object")
                    for k, v in parsed.items():
                        if not EZ_ENVKEY.match(str(k)):
                            continue
                        env[str(k)] = str(v)
                n = int(self.headers.get("Content-Length") or 0)
                if n > EZ_LIMIT:
                    return self._json(413, {"error": "zip too large"})
                if not name:
                    return self._json(400, {"error": "missing bot name"})
                data = self.rfile.read(n)
                res = _ez_install(serial, name, data, token, env)
                return self._json(200, res)
            except ValueError as e:
                return self._json(422, {"error": str(e)})
        return self._json(404, {"error": "not found"})


# ---- app --------------------------------------------------------------

def serve(port, bind):
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            et = sys.exc_info()[0]
            if et in (ConnectionResetError, BrokenPipeError):
                return
            super().handle_error(request, client_address)

    threading.Thread(target=refresh_status_loop, daemon=True).start()
    httpd = Server((bind, port), Handler)
    print(f"panel http on {bind}:{port}")
    httpd.serve_forever()


def set_password(pw):
    cfg = cfg_load()
    cfg["password_hash"] = hash_pw(str(pw))
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG)
    print(f"password set in {CONFIG}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--set-pass", default=None)
    args = ap.parse_args()
    if args.set_pass:
        set_password(args.set_pass)
        return
    if not cfg_load().get("password_hash"):
        print("no password set; run: python3 panel.py --set-pass <password>")
        return
    serve(args.port, args.bind)


if __name__ == "__main__":
    main()
