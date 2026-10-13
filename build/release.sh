#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
source build/versions.env
set +a
build/check.sh
build/compile.sh
python3 build/package.py "$1" .cache/release-source.tar
