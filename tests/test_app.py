import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .kodi import kodi


class Harness(unittest.TestCase):
    """An app with its page server and controller running."""

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


class AppTests(Harness):
    def test_commands_and_initial_event(self):
        self.assertEqual(set(self.app.commands),
                         {"ready", "exit", "cpu-rendering", "forward-source", "add", "refuse", "fail", "opaque", "slow"})
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
        # A clean exit after a requested settings restart must relaunch the shell.
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

    def test_files_are_served_from_the_files_directory_only(self):
        files = self.directory / "files" / "fonts"
        files.mkdir(parents=True)
        (files / "a.css").write_text("body {}")
        self.app.server.origin = "http://dev.example:5173"
        response = urlopen(Request(f"{self.base}/files/fonts/a.css", headers={"Origin": self.app.server.origin}), timeout=3)
        with response:
            self.assertEqual(response.read(), b"body {}")
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.app.server.origin)
        for path in ("/files/fonts/missing.css", "/files/fonts", "/files/../store.json", "/files/%2e%2e/addon.log"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_only_opted_in_command_fields_are_logged(self):
        self.cmd(cmd="add", amount=2)
        self.cmd(cmd="refuse")
        self.cmd(cmd="fail")
        self.cmd(cmd="cpu-rendering", on=False)
        text = self.log_path.read_text()
        self.assertIn(" command add\n", text)
        self.assertNotIn("'amount': 2", text)
        self.assertIn(" command cpu-rendering {'on': False}\n", text)
        self.assertIn(" command refuse refused: no playback\n", text)
        self.assertEqual(text.count("Traceback"), 1)

    def test_keys_are_filtered_before_forwarding(self):
        self.cmd(cmd="ready")
        sent = self.oriel.now()
        for action in (3, 144, 11, self.oriel.ACTION_BUILT_IN_FUNCTION):
            self.app.on_action(action, sent, False)
        self.app.page_shown = lambda: False
        self.app.on_action(4, sent, False)
        self.assertEqual(self.app.shell.key.call_args_list, [((103, sent),), ((5, sent),)])

    def test_digit_actions_reach_the_page_as_digits(self):
        self.cmd(cmd="ready")
        sent = self.oriel.now()
        # Cover Number0/1 (58/59), keyboard Number2 (60) and remote JumpSMS2 (142).
        for action in (58, 59, 60, 142):
            self.app.on_action(action, sent, False)
        self.assertEqual(self.app.shell.key.call_args_list,
                         [((11, sent),), ((2, sent),), ((3, sent),), ((3, sent),)])

    def test_command_timeout_has_a_distinct_code(self):
        with patch.object(self.oriel, "COMMAND_TIMEOUT", 0.05):
            self.assertEqual(self.cmd(cmd="slow"), (504, {"error": "no reply in time", "code": "timeout"}))
        self.app.slow_release.set()

    def test_failing_stop_hook_still_closes_window(self):
        self.app.fail_stop = True
        self.assertEqual(self.cmd(cmd="exit"), (200, {"result": None}))
        self.app.display.stop.assert_called_once()
        self.assertIn(" on_stop failed\n", self.log_path.read_text())

    def test_failed_start_lets_go_of_the_display(self):
        connection = Mock()
        with patch.object(self.oriel.display, "connect", return_value=connection), \
                patch.object(self.oriel, "Server", side_effect=OSError("port taken")), \
                self.assertRaises(OSError):
            self.oriel.App()
        connection.stop.assert_called_once()

    def test_tick_stops_once_the_app_exits(self):
        waits = lambda monitor, timeout: time.sleep(0.01) or False
        with patch.object(self.oriel.Monitor, "waitForAbort", waits, create=True):
            tick = threading.Thread(target=self.app.tick, daemon=True)
            tick.start()
            self.assertEqual(self.cmd(cmd="exit"), (200, {"result": None}))
            tick.join(1)
        self.assertFalse(tick.is_alive(), "tick thread outlived the app")


class Upstream(BaseHTTPRequestHandler):
    """A server that answers with what it received."""

    def answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/silo/moved"):
            self.send_response(302)
            self.send_header("Location", "/silo/api/here")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        reply = json.dumps({"method": self.command, "path": self.path, "body": body.decode(),
                            "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"v1"')
        self.send_header("Set-Cookie", "session=1")
        self.send_header("Access-Control-Allow-Origin", "https://elsewhere.example")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = answer

    def log_message(self, format, *args):
        pass


class ForwardTests(Harness):
    def setUp(self):
        super().setUp()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.source = f"http://127.0.0.1:{self.upstream.server_address[1]}/silo/"

    def forward(self, path, method="GET", body=None, **headers):
        class Stay(HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        try:
            response = build_opener(Stay).open(Request(self.base + path, body, headers, method=method), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            json_reply = response.headers["Content-Type"] == "application/json" and data
            return response.status, response.headers, json.loads(data) if json_reply else None

    def test_requests_reach_the_source_with_their_method_query_body_and_headers(self):
        self.assertEqual(self.cmd(cmd="forward-source", base=self.source), (200, {"result": None}))
        status, headers, echo = self.forward(
            "/api/forward/api/v2/items?limit=5", "POST", b'{"a": 1}',
            Authorization="Bearer secret", **{"Content-Type": "application/json", "X-Profile-Id": "p1",
                                              "Cookie": "c=1", "Origin": self.base})
        self.assertEqual((status, echo["method"], echo["path"], echo["body"]), (201, "POST", "/silo/api/v2/items?limit=5", '{"a": 1}'))
        self.assertEqual(echo["headers"]["authorization"], "Bearer secret")
        self.assertEqual(echo["headers"]["x-profile-id"], "p1")
        self.assertEqual(echo["headers"]["host"], f"127.0.0.1:{self.upstream.server_address[1]}")
        for dropped in ("cookie", "origin"):
            self.assertNotIn(dropped, echo["headers"])
        self.assertEqual(headers["ETag"], '"v1"')
        self.assertIsNone(headers["Set-Cookie"])
        self.assertIsNone(headers["Access-Control-Allow-Origin"])
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.forward("/api/forward/x", method, b"{}")[2]["method"], method)
        status, headers, data = self.forward("/api/forward/x", "HEAD")
        self.assertEqual((status, data), (201, None))
        self.assertNotIn("Authorization", self.log_path.read_text())

    def test_redirects_within_the_source_stay_on_the_forward_route(self):
        self.cmd(cmd="forward-source", base=self.source)
        status, headers, _ = self.forward("/api/forward/moved")
        self.assertEqual((status, headers["Location"]), (302, "/api/forward/api/here"))

    def test_failures_are_marked_apart_from_the_servers_replies(self):
        status, headers, reply = self.forward("/api/forward/x")
        self.assertEqual((status, headers["Oriel-Forward"], reply["code"]), (409, "unavailable", "unavailable"))
        self.cmd(cmd="forward-source", base="http://127.0.0.1:9/")
        status, headers, reply = self.forward("/api/forward/x")
        self.assertEqual((status, headers["Oriel-Forward"], reply["code"]), (502, "unreachable", "unreachable"))
        self.cmd(cmd="forward-source", base=None)
        self.assertEqual(self.forward("/api/forward/x")[0], 409)

    def test_bad_sources_are_refused_and_keep_the_previous_one(self):
        self.cmd(cmd="forward-source", base=self.source)
        for base in ("ftp://host/", "http://user:pw@host/", "http://host/?q=1", "/relative", 5):
            with self.subTest(base=base):
                self.assertEqual(self.cmd(cmd="forward-source", base=base)[1]["code"], "invalid")
        self.assertEqual(self.forward("/api/forward/x")[0], 201)

    def test_only_the_page_and_development_server_may_forward(self):
        self.cmd(cmd="forward-source", base=self.source)
        self.assertEqual(self.forward("/api/forward/x", Origin="http://foreign.example")[0], 403)
        self.assertEqual(self.forward("/api/forwarded", "PUT", b"{}")[0], 404)
        self.assertEqual(self.forward("/api/forward/x", "OPTIONS")[0], 403)
        self.app.server.origin = "http://dev.example:5173"
        status, headers, _ = self.forward("/api/forward/x", "OPTIONS", Origin=self.app.server.origin,
                                          **{"Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "authorization, content-type"})
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (204, self.app.server.origin))
        self.assertEqual(headers["Access-Control-Allow-Headers"], "authorization, content-type")
        status, headers, _ = self.forward("/api/forward/x", Origin=self.app.server.origin)
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (201, self.app.server.origin))
        self.assertEqual(headers["Access-Control-Expose-Headers"], "*")
