#!/usr/bin/env bash
# Extract Tesseract + Poppler from Homebrew bottles and fix rpaths.
# These are used for OCR (pdftoppm + tesseract).
set -euo pipefail

DEST_DIR="${DEST_DIR:-$(pwd)/build}"
TESSERACT_DIR="$DEST_DIR/tesseract"
POPPLER_DIR="$DEST_DIR/poppler"

echo "==> Extracting Tesseract and Poppler from Homebrew"

# Skip if already extracted
if [ -f "$TESSERACT_DIR/bin/tesseract" ] && [ -f "$POPPLER_DIR/bin/pdftoppm" ]; then
    echo "==> Already extracted, skipping"
    exit 0
fi

# Ensure Homebrew deps are installed
brew list tesseract &>/dev/null || brew install tesseract
brew list poppler &>/dev/null || brew install poppler
brew list tesseract-lang &>/dev/null || true  # eng+fra included in base

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

# ── Poppler (pdftoppm only) ──
mkdir -p "$POPPLER_DIR/bin" "$POPPLER_DIR/lib"

POPPLER_PREFIX="$(brew --prefix poppler)"
cp "$POPPLER_PREFIX/bin/pdftoppm" "$POPPLER_DIR/bin/"

for dylib in $(otool -L "$POPPLER_DIR/bin/pdftoppm" | grep '/opt/homebrew\|/usr/local' | awk '{print $1}'); do
    cp "$dylib" "$POPPLER_DIR/lib/" 2>/dev/null || true
    libname=$(basename "$dylib")
    install_name_tool -change "$dylib" "@executable_path/../lib/$libname" "$POPPLER_DIR/bin/pdftoppm" 2>/dev/null || true
done

# Recursively fix dylib dependencies (2 levels deep should suffice)
for pass in 1 2; do
    for lib in "$POPPLER_DIR/lib/"*.dylib; do
        for dep in $(otool -L "$lib" | grep '/opt/homebrew\|/usr/local' | awk '{print $1}'); do
            depname=$(basename "$dep")
            if [ ! -f "$POPPLER_DIR/lib/$depname" ]; then
                cp "$dep" "$POPPLER_DIR/lib/" 2>/dev/null || true
            fi
            install_name_tool -change "$dep" "@loader_path/$depname" "$lib" 2>/dev/null || true
        done
    done
done

echo "==> Tesseract installed to ${TESSERACT_DIR}"
echo "==> Poppler installed to ${POPPLER_DIR}"
