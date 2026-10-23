import json
from pathlib import Path
import queue
import re
import socket
import sys
import threading
import unittest
from unittest.mock import Mock, call, patch

from .kodi import kodi

REPO = Path(__file__).resolve().parent.parent
# Locate the protocol crate extracted by build/compile.sh.
PROTOCOL = REPO / ".cache/display/protocol"

# Use datagrams on macOS, which lacks SOCK_SEQPACKET. They preserve message
# boundaries but cannot report peer disconnection.
KIND = socket.SOCK_SEQPACKET if sys.platform == "linux" else socket.SOCK_DGRAM


class AddOn:
    """Simulate the display add-on's control socket endpoint."""

    def __init__(self, test, hello=None):
        self.socket, client = socket.socketpair(socket.AF_UNIX, KIND)
        client.settimeout(1)
        test.addCleanup(self.socket.close)
        test.addCleanup(client.close)
        self.socket.settimeout(1)
        self.client = client
        self.hello = {"hello": 1} if hello is None else hello

    def answer_hello(self):
        message = self.receive()
        self.send(**self.hello)
        return message

    def send(self, **message):
        self.socket.send(json.dumps(message).encode())

    def receive(self):
        return json.loads(self.socket.recv(4096))


class DisplayClientTests(unittest.TestCase):
    def setUp(self):
        self.oriel, _, _ = self.enterContext(kodi(display=False))
        self.display = sys.modules["oriel.display"]
        self.events = queue.Queue()

    def connected(self):
        addon = AddOn(self)
        hello = threading.Thread(target=addon.answer_hello)
        hello.start()
        display = self.display.Display(self.events, addon.client)
        hello.join()
        self.addCleanup(display.stop)
        return addon, display

    def event(self):
        return self.events.get(timeout=1)

    def test_windows_keys_and_requests(self):
        addon, display = self.connected()
        display.open("browse", "/mark.png")
        display.open("player")
        display.started()
        display.draw(False)
        self.assertEqual([addon.receive() for _ in range(4)], [
            {"open": "browse", "mark": "/mark.png"}, {"open": "player"}, {"started": True}, {"draw": False}])

        addon.send(window="player", id=14001, state="opened")
        self.assertEqual(self.event(), ("display-window", "player", 14001, "opened"))
        self.assertEqual(display.ids, {"player": 14001})
        addon.send(key=7, t=12.5, kodi=False)
        self.assertEqual(self.event(), ("key", 7, 12.5, False))
        addon.send(key=12, t=13.0, kodi=True)
        self.assertEqual(self.event(), ("key", 12, 13.0, True))

        # A waiting close completes only after the window-closed notification.
        def close_it():
            self.assertEqual(addon.receive(), {"close": "player"})
            addon.send(window="player", state="closed")

        closer = threading.Thread(target=close_it)
        closer.start()
        display.close("player", wait=1)
        closer.join()
        self.assertEqual(display.ids, {})
        self.assertEqual(self.event(), ("display-window", "player", None, "closed"))

    def test_closing_a_window_that_is_not_open_does_not_wait(self):
        addon, display = self.connected()
        with patch.object(self.display, "log") as log:
            display.close("player", wait=5)
        self.assertEqual(addon.receive(), {"close": "player"})
        log.assert_not_called()

    def test_refusal_is_an_error(self):
        addon = AddOn(self, hello={"error": "version 2", "hello": 2})
        threading.Thread(target=addon.answer_hello).start()
        with self.assertRaisesRegex(OSError, "refused"):
            self.display.Display(self.events, addon.client)

    def test_a_different_hello_version_is_refused(self):
        addon = AddOn(self, hello={"hello": 2})
        threading.Thread(target=addon.answer_hello).start()
        with self.assertRaisesRegex(OSError, "refused"):
            self.display.Display(self.events, addon.client)

    def test_close_waits_even_while_a_window_is_opening(self):
        addon, display = self.connected()
        display.open("player")
        self.assertEqual(addon.receive(), {"open": "player"})

        def close_it():
            self.assertEqual(addon.receive(), {"close": "player"})
            addon.send(window="player", id=14001, state="opened")
            addon.send(window="player", state="closed")

        closer = threading.Thread(target=close_it)
        closer.start()
        display.close("player", wait=1)
        self.assertTrue(display.closed["player"].is_set())
        closer.join()

    def test_unloading_add_on_is_gone_but_our_stop_is_not(self):
        addon, display = self.connected()
        addon.send(gone=True)
        self.assertEqual(self.event(), ("display-gone",))
        addon, display = self.connected()
        display.stop()
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=0.2)

    @unittest.skipUnless(KIND == socket.SOCK_SEQPACKET, "needs SOCK_SEQPACKET")
    def test_add_on_closing_the_socket_is_gone(self):
        addon, display = self.connected()
        addon.socket.close()
        self.assertEqual(self.event(), ("display-gone",))

    def test_without_the_add_on_there_is_no_display(self):
        self.assertIsNone(self.display.connect(self.events))
        sys.modules["xbmc"].getCondVisibility.return_value = True
        with patch.object(self.display, "HELLO_TIMEOUT", 0.1), patch.object(self.display, "SOCKET", "\0oriel-test-none"):
            self.assertIsNone(self.display.connect(self.events))


