#!/usr/bin/env python3
"""Measure a framework app and the stack under it on a CoreELEC box.

    bench.py run [--app ID] [--scenario FILE|-] [--suite stack|app]... [--case NAME]...
                 [--env NAME=VALUE]... [--repeat N] [--warmup N] [--label TEXT] [--out DIR]
    bench.py run --list [--scenario FILE|-]
    bench.py latest
    bench.py stop
    bench.py report REPORT.json
    bench.py compare BASE.json NEW.json

`run` needs the box idle at Kodi's Home screen. It closes a running framework
app, then opens the scenario's app, or the one --app names, once per
repetition. The stack suite loads stack.html in place of that app's page and
runs fixed workloads, so any installed framework app will do; the app suite
opens the app's own page and plays the scenario's keys. --case limits a run to the named
cases, which --list prints. An app step still plays the steps before it, so it
starts from the same screen, but only the named steps are measured. Each run writes report.json,
report.md and the raw logs to the output folder.

The shell's ORIEL_FRAME_LOG gives a timestamp for every frame Kodi draws.
Frameworks without it still give startup, CPU and memory figures.

--dev-page measures the app's development page from its development
settings, such as a Vite server, in place of its installed page. --env adds a
variable to the shell's environment, such as a WebKit setting to try; the
report lists it.

A run keeps going when the connection that started it closes, so a dropped
ssh session doesn't lose it: `latest` prints the newest finished report
folder and `stop` ends a run, restoring the app's development settings. One
run at a time can use the box.

`report` prints a report as Markdown. `compare` prints the changes between
two reports, and runs anywhere with Python 3. It exits with status 3 when
a measure got worse, 1 on errors.

Only the Python standard library is used.
"""
import argparse
import fcntl
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kodirpc  # noqa: E402

SCHEMA = "oriel-bench/1"
HERE = Path(__file__).resolve().parent
KODI_HOME = 10000
APP_PORT = 32499
ADDONS = Path("/storage/.kodi/addons")
ADDON_DATA = Path("/storage/.kodi/userdata/addon_data")
DEV = Path("/storage/oriel/dev")
RUN = Path("/run/oriel-bench")
LOCK = RUN / "lock"
RESULTS = Path("/storage/oriel/bench")
FRAMEWORK = "script.module.oriel.framework"
STACK_ADDONS = {"framework": FRAMEWORK, "webkit": "script.module.oriel.webkit", "display": "vfs.oriel.display"}
KEYS = {
    "up": "Input.Up", "down": "Input.Down", "left": "Input.Left", "right": "Input.Right",
    "select": "Input.Select", "back": "Input.Back", "info": "Input.Info", "menu": "Input.ContextMenu",
}
# The stack page's cases, in the order it runs them.
STACK_CASES = ["idle", "small-paint", "full-paint", "transform", "scroll", "crossfade", "layout",
               "script", "decode", "keys"]
STACK_KEYS = ["right"] * 6 + ["down"] * 2 + ["left"] * 6 + ["up"] * 2
STACK_KEY_INTERVAL = 0.4
# A gap this long ends a burst of frames: the screen has settled.
QUIET = 0.5
# Frames closer together than this belong to one animation when counting late frames.
BURST = 0.25

# Metric name: (unit, which way is better, description).
METRICS = {
    "first_frame_ms": ("ms", "lower", "launch to the first frame Kodi drew"),
    "ready_ms": ("ms", "lower", "launch to the page's ready command"),
    "settled_ms": ("ms", "lower", "launch to the first screen settling"),
    "fps": ("fps", "higher", "frames Kodi drew per second"),
    "delivered_pct": ("%", "higher", "frames drawn per display refresh"),
    "late": ("frames", "lower", "frames later than 1.5 refreshes after the previous one"),
    "frame_p95_ms": ("ms", "lower", "95th percentile time between frames"),
    "frame_max_ms": ("ms", "lower", "longest time between frames"),
    "pipeline_ms": ("ms", "lower", "median time from WebKit finishing a frame to Kodi drawing it"),
    "raf_p95_ms": ("ms", "lower", "95th percentile requestAnimationFrame interval in the page"),
    "script_p95_ms": ("ms", "lower", "95th percentile page script time per frame"),
    "script_ms": ("ms", "lower", "fixed script workload"),
    "decode_ms": ("ms", "lower", "median decode time of a 1920x1080 JPEG"),
    "key_delivery_ms": ("ms", "lower", "median time from sending a key through Kodi to WebKit receiving it"),
    "key_frame_ms": ("ms", "lower", "median time from a key to the first frame drawn after it"),
    "key_frame_max_ms": ("ms", "lower", "longest time from a key to the first frame drawn after it"),
    "unanswered": ("keys", "lower", "keys with no frame drawn before the next key"),
    "settle_ms": ("ms", "lower", "last key to the screen settling"),
    "cpu_pct": ("%", "lower", "busy CPU time across all cores"),
    "webkit_cpu_ms": ("ms", "lower", "CPU time used by the shell and WebKit processes"),
    "kodi_cpu_ms": ("ms", "lower", "CPU time used by Kodi, including the app's Python"),
    "gpu_pct": ("%", "lower", "GPU busy time"),
    "webkit_pss_mb": ("MB", "lower", "proportional memory of the shell and WebKit processes"),
    "gpu_mem_mb": ("MB", "lower", "GPU memory held by the shell and WebKit processes"),
    "available_mb": ("MB", "higher", "memory available to the system"),
}
# Report tables: the measures each shows, as columns.
TABLES = [
    ("Startup", ["ready_ms", "first_frame_ms", "settled_ms"]),
    ("Frames", ["fps", "delivered_pct", "late", "frame_p95_ms", "frame_max_ms", "pipeline_ms"]),
    ("Keys", ["key_delivery_ms", "key_frame_ms", "key_frame_max_ms", "unanswered", "settle_ms"]),
    ("Load", ["cpu_pct", "webkit_cpu_ms", "kodi_cpu_ms", "gpu_pct", "raf_p95_ms", "script_p95_ms"]),
    ("Work", ["script_ms", "decode_ms"]),
    ("Memory", ["webkit_pss_mb", "gpu_mem_mb", "available_mb"]),
]
# Headline metrics: (suite, case, metric).
HEADLINE = [
    ("stack", "startup", "first_frame_ms"),
    ("stack", "scroll", "delivered_pct"),
    ("stack", "crossfade", "delivered_pct"),
    ("stack", "script", "script_ms"),
    ("stack", "decode", "decode_ms"),
    ("stack", "keys", "key_frame_ms"),
    ("app", "startup", "settled_ms"),
    ("app", "end", "webkit_pss_mb"),
]


