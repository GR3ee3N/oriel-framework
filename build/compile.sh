#!/bin/bash
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
set -a
source build/versions.env
set +a
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $WEBKIT_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $DISPLAY_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "VERSION, WEBKIT_VERSION and DISPLAY_VERSION must be x.y.z" >&2; exit 2; }
CACHE=$REPO/.cache
export CARGO_HOME=$CACHE/cargo RUSTUP_HOME=$CACHE/rustup CARGO_TARGET_DIR=$CACHE/cargo-target
DISPLAY_ARCHIVE=$CACHE/display/oriel-display-$DISPLAY_VERSION-protocol.tar.xz
if [[ -z ${DISPLAY_DEV:-} && ! -f $DISPLAY_ARCHIVE ]]; then
    curl -fL --create-dirs -o "$DISPLAY_ARCHIVE.part" \
        "https://github.com/GR3ee3N/oriel-display/releases/download/v$DISPLAY_VERSION/${DISPLAY_ARCHIVE##*/}"
    mv "$DISPLAY_ARCHIVE.part" "$DISPLAY_ARCHIVE"
fi
mkdir -p "$CACHE/display"
rm -rf "$CACHE/display/protocol"
if [[ -n ${DISPLAY_DEV:-} ]]; then
    cp -r "$DISPLAY_DEV" "$CACHE/display/protocol"
else
    python3 - "$DISPLAY_ARCHIVE" "$CACHE/display" <<'PY'
from pathlib import Path
import sys
import tarfile
with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(Path(sys.argv[2]), filter="data")
PY
fi
DEV_ARCHIVE=$CACHE/webkit/oriel-webkit-$WEBKIT_VERSION-dev.tar.xz
if [[ -z ${WEBKIT_DEV:-} && ! -f $DEV_ARCHIVE ]]; then
    curl -fL --create-dirs -o "$DEV_ARCHIVE.part" \
        "https://github.com/GR3ee3N/oriel-webkit/releases/download/v$WEBKIT_VERSION/${DEV_ARCHIVE##*/}"
    mv "$DEV_ARCHIVE.part" "$DEV_ARCHIVE"
fi
DEV=${WEBKIT_DEV:-$CACHE/webkit/dev}
if [[ -z ${WEBKIT_DEV:-} ]]; then
    rm -rf "$DEV"
    mkdir -p "$DEV"
    tar -xJf "$DEV_ARCHIVE" --strip-components=1 -C "$DEV"
fi

# Debian's ARM64 compiler and libc, linking against WebKit's dev sysroot.
TARGET=aarch64-unknown-linux-gnu
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
# Mali satisfies graphics symbols on the box.
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-Wl,--allow-shlib-undefined -C link-arg=-Wl,-rpath-link,$DEV/sysroot/usr/lib"
export PKG_CONFIG_ALLOW_CROSS=1 PKG_CONFIG_SYSROOT_DIR=$DEV/sysroot
export PKG_CONFIG_LIBDIR=$DEV/sysroot/usr/lib/pkgconfig:$DEV/sysroot/usr/share/pkgconfig
rustup toolchain install "$(sed -n 's/channel = "\(.*\)"/\1/p' rust-toolchain.toml)" --profile minimal --target "$TARGET"
cargo build --locked --release --target "$TARGET" -p oriel-shell
SHELL_BINARY=$CARGO_TARGET_DIR/$TARGET/release/oriel-shell
python3 build/check-shell.py "$SHELL_BINARY" "$DEV"

ADDON=$CACHE/runtime/script.module.oriel.framework
rm -rf "$CACHE/runtime"
mkdir -p "$ADDON"
rsync -a --exclude __pycache__ addon/ "$ADDON/"
install -D -m 755 "$SHELL_BINARY" "$ADDON/bin/oriel-shell"
sed -i "s/@VERSION@/$VERSION/g; s/@WEBKIT_VERSION@/$WEBKIT_VERSION/g; s/@DISPLAY_VERSION@/$DISPLAY_VERSION/g" "$ADDON/addon.xml"
echo "Built $ADDON with the ARM64 shell."
