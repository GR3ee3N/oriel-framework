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


class FakeKodi:
    """Kodi's VideoPlayer as far as tracks go. The player's methods queue
    their switches, applied later and in order, as Kodi does; each audio or
    embedded subtitle switch seeks. forced maps forced subtitles to their
    language; languages are the audio tracks'."""

    def __init__(self, audio, subtitle, enabled, forced=None, languages=()):
        self.audio, self.subtitle, self.enabled = audio, subtitle, enabled
        # Whether the subtitle's packets reach the player.
        self.demuxed = enabled and subtitle is not None
        self.forced = forced or {}
        self.languages = list(languages)
        self.queue = []
        self.seeks = 0
        self.play, self.pause = Mock(), Mock()

    def properties(self, method, playerid, properties):
        subtitle = {} if self.subtitle is None else {"index": self.subtitle, "isforced": self.subtitle in self.forced}
        return {"currentaudiostream": {"index": self.audio}, "currentsubtitle": subtitle,
                "subtitleenabled": self.enabled,
                "subtitles": [{"index": index, "isforced": index in self.forced} for index in range(4)]}

    def setAudioStream(self, index):
        self.queue.append(("audio", index))

    def setSubtitleStream(self, index):
        # The binding shows it too.
        self.queue += [("subtitle", index), ("visible", True)]

    def showSubtitles(self, visible):
        self.queue.append(("visible", visible))

    def setSubtitles(self, path):
        self.queue.append(("file", path))

    def process(self):
        for kind, value in self.queue:
            if kind == "audio":
                self.audio = value
                self.seeks += 1
                # AdaptForcedSubtitles
                if self.subtitle in self.forced:
                    language = self.languages[value] if value < len(self.languages) else None
                    match = [index for index, forced in sorted(self.forced.items()) if forced == language]
                    if match:
                        self.subtitle, self.demuxed = match[0], True
                    self.enabled = bool(match)
                    self.demuxed = self.demuxed and self.enabled
            elif kind == "subtitle":
                self.subtitle, self.demuxed = value, True
                self.seeks += 1
            elif kind == "visible":
                # Showing doesn't turn the stream back on in the demuxer.
                self.enabled = value
                self.demuxed = self.demuxed and value
        self.queue = []

    def shown(self):
        """The subtitle on screen, or None."""
        return self.subtitle if self.enabled and self.demuxed else None


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
        self.assertEqual(set(app.commands), {"ready", "exit", "hdr-white", "cpu-rendering", "media-source", "play",
                                             "osd-on", "osd-off", "set-paused", "stop", "seek-to", "audio", "subtitle"})

    def tracks_app(self, phase, kodi):
        app = self.app_in(phase)
        app.media_url = lambda path: path
        app.player = kodi
        self.video.jsonrpc = kodi.properties
        return app

    def start(self, kodi, **streams):
        app = self.tracks_app("loading", kodi)
        app.start_streams = streams
        with patch.object(self.video.threading, "Thread"):
            app.on_started()
        kodi.process()
        self.assertEqual(app.phase, "playing")
        return app

    def test_start_switches_only_tracks_that_differ(self):
        for audio, subtitle, enabled, want_audio, want_subtitle, seeks in (
            (1, 2, True, 1, 2, 0), (1, 2, True, None, None, 0), (1, 2, True, 1, -1, 0),
            (0, 2, True, 1, 2, 1), (1, 0, True, 1, 2, 1), (0, 0, True, 1, 2, 2),
            # Kodi selected the track but hid it: it must be switched on in the demuxer.
            (1, 2, False, 1, 2, 1),
        ):
            with self.subTest(audio=audio, subtitle=subtitle, enabled=enabled,
                              want_audio=want_audio, want_subtitle=want_subtitle):
                kodi = FakeKodi(audio, subtitle, enabled)
                self.start(kodi, audio=want_audio, subtitle=want_subtitle)
                self.assertEqual(kodi.audio, audio if want_audio is None else want_audio)
                if want_subtitle is None:
                    self.assertEqual(kodi.shown(), subtitle if enabled else None)
                else:
                    self.assertEqual(kodi.shown(), None if want_subtitle < 0 else want_subtitle)
                self.assertEqual(kodi.seeks, seeks)

    def test_start_with_new_audio_overrides_forced_replacement(self):
        for want in (0, 2, -1):
            with self.subTest(want=want):
                kodi = FakeKodi(0, 0, True, forced={0: "en", 1: "de"}, languages=["en", "de"])
                self.start(kodi, audio=1, subtitle=want)
                self.assertEqual((kodi.audio, kodi.shown()), (1, None if want < 0 else want))

    def test_audio_change_keeps_subtitle_choice(self):
        for subtitle, enabled, forced, seeks in (
            (2, True, {}, 1), (None, False, {}, 1),
            # Kodi swaps a forced subtitle when audio changes, even a hidden one.
            (0, True, {0: "en", 1: "de"}, 2), (0, False, {0: "en", 1: "de"}, 1),
            (0, True, {0: "en"}, 2), (0, False, {0: "en"}, 1),
        ):
            with self.subTest(subtitle=subtitle, enabled=enabled, forced=forced):
                kodi = FakeKodi(0, subtitle, enabled, forced=forced, languages=["en", "de"])
                self.tracks_app("osd", kodi).page_audio(1)
                kodi.process()
                self.assertEqual(kodi.audio, 1)
                self.assertEqual(kodi.shown(), subtitle if enabled else None)
                self.assertEqual(kodi.seeks, seeks)

    def test_reselecting_the_audio_track_does_nothing(self):
        kodi = FakeKodi(1, 0, True, forced={0: "en"}, languages=["en", "de"])
        self.tracks_app("osd", kodi).page_audio(1)
        self.assertEqual(kodi.queue, [])

    def test_subtitle_commands(self):
        for subtitle, enabled, commands, shown, seeks in (
            (2, True, [{"index": 2}], 2, 0), (2, True, [{"index": 3}], 3, 1),
            (2, True, [{"index": -1}], None, 0), (2, True, [{}], None, 0),
            (2, True, [{"index": -1}, {"index": 2}], 2, 1), (2, False, [{"index": 2}], 2, 1),
        ):
            with self.subTest(subtitle=subtitle, enabled=enabled, commands=commands):
                kodi = FakeKodi(0, subtitle, enabled)
                app = self.tracks_app("osd", kodi)
                for args in commands:
                    app.page_subtitle(**args)
                    kodi.process()
                self.assertEqual((kodi.shown(), kodi.seeks), (shown, seeks))

    def test_commands_before_kodi_applies_the_last(self):
        for subtitle, enabled, forced, commands, shown in (
            (2, True, {}, [("subtitle", -1), ("subtitle", 2)], 2),
            (2, True, {}, [("subtitle", 2), ("subtitle", -1)], None),
            # A forced subtitle Kodi doesn't report yet is still swapped by the audio switch.
            (2, True, {0: "en"}, [("subtitle", 0), ("audio", 1)], 0),
            (0, True, {0: "en"}, [("subtitle", 2), ("audio", 1)], 2),
        ):
            with self.subTest(subtitle=subtitle, enabled=enabled, forced=forced, commands=commands):
                kodi = FakeKodi(0, subtitle, enabled, forced=forced, languages=["en", "de"])
                app = self.tracks_app("osd", kodi)
                for command, index in commands:
                    getattr(app, f"page_{command}")(index=index)
                kodi.process()
                self.assertEqual(kodi.shown(), shown)

    def test_new_file_forgets_asked_subtitle(self):
        kodi = FakeKodi(0, 2, True)
        app = self.tracks_app("osd", kodi)
        app.page_subtitle(index=-1)
        kodi.process()
        with patch.object(self.video.threading, "Thread"):
            app.page_play("/next.mkv", session="two", audio=0, subtitle=2)
        # Kodi opened the new file with subtitle 2 showing.
        kodi.enabled = kodi.demuxed = True
        app.on_started()
        kodi.process()
        self.assertEqual((kodi.shown(), kodi.seeks), (2, 0))

    def test_subtitle_file_is_loaded(self):
        kodi = FakeKodi(0, 2, True)
        self.tracks_app("osd", kodi).page_subtitle(index=2, file="/subs.srt")
        self.assertEqual(kodi.queue, [("file", "/subs.srt")])

    def test_unknown_tracks_switch_as_asked(self):
        kodi = FakeKodi(1, 2, True)
        kodi.properties = lambda *args, **kwargs: None
        self.start(kodi, audio=1, subtitle=2)
        self.assertEqual((kodi.audio, kodi.shown(), kodi.seeks), (1, 2, 2))

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
