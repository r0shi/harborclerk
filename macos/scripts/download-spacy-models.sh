#!/usr/bin/env bash
# Download spaCy NER models into the venv.
# Uses `uv pip install --python` when available because the runtime venv is
# deliberately stripped of pip by build-venv.sh before this target runs.
# Falls back to bootstrapping pip only for environments without uv.
# Env vars: VENV_DIR (path to venv)
set -euo pipefail

VENV_DIR="${VENV_DIR:?Set VENV_DIR to the venv path}"
PYTHON="$VENV_DIR/bin/python"
PIP_BOOTSTRAPPED=0

SPACY_VERSION=$("$PYTHON" -c "import spacy; v=spacy.__version__.split('.'); print(f'{v[0]}.{v[1]}.0')")
echo "==> Downloading spaCy $SPACY_VERSION models into $VENV_DIR"

BASE_URL="https://github.com/explosion/spacy-models/releases/download"

install_model() {
    local url="$1"

    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$PYTHON" --no-deps "$url"
        return
    fi

    if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
        "$PYTHON" -m ensurepip --default-pip
        PIP_BOOTSTRAPPED=1
    fi

    "$PYTHON" -m pip install --no-cache-dir --disable-pip-version-check --no-deps "$url"
}

for MODEL in en_core_web_sm fr_core_news_sm; do
    URL="$BASE_URL/${MODEL}-${SPACY_VERSION}/${MODEL}-${SPACY_VERSION}-py3-none-any.whl"
    echo "  -> $MODEL $SPACY_VERSION"
    install_model "$URL"
done

if [ "$PIP_BOOTSTRAPPED" = "1" ]; then
    "$PYTHON" -m pip uninstall -y --disable-pip-version-check pip >/dev/null || true
fi

"$PYTHON" - <<'PY'
import spacy

for model in ("en_core_web_sm", "fr_core_news_sm"):
    spacy.load(model)
    print(f"  OK: {model} loads")
PY

echo "==> spaCy models installed and verified"
