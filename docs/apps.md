# Writing an app

An Oriel app packages an `oriel.App` subclass and a web page as a Kodi
add-on. WPE WebKit renders the page, and Kodi draws it in its GUI. The
add-on publishes state to the page over HTTP at `127.0.0.1:32499`; the page
sends commands to the same server.

This document defines the public interface. Other code in
`addon/lib/oriel` and `@oriel/web` may change without notice.

`examples/hello` is a complete app using plain HTML and the HTTP API.
Install it with the framework using `tools/deploy.sh --example`. The
[Oriel Plex client](https://github.com/GR3ee3N/Oriel) is a video app using
Solid and `@oriel/web`.

## Add-on layout

```
addon.xml           dependency on script.module.oriel.framework
default.py          entry point: oriel.run(MyApp)
resources/web/      page served at /
resources/mark.png  optional startup image, shown on black until the first frame
```

Declare the framework dependency and an `xbmc.python.script` entry point
in `addon.xml`:

```xml
<requires>
  <import addon="xbmc.python" version="3.1.0"/>
  <import addon="script.module.oriel.framework" version="<version>"/>
</requires>
<extension point="xbmc.python.script" library="default.py">
  <provides>executable</provides>
</extension>
```

Set `<version>` to the oldest framework release that has everything the app
uses; Kodi then installs or updates the framework to at least that release.

The framework imports `script.module.oriel.webkit` and `vfs.oriel.display`
for the app. Oriel display owns the browse window and playback dialog;
Python controls input handling and playback.

Requests for nonexistent paths without a file extension serve
`index.html`, allowing the page to handle its own routes.

WebKit has only DejaVu Sans as a system font. Ship every other font the
page shows, such as for Chinese, Japanese, Korean or emoji, and load it with
`@font-face`: from `resources/web/`, or from `/files/` for fonts the app
downloads later.

All Oriel apps share the HTTP port, so only one can run at a time.

## Python API

```python
import oriel


class MyApp(oriel.App):
    @oriel.command("lookup")
    def lookup(self, query):
        return {"items": find(query)}


oriel.run(MyApp)
```

`oriel.run(cls)` creates the app and blocks until it closes. Kodi callbacks
queue events from Kodi's threads. One controller thread runs the hooks,
command handlers and Kodi calls, so those handlers share state without locks.
Keep hooks short: input and commands wait for them to finish.

### `oriel.App`

Override these hooks:

| Hook | Called when / contract |
|---|---|
| `on_start()` | Before loading the page. |
| `on_stop()` | During shutdown, after WebKit exits. |
| `state() -> dict` | Return page state, extending the base: `{**super().state(), ...}`. |
| `poll()` | Every 250 ms. |
| `on_key(key) -> bool` | Handle a named remote key (see Keys) before `VideoApp`'s playback handling and the page. Return `True` to keep it from both. |
| `on_event(kind, args)` | Handle an app-defined queued event (see `events` below). |
| `ours(dialog) -> bool` | Return `True` if this Kodi dialog window id belongs to the app and should leave the page visible. |

The app also provides:

| | |
|---|---|
| `own_screensaver` | Class attribute, default `False`: hide the page while Kodi's screensaver runs. With `True`, suppress Kodi's screensaver and expose its idle delay in the state's `screensaver` field, in minutes, for the page to use. The field is null if Kodi's screensaver is disabled. |
| `data_dir` | Path to the app's `addon_data` directory. |
| `files_dir` | `data_dir/files`, served to the page at `/files/`. The app creates it and keeps there what it downloads for the page; Kodi keeps it through add-on updates. |
| `publish()` | Publish `state()` after changing app state. |
| `events` | Call `events.put((kind, *args))` from any thread to invoke `on_event(kind, args)` on the controller thread. Framework-reserved kinds: `key`, `page`, `tick`, `hide`, `shell-exit`, `restart-shell`, `sleep`, `abort`, `display-window`, `display-gone`, plus `VideoApp`'s `started` and `ended`. |
| `close()` | Request app shutdown. |

`App.__init__` takes no arguments. Call `super().__init__()` first when
overriding it.

The app closes on an `exit` command, navigation away from its Kodi window
(such as Home), device sleep, normal shell exit or display add-on unload.

A web-process crash reloads the page; the third crash within one minute
terminates the shell. The framework allows two shell restarts within five
minutes, then closes the app on another failure.

### Commands

`@oriel.command(name)` registers a method as a page command handler.
Command fields become keyword arguments; the method's JSON-serializable
return value becomes the result. Subclasses inherit registered commands.

| Outcome | Reply |
|---|---|
| Success | `200 {"result": …}` |
| Unknown command, wrong fields, or `CommandError(message, "invalid")` | `400 {"error": …, "code": "invalid"}` |
| `CommandError(message)` | `409 {"error": …, "code": "unavailable"}` |
| Unhandled handler exception | `500 {"error": …, "code": "failed"}` |
| No reply within 10 s | `504 {"error": …, "code": "timeout"}` |

Use the failure code to decide whether to retry:

- `invalid`: correct the command before retrying.
- `unavailable`: retry when the required state exists, such as active playback.
- `failed`: the handler raised an unexpected exception.
- `timeout`: the result is unknown. The handler continues after the 10 s
  response deadline. A command that sets a state can be sent again.

Raise `oriel.CommandError` for an expected refusal: use `"invalid"` for
bad values or the default `"unavailable"` for the current app state. These
refusals produce a single log entry without a traceback.

Check each command's success contract. Some complete before replying;
others submit work to Kodi and report the outcome through published state.

Command logs contain names only by default. `@oriel.command(name, log=True)`
also logs fields; keep it disabled for commands containing secrets.

The base `App` has three commands:

| Command | Behavior | Success means |
|---|---|---|
| `ready` | Report the first rendered page frame. Until this command, the startup mark remains visible and keys are withheld from the page. | Done. |
| `exit` | Close the app. | Closed. |
| `cpu-rendering {on}` | Select CPU (`true`) or GPU (`false`) rendering for all Oriel apps. CPU rendering is a fallback for GPU rendering errors. | Setting saved. A changed value restarts the shell and reloads the page, losing its in-memory state. |

### State

Base state fields:

| Field | Meaning |
|---|---|
| `run` | Id of the current app run. A reloaded page or restarted WebKit sees the same id; a new start of the app gets a new one. |
| `cpuRendering` | CPU rendering enabled; defaults to false and is set by `cpu-rendering`. |
| `screensaver` | Idle delay in minutes for the page screensaver; null unless `own_screensaver` is enabled and Kodi has a screensaver configured. |

Extend `state()` to add app-specific fields.

Framework state fields are always present. Null means unknown or absent;
fields are not omitted. Optional command fields accept omission or null
for their default. Commands validate types and ranges before changing state
and return `invalid` for rejected values.

`python3 -m tests.test_contract --update` captures published state fixtures
in `web/tests/states.ts`. TypeScript checking verifies these against the
`@oriel/web` types.

### Keys

The display window forwards keys to the framework, which maps each action
to a name and calls `on_key`. Unless the hook returns `True`, the page
receives a `KeyboardEvent`:

| Key | DOM `key` |
|---|---|
| `left`, `right`, `up`, `down` | `ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown` |
| `select` | `Enter` |
| `back` | `Escape` |
| `info` | `F1` |
| `menu` | `ContextMenu` |
| `playpause`, `stop` | `MediaPlay`, `MediaStop` |
| `next`, `previous` | `MediaTrackNext`, `MediaTrackPrevious` |
| `fastforward`, `rewind` | `MediaFastForward`, `MediaRewind` |
| `pageup`, `pagedown` | `PageUp`, `PageDown` |
| `0` to `9` | `0` to `9` |

Keys are withheld from `on_key` and the page until `ready`. While
`VideoApp` hides the page during playback, `on_key` still receives them and
the page doesn't. Colour and channel up/down buttons are not exposed as
separate keys.

The page decides what each key does. Focus, D-pad navigation and Back are the
app's own; `action(event)` in `@oriel/web` names the remote's keys. Other
keys from a keyboard, such as Backspace, reach the page as typed and have no
action name.

During playback, `vfs.oriel.display` marks Play/Pause, Stop, Fast-forward,
Rewind and Previous as handled by Kodi. `VideoApp` consumes them without
forwarding them to the page.

### `oriel.video.VideoApp`

Use `VideoApp` for Kodi media playback with a web interface. The page
provides browsing and playback controls over Kodi's video. The Oriel Plex
client is the reference app.

```python
from oriel import run
from oriel.video import VideoApp


class MyVideoApp(VideoApp):
    pass


run(MyVideoApp)
```

`mode()` returns `browse`, `osd` (page over video) or `video` (page hidden).
The base `App` always uses `browse`. During video playback with the page
hidden, Back stops playback and other named keys except Stop open the OSD.
Kodi-only actions, such as volume, leave the page hidden. Override `on_key`
to handle a key differently; Kodi still acts on the keys it handles itself,
such as Play/Pause.

Play media from absolute URLs or paths relative to a `media-source`.
For relative paths, the proxy at `127.0.0.1:32500` adds the source headers
to Kodi's requests, keeping tokens out of Kodi's URLs. If a source ends a
file response early, the proxy requests the remaining bytes with a range
request. Override `media_url(path)` and `is_ours(file)` for other sources;
`is_ours` determines whether shutdown stops Kodi's current file.

If Kodi hasn't started the file after 30 seconds, the framework stops
playback & returns to browse mode. The player state becomes `idle` with
`ended` set to `error`.

Positions are seconds on the item's timeline. Set `streamOffset` to the
start of a stream that begins partway through the item, such as a transcode
created for a seek.

| Command | Behavior | Success means |
|---|---|---|
| `media-source {base, headers?}` | Set an HTTP/HTTPS base URL and request headers for relative paths. A rejected source returns `invalid` and preserves the previous source. Proxy startup or response failure returns `unavailable`. | Source applied to the proxy. |
| `play {path, title?, session?, position?, streamOffset?, duration?, audio?, subtitle?, subtitleFile?, meta?}` | Play a path or URL from `position`. Supply `duration` when Kodi cannot determine it. Apply Kodi track indexes `audio` and `subtitle` after startup; subtitle -1 disables subtitles. `session` identifies playback; `meta` is returned in state to restore a reloaded page. Replacing a file within the same session preserves pause state. | Playback requested; observe `player.state` and `ended` for the outcome. |
| `seek-to {seconds}` | Seek within the stream's bounds. For transcodes, start a stream at the target position because Kodi seeks inaccurately within them. | Seek requested; observe `time`. |
| `audio {index}` | Select a Kodi audio index while preserving the subtitle choice. | Selection requested; observe `audioIndex`. |
| `subtitle {index, file?}` | Select a Kodi subtitle index, use -1 to disable subtitles, or provide a subtitle file path or URL. | Selection requested; observe `subtitleIndex`. |
| `set-paused {paused}` | Set the pause state. | Done. |
| `stop` | Stop playback; succeeds without action in browse mode. | Browse mode restored and stop requested; observe `ended`. |
| `osd-on` | Show the page over playback, such as for a prompt. | Done. |
| `osd-off` | Hide the playback page. | Done. |

Commands that set an already active state succeed without further action.
Commands incompatible with the current mode are refused.

`VideoApp` adds `mode`, `player` and `decoders` to the base state.
`decoders` lists hardware video codecs by FFmpeg name, or null when the
hardware cannot report them. The `player` fields are:

| Field | Meaning |
|---|---|
| `state` | `idle`, `loading`, `playing` or `paused`. |
| `time` | Position in seconds on the item's timeline. |
| `duration` | Duration in seconds; null until supplied by `play` or Kodi. |
| `session`, `meta` | Values supplied by `play`, or null when omitted. |
| `ended` | Null during playback; then `ended`, `stopped` or `error` if Kodi failed to open or play the file. |
| `audio`, `subtitles` | Kodi track lists: null until the session's file starts, then an array, empty if there are no tracks of that kind. Each `play` resets them to null. |
| `audioIndex`, `subtitleIndex` | Active Kodi track indexes; `subtitleIndex` is null when subtitles are disabled. |

## Page API

### Protocol

| Request | Response / behavior |
|---|---|
| `GET /api/events` | Server-sent `state` events containing JSON: the current state on connection, then each published update. |
| `POST /api/cmd` | Send `{"cmd": name, ...fields}`; command results and errors follow the format above. |
| `GET /api/store` | Saved app settings. |
| `POST /api/store` | Merge an object into saved settings; null deletes a key. Return the merged settings. |
| `GET /files/<path>` | A file from the app's `files_dir`, or 404. |

The add-on stores settings in `store.json`, shared by bundled pages and
development-server pages.

Send `ready` after rendering the first frame. Send JSON bodies as
`text/plain`, the default when passing a string to `fetch`. This permits
requests from development-server pages without CORS preflights, which the
add-on does not handle.

Parallel requests can execute out of order. Wait for each reply before
sending the next command when order matters.

Requests must use `127.0.0.1:32499` or `localhost:32499` as the host.
Accepted origins are the app's page and the configured development server;
requests without an `Origin` are also accepted. Other origins receive 403.

### `@oriel/web`

`@oriel/web` works with any UI library or none. Framework releases publish
its TypeScript sources as `oriel-web-<version>.tgz`, so the app's build has
to compile TypeScript, as Vite and esbuild do. Install the package by
release URL:

```json
"dependencies": {
  "@oriel/web": "https://github.com/GR3ee3N/oriel-framework/releases/download/v<version>/oriel-web-<version>.tgz"
}
```

Set `allow-remote=root` in `.npmrc` to permit the URL dependency.

| Export | Contract |
|---|---|
| `createBridge<State, Command>(initial)` | Create a connection with `state()` (the latest state, including `connected`; each update is a new object), `onState(listener)` (call `listener` with each new state; return a function that removes it), `send(command)` (log failures), `call(command)` (return a result promise or reject with `CommandFailure`), `connect()` (call once at startup) and `ready()` (call after the first render). Both command methods share a queue and execute in send order. |
| `BaseState`, `BaseCommand` | Base app state and command types. |
| `CommandFailure`, `FailureCode` | Failure message and response `code`; `unreachable` means no response or an unreadable response body. |
| `VideoState`, `VideoCommand`, `Player`, `Mode`, `AudioTrack`, `SubtitleTrack`, `IDLE` | Video app types and initial player state (`IDLE`). |
| `load()`, `save(changes)` | Load and update saved settings. |
| `action(event)`, `Action`, `Digit` | Map a `KeyboardEvent` to a remote action name. |
| `ORIGIN` | API origin: empty for the bundled page, which the add-on serves on port 32499; `http://127.0.0.1:32499` for a page from a development server. |

### Drawing over video

The shell passes WebKit dma-bufs to `vfs.oriel.display`, whose rendering
control imports them as textures for Kodi's GUI frame. A full-screen
animation requires WebKit rendering and a full-screen texture draw per
frame; both must fit within the frame budget.

The framework hides the page while a non-passive Kodi dialog is open.

App startup requires the display add-on. If it is unavailable, startup
stops and Kodi displays a notification.

## Development

### Development settings

The app reads `/storage/oriel/dev/<add-on id>.json` on startup:

```json
{"url": "http://<computer>:5173/", "inspector": "0.0.0.0:9226", "env": {}}
```

- `url`: page URL on the development server. The add-on permits this origin
  on `/api`. The page addresses the API at `http://127.0.0.1:32499`, as
  `ORIGIN` does for any page not served on that port.
- `inspector`: address for WebKit's remote inspector.
- `env`: additional shell environment variables. With `ORIEL_FRAME_LOG` set
  to a file path, the shell writes a line for each frame Kodi draws:
  `rendered sent drawn`, the `CLOCK_MONOTONIC` seconds at which WebKit
  finished the frame, the shell sent it to `vfs.oriel.display` and Kodi
  first drew it.

### Logs and files

The app's `addon_data` directory contains:

- `addon.log`: add-on and framework messages.
- `shell.log`: WebKit output and the page's console.
- `store.json`: saved page settings.
- `files/`: the app's `files_dir`.

Both logs are overwritten each time the app opens. The shared CPU rendering
setting is stored in `addon_data/script.module.oriel.framework/settings.json`.
