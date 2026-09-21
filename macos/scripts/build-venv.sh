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

# ── What the lock pins, with hashes ──
# The app ships exactly what `uv.lock` pins, which is what CI tests and what `dependency-audit` audits. It
# used to `pip install --upgrade` the two projects, which upgrades the project and leaves every dependency at
# whatever this long-lived venv first received. The signed build of 2026-09-20 carried torch 2.11.0 against a
# lock at 2.13.0, and on MPS that torch keeps about 1 GB for every new input shape and never returns it: the
# embedder reached 43 GB six minutes into an ingest, every later embed failed, and the Mac went 58 GB into swap
# (#685). The same build carried anyio 4.13.0 after the lock had moved to 4.14.2 for a critical advisory.
#
# One venv hosts both projects and their locks disagree on a few shared pins, so the root lock decides: it is
# the superset, and already pins torch, transformers and sentence-transformers for the reranker.
command -v uv >/dev/null || { echo "ERROR: uv is required to export the lock (https://docs.astral.sh/uv/)"; exit 1; }
REQUIREMENTS="$DEST_DIR/requirements.lock.txt"
# --no-header: uv's header records the command line, output path included, so with it the hash below would
# change with the build directory and the uv version instead of with the lock.
(cd "$PROJECT_ROOT" && uv export --quiet --frozen --no-dev --no-emit-project --no-header \
    --format requirements-txt --output-file "$REQUIREMENTS")
LOCK_SHA="$(shasum -a 256 "$REQUIREMENTS" | cut -d' ' -f1)"

# ── Create venv ──
# A venv built from another lock is not upgraded in place: pip would leave behind whatever the new lock no
# longer names, and that is how an unpinned package ships. It is rebuilt.
if [ ! -x "$VENV_DIR/bin/python3" ] || [ "$(cat "$VENV_DIR/.lock-sha256" 2>/dev/null)" != "$LOCK_SHA" ]; then
    echo "==> Creating virtual environment (lock ${LOCK_SHA:0:12})"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Use the venv's python directly (not pip script, since shebangs get patched later)
VENV_PYTHON="$VENV_DIR/bin/python3"

# ── Ensure pip is installed ──
# Fresh venvs from `python -m venv` ship with pip. But this script strips pip
# at the end (see "Removing pip from runtime venv" below — the bundled appliance
# is frozen and doesn't need pip at runtime). Reusing a cached venv from a
# prior successful build means pip is gone, so the install step below would
# fail with "No module named pip". `ensurepip --default-pip` is a stdlib no-op
# when pip is already present and reinstalls from the bundled wheel otherwise.
echo "==> Ensuring pip is installed"
"$VENV_PYTHON" -m ensurepip --default-pip

# ── Install packages ──
echo "==> Installing the locked dependencies"
"$VENV_PYTHON" -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r "$REQUIREMENTS"

# The local packages, and nothing else: --no-deps, so pip resolves nothing the lock did not. --upgrade because
# their version does not change when their source does, and a stale copy inside a fresh app has shipped before.
echo "==> Installing harbor-clerk and embedder"
"$VENV_PYTHON" -m pip install --no-cache-dir --disable-pip-version-check --no-deps --upgrade \
    "$PROJECT_ROOT" "$PROJECT_ROOT/embedder"

# Every requirement of both projects must be met by what the lock installed. This is where an embedder
# requirement the root lock cannot satisfy would show.
"$VENV_PYTHON" -m pip check --disable-pip-version-check
echo "$LOCK_SHA" > "$VENV_DIR/.lock-sha256"

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
