#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
shellcheck -x -e SC2029 tools/*.sh build/*.sh
python3 -m unittest discover -s tests -t . -v
(cd web && npm ci && npm run check && npm test)
