#!/usr/bin/env bash
# Download the sentence-transformers model for embedding.
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-all-MiniLM-L6-v2}"
DEST_DIR="${DEST_DIR:-$(pwd)/build/model/${MODEL_NAME}}"

echo "==> Downloading sentence-transformers model: ${MODEL_NAME}"

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

model_name = '${MODEL_NAME}'
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
