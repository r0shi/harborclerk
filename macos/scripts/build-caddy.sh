#!/usr/bin/env bash
# Copy Homebrew Caddy into the native macOS bundle resources.
set -euo pipefail

DEST_DIR="${DEST_DIR:?DEST_DIR must be set}"
CADDY_DIR="$DEST_DIR"

echo "==> Preparing Caddy"

mkdir -p "$CADDY_DIR/bin"

if [ -x "$CADDY_DIR/bin/caddy" ]; then
    echo "==> Caddy already present, skipping"
    exit 0
fi

brew list caddy &>/dev/null || brew install caddy

CADDY_PREFIX="$(brew --prefix caddy)"
cp "$CADDY_PREFIX/bin/caddy" "$CADDY_DIR/bin/caddy"
chmod +x "$CADDY_DIR/bin/caddy"

# Ad-hoc sign so macOS will execute the binary from inside the app bundle.
codesign --force --sign - "$CADDY_DIR/bin/caddy" 2>/dev/null || true

echo "==> Caddy installed to ${CADDY_DIR}"
