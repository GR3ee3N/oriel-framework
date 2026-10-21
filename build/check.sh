#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
shellcheck -x -e SC2029 tools/*.sh build/*.sh
python3 -m unittest discover -s tests -t . -v
# The shell compiles only against WebKit (build.sh); its formatting needs neither.
rustup toolchain install "$(sed -n 's/channel = "\(.*\)"/\1/p' rust-toolchain.toml)" --profile minimal --component rustfmt
rustfmt --edition 2024 --check crates/shell/src/main.rs crates/wpe-sys/src/lib.rs crates/wpe-sys/build.rs
(cd web && npm ci && npm run check && npm test)
