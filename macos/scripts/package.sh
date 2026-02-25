#!/usr/bin/env bash
# Assemble app bundles by copying built resources into the Xcode output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$MACOS_DIR")"
BUILD_DIR="${BUILD_DIR:-$MACOS_DIR/build}"
mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"

echo "==> Packaging Harbor Clerk apps"

FRONTEND_DIST="$PROJECT_ROOT/frontend/dist"

# ── Build Xcode projects ──
echo "==> Building Harbor Clerk Server.app"
cd "$MACOS_DIR/HarborClerkServer"
xcodebuild -project HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -configuration Release \
    -derivedDataPath "$BUILD_DIR/derived" \
    -arch arm64 \
    ONLY_ACTIVE_ARCH=NO \
    build

echo "==> Building Harbor Clerk.app"
cd "$MACOS_DIR/HarborClerk"
xcodebuild -project HarborClerk.xcodeproj \
    -scheme HarborClerk \
    -configuration Release \
    -derivedDataPath "$BUILD_DIR/derived" \
    -arch arm64 \
    ONLY_ACTIVE_ARCH=NO \
    build

# ── Locate built apps ──
SERVER_APP=$(find "$BUILD_DIR/derived" -name "HarborClerkServer.app" -type d | head -1)
CLIENT_APP=$(find "$BUILD_DIR/derived" -name "HarborClerk.app" -type d | head -1)

if [ -z "$SERVER_APP" ] || [ -z "$CLIENT_APP" ]; then
    echo "ERROR: Could not find built apps"
    exit 1
fi

# ── Copy resources into server app bundle ──
RESOURCES="$SERVER_APP/Contents/Resources"
# Clean previous resources to avoid permission errors and nested dirs
rm -rf "$RESOURCES"
mkdir -p "$RESOURCES"
echo "==> Copying resources to server app bundle"

# PostgreSQL
if [ -d "$BUILD_DIR/postgres" ]; then
    cp -R "$BUILD_DIR/postgres" "$RESOURCES/postgres"
fi

# Java JRE (for Tika)
if [ -d "$BUILD_DIR/java" ]; then
    cp -R "$BUILD_DIR/java" "$RESOURCES/java"
fi

# Tika
if [ -d "$BUILD_DIR/tika" ]; then
    cp -R "$BUILD_DIR/tika" "$RESOURCES/tika"
fi

# Python + venv
if [ -d "$BUILD_DIR/python" ]; then
    cp -R "$BUILD_DIR/python" "$RESOURCES/python"
fi
if [ -d "$BUILD_DIR/venv" ]; then
    cp -R "$BUILD_DIR/venv" "$RESOURCES/venv"
fi

# Tesseract
if [ -d "$BUILD_DIR/tesseract" ]; then
    cp -R "$BUILD_DIR/tesseract" "$RESOURCES/tesseract"
fi

# Model
if [ -d "$BUILD_DIR/model" ]; then
    cp -R "$BUILD_DIR/model" "$RESOURCES/model"
fi

# llama-server
if [ -d "$BUILD_DIR/llama" ]; then
    cp -R "$BUILD_DIR/llama" "$RESOURCES/llama"
fi

# Alembic
cp -R "$PROJECT_ROOT/alembic" "$RESOURCES/alembic"
cp "$PROJECT_ROOT/alembic.ini" "$RESOURCES/alembic.ini"

# Frontend
cp -R "$FRONTEND_DIST" "$RESOURCES/frontend-dist"

# Menubar icon
cp "$PROJECT_ROOT/art/logo-favicon.png" "$RESOURCES/menubar_icon.png"

# ── Copy to output ──
OUTPUT_DIR="$BUILD_DIR/output"
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"
cp -R "$SERVER_APP" "$OUTPUT_DIR/"
cp -R "$CLIENT_APP" "$OUTPUT_DIR/"

echo "==> Apps assembled in ${OUTPUT_DIR}"
echo "==> Server app: $(du -sh "$OUTPUT_DIR/HarborClerkServer.app" | cut -f1)"
echo "==> Client app: $(du -sh "$OUTPUT_DIR/HarborClerk.app" | cut -f1)"
