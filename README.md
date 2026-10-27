# Oriel framework

Oriel framework runs web apps inside Kodi on CoreELEC. WPE WebKit renders
the page, and Oriel display (`vfs.oriel.display`) draws it in Kodi's GUI,
including over video. The framework connects the page to Kodi playback,
saved settings and remote control input.

It provides two packages:

- `script.module.oriel.framework`: the Kodi add-on with the Python API and
  Rust shell that hosts WebKit.
- `@oriel/web`: the TypeScript package for add-on communication, saved
  settings, playback controls and remote key names.

Apps depend on the framework. The framework imports the Oriel WebKit
runtime and Oriel display binary add-on as dependencies.

## Status

> [!WARNING]
> **Experimental software.** CoreELEC or Kodi updates can break it.

No support is provided.

## Hardware

Requires an Amlogic device with a Mali GPU running CoreELEC 22
(Kodi 22, 64-bit). Tested on:

- Ugoos AM9 Pro (S905X5M, Mali-G310)
- Ugoos AM6B+ (S922X, Mali-G52)

Other devices and CoreELEC versions have not been tested.

## Installation

Install [Oriel WebKit](https://github.com/GR3ee3N/oriel-webkit#installation)
and [Oriel display](https://github.com/GR3ee3N/oriel-display#installation)
for Kodi 22 first.

For manual installation, download `script.module.oriel.framework-<version>.zip` from
the [releases page](https://github.com/GR3ee3N/oriel-framework/releases), then:

1. In Kodi, enable **Settings → System → Add-ons → Unknown sources**.
2. Go to **Add-ons → Install from zip file** and select the downloaded ZIP.

Apps can install this add-on as a dependency through the
[Oriel repository](https://github.com/GR3ee3N/oriel-repository).

## Checks and builds

Use Linux on x86_64 or ARM64 with Docker running. From the repository root:

```sh
tools/check.sh
tools/build.sh
```

`tools/check.sh` runs ShellCheck, Python tests, the Python-to-TypeScript
contract check, Rust formatting checks, TypeScript checking and Vitest
inside Docker. It does not download WebKit or compile the shell.

`tools/build.sh` compiles the ARM64 shell, checks its library requirements
and stages the add-on in `.cache/runtime/script.module.oriel.framework`.
Both scripts work from extracted source archives without Git metadata.

`WEBKIT_VERSION` and `DISPLAY_VERSION` in `build/versions.env` select the
WebKit development package and display protocol package used to build the
shell. They also set the minimum add-on versions required by the framework.
Downloads and build caches are stored in `.cache`.

To build against unpublished dependencies, set `DISPLAY_DEV` to a staged
`.cache/runtime/protocol` directory or `WEBKIT_DEV` to a WebKit development
package directory. These paths can point to any checkout.

## Writing an app

An app contains an `oriel.App` subclass and a web page, packaged as a Kodi
add-on that depends on `script.module.oriel.framework`.
[docs/apps.md](docs/apps.md) documents the Python API, HTTP protocol and
`@oriel/web` exports.

`examples/hello` is a complete example using plain HTML and the HTTP API.
The [Oriel Plex client](https://github.com/GR3ee3N/Oriel) uses Solid and
`@oriel/web` for browsing and video playback.

## Device testing

After building, deploy to a CoreELEC device over SSH:

```sh
BOX=root@coreelec.local tools/deploy.sh
```

Set `BOX` to the device's SSH address. Deployment requires SSH, rsync and
Kodi's JSON-RPC TCP service. The script waits for add-on registration and
verifies activation on each installation attempt.

The script installs the framework and `tools/box`. Install WebKit and Oriel
display separately through Kodi or their deployment scripts. Pass
`--example` to install the example app too. Exit and reopen running Oriel
apps to load the installed framework.

## Benchmarks

`tools/box/bench/bench.py` measures a framework app and the stack under it
on the box; the deploy script installs it with the other box tools. It has
two suites:

- **stack** loads `tools/box/bench/stack.html` in place of the app's page.
  The page draws its pictures from fixed seeds, so every box renders the
  same pixels without a network. It runs seven 5-second phases (still page,
  small paint, full-screen paint, 24 moving layers, a scrolling poster grid,
  a backdrop crossfade under a blurring panel, text reflow), a fixed script
  workload, eight 1920x1080 JPEG decodes and 16 keys sent through Kodi.
- **app** opens the app's own page and plays a scenario, a JSON list of key
  steps.

The scenario's `app` field names the add-on to open. Without a scenario,
`--app` names it; the stack suite works with any installed framework app.

```sh
ssh "$BOX" python3 /storage/oriel/tools/box/bench/bench.py run --suite stack --app <addon-id>
```

`--case` runs only the named cases, and `--list` prints them. An app step
still plays the steps before it, so it starts from the same screen, but
only the named steps are measured. `--repeat 2 --warmup 0` with one case
takes about a minute per suite, which suits trying out a change.

`--dev-page` measures the page at the `url` in the app's development
settings, such as a Vite server, in place of the installed page. The report
names the page it measured. `--env NAME=VALUE` adds a variable to the
shell's environment for every launch, such as a WebKit setting to try; the
report lists it.

Start with the box at Kodi's Home and nothing playing; bench.py closes a
running framework app. Each suite opens the app six times. The first run
warms caches and isn't counted; the report gives the median and range of
the other five. Before each launch bench.py waits for the CPUs to drop below
15% busy.

Only one run can use the box at a time; a second one exits with an error.
A run keeps going when the ssh session that started it closes, then saves
its report. `bench.py latest` prints the newest finished report folder.
`bench.py stop` ends a run, which closes the app and restores the app's
development settings first.

The report records the box, display mode, add-on versions, the CPU rendering
setting and the temperature, and warns when the CPU clocked below its
maximum under the performance governor. `report.json` holds the numbers and
`report.md` shows them in fixed tables. `bench.py compare BASE.json
NEW.json` lists the changes and exits with status 3 when a measure got
worse. A change counts when the medians differ by at
least 5% and by the measure's floor, such as 2 ms for times and 50 ms for
CPU time, and the ranges of the runs don't overlap.

Frame timing comes from the shell's `ORIEL_FRAME_LOG` (see
[docs/apps.md](docs/apps.md#development-settings)), which bench.py sets
for its runs. A framework without it still gives startup, CPU, GPU and
memory figures, but no frame timing.

## Releases

Update `WEBKIT_VERSION` and `DISPLAY_VERSION` when selecting newer
published dependencies.

Bump `VERSION` in `build/versions.env` and commit. From a clean working tree,
with the GitHub CLI (`gh`) authenticated to publish releases, run:

```sh
tools/release.sh "Release notes"
```

The script checks, builds and packages a snapshot of the committed source,
then uploads `.cache/release` files as a draft release under `v<version>`.
It rejects uncommitted changes, local `WEBKIT_DEV` or `DISPLAY_DEV`
overrides, and a checkout that changes during the release.
