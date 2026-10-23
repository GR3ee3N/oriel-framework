import copy
import json
import sys
import unittest
from unittest.mock import Mock

from .kodi import ROOT, kodi

STATES = ROOT / "web/tests/states.ts"


def states():
    with kodi() as (oriel, video, _):
        oriel.screensaver_minutes = lambda: 10
        app = oriel.App()
        app.server.server_close()
        base = app.state()

        class Video(video.VideoApp):
            own_screensaver = True

        app = Video()
        app.server.server_close()
        app.decoders = ["h264", "hevc"]
        app.player = Mock(**{"isPlayingVideo.return_value": True, "getTime.return_value": 30.0,
                             "getTotalTime.return_value": 0.0})
        app.hide_later = Mock()
        video.jsonrpc = lambda *args, **kwargs: {
            "audiostreams": [{"index": 0, "language": "de", "name": "E-AC3 5.1", "codec": "eac3", "channels": 6}],
            "subtitles": [{"index": 0, "language": "de", "name": "Forced", "isforced": True, "isimpaired": False}],
            "currentaudiostream": {"index": 0}, "currentsubtitle": {"index": 0}, "subtitleenabled": False,
        }
        app.phase = "browse"
        published = [app.state()]
        app.server.publish = lambda state: published.append(copy.deepcopy(state))

        meta = {"title": "Movie"}
        app.page_play("http://media.example/movie.mkv", title="Movie", session="one", position=120,
                                   duration=5400, audio=0, subtitle=-1, meta=meta)
        app.on_started()
        app.poll()
        app.on_ended("ended")
        # Capture a transcode starting at 600 s before Kodi reports its duration.
        app.page_play("http://media.example/transcode.mkv", session="two", position=600,
                                   streamOffset=600)
        app.on_started()
        app.poll()
        app.on_ended("stopped")
    return base, published


def render(base, published):
    return (
        "// Written by tests/test_contract.py from the states the add-on publishes;\n"
        "// `npm run check` holds them to the page's types.\n\n"
        "import type { BaseState } from '../src/bridge';\n"
        "import type { VideoState } from '../src/video';\n\n"
        f"export const base = {json.dumps(base, indent=2)} satisfies Omit<BaseState, 'connected'>;\n\n"
        f"export const video = {json.dumps(published, indent=2)} satisfies Omit<VideoState<{{ title: string }}>, 'connected'>[];\n"
    )


class ContractTests(unittest.TestCase):
    def test_fixture_matches_published_states(self):
        self.assertEqual(STATES.read_text(), render(*states()),
                         "run python3 -m tests.test_contract --update")

    def test_intermediate_publication_is_recorded(self):
        _, published = states()
        self.assertTrue(any(s["player"]["state"] == "playing" and s["player"]["time"] == 30
                            and s["player"]["audio"] is None for s in published))


if __name__ == "__main__":
    if sys.argv[1:] == ["--update"]:
        STATES.write_text(render(*states()))
    else:
        unittest.main()
