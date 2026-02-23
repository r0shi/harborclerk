#!/usr/bin/env bash
# Build llama-server (llama.cpp HTTP server) with Metal GPU support.
set -euo pipefail

DEST_DIR="${DEST_DIR:?DEST_DIR must be set}"
BUILD_DIR="${BUILD_DIR:-/tmp/llama-build}"
LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-b4722}"

mkdir -p "$DEST_DIR" "$BUILD_DIR"

echo "==> Cloning llama.cpp at tag $LLAMA_CPP_TAG"
if [ ! -d "$BUILD_DIR/llama.cpp" ]; then
    git clone --depth 1 --branch "$LLAMA_CPP_TAG" \
        https://github.com/ggerganov/llama.cpp.git "$BUILD_DIR/llama.cpp"
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

# Copy Metal shader library if present
if [ -f build/bin/default.metallib ]; then
    cp build/bin/default.metallib "$DEST_DIR/default.metallib"
fi

echo "==> llama-server built: $(du -sh "$DEST_DIR/llama-server" | cut -f1)"
