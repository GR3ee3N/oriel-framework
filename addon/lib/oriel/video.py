"""Play media in Kodi with page-based browsing and playback controls.

Page commands: media-source, play, set-paused, stop, seek-to, audio,
subtitle, osd-on and osd-off. Setting an already active state succeeds
without further action. Relative paths use the media proxy and its source
headers; absolute URLs play directly.

Positions use the item's timeline. streamOffset identifies the start of a
stream that begins partway through an item. Kodi handles actions marked
with the kodi flag during playback. Other named keys can open the OSD;
unnamed actions, such as volume and mute, leave page visibility unchanged.

Close the playback dialog before stopping playback. Closing it during
Kodi's stop operation deadlocks the GUI.
"""
import traceback
from urllib.parse import urlsplit

import xbmc
import xbmcgui

from . import config, device
from .app import App, CommandError, command, jsonrpc, kodi_setting, now
from .log import log
from .proxy import ProxyProcess

PROXY = f"http://127.0.0.1:{config.PROXY_PORT}"
TRACK_PROPERTIES = ["audiostreams", "subtitles", "currentaudiostream", "currentsubtitle", "subtitleenabled"]
# Preserve the requested subtitle selection across audio switches in select_tracks.
KEEP = "keep"

# Treat Back immediately after OSD closure as OSD input, not a playback stop.
BACK_GRACE = 1.0
# Kodi setting value for restoring the browse display mode after playback:
# "Adjust display refresh rate: On start / stop".
REFRESH_ON_START_STOP = 2
# Maximum wait in seconds for the browse display mode to return.
MODE_WAIT = 5.0


def output_mode():
    try:
        with open("/sys/class/display/mode") as f:
            return f.read().strip()
    except OSError:
        return None


def player_state(state="idle", time=0, duration=None, session=None, meta=None, ended=None):
    """Build player state with every field present, using null for unknown or
    absent values. Track lists remain null until this session's file starts.
    """
    return {"state": state, "time": time, "duration": duration, "session": session, "meta": meta,
            "ended": ended, "audio": None, "subtitles": None, "audioIndex": None, "subtitleIndex": None}


# Validate command values before changing state; None selects optional defaults.

def invalid(name, what):
    return CommandError(f"{name} must be {what}", "invalid")


