#!/usr/bin/env bash
# Download spaCy NER models into the venv.
# Uses `python -m pip` directly instead of `spacy download` because the
# latter delegates to uv/pip which can install into the wrong environment
# when the venv is relocatable or uv is on the PATH.
# Env vars: VENV_DIR (path to venv)
set -euo pipefail

VENV_DIR="${VENV_DIR:?Set VENV_DIR to the venv path}"
PYTHON="$VENV_DIR/bin/python"

SPACY_VERSION=$("$PYTHON" -c "import spacy; v=spacy.__version__.split('.'); print(f'{v[0]}.{v[1]}.0')")
echo "==> Downloading spaCy $SPACY_VERSION models into $VENV_DIR"

BASE_URL="https://github.com/explosion/spacy-models/releases/download"

for MODEL in en_core_web_sm fr_core_news_sm; do
    URL="$BASE_URL/${MODEL}-${SPACY_VERSION}/${MODEL}-${SPACY_VERSION}-py3-none-any.whl"
    echo "  -> $MODEL $SPACY_VERSION"
    "$PYTHON" -m pip install --no-deps "$URL" 2>/dev/null || {
        echo "  WARN: Failed to install $MODEL $SPACY_VERSION — NER will be unavailable for this language"
    }
done

echo "==> spaCy models installed"
