#!/usr/bin/env bash
# Assemble app bundles by copying built resources into the Xcode output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$MACOS_DIR")"
BUILD_DIR="${BUILD_DIR:-$MACOS_DIR/build}"
mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"

# Avoid propagating Finder/provenance extended attributes into assembled bundles.
export COPYFILE_DISABLE=1

copy_bundle() {
    local src="$1"
    local dest="$2"
    remove_tree "$dest"
    ditto --norsrc --noextattr --noqtn --noacl "$src" "$dest"
}

remove_tree() {
    local path="$1"
    if [ -e "$path" ]; then
        xattr -cr "$path" 2>/dev/null || true
        chmod -R u+w "$path" 2>/dev/null || true
        rm -rf "$path"
    fi
}


echo "==> Packaging Harbor Clerk apps"

FRONTEND_DIST="$PROJECT_ROOT/frontend/dist"

# ── Version stamps ──
# The release tag is the only source of truth for a version. scripts/build-version.sh
# derives every stamp from `git describe` of the commit being built, and the same
# values go to both Xcode projects (below), into the venv beside build-hash.txt
# (for /api/system/health) and into the plist check at the end of this script.
# The pbxproj files carry a 0.0.0-dev sentinel so a plain Xcode build is never
# mistaken for a release; nothing edits them per release.
# Two steps, not `eval "$(...)"`: eval of an empty string succeeds, so a failing
# derivation would otherwise build with every stamp blank. The assignment fails
# the script under `set -e`.
version_stamps="$(bash "$SCRIPT_DIR/build-version.sh")"
eval "$version_stamps"
echo "==> Version ${HC_VERSION_FULL} (CFBundleShortVersionString ${HC_VERSION}, CFBundleVersion ${HC_BUILD_NUMBER}, commit ${HC_BUILD_HASH})"
if [ "$HC_RELEASE" != "1" ]; then
    echo "    Not a clean build of a tagged commit: this is a development build and is labelled as one."
fi

# ── Build Xcode projects ──
# Built without signing. `make sign` (scripts/notarize.sh) re-signs everything
# with `codesign --force --deep --options runtime` and Developer ID, and passes
# entitlements explicitly, so any signature applied here is discarded anyway.
#
# Requiring one regardless cost real time: Xcode's automatic signing wanted a
# Mac App Development profile, which needs a Development certificate for the
# project's team *and* this Mac registered as a device in the developer account
# — neither of which the release path uses. It also made the build depend on
# Xcode holding a live account session, which it loses readily.
#
# So the only credential the release needs is the Developer ID key, used in one
# place: notarize.sh.
echo "==> Building Harbor Clerk Server.app"
cd "$MACOS_DIR/HarborClerkServer"
xcodebuild -project HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -configuration Release \
    -derivedDataPath "$BUILD_DIR/derived" \
    -arch arm64 \
    ONLY_ACTIVE_ARCH=NO \
    CODE_SIGNING_ALLOWED=NO \
    MARKETING_VERSION="$HC_VERSION" \
    CURRENT_PROJECT_VERSION="$HC_BUILD_NUMBER" \
    build

echo "==> Building Harbor Clerk.app"
cd "$MACOS_DIR/HarborClerk"
xcodebuild -project HarborClerk.xcodeproj \
    -scheme HarborClerk \
    -configuration Release \
    -derivedDataPath "$BUILD_DIR/derived" \
    -arch arm64 \
    ONLY_ACTIVE_ARCH=NO \
    CODE_SIGNING_ALLOWED=NO \
    MARKETING_VERSION="$HC_VERSION" \
    CURRENT_PROJECT_VERSION="$HC_BUILD_NUMBER" \
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
remove_tree "$RESOURCES"
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
    # Stamped here, at the moment the bundle is assembled from this checkout, so
    # the venv and the Info.plist above describe the same commit by construction.
    # ServiceManager.swift exports both to the API as BUILD_HASH / BUILD_VERSION;
    # /api/system/health reports them as "build" / "version".
    printf '%s\n' "$HC_BUILD_HASH" > "$BUILD_DIR/venv/build-hash.txt"
    printf '%s\n' "$HC_VERSION_FULL" > "$BUILD_DIR/venv/build-version.txt"
    cp -R "$BUILD_DIR/venv" "$RESOURCES/venv"
