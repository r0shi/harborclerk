#!/usr/bin/env bash
# Build Redis from source — produces a single static-ish binary (~3.5MB).
set -euo pipefail

REDIS_VERSION="${REDIS_VERSION:-7.4.1}"
DEST_DIR="${DEST_DIR:-$(pwd)/build/redis}"
BUILD_DIR="${BUILD_DIR:-$(pwd)/build/redis-build}"
ARCH="${ARCH:-arm64}"

echo "==> Building Redis ${REDIS_VERSION} for ${ARCH}"

mkdir -p "$BUILD_DIR" "$DEST_DIR/bin"

cd "$BUILD_DIR"
if [ ! -d "redis-${REDIS_VERSION}" ]; then
    echo "==> Downloading Redis ${REDIS_VERSION}"
    curl -fsSL "https://download.redis.io/releases/redis-${REDIS_VERSION}.tar.gz" | tar xz
fi

cd "redis-${REDIS_VERSION}"
make -j"$(sysctl -n hw.ncpu)" \
    CFLAGS="-arch ${ARCH} -O2" \
    LDFLAGS="-arch ${ARCH}" \
    USE_SYSTEMD=no \
    BUILD_TLS=no

cp src/redis-server "$DEST_DIR/bin/"
cp src/redis-cli "$DEST_DIR/bin/"
strip -x "$DEST_DIR/bin/redis-server"
strip -x "$DEST_DIR/bin/redis-cli"

echo "==> Redis installed to ${DEST_DIR}"
echo "==> Size: $(du -sh "$DEST_DIR/bin" | cut -f1)"
