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

# ── Ensure pip is installed ──
# Fresh venvs from `python -m venv` ship with pip. But this script strips pip
# at the end (see "Removing pip from runtime venv" below — the bundled appliance
# is frozen and doesn't need pip at runtime). Reusing a cached venv from a
# prior successful build means pip is gone, so the upgrade step below would
# fail with "No module named pip". `ensurepip --default-pip` is a stdlib no-op
# when pip is already present and reinstalls from the bundled wheel otherwise.
echo "==> Ensuring pip is installed"
"$VENV_PYTHON" -m ensurepip --default-pip

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
#
# The guard is `find_spec`, and that is satisfied by a *directory*, not by a
# working package. `pip uninstall` will not remove a directory that still holds
# a file it did not install — and on a Mac that file is `.DS_Store`, dropped by
# Finder the moment anyone browses the venv. The empty `plotly/` left behind is
# an implicit namespace package, so `find_spec("plotly")` returns a spec,
# bertopic concludes plotly is available, skips MockPlotlyModule, and then dies
# on `import plotly.express`.
#
# That shipped: topic computation was broken in every build, not degraded —
# `recompute_topics` raised ModuleNotFoundError and the Topics view stayed
# permanently empty. The warmup log line said "topic computation will be slower
# on first use", which is why it read as cosmetic for so long.
echo "==> Removing plotly (unused transitive dep)"
"$VENV_PYTHON" -m pip uninstall -y --disable-pip-version-check plotly || true
rm -rf "$VENV_DIR/lib"/python*/site-packages/plotly

# Assert the guard actually sees it as absent. Checking the directory is gone is
# not the same question — what matters is what `find_spec` returns, which is the
# thing bertopic branches on.
if ! "$VENV_PYTHON" - <<'PY'
import sys
from importlib.util import find_spec
sys.exit(0 if find_spec("plotly") is None else 1)
PY
then
    echo "ERROR: find_spec('plotly') still resolves after removal." >&2
    echo "       bertopic will skip its fallback and fail on import plotly.express," >&2
    echo "       breaking topic computation in the shipped app. Check for a" >&2
    echo "       leftover site-packages/plotly directory (.DS_Store et al)." >&2
    exit 1
fi

# Strip bundled test suites from every installed package. Never imported at
# runtime. Saves ~150 MB across pandas/tests, numba/tests, scipy submodule
# tests, networkx/<sub>/tests, sympy submodule tests, etc.
#
# Restricted to `-name tests` (plural) on purpose. Many packages have a
# `testing` submodule that is real public API our dependencies use
# transitively -- torch.testing (imported by torch.autograd.gradcheck),
# numpy.testing, sympy.testing, sqlalchemy.testing, alembic.testing, etc.
# Likewise `test` (singular) appears in torch/include/c10/test and similar
# header-tree paths that aren't test SUITES. The earlier pattern that also
# matched `test` and `testing` broke `import torch` at runtime.
echo "==> Stripping bundled test suites from site-packages"
find "$VENV_DIR/lib/python${PYTHON_VERSION}/site-packages" \
    -type d -name tests \
    -prune -exec rm -rf {} +

# Remove pip from the runtime venv: this is a frozen appliance, not a
# pip-managed environment. The pip-wrapper block further down silently skips
# when bin/pip* no longer exists.
echo "==> Removing pip from runtime venv"
"$VENV_PYTHON" -m pip uninstall -y --disable-pip-version-check pip || true
# Same idiom as the plotly removal above, swept for deliberately: pip uninstall
# leaves the directory when a stray file (.DS_Store) is inside it. No consumer
# branches on `find_spec("pip")` the way bertopic does on plotly, so this is
# hygiene rather than a fix — but leaving a namespace-package stub behind is
# exactly what made the plotly one invisible for so long.
rm -rf "$VENV_DIR/lib"/python*/site-packages/pip

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
