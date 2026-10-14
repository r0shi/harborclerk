#!/usr/bin/env bash
# Tag a release and publish it on GitHub.
#
# The tag is the single source of truth for every version string: package.sh
# derives MARKETING_VERSION, CURRENT_PROJECT_VERSION and venv/build-version.txt
# from `git describe` of the commit it builds (scripts/build-version.sh). Two
# consequences, both enforced here:
#
#   * pyproject.toml and frontend/package.json (and its lock) must already carry
#     the number being released — bumped and landed via PR beforehand — or the
#     Python and npm packages would claim a different version than the app.
#     Every release up to v0.9.3 shipped that way: pyproject 0.8.0, app 1.0 (#624).
#   * the tag must exist before the apps are built, or the build describes the
#     previous tag plus a distance. So this script tags first and builds second,
#     and pushes the tag only once the build exists and its stamps match it.
#
# Usage:
#   bash scripts/release.sh vX.Y.Z --check        preflight only: sources agree, tree clean, tag free
#   bash scripts/release.sh vX.Y.Z                preflight, tag, build, verify stamps, push tag, publish
#   bash scripts/release.sh vX.Y.Z --skip-build   publish an existing build/output; the local tag must
#                                                 already exist at HEAD and the build must carry it
#
# Signing is a human step and never runs from here. The sequence that ships a
# notarized DMG is:
#
#   bash macos/scripts/release.sh vX.Y.Z --check
#   git tag -a vX.Y.Z -m "Release vX.Y.Z"
#   (cd macos && make all && make sign)
#   bash macos/scripts/release.sh vX.Y.Z --skip-build
#
# The last step verifies the bundles carry vX.Y.Z, pushes the tag, and uploads
# build/output/HarborClerk.dmg when it exists, the two app zips otherwise. If the
# upload fails after the push, the same command resumes: a tag already on origin
# at HEAD with no GitHub release is accepted, not refused. A tag this script
# created is deleted again on any failure before the push.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MACOS_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$MACOS_DIR")"
BUILD_OUTPUT="$MACOS_DIR/build/output"
# Overridable so the lifecycle tests can run where PlistBuddy does not exist.
PLISTBUDDY="${PLISTBUDDY:-/usr/libexec/PlistBuddy}"

# ── Parse args ──

VERSION="${1:-}"
SKIP_BUILD=false
CHECK_ONLY=false

if [ -z "$VERSION" ]; then
    echo "Usage: $0 <version> [--check | --skip-build]"
    echo "  e.g. $0 v1.0.0"
    exit 1
fi

shift
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=true ;;
        --check) CHECK_ONLY=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Validate version format
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Version must match vX.Y.Z (e.g. v1.0.0)"
    exit 1
fi
EXPECTED="${VERSION#v}"

cd "$PROJECT_ROOT"

# ── Preflight: the sources must already say what the tag will say ──
# Refusing here is the point of this script. The repo-shape test
# (tests/test_release_version.py) holds the same files together on every PR;
# this is the last check before a tag makes the number permanent.

