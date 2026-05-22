#!/usr/bin/env bash
# Download the embedding and reranker models for Harbor Clerk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${BUILD_DIR:-$MACOS_DIR/build}"
MODEL_DIR="${MODEL_DIR:-$BUILD_DIR/model}"

VENV_DIR="${VENV_DIR:-$BUILD_DIR/venv}"
if [ -d "$VENV_DIR" ]; then
    PYTHON="$VENV_DIR/bin/python"
else
    PYTHON="python3"
fi

mkdir -p "$MODEL_DIR"

# ── Granite-R2 embedder (~700 MB) ──────────────────────────────────────────
GRANITE_DEST="$MODEL_DIR/granite-embedding-311m-multilingual-r2"
echo "==> Downloading Granite-R2 embedder model"
"$PYTHON" -c "
from sentence_transformers import SentenceTransformer
import os

model_name = 'ibm-granite/granite-embedding-311m-multilingual-r2'
dest = '${GRANITE_DEST}'

print(f'Loading model {model_name}...')
model = SentenceTransformer(model_name)
print(f'Saving to {dest}...')
model.save(dest)
print('Done.')
"
echo "==> Granite-R2 saved to ${GRANITE_DEST}"
echo "==> Size: $(du -sh "$GRANITE_DEST" | cut -f1)"

# ── bge-reranker-v2-m3 cross-encoder (~1.2 GB) ────────────────────────────
BGE_DEST="$MODEL_DIR/bge-reranker-v2-m3"
echo "==> Downloading bge-reranker-v2-m3 reranker model"
"$PYTHON" -c "
from sentence_transformers import CrossEncoder
import os

model_name = 'BAAI/bge-reranker-v2-m3'
dest = '${BGE_DEST}'

print(f'Loading model {model_name}...')
model = CrossEncoder(model_name)
# Save underlying HuggingFace model + tokenizer
model.model.save_pretrained(dest)
model.tokenizer.save_pretrained(dest)
print(f'Saved to {dest}')
print('Done.')
"
echo "==> bge-reranker-v2-m3 saved to ${BGE_DEST}"
echo "==> Size: $(du -sh "$BGE_DEST" | cut -f1)"
