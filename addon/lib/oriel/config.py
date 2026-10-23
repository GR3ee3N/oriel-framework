"""Device paths and app development settings.

Read /storage/oriel/dev/<add-on id>.json at app startup:

    {"url": "http://<pc>:5173/",        development page URL
     "inspector": "0.0.0.0:9226",       remote inspector address
     "env": {"NAME": "value"}}          additional shell environment
"""
import json
import os

MODULE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHELL = os.path.join(MODULE, "bin", "oriel-shell")
OVERLAY = os.path.join(MODULE, "bin", "run-in-overlay.py")
WEBKIT_ADDON = "script.module.oriel.webkit"
FRAMEWORK_ADDON = "script.module.oriel.framework"
CTL = "/run/oriel/ctl"
DEV = "/storage/oriel/dev"

# Shared ports require Oriel apps to run one at a time.
PORT = 32499
PROXY_PORT = 32500


def addon_id():
    import xbmcaddon

    return xbmcaddon.Addon().getAddonInfo("id")


def dev():
    path = os.path.join(DEV, f"{addon_id()}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise RuntimeError(f"{path}: {e}") from e


def data_dir(addon=None):
    import xbmcvfs

    path = xbmcvfs.translatePath(f"special://profile/addon_data/{addon or addon_id()}")
    os.makedirs(path, exist_ok=True)
    return path


def settings_path():
    """Locate framework settings shared by all apps."""
    return os.path.join(data_dir(FRAMEWORK_ADDON), "settings.json")


def load_settings():
    try:
        with open(settings_path()) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(settings):
    path = settings_path()
    with open(path + ".tmp", "w") as f:
        json.dump(settings, f)
    os.replace(path + ".tmp", path)


def webkit_dir():
    import xbmcaddon

    return xbmcaddon.Addon(WEBKIT_ADDON).getAddonInfo("path")
