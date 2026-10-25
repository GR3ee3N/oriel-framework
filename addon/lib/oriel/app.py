"""Run a web page inside Kodi using App or video.VideoApp.

Subclass the app and call run() from default.py. Override on_start,
on_stop, on_key, state, poll, on_event and ours; register page commands
with @command.

vfs.oriel.display draws the page in Kodi windows and forwards their keys
to the shell through display.py. Kodi callbacks queue events without
waiting. One controller thread owns app state and makes Kodi calls to
prevent callbacks from blocking on the controller.
"""
import inspect
import json
import os
import queue
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

import xbmc
import xbmcaddon
import xbmcgui

from . import config, display, webkit
from .log import close_log, log, open_log
from .server import Server, failure
from .shell import Shell

# Map remote key names to (Kodi actions, page evdev code). xkb/ maps codes
# WebKit does not recognize to DOM keys listed in web/src/keys.ts.
# Kodi routes colour buttons to other windows and maps channel keys to
# page up/down, so neither has a separate page action.
KEYS = {
    "left": ((1,), 105),
    "right": ((2,), 106),
    "up": ((3,), 103),
    "down": ((4,), 108),
    "select": ((7,), 28),  # Enter
    "back": ((10, 92), 1),  # Previous menu and Back map to Escape.
    "info": ((11,), 59),  # F1
    "menu": ((117,), 139),  # Context menu.
    "playpause": ((12, 79, 229), 164),  # Pause, Play and Play/Pause.
    "stop": ((13,), 166),
    "next": ((14,), 163),
    "previous": ((15,), 165),
    "fastforward": ((77,), 208),
    "rewind": ((78,), 168),
    "pageup": ((5,), 104),
    "pagedown": ((6,), 109),
    # Remote digits use Number0/1 (58/59) and JumpSMS2-9 (142-149).
    # Keyboard digits use Number0-9 (58-67).
    "0": ((58,), 11),
    "1": ((59,), 2),
    **{str(n): ((58 + n, 140 + n), 1 + n) for n in range(2, 10)},
}
KEY_NAMES = {action: name for name, (actions, _) in KEYS.items() for action in actions}
# Kodi executes built-in keymap commands such as RunAddon without page input.
ACTION_BUILT_IN_FUNCTION = 122

KODI_VIDEO_WINDOWS = (12005, 12901)

# Volume, notification, seek and busy dialogs leave input with the page.
# Other dialogs take input and require the page to be hidden.
PASSIVE_DIALOGS = (9999, 10104, 10107, 10115, 10138, 10151, 10160)

RESTARTS = 2
RESTART_WINDOW = 300
# Shell exit status for display disconnection or protocol refusal.
SHELL_DISPLAY_GONE = 4

# Let the page clear before hiding it so stale content cannot flash on return.
HIDE_DELAY = 0.2

# Command response deadline in seconds.
COMMAND_TIMEOUT = 10


def now():
    return time.monotonic()


def command(name, log=False):
    """Register a page command handler with fields as keyword arguments and a
    JSON-serializable return value as the result. log=True includes fields
    in logs; keep it disabled for commands carrying secrets.
    """

    def register(method):
        method.oriel_command = (name, log)
        return method

    return register


class CommandError(Exception):
    """Refuse a command with a message and an invalid or unavailable code.

    Return the refusal to the page and log it without a traceback.
    """

    def __init__(self, message, code="unavailable"):
        if code not in ("invalid", "unavailable"):
            raise ValueError(f"not a refusal: {code}")
        super().__init__(message)
        self.code = code


class Reply:
    """Pass command results from the controller thread to the HTTP thread."""

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
        # Accept Other.System.OnSleep as a manually sent sleep event for tests.
        if method in ("System.OnSleep", "Other.System.OnSleep"):
            self.events.put(("sleep",))


