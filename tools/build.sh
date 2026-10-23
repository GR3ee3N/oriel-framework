#!/bin/sh
# Set WEBKIT_DEV=<oriel-webkit>/.cache/runtime/dev to build against local WebKit.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$#" -eq 0 ] || { echo "usage: tools/build.sh" >&2; exit 2; }
exec "$REPO/build/container.sh" build/compile.sh