class BenchError(Exception):
    pass


def log(*args):
    # Keep running after the connection that started the run has closed.
    try:
        print(*args, file=sys.stderr, flush=True)
    except OSError:
        pass


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


# ---------------------------------------------------------------- Kodi


class Kodi(kodirpc.Kodi):
    def key(self, name):
        """Send a remote key and return the CLOCK_MONOTONIC time it left."""
        t = time.monotonic()
        self.call(KEYS[name])
        return t

    def window(self):
        return self.call("GUI.GetProperties", {"properties": ["currentwindow"]})["currentwindow"]["id"]

    def version(self, addon):
        try:
            details = self.call("Addons.GetAddonDetails", {"addonid": addon, "properties": ["version"]})
        except RuntimeError:
            return None
        return details["addon"]["version"]


def api(cmd):
    req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}/api/cmd", data=json.dumps({"cmd": cmd}).encode())
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def port_free():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", APP_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def processes():
    """Map pid to command name for every process."""
    found = {}
    for path in Path("/proc").glob("[0-9]*"):
        name = read(path / "comm")
        if name:
            found[int(path.name)] = name
    return found


def app_running():
    return not port_free() or "oriel-shell" in processes().values()


def close_app(timeout=20):
    if not app_running():
        return
    log("closing the running app")
    try:
        api("exit")
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while app_running():
        if time.monotonic() > deadline:
            raise BenchError("the app did not close; exit it and run again")
        time.sleep(0.25)


# ---------------------------------------------------------------- box


def refresh_rate(mode):
    """Read the refresh rate from a CoreELEC mode name such as 1080p60hz."""
    m = re.search(r"[pi](\d+)", mode or "")
    hz = int(m.group(1)) if m else 60
    return {23: 24000 / 1001, 29: 30000 / 1001, 59: 60000 / 1001}.get(hz, float(hz or 60))


def first(pattern):
    paths = sorted(Path("/").glob(pattern.lstrip("/")))
    return paths[0] if paths else None


