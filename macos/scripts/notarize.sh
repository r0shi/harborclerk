#!/usr/bin/env bash
# Codesign and notarize the app bundles.
# Requires: Developer ID certificate, APPLE_ID, TEAM_ID, APP_PASSWORD env vars.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${BUILD_DIR:-$MACOS_DIR/build}"
OUTPUT_DIR="$BUILD_DIR/output"

IDENTITY="${SIGNING_IDENTITY:-Developer ID Application}"
APPLE_ID="${APPLE_ID:?Set APPLE_ID env var}"
TEAM_ID="${TEAM_ID:?Set TEAM_ID env var}"
APP_PASSWORD="${APP_PASSWORD:?Set APP_PASSWORD env var (app-specific password)}"

echo "==> Signing and notarizing apps"

# ── Codesign helper ──
codesign_app() {
    local app_path="$1"
    local entitlements="$2"
    local app_name=$(basename "$app_path")

    echo "==> Codesigning ${app_name}"

    # Sign all nested binaries first
    find "$app_path" -type f \( -name '*.dylib' -o -name '*.so' -o -perm +111 \) | while read -r binary; do
        # Skip non-Mach-O files
        file "$binary" | grep -q "Mach-O" || continue
        codesign --force --options runtime --sign "$IDENTITY" \
            --entitlements "$entitlements" \
            --timestamp "$binary" 2>/dev/null || true
    done

    # Sign the app bundle itself
    codesign --force --deep --options runtime --sign "$IDENTITY" \
        --entitlements "$entitlements" \
        --timestamp "$app_path"

    echo "==> Verifying ${app_name}"
    codesign --verify --deep --strict "$app_path"
}

# ── Sign apps ──
SERVER_APP="$OUTPUT_DIR/HarborClerkServer.app"
CLIENT_APP="$OUTPUT_DIR/HarborClerk.app"

SERVER_ENTITLEMENTS="$MACOS_DIR/HarborClerkServer/HarborClerkServer/HarborClerkServer.entitlements"
CLIENT_ENTITLEMENTS="$MACOS_DIR/HarborClerk/HarborClerk/HarborClerk.entitlements"

codesign_app "$SERVER_APP" "$SERVER_ENTITLEMENTS"
codesign_app "$CLIENT_APP" "$CLIENT_ENTITLEMENTS"

# ── Create DMG ──
DMG_PATH="$OUTPUT_DIR/HarborClerk.dmg"
echo "==> Creating DMG"

STAGING="$BUILD_DIR/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$SERVER_APP" "$STAGING/"
cp -R "$CLIENT_APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "Harbor Clerk" \
    -srcfolder "$STAGING" \
    -ov -format UDZO \
    "$DMG_PATH"

codesign --force --sign "$IDENTITY" --timestamp "$DMG_PATH"

# ── Notarize ──
echo "==> Submitting for notarization"
xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --team-id "$TEAM_ID" \
    --password "$APP_PASSWORD" \
    --wait

echo "==> Stapling"
xcrun stapler staple "$DMG_PATH"

echo "==> Done: ${DMG_PATH}"
echo "==> Size: $(du -sh "$DMG_PATH" | cut -f1)"