class DisplayModeTests(unittest.TestCase):
    """Exercise apps whose page is drawn by vfs.oriel.display."""

    def setUp(self):
        self.oriel, self.video, _ = self.enterContext(kodi())
        self.client = Mock(ids={})
        self.enterContext(patch.object(self.oriel.display, "connect", return_value=self.client))

    def app(self, cls):
        app = cls()
        app.server.server_close()
        app.shell = Mock()
        app.publish = Mock()
        return app

    def test_app_needs_the_add_on(self):
        with patch.object(self.oriel.display, "connect", return_value=None):
            with self.assertRaises(self.oriel.display.Unavailable):
                self.oriel.App()

    def test_shell_gets_the_control_fifo_and_page(self):
        shell_module = sys.modules["oriel.shell"]
        shell = shell_module.Shell("/dev/null", Mock(), Mock())
        os = shell_module.os
        with patch.object(shell_module.subprocess, "Popen") as popen, patch.object(os, "mkfifo"), \
                patch.object(os, "open"), patch.object(os, "makedirs"), patch.object(os, "access", return_value=True), \
                patch.object(shell_module.threading, "Thread"), patch.object(shell_module, "kill_strays"):
            shell.start("http://page/", {}, "/stage")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--control") + 1], shell_module.config.CTL)
        self.assertEqual(argv[-1], "http://page/")

    def test_page_visibility_goes_to_the_add_on(self):
        app = self.app(self.oriel.App)
        app.page_ready()
        self.client.started.assert_called_once_with()
        self.assertEqual(app.phase, "browse")
        xbmcgui = sys.modules["xbmcgui"]
        with patch.object(xbmcgui, "getCurrentWindowDialogId", Mock(return_value=10100), create=True):
            app.check_covered()
        with patch.object(xbmcgui, "getCurrentWindowDialogId", Mock(return_value=9999), create=True):
            app.check_covered()
        self.assertEqual(self.client.draw.call_args_list, [((False,),), ((True,),)])
        app.shell.send.assert_not_called()

    def test_window_id_comes_from_the_add_on(self):
        app = self.app(self.oriel.App)
        app.phase = "browse"
        xbmcgui = sys.modules["xbmcgui"]
        with patch.object(xbmcgui, "getCurrentWindowId", Mock(return_value=10000), create=True):
            # Do not treat the unopened window as an app exit.
            for _ in range(5):
                app.check_away()
            self.assertFalse(app.exiting)
            app.handle("display-window", ["browse", 14000, "opened"])
            self.assertEqual(app.main_window_id, 14000)
            for _ in range(4):
                app.check_away()
        self.assertTrue(app.exiting)

    def test_close_stops_the_shell_then_the_add_on(self):
        app = self.app(self.oriel.App)
        order = Mock()
        order.attach_mock(app.shell.stop, "shell_stop")
        order.attach_mock(self.client.stop, "display_stop")
        app.close()
        self.assertEqual([c[0] for c in order.mock_calls], ["shell_stop", "display_stop"])

    def test_close_leaves_no_dead_window_behind(self):
        xbmcgui, xbmc = sys.modules["xbmcgui"], sys.modules["xbmc"]
        for current, back in ((14000, True), (12005, True), (10025, False)):
            with self.subTest(current=current):
                xbmcgui.getCurrentWindowId.return_value = 10000
                app = self.app(self.oriel.App)
                app.main_window_id = 14000
                xbmcgui.getCurrentWindowId.return_value = current
                xbmc.executebuiltin.reset_mock()
                app.close()
                calls = [c.args[0] for c in xbmc.executebuiltin.call_args_list if "ActivateWindow" in c.args[0]]
                self.assertEqual(calls, ["ActivateWindow(10000)"] if back else [])

    def test_add_on_going_away_closes_the_app(self):
        for event in (("display-gone",), ("shell-exit", self.oriel.SHELL_DISPLAY_GONE)):
            with self.subTest(event=event):
                app = self.app(self.oriel.App)
                app.handle(event[0], list(event[1:]))
                self.assertTrue(app.exiting)
                app.shell.start.assert_not_called()

    def test_playback_dialog_is_the_add_ons_player_window(self):
        app = self.app(self.video.VideoApp)
        app.player = Mock()
        app.phase = "loading"
        app.on_started()
        self.client.open.assert_called_once_with("player")
        self.assertFalse(app.ours(14001))
        self.client.ids["player"] = 14001
        self.assertTrue(app.ours(14001))
        self.assertFalse(app.ours(10100))

        calls = Mock()
        calls.attach_mock(self.client.close, "close")
        with patch.object(self.video.xbmc, "executebuiltin", calls.builtin):
            app.stop_playback("test")
        # Require dialog closure before the Kodi stop call.
        self.assertEqual(calls.mock_calls[0], call.close("player", wait=2))
        self.assertIn(call.builtin("PlayerControl(Stop)"), calls.mock_calls)
        self.assertFalse(app.ours(14001))
        app.close_osd()
        self.client.close.assert_called_once()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(kodi(display=False))
        self.display = sys.modules["oriel.display"]

    def crate(self):
        """Read the protocol crate's lib.rs for contract checks."""
        try:
            return (PROTOCOL / "src/lib.rs").read_text()
        except FileNotFoundError:
            self.skipTest("no protocol crate; build/compile.sh unpacks it")

    def test_control_socket_and_version_match_the_crate(self):
        lib = self.crate()
        socket_name = re.search(r'^pub const CONTROL_SOCKET: &\[u8\] = b"(.*)";$', lib, re.M)[1]
        version = re.search(r"^pub const CONTROL_VERSION: u32 = (\d+);$", lib, re.M)[1]
        self.assertEqual(self.display.SOCKET, "\0" + socket_name)
        self.assertEqual(self.display.VERSION, int(version))
