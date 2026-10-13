# Writing an app

An Oriel app is a Kodi add-on with two halves: Python that subclasses
`oriel.App` and runs on Kodi's side, and a web page that WPE WebKit shows
over Kodi. They talk over HTTP on `127.0.0.1:32499`: the add-on publishes
its state to the page, and the page sends it commands.

Everything listed here is the public interface. Anything else in
`addon/lib/oriel` or `@oriel/web` may change without notice.

`examples/hello` is a complete app with a plain HTML page, written against
the HTTP protocol directly. Install it next to the framework with
`tools/deploy.sh --example`. The [Oriel Plex client](https://github.com/GR3ee3N/Oriel)
is a video app written with Solid and `@oriel/web`.

## Add-on layout

```
addon.xml           requires script.module.oriel.framework
default.py          the entry point: oriel.run(MyApp)
resources/web/      the page, served at /
resources/mark.png  optional: shown on black until the page's first frame
```

`addon.xml` imports the framework, and its entry point is an
`xbmc.python.script` extension:

```xml
<requires>
  <import addon="xbmc.python" version="3.1.0"/>
  <import addon="script.module.oriel.framework" version="0.1.0"/>
</requires>
<extension point="xbmc.python.script" library="default.py">
  <provides>executable</provides>
</extension>
```

A path that isn't a file and has no extension gets `index.html`, so the
page can use its own routes.

Only one Oriel app runs at a time; they share the port.

## The Python side

```python
import oriel


class MyApp(oriel.App):
    @oriel.command("lookup")
    def lookup(self, query):
        return {"items": find(query)}


oriel.run(MyApp)
```

`oriel.run(cls)` creates the app and blocks until it closes. Kodi calls
back on its own threads, which only queue events; one controller thread runs
every hook and command handler and makes every Kodi call, so the app's code
needs no locks. Hooks must not block for long: keys and commands wait
behind them.

### `oriel.App`

Override these:

| Hook | |
|---|---|
| `on_start()` | Before the page loads. |
| `on_stop()` | On close, after WebKit has quit. |
| `state() -> dict` | The page's state. Extend the base: `{**super().state(), ...}`. |
| `poll()` | Four times a second. |
| `on_key(key) -> bool` | A remote key for the page (see Keys). Return True to keep it from the page. |
| `on_event(kind, args)` | An event the app queued itself (see below). |
| `ours(dialog) -> bool` | Return True if the Kodi dialog with this window id is the app's own, so it doesn't hide the page. |

and use these:

| | |
|---|---|
| `own_screensaver` | Class attribute, default False: Kodi's screensaver runs and the page is hidden while it is up. True holds Kodi's screensaver off; the state's `screensaver` is then Kodi's delay in minutes (null if Kodi's screensaver is off), for the page's own. |
| `data_dir` | The app's `addon_data` directory. |
| `publish()` | Sends `state()` to the page. Call it after the state changes. |
| `events` | A queue. `events.put((kind, *args))` from any thread calls `on_event(kind, args)` on the controller thread. Kinds the framework uses: `key`, `page`, `tick`, `hide`, `shell-exit`, `sleep`, `abort`, and `VideoApp`'s `started` and `ended`. |
| `close()` | Closes the app. |

`App.__init__` takes no arguments. An override calls `super().__init__()`
first.

The app closes when the page sends `exit`, Kodi leaves the app's window
(Home, say), the box goes to sleep, or the shell exits normally.

A web-process crash reloads the page. Three crashes within one minute
terminate the shell. The framework restarts a failed shell at most twice
within five minutes; a further shell failure closes the app.

### Commands

`@oriel.command(name)` makes a method the handler of the page's command
`name`. The command's fields are its keyword arguments, and its return value
(anything `json` can encode) is the reply's result. Subclasses inherit their
bases' commands.

| Outcome | Reply |
|---|---|
| Success | `200 {"result": …}` |
| Unknown command, wrong fields, or `CommandError(message, "invalid")` | `400 {"error": …, "code": "invalid"}` |
| `CommandError(message)` | `409 {"error": …, "code": "unavailable"}` |
| The handler raised anything else | `500 {"error": …, "code": "failed"}` |
| No reply within 10 s | `504 {"error": …, "code": "timeout"}` |

The codes tell the app what to do about a failure:

- `invalid`: the command can't work as sent, so sending it again won't help.
- `unavailable`: not in the app's current state (no playback, say); it may
  work later.
- `failed`: the handler broke, a bug.
- `timeout`: the command may still have run; a handler that takes longer
  than 10 s runs to the end. Commands that set a state can be sent again.

`oriel.CommandError` is for refusals, `"invalid"` for a bad value and the
default `"unavailable"` for anything else; the log gets one line instead of
a traceback.

Each command documents what its success means: done, or (for work Kodi
does in the background) that Kodi was asked, with the outcome reported in
the state.

The log records only a command's name. `@oriel.command(name, log=True)`
also logs its fields; leave it off for commands that carry secrets.

The base `App` has three commands:

| Command | | Success means |
|---|---|---|
| `ready` | The page has drawn its first frame. Until then the app's mark shows and keys don't reach the page. | Done. |
| `exit` | Closes the app. | Closed. |
| `hdr-white {nits}` | Sets graphics white on HDR output, 80 to 1000 nits, for every Oriel app. | Saved; WebKit uses it from its next frame. |

### State

The base state is

| Field | |
|---|---|
| `hdrWhite` | Graphics white in nits on HDR output: 203 (BT.2408's reference white) unless set with `hdr-white`. |
| `screensaver` | Minutes until the page's own screensaver; null for an app without `own_screensaver`, or when Kodi's screensaver is off. |

The app adds its own fields in `state()`.

The framework's fields are always present; null means unknown or none,
never a missing field. In commands, an optional field may be left out or
null, and both mean its default. A command's values are checked before it
changes anything: a value of the wrong type or out of range is `invalid`.
`python3 -m tests.test_contract --update` captures published states in
`web/tests/states.ts`. TypeScript checking verifies that they match
`@oriel/web`'s state types.

### Keys

Kodi keeps the remote. The app's window receives each key, the framework
names it and calls `on_key`, and unless that returns True the key reaches
the page as a `KeyboardEvent`:

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

Keys reach neither `on_key` nor the page before `ready`, or while
`VideoApp` hides the page during playback. The colour buttons and channel up/down aren't
available: Kodi's default keymaps send the colour buttons to other windows
and channel up/down as page up/down.

During playback Kodi keeps Play/Pause, Stop, Fast-forward, Rewind and
Previous for itself in every window, so `VideoApp` doesn't pass them on.

### `oriel.video.VideoApp`

For apps whose media Kodi plays. The page is the app's interface while
browsing and the OSD during playback; Kodi's video runs underneath. Oriel is
its reference app.

```python
from oriel import run
from oriel.video import VideoApp


class MyVideoApp(VideoApp):
    pass


run(MyVideoApp)
```

`mode()` gives the page's mode: `browse`, `osd` (the page over the video)
or `video` (the page hidden; the base `App` never hides it). While the video plays without the OSD, Back
stops playback and any other key except Stop shows the OSD.

Media plays from absolute URLs, or from relative paths on the media source
that the page sets with `media-source`: the framework's proxy on
`127.0.0.1:32500` forwards Kodi's requests to the source with the given
headers, so tokens stay out of Kodi's URLs. Override
`media_url(path)` and `is_ours(file)` to play from elsewhere; `is_ours`
decides whether Kodi's current file is stopped when the app closes.

Times are in the item's timeline. A stream that begins into the item, such
as a transcode started at a seek position, gives `streamOffset`, where it
begins.

| Command | | Success means |
|---|---|---|
| `media-source {base, headers?}` | Where relative paths play from (an http or https URL), and the headers to add. A source the proxy rejects is `invalid` and leaves the previous one; a proxy that can't start or answer is `unavailable`. | The proxy uses it. |
| `play {path, title?, session?, position?, streamOffset?, duration?, audio?, subtitle?, subtitleFile?, meta?}` | Plays a path or URL from `position`. `duration` is for streams whose length Kodi can't tell. `audio` and `subtitle` are Kodi's track indexes, applied once the file starts; subtitle -1 is none. `session` identifies the playback; `meta` comes back in the state for a page reloaded during playback. During playback, `play` replaces the file; with the same `session` a paused playback stays paused. | Kodi was asked to play; `player.state` and `ended` tell whether it does. |
| `seek-to {seconds}` | Refused outside the stream. Kodi seeks inaccurately in transcode streams; start a new stream instead. | Kodi was asked; `time` follows. |
| `audio {index}` | Kodi's index; keeps the current subtitle choice. | Kodi was asked; `audioIndex` follows. |
| `subtitle {index, file?}` | Kodi's index, -1 for none, or a subtitle file's path or URL. | Kodi was asked; `subtitleIndex` follows. |
| `set-paused {paused}` | Pauses or resumes. | Done. |
| `stop` | Stops playback; nothing to do in browse. | Back in browse, and Kodi was asked to stop; `ended` follows. |
| `osd-on` | Shows the page during playback, e.g. for a prompt. | Done. |
| `osd-off` | Hides it again. | Done. |

Commands set a state rather than toggle one: a command whose state already
holds succeeds without doing anything. Other commands that don't fit the
current mode are refused.

The state adds `mode`, `player` and `decoders`, the video codecs (ffmpeg's
names) the box decodes in hardware, or null if it doesn't say. `player` has

| Field | |
|---|---|
| `state` | `idle`, `loading`, `playing` or `paused`. |
| `time` | Seconds, in the item's timeline. |
| `duration` | Seconds; null while neither `play` nor Kodi says (a transcode, say). |
| `session`, `meta` | From `play`; null if it gave none. |
| `ended` | Once playback ends: `ended`, `stopped` or `error` (Kodi failed to open or play the file); null before. |
| `audio`, `subtitles` | Kodi's track lists; null until the session's file has started, `[]` if it has none of the kind. A new `play` sets them back to null. |
| `audioIndex`, `subtitleIndex` | Kodi's current tracks; `subtitleIndex` is null while subtitles are off. |

## The page

### Protocol

| Request | |
|---|---|
| `GET /api/events` | Server-sent events: `state` with the state as JSON, first the current one, then each published change. |
| `POST /api/cmd` | A command, `{"cmd": name, ...fields}`. The reply is as above. |
| `GET /api/store` | The app's saved settings, plus `run`, an id that changes each time the app starts but not when WebKit restarts. |
| `POST /api/store` | Merges an object into the saved settings; null removes a key. Replies with the result. |

The add-on keeps the settings in `store.json`, so they also work from a dev
server.

The page sends `ready` once it has drawn its first frame. Send JSON bodies
as `text/plain`, `fetch`'s default for a string: the add-on doesn't answer
CORS preflights, which a page on a dev server would need otherwise.

Commands sent in parallel can run in any order: send the next one after
the previous reply when the order matters.

The add-on answers only requests to `127.0.0.1:32499` or `localhost:32499`
that come from the app's own page, the dev server's origin, or a program
without an `Origin`; other pages get 403.

### `@oriel/web`

`@oriel/web` is the page side for apps written with [Solid](https://www.solidjs.com/)
and [Vite](https://vite.dev/); apps that use it must be both. It ships as
TypeScript sources in `oriel-web-<version>.tgz` with each framework release,
installed by URL:

```json
"dependencies": {
  "@oriel/web": "https://github.com/GR3ee3N/oriel-framework/releases/download/v<version>/oriel-web-<version>.tgz",
  "solid-js": "^1.9.15"
}
```

npm needs `allow-remote=root` in `.npmrc` for the URL. In `vite.config.ts`,
set `resolve.dedupe: ['solid-js']` so the app and the package share one Solid.

| Export | |
|---|---|
| `createBridge<State, Command>(initial)` | The connection to the add-on: `state` (a Solid store, with `connected`), `send(command)` (fire and forget; failures are logged), `call(command)` (a promise of the result, rejected with a `CommandFailure`), `connect()` (call once at start) and `ready()` (call after the first render). Commands from `send` and `call` reach the add-on one at a time, in the order sent. |
| `BaseState`, `BaseCommand` | The base state and commands. |
| `CommandFailure`, `FailureCode` | A failed `call`: the message and the reply's `code`, or `unreachable` when the add-on didn't answer or its reply was cut off. |
| `VideoState`, `VideoCommand`, `Player`, `Mode`, `AudioTrack`, `SubtitleTrack`, `IDLE` | `VideoApp`'s state and commands; `IDLE` is the initial `player`. |
| `load()`, `save(changes)`, `BaseSaved` | The saved settings. |
| `action(event)`, `Action`, `Digit` | A `KeyboardEvent`'s key name. |
| `focusable`, `setScope`, `focusOn`, `move`, `select`, `playFocused`, `focused`, `activeScope`, `Direction`, `Options` | Spatial D-pad focus. The focused element gets the `focused` class. |
| `ScopeContext`, `useScope` | Focus scopes, one per screen. |
| `createRouter(home)`, `Entry` | A stack of screens. |
| `onBack(handler)`, `back()` | Back handlers, innermost first. |
| `uuid()` | A UUID, also outside secure contexts. |
| `ORIGIN` | The add-on's origin: empty in a build, `http://127.0.0.1:32499` under Vite's dev server. |

## Development

### Dev settings

`/storage/oriel/dev/<add-on id>.json` on the box, read when the app starts:

```json
{"url": "http://<computer>:5173/", "inspector": "0.0.0.0:9226", "env": {}}
```

- `url` loads the page from a dev server instead of `resources/web`. The
  add-on then allows that origin on `/api`, and the page must address the
  add-on at `http://127.0.0.1:32499` (`ORIGIN` does this under Vite).
- `inspector` turns on WebKit's remote inspector at that address.
- `env` sets environment variables for the shell; the debug switches are
  described in `addon/lib/oriel/config.py`.

### Logs and files

In the app's `addon_data` directory:

- `addon.log`: the add-on and the framework.
- `shell.log`: WebKit, with the page's console.
- `store.json`: the page's saved settings.

Both logs start over each time the app opens. The framework's own settings
(HDR white) are in `addon_data/script.module.oriel.framework/settings.json`.
