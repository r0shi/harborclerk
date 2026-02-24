#!/usr/bin/env bash
# Extract Tesseract from Homebrew bottles and fix rpaths.
# Used for OCR (tesseract).
set -euo pipefail

DEST_DIR="${DEST_DIR:-$(pwd)/build}"
TESSERACT_DIR="$DEST_DIR/tesseract"

echo "==> Extracting Tesseract from Homebrew"

# Skip if already extracted
if [ -f "$TESSERACT_DIR/bin/tesseract" ]; then
    echo "==> Already extracted, skipping"
    exit 0
fi

# Ensure Homebrew deps are installed
brew list tesseract &>/dev/null || brew install tesseract

# ── Tesseract ──
mkdir -p "$TESSERACT_DIR/bin" "$TESSERACT_DIR/share/tessdata"

TESS_PREFIX="$(brew --prefix tesseract)"
cp "$TESS_PREFIX/bin/tesseract" "$TESSERACT_DIR/bin/"

# Copy tessdata (eng + fra only to save space)
TESSDATA="$(brew --prefix tesseract)/share/tessdata"
for lang in eng fra osd; do
    if [ -f "$TESSDATA/${lang}.traineddata" ]; then
        cp "$TESSDATA/${lang}.traineddata" "$TESSERACT_DIR/share/tessdata/"
    fi
done

# Copy required dylibs and fix rpaths
mkdir -p "$TESSERACT_DIR/lib"
for dylib in $(otool -L "$TESSERACT_DIR/bin/tesseract" | grep '/opt/homebrew\|/usr/local' | awk '{print $1}'); do
    cp "$dylib" "$TESSERACT_DIR/lib/" 2>/dev/null || true
    libname=$(basename "$dylib")
    install_name_tool -change "$dylib" "@executable_path/../lib/$libname" "$TESSERACT_DIR/bin/tesseract" 2>/dev/null || true
done

# Recursively fix dylib dependencies
for lib in "$TESSERACT_DIR/lib/"*.dylib; do
    for dep in $(otool -L "$lib" | grep '/opt/homebrew\|/usr/local' | awk '{print $1}'); do
        depname=$(basename "$dep")
        if [ ! -f "$TESSERACT_DIR/lib/$depname" ]; then
            cp "$dep" "$TESSERACT_DIR/lib/" 2>/dev/null || true
        fi
        install_name_tool -change "$dep" "@loader_path/$depname" "$lib" 2>/dev/null || true
    done
done

echo "==> Tesseract installed to ${TESSERACT_DIR}"
