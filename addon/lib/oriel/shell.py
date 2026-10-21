"""Runs oriel-shell. Its output goes to shell.log with URL tokens blanked."""
import os
import re
import signal
import subprocess
import threading
import time

from . import config

TOKEN = re.compile(r"([\w-]*token(?:=|%3D))[^&\s\"'#]+", re.IGNORECASE)


def kill_strays(log):
    """Kills shells left over from a crash."""
    for pid in os.listdir("/proc"):
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "oriel-shell":
                    continue
            os.kill(int(pid), signal.SIGKILL)
            log("killed a stray oriel-shell, pid", pid)
        except (OSError, ValueError):
            pass


class Shell:
    def __init__(self, log_path, on_exit, log):
        self.log_path = log_path
        self.on_exit = on_exit
        self.log = log
        self.process = None
        self.ctl = None

    def start(self, url, env, stage, append_log=False):
        kill_strays(self.log)
        # Kodi unzips the add-on without the execute bit.
        if not os.access(config.SHELL, os.X_OK):
            os.chmod(config.SHELL, 0o755)
        os.makedirs(os.path.dirname(config.CTL), exist_ok=True)
        self.log_mode = "a" if append_log else "w"
        if self.ctl is not None:
            os.close(self.ctl)
            self.ctl = None
        if os.path.exists(config.CTL):
            os.unlink(config.CTL)
        os.mkfifo(config.CTL, 0o600)
        # Kodi's LD_LIBRARY_PATH and Python paths must not reach WebKit.
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/storage",
            "XDG_RUNTIME_DIR": "/var/run",
            **env,
        }
        argv = [
            "/usr/bin/python3", config.OVERLAY, stage, config.SHELL,
            "--control", config.CTL,
            "--feature", "PropagateDamagingInformation=1", url,
        ]
        self.process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
            # So a kill reaches WebKit's processes too.
            start_new_session=True)
        # Read-write, so opening doesn't block until the shell reads.
        self.ctl = os.open(config.CTL, os.O_RDWR | os.O_NONBLOCK)
        threading.Thread(target=self.read, name="oriel-shell-out", daemon=True).start()
        self.log("shell started, pid", self.process.pid, "url", url)

    def read(self):
        with open(self.log_path, self.log_mode, buffering=1, encoding="utf-8") as out:
            for raw in self.process.stdout:
                line = TOKEN.sub(r"\1…", raw.decode("utf-8", "replace").rstrip())
                out.write(f"{time.monotonic():.6f} {line}\n")
        self.on_exit(self.process.wait())

    def send(self, line):
        if self.ctl is None:
            return
        try:
            os.write(self.ctl, (line + "\n").encode())
        except OSError as e:
            self.log("ctl write failed:", e)

    def key(self, code, sent):
        self.send(f"key {code} {sent:.6f}")

    def stop(self):
        if not self.process:
            return
        self.send("quit")
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.log("shell did not quit; killing it")
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            kill_strays(self.log)
        if self.ctl is not None:
            os.close(self.ctl)
            self.ctl = None
