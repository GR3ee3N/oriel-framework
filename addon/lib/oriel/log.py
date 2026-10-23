"""Write addon.log using the monotonic timestamps shared with shell.log."""
import time

log_file = None


def open_log(path):
    global log_file
    # Specify UTF-8 because Kodi's Python locale is ASCII.
    log_file = open(path, "w", encoding="utf-8")


def close_log():
    global log_file
    if log_file:
        log_file.close()
        log_file = None


def log(*args):
    if not log_file:
        return
    line = " ".join(str(a) for a in args)
    log_file.write(f"{time.monotonic():.6f} {line}\n")
    log_file.flush()
