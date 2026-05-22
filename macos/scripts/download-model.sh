#!/usr/bin/env bash
# Download the embedding + reranker models for Harbor Clerk's macOS bundle.
#
# Both models are fetched with huggingface_hub.snapshot_download into
# MODEL_DIR/<name>. The <name> directories must match the paths the Swift
# services load (EmbedderService.swift, RerankerService.swift) and the names
# the Docker images use. snapshot_download writes real files into local_dir
# (huggingface_hub 1.x has no symlink-into-cache mode), so the resulting tree
# is safe to bundle into a relocatable .app.
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

# download_model <hf-repo-id> <local-dir-name> [ignore-pattern ...]
download_model() {
    local repo_id="$1"
    local name="$2"
    shift 2
    local dest="$MODEL_DIR/$name"
    echo "==> Downloading ${repo_id}"
    # Re-fetch into a clean directory: makes the result deterministic and
    # stops ignore_patterns from leaving stale files behind on a rebuild.
    rm -rf "$dest"
    "$PYTHON" - "$repo_id" "$dest" "$@" <<'PY'
import sys

from huggingface_hub import snapshot_download

repo_id, dest, *ignore = sys.argv[1:]
snapshot_download(repo_id=repo_id, local_dir=dest, ignore_patterns=ignore or None)
print(f"==> {repo_id} saved to {dest}")
PY
    # snapshot_download leaves a .cache/ with download bookkeeping; not used at runtime.
    rm -rf "$dest/.cache"
    echo "==> Size: $(du -sh "$dest" | cut -f1)"
}

# Granite-R2 embedder — EmbedderService.swift loads model/granite-embedding-311m-multilingual-r2.
# Skip the onnx/ + openvino/ variants (~3 GB): the embedder loads the PyTorch backend only.
download_model "ibm-granite/granite-embedding-311m-multilingual-r2" \
    "granite-embedding-311m-multilingual-r2" \
    "onnx/*" "openvino/*" "openvino_model.*" \
    "README.md" ".gitattributes"

# bge-reranker-v2-m3 cross-encoder — RerankerService.swift loads model/bge-reranker-v2-m3.
# Skip the README assets (benchmark PNGs) and git metadata: not needed at runtime.
download_model "BAAI/bge-reranker-v2-m3" "bge-reranker-v2-m3" \
    "assets/*" "README.md" ".gitattributes"

# Convert reranker weights to fp16. The released fp32 weights are all exactly
# fp16-representable (the lower 13 mantissa bits are zero — verified by direct
# round-trip across all 568M weights), so this halves the file from ~2.1 GB to
# ~1.05 GB with provably zero change to model outputs when the loader upcasts
# to fp32 (see embedder/src/embedder/reranker.py).
echo "==> Converting bge-reranker-v2-m3 weights to fp16"
"$PYTHON" - "$MODEL_DIR/bge-reranker-v2-m3" <<'PY'
import json
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

dest = Path(sys.argv[1])

weights = load_file(str(dest / "model.safetensors"))
weights_fp16 = {k: v.to(torch.float16) for k, v in weights.items()}
save_file(weights_fp16, str(dest / "model.safetensors"), metadata={"format": "pt"})

cfg_path = dest / "config.json"
cfg = json.loads(cfg_path.read_text())
cfg["torch_dtype"] = "float16"
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"==> converted weights to fp16; updated {cfg_path.name}")
PY
echo "==> Reranker size after conversion: $(du -sh "$MODEL_DIR/bge-reranker-v2-m3" | cut -f1)"

echo "==> All models downloaded to ${MODEL_DIR}"
