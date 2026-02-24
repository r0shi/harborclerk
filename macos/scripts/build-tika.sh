#!/usr/bin/env bash
# Download Eclipse Temurin JRE 21 + Apache Tika Server for macOS aarch64.
# Output: DEST_DIR/java/ (~100MB) + DEST_DIR/tika/tika-server.jar (~90MB)
set -euo pipefail

DEST_DIR="${DEST_DIR:-$(pwd)/build}"
JAVA_DIR="$DEST_DIR/java"
TIKA_DIR="$DEST_DIR/tika"

TEMURIN_VERSION="21.0.5+11"
TEMURIN_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-${TEMURIN_VERSION}/OpenJDK21U-jre_aarch64_mac_hotspot_${TEMURIN_VERSION//+/_}.tar.gz"

TIKA_VERSION="3.0.0"
TIKA_URL="https://archive.apache.org/dist/tika/${TIKA_VERSION}/tika-server-standard-${TIKA_VERSION}.jar"

# Skip if already built
if [ -d "$JAVA_DIR/bin" ] && [ -f "$TIKA_DIR/tika-server.jar" ]; then
    echo "==> JRE + Tika already present, skipping"
    exit 0
fi

mkdir -p "$JAVA_DIR" "$TIKA_DIR"

# ── Eclipse Temurin JRE 21 (GPLv2+CE — Classpath Exception) ──
if [ ! -d "$JAVA_DIR/bin" ]; then
    echo "==> Downloading Eclipse Temurin JRE 21 for aarch64..."
    TMPTAR="$(mktemp)"
    curl -fSL -o "$TMPTAR" "$TEMURIN_URL"

    echo "==> Extracting JRE..."
    # Extract, stripping the top-level directory
    tar xzf "$TMPTAR" -C "$JAVA_DIR" --strip-components=1
    rm "$TMPTAR"

    # Strip to runtime-only (remove non-essential files to save ~50MB)
    rm -rf "$JAVA_DIR/man"
    rm -rf "$JAVA_DIR/demo"
    rm -f  "$JAVA_DIR/lib/src.zip"
    rm -rf "$JAVA_DIR/jmods"

    echo "==> JRE installed to $JAVA_DIR ($(du -sh "$JAVA_DIR" | cut -f1))"
fi

# ── Apache Tika Server (Apache 2.0) ──
if [ ! -f "$TIKA_DIR/tika-server.jar" ]; then
    echo "==> Downloading Apache Tika Server ${TIKA_VERSION}..."
    curl -fSL -o "$TIKA_DIR/tika-server.jar" "$TIKA_URL"
    echo "==> Tika JAR downloaded ($(du -sh "$TIKA_DIR/tika-server.jar" | cut -f1))"
fi

echo "==> Tika build complete"
