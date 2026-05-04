#!/usr/bin/env bash
# Download Eclipse Temurin JRE 21 + Apache Tika Server for macOS aarch64.
# Output: DEST_DIR/java/ (~100MB) + DEST_DIR/tika/tika-server.jar (~90MB)
set -euo pipefail

DEST_DIR="${DEST_DIR:-$(pwd)/build}"
JAVA_DIR="$DEST_DIR/java"
TIKA_DIR="$DEST_DIR/tika"

TEMURIN_VERSION="21.0.11+10"
TEMURIN_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-${TEMURIN_VERSION}/OpenJDK21U-jre_aarch64_mac_hotspot_${TEMURIN_VERSION//+/_}.tar.gz"

TIKA_VERSION="3.3.0"
TIKA_URL="https://archive.apache.org/dist/tika/${TIKA_VERSION}/tika-server-standard-${TIKA_VERSION}.jar"
TIKA_SHA512_URL="${TIKA_URL}.sha512"

# Apache mirrors and the GitHub releases CDN occasionally close connections
# mid-flight; `curl -fSL` returns success on a partial download, leaving a
# corrupt artifact that breaks subsequent builds. Use --retry-all-errors so
# transient drops are retried, and verify checksums where the upstream
# publishes them.
CURL_OPTS=(--fail --location --silent --show-error
           --retry 5 --retry-delay 5 --retry-all-errors --retry-max-time 600)

# JRE is structured differently on macOS (Contents/Home/...) vs Linux (bin/).
java_present() {
    [ -x "$JAVA_DIR/bin/java" ] || [ -x "$JAVA_DIR/Contents/Home/bin/java" ]
}

# Skip if already built
if java_present && [ -f "$TIKA_DIR/tika-server.jar" ]; then
    echo "==> JRE + Tika already present, skipping"
    exit 0
fi

mkdir -p "$JAVA_DIR" "$TIKA_DIR"

# ── Eclipse Temurin JRE 21 (GPLv2+CE — Classpath Exception) ──
if ! java_present; then
    echo "==> Downloading Eclipse Temurin JRE 21 for aarch64..."
    TMPTAR="$(mktemp)"
    curl "${CURL_OPTS[@]}" -o "$TMPTAR" "$TEMURIN_URL"

    echo "==> Extracting JRE..."
    # Extract, stripping the top-level directory
    tar xzf "$TMPTAR" -C "$JAVA_DIR" --strip-components=1
    rm "$TMPTAR"

    # Strip to runtime-only (remove non-essential files to save ~50MB)
    JAVA_HOME_DIR="$JAVA_DIR"
    [ -d "$JAVA_DIR/Contents/Home" ] && JAVA_HOME_DIR="$JAVA_DIR/Contents/Home"
    rm -rf "$JAVA_HOME_DIR/man"
    rm -rf "$JAVA_HOME_DIR/demo"
    rm -f  "$JAVA_HOME_DIR/lib/src.zip"
    rm -rf "$JAVA_HOME_DIR/jmods"

    echo "==> JRE installed to $JAVA_DIR ($(du -sh "$JAVA_DIR" | cut -f1))"
fi

# ── Apache Tika Server (Apache 2.0) ──
if [ ! -f "$TIKA_DIR/tika-server.jar" ]; then
    echo "==> Downloading Apache Tika Server ${TIKA_VERSION}..."
    TMPJAR="$TIKA_DIR/tika-server.jar.partial"
    curl "${CURL_OPTS[@]}" -o "$TMPJAR" "$TIKA_URL"

    # Verify the SHA512 published by the Apache release. Apache's archive
    # mirror has been known to truncate large downloads silently — without
    # this check, a 75 MB partial JAR would be cached as the canonical
    # artifact and break Tika startup on every subsequent run.
    echo "==> Verifying SHA512..."
    EXPECTED_SHA512="$(curl "${CURL_OPTS[@]}" "$TIKA_SHA512_URL" | awk '{print $1}')"
    ACTUAL_SHA512="$(shasum -a 512 "$TMPJAR" | awk '{print $1}')"
    if [ "$EXPECTED_SHA512" != "$ACTUAL_SHA512" ]; then
        echo "ERROR: Tika JAR SHA512 mismatch"
        echo "  expected: $EXPECTED_SHA512"
        echo "  actual:   $ACTUAL_SHA512"
        rm -f "$TMPJAR"
        exit 1
    fi

    mv "$TMPJAR" "$TIKA_DIR/tika-server.jar"
    echo "==> Tika JAR verified ($(du -sh "$TIKA_DIR/tika-server.jar" | cut -f1))"
fi

echo "==> Tika build complete"
