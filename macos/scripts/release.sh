#!/usr/bin/env bash
# Create a GitHub release with zipped macOS app bundles.
#
# Usage:
#   bash scripts/release.sh v1.0.0              # tag, build, release
#   bash scripts/release.sh v1.0.0 --skip-build # tag + release existing build
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$MACOS_DIR")"
BUILD_OUTPUT="$MACOS_DIR/build/output"

# ── Parse args ──

VERSION="${1:-}"
SKIP_BUILD=false

if [ -z "$VERSION" ]; then
    echo "Usage: $0 <version> [--skip-build]"
    echo "  e.g. $0 v1.0.0"
    exit 1
fi

shift
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Validate version format
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Version must match vX.Y.Z (e.g. v1.0.0)"
    exit 1
fi

cd "$PROJECT_ROOT"

# ── Check prerequisites ──

if ! command -v gh &>/dev/null; then
    echo "Error: gh (GitHub CLI) is required. Install with: brew install gh"
    exit 1
fi

if git rev-parse "$VERSION" &>/dev/null; then
    echo "Error: Tag $VERSION already exists"
    exit 1
fi

# ── Build ──

if [ "$SKIP_BUILD" = false ]; then
    echo "==> Building apps..."
    cd "$MACOS_DIR"
    make clean && make all
    cd "$PROJECT_ROOT"
fi

if [ ! -d "$BUILD_OUTPUT/HarborClerkServer.app" ] || [ ! -d "$BUILD_OUTPUT/HarborClerk.app" ]; then
    echo "Error: Built apps not found in $BUILD_OUTPUT"
    exit 1
fi

# ── Tag ──

echo "==> Creating tag $VERSION"
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"

# ── Package ──

STAGING=$(mktemp -d)
echo "==> Packaging release artifacts..."

# Zip each app separately so users can download what they need
ditto -c -k --sequesterRsrc --keepParent \
    "$BUILD_OUTPUT/HarborClerkServer.app" \
    "$STAGING/HarborClerkServer-${VERSION}.zip"

ditto -c -k --sequesterRsrc --keepParent \
    "$BUILD_OUTPUT/HarborClerk.app" \
    "$STAGING/HarborClerk-${VERSION}.zip"

SERVER_SIZE=$(du -h "$STAGING/HarborClerkServer-${VERSION}.zip" | cut -f1)
CLIENT_SIZE=$(du -h "$STAGING/HarborClerk-${VERSION}.zip" | cut -f1)

echo "  Server: $SERVER_SIZE"
echo "  Client: $CLIENT_SIZE"

# ── Create release ──

echo "==> Creating GitHub release $VERSION"
gh release create "$VERSION" \
    "$STAGING/HarborClerkServer-${VERSION}.zip" \
    "$STAGING/HarborClerk-${VERSION}.zip" \
    --title "Harbor Clerk $VERSION" \
    --notes "$(cat <<EOF
## Downloads

| File | Description | Size |
|------|-------------|------|
| \`HarborClerkServer-${VERSION}.zip\` | Menubar server app (includes all services) | $SERVER_SIZE |
| \`HarborClerk-${VERSION}.zip\` | Client app (web UI wrapper) | $CLIENT_SIZE |

### Installation

1. Download both zip files
2. Unzip and move both apps to \`/Applications\`
3. Launch **Harbor Clerk Server** — services start automatically
4. Launch **Harbor Clerk** to open the web UI
5. Create your admin account on the setup page

### Requirements

- Apple Silicon Mac (M1 or later)
- macOS 15.0 (Sequoia) or later
- 16 GB RAM minimum
EOF
)"

# ── Cleanup ──

rm -rf "$STAGING"

echo "==> Release $VERSION published!"
echo "    https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/$VERSION"
