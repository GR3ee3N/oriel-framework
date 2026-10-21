from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
import tempfile
import types
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def kodi(display=True):
    """display: vfs.oriel.display is a Mock that draws the page; False keeps the real client."""
    package = types.ModuleType("oriel")
    package.__path__ = [str(ROOT / "addon/lib/oriel")]
    with tempfile.TemporaryDirectory(prefix="oriel-test-") as temporary, patch.dict(sys.modules, {
        "oriel": package,
        "xbmc": types.SimpleNamespace(Monitor=object, Player=object, executebuiltin=Mock(),
                                      executeJSONRPC=Mock(return_value="{}"), getCondVisibility=Mock(return_value=False)),
        "xbmcaddon": types.SimpleNamespace(
            Addon=lambda *args: types.SimpleNamespace(getAddonInfo={"id": "script.test", "path": temporary}.get)),
        "xbmcgui": types.SimpleNamespace(ListItem=Mock(), getCurrentWindowId=Mock(return_value=10000)),
    }):
        app = importlib.import_module("oriel.app")
        video = importlib.import_module("oriel.video")
        app.config.data_dir = lambda addon=None: temporary
        app.config.dev = lambda: {}
        app.config.PORT = 0
        app.webkit.ensure = lambda: ""
        if display:
            app.display.connect = Mock(side_effect=lambda events: Mock(ids={}))
        video.device.decoders = lambda: None
        yield app, video, Path(temporary)
