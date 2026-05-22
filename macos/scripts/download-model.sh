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
    echo "==> Size: $(du -sh "$dest" | cut -f1)"
}

# Granite-R2 embedder — EmbedderService.swift loads model/granite-embedding-311m-multilingual-r2.
# Skip the onnx/ + openvino/ variants (~3 GB): the embedder loads the PyTorch backend only.
download_model "ibm-granite/granite-embedding-311m-multilingual-r2" \
    "granite-embedding-311m-multilingual-r2" \
    "onnx/*" "openvino/*" "openvino_model.*"

# bge-reranker-v2-m3 cross-encoder — RerankerService.swift loads model/bge-reranker-v2-m3
download_model "BAAI/bge-reranker-v2-m3" "bge-reranker-v2-m3"

echo "==> All models downloaded to ${MODEL_DIR}"
