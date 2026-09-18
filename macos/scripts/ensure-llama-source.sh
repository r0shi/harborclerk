#!/usr/bin/env bash
# Make <dir> a clean checkout of llama.cpp at exactly <tag>.
#
#   ensure-llama-source.sh <dir> <tag>
#
# An existing clone is kept only when HEAD is that tag's commit and no tracked
# file is modified, so the cmake build tree inside it survives between builds
# of one version. Anything else is replaced: a clone left by an older pin used
# to be reused as-is, so bumping the pin rebuilt the old version without a word.
set -euo pipefail

SRC="${1:?usage: ensure-llama-source.sh <dir> <tag>}"
TAG="${2:?usage: ensure-llama-source.sh <dir> <tag>}"
REPO="${LLAMA_CPP_REPO:-https://github.com/ggml-org/llama.cpp.git}"

if [ -d "$SRC/.git" ]; then
    origin="$(git -C "$SRC" config --get remote.origin.url || true)"
    if [ "$origin" != "$REPO" ]; then
        echo "error: $SRC is a clone of '${origin:-nothing}', not of $REPO; not touching it" >&2
        exit 1
    fi
    want="$(git -C "$SRC" rev-parse -q --verify "refs/tags/$TAG^{commit}" 2>/dev/null || true)"
    have="$(git -C "$SRC" rev-parse -q --verify HEAD 2>/dev/null || true)"
    if [ -n "$want" ] && [ "$want" = "$have" ] && [ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
        echo "    (using existing clone at $TAG)"
        exit 0
    fi
    at="$(git -C "$SRC" describe --tags --always 2>/dev/null || echo unknown)"
    echo "    existing clone is at $at, not a clean checkout of $TAG: replacing it"
    rm -rf "$SRC"
elif [ -e "$SRC" ]; then
    echo "error: $SRC exists and is not a git clone; not touching it" >&2
    exit 1
fi

git clone --depth 1 --branch "$TAG" "$REPO" "$SRC"
