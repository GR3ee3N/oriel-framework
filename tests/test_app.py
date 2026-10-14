import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .kodi import kodi


class AppTests(unittest.TestCase):
    def setUp(self):
        self.oriel, _, self.directory = self.enterContext(kodi())
        self.log_path = self.directory / "addon.log"
        self.oriel.open_log(self.log_path)
        self.addCleanup(self.oriel.close_log)
        oriel = self.oriel

        class App(oriel.App):
            count = 0
            fail_stop = False

            def state(self):
                return {**super().state(), "count": self.count}

            def on_key(self, key):
                return key == "info"

            def on_stop(self):
                if self.fail_stop:
                    raise ValueError("app bug")

            @oriel.command("add")
            def add(self, amount):
                self.count += amount
                self.publish()
                return {"count": self.count}

            @oriel.command("refuse")
            def refuse(self):
                raise oriel.CommandError("no playback")

            @oriel.command("fail")
            def fail(self):
                raise ValueError("broken")

            @oriel.command("opaque")
            def opaque(self):
                return object()

            @oriel.command("slow")
            def slow(self):
                self.slow_release.wait(3)

        self.app = App()
        self.app.shell = Mock()
        self.app.slow_release = threading.Event()
        self.app.server.start()
        self.addCleanup(self.app.server.stop)
        self.controller = threading.Thread(target=self.app.loop, daemon=True)
        self.controller.start()
        self.addCleanup(self.stop_controller)
        self.base = f"http://127.0.0.1:{self.app.server.server_address[1]}"

    def stop_controller(self):
        self.app.slow_release.set()
        self.app.events.put(("abort",))
        self.controller.join(5)
        self.assertFalse(self.controller.is_alive(), "controller did not stop")

    def request(self, path, body=None, **headers):
        try:
            response = urlopen(Request(self.base + path, body, headers), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            data = json.load(response) if response.headers["Content-Type"] == "application/json" else None
            return response.status, data

    def cmd(self, **command):
        return self.request("/api/cmd", json.dumps(command).encode())

    def test_commands_and_initial_event(self):
        self.assertEqual(set(self.app.commands),
                         {"ready", "exit", "hdr-white", "cpu-rendering", "add", "refuse", "fail", "opaque", "slow"})
        self.assertEqual(self.cmd(cmd="add", amount=2), (200, {"result": {"count": 2}}))
        with urlopen(f"{self.base}/api/events", timeout=3) as events:
            self.assertEqual(events.readline(), b"event: state\n")
            self.assertEqual(json.loads(events.readline().removeprefix(b"data: "))["count"], 2)
        self.assertEqual(self.cmd(cmd="ready"), (200, {"result": None}))

    def test_failures_keep_their_codes(self):
        self.assertEqual(self.cmd(cmd="nope"), (400, {"error": "unknown command nope", "code": "invalid"}))
        self.assertEqual(self.cmd(cmd="refuse"), (409, {"error": "no playback", "code": "unavailable"}))
        self.assertEqual(self.cmd(cmd="fail"), (500, {"error": "broken", "code": "failed"}))
        status, reply = self.cmd(cmd="opaque")
        self.assertEqual((status, reply["code"]), (500, "failed"))
        self.assertTrue(reply["error"].startswith("the result is not JSON"))

    def test_invalid_requests_change_nothing(self):
        bodies = ('[]', '{"cmd": 1}', '{"cmd": "add"}', '{"cmd": "add", "amount": 1, "by": 2}',
                  '{"cmd": "add", "amount": 1e309}', '{"cmd": "add", "amount": NaN}')
        for body in bodies:
            with self.subTest(body=body):
                status, reply = self.request("/api/cmd", body.encode())
                self.assertEqual((status, reply["code"]), (400, "invalid"))
        self.assertEqual(self.app.count, 0)

    def test_hdr_setting_survives_shell_restart(self):
        self.assertEqual(self.cmd(cmd="hdr-white", nits=2000), (400, {"error": "nits must be 80 to 1000", "code": "invalid"}))
        self.assertEqual(self.cmd(cmd="hdr-white", nits=300), (200, {"result": None}))
        self.app.shell.send.assert_called_once_with("hdr-white 300")
        self.app.on_shell_exit(1)
        self.assertEqual(self.app.shell.start.call_args.args[1]["ORIEL_HDR_WHITE"], "300")
        self.assertEqual(self.app.server.state["hdrWhite"], 300)
        self.assertEqual(json.loads((self.directory / "settings.json").read_text()), {"hdrWhite": 300})

    def test_cpu_rendering_restarts_the_shell_with_it(self):
        self.assertEqual(self.cmd(cmd="cpu-rendering", on="yes"), (400, {"error": "on must be true or false", "code": "invalid"}))
        self.assertEqual(self.cmd(cmd="cpu-rendering", on=False), (200, {"result": None}))
        self.app.shell.stop.assert_not_called()

        self.assertEqual(self.cmd(cmd="cpu-rendering", on=True), (200, {"result": None}))
        self.assertTrue(self.app.server.state["cpuRendering"])
        self.assertEqual(json.loads((self.directory / "settings.json").read_text()), {"cpuRendering": True})
        deadline = time.monotonic() + 3
        while not self.app.shell.stop.called and time.monotonic() < deadline:
            time.sleep(0.01)
        self.app.shell.stop.assert_called_once()
        # The stopped shell exits cleanly; that starts it again rather than closing the app.
        self.app.on_shell_exit(0)
        self.assertFalse(self.app.exiting)
        self.assertEqual(self.app.restarts, [])
        self.assertEqual(self.app.shell.start.call_args.args[1]["WEBKIT_SKIA_ENABLE_CPU_RENDERING"], "1")

        self.assertEqual(self.cmd(cmd="cpu-rendering", on=True), (200, {"result": None}))
        self.assertEqual(self.app.shell.stop.call_count, 1)
        self.assertEqual(self.cmd(cmd="cpu-rendering", on=False), (200, {"result": None}))
        deadline = time.monotonic() + 3
        while self.app.shell.stop.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.app.on_shell_exit(0)
        self.assertNotIn("WEBKIT_SKIA_ENABLE_CPU_RENDERING", self.app.shell.start.call_args.args[1])
        self.assertFalse(self.app.exiting)

    def test_foreign_origins_and_hosts_cannot_change_store(self):
        self.assertEqual(self.request("/api/store", b'{"token": "t"}')[0], 200)
        for origin in ("http://foreign.example", "null"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("/api/store", b'{"token": null}', Origin=origin)[0], 403)
        self.assertEqual(self.request("/api/cmd", b'{"cmd": "add", "amount": 1}', Origin="http://foreign.example")[0], 403)
        self.assertEqual(self.request("/api/store", Host=f"foreign.example:{self.app.server.server_address[1]}")[0], 403)
        self.assertEqual(self.request("/api/store", Origin=self.base)[0], 200)
        for body in (b'{"token": Infinity}', b'{"token": 1e309}'):
            self.assertEqual(self.request("/api/store", body)[0], 400)
        self.assertEqual(json.loads((self.directory / "store.json").read_text()), {"token": "t"})
        self.app.server.origin = "http://dev.example:5173"
        self.assertEqual(self.request("/api/cmd", b'{"cmd": "add", "amount": 1}', Origin=self.app.server.origin)[0], 200)

    def test_only_opted_in_command_fields_are_logged(self):
        self.cmd(cmd="add", amount=2)
        self.cmd(cmd="refuse")
        self.cmd(cmd="fail")
        self.cmd(cmd="hdr-white", nits=300)
        text = self.log_path.read_text()
        self.assertIn(" command add\n", text)
        self.assertNotIn("'amount': 2", text)
        self.assertIn(" command hdr-white {'nits': 300}\n", text)
        self.assertIn(" command refuse refused: no playback\n", text)
        self.assertEqual(text.count("Traceback"), 1)

    def test_keys_are_filtered_before_forwarding(self):
        self.cmd(cmd="ready")
        sent = self.oriel.now()
        for action in (3, 144, 11, self.oriel.ACTION_BUILT_IN_FUNCTION):
            self.app.on_action(action, sent)
        self.app.page_shown = lambda: False
        self.app.on_action(4, sent)
        self.assertEqual(self.app.shell.key.call_args_list, [((103, sent),), ((5, sent),)])

    def test_command_timeout_has_a_distinct_code(self):
        with patch.object(self.oriel, "COMMAND_TIMEOUT", 0.05):
            self.assertEqual(self.cmd(cmd="slow"), (504, {"error": "no reply in time", "code": "timeout"}))
        self.app.slow_release.set()

    def test_failing_stop_hook_still_closes_window(self):
        self.app.fail_stop = True
        with patch("builtins.open", side_effect=OSError("no fb1")):
            self.assertEqual(self.cmd(cmd="exit"), (200, {"result": None}))
        self.app.main.close.assert_called_once()
        self.assertIn(" on_stop failed\n", self.log_path.read_text())

    def test_tick_stops_once_the_app_exits(self):
        waits = lambda monitor, timeout: time.sleep(0.01) or False
        with patch.object(self.oriel.Monitor, "waitForAbort", waits, create=True):
            tick = threading.Thread(target=self.app.tick, daemon=True)
            tick.start()
            self.assertEqual(self.cmd(cmd="exit"), (200, {"result": None}))
            tick.join(1)
        self.assertFalse(tick.is_alive(), "tick thread outlived the app")
