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
if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "==> Creating virtual environment"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Use the venv's python directly (not pip script, since shebangs get patched later)
VENV_PYTHON="$VENV_DIR/bin/python3"

# ── Upgrade pip ──
# python-build-standalone ships an older pip; upgrade so subsequent installs
# don't print the "new release available" notice on every invocation.
echo "==> Upgrading pip"
"$VENV_PYTHON" -m pip install --no-cache-dir --upgrade --disable-pip-version-check pip

# ── Install packages ──
echo "==> Installing harbor-clerk"
"$VENV_PYTHON" -m pip install --no-cache-dir --disable-pip-version-check --upgrade "$PROJECT_ROOT"

echo "==> Installing embedder"
"$VENV_PYTHON" -m pip install --no-cache-dir --disable-pip-version-check --upgrade "$PROJECT_ROOT/embedder"

if ! "$VENV_PYTHON" -c "import striprtf" 2>/dev/null; then
    echo "==> Installing striprtf"
    "$VENV_PYTHON" -m pip install --no-cache-dir --disable-pip-version-check striprtf
else
    echo "==> striprtf already installed, skipping"
fi

# ── Slim the venv ──
# Remove plotly: pulled in transitively by bertopic, but bertopic guards with
# `find_spec("plotly")` and falls back to MockPlotlyModule. Nothing in this app
# calls bertopic's plotting methods. Saves ~64 MB.
echo "==> Removing plotly (unused transitive dep)"
"$VENV_PYTHON" -m pip uninstall -y --disable-pip-version-check plotly || true

# Strip bundled test suites from every installed package. Never imported at
# runtime. Saves ~160 MB across pandas/tests, numba/tests, scipy submodule
# tests, spaCy/tests, etc.
echo "==> Stripping bundled test suites from site-packages"
find "$VENV_DIR/lib/python${PYTHON_VERSION}/site-packages" \
    -type d \( -name tests -o -name test -o -name testing \) \
    -prune -exec rm -rf {} +

# Remove pip from the runtime venv: this is a frozen appliance, not a
# pip-managed environment. The pip-wrapper block further down silently skips
# when bin/pip* no longer exists.
echo "==> Removing pip from runtime venv"
"$VENV_PYTHON" -m pip uninstall -y --disable-pip-version-check pip || true

# Make the venv relocatable
echo "==> Making venv relocatable"

# Fix python3 symlink: venv creates an absolute symlink to the build Python;
# repoint it to a relative path so the bundle is self-contained.
if [ -L "$VENV_DIR/bin/python3" ]; then
    rm "$VENV_DIR/bin/python3"
    ln -s ../../python/bin/python3 "$VENV_DIR/bin/python3"
fi

# Patch shebangs
for script in "$VENV_DIR/bin/"*; do
    if [ -f "$script" ] && head -1 "$script" | grep -q "^#!.*$VENV_DIR"; then
        sed -i '' "1s|.*|#!/usr/bin/env python3|" "$script"
    fi
done

# The shebang patch above (#!/usr/bin/env python3) is fine for entry
# points launched by the bundled app via known absolute paths, but it's
# a footgun for `pip`: an operator running the venv's pip from a shell
# where another `python3` (Homebrew, asdf, system) is first on PATH will
# silently install into THAT Python's site-packages and assume the
# bundled venv was updated. The wrapper below resolves the venv's python
# relative to the wrapper's own location, which is stable wherever the
# bundle ends up installed.
for pip_script in "$VENV_DIR/bin/"pip*; do
    [ -f "$pip_script" ] || continue
    cat > "$pip_script" <<'SH'
#!/bin/sh
exec "$(dirname "$0")/python3" -m pip "$@"
SH
    chmod +x "$pip_script"
done

echo "==> Venv installed to ${VENV_DIR}"
echo "==> Size: $(du -sh "$VENV_DIR" | cut -f1)"
