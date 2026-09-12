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

# ── Refuse to sign from a stale checkout ──
# The launch probe below only exists in the tree it is run from. A notarized
# DMG was shipped dead-on-launch *twice*: the second time because the fix had
# merged on GitHub but the local tree had not pulled it, so an older notarize.sh
# (no probe) signed with older entitlements. Print what is being signed, and
# refuse if origin/main has commits this tree lacks. NOTARIZE_ALLOW_STALE=1 opts
# out for deliberate hotfix builds from a branch.
sign_commit=$(git -C "$MACOS_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
echo "==> Signing from commit ${sign_commit} ($(git -C "$MACOS_DIR" branch --show-current 2>/dev/null || echo detached))"
if [ "${NOTARIZE_ALLOW_STALE:-0}" != "1" ]; then
    git -C "$MACOS_DIR" fetch -q origin main 2>/dev/null || true
    behind=$(git -C "$MACOS_DIR" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
    if [ "$behind" -gt 0 ]; then
        echo "ERROR: this checkout is ${behind} commit(s) behind origin/main." >&2
        echo "       The build scripts, entitlements and probe used for signing come from" >&2
        echo "       the working tree, not from GitHub. Pull first, or set NOTARIZE_ALLOW_STALE=1" >&2
        echo "       to sign a deliberately older tree." >&2
        exit 1
    fi
fi

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

# ── Entitlements ──
# Xcode expands `$(AppIdentifierPrefix)` at build time; `codesign` does not, so
# passing the source .entitlements straight to `--sign` embeds the variable
# *literally*. That once shipped a keychain access group literally named
# "$(AppIdentifierPrefix)com.harborclerk.shared". No entitlement uses the
# variable today, so the substitution is a no-op — kept, with the unresolved-
# variable check below, so the next one cannot ship the same way.
#
# TEAM_ID is already required above, and AppIdentifierPrefix is exactly the team
# ID plus a trailing dot, so resolve it here.
resolve_entitlements() {
    local src="$1"
    local out="$2"
    sed "s/\$(AppIdentifierPrefix)/${TEAM_ID}./g" "$src" > "$out"

    # Any remaining $(...) would ship literally the same way. Fail loudly rather
    # than embed a second one silently — this bug was invisible precisely
    # because a bad entitlement signs and verifies just fine.
    if grep -q '\$(' "$out"; then
        echo "ERROR: unresolved build variable in $src after substitution:" >&2
        grep -n '\$(' "$out" | sed 's/^/  /' >&2
        exit 1
    fi
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

RESOLVED_DIR=$(mktemp -d)
trap 'rm -rf "$RESOLVED_DIR"' EXIT
resolve_entitlements "$SERVER_ENTITLEMENTS" "$RESOLVED_DIR/server.entitlements"
resolve_entitlements "$CLIENT_ENTITLEMENTS" "$RESOLVED_DIR/client.entitlements"

codesign_app "$SERVER_APP" "$RESOLVED_DIR/server.entitlements"
codesign_app "$CLIENT_APP" "$RESOLVED_DIR/client.entitlements"

# ── Launch probe ──
# Run each signed app for two seconds before anything is submitted. A restricted
# entitlement without a provisioning profile makes the kernel SIGKILL the app at
# exec, and every check before this point — codesign --verify, spctl, the notary
# service — passes it, because none of them execute the binary. That shipped once
# as an Accepted, stapled DMG whose app died on launch (exit 137, no stderr).
#
# XCTestConfigurationFilePath is the "start nothing" switch both apps honour
# (server: AppDelegate; client: AuthManager). The server touches nothing. The
# client shows its waiting view for a moment and its BackendDetector still polls
# /api/system/health on localhost; it does not read or write the Keychain.
launch_probe() {
    local app exe pid rc probe_dir probe_app
    # Absolutise first. The Makefile hands this script BUILD_DIR=build, so every
    # path here is relative — and `defaults read` treats a relative path as a
    # *domain name* ("Domain 'build/output/.../Info.plist' not found"). Every
    # test of this function had used absolute paths, which is why that never
    # showed until the real `make sign`. PlistBuddy reads a path as a path.
    app="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    exe=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$app/Contents/Info.plist")
    # Probe a copy, never the artifact itself. Launching a notarized bundle in
    # place puts it under App Management protection, after which nothing — not
    # even the Terminal that signed it — may write inside it again; a second
    # `make sign` on the same output then dies re-signing tika-server.jar with
    # "Operation not permitted". ditto preserves the signature, so the copy is
    # what would ship, minus the side effect. ~3.7 GB, so this takes a moment.
    probe_dir=$(mktemp -d)
    probe_app="$probe_dir/$(basename "$app")"
    ditto "$app" "$probe_app"
    XCTestConfigurationFilePath=/dev/null "$probe_app/Contents/MacOS/$exe" >/dev/null 2>&1 &
    pid=$!
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true
        rm -rf "$probe_dir"
        echo "==> Launch probe OK: $(basename "$app")"
    else
        rc=0; wait "$pid" || rc=$?
        rm -rf "$probe_dir"
        echo "ERROR: $(basename "$app") exited within 2s of launch (rc=$rc) after signing." >&2
        [ "$rc" = 137 ] && echo "       rc=137 is a kernel SIGKILL at exec: almost always a restricted entitlement" >&2 \
                        && echo "       (e.g. keychain-access-groups) with no embedded provisioning profile." >&2
        echo "       Not submitting for notarization." >&2
        exit 1
    fi
}
launch_probe "$SERVER_APP"
launch_probe "$CLIENT_APP"

# ── Create DMG ──
DMG_PATH="$OUTPUT_DIR/HarborClerk.dmg"
echo "==> Creating DMG"

STAGING="$BUILD_DIR/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$SERVER_APP" "$STAGING/"
cp -R "$CLIENT_APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# ULMO (lzma), not UDZO (zlib). The bundle grew from 1.2 GB to 2.5 GB when the
# granite embedding and bge-reranker weights were bundled, and GitHub refuses
# release assets over 2 GiB. Measured on the v0.9.2 candidate: UDZO 2.54 GiB,
# ULMO 1.94 GiB. Converting after the fact is not an option — the DMG is
# codesigned and stapled below, so the compressed image has to exist before
# notarization, i.e. here.
hdiutil create -volname "Harbor Clerk" \
    -srcfolder "$STAGING" \
    -ov -format ULMO \
    "$DMG_PATH"

# GitHub refuses release assets of 2 GiB or more, and it does so at upload —
# after the ten-minute notarization below has already been paid for. Check here.
GITHUB_ASSET_LIMIT=$((2 * 1024 * 1024 * 1024))
DMG_BYTES=$(stat -f %z "$DMG_PATH")
if [ "$DMG_BYTES" -ge "$GITHUB_ASSET_LIMIT" ]; then
    echo "ERROR: $DMG_PATH is $DMG_BYTES bytes ($(( DMG_BYTES / 1048576 )) MiB); GitHub's release asset limit is 2 GiB." >&2
    echo "       Find what grew: du -sk '$SERVER_APP/Contents/Resources/'* | sort -rn | head" >&2
    exit 1
fi

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
