#!/usr/bin/env bash
# Build PostgreSQL 16 + pgvector from source for arm64 macOS.
# Output: $DEST_DIR/{bin,lib/postgresql,share}
set -euo pipefail

PG_VERSION="${PG_VERSION:-16.4}"
PGVECTOR_VERSION="${PGVECTOR_VERSION:-0.8.0}"
ARCH="${ARCH:-arm64}"

# Resolve to absolute paths before cd-ing around
DEST_DIR="${DEST_DIR:-$(pwd)/build/postgres}"
BUILD_DIR="${BUILD_DIR:-$(pwd)/build/pg-build}"
mkdir -p "$BUILD_DIR" "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"

echo "==> Building PostgreSQL ${PG_VERSION} + pgvector ${PGVECTOR_VERSION} for ${ARCH}"

# ── PostgreSQL ──
cd "$BUILD_DIR"
if [ ! -d "postgresql-${PG_VERSION}" ]; then
    echo "==> Downloading PostgreSQL ${PG_VERSION}"
    curl -fsSL "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2" | tar xj
fi

cd "postgresql-${PG_VERSION}"
./configure \
    --prefix="$DEST_DIR" \
    --without-readline \
    --without-zlib \
    --without-icu \
    --with-uuid=e2fs \
    CFLAGS="-arch ${ARCH} -O2"

make -j"$(sysctl -n hw.ncpu)" world-bin
make install-world-bin

# ── pgvector ──
cd "$BUILD_DIR"
if [ ! -d "pgvector-${PGVECTOR_VERSION}" ]; then
    echo "==> Downloading pgvector ${PGVECTOR_VERSION}"
    curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" | tar xz
fi

cd "pgvector-${PGVECTOR_VERSION}"
export PG_CONFIG="$DEST_DIR/bin/pg_config"
make -j"$(sysctl -n hw.ncpu)"
make install

# ── Cleanup ──
# Strip binaries to reduce size
find "$DEST_DIR/bin" -type f -perm +111 -exec strip -x {} \; 2>/dev/null || true
find "$DEST_DIR/lib" -name '*.dylib' -exec strip -x {} \; 2>/dev/null || true
find "$DEST_DIR/lib" -name '*.so' -exec strip -x {} \; 2>/dev/null || true

# Remove unnecessary files
rm -rf "$DEST_DIR/include" "$DEST_DIR/share/doc" "$DEST_DIR/share/man"

echo "==> PostgreSQL installed to ${DEST_DIR}"
echo "==> Binaries: $(du -sh "$DEST_DIR/bin" | cut -f1)"
echo "==> Libraries: $(du -sh "$DEST_DIR/lib" | cut -f1)"
echo "==> Share: $(du -sh "$DEST_DIR/share" | cut -f1)"
