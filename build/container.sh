#!/bin/bash
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
set -a
source build/versions.env
set +a
# Docker on an Apple silicon Mac runs the ARM64 Linux build container natively.
case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:aarch64|Darwin:arm64) ;;
    *) echo "build tools require Linux on x86_64 or ARM64, or a Mac with Apple silicon" >&2; exit 1 ;;
esac
command -v docker >/dev/null || { echo "install and start Docker before building" >&2; exit 1; }
command -v flock >/dev/null || { echo "install flock before building" >&2; exit 1; }
# Reuse checkout dependency caches and output paths for release snapshots.
CACHE=${ORIEL_CACHE:-$REPO/.cache}
mkdir -p "$CACHE"
CACHE=$(cd "$CACHE" && pwd)
exec 9>"$CACHE/build.lock"
flock -n 9 || { echo "another local build or check is running" >&2; exit 1; }
DEV_MOUNT=()
if [[ $1 != build/check.sh && -n ${WEBKIT_DEV:-} ]]; then
    [[ -f $WEBKIT_DEV/provided.txt ]] || { echo "WEBKIT_DEV must be a WebKit build's dev directory" >&2; exit 2; }
    DEV_MOUNT=(-v "$(cd "$WEBKIT_DEV" && pwd):/webkit-dev:ro" -e WEBKIT_DEV=/webkit-dev)
fi
docker build --build-arg "DEBIAN_SNAPSHOT=$DEBIAN_SNAPSHOT" -t oriel-framework-builder -f build/Dockerfile build
DISPLAY_MOUNT=()
if [[ $1 != build/check.sh && -n ${DISPLAY_DEV:-} ]]; then
    [[ -f $DISPLAY_DEV/Cargo.toml && -f $DISPLAY_DEV/LICENSE ]] || { echo "DISPLAY_DEV must be a display build's protocol directory" >&2; exit 2; }
    DISPLAY_MOUNT=(-v "$(cd "$DISPLAY_DEV" && pwd):/display-dev:ro" -e DISPLAY_DEV=/display-dev)
fi
# Expand the optional mounts this way for macOS's bash 3.2, which rejects empty arrays under set -u.
exec docker run --rm --init --user "$(id -u):$(id -g)" \
    -e NPM_CONFIG_CACHE=/work/.cache/npm \
    -e CARGO_HOME=/work/.cache/cargo -e RUSTUP_HOME=/work/.cache/rustup -e CARGO_TARGET_DIR=/work/.cache/cargo-target \
    ${DEV_MOUNT[@]+"${DEV_MOUNT[@]}"} ${DISPLAY_MOUNT[@]+"${DISPLAY_MOUNT[@]}"} -v "$REPO:/work" -v "$CACHE:/work/.cache" oriel-framework-builder "$@"