fi

# Tesseract
if [ -d "$BUILD_DIR/tesseract" ]; then
    cp -R "$BUILD_DIR/tesseract" "$RESOURCES/tesseract"
fi

# Models — only the two the Swift services load (EmbedderService.swift and
# RerankerService.swift), by name. A wholesale copy of build/model shipped a
# stale multilingual-e5-small directory left over from an April build: 466 MB
# of fp32 weights nothing referenced, which compress poorly and pushed the
# v0.9.2 DMG to 2.23 GiB — over GitHub's 2 GiB asset limit — after a
# notarization that had already succeeded.
mkdir -p "$RESOURCES/model"
for m in granite-embedding-311m-multilingual-r2 bge-reranker-v2-m3; do
    if [ ! -d "$BUILD_DIR/model/$m" ]; then
        echo "ERROR: $BUILD_DIR/model/$m is missing — run 'make model' first" >&2
        exit 1
    fi
    cp -R "$BUILD_DIR/model/$m" "$RESOURCES/model/$m"
done

# llama-server
if [ -d "$BUILD_DIR/llama" ]; then
    cp -R "$BUILD_DIR/llama" "$RESOURCES/llama"
fi

# Caddy HTTPS gateway
if [ -d "$BUILD_DIR/caddy" ]; then
    cp -R "$BUILD_DIR/caddy" "$RESOURCES/caddy"
fi

# apple-summarize (Apple Intelligence CLI tool — optional, macOS 26+ only)
if [ -f "$BUILD_DIR/apple-summarize/apple-summarize" ]; then
    cp "$BUILD_DIR/apple-summarize/apple-summarize" "$RESOURCES/apple-summarize"
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
remove_tree "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"
copy_bundle "$SERVER_APP" "$OUTPUT_DIR/HarborClerkServer.app"
copy_bundle "$CLIENT_APP" "$OUTPUT_DIR/HarborClerk.app"

# No re-signing here. The build is unsigned and `make sign` (notarize.sh) is the
# only signing step, so extracting entitlements back out of the built app —
# which this used to do — now yields a zero-byte file. codesign accepts that
# with a warning and produces an app stripped of disable-library-validation,
# allow-unsigned-executable-memory and both network entitlements, which the
# server needs to spawn llama and Python under the hardened runtime. Silently.

# ── The bundles must say what the tag says ──
PLISTBUDDY="${PLISTBUDDY:-/usr/libexec/PlistBuddy}"
# Command-line build settings override the pbxproj, but a renamed setting or a
# target that stopped reading $(MARKETING_VERSION) in its Info.plist would ship
# the 0.0.0-dev sentinel silently. Read the plists back.
for app in "$OUTPUT_DIR/HarborClerkServer.app" "$OUTPUT_DIR/HarborClerk.app"; do
    plist="$app/Contents/Info.plist"
    short="$("$PLISTBUDDY" -c "Print :CFBundleShortVersionString" "$plist")"
    bundle_version="$("$PLISTBUDDY" -c "Print :CFBundleVersion" "$plist")"
    if [ "$short" != "$HC_VERSION" ] || [ "$bundle_version" != "$HC_BUILD_NUMBER" ]; then
        echo "ERROR: $(basename "$app") reports version ${short} (${bundle_version}); expected ${HC_VERSION} (${HC_BUILD_NUMBER})." >&2
        echo "       MARKETING_VERSION / CURRENT_PROJECT_VERSION did not reach the target's Info.plist." >&2
        exit 1
    fi
done

echo "==> Apps assembled in ${OUTPUT_DIR}"
echo "==> Version: ${HC_VERSION} (${HC_BUILD_NUMBER}), ${HC_VERSION_FULL}"
echo "==> Server app: $(du -sh "$OUTPUT_DIR/HarborClerkServer.app" | cut -f1)"
echo "==> Client app: $(du -sh "$OUTPUT_DIR/HarborClerk.app" | cut -f1)"
