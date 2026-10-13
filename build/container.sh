#!/bin/bash
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
set -a
source build/versions.env
set +a
case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:aarch64) ;;
    *) echo "build tools require Linux on x86_64 or ARM64" >&2; exit 1 ;;
esac
command -v docker >/dev/null || { echo "install and start Docker before building" >&2; exit 1; }
# Release snapshots share the checkout's dependency caches and output directory.
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
exec docker run --rm --init --user "$(id -u):$(id -g)" \
    -e NPM_CONFIG_CACHE=/work/.cache/npm \
    -e CARGO_HOME=/work/.cache/cargo -e RUSTUP_HOME=/work/.cache/rustup -e CARGO_TARGET_DIR=/work/.cache/cargo-target \
    "${DEV_MOUNT[@]}" -v "$REPO:/work" -v "$CACHE:/work/.cache" oriel-framework-builder "$@"
