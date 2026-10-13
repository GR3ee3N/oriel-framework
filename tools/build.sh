#!/bin/sh
# WEBKIT_DEV=<oriel-webkit>/.cache/runtime/dev uses a local WebKit build.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$#" -eq 0 ] || { echo "usage: tools/build.sh" >&2; exit 2; }
exec "$REPO/build/container.sh" build/compile.sh
