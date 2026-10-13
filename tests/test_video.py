import copy
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .kodi import kodi


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.oriel, self.video, _ = self.enterContext(kodi())

    def app_in(self, phase):
        app = self.video.VideoApp()
        app.server.server_close()
        app.phase = phase
        app.player = Mock()
        app.publish = Mock()
        app.hide_later = Mock()
        app.show_plane = Mock()
        app.close_osd = Mock()
        app.stop_playback = Mock()
        return app

    def test_video_commands_inherit_base_commands(self):
        app = self.app_in("browse")
        self.assertEqual(set(app.commands), {"ready", "exit", "hdr-white", "media-source", "play", "osd-on", "osd-off",
                                             "set-paused", "stop", "seek-to", "audio", "subtitle"})

    def test_audio_change_preserves_subtitle_choice(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                tracks = {"currentsubtitle": {"index": 3}, "subtitleenabled": enabled}
                app = self.app_in("osd")
                app.player.setAudioStream.side_effect = lambda index: tracks.update(
                    currentsubtitle={"index": 0}, subtitleenabled=True)
                app.player.setSubtitleStream.side_effect = lambda index: tracks.update(currentsubtitle={"index": index})
                app.player.showSubtitles.side_effect = lambda on: tracks.update(subtitleenabled=on)
                self.video.jsonrpc = lambda *args, **kwargs: copy.deepcopy(tracks)
                app.page_audio(1)
                self.assertEqual(tracks["subtitleenabled"], enabled)
                if enabled:
                    self.assertEqual(tracks["currentsubtitle"]["index"], 3)

    def test_replacements_preserve_pause_only_in_same_session(self):
        for state, previous, session, plays, paused in (
            ("paused", "same", "same", 1, True), ("paused", "same", "same", 2, True),
            ("playing", "same", "same", 1, False), ("playing", "same", "same", 2, False),
            ("paused", "old", "new", 1, False), ("paused", None, None, 1, False),
        ):
            with self.subTest(state=state, previous=previous, session=session, plays=plays):
                app = self.app_in("osd")
                app.player_state = self.video.player_state(state, session=previous)
                app.media_url = lambda path: path
                with patch.object(self.video.threading, "Thread"):
                    for _ in range(plays):
                        app.page_play("/movie.mkv", session=session, position=120, streamOffset=120, subtitle=-1)
                    app.on_started()
                self.assertEqual(app.player.pause.called, paused)
                self.assertEqual(app.phase, "osd" if paused else "playing")
                self.assertEqual(app.show_plane.called, paused)
                self.assertEqual(app.player.play.call_count, plays)

    def test_pause_sets_desired_state(self):
        for paused in (True, False):
            with self.subTest(paused=paused):
                self.video.jsonrpc = Mock(return_value={"speed": 0})
                self.app_in("osd").page_set_paused(paused)
                self.video.jsonrpc.assert_called_once_with("Player.PlayPause", playerid=1, play=not paused)

    def test_transcode_clock_and_seeks_use_movie_time(self):
        app = self.app_in("browse")
        app.media_url = lambda path: path
        app.player.getTime.return_value = 30
        app.player.getTotalTime.return_value = 0
        with patch.object(self.video.threading, "Thread"):
            app.page_play("/transcode.mkv", session="one", position=600, streamOffset=600, duration=5400)
            app.on_started()
        app.poll()
        self.assertEqual((app.player_state["time"], app.player_state["duration"]), (630, 5400))
        app.page_seek_to(660)
        app.player.seekTime.assert_called_once_with(60)

    def test_commands_succeed_when_state_already_holds(self):
        for phase, command in (("osd", "page_osd_on"), ("playing", "page_osd_off"), ("browse", "page_stop")):
            with self.subTest(phase=phase, command=command):
                app = self.app_in(phase)
                getattr(app, command)()
                self.assertEqual(app.phase, phase)
                app.publish.assert_not_called()
                app.stop_playback.assert_not_called()

    def test_refusals_keep_invalid_and_unavailable_distinct(self):
        for phase, command, args, code in (
            ("browse", "page_set_paused", (True,), "unavailable"),
            ("osd", "page_set_paused", (1,), "invalid"),
            ("browse", "page_osd_on", (), "unavailable"), ("browse", "page_osd_off", (), "unavailable"),
        ):
            with self.subTest(phase=phase, command=command, args=args):
                with self.assertRaises(self.oriel.CommandError) as raised:
                    getattr(self.app_in(phase), command)(*args)
                self.assertEqual(raised.exception.code, code)

    def test_bad_play_values_change_nothing(self):
        for fields in ({"audio": 2**31}, {"subtitle": -2}, {"position": -1}, {"duration": "long"}, {"path": 42}):
            with self.subTest(fields=fields):
                app = self.app_in("osd")
                app.player_state = self.video.player_state("playing", 30, 600, "s")
                app.media_url = lambda path: path
                with self.assertRaises(self.oriel.CommandError) as raised:
                    app.page_play(**{"path": "/movie.mkv", **fields})
                self.assertEqual(raised.exception.code, "invalid")
                self.assertEqual((app.phase, app.player_state["state"]), ("osd", "playing"))
                app.close_osd.assert_not_called()
                app.player.play.assert_not_called()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        _, self.video, _ = self.enterContext(kodi())
        processes = self.enterContext(ExitStack())
        spawn = subprocess.Popen
        self.enterContext(patch.object(subprocess, "Popen", side_effect=lambda *args, **kwargs:
                                       processes.enter_context(spawn(*args, **kwargs))))

    def proxy(self, port):
        proxy = self.video.ProxyProcess(port, lambda *args: None)
        self.addCleanup(proxy.stop)
        return proxy

    def test_taken_port_refuses_start(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            proxy = self.proxy(taken.getsockname()[1])
            with self.assertRaises(RuntimeError):
                proxy.set_source("http://127.0.0.1:1", {})
            self.assertIsNone(proxy.process)

    def source(self):
        """A media source that records each request's path and X-Token; its base URL and the record."""
        seen = []

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers["X-Token"]))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        self.addCleanup(source.server_close)
        threading.Thread(target=source.serve_forever, daemon=True).start()
        self.addCleanup(source.shutdown)
        return f"http://127.0.0.1:{source.server_address[1]}/base", seen

    def free_port(self):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            return free.getsockname()[1]

    def test_source_is_applied_before_ack_and_invalid_source_keeps_previous(self):
        base, seen = self.source()
        port = self.free_port()
        proxy = self.proxy(port)
        proxy.set_source(base, {"X-Token": "t"})

        def fetch():
            with urlopen(f"http://127.0.0.1:{port}/file.mkv", timeout=3) as response:
                self.assertEqual(response.read(), b"ok")

        fetch()
        self.assertEqual(seen, [("/base/file.mkv", "t")])
        for bad, headers in (("/no/scheme", {}), ("ftp://source.example", {}),
                             ("http://127.0.0.1:99999", {}), (base, ["X-Token", "t"])):
            with self.subTest(base=bad, headers=headers), self.assertRaises(ValueError):
                proxy.set_source(bad, headers)
        fetch()
        self.assertEqual(seen[-1], ("/base/file.mkv", "t"))

    def test_requests_not_from_kodi_get_no_tokens(self):
        base, seen = self.source()
        port = self.free_port()
        self.proxy(port).set_source(base, {"X-Token": "t"})
        for headers in ({"Host": f"rebound.example:{port}"}, {"Host": "127.0.0.1:1"},
                        {"Origin": "http://rebound.example"}):
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as refused:
                    urlopen(Request(f"http://127.0.0.1:{port}/file.mkv", headers=headers), timeout=3)
                refused.exception.close()
                self.assertEqual(refused.exception.code, 403)
        self.assertEqual(seen, [])
        with urlopen(f"http://localhost:{port}/file.mkv", timeout=3) as response:
            self.assertEqual(response.read(), b"ok")
        self.assertEqual(seen, [("/base/file.mkv", "t")])

    def test_proxy_dying_on_source_is_not_success(self):
        proxy = self.proxy(0)
        proxy.process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        with self.assertRaises(RuntimeError):
            proxy.set_source("http://127.0.0.1:1", {})
        self.assertIsNone(proxy.process)
