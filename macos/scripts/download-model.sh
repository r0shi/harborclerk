#!/usr/bin/env bash
# Download the embedding model for Harbor Clerk.
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-nomic-embed-text-v1.5}"
DEST_DIR="${DEST_DIR:-$(pwd)/build/model/${MODEL_NAME}}"

echo "==> Downloading embedding model: ${MODEL_NAME}"

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
model = SentenceTransformer(model_name, trust_remote_code=True)

# Save to destination
print(f'Saving to {dest}...')
model.save(dest)
print('Done.')
"

echo "==> Model saved to ${DEST_DIR}"
echo "==> Size: $(du -sh "$DEST_DIR" | cut -f1)"
