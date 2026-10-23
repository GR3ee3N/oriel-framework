#!/bin/sh
# Install WebKit on the device first; pass --example to install examples/hello.
# Exit and reopen running apps after deployment to load the installed code.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BOX="${BOX:?set BOX to your box, e.g. BOX=root@coreelec.local}"
BASE=/storage/oriel
ADDONS=/storage/.kodi/addons
[ "$#" -le 1 ] || { echo "usage: tools/deploy.sh [--example]" >&2; exit 2; }
case "${1:-}" in
  "") EXAMPLE= ;;
  --example) EXAMPLE=1 ;;
  *) echo "usage: tools/deploy.sh [--example]" >&2; exit 2 ;;
esac
cd "$REPO"
STAGE=.cache/runtime/script.module.oriel.framework
[ -f "$STAGE/bin/oriel-shell" ] || { echo "run tools/build.sh first" >&2; exit 1; }
ssh "$BOX" "mkdir -p $BASE/tools/box"
rsync -rlt --exclude __pycache__ tools/box/ "$BOX:$BASE/tools/box/"

install_addon() {
  # Retain installed files until their replacements finish transferring.
  rsync -rlpt --exclude __pycache__ --delete-after --delay-updates "$1/" "$BOX:$ADDONS/$2/"
  ssh "$BOX" "kodi-send --action=UpdateLocalAddons >/dev/null"
  ssh "$BOX" "python3 $BASE/tools/box/kodirpc.py --enable '$2' >/dev/null"
  echo "installed $2"
}

echo "installed the box tools"
install_addon "$STAGE" script.module.oriel.framework
[ -z "$EXAMPLE" ] || install_addon examples/hello script.oriel.hello