def cpu_info():
    parts, implementers = {}, {}
    for block in (read("/proc/cpuinfo") or "").split("\n\n"):
        fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        if "processor" in fields:
            parts[int(fields["processor"])] = fields.get("CPU part")
    freqs = []
    for cpu in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        f = read(cpu / "cpufreq/cpuinfo_max_freq")
        freqs.append(int(f) // 1000 if f else None)
    # ARM part numbers for the cores Amlogic boxes use.
    names = {"0xd03": "Cortex-A53", "0xd05": "Cortex-A55", "0xd09": "Cortex-A73", "0xd0b": "Cortex-A76",
             "0xd41": "Cortex-A78", "0xd46": "Cortex-A510", "0xd4d": "Cortex-A715"}
    clusters = {}
    for i, f in enumerate(freqs):
        key = (names.get(parts.get(i), parts.get(i) or "core"), f)
        clusters[key] = clusters.get(key, 0) + 1
    return ", ".join(f"{n}x {name} {f} MHz" if f else f"{n}x {name}" for (name, f), n in clusters.items())


def box_info(kodi):
    mode = read("/sys/class/display/mode")
    os_release = dict(line.split("=", 1) for line in (read("/etc/os-release") or "").splitlines() if "=" in line)
    version = kodi.call("Application.GetProperties", {"properties": ["version"]})["version"]
    gpu = first("/sys/class/misc/mali*/device/gpuinfo")
    governors = sorted({read(p) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")} - {None})
    mem = meminfo()
    return {
        "host": platform.node(),
        "model": (read("/proc/device-tree/model") or "").strip("\0 ") or None,
        "soc": ([c for c in (read("/proc/device-tree/compatible") or "").split("\0") if c] or [None])[-1],
        "cpu": cpu_info(),
        "cores": os.cpu_count(),
        "gpu": read(gpu) if gpu else None,
        "memory_mb": round(mem.get("MemTotal", 0) / 1024),
        "kernel": platform.release(),
        "os": os_release.get("PRETTY_NAME", "").strip('"') or None,
        "kodi": f"{version['major']}.{version['minor']} {version.get('tag', '')}".strip(),
        "display_mode": mode,
        "refresh_hz": round(refresh_rate(mode), 3),
        "governor": ", ".join(governors) or None,
    }


def meminfo():
    info = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        name, _, value = line.partition(":")
        if value.split():
            info[name] = int(value.split()[0])
    return info


def temperature():
    temps = [int(t) / 1000 for t in (read(p) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")) if t]
    return max(temps) if temps else None


def cpu_busy():
    """Return (busy, total) jiffies across all CPUs."""
    fields = [int(v) for v in (read("/proc/stat") or "cpu 0").splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields) - idle, sum(fields)


class Gpu:
    """Mali busy time from the kbase driver's debugfs, where present."""

    def __init__(self):
        self.path = first("/sys/kernel/debug/mali*/dvfs_utilization")

    def sample(self):
        text = read(self.path) if self.path else None
        m = re.search(r"busy_time:\s*(\d+)\s+idle_time:\s*(\d+)", text or "")
        return (int(m.group(1)), int(m.group(1)) + int(m.group(2))) if m else None


def gpu_memory():
    """Map pid to GPU memory in bytes from the Mali driver's gpu_memory table."""
    path = first("/sys/kernel/debug/mali*/gpu_memory") or first("/sys/class/misc/mali*/device/gpu_memory")
    used = {}
    for line in (read(path) or "").splitlines() if path else []:
        parts = line.split()
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            used[int(parts[1])] = used.get(int(parts[1]), 0) + int(parts[2]) * 4096
    return used


def group(name):
    if name == "kodi.bin":
        return "kodi"
    if name == "oriel-shell" or name.startswith("WPE"):
        return "webkit"
    return None


def cpu_ticks():
    """Return CPU ticks per process group."""
    ticks = {"kodi": 0, "webkit": 0}
    for pid, name in processes().items():
        g = group(name)
        if not g:
            continue
        stat = read(f"/proc/{pid}/stat")
        if stat:
            fields = stat.rsplit(")", 1)[1].split()
            ticks[g] += int(fields[11]) + int(fields[12])
    return ticks


def memory():
    pss = 0
    gpu = gpu_memory()
    gpu_used = 0
    for pid, name in processes().items():
        if group(name) != "webkit":
            continue
        for line in (read(f"/proc/{pid}/smaps_rollup") or "").splitlines():
            if line.startswith("Pss:"):
                pss += int(line.split()[1]) * 1024
        gpu_used += gpu.get(pid, 0)
    return {
        "webkit_pss_mb": round(pss / 2**20, 1),
        "gpu_mem_mb": round(gpu_used / 2**20, 1) if gpu else None,
        "available_mb": round(meminfo().get("MemAvailable", 0) / 1024, 1),
    }


class Usage:
    """CPU and GPU use between start() and stop()."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.hz = os.sysconf("SC_CLK_TCK")

    def start(self):
        self.t = time.monotonic()
        self.cpu = cpu_busy()
        self.ticks = cpu_ticks()
        self.gpu_start = self.gpu.sample()

    def stop(self):
        cpu = cpu_busy()
        ticks = cpu_ticks()
        out = {
            "cpu_pct": round(100 * (cpu[0] - self.cpu[0]) / max(1, cpu[1] - self.cpu[1]), 1),
            "webkit_cpu_ms": round(max(0, ticks["webkit"] - self.ticks["webkit"]) * 1000 / self.hz),
            "kodi_cpu_ms": round(max(0, ticks["kodi"] - self.ticks["kodi"]) * 1000 / self.hz),
        }
        gpu = self.gpu.sample()
        if gpu and self.gpu_start and gpu[1] > self.gpu_start[1]:
            out["gpu_pct"] = round(100 * (gpu[0] - self.gpu_start[0]) / (gpu[1] - self.gpu_start[1]), 1)
        return out


class Monitor(threading.Thread):
    """Record temperature and CPU clocks once a second while the bench runs."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stopped = threading.Event()
        self.temps = []
        self.throttled = False
        self.governors = {p: read(p.parent / "scaling_governor") for p in
                          Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")}

    def run(self):
        while not self.stopped.wait(1):
            t = temperature()
            if t is not None:
                self.temps.append(t)
            for path, governor in self.governors.items():
                cur, top = read(path), read(path.parent / "scaling_max_freq")
                # Under the performance governor any clock below the maximum is throttling.
                if governor == "performance" and cur and top and int(cur) < int(top):
                    self.throttled = True


def wait_idle(limit=15.0, timeout=60):
    """Wait until the CPUs are below limit percent busy over two seconds."""
    deadline = time.monotonic() + timeout
    while True:
        a = cpu_busy()
        time.sleep(2)
        b = cpu_busy()
        busy = 100 * (b[0] - a[0]) / max(1, b[1] - a[1])
        if busy < limit:
            return busy
        if time.monotonic() > deadline:
            return busy


# ---------------------------------------------------------------- frames


class Frames:
    """Read the shell's frame log: (rendered, sent, drawn) per frame."""

    def __init__(self, path):
        self.path = path
        self.frames = []
        self.offset = 0
        self.partial = ""

    def poll(self):
        try:
            with open(self.path) as f:
                f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except FileNotFoundError:
            return self.frames
        lines = (self.partial + data).split("\n")
        self.partial = lines.pop()
        for line in lines:
            parts = line.split()
            if len(parts) == 3:
                self.frames.append(tuple(float(p) for p in parts))
        return self.frames

    @property
    def available(self):
        return Path(self.path).exists()

    def drawn(self, a, b):
        return [f for f in self.frames if a <= f[2] < b]

    def last_drawn(self):
        return self.frames[-1][2] if self.frames else None

    def wait_quiet(self, since, quiet=QUIET, timeout=10.0):
        """Wait until no frame has been drawn for quiet seconds; return the last frame's time."""
        deadline = since + timeout
        while True:
            self.poll()
            now = time.monotonic()
            last = max(since, self.last_drawn() or since)
            if now - last >= quiet:
                return last if last > since else None
            if now >= deadline:
                return None
            time.sleep(0.05)


def frame_metrics(frames, a, b, refresh, bursts=False):
    """Summarise frames drawn between a and b. With bursts, time frames only
    within animations, leaving out the still screen between keys."""
    drawn = frames.drawn(a, b)
    period = 1 / refresh
    out = {"fps": round(len(drawn) / max(1e-6, b - a), 1),
           "delivered_pct": round(100 * len(drawn) * period / max(1e-6, b - a), 1)}
    gaps = [y[2] - x[2] for x, y in zip(drawn, drawn[1:])]
    if bursts:
        gaps = [g for g in gaps if g < BURST]
    if gaps:
        gaps.sort()
        out["frame_p95_ms"] = round(1000 * quantile(gaps, 0.95), 1)
        out["frame_max_ms"] = round(1000 * gaps[-1], 1)
    out["late"] = sum(1 for g in gaps if 1.5 * period < g < BURST)
    if drawn:
        out["pipeline_ms"] = round(1000 * statistics.median(f[2] - f[0] for f in drawn), 2)
    return out


def key_metrics(frames, sent, end, arrivals):
    """Summarise keys sent at the given times: delivery and first frame after each."""
    out = {}
    latencies, unanswered = [], 0
    for i, t in enumerate(sent if frames.available else []):
        limit = sent[i + 1] if i + 1 < len(sent) else end
        after = [f for f in frames.frames if f[0] > t and f[2] < limit]
        if after:
            latencies.append(after[0][2] - t)
        else:
            unanswered += 1
    if latencies:
        out["key_frame_ms"] = round(1000 * statistics.median(latencies), 1)
        out["key_frame_max_ms"] = round(1000 * max(latencies), 1)
    if frames.available:
        out["unanswered"] = unanswered
    delivery = []
    for t in sent:
        later = [a for a in arrivals if a >= t]
        if later and later[0] - t < 2:
            delivery.append(later[0] - t)
    if delivery:
        out["key_delivery_ms"] = round(1000 * statistics.median(delivery), 1)
    return out


def quantile(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))]


def key_arrivals(shell_log, since):
    times = []
    for line in shell_log.splitlines():
        m = re.match(r"([\d.]+) oriel-shell: key code=\d+", line)
        if m and float(m.group(1)) >= since:
            times.append(float(m.group(1)))
    return times


# ---------------------------------------------------------------- runs


class Session:
    """One bench invocation: the box, its conditions and the collected results."""

    def __init__(self, args):
        self.args = args
        self.kodi = Kodi()
        self.gpu = Gpu()
        self.out = Path(args.out)
        self.raw = self.out / "raw"
        self.results = {}
        self.warnings = []
        self.dev_backups = {}

    # ---- dev settings

    def set_dev(self, app, settings):
        path = DEV / f"{app}.json"
        if app not in self.dev_backups:
            self.dev_backups[app] = path.read_bytes() if path.exists() else None
        DEV.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings))

    def restore_dev(self):
        for app, data in self.dev_backups.items():
            path = DEV / f"{app}.json"
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        self.dev_backups.clear()

    # ---- launching

    def preflight(self):
        if self.kodi.call("Player.GetActivePlayers"):
            raise BenchError("Kodi is playing media; stop playback and run again")
        close_app()
        if self.kodi.window() != KODI_HOME:
            self.kodi.call("GUI.ActivateWindow", {"window": "home"})
            time.sleep(1)
            if self.kodi.window() != KODI_HOME:
                raise BenchError("a Kodi window or dialog stays open over Home; close it and run again")

    def launch(self, app, dev, tag):
        """Open app with dev settings; return (launch time, frame reader, log paths)."""
        RUN.mkdir(parents=True, exist_ok=True)
        frame_log = RUN / f"{tag}.frames"
        frame_log.unlink(missing_ok=True)
        dev = {**dev, "env": {**dev.get("env", {}), **self.env, "ORIEL_FRAME_LOG": str(frame_log)}}
        self.set_dev(app, dev)
        busy = wait_idle()
        if busy >= 15:
            self.warnings.append(f"{tag}: the CPUs were {busy:.0f}% busy before launch")
        data = ADDON_DATA / app
        t0 = time.monotonic()
        self.kodi.call("Addons.ExecuteAddon", {"addonid": app})
        return t0, Frames(frame_log), data / "addon.log", data / "shell.log"

    def wait_log(self, path, pattern, since, timeout):
        """Wait for a line matching pattern with a monotonic prefix at or after since."""
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            for line in (read(path) or "").splitlines():
                m = re.match(r"([\d.]+) (.*)", line)
                if m and float(m.group(1)) >= since and regex.search(m.group(2)):
                    return float(m.group(1)), m
            time.sleep(0.1)
        return None, None

    def finish(self, tag, frames, addon_log, shell_log):
        try:
            close_app()
        finally:
            self.restore_dev()
        for path in (addon_log, shell_log, frames.path):
            if Path(path).exists():
                shutil.copy(path, self.raw / f"{tag}-{Path(path).name}")

    def startup(self, t0, frames, addon_log, timeout=30):
        ready, _ = self.wait_log(addon_log, r"^command ready", t0, timeout)
        if ready is None:
            raise BenchError("the page did not report ready")
        out = {"ready_ms": round(1000 * (ready - t0))}
        frames.poll()
        if frames.frames:
            out["first_frame_ms"] = round(1000 * (frames.frames[0][2] - t0))
        return out

    def record(self, suite, case, metrics):
        bucket = self.results.setdefault(suite, {}).setdefault(case, {})
        for name, value in metrics.items():
            if value is not None and not isinstance(value, bool):
                bucket.setdefault(name, []).append(value)

    # ---- stack suite

    def stack_run(self, tag, measured):
        events = Queue()
        config = {"keys": len(STACK_KEYS), "cases": self.cases("stack")}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/config":
                    return self.reply(json.dumps(config).encode(), "application/json")
                if self.path.split("?")[0] in ("/", "/stack.html"):
                    return self.reply((HERE / "stack.html").read_bytes(), "text/html")
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                events.put((time.monotonic(), json.loads(body)))
                self.reply(b"{}", "application/json")

            def reply(self, data, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/stack.html"
        t0, frames, addon_log, shell_log = self.launch(self.args.app, {"url": url}, tag)
        usage = Usage(self.gpu)
        refresh = self.box["refresh_hz"]
        results = {}
        try:
            results["startup"] = self.startup(t0, frames, addon_log)
            started = {}
            while True:
                try:
                    t, event = events.get(timeout=60)
                except Empty:
                    raise BenchError("the stack page stopped reporting") from None
                frames.poll()
                kind, phase = event["event"], event.get("phase")
                if kind == "error":
                    raise BenchError(f"stack page: {event['message']}")
                if kind == "done":
                    break
                if kind == "start":
                    started[phase] = t
                    usage.start()
                    if phase == "keys":
                        sent = []
                        for name in STACK_KEYS:
                            sent.append(self.kodi.key(name))
                            time.sleep(STACK_KEY_INTERVAL)
                        started["keys-sent"] = sent
                elif kind == "end":
                    a = started[phase]
                    m = {k: v for k, v in event.items() if k in METRICS}
                    m.update(usage.stop())
                    if frames.available and phase not in ("script", "decode"):
                        # Skip the first frames, which carry the phase's setup.
                        m.update(frame_metrics(frames, a + 0.2, t, refresh))
                    if phase == "keys":
                        frames.poll()
                        arrivals = key_arrivals(read(shell_log) or "", a)
                        m.update(key_metrics(frames, started["keys-sent"], t, arrivals))
                        if event.get("keys_seen") != len(STACK_KEYS):
                            self.warnings.append(f"{tag}: the page saw {event.get('keys_seen')} of {len(STACK_KEYS)} keys")
                        for k in ("fps", "delivered_pct", "late", "frame_p95_ms", "frame_max_ms"):
                            m.pop(k, None)
                    if phase == "idle":
                        for k in ("delivered_pct", "late", "frame_p95_ms", "frame_max_ms", "pipeline_ms", "raf_p95_ms"):
                            m.pop(k, None)
                    results[phase] = m
            results["end"] = memory()
        finally:
            server.shutdown()
            server.server_close()
            self.finish(tag, frames, addon_log, shell_log)
        if not frames.available:
            self.warnings.append(f"{tag}: this framework has no frame log, so frame timing is missing")
        if measured:
            for case, m in results.items():
                self.record("stack", case, m)

    # ---- app suite

    def app_run(self, scenario, tag, measured):
        app = self.args.app
        t0, frames, addon_log, shell_log = self.launch(app, {"url": self.dev_page} if self.dev_page else {}, tag)
        refresh = self.box["refresh_hz"]
        usage = Usage(self.gpu)
        results = {}
        try:
            start = self.startup(t0, frames, addon_log)
            startup = scenario.get("startup", {})
            marker = startup.get("log")
            if marker:
                t, m = self.wait_log(shell_log, marker, t0, startup.get("timeout", 40))
                if t is None:
                    raise BenchError(f"the app's log never showed {marker!r}")
                require = scenario.get("require")
                if require and not re.search(require["match"], m.group(2)):
                    raise BenchError(require.get("message", f"the app's log did not match {require['match']!r}"))
            settled = frames.wait_quiet(t0, quiet=startup.get("quiet", 1.0), timeout=startup.get("timeout", 40))
            if settled:
                start["settled_ms"] = round(1000 * (settled - t0))
            results["startup"] = start
            time.sleep(scenario.get("pause", 1.0))
            selected = self.cases("app", scenario)
            last_selected = max(i for i, step in enumerate(scenario["steps"]) if step["name"] in selected)
            for step in scenario["steps"][:last_selected + 1]:
                interval = step.get("interval", 0.5)
                usage.start()
                a = time.monotonic()
                sent = []
                for i, name in enumerate(step["keys"]):
                    sent.append(self.kodi.key(name))
                    if i + 1 < len(step["keys"]):
                        time.sleep(interval)
                last = frames.wait_quiet(sent[-1], quiet=step.get("quiet", QUIET), timeout=step.get("timeout", 10))
                b = time.monotonic()
                m = usage.stop()
                frames.poll()
                if frames.available:
                    m.update(frame_metrics(frames, a, b, refresh, bursts=True))
                    m.pop("fps", None)
                    m.pop("delivered_pct", None)
                    m["settle_ms"] = round(1000 * (last - sent[-1])) if last else None
                    if last is None:
                        self.warnings.append(f"{tag}: {step['name']} drew nothing after its last key or never settled")
                arrivals = key_arrivals(read(shell_log) or "", a)
                m.update(key_metrics(frames, sent, b, arrivals))
                if step["name"] in selected:
                    results[step["name"]] = m
                time.sleep(step.get("pause", 0.5))
            results["end"] = memory()
        finally:
            self.finish(tag, frames, addon_log, shell_log)
        if measured:
            for case, m in results.items():
                self.record("app", case, m)

    def cases(self, suite, scenario=None):
        """Return the cases of suite that this run measures."""
        names = suite_cases(suite, scenario)
        return [n for n in names if n in self.args.case] if self.args.case else names

    # ---- driver

    def run(self, scenario):
        RUN.mkdir(parents=True, exist_ok=True)
        # Hold the lock until the process exits.
        self.lock = open(LOCK, "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BenchError("another benchmark is running on this box; bench.py stop ends it") from None
        self.lock.truncate(0)
        self.lock.write(str(os.getpid()))
        self.lock.flush()
        app = self.args.app
        self.env = dict(spec.split("=", 1) for spec in self.args.env or [])
        self.dev_page = None
        if self.args.dev_page:
            dev = json.loads(read(DEV / f"{app}.json") or "{}")
            self.dev_page = dev.get("url")
            if not self.dev_page:
                raise BenchError(f"{DEV / (app + '.json')} sets no development page url")
        self.out.mkdir(parents=True, exist_ok=False)
        self.raw.mkdir()
        self.preflight()
        self.box = box_info(self.kodi)
        software = {"app": app, "app_version": self.kodi.version(app), "page": self.dev_page or "installed"}
        software.update({k: self.kodi.version(v) for k, v in STACK_ADDONS.items()})
        settings = read(ADDON_DATA / FRAMEWORK / "settings.json")
        cpu_rendering = bool(json.loads(settings).get("cpuRendering")) if settings else False
        monitor = Monitor()
        conditions = {"temperature_start_c": temperature(), "cpu_rendering": cpu_rendering,
                      "dev_settings": sorted(p.name for p in DEV.glob("*.json")) if DEV.exists() else []}
        monitor.start()
        try:
            for suite in self.args.suite:
                total = self.args.warmup + self.args.repeat
                for i in range(total):
                    measured = i >= self.args.warmup
                    tag = f"{suite}-{i + 1}{'' if measured else '-warmup'}"
                    log(f"{tag} ({i + 1}/{total})")
                    if suite == "stack":
                        self.stack_run(tag, measured)
                    else:
                        self.app_run(scenario, tag, measured)
        finally:
            monitor.stopped.set()
            self.restore_dev()
        conditions.update({
            "temperature_max_c": max(monitor.temps) if monitor.temps else None,
            "temperature_end_c": temperature(),
            "throttled": monitor.throttled,
        })
        if monitor.throttled:
            self.warnings.append("the CPU clocked below its maximum during the run: the box throttled")
        report = {
            "schema": SCHEMA,
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "label": self.args.label,
            "box": self.box,
            "software": software,
            "conditions": conditions,
            "settings": {
                "suites": self.args.suite, "cases": self.args.case, "repeat": self.args.repeat,
                "warmup": self.args.warmup, "env": self.env,
                "stack_page": hashlib.sha256((HERE / "stack.html").read_bytes()).hexdigest()[:12],
                "scenario": scenario.get("name") if scenario else None,
                "scenario_hash": hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest()[:12]
                if scenario else None,
            },
            "results": summarise(self.results),
            "warnings": self.warnings,
        }
        (self.out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
        (self.out / "report.md").write_text(render(report))
        return report


def suite_cases(suite, scenario):
    if suite == "stack":
        return STACK_CASES
    return [step["name"] for step in scenario["steps"]] if scenario else []


def summarise(results):
    rows = []
    for suite, cases in results.items():
        for case, metrics in cases.items():
            for metric, values in metrics.items():
                unit, better, _ = METRICS.get(metric, ("", "lower", ""))
                rows.append({
                    "suite": suite, "case": case, "metric": metric, "unit": unit, "better": better,
                    "values": values, "median": round(statistics.median(values), 2),
                    "min": min(values), "max": max(values),
                })
    return rows


# ---------------------------------------------------------------- reports


def fmt(value):
    if value is None:
        return "–"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.1f}" if abs(value) < 100 else f"{value:.0f}"
    return f"{int(value)}"


def cell(row):
    if not row:
        return "–"
    if row["min"] == row["max"]:
        return fmt(row["median"])
    return f"{fmt(row['median'])} ({fmt(row['min'])}–{fmt(row['max'])})"


def env_spec(spec):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", spec):
        raise argparse.ArgumentTypeError(f"{spec!r} is not NAME=VALUE")
    return spec


def describe(report):
    box, sw, cond, st = report["box"], report["software"], report["conditions"], report["settings"]
    return [
        ("Box", f"{box.get('model')} ({box.get('soc')}), {box.get('gpu')}"),
        ("CPU", f"{box.get('cpu')}, governor {box.get('governor')}"),
        ("Memory", f"{box.get('memory_mb')} MB"),
        ("System", f"{box.get('os')}, kernel {box.get('kernel')}, Kodi {box.get('kodi')}"),
        ("Display", f"{box.get('display_mode')} ({box.get('refresh_hz')} Hz)"),
        ("App", f"{sw.get('app')} {sw.get('app_version')}, {sw.get('page', 'installed')} page"),
        ("Stack", f"framework {sw.get('framework')}, WebKit {sw.get('webkit')}, display {sw.get('display')}"),
        ("Rendering", "CPU" if cond.get("cpu_rendering") else "GPU"),
        ("Environment", " ".join(f"{k}={v}" for k, v in sorted(st.get("env", {}).items())) or "unchanged"),
        ("Temperature", f"{fmt(cond.get('temperature_start_c'))} °C at start, "
                        f"{fmt(cond.get('temperature_max_c'))} °C peak"
                        + (", throttled" if cond.get("throttled") else "")),
        ("Runs", f"{st.get('repeat')} measured after {st.get('warmup')} warm-up per suite; "
                 f"stack page {st.get('stack_page')}"
                 + (f", scenario {st.get('scenario')} {st.get('scenario_hash')}" if st.get("scenario") else "")),
    ]


def index(report):
    return {(r["suite"], r["case"], r["metric"]): r for r in report["results"]}


def ordered(rows, key):
    order = list(METRICS)
    seen = []
    for r in rows:
        if r[key] not in seen:
            seen.append(r[key])
    return sorted(seen, key=lambda m: order.index(m) if m in order else len(order)) if key == "metric" else seen


def render(report):
    rows = index(report)
    title = f"Benchmark: {report['box'].get('model')}"
    lines = [f"# {title}", "", f"{report['created']}" + (f" · {report['label']}" if report.get("label") else ""), ""]
    lines += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in describe(report)] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    head = [(s, c, m) for s, c, m in HEADLINE if (s, c, m) in rows]
    if head:
        lines += ["## Headline", "", "| Measure | Median (range) | Unit |", "|---|---|---|"]
        for key in head:
            r = rows[key]
            lines.append(f"| {key[0]} {key[1]}: {METRICS[key[2]][2]} | {cell(r)} | {r['unit']} |")
        lines.append("")
    for suite in ("stack", "app"):
        suite_rows = [r for r in report["results"] if r["suite"] == suite]
        if not suite_rows:
            continue
        lines += [f"## {suite.capitalize()} suite", ""]
        cases = ordered(suite_rows, "case")
        for name, columns in TABLES:
            columns = [m for m in columns if any(r["metric"] == m for r in suite_rows)]
            if not columns:
                continue
            lines += [f"### {name}", "",
                      "| Case | " + " | ".join(f"{m} ({METRICS[m][0]})" for m in columns) + " |",
                      "|---|" + "---|" * len(columns)]
            for case in cases:
                cells = [cell(rows.get((suite, case, m))) for m in columns]
                if any(c != "–" for c in cells):
                    lines.append(f"| {case} | " + " | ".join(cells) + " |")
            lines.append("")
    lines += ["## Measures", "", "Each cell is the median over the measured runs, with the lowest and highest run.", ""]
    used = {r["metric"] for r in report["results"]}
    lines += [f"- `{m}` ({u}, {b} is better): {d}" for m, (u, b, d) in METRICS.items() if m in used] + [""]
    return "\n".join(lines)


# Changes smaller than these are reported as unchanged: per unit, then per measure.
# CPU time comes in 10 ms kernel ticks.
FLOOR = {"ms": 2.0, "%": 1.0, "MB": 2.0, "fps": 1.0, "frames": 1.0, "keys": 1.0,
         "webkit_cpu_ms": 50.0, "kodi_cpu_ms": 50.0, "cpu_pct": 2.0, "gpu_pct": 3.0}
RELATIVE = 0.05


def verdict(a, b):
    """Call a change only when it beats the noise floor and the run ranges don't overlap."""
    delta = b["median"] - a["median"]
    floor = FLOOR.get(a["metric"], FLOOR.get(a["unit"], 0))
    if abs(delta) < floor or abs(delta) < RELATIVE * abs(a["median"]):
        return "same"
    if b["min"] <= a["max"] and a["min"] <= b["max"]:
        return "noise"
    improved = delta < 0 if a["better"] == "lower" else delta > 0
    return "better" if improved else "worse"


def regressions(base, new):
    a, b = index(base), index(new)
    return sorted(k for k in set(a) & set(b) if verdict(a[k], b[k]) == "worse")


def latest():
    """Return the newest folder holding a finished report."""
    done = sorted(p.parent for p in RESULTS.glob("*/report.json"))
    if not done:
        raise BenchError(f"no finished reports in {RESULTS}")
    return done[-1]


def stop():
    """End a running benchmark and wait for it to restore the box."""
    if not LOCK.exists():
        return False
    with open(LOCK, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            lock.seek(0)
            os.kill(int(lock.read()), signal.SIGTERM)
        fcntl.flock(lock, fcntl.LOCK_EX)
    return True


def compare(base, new):
    a_rows, b_rows = index(base), index(new)
    lines = ["# Benchmark comparison", ""]
    lines += ["| | Base | New |", "|---|---|---|"]
    for (k, va), (_, vb) in zip(describe(base), describe(new)):
        lines.append(f"| {k} | {va} | {vb}{' ' if va == vb else ' **(differs)**'} |")
    lines.append("")
    if base["box"].get("refresh_hz") != new["box"].get("refresh_hz"):
        lines += ["The display refresh rates differ, so frame rates and frame times are not comparable.", ""]
    if base["settings"].get("stack_page") != new["settings"].get("stack_page"):
        lines += ["The stack pages differ, so the stack workloads changed between these runs.", ""]
    if base["settings"].get("scenario_hash") != new["settings"].get("scenario_hash"):
        lines += ["The app scenarios differ.", ""]
    if base["settings"].get("cases") != new["settings"].get("cases"):
        lines += ["The reports measured different cases. The app suite's end figures come after "
                  "the last step that ran, so they differ with the steps.", ""]
    counts = {"better": 0, "worse": 0, "same": 0, "noise": 0}
    table = []
    for key in sorted(set(a_rows) & set(b_rows), key=lambda k: (k[0] != "stack", ordered_case(base, k), list(METRICS).index(k[2]) if k[2] in METRICS else 99)):
        a, b = a_rows[key], b_rows[key]
        v = verdict(a, b)
        counts[v] += 1
        change = f"{100 * (b['median'] - a['median']) / a['median']:+.0f}%" if a["median"] else "–"
        mark = {"better": "better", "worse": "**worse**", "same": "", "noise": "within noise"}[v]
        table.append(f"| {key[0]} | {key[1]} | {key[2]} ({a['unit']}) | {cell(a)} | {cell(b)} | {change} | {mark} |")
    lines += [f"{counts['better']} better, {counts['worse']} worse, {counts['same']} unchanged, "
              f"{counts['noise']} within the spread of the runs.", ""]
    lines += ["| Suite | Case | Measure | Base | New | Change | |", "|---|---|---|---|---|---|---|"] + table + [""]
    lines += [f"A change counts when the medians differ by at least {RELATIVE:.0%} and by more than "
              "the measure's floor (2 ms for times, 50 ms for CPU time, 1 % for frame rates, 3 % for "
              "GPU load, 2 MB), and the ranges of the runs don't overlap.", ""]
    missing = sorted(set(a_rows) ^ set(b_rows))
    if missing:
        lines += ["Measured in only one report: " + ", ".join(" ".join(k) for k in missing), ""]
    return "\n".join(lines)


def ordered_case(report, key):
    cases = []
    for r in report["results"]:
        if r["suite"] == key[0] and r["case"] not in cases:
            cases.append(r["case"])
    return cases.index(key[1]) if key[1] in cases else 99


def load(path):
    report = json.loads(Path(path).read_text())
    if report.get("schema") != SCHEMA:
        raise BenchError(f"{path}: not an {SCHEMA} report")
    return report


# ---------------------------------------------------------------- main


def interrupt(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--app", help="framework app to open when the scenario names none")
    run.add_argument("--scenario", help="app scenario JSON file, or - for standard input")
    run.add_argument("--suite", action="append", choices=["stack", "app"],
                     help="suite to run; repeat for both (default: stack, and app with a scenario)")
    run.add_argument("--case", action="append",
                     help="measure only this case; repeat for more (default: every case)")
    run.add_argument("--dev-page", action="store_true",
                     help="measure the development page from the app's development settings")
    run.add_argument("--env", action="append", type=env_spec, metavar="NAME=VALUE",
                     help="add a variable to the shell's environment; repeat for more")
    run.add_argument("--list", action="store_true", help="print the cases of each suite and exit")
    run.add_argument("--repeat", type=int, default=5, help="measured runs per suite (default %(default)s)")
    run.add_argument("--warmup", type=int, default=1, help="unmeasured runs first (default %(default)s)")
    run.add_argument("--label", help="free text stored in the report, such as what changed")
    run.add_argument("--out", help="output folder (default /storage/oriel/bench/<time>)")
    sub.add_parser("report").add_argument("report")
    cmp = sub.add_parser("compare")
    cmp.add_argument("base")
    cmp.add_argument("new")
    sub.add_parser("latest")
    sub.add_parser("stop")
    args = parser.parse_args()
    try:
        if args.command == "report":
            print(render(load(args.report)))
        elif args.command == "compare":
            base, new = load(args.base), load(args.new)
            print(compare(base, new))
            if regressions(base, new):
                sys.exit(3)
        elif args.command == "latest":
            print(latest())
        elif args.command == "stop":
            log("stopped the running benchmark" if stop() else "no benchmark is running")
        else:
            scenario = None
            if args.scenario:
                scenario = json.load(sys.stdin if args.scenario == "-" else open(args.scenario))
            suites = {s: suite_cases(s, scenario) for s in ("stack", "app")}
            if args.list:
                for suite, names in suites.items():
                    if names:
                        print(f"{suite}: {' '.join(names)}")
                return
            if args.case:
                unknown = [c for c in args.case if not any(c in names for names in suites.values())]
                if unknown:
                    raise BenchError(f"unknown case {', '.join(unknown)}; --list prints the cases")
                # Without --suite, run the suites that hold the named cases.
                args.suite = args.suite or [s for s, names in suites.items() if set(args.case) & set(names)]
                empty = [s for s in args.suite if not set(args.case) & set(suites[s])]
                if empty:
                    raise BenchError(f"none of the named cases are in the {' or '.join(empty)} suite")
            args.suite = args.suite or (["stack", "app"] if scenario else ["stack"])
            if "app" in args.suite and not scenario:
                raise BenchError("the app suite needs --scenario")
            args.app = (scenario or {}).get("app") or args.app
            if not args.app:
                raise BenchError("name the app to open with --app or the scenario's app field")
            if args.repeat < 1 or args.warmup < 0:
                raise BenchError("--repeat must be at least 1 and --warmup at least 0")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            args.out = args.out or str(RESULTS / stamp)
            # Turn SIGTERM into an exception so dev settings are restored.
            signal.signal(signal.SIGTERM, interrupt)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            Session(args).run(scenario)
            try:
                print(args.out, flush=True)
            except OSError:
                pass
    except (BenchError, RuntimeError, OSError) as e:
        log(f"bench: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("bench: interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
