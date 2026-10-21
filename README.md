# Oriel framework

Oriel framework runs web apps in Kodi on CoreELEC. WPE WebKit renders the
interface and Oriel display (`vfs.oriel.display`) draws it in Kodi's own GUI
frame, also over video. The framework connects the web interface to Kodi
and forwards remote control input to the page.

It has two parts:

- `script.module.oriel.framework` — the Kodi add-on containing the Python API
  and the Rust shell that hosts WebKit.
- `@oriel/web` — the TypeScript package for communication with the add-on,
  saved settings, playback controls and remote navigation.

The framework depends on the Oriel WebKit runtime and the Oriel display
binary add-on. Apps depend only on the framework.

## Status

> [!WARNING]
> **Oriel framework is highly experimental.** It is a personal project, tested on only
> a few boxes, and it relies on CoreELEC and Kodi internals that are outside its control.
> Any update to those can break it, and it could stop working at any time.
> Use it at your own risk.

No support is provided.

## Hardware

The framework targets Amlogic devices with a Mali GPU running CoreELEC 22
(Kodi 22, 64-bit). It has been tested on:

- Ugoos AM9 Pro (S905X5M)
- Ugoos AM6B+ (S922X)

Other devices and CoreELEC versions have not been tested.

## Installation

Install [Oriel WebKit](https://github.com/GR3ee3N/oriel-webkit#installation)
and [Oriel display](https://github.com/GR3ee3N/oriel-display#installation)
for Kodi 22. Then download
`script.module.oriel.framework-<version>.zip` from the
[releases page](https://github.com/GR3ee3N/oriel-framework/releases).

1. In Kodi, enable **Settings → System → Add-ons → Unknown sources**.
2. Go to **Add-ons → Install from zip file** and select the downloaded ZIP.

Apps can also install the framework as a dependency through the
[Oriel repository](https://github.com/GR3ee3N/oriel-repository).

## Building

Checking and building require Linux on x86_64 or ARM64 and a running Docker
installation. Run these commands from the repository root:

```sh
tools/check.sh
tools/build.sh
```

`check.sh` runs ShellCheck, the Python tests, the Python-to-TypeScript contract
check, TypeScript checking and Vitest inside Docker. It does not download WebKit
or compile the shell. `build.sh` builds the ARM64 shell, checks its library
requirements and stages the add-on. Both commands work from an extracted source
archive without Git metadata.

The shell builds against the WebKit development package and display protocol
package selected by `WEBKIT_VERSION` and `DISPLAY_VERSION` in
`build/versions.env`. The framework add-on requires those add-on versions or
newer. Downloads and build caches are kept in `.cache`.

For an unpublished display build, set `DISPLAY_DEV` to its staged
`.cache/runtime/protocol` directory. `WEBKIT_DEV` similarly accepts a WebKit
development package directory. Neither requires a particular checkout layout.

The built add-on is written to
`.cache/runtime/script.module.oriel.framework`.

## Writing an app

An app is a Kodi add-on that depends on `script.module.oriel.framework`: a
Python `oriel.App` subclass and a web page. [docs/apps.md](docs/apps.md)
describes the app interface, the page protocol and `@oriel/web`.
`examples/hello` is a minimal app, and the
[Oriel Plex client](https://github.com/GR3ee3N/Oriel) is a video app built
with the framework.

## Testing on a device

After building, deploy the add-on to a CoreELEC device over SSH:

```sh
BOX=root@coreelec.local tools/deploy.sh
```

Replace `root@coreelec.local` with the device's SSH address. Deployment requires
SSH, rsync and Kodi's JSON-RPC TCP service to verify add-on activation.
The script installs the framework and its box tools (`tools/box`); install
WebKit and Oriel display separately through Kodi or their deployment scripts.
`--example` also installs the example app. Reopen any running Oriel app to
load the updated framework.

## Releasing

Bump `VERSION` in `build/versions.env`. Update `WEBKIT_VERSION` and
`DISPLAY_VERSION` when building against new published releases, and commit
the changes. Build and publish from a clean working tree using the GitHub CLI
(`gh`), authenticated with an account that can publish releases:

```sh
tools/release.sh "Release notes"
```

`release.sh` checks, builds and packages a snapshot of the committed source,
then uploads the files in `.cache/release` as a draft under `v<version>`.
It refuses uncommitted changes, local `WEBKIT_DEV` or `DISPLAY_DEV` overrides,
or a checkout that changes while the release runs.
