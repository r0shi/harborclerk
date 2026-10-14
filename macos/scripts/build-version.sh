#!/usr/bin/env bash
# Derive every version stamp a build carries from one source: the release tag.
#
# Prints shell assignments, meant for `eval "$(bash scripts/build-version.sh)"`:
#
#   HC_VERSION        MARKETING_VERSION / CFBundleShortVersionString: the nearest
#                     `v[0-9]*` tag without its `v` — "0.9.3"
#   HC_BUILD_NUMBER   CURRENT_PROJECT_VERSION / CFBundleVersion: commits since that
#                     tag — "0" is the tagged commit itself, anything else is a
#                     development build. Kept numeric: Apple allows only digits
#                     and periods here, so the dirty marker cannot live in it.
#   HC_BUILD_HASH     short commit, "-dirty" appended when the tree has
#                     uncommitted tracked changes — "887c3fb", "887c3fb-dirty"
#   HC_VERSION_FULL   what /api/system/health reports as "version": HC_VERSION
#                     alone for a clean build of the tagged commit, otherwise
#                     HC_VERSION plus a local segment that says exactly how far
#                     from it the build is — "0.9.3+2.g887c3fb", "0.9.3+0.ga40dbe9.dirty"
#   HC_RELEASE        1 for a clean build of the tagged commit, else 0
#
# Usage:
#   build-version.sh              describe the checkout this script lives in
#   build-version.sh DESCRIBE     parse a fixed `git describe --tags --long --dirty`
#                                 string instead, for tests
#
# Without any `v[0-9]*` tag reachable (shallow CI clone, tarball) the version is
# 0.0.0 with an "untagged" local segment, never a number that looks released.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ "$#" -ge 1 ]; then
    describe="$1"
else
    # --always falls back to the bare short hash when no tag matches, so the one
    # failure mode left is "not a git checkout at all", which yields "".
    # 'v[0-9]*', not 'v*': a tag such as `vendor-x` would otherwise describe as a version.
    describe="$(git -C "$PROJECT_ROOT" describe --tags --match 'v[0-9]*' --long --dirty --always 2>/dev/null || true)"
fi

# The output is `eval`ed by package.sh and the Makefile, so every value is
# validated here: a tag is text anyone with push rights can write, and
# `v1.2.3;echo` must fail, not run. Exactly X.Y.Z, the only shape release.sh
# publishes: a hand-tagged `v1.0.0-rc1` is refused here too rather than built
# as a release that release.sh would then refuse to ship.
VERSION_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+$'

dirty=""
if [[ "$describe" =~ ^v(.+)-([0-9]+)-g([0-9a-f]+)(-dirty)?$ ]]; then
    version="${BASH_REMATCH[1]}"
    distance="${BASH_REMATCH[2]}"
    hash="${BASH_REMATCH[3]}"
    dirty="${BASH_REMATCH[4]}"
    if ! [[ "$version" =~ $VERSION_PATTERN ]]; then
        echo "ERROR: cannot derive a version from git describe output '${describe}': tag '${version}' is not X.Y.Z" >&2
        exit 1
    fi
    if [ "$distance" = "0" ] && [ -z "$dirty" ]; then
        full="$version"
    else
        full="${version}+${distance}.g${hash}${dirty:+.dirty}"
    fi
elif [[ "$describe" =~ ^([0-9a-f]+)(-dirty)?$ ]]; then
    version="0.0.0"
    distance="0"
    hash="${BASH_REMATCH[1]}"
    dirty="${BASH_REMATCH[2]}"
    full="${version}+untagged.g${hash}${dirty:+.dirty}"
elif [ -z "$describe" ]; then
    version="0.0.0"
    distance="0"
    hash="unknown"
    full="${version}+untagged"
else
    echo "ERROR: cannot derive a version from git describe output '${describe}'" >&2
    exit 1
fi

release=0
if [ "$distance" = "0" ] && [ -z "$dirty" ] && [ "$version" != "0.0.0" ]; then
    release=1
fi

printf 'HC_VERSION=%s\n' "$version"
printf 'HC_BUILD_NUMBER=%s\n' "$distance"
printf 'HC_BUILD_HASH=%s\n' "${hash}${dirty}"
printf 'HC_VERSION_FULL=%s\n' "$full"
printf 'HC_RELEASE=%s\n' "$release"
