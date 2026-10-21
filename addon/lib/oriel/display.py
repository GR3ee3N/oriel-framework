"""The client of vfs.oriel.display, which draws the page in Kodi's own GUI
(oriel-display's docs/protocol.md, the control socket). Its windows show the
page and hand us their keys; the shell sends it the page's frames.

The reader thread queues events for the controller: ("key", action, t, kodi),
("display-window", name, id, state) and ("display-gone",).
"""
import json
import socket
import threading

import xbmc

from .log import log

ADDON = "vfs.oriel.display"
SOCKET = "\0oriel-display-control"
VERSION = 1
# The add-on takes one client at a time; a stuck one would keep us waiting.
HELLO_TIMEOUT = 2


class Unavailable(Exception):
    def __init__(self):
        super().__init__("Oriel display is not available")


def available():
    return xbmc.getCondVisibility(f"System.AddonIsEnabled({ADDON})")


def connect(events):
    """A Display, or None when the add-on isn't there to draw the page."""
    if not available():
        log(f"{ADDON} is not enabled")
        return None
    connection = None
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.settimeout(HELLO_TIMEOUT)
        connection.connect(SOCKET)
        return Display(events, connection)
    except (OSError, ValueError) as e:
        if connection:
            connection.close()
        log(f"{ADDON}: {e}")
        return None


class Display:
    def __init__(self, events, connection):
        """connection: the control socket, connected; the hello may time out."""
        self.events = events
        self.stopping = False
        # Kodi's id of each open window, kept by the reader thread.
        self.ids = {}
        self.closed = {"browse": threading.Event(), "player": threading.Event()}
        for closed in self.closed.values():
            closed.set()
        self.socket = connection
        self.send(hello=VERSION)
        reply = json.loads(self.socket.recv(4096))
        if not isinstance(reply, dict) or reply.get("hello") != VERSION or "error" in reply:
            raise OSError(f"refused: {reply}")
        self.socket.settimeout(None)
        threading.Thread(target=self.read, name="oriel-display", daemon=True).start()
        log(f"drawing through {ADDON}")

    def send(self, **message):
        try:
            self.socket.send(json.dumps(message).encode())
        except OSError as e:
            log(f"{ADDON}: send failed:", e)

    def open(self, which, mark=None):
        self.closed[which].clear()
        self.send(**{"open": which, **({"mark": mark} if which == "browse" else {})})

    def close(self, which, wait=0):
        """Closes the window; waits up to `wait` seconds for Kodi to have closed it."""
        self.send(close=which)
        if wait and not self.closed[which].wait(wait):
            log(f"the {which} window did not close")

    def started(self):
        self.send(started=True)

    def draw(self, on):
        self.send(draw=on)

    def stop(self):
        """Disconnects, which closes the windows."""
        self.stopping = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

    def read(self):
        while True:
            try:
                data = self.socket.recv(4096)
            except OSError:
                data = b""
            if not data:
                for closed in self.closed.values():
                    closed.set()
                if not self.stopping:
                    self.events.put(("display-gone",))
                return
            try:
                message = json.loads(data)
                if not isinstance(message, dict):
                    raise ValueError("expected an object")
            except ValueError:
                log(f"{ADDON}: not JSON:", data[:100])
                continue
            if "key" in message:
                self.events.put(("key", message["key"], message["t"], message.get("kodi", False)))
            elif "window" in message:
                name, state = message["window"], message.get("state")
                if name not in self.closed or state not in ("opened", "closed"):
                    log(f"{ADDON}: unknown window event:", message)
                    continue
                if state == "opened":
                    self.ids[name] = message.get("id")
                else:
                    self.ids.pop(name, None)
                    self.closed[name].set()
                self.events.put(("display-window", name, message.get("id"), state))
            elif message.get("gone"):
                self.stopping = True
                self.events.put(("display-gone",))
            elif "error" in message:
                log(f"{ADDON}:", message["error"], message.get("message", ""))
