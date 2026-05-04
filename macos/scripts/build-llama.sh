#!/usr/bin/env bash
# Build llama-server (llama.cpp HTTP server) with Metal GPU support.
set -euo pipefail

DEST_DIR="${DEST_DIR:?DEST_DIR must be set}"
BUILD_DIR="${BUILD_DIR:-/tmp/llama-build}"
LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-b9018}"

mkdir -p "$DEST_DIR" "$BUILD_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"

echo "==> Cloning llama.cpp at tag $LLAMA_CPP_TAG"
if [ ! -d "$BUILD_DIR/llama.cpp" ]; then
    git clone --depth 1 --branch "$LLAMA_CPP_TAG" \
        https://github.com/ggml-org/llama.cpp.git "$BUILD_DIR/llama.cpp"
else
    echo "    (using existing clone)"
fi

echo "==> Building llama-server with Metal support"
cd "$BUILD_DIR/llama.cpp"
cmake -B build \
    -DGGML_METAL=ON \
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

# Copy Metal shader library if present
if [ -f build/bin/default.metallib ]; then
    cp build/bin/default.metallib "$DEST_DIR/default.metallib"
fi

echo "==> llama-server built: $(du -sh "$DEST_DIR/llama-server" | cut -f1)"
