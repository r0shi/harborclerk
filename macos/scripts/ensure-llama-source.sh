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
    want="$(git -C "$SRC" rev-parse -q --verify "refs/tags/$TAG^{commit}" 2>/dev/null || true)"
    have="$(git -C "$SRC" rev-parse -q --verify HEAD 2>/dev/null || true)"
    dirty="$(git -C "$SRC" status --porcelain --untracked-files=no)"
    if [ "$origin" = "$REPO" ] && [ -n "$want" ] && [ "$want" = "$have" ] && [ -z "$dirty" ]; then
        echo "    (using existing clone at $TAG)"
        exit 0
    fi
    # Only what this script itself leaves behind is replaced without asking: a
    # clean, detached checkout of some tag with no local branches, which is
    # what `git clone --depth 1 --branch <tag>` produces. Anything else may be
    # a person's clone with work in it, and the old script never deleted
    # anything; this one refuses and prints the command.
    branches="$(git -C "$SRC" for-each-ref --format='%(refname:short)' refs/heads/ | tr '\n' ' ')"
    on_branch="$(git -C "$SRC" symbolic-ref -q --short HEAD 2>/dev/null || true)"
    if [ "$origin" != "$REPO" ] || [ -n "$dirty" ] || [ -n "$branches" ] || [ -n "$on_branch" ]; then
        echo "error: $SRC is not a checkout this script made (origin '${origin:-none}', branches '${branches:-none}'," >&2
        echo "       modified files: ${dirty:+yes}${dirty:-no}); not touching it. To rebuild from scratch: rm -rf \"$SRC\"" >&2
        exit 1
    fi
    at="$(git -C "$SRC" describe --tags --always 2>/dev/null || echo unknown)"
    echo "    existing clone is at $at, not $TAG: replacing it"
    rm -rf "$SRC"
elif [ -e "$SRC" ]; then
    echo "error: $SRC exists and is not a git clone; not touching it" >&2
    exit 1
fi

git clone --depth 1 --branch "$TAG" "$REPO" "$SRC"
if ! git -C "$SRC" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null; then
    echo "error: '$TAG' is a branch, not a tag; the pin must be a release tag" >&2
    exit 1
fi