# First `version = "..."` inside [project]; pyproject has other tables with keys
# of their own, so do not take the first match in the file.
pyproject_version="$(awk -F'"' '/^\[project\]/ { p = 1; next } /^\[/ { p = 0 } p && /^version = "/ { print $2; exit }' pyproject.toml)"
# npm writes the root "version" at two-space indent; nested packages are deeper.
package_version="$(sed -n -E 's/^  "version": "([^"]+)",?$/\1/p' frontend/package.json | head -1)"
lock_version="$(sed -n -E 's/^  "version": "([^"]+)",?$/\1/p' frontend/package-lock.json | head -1)"

mismatch=0
check_source() {
    local label="$1" actual="$2"
    if [ "$actual" != "$EXPECTED" ]; then
        echo "ERROR: ${label} says '${actual:-<none>}'; the release is ${EXPECTED}." >&2
        mismatch=1
    fi
}
check_source "pyproject.toml [project] version" "$pyproject_version"
check_source "frontend/package.json version" "$package_version"
check_source "frontend/package-lock.json version" "$lock_version"
if [ "$mismatch" = 1 ]; then
    echo "       Refusing to tag: the apps would carry ${EXPECTED} while the packages claim another version." >&2
    echo "       Bump them to ${EXPECTED} (and run 'uv lock' so uv.lock follows), land that via PR, then release." >&2
    exit 1
fi

# A dirty tree builds as "${EXPECTED}+0.g<hash>.dirty" and is refused by the
# stamp check below anyway; say so before anything is tagged.
git update-index -q --refresh  # a touched-but-unchanged file otherwise reads as modified
if ! git diff-index --quiet HEAD --; then
    echo "ERROR: the working tree has uncommitted changes to tracked files; a release is built from a clean tree." >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

head_commit="$(git rev-parse HEAD)"
tag_commit="$(git rev-parse -q --verify "refs/tags/${VERSION}^{commit}" 2>/dev/null || true)"
if [ -n "$tag_commit" ] && [ "$tag_commit" != "$head_commit" ]; then
    echo "ERROR: tag ${VERSION} already exists and points at ${tag_commit:0:7}, not at HEAD (${head_commit:0:7})." >&2
    exit 1
fi
# Two `v[0-9]*` tags at HEAD: after a botched release re-tagged under a new
# number, `git describe` picks between them by tagger date with an alphabetical
# tie-break, and an hour-long build would fail in verify_build. Refuse now.
other_tags="$(git tag --points-at HEAD 'v[0-9]*' | grep -vx -- "$VERSION" || true)"
if [ -n "$other_tags" ]; then
    echo "ERROR: HEAD also carries release tag(s) $(printf '%s' "$other_tags" | tr '\n' ' ')— git describe could stamp the build with one of those instead of ${VERSION}." >&2
    echo "       Delete the stray tag(s) locally (and on origin if pushed), or release the version they name." >&2
    exit 1
fi

# A tag already on origin normally means the release exists. The exception is a
# publish that failed *after* the push (v0.9.2: the DMG was over GitHub's 2 GiB
# asset limit): the tag is upstream at HEAD and no release has it, so a re-run
# must resume rather than refuse. `ls-remote` lists an annotated tag twice, the
# tag object and its peeled commit (`^{}`); take the peeled line when present.
remote_tag_at_head=0
fetch_tag=0
delete_draft=0
if git remote get-url origin >/dev/null 2>&1; then
    # The rc is kept apart from the output: offline, an empty answer used to read
    # as "tag is free", and a full run would tag and build for an hour before the
    # push told the truth.
    ls_rc=0
    ls_err="$(mktemp)"
    remote_refs="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags origin "refs/tags/${VERSION}" "refs/tags/${VERSION}^{}" 2>"$ls_err")" || ls_rc=$?
    if [ "$ls_rc" -ne 0 ]; then
        echo "ERROR: cannot reach origin to check whether tag ${VERSION} exists there; 'git ls-remote' exited ${ls_rc} with:" >&2
        sed 's/^/       /' "$ls_err" >&2
        rm -f "$ls_err"
        exit 1
    fi
    rm -f "$ls_err"
    if [ -n "$remote_refs" ]; then
        remote_commit="$(printf '%s\n' "$remote_refs" | awk '/\^\{\}$/ { peeled = $1 } !/\^\{\}$/ { plain = $1 } END { print (peeled != "" ? peeled : plain) }')"
        if [ "$remote_commit" != "$head_commit" ]; then
            echo "ERROR: tag ${VERSION} is on origin at ${remote_commit:0:7}, and HEAD is ${head_commit:0:7}." >&2
            echo "       If a publish of ${VERSION} failed after its push (and main has moved since), finish it from that commit:" >&2
            echo "         git checkout ${VERSION} && bash macos/scripts/release.sh ${VERSION} --skip-build" >&2
            echo "       Otherwise ${VERSION} is taken; release the next number." >&2
            exit 1
        fi
        # Only "no such release" means resume. Any other failure of gh (expired
        # auth, no network) must stop here: treating it as "no release" would
        # make `gh release create` fail with "already exists" and the resume
        # hint point back at this same command, forever. A *draft* is the
        # failed-upload leftover itself: `gh release create` uploads into a
        # draft and publishes last, and when the upload dies it may not manage
        # to delete the draft either. `gh release view` finds drafts, so ask
        # which it is and clear a draft out of the way before publishing.
        if ! command -v gh >/dev/null 2>&1; then
            echo "ERROR: tag ${VERSION} is on origin; gh is needed to tell whether release ${VERSION} already exists. Install with: brew install gh" >&2
            exit 1
        fi
        view_rc=0
        view_err="$(mktemp)"
        is_draft="$(gh release view "$VERSION" --json isDraft -q .isDraft 2>"$view_err")" || view_rc=$?
        if [ "$view_rc" -eq 0 ] && [ "$is_draft" = "true" ]; then
            delete_draft=1
            echo "==> GitHub holds a DRAFT release ${VERSION}: the leftover of a failed upload. It will be deleted before publishing."
        elif [ "$view_rc" -eq 0 ]; then
            rm -f "$view_err"
            echo "ERROR: tag ${VERSION} is on origin and GitHub already has release ${VERSION}; nothing to do." >&2
            exit 1
        elif ! grep -qi 'release not found' "$view_err"; then
            echo "ERROR: could not tell whether release ${VERSION} exists: 'gh release view' failed with:" >&2
            sed 's/^/       /' "$view_err" >&2
            rm -f "$view_err"
            exit 1
        fi
        rm -f "$view_err"
        remote_tag_at_head=1
        echo "==> Tag ${VERSION} is already on origin at HEAD with no published release: resuming the publish (the tag push is skipped)."
        if [ -z "$tag_commit" ]; then
            # The local tag is gone (an interrupted run, another clone, a hand
            # `git tag -d`). Minting a new tag object under the same name would
            # make the next `git fetch --tags` refuse to clobber origin's; take
            # origin's instead and treat it as pre-existing in both modes. Done
            # after the --check exit below: a preflight must not touch refs.
            fetch_tag=1
        fi
    fi
fi

tag_state="is free"
if [ "$fetch_tag" = 1 ]; then tag_state="is on origin at HEAD, release pending (not local yet; a run fetches it)"
elif [ "$remote_tag_at_head" = 1 ]; then tag_state="is on origin at HEAD, release pending"
elif [ -n "$tag_commit" ]; then tag_state="exists locally at HEAD"; fi
echo "==> Preflight OK: pyproject.toml, package.json and package-lock.json all say ${EXPECTED};" \
     "tree clean at ${head_commit:0:7}; tag ${VERSION} ${tag_state}"
if [ "$CHECK_ONLY" = true ]; then
    echo "==> Ready to release ${VERSION}."
    exit 0
fi

# ── Check prerequisites ──

if ! command -v gh &>/dev/null; then
    echo "Error: gh (GitHub CLI) is required. Install with: brew install gh"
    exit 1
fi

if [ "$fetch_tag" = 1 ]; then
    git fetch -q origin "refs/tags/${VERSION}:refs/tags/${VERSION}"
    tag_commit="$(git rev-parse -q --verify "refs/tags/${VERSION}^{commit}")"
    echo "==> Fetched tag ${VERSION} from origin"
fi

# ── Tag (local only, until the build is verified) ──

created_tag=0
if [ -z "$tag_commit" ]; then
    if [ "$SKIP_BUILD" = true ]; then
        echo "ERROR: --skip-build publishes an existing build, which must have been built with the tag in place," >&2
        echo "       and tag ${VERSION} does not exist. Either drop --skip-build, or:" >&2
        echo "         git tag -a ${VERSION} -m 'Release ${VERSION}' && (cd macos && make all && make sign)" >&2
        echo "       and run this again." >&2
        exit 1
    fi
    echo "==> Creating tag ${VERSION} at ${head_commit:0:7} (pushed only once the build carries it)"
    git tag -a "$VERSION" -m "Release $VERSION"
    created_tag=1
fi
# On EXIT, not ERR: an ERR trap does not fire on an explicit `exit 1`, and
# without `set -E` it is not inherited into functions, so a PlistBuddy failure
# inside verify_build or the "apps not found" exit below would have left the tag
# behind. EXIT fires on every path out of the script, including Ctrl-C during
# the build; the status decides whether there is anything to undo.
STAGING=""
undo_tag_on_failure() {
    local rc=$?
    [ -n "$STAGING" ] && rm -rf "$STAGING"
    if [ "$rc" -ne 0 ] && [ "$created_tag" = 1 ]; then
        git tag -d "$VERSION" >/dev/null 2>&1 || true
        echo "    (removed the local tag ${VERSION}; nothing was pushed)" >&2
    fi
}
trap undo_tag_on_failure EXIT

# ── Build ──

if [ "$SKIP_BUILD" = false ]; then
    echo "==> Building apps..."
    cd "$MACOS_DIR"
    make clean
    make all
    cd "$PROJECT_ROOT"
fi

if [ ! -d "$BUILD_OUTPUT/HarborClerkServer.app" ] || [ ! -d "$BUILD_OUTPUT/HarborClerk.app" ]; then
    echo "Error: Built apps not found in $BUILD_OUTPUT"
    exit 1
fi

# ── The build must carry the tag ──
# A bundle built before the tag existed carries the previous tag and a distance;
# one built from a dirty tree carries ".dirty"; one from another commit carries
# that commit. None of those may be published as ${VERSION}.
verify_build() {
    local app plist short num stamp
    for app in HarborClerkServer HarborClerk; do
        plist="$BUILD_OUTPUT/$app.app/Contents/Info.plist"
        short="$("$PLISTBUDDY" -c "Print :CFBundleShortVersionString" "$plist")"
        num="$("$PLISTBUDDY" -c "Print :CFBundleVersion" "$plist")"
        if [ "$short" != "$EXPECTED" ] || [ "$num" != "0" ]; then
            echo "ERROR: ${app}.app carries version ${short} (${num}); release ${VERSION} needs ${EXPECTED} (0)." >&2
            echo "       The bundle was built before the tag existed, from another commit, or from a dirty tree." >&2
            echo "       Run 'make apps' (then 'make sign') with the tag in place and try again." >&2
            return 1
        fi
    done
    stamp="$(cat "$BUILD_OUTPUT/HarborClerkServer.app/Contents/Resources/venv/build-version.txt" 2>/dev/null || echo unstamped)"
    if [ "$stamp" != "$EXPECTED" ]; then
        echo "ERROR: the bundled venv is stamped '${stamp}', not ${EXPECTED}; /api/system/health would report that." >&2
        echo "       Run 'make apps' (then 'make sign') with the tag in place and try again." >&2
        return 1
    fi
    echo "==> Bundles carry ${EXPECTED} (0) and the venv is stamped ${stamp}"
}
verify_build

# ── Push the tag ──

# From here the tag is treated as upstream *before* the push runs: an
# interruption between a successful push and this line would otherwise delete
# the local tag while origin keeps it, and the next run would mint a second tag
# object under the same name. A push that fails leaves a local tag the owner
# can push by hand or delete; the resume path above handles a pushed one.
created_tag=0
if [ "$remote_tag_at_head" = 1 ]; then
    echo "==> Tag ${VERSION} is already on origin; not pushing again"
else
    echo "==> Pushing tag ${VERSION}"
    git push origin "$VERSION"
fi

# ── Package ──

STAGING=$(mktemp -d)
echo "==> Packaging release artifacts..."

assets=()
notes_rows=""
DMG_PATH="$BUILD_OUTPUT/HarborClerk.dmg"
if [ -f "$DMG_PATH" ]; then
    # `make sign` produced a notarized, stapled image holding both apps.
    cp "$DMG_PATH" "$STAGING/HarborClerk-${VERSION}.dmg"
    assets+=("$STAGING/HarborClerk-${VERSION}.dmg")
    notes_rows="| \`HarborClerk-${VERSION}.dmg\` | Both apps, signed and notarized | $(du -h "$DMG_PATH" | cut -f1) |"
    echo "  DMG: $(du -h "$DMG_PATH" | cut -f1)"
else
    # Zip each app separately so users can download what they need
    ditto -c -k --sequesterRsrc --keepParent \
        "$BUILD_OUTPUT/HarborClerkServer.app" \
        "$STAGING/HarborClerkServer-${VERSION}.zip"

    ditto -c -k --sequesterRsrc --keepParent \
        "$BUILD_OUTPUT/HarborClerk.app" \
        "$STAGING/HarborClerk-${VERSION}.zip"

    SERVER_SIZE=$(du -h "$STAGING/HarborClerkServer-${VERSION}.zip" | cut -f1)
    CLIENT_SIZE=$(du -h "$STAGING/HarborClerk-${VERSION}.zip" | cut -f1)
    assets+=("$STAGING/HarborClerkServer-${VERSION}.zip" "$STAGING/HarborClerk-${VERSION}.zip")
    notes_rows="| \`HarborClerkServer-${VERSION}.zip\` | Menubar server app (includes all services) | $SERVER_SIZE |
| \`HarborClerk-${VERSION}.zip\` | Client app (web UI wrapper) | $CLIENT_SIZE |"

    echo "  Server: $SERVER_SIZE"
    echo "  Client: $CLIENT_SIZE"
fi

# ── Create release ──

if [ "$delete_draft" = 1 ]; then
    # gh's own "already exists" check looks at published releases only, but the
    # draft would still shadow the tag; clear it first.
    echo "==> Deleting the leftover draft release ${VERSION}"
    gh release delete "$VERSION" --yes
fi

echo "==> Creating GitHub release $VERSION"
publish_failed() {
    echo "ERROR: 'gh release create ${VERSION}' failed. The tag is pushed and the build is verified;" >&2
    echo "       fix the cause (asset size, auth, network) and resume with:" >&2
    echo "         bash macos/scripts/release.sh ${VERSION} --skip-build" >&2
    echo "       which recognises the pushed tag and publishes without re-tagging." >&2
    exit 1
}
gh release create "$VERSION" \
    "${assets[@]}" \
    --title "Harbor Clerk $VERSION" \
    --notes "$(cat <<EOF
## Downloads

| File | Description | Size |
|------|-------------|------|
${notes_rows}

### Installation

1. Download and open the image (or unzip both archives)
2. Move both apps to \`/Applications\`
3. Launch **Harbor Clerk Server** — services start automatically
4. Launch **Harbor Clerk** to open the web UI
5. Create your admin account on the setup page

### Requirements

- Apple Silicon Mac (M1 or later)
- macOS 15.0 (Sequoia) or later
- 16 GB RAM minimum
EOF
)" || publish_failed

# STAGING is removed by the EXIT trap on every path out.

echo "==> Release $VERSION published!"
echo "    https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/$VERSION"
