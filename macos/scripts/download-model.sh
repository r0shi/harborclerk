#!/usr/bin/env bash
# Download the embedding model for Harbor Clerk.
# MODEL_REPO must be a full HuggingFace repo ID (e.g. nomic-ai/nomic-embed-text-v1.5).
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-intfloat/multilingual-e5-small}"
MODEL_SHORT="${MODEL_REPO##*/}"
DEST_DIR="${DEST_DIR:-$(pwd)/build/model/${MODEL_SHORT}}"

echo "==> Downloading embedding model: ${MODEL_REPO}"

mkdir -p "$DEST_DIR"

# Use Python to download via sentence_transformers (or huggingface_hub)
VENV_DIR="${VENV_DIR:-$(pwd)/build/venv}"
if [ -d "$VENV_DIR" ]; then
    PYTHON="$VENV_DIR/bin/python"
else
    PYTHON="python3"
fi

"$PYTHON" -c "
from sentence_transformers import SentenceTransformer
import shutil, os

model_name = '${MODEL_REPO}'
dest = '${DEST_DIR}'

print(f'Loading model {model_name}...')
model = SentenceTransformer(model_name)

# Save to destination
print(f'Saving to {dest}...')
model.save(dest)
print('Done.')
"

echo "==> Model saved to ${DEST_DIR}"
echo "==> Size: $(du -sh "$DEST_DIR" | cut -f1)"
