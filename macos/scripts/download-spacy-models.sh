#!/usr/bin/env bash
# Download spaCy NER models into the venv.
# Env vars: VENV_DIR (path to venv)
set -euo pipefail

VENV_DIR="${VENV_DIR:?Set VENV_DIR to the venv path}"
PYTHON="$VENV_DIR/bin/python"

echo "Downloading spaCy models into $VENV_DIR ..."

"$PYTHON" -m spacy download en_core_web_sm
"$PYTHON" -m spacy download fr_core_news_sm

echo "spaCy models installed."
