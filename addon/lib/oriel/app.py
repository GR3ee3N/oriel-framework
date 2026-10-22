"""An Oriel app: a web page over the running Kodi.

Subclass App (or video.VideoApp) and call run() from default.py. Hooks:
on_start, on_stop, on_key, state, poll, on_event and ours; @command
registers the page's commands.

Kodi draws the page in its own GUI, in vfs.oriel.display's windows
(display.py), and keeps the remote: those windows' keys come from the
add-on and go on to the shell. Kodi callbacks only queue events; one
controller thread owns the state and makes every Kodi call, since no
callback may wait on us.
"""
import inspect
import json
import os
import queue
import threading
import time
import traceback
from urllib.parse import urlsplit

import xbmc
import xbmcaddon
import xbmcgui

from . import config, display, webkit
from .log import close_log, log, open_log
from .server import Server, failure
from .shell import Shell

# The remote's keys: name -> (Kodi actions, evdev code for the page). The
# page sees them as DOM keys (web/src/nav/keys.ts); xkb/ remaps those WebKit
# doesn't know. Kodi's default keymaps send the colour buttons to other
# windows, and channel up/down as page up/down, so they aren't here.
KEYS = {
    "left": ((1,), 105),
    "right": ((2,), 106),
    "up": ((3,), 103),
    "down": ((4,), 108),
    "select": ((7,), 28),  # Enter
    "back": ((10, 92), 1),  # previous menu, back -> Escape
    "info": ((11,), 59),  # F1
    "menu": ((117,), 139),  # context menu
    "playpause": ((12, 79, 229), 164),  # pause, play, play/pause
    "stop": ((13,), 166),
    "next": ((14,), 163),
    "previous": ((15,), 165),
    "fastforward": ((77,), 208),
    "rewind": ((78,), 168),
    "pageup": ((5,), 104),
    "pagedown": ((6,), 109),
    # The remote keymap sends 0 and 1 as Number0/1 (58/59) and 2-9 as
    # JumpSMS2-9 (142-149); the keyboard keymap sends Number0-9 (58-67).
    "0": ((58,), 11),
    "1": ((59,), 2),
    **{str(n): ((58 + n, 140 + n), 1 + n) for n in range(2, 10)},
}
KEY_NAMES = {action: name for name, (actions, _) in KEYS.items() for action in actions}
# A keymap command (RunAddon and such); Kodi runs it, it's not our key.
ACTION_BUILT_IN_FUNCTION = 122

KODI_VIDEO_WINDOWS = (12005, 12901)

# Dialogs that leave the keys to us (volume, notifications, seek bar, busy).
# Any other dialog takes them, so the page is hidden while it's up.
PASSIVE_DIALOGS = (9999, 10104, 10107, 10115, 10138, 10151, 10160)

RESTARTS = 2
RESTART_WINDOW = 300
# The shell's status when vfs.oriel.display went away or refused it.
SHELL_DISPLAY_GONE = 4

# Lets the page clear itself first, so its old screen doesn't flash when it shows again.
HIDE_DELAY = 0.2

# How long the page waits for a command's reply.
COMMAND_TIMEOUT = 10


def now():
    return time.monotonic()


def command(name, log=False):
    """Makes an App method the handler of the page's command `name`. The
    command's fields are its keyword arguments, its return value (JSON) the
    reply's result. log=True also logs the fields; leave it off for secrets."""

    def register(method):
        method.oriel_command = (name, log)
        return method

    return register


class CommandError(Exception):
    """Raise from a command handler to refuse it: the page gets the message
    and code (server.FAILURES: "invalid" or "unavailable") as the error, the
    log a line without a traceback."""

    def __init__(self, message, code="unavailable"):
        if code not in ("invalid", "unavailable"):
            raise ValueError(f"not a refusal: {code}")
        super().__init__(message)
        self.code = code


class Reply:
    """A command's reply, from the controller thread to the HTTP thread."""

    def __init__(self):
        self.done = threading.Event()
        self.result = failure("timeout", "no reply in time")

    def send(self, status, body):
        self.result = (status, body)
        self.done.set()


