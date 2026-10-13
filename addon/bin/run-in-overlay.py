#!/usr/bin/env python3
"""Runs a command with WebKit's usr/ overlaid on /usr, which WebKit's
compiled-in paths need, in a private mount namespace.

    run-in-overlay.py <usr-dir> <command> [args...]
"""

import ctypes
import os
import signal
import subprocess
import sys

MS_RDONLY = 1
MS_REC = 0x4000
MS_PRIVATE = 1 << 18

libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p] * 3 + [ctypes.c_ulong, ctypes.c_char_p]


def mount(source, target, fstype, flags, data):
    if libc.mount(source, target, fstype, flags, data) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"mount {target}: {os.strerror(err)}")


def main():
    stage, argv = sys.argv[1], sys.argv[2:]
    subprocess.run(["modprobe", "overlay"], check=False)

    os.unshare(os.CLONE_NEWNS)
    mount(None, b"/", None, MS_REC | MS_PRIVATE, None)
    opts = f"lowerdir={stage}:/usr".encode()
    mount(b"overlay", b"/usr", b"overlay", MS_RDONLY, opts)

    env = dict(os.environ)
    env.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")
    env.setdefault("XKB_CONFIG_EXTRA_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xkb"))
    # libproxy deadlocked the UI process when the inspector server started early.
    env.setdefault("GIO_USE_PROXY_RESOLVER", "dummy")
    env.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")

    # A pty keeps WebKit's console output line-buffered.
    pid, fd = os.forkpty()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
    signal.signal(signal.SIGTERM, lambda *_: os.kill(pid, signal.SIGTERM))
    while True:
        try:
            data = os.read(fd, 65536)
        except InterruptedError:
            continue
        except OSError:
            break
        if not data:
            break
        sys.stdout.buffer.write(data.replace(b"\r\n", b"\n"))
        sys.stdout.flush()
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
