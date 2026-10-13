#!/bin/sh
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ "$#" -eq 0 ] || { echo "usage: tools/check.sh" >&2; exit 2; }
exec "$REPO/build/container.sh" build/check.sh