class App:
    # True suppresses Kodi's screensaver and publishes its delay for the page.
    # False hides the page during Kodi's screensaver and publishes a null delay.
    own_screensaver = False

    def __init__(self):
        self.data_dir = config.data_dir()
        # Identifies this app run; a reloaded page or restarted WebKit keeps it.
        self.run_id = uuid.uuid4().hex
        self.stage = webkit.ensure()
        self.events = queue.Queue()
        self.exiting = False
        self.phase = "starting"
        self.away = 0
        self.covered = False
        self.main_window_id = None
        # Retain the Kodi window to restore when the app closes.
        self.start_window_id = xbmcgui.getCurrentWindowId()
        self.screensaver = screensaver_minutes() if self.own_screensaver else None
        self.settings = config.load_settings()
        self.inhibited = None
        self.restarts = []
        # Distinguish a settings-triggered shell restart from app shutdown.
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
        # Release the single display connection if startup fails.
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
        """Initialize app state before loading the page."""

    def on_stop(self):
        """Release app resources after the shell and page have exited."""

    def on_key(self, key):
        """Handle a named key from KEYS before VideoApp and the page; return True to keep it from both."""
        return False

    def page_shown(self):
        """Return False to withhold page keys while VideoApp hides the page."""
        return True

    def state(self):
        """Return page state; subclasses extend the base fields."""
        return {
            "run": self.run_id,
            "cpuRendering": self.settings.get("cpuRendering", False),
            "screensaver": self.screensaver,
        }

    def poll(self):
        """Update app state on the controller's 250 ms poll."""

    def on_event(self, kind, args):
        """Handle an app-defined event from the controller queue."""
        log("ignored event", kind)

    def ours(self, dialog):
        """Return True for an app-owned dialog that should leave the page visible."""
        return False

    def start(self):
        self.on_start()
        self.server.start()
        self.publish()
        self.shell.start(self.shell_url, self.shell_env(), self.stage)
        threading.Thread(target=self.tick, name="oriel-tick", daemon=True).start()

    def shell_env(self):
        """Build the WebKit environment from shared and development settings."""
        env = {}
        if self.dev.get("inspector"):
            env["WEBKIT_INSPECTOR_HTTP_SERVER"] = self.dev["inspector"]
        if self.settings.get("cpuRendering", False):
            env["WEBKIT_SKIA_ENABLE_CPU_RENDERING"] = "1"
        env.update(self.dev.get("env", {}))
        return env

    def tick(self):
        monitor = Monitor(self.events)
        # This thread keeps the Kodi script interpreter alive.
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
            # Close the display connection even if app cleanup fails.
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
        """Queue an HTTP command for the controller and wait for its status/body reply."""
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
        # WebKit reads rendering settings at startup; restart after sending the reply.
        self.events.put(("restart-shell",))

    def on_action(self, action_id, sent, kodi):
        key = KEY_NAMES.get(action_id)
        log(f"action {action_id} ({key}) in {self.phase}, queued {(now() - sent) * 1e3:.1f} ms")
        if action_id == ACTION_BUILT_IN_FUNCTION:
            return
        named = key is not None and self.phase != "starting"
        if named and self.on_key(key) or self.intercept_key(key, sent, kodi):
            return
        if named and self.page_shown():
            self.shell.key(KEYS[key][1], sent)

    def intercept_key(self, key, sent, kodi):
        """Let VideoApp consume a key before on_key and page delivery.

        Unnamed keys use None; kodi indicates that Kodi also handles the action.
        """
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
        """Treat navigation away from the app's Kodi window as an exit."""
        # Use the browse window id reported by vfs.oriel.display.
        if self.covered or self.main_window_id is None:
            self.away = 0
            return
        current = xbmcgui.getCurrentWindowId()
        if current in KODI_VIDEO_WINDOWS or xbmc.getCondVisibility("Player.HasMedia"):
            # Allow Kodi time to leave fullscreen video after playback stops.
            self.away = 0
        elif current != self.main_window_id:
            self.away += 1
            if self.away >= 4:
                log("Kodi left our window for", current)
                self.close()
        else:
            self.away = 0

    def check_covered(self):
        """Hide the page while a blocking Kodi dialog or screensaver is visible."""
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
        # Remove the startup development fragment so a reload does not repeat it.
        self.shell.start(self.shell_url.split("#")[0], self.shell_env(), self.stage, append_log=True)

    def close(self):
        if self.exiting:
            return
        self.exiting = True
        log("closing")
        self.shell.stop()
        self.display.stop()
        # Restore a Kodi window with active content before the app window closes.
        current = xbmcgui.getCurrentWindowId()
        if current == self.main_window_id or current in KODI_VIDEO_WINDOWS:
            xbmc.executebuiltin(f"ActivateWindow({self.start_window_id})")
        # Restore Kodi even if an app shutdown hook fails.
        try:
            self.on_stop()
        except Exception:
            log("on_stop failed")
            log(traceback.format_exc())
        self.inhibit(False)


def jsonrpc(method, **params):
    """Return the Kodi JSON-RPC result, or None when absent."""
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
        # Kodi invokes callbacks for objects created here, including VideoApp's
        # Player, only during Kodi calls made by this thread.
        while controller.is_alive():
            xbmc.sleep(100)
    except display.Unavailable as e:
        log("failed:", e)
        xbmc.executebuiltin(f"Notification({xbmcaddon.Addon().getAddonInfo('name')},{e},8000)")
    except Exception as e:
        log("failed:", repr(e))
        raise
    finally:
        # Run controller cleanup even when Kodi has already closed the app window.
        if controller:
            app.events.put(("abort",))
            controller.join(5)
        elif app:
            app.close()
        if app:
            app.server.stop()
        log("done, Kodi window", xbmcgui.getCurrentWindowId())
        close_log()
