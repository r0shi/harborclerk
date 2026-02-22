#!/usr/bin/env bash
# Download python-build-standalone and create a venv with harbor-clerk + embedder.
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DEST_DIR="${DEST_DIR:-$(pwd)/build}"
mkdir -p "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"
PYTHON_DIR="$DEST_DIR/python"
VENV_DIR="$DEST_DIR/venv"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

echo "==> Setting up Python ${PYTHON_VERSION} + venv"

# ── Download python-build-standalone ──
if [ ! -d "$PYTHON_DIR" ]; then
    echo "==> Downloading python-build-standalone"
    mkdir -p "$PYTHON_DIR"

    # Find the latest release for the target Python version
    RELEASE_URL=$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
        | python3 -c "
import json, sys
data = json.load(sys.stdin)
for asset in data['assets']:
    name = asset['name']
    if 'cpython-${PYTHON_VERSION}' in name and 'aarch64-apple-darwin' in name and 'install_only' in name and name.endswith('.tar.gz'):
        print(asset['browser_download_url'])
        break
")

    if [ -z "$RELEASE_URL" ]; then
        echo "ERROR: Could not find python-build-standalone release for Python ${PYTHON_VERSION} arm64"
        exit 1
    fi

    echo "==> Downloading from ${RELEASE_URL}"
    curl -fsSL "$RELEASE_URL" | tar xz -C "$PYTHON_DIR" --strip-components=1
fi

PYTHON_BIN="$PYTHON_DIR/bin/python3"

# ── Create venv ──
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# ── Install packages ──
echo "==> Installing harbor-clerk"
"$VENV_DIR/bin/pip" install --no-cache-dir "$PROJECT_ROOT"

echo "==> Installing embedder"
"$VENV_DIR/bin/pip" install --no-cache-dir "$PROJECT_ROOT/embedder"

echo "==> Installing striprtf"
"$VENV_DIR/bin/pip" install --no-cache-dir striprtf

# Make the venv relocatable by patching shebangs
echo "==> Patching shebangs for relocatability"
for script in "$VENV_DIR/bin/"*; do
    if [ -f "$script" ] && head -1 "$script" | grep -q "^#!.*python"; then
        sed -i '' "1s|.*|#!/usr/bin/env python3|" "$script"
    fi
done

echo "==> Venv installed to ${VENV_DIR}"
echo "==> Size: $(du -sh "$VENV_DIR" | cut -f1)"
