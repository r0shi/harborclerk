#!/usr/bin/env bash
# Build llama-server (llama.cpp HTTP server) with Metal GPU support.
set -euo pipefail

DEST_DIR="${DEST_DIR:?DEST_DIR must be set}"
BUILD_DIR="${BUILD_DIR:-/tmp/llama-build}"
# A stable upstream release, not a rolling `b` build. docker-compose.yml pins
# the image of the same release (tests/test_llama_cpp_pin.py holds them
# together), so both deployments run one llama.cpp.
LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-v0.4.1}"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$DEST_DIR" "$BUILD_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"

echo "==> llama.cpp source at tag $LLAMA_CPP_TAG"
bash "$SCRIPTS_DIR/ensure-llama-source.sh" "$BUILD_DIR/llama.cpp" "$LLAMA_CPP_TAG"

echo "==> Building llama-server with Metal support"
cd "$BUILD_DIR/llama.cpp"
# LLAMA_OPENSSL defaults to ON and links whatever OpenSSL cmake finds, which on
# a build machine is Homebrew's, by absolute path. The bundle carries no OpenSSL,
# so that llama-server dies in dyld on any Mac without Homebrew. It only serves
# llama-server's own HTTPS downloads (`-hf`), which the product never uses: the
# app downloads models itself.
cmake -B build \
    -DGGML_METAL=ON \
    -DLLAMA_OPENSSL=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64

cmake --build build --target llama-server -j "$(sysctl -n hw.ncpu)"

echo "==> Copying llama-server binary"
cp build/bin/llama-server "$DEST_DIR/llama-server"
chmod +x "$DEST_DIR/llama-server"

# Copy the sibling dylibs (libllama, libggml*, libmtmd, ...) that
# llama-server links against. Without these, the binary's @rpath load
# commands fail (the build-time rpath points to the build directory,
# which is gone after `make clean`), and dyld errors out before the
# server can start. The `-a` flag preserves symlinks so the major-version
# and unversioned symlinks remain valid.
echo "==> Copying llama dylibs"
cp -a build/bin/*.dylib "$DEST_DIR/"

# Make llama-server's rpath portable: drop the build-time absolute path
# and add @loader_path so dyld resolves the dylibs next to the binary
# regardless of where the bundle ends up installed. Without this, the
# binary fails to launch with "Library not loaded: @rpath/libmtmd.0.dylib"
# the moment the build directory is cleaned. `-delete_rpath` is
# best-effort: clobber-tolerant for the case where the build-time
# absolute path isn't actually present (e.g. a future cmake change).
echo "==> Fixing llama-server rpath"
BUILD_RPATH="$BUILD_DIR/llama.cpp/build/bin"
install_name_tool -delete_rpath "$BUILD_RPATH" "$DEST_DIR/llama-server" 2>/dev/null || true
install_name_tool -add_rpath @loader_path "$DEST_DIR/llama-server"
codesign --force --sign - "$DEST_DIR/llama-server"

# Everything copied must load on a Mac that has only the bundle and the OS.
# Homebrew's OpenSSL got in unnoticed for months because both build machines
# have Homebrew. A dylib's first `otool -L` entry is its own install name.
echo "==> Checking that nothing links outside the bundle and the OS"
NOT_PORTABLE=""
for f in "$DEST_DIR/llama-server" "$DEST_DIR"/*.dylib; do
    [ -L "$f" ] && continue
    own="$(otool -D "$f" | tail -n +2)"
    deps="$(otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -vxF "${own:-/no/install/name}" \
        | grep -Ev '^(@rpath|@loader_path|/System/Library|/usr/lib)/' || true)"
    if [ -n "$deps" ]; then
        NOT_PORTABLE+="$(basename "$f"): $(tr '\n' ' ' <<<"$deps")"$'\n'
    fi
done
if [ -n "$NOT_PORTABLE" ]; then
    echo "error: these would not load on a Mac without this machine's libraries:" >&2
    printf '%s' "$NOT_PORTABLE" >&2
    exit 1
fi

# Copy Metal shader library if present
if [ -f build/bin/default.metallib ]; then
    cp build/bin/default.metallib "$DEST_DIR/default.metallib"
fi

# The binary names the commit it was built from. It must be the pinned tag's:
# a stale build tree is the other way to ship the wrong version.
WANT_COMMIT="$(git rev-parse --short=7 HEAD)"
BUILT="$("$DEST_DIR/llama-server" --version 2>&1 || true)"
if ! grep -q "$WANT_COMMIT" <<<"$BUILT"; then
    echo "error: the built llama-server does not report commit $WANT_COMMIT ($LLAMA_CPP_TAG):" >&2
    echo "$BUILT" >&2
    exit 1
fi

echo "==> llama-server built at $LLAMA_CPP_TAG ($WANT_COMMIT): $(du -sh "$DEST_DIR/llama-server" | cut -f1)"