class Monitor(xbmc.Monitor):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def onNotification(self, sender, method, data):
        # Other.System.OnSleep: the same, sent by hand for testing.
        if method in ("System.OnSleep", "Other.System.OnSleep"):
            self.events.put(("sleep",))


class App:
    # True: Kodi's screensaver is held off and the page runs its own after
    # Kodi's delay (`screensaver` in the state). False: Kodi's runs, over
    # the hidden page, and `screensaver` is null.
    own_screensaver = False

    def __init__(self):
        self.data_dir = config.data_dir()
        self.stage = webkit.ensure()
        self.events = queue.Queue()
        self.exiting = False
        self.phase = "starting"
        self.away = 0
        self.covered = False
        self.main_window_id = None
        # Where Kodi was when the app started.
        self.start_window_id = xbmcgui.getCurrentWindowId()
        self.screensaver = screensaver_minutes() if self.own_screensaver else None
        self.settings = config.load_settings()
        self.inhibited = None
        self.restarts = []
        # The shell was stopped to start again with new settings, not to close.
        self.restarting = False
        self.commands = {}
        for cls in reversed(type(self).__mro__):
            for attr, value in vars(cls).items():
                if hasattr(value, "oriel_command"):
                    name, logged = value.oriel_command
                    self.commands[name] = (getattr(self, attr), logged)
        self.dev = config.dev()
        url = self.dev.get("url")
        self.shell_url = url or f"http://127.0.0.1:{config.PORT}/"
        addon_path = xbmcaddon.Addon().getAddonInfo("path")
        mark = os.path.join(addon_path, "resources", "mark.png")
        self.mark = mark if os.path.isfile(mark) else None
        self.display = display.connect(self.events)
        if not self.display:
            raise display.Unavailable()
        # vfs.oriel.display takes one client at a time: let go of it if the app can't start.
        try:
            self.server = Server(
                config.PORT, os.path.join(addon_path, "resources", "web"), os.path.join(self.data_dir, "store.json"),
                self.page_command, "{0.scheme}://{0.netloc}".format(urlsplit(url)) if url else None)
            self.shell = Shell(
                os.path.join(self.data_dir, "shell.log"), lambda code: self.events.put(("shell-exit", code)), log)
        except Exception:
            self.display.stop()
            raise

    def on_start(self):
        """Called before the page loads."""

    def on_stop(self):
        """Called on close, after the shell quit and the page is gone."""

    def on_key(self, key):
        """A key for the page (a name in KEYS). Return True to keep it from the page."""
        return False

    def page_shown(self):
        """For VideoApp: False while it hides the page; keys then don't go to it."""
        return True

    def state(self):
        """The page's state; extend it."""
        return {
            "cpuRendering": self.settings.get("cpuRendering", False),
            "screensaver": self.screensaver,
        }

    def poll(self):
        """Called four times a second."""

    def on_event(self, kind, args):
        """An event the app queued itself."""
        log("ignored event", kind)

    def ours(self, dialog):
        """Return True if the top dialog is the app's own."""
        return False

    def start(self):
        self.on_start()
        self.server.start()
        self.publish()
        self.shell.start(self.shell_url, self.shell_env(), self.stage)
        threading.Thread(target=self.tick, name="oriel-tick", daemon=True).start()

    def shell_env(self):
        """WebKit's environment, from the current settings."""
        env = {}
        if self.dev.get("inspector"):
            env["WEBKIT_INSPECTOR_HTTP_SERVER"] = self.dev["inspector"]
        if self.settings.get("cpuRendering", False):
            env["WEBKIT_SKIA_ENABLE_CPU_RENDERING"] = "1"
        env.update(self.dev.get("env", {}))
        return env

    def tick(self):
        monitor = Monitor(self.events)
        # Kodi keeps the script's interpreter alive while this thread runs.
        while not self.exiting and not monitor.waitForAbort(0.25):
            self.events.put(("tick",))
        self.events.put(("abort",))

    def publish(self):
        self.server.publish(self.state())

    def show_page(self):
        if not self.covered:
            self.display.draw(True)

    def hide_later(self):
        threading.Timer(HIDE_DELAY, self.events.put, (("hide",),)).start()

    def run(self):
        try:
            self.loop()
        finally:
            # Never strand our window over Kodi.
            if not self.exiting:
                log("controller died")
                self.close()

    def loop(self):
        while True:
            kind, *args = self.events.get()
            try:
                self.handle(kind, args)
            except Exception:
                log("error handling", kind, args if kind != "page" else args[0]["cmd"])
                log(traceback.format_exc())
            if self.exiting:
                return

    def handle(self, kind, args):
        if kind not in ("tick", "page"):
            log("event", kind, *args)
        if kind == "key":
            self.on_action(*args)
        elif kind == "page":
            self.on_page(*args)
        elif kind == "tick":
            self.on_tick()
        elif kind == "hide":
            if not self.page_shown():
                self.display.draw(False)
        elif kind == "shell-exit":
            self.on_shell_exit(*args)
        elif kind == "restart-shell":
            self.restarting = True
            self.shell.stop()
        elif kind == "sleep":
            self.close()
        elif kind == "display-window":
            self.on_display_window(*args)
        elif kind == "display-gone":
            log("vfs.oriel.display went away")
            self.close()
        elif kind == "abort":
            self.close()
        else:
            self.on_event(kind, args)

    def page_command(self, command):
        """From an HTTP thread: runs a page command on the controller thread
        and returns its (status, body)."""
        reply = Reply()
        self.events.put(("page", command, reply))
        reply.done.wait(COMMAND_TIMEOUT)
        return reply.result

    def on_page(self, command, reply):
        fields = dict(command)
        name = fields.pop("cmd")
        if name not in self.commands:
            log("command", name, "unknown")
            return reply.send(*failure("invalid", f"unknown command {name}"))
        handler, logged = self.commands[name]
        log("command", name, *([fields] if logged else []))
        try:
            inspect.signature(handler).bind(**fields)
        except TypeError as e:
            log("command", name, "refused:", e)
            return reply.send(*failure("invalid", str(e)))
        try:
            result = handler(**fields)
        except CommandError as e:
            log("command", name, "refused:", e)
            return reply.send(*failure(e.code, str(e)))
        except Exception as e:
            log("command", name, "failed")
            log(traceback.format_exc())
            return reply.send(*failure("failed", str(e) or type(e).__name__))
        reply.send(200, {"result": result})

    def on_display_window(self, name, window_id, state):
        if name == "browse" and state == "opened" and self.main_window_id is None:
            self.main_window_id = window_id

    @command("ready")
    def page_ready(self):
        self.display.started()
        if self.phase == "starting":
            self.phase = "browse"

    @command("exit")
    def page_exit(self):
        self.close()

    @command("cpu-rendering", log=True)
    def page_cpu_rendering(self, on):
        if not isinstance(on, bool):
            raise CommandError("on must be true or false", "invalid")
        if self.settings.get("cpuRendering", False) == on:
            return
        self.settings["cpuRendering"] = on
        config.save_settings(self.settings)
        self.publish()
        # WebKit reads it at start: restart the shell after this reply.
        self.events.put(("restart-shell",))

    def on_action(self, action_id, sent, kodi):
        key = KEY_NAMES.get(action_id)
        log(f"action {action_id} ({key}) in {self.phase}, queued {(now() - sent) * 1e3:.1f} ms")
        if action_id == ACTION_BUILT_IN_FUNCTION or self.intercept_key(key, sent, kodi):
            return
        if key and self.phase != "starting" and self.page_shown() and not self.on_key(key):
            self.shell.key(KEYS[key][1], sent)

    def intercept_key(self, key, sent, kodi):
        """For VideoApp: True keeps the key (None if unnamed) from on_key and the
        page. kodi: Kodi also acts on it."""
        return False

    def inhibit(self, on):
        if on != self.inhibited:
            self.inhibited = on
            xbmc.executebuiltin(f"InhibitScreensaver({'true' if on else 'false'})")

    def on_tick(self):
        self.inhibit(self.phase == "browse" and self.screensaver is not None)
        self.check_covered()
        self.poll()
        if self.phase == "browse":
            self.check_away()

    def check_away(self):
        """Kodi leaving our window, e.g. for Home, counts as exit."""
        # Our window's id comes from vfs.oriel.display when the window opens.
        if self.covered or self.main_window_id is None:
            self.away = 0
            return
        current = xbmcgui.getCurrentWindowId()
        if current in KODI_VIDEO_WINDOWS or xbmc.getCondVisibility("Player.HasMedia"):
            # Kodi can sit in fullscreen video for a second after we stop playback.
            self.away = 0
        elif current != self.main_window_id:
            self.away += 1
            if self.away >= 4:
                log("Kodi left our window for", current)
                self.close()
        else:
            self.away = 0

    def check_covered(self):
        """Hides the page while a Kodi dialog or the screensaver is up."""
        if self.phase == "starting":
            return
        dialog = xbmcgui.getCurrentWindowDialogId()
        covered = xbmc.getCondVisibility("System.ScreenSaverActive") or (
            dialog not in PASSIVE_DIALOGS and not self.ours(dialog))
        if covered == self.covered:
            return
        self.covered = covered
        log("covered by Kodi" if covered else "uncovered", "dialog", dialog)
        if covered:
            self.display.draw(False)
        elif self.page_shown():
            self.show_page()

    def on_shell_exit(self, code):
        if self.restarting:
            self.restarting = False
            log("shell stopped with", code, "- starting it with the new settings")
            self.shell.start(self.shell_url.split("#")[0], self.shell_env(), self.stage, append_log=True)
            return
        t = now()
        self.restarts = [r for r in self.restarts if t - r < RESTART_WINDOW]
        if code == SHELL_DISPLAY_GONE:
            log("vfs.oriel.display refused the shell or went away - closing")
            return self.close()
        if code == 0 or len(self.restarts) >= RESTARTS:
            log("shell ended with", code, "- closing")
            return self.close()
        self.restarts.append(t)
        log("shell died with", code, "- starting it again")
        # Drop the dev hook (#play=…); it already ran.
        self.shell.start(self.shell_url.split("#")[0], self.shell_env(), self.stage, append_log=True)

    def close(self):
        if self.exiting:
            return
        self.exiting = True
        log("closing")
        self.shell.stop()
        self.display.stop()
        # Kodi would come back to our window, which nothing draws now.
        current = xbmcgui.getCurrentWindowId()
        if current == self.main_window_id or current in KODI_VIDEO_WINDOWS:
            xbmc.executebuiltin(f"ActivateWindow({self.start_window_id})")
        # The app's bug must not leave Kodi behind our window.
        try:
            self.on_stop()
        except Exception:
            log("on_stop failed")
            log(traceback.format_exc())
        self.inhibit(False)


