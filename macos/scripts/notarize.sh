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

# Apple's notarization unzips JARs and validates every Mach-O binary inside.
# `find` only walks the filesystem, so JAR-embedded dylibs must be extracted,
# signed, and repacked before the parent app bundle is signed.
sign_jar_dylibs() {
    local jar_path="$1"

    local dylibs
    dylibs=$(unzip -Z1 "$jar_path" 2>/dev/null | grep -E '\.dylib$' || true)
    [ -z "$dylibs" ] && return 0

    local jar_name count abs_jar staging
    jar_name=$(basename "$jar_path")
    count=$(printf '%s\n' "$dylibs" | wc -l | tr -d ' ')
    abs_jar=$(cd "$(dirname "$jar_path")" && pwd)/$(basename "$jar_path")
    staging=$(mktemp -d)

    echo "    Signing ${count} dylib(s) inside ${jar_name}"
    (
        cd "$staging"
        while IFS= read -r entry; do
            unzip -o -q "$abs_jar" "$entry"
            codesign --force --options runtime --sign "$IDENTITY" \
                --timestamp "$entry"
        done <<< "$dylibs"
        # Updates entries in place; preserves everything else.
        while IFS= read -r entry; do
            zip -q "$abs_jar" "$entry"
        done <<< "$dylibs"
    )
    rm -rf "$staging"
}

# ── Codesign helper ──
codesign_app() {
    local app_path="$1"
    local entitlements="$2"
    local app_name=$(basename "$app_path")

    echo "==> Codesigning ${app_name}"

    # Sign dylibs embedded in JARs (must happen before bundle signing — JAR
    # mutation invalidates the parent bundle's CodeResources hash).
    while IFS= read -r jar; do
        sign_jar_dylibs "$jar"
    done < <(find "$app_path" -type f -name '*.jar')

    # Sign all loose nested binaries
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
NOTARY_LOG=$(mktemp)
xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --team-id "$TEAM_ID" \
    --password "$APP_PASSWORD" \
    --wait | tee "$NOTARY_LOG"

# notarytool's --wait exits 0 even when Apple rejects the submission, so we
# must read the final status ourselves and surface the failure log.
NOTARY_STATUS=$(awk '/^[[:space:]]*status:/ { val=$2 } END { print val }' "$NOTARY_LOG")
SUBMISSION_ID=$(awk '/^[[:space:]]*id:/ { print $2; exit }' "$NOTARY_LOG")
rm -f "$NOTARY_LOG"

if [ "$NOTARY_STATUS" != "Accepted" ]; then
    echo "ERROR: Notarization status: ${NOTARY_STATUS:-unknown}"
    if [ -n "$SUBMISSION_ID" ]; then
        echo "==> Notary issue log for ${SUBMISSION_ID}:"
        xcrun notarytool log "$SUBMISSION_ID" \
            --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APP_PASSWORD"
    fi
    exit 1
fi

echo "==> Stapling"
xcrun stapler staple "$DMG_PATH"

echo "==> Done: ${DMG_PATH}"
echo "==> Size: $(du -sh "$DMG_PATH" | cut -f1)"