def number(name, value, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise invalid(name, "a number, 0 or more")
    return float(value)


# Bound track indexes to Kodi's signed C int range.
INDEX_MAX = 2**31 - 1


def integer(name, value, minimum, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= INDEX_MAX:
        raise invalid(name, f"a whole number from {minimum} to {INDEX_MAX}")
    return value


def text(name, value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise invalid(name, "a string")
    return value


class Player(xbmc.Player):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def onAVStarted(self):
        self.events.put(("started",))

    def onPlayBackStopped(self):
        self.events.put(("ended", "stopped"))

    def onPlayBackEnded(self):
        self.events.put(("ended", "ended"))

    def onPlayBackError(self):
        self.events.put(("ended", "error"))


class VideoApp(App):
    def __init__(self):
        super().__init__()
        self.player_state = player_state()
        self.start_streams = {}
        # Retain the last requested (subtitle index, visibility) until Kodi reports it.
        self.subtitle_asked = None
        # Kodi starts each stream at time 0; add streamOffset for the item timeline.
        # Prefer page-supplied transcode duration; None falls back to Kodi's duration.
        self.stream_offset = 0.0
        self.duration = None
        self.decoders = device.decoders()
        log("hardware decoders:", "unknown" if self.decoders is None else " ".join(self.decoders))
        self.proxy = ProxyProcess(config.PROXY_PORT, log)
        self.media_source = False
        self.loading_since = 0.0
        self.replaced_at = -1e9
        self.played_url = None
        # Track whether the display playback dialog is open.
        self.osd_open = False
        self.osd_off_at = 0.0
        self.player = Player(self.events)
        self.idle = 0
        self.polls = 0
        # Retain the browse output mode and the deadline for restoring page visibility.
        self.browse_mode = None
        self.show_by = None

    def media_url(self, path):
        """Resolve page media and subtitle paths to Kodi playback URLs."""
        if urlsplit(path).scheme:
            return path
        if not self.media_source:
            raise CommandError("no media source")
        return PROXY + path

    def is_ours(self, file):
        """Return True for app-owned playback that must stop when the app closes."""
        return file == self.played_url or file.startswith(PROXY + "/")

    def mode(self):
        """Return browse, osd (page over video) or video (page hidden)."""
        return {"osd": "osd", "loading": "video", "playing": "video"}.get(self.phase, "browse")

    def page_shown(self):
        return self.mode() != "video"

    def state(self):
        return {**super().state(), "mode": self.mode(), "player": dict(self.player_state), "decoders": self.decoders}

    def end(self):
        """Return the stream end on the item timeline, or None while unknown."""
        if self.duration:
            return self.duration
        total = self.player.getTotalTime()
        return self.stream_offset + total if total > 0 else None

    def set_player(self, **changes):
        if any(self.player_state[k] != v for k, v in changes.items()):
            self.player_state.update(changes)
            self.publish()

    def on_event(self, kind, args):
        if kind == "started":
            self.on_started()
        elif kind == "ended":
            self.on_ended(*args)
        else:
            super().on_event(kind, args)

    def require(self, *phases):
        if self.phase not in phases:
            raise CommandError(f"not possible in {self.phase}")

    @command("media-source")
    def page_media_source(self, base, headers=None):
        # Keep source headers out of logs because they can contain tokens.
        log("media source", base)
        try:
            self.proxy.set_source(base, headers or {})
        except ValueError as e:
            raise CommandError(str(e), "invalid") from e
        except RuntimeError as e:
            raise CommandError(str(e)) from e
        self.media_source = True

    @command("play")
    def page_play(self, path, title=None, session=None, position=None, streamOffset=None, duration=None,
                  audio=None, subtitle=None, subtitleFile=None, meta=None):
        self.require("browse", "loading", "playing", "osd")
        url = self.media_url(text("path", path))
        title = text("title", title, optional=True) or ""
        text("session", session, optional=True)
        position = number("position", position, optional=True) or 0.0
        stream_offset = number("streamOffset", streamOffset, optional=True) or 0.0
        duration = number("duration", duration, optional=True) or None
        integer("audio", audio, 0, optional=True)
        integer("subtitle", subtitle, -1, optional=True)
        if text("subtitleFile", subtitleFile, optional=True):
            self.media_url(subtitleFile)
        start = position - stream_offset
        if start < 0:
            raise CommandError("the position is before the stream's beginning", "invalid")
        # Preserve pause state for same-session replacements, including files still loading.
        same = session is not None and session == self.player_state["session"]
        paused = same and (self.player_state["state"] == "paused"
                           or self.phase == "loading" and self.start_streams.get("paused"))
        # Kodi replaces files without stopping, such as on a transcode seek;
        # ignore end notifications for the previous file.
        if self.phase != "browse":
            self.close_osd()
            self.replaced_at = now()
        elif self.show_by is None:
            self.browse_mode = output_mode()
        self.show_by = None
        self.phase = "loading"
        self.loading_since = now()
        self.stream_offset = stream_offset
        self.duration = duration
        self.subtitle_asked = None
        # Defer track selection until on_started; subtitle -1 disables subtitles.
        self.start_streams = {
            "audio": audio, "subtitle": subtitle, "subtitleFile": subtitleFile, "paused": paused,
        }
        # Retain metadata so a reloaded page can restore playback state.
        self.player_state = player_state("loading", int(position), duration and int(duration), session, meta)
        self.publish()
        self.hide_later()
        item = xbmcgui.ListItem(title)
        if start:
            item.setProperty("StartOffset", f"{start:.3f}")
        self.played_url = url
        self.player.play(url, item)

    @command("osd-off")
    def page_osd_off(self):
        if self.phase == "playing":
            return
        self.require("osd")
        self.phase = "playing"
        self.osd_off_at = now()
        self.publish()
        self.hide_later()

    @command("set-paused", log=True)
    def page_set_paused(self, paused):
        self.require("playing", "osd")
        if not isinstance(paused, bool):
            raise CommandError("paused must be true or false", "invalid")
        # Set an explicit pause state because Kodi may already have handled Play/Pause.
        if jsonrpc("Player.PlayPause", playerid=1, play=not paused) is None:
            raise CommandError("Kodi refused")

    @command("stop")
    def page_stop(self):
        if self.phase in ("loading", "playing", "osd"):
            self.stop_playback("stop from the page")

    @command("seek-to", log=True)
    def page_seek_to(self, seconds):
        self.require("playing", "osd")
        seconds = number("seconds", seconds)
        end = self.end()
        if seconds < self.stream_offset or (end is not None and seconds > end):
            raise CommandError("outside the stream")
        self.player.seekTime(seconds - self.stream_offset)

    @command("audio", log=True)
    def page_audio(self, index):
        self.require("playing", "osd")
        self.select_tracks(audio=integer("index", index, 0), subtitle=KEEP)

    @command("subtitle", log=True)
    def page_subtitle(self, index=None, file=None):
        self.require("playing", "osd")
        index = integer("index", index, -1, optional=True)
        if text("file", file, optional=True):
            self.media_url(file)
        self.select_tracks(subtitle=-1 if index is None else index, file=file)

    @command("osd-on")
    def page_osd_on(self):
        # Playback prompts can request page visibility.
        if self.phase == "osd":
            return
        self.require("playing")
        self.show_osd()

    def intercept_key(self, key, sent, kodi):
        if key is None:
            return False
        if self.phase in ("playing", "osd") and kodi:
            # Kodi handles these actions; its stop completes through on_ended.
            if key != "stop" and self.phase == "playing":
                self.show_osd()
        elif self.phase == "playing" and key == "back":
            if sent - self.osd_off_at > BACK_GRACE:
                self.stop_playback("back during playback")
        elif self.phase == "playing":
            self.show_osd()
        else:
            return False
        return True

    def stop_playback(self, why):
        # Close the dialog before stopping to prevent the Kodi GUI deadlock described above.
        self.on_ended(why)
        xbmc.executebuiltin("PlayerControl(Stop)")

    def show_osd(self):
        self.phase = "osd"
        self.publish()
        self.show_page()

    def select_tracks(self, audio=None, subtitle=None, file=None):
        """Apply track changes only when needed: Kodi reopens and seeks the stream
        on audio or embedded-subtitle selection, which can disturb A/V sync.
        Subtitle -1 disables subtitles, None leaves Kodi's selection, and KEEP
        preserves the viewer's choice; a subtitle file overrides these options.
        """
        # Kodi queues switches and reports them after applying them. Base decisions
        # on active tracks, using the last requested subtitle while its report is pending.
        tracks = jsonrpc("Player.GetProperties", playerid=1, properties=[
            "currentaudiostream", "currentsubtitle", "subtitleenabled", "subtitles"]) or {}
        index, shown = self.subtitle_asked or (
            (tracks.get("currentsubtitle") or {}).get("index"), tracks.get("subtitleenabled"))
        showing = index if shown else None
        replaced = False
        if audio is not None and (tracks.get("currentaudiostream") or {}).get("index") != audio:
            self.player.setAudioStream(audio)
            # Audio changes replace or hide forced subtitles according to audio language,
            # even when the current forced subtitle is hidden.
            replaced = any(s["index"] == index and s.get("isforced") for s in tracks.get("subtitles", []))
        if subtitle == KEEP:
            subtitle = (-1 if showing is None else showing) if replaced else None
        if file:
            self.player.setSubtitles(self.media_url(file))
            self.subtitle_asked = None
        elif subtitle is None:
            pass
        elif subtitle < 0:
            self.player.showSubtitles(False)
            self.subtitle_asked = (index, False)
        else:
            # Showing a hidden subtitle does not restore its demuxed packets;
            # reselect it unless it is already visible.
            if replaced or showing != subtitle:
                self.player.setSubtitleStream(subtitle)
            self.player.showSubtitles(True)
            self.subtitle_asked = (subtitle, True)

    def on_started(self):
        if self.phase != "loading":
            return
        streams = self.start_streams or {}
        self.select_tracks(streams.get("audio"), streams.get("subtitle"), streams.get("subtitleFile"))
        self.phase = "playing"
        self.publish()
        self.open_osd()
        if streams.get("paused"):
            self.player.pause()
            self.show_osd()

    def on_ended(self, why):
        if why in ("stopped", "ended") and now() - self.replaced_at < 5 and self.player.isPlayingVideo():
            log("ignored: the end of the file replaced")
            return
        self.close_osd()
        if self.phase in ("loading", "playing", "osd"):
            log("back to browse:", why)
            # A stop before successful startup can mean Kodi failed to open the file.
            if why in ("error", "did not start") or (why == "stopped" and self.phase == "loading"):
                ended = "error"
            elif why == "ended" or (why == "no longer playing" and self.near_end()):
                ended = "ended"
            else:
                ended = "stopped"
            self.phase = "browse"
            s = self.player_state
            self.player_state = player_state("idle", s["time"], s["duration"], s["session"], s["meta"], ended)
            self.publish()
            # Wait for the browse output mode so the TV's mode switch does not interrupt
            # a briefly visible page.
            if (self.browse_mode and output_mode() != self.browse_mode
                    and kodi_setting("videoplayer.adjustrefreshrate") == REFRESH_ON_START_STOP):
                log("waiting for the output to go back to", self.browse_mode)
                self.show_by = now() + MODE_WAIT
            else:
                self.show_page()

    def near_end(self):
        # Polling can detect playback end before Kodi delivers its callback.
        t, d = self.player_state["time"], self.player_state["duration"]
        return d is not None and t >= d - 10

    def open_osd(self):
        self.osd_open = True
        self.display.open("player")

    def close_osd(self):
        if self.osd_open:
            self.osd_open = False
            self.display.close("player", wait=2)

    def show_page(self):
        if self.show_by is None:
            super().show_page()

    def poll(self):
        if self.show_by is not None and (output_mode() == self.browse_mode or now() > self.show_by):
            log("output", output_mode(), "- showing the page")
            self.show_by = None
            self.show_page()
        if self.phase in ("playing", "osd"):
            # Polling also detects playback ends without a Kodi callback.
            if not self.player.isPlayingVideo():
                self.idle += 1
                if self.idle >= 3:
                    self.idle = 0
                    self.on_ended("no longer playing")
                return
            self.idle = 0
            try:
                end = self.end()
                self.set_player(
                    state="paused" if xbmc.getCondVisibility("Player.Paused") else "playing",
                    time=int(self.stream_offset + self.player.getTime()),
                    duration=None if end is None else int(end))
            except RuntimeError:
                pass
            # Refresh tracks once a second because Kodi applies requested changes asynchronously.
            self.polls += 1
            if self.polls % 4 == 1:
                self.update_tracks()
        elif self.phase == "loading" and now() - self.loading_since > 30 and not self.player.isPlayingVideo():
            # Detect startup failures that Kodi did not report through a callback.
            self.on_ended("did not start")

    def update_tracks(self):
        r = jsonrpc("Player.GetProperties", playerid=1, properties=TRACK_PROPERTIES)
        if not r:
            return
        self.set_player(
            audio=[{"index": a["index"], "language": a.get("language", ""), "name": a.get("name", ""),
                    "codec": a.get("codec", ""), "channels": a.get("channels", 0)}
                   for a in r.get("audiostreams", [])],
            subtitles=[{"index": s["index"], "language": s.get("language", ""), "name": s.get("name", ""),
                        "forced": s.get("isforced", False), "impaired": s.get("isimpaired", False)}
                       for s in r.get("subtitles", [])],
            audioIndex=(r.get("currentaudiostream") or {}).get("index"),
            subtitleIndex=(r.get("currentsubtitle") or {}).get("index") if r.get("subtitleenabled") else None)

    def ours(self, dialog):
        return self.osd_open and dialog == self.display.ids.get("player")

    def close(self):
        if self.exiting:
            return
        self.close_osd()
        # Stop app-owned playback before shutting down a possible media source.
        try:
            if self.player.isPlaying() and self.is_ours(self.player.getPlayingFile()):
                log("stopping the playback with the app")
                self.player.stop()
        except RuntimeError:
            pass
        except Exception:
            # Complete shutdown even if the app's ownership check raises.
            log("is_ours failed")
            log(traceback.format_exc())
        super().close()
        self.proxy.stop()