def jsonrpc(method, **params):
    """Kodi's JSON-RPC from Python: the result, or None."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(request))).get("result")
    except ValueError:
        return None


def kodi_setting(name):
    return (jsonrpc("Settings.GetSettingValue", setting=name) or {}).get("value")


def screensaver_minutes():
    if not kodi_setting("screensaver.mode"):
        return None
    return kodi_setting("screensaver.time") or None


def run(cls):
    open_log(os.path.join(config.data_dir(), "addon.log"))
    log("start", xbmcaddon.Addon().getAddonInfo("id"), xbmcaddon.Addon().getAddonInfo("version"),
        "Kodi window", xbmcgui.getCurrentWindowId())
    app = controller = None
    try:
        app = cls()
        app.start()
        controller = threading.Thread(target=app.run, name="oriel-controller", daemon=True)
        controller.start()
        app.display.open("browse", app.mark)
        # Kodi calls back objects made on this thread (VideoApp's Player)
        # only from within its own calls on it.
        while controller.is_alive():
            xbmc.sleep(100)
    except display.Unavailable as e:
        log("failed:", e)
        xbmc.executebuiltin(f"Notification({xbmcaddon.Addon().getAddonInfo('name')},{e},8000)")
    except Exception as e:
        log("failed:", repr(e))
        raise
    finally:
        # If Kodi closed our window, the controller still has to clean up.
        if controller:
            app.events.put(("abort",))
            controller.join(5)
        elif app:
            app.close()
        if app:
            app.server.stop()
        log("done, Kodi window", xbmcgui.getCurrentWindowId())
        close_log()
