"""The release tag is the only source of truth for a version (#624).

Up to v0.9.3 every release shipped with `pyproject.toml` at 0.8.0, the npm
package at 0.8.0 and both apps at `1.0 (1)`, because nothing tied those numbers
to the tag. Now `macos/scripts/build-version.sh` derives every stamp from
`git describe` of the commit being built, `package.sh` passes the result to
both Xcode projects and writes it into the venv, and `release.sh` refuses to
tag while the checked-in versions disagree with the tag. These tests hold the
pieces together on every PR:

- `pyproject.toml`, `frontend/package.json`, its lock and `uv.lock` agree;
- on a tagged commit, that number is the tag's (skipped elsewhere, with why);
- the derivation gives the documented answers for fixed `git describe` inputs,
  so it is checked without a real tag;
- the pbxproj files carry only the `0.0.0-dev` sentinel, so a plain Xcode build
  is distinguishable and nobody bumps them by hand again;
- every release `xcodebuild` receives the version, and both `Info.plist`s read it;
- `release.sh` refuses to tag on a mismatch, a dirty tree or a misplaced tag,
  and passes `--check` when everything agrees — run against a fixture repo, so
  no real tag is involved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MACOS = REPO / "macos"
BUILD_VERSION_SH = MACOS / "scripts" / "build-version.sh"
RELEASE_SH = MACOS / "scripts" / "release.sh"
PACKAGE_SH = MACOS / "scripts" / "package.sh"
PBXPROJS = sorted(MACOS.glob("*/*.xcodeproj/project.pbxproj"))
INFO_PLISTS = sorted(MACOS.glob("*/*/Info.plist"))
RELEASE_NUMBER = re.compile(r"^\d+\.\d+\.\d+$")


# ── The checked-in versions agree ───────────────────────────────────────────


def _pyproject_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _package_json_version() -> str:
    return json.loads((REPO / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]


def _package_lock_versions() -> tuple[str, str]:
    lock = json.loads((REPO / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    return lock["version"], lock["packages"][""]["version"]


def _uv_lock_version() -> str:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    (entry,) = [p for p in lock["package"] if p["name"] == "harbor-clerk"]
    return entry["version"]


def test_checked_in_versions_agree() -> None:
    versions = {
        "pyproject.toml": _pyproject_version(),
        "frontend/package.json": _package_json_version(),
        "frontend/package-lock.json": _package_lock_versions()[0],
        "frontend/package-lock.json packages['']": _package_lock_versions()[1],
        "uv.lock harbor-clerk": _uv_lock_version(),
    }
    assert len(set(versions.values())) == 1, f"versions disagree: {versions}"


def test_checked_in_version_is_a_release_number() -> None:
    assert RELEASE_NUMBER.match(_pyproject_version()), (
        f"pyproject.toml version {_pyproject_version()!r} is not X.Y.Z; release.sh only tags vX.Y.Z"
    )


def _git(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)


def test_a_tagged_head_carries_the_checked_in_version() -> None:
    """Only checkable on a release commit; everywhere else the reason is the skip."""
    exact = _git("describe", "--tags", "--match", "v[0-9]*", "--exact-match", "HEAD")
    if exact.returncode != 0:
        nearest = (
            _git("describe", "--tags", "--match", "v[0-9]*", "--long", "--always").stdout.strip() or "no tag reachable"
        )
        pytest.skip(f"HEAD is not a tagged commit ({nearest}); the tag/version agreement is checked on release commits")
    tag = exact.stdout.strip()
    assert tag == f"v{_pyproject_version()}", (
        f"HEAD is tagged {tag} but pyproject.toml says {_pyproject_version()}; release.sh should have refused this tag"
    )


# ── The derivation, on fixed inputs ─────────────────────────────────────────


def _derive(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(BUILD_VERSION_SH), *args], capture_output=True, text=True, timeout=60)


def _parse(stdout: str) -> dict[str, str]:
    pairs = [line.split("=", 1) for line in stdout.splitlines() if line]
    assert all(len(p) == 2 for p in pairs), f"not KEY=value lines: {stdout!r}"
    return dict(pairs)


@pytest.mark.parametrize(
    ("describe", "expected"),
    [
        (
            "v0.9.3-0-ga40dbe9",
            {
                "HC_VERSION": "0.9.3",
                "HC_BUILD_NUMBER": "0",
                "HC_BUILD_HASH": "a40dbe9",
                "HC_VERSION_FULL": "0.9.3",
                "HC_RELEASE": "1",
            },
        ),
        (
            "v0.9.3-2-g887c3fb",
            {
                "HC_VERSION": "0.9.3",
                "HC_BUILD_NUMBER": "2",
                "HC_BUILD_HASH": "887c3fb",
                "HC_VERSION_FULL": "0.9.3+2.g887c3fb",
                "HC_RELEASE": "0",
            },
        ),
        (
            "v0.9.3-2-g887c3fb-dirty",
            {
                "HC_VERSION": "0.9.3",
                "HC_BUILD_NUMBER": "2",
                "HC_BUILD_HASH": "887c3fb-dirty",
                "HC_VERSION_FULL": "0.9.3+2.g887c3fb.dirty",
                "HC_RELEASE": "0",
            },
        ),
        (
            # The tagged commit with uncommitted changes is not the release either.
            "v0.9.3-0-ga40dbe9-dirty",
            {
                "HC_VERSION": "0.9.3",
                "HC_BUILD_NUMBER": "0",
                "HC_BUILD_HASH": "a40dbe9-dirty",
                "HC_VERSION_FULL": "0.9.3+0.ga40dbe9.dirty",
                "HC_RELEASE": "0",
            },
        ),
        (
            # No v* tag reachable (shallow clone): never a number that looks released.
            "887c3fb",
            {
                "HC_VERSION": "0.0.0",
                "HC_BUILD_NUMBER": "0",
                "HC_BUILD_HASH": "887c3fb",
                "HC_VERSION_FULL": "0.0.0+untagged.g887c3fb",
                "HC_RELEASE": "0",
            },
        ),
        (
            "",
            {
                "HC_VERSION": "0.0.0",
                "HC_BUILD_NUMBER": "0",
                "HC_BUILD_HASH": "unknown",
                "HC_VERSION_FULL": "0.0.0+untagged",
                "HC_RELEASE": "0",
            },
        ),
    ],
    ids=["exact-tag", "past-tag", "past-tag-dirty", "exact-tag-dirty", "untagged", "not-a-checkout"],
)
def test_build_version_derivation(describe: str, expected: dict[str, str]) -> None:
    result = _derive(describe)
    assert result.returncode == 0, result.stderr
    assert _parse(result.stdout) == expected


@pytest.mark.parametrize(
    "describe",
    [
        "release-candidate",
        # The output is `eval`ed by package.sh and the Makefile: a tag is text
        # anyone with push rights can write, and this one must fail, not run.
        "v1.2.3;echo-0-gabc1234",
        'v1.2.3"$(id)-0-gabc1234',
        # `--match 'v*'` used to accept this and derive MARKETING_VERSION=endor-x.
        "vendor-x-0-gabc1234",
        "v1.2-0-gabc1234",
        # A hand-tagged pre-release would otherwise build as HC_RELEASE=1 and then
        # be refused by release.sh, which only publishes vX.Y.Z.
        "v1.0.0-rc1-0-gabc1234",
    ],
    ids=["not-a-describe", "shell-metachar", "command-substitution", "vendor-tag", "two-part", "pre-release"],
)
def test_build_version_rejects_what_it_cannot_parse(describe: str) -> None:
    result = _derive(describe)
    assert result.returncode != 0, f"{describe!r} must fail the build, not stamp something: {result.stdout!r}"
    assert "cannot derive" in result.stderr
    assert not result.stdout.strip(), "nothing may reach eval on the error path"


def test_build_version_matches_only_numeric_v_tags() -> None:
    assert "--match 'v[0-9]*'" in BUILD_VERSION_SH.read_text(encoding="utf-8")


def test_build_version_describes_this_checkout_through_the_same_parser() -> None:
    """The no-argument path runs `git describe` itself; its answer must equal the
    fixed-input path fed the same string, and must be a usable version."""
    describe = _git("describe", "--tags", "--match", "v[0-9]*", "--long", "--dirty", "--always").stdout.strip()
    live, fixed = _derive(), _derive(describe)
    assert live.returncode == 0, live.stderr
    assert _parse(live.stdout) == _parse(fixed.stdout)
    assert RELEASE_NUMBER.match(_parse(live.stdout)["HC_VERSION"])
    assert _parse(live.stdout)["HC_BUILD_NUMBER"].isdigit()


# ── Nothing but the build line sets a version in Xcode ──────────────────────


def test_both_xcode_projects_exist() -> None:
    assert [p.parent.parent.name for p in PBXPROJS] == ["HarborClerk", "HarborClerkServer"], PBXPROJS


@pytest.mark.parametrize("pbxproj", PBXPROJS, ids=lambda p: p.parent.parent.name)
def test_pbxproj_carries_only_the_dev_sentinel(pbxproj: Path) -> None:
    """`MARKETING_VERSION` in the project file is a sentinel, not a version: a
    build that bypasses package.sh must be recognisable as such, and a hand-bump
    here is how 1.0 shipped on every 0.9.x release."""
    text = pbxproj.read_text(encoding="utf-8")
    marketing = re.findall(r"MARKETING_VERSION = ([^;]+);", text)
    project = re.findall(r"CURRENT_PROJECT_VERSION = ([^;]+);", text)
    assert marketing and project, f"{pbxproj} no longer sets the version keys package.sh overrides"
    assert set(marketing) == {'"0.0.0-dev"'}, f"MARKETING_VERSION must stay the 0.0.0-dev sentinel: {marketing}"
    assert set(project) == {"0"}, f"CURRENT_PROJECT_VERSION must stay 0: {project}"


@pytest.mark.parametrize("plist", INFO_PLISTS, ids=lambda p: p.parent.name)
def test_info_plist_reads_the_build_settings(plist: Path) -> None:
    text = plist.read_text(encoding="utf-8")
    assert re.search(r"<key>CFBundleShortVersionString</key>\s*<string>\$\(MARKETING_VERSION\)</string>", text)
    assert re.search(r"<key>CFBundleVersion</key>\s*<string>\$\(CURRENT_PROJECT_VERSION\)</string>", text)


def test_package_sh_passes_the_version_to_every_xcodebuild_and_stamps_the_venv() -> None:
    text = PACKAGE_SH.read_text(encoding="utf-8")
    invocations = len(re.findall(r"^xcodebuild ", text, re.MULTILINE))
    assert invocations == 2, f"expected two xcodebuild invocations in package.sh, found {invocations}"
    assert text.count('MARKETING_VERSION="$HC_VERSION"') == invocations
    assert text.count('CURRENT_PROJECT_VERSION="$HC_BUILD_NUMBER"') == invocations
    assert "build-version.sh" in text, "package.sh must derive the version from build-version.sh"
    assert '"$HC_BUILD_HASH" > "$BUILD_DIR/venv/build-hash.txt"' in text
    assert '"$HC_VERSION_FULL" > "$BUILD_DIR/venv/build-version.txt"' in text
    assert "CFBundleShortVersionString" in text, "package.sh must read the built plist back"


def test_makefile_no_longer_stamps_the_venv_itself() -> None:
    """Two writers of build-hash.txt would disagree the moment one ran without the
    other; the stamp lives where the bundle is assembled."""
    # Join backslash continuations first: `make dev` is one recipe line spread
    # over several, and the derivation and the xcodebuild must be on the same one.
    text = (MACOS / "Makefile").read_text(encoding="utf-8").replace("\\\n", " ")
    recipes = [line for line in text.splitlines() if line.startswith("\t")]
    assert not [r for r in recipes if "build-hash.txt" in r], "the Makefile writes build-hash.txt; package.sh owns it"
    assert any("build-version.sh" in r and "xcodebuild" in r for r in recipes), (
        "no recipe both derives the version (build-version.sh) and runs xcodebuild; make dev lost the stamps"
    )


# ── release.sh refuses to mint a tag the sources contradict ─────────────────


def _fixture(tmp_path: Path, *, pyproject: str = "9.9.9", package: str = "9.9.9", lock: str = "9.9.9") -> Path:
    """A throwaway repo shaped like this one where release.sh matters: the three
    version files and the script at its real relative location, so its own
    PROJECT_ROOT discovery resolves to the fixture."""
    root = tmp_path / "repo"
    (root / "macos" / "scripts").mkdir(parents=True)
    (root / "frontend").mkdir()
    shutil.copy(RELEASE_SH, root / "macos" / "scripts" / "release.sh")
    (root / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "fixture"\nversion = "{pyproject}"\n\n'
        '[tool.other]\nversion = "0.0.1"\n',
        encoding="utf-8",
    )
    (root / "frontend" / "package.json").write_text(
        json.dumps({"name": "fixture", "version": package, "dependencies": {"left-pad": "^1.0.0"}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "frontend" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": lock,
                "lockfileVersion": 3,
                "packages": {"": {"version": lock}, "node_modules/left-pad": {"version": "1.3.0"}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # `make clean && make all` must run and do nothing, so release.sh gets past
    # the build and to the paths that own the tag.
    (root / "macos" / "Makefile").write_text(".PHONY: clean all\nclean:\n\t@true\nall:\n\t@true\n", encoding="utf-8")
    _fixture_git(root, "init", "-q")
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "fixture")
    return root


def _stub_gh(tmp_path: Path) -> Path:
    """A `gh` whose `release view --json isDraft -q .isDraft` answers per
    FIXTURE_GH_RELEASE_VIEW — `missing` (gh's real "release not found" on stderr,
    exit 1; the default), `exists` (prints false), `draft` (prints true) or
    `error` (an auth failure on stderr, exit 1) — records `release create` and
    `release delete` calls in FIXTURE_GH_LOG (create exits FIXTURE_GH_CREATE_RC),
    and names a repo. Beside it, a `PlistBuddy` that answers `Print :Key` from the one-line
    XML the fixture writes and fails like the real one on a missing key, so the
    bundle checks run where /usr/libexec/PlistBuddy does not exist."""
    d = tmp_path / "stub-bin"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "release view")\n'
        '    case "${FIXTURE_GH_RELEASE_VIEW:-missing}" in\n'
        "      exists) echo false; exit 0 ;;\n"
        "      draft) echo true; exit 0 ;;\n"
        '      error) echo "HTTP 401: Bad credentials (https://api.github.com/graphql)" >&2; exit 1 ;;\n'
        '      *) echo "release not found" >&2; exit 1 ;;\n'
        "    esac ;;\n"
        '  "release delete") printf \'%s\\n\' "$*" >> "${FIXTURE_GH_LOG:-/dev/null}" ;;\n'
        '  "release create") printf \'%s\\n\' "$*" >> "${FIXTURE_GH_LOG:-/dev/null}"; exit "${FIXTURE_GH_CREATE_RC:-0}" ;;\n'
        '  "repo view") echo fixture/repo ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    plistbuddy = d / "PlistBuddy"
    plistbuddy.write_text(
        "#!/bin/sh\n"
        '# usage: PlistBuddy -c "Print :Key" file\n'
        'key="${2#Print :}"; file="$3"\n'
        'value="$(sed -n "s|.*<key>${key}</key><string>\\([^<]*\\)</string>.*|\\1|p" "$file" | head -1)"\n'
        'if [ -z "$value" ]; then echo "Print: Entry, \\":${key}\\", Does Not Exist" >&2; exit 1; fi\n'
        "printf '%s\\n' \"$value\"\n",
        encoding="utf-8",
    )
    plistbuddy.chmod(0o755)
    return d


def _add_origin(root: Path) -> Path:
    remote = root.parent / "origin.git"
    _fixture_git(root, "init", "-q", "--bare", str(remote))
    _fixture_git(root, "remote", "add", "origin", str(remote))
    return remote


def _fake_output(root: Path, *, short: str = "9.9.9", num: str = "0", stamp: str = "9.9.9") -> None:
    """What package.sh leaves in build/output, reduced to what verify_build reads."""
    out = root / "macos" / "build" / "output"
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"<key>CFBundleShortVersionString</key><string>{short}</string>\n"
        f"<key>CFBundleVersion</key><string>{num}</string>\n"
        "</dict></plist>\n"
    )
    for app in ("HarborClerkServer", "HarborClerk"):
        contents = out / f"{app}.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_text(plist, encoding="utf-8")
    venv = out / "HarborClerkServer.app" / "Contents" / "Resources" / "venv"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "build-version.txt").write_text(stamp + "\n", encoding="utf-8")


def _remote_tags(remote: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(remote), "tag", "--list"], capture_output=True, text=True, check=True)
    return out.stdout.split()


def _fixture_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result


def _release(root: Path, *args: str, gh: Path | None = None, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        # release.sh runs a bare `git tag -a`, which needs an identity.
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        **extra_env,
    }
    if gh is not None:
        env["PATH"] = f"{gh}{os.pathsep}{env['PATH']}"
        env["PLISTBUDDY"] = str(gh / "PlistBuddy")
    return subprocess.run(
        ["bash", str(root / "macos" / "scripts" / "release.sh"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _tags(root: Path) -> list[str]:
    return _fixture_git(root, "tag", "--list").stdout.split()


def test_release_check_passes_when_every_source_agrees(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    result = _release(root, "v9.9.9", "--check")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Ready to release v9.9.9" in result.stdout
    assert _tags(root) == [], "--check must not tag"


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"pyproject": "9.9.8"}, "pyproject.toml"),
        ({"package": "9.9.8"}, "frontend/package.json"),
        ({"lock": "9.9.8"}, "frontend/package-lock.json"),
    ],
    ids=["pyproject", "package.json", "package-lock.json"],
)
def test_release_refuses_to_tag_when_a_source_disagrees(tmp_path: Path, kwargs: dict[str, str], named: str) -> None:
    root = _fixture(tmp_path, **kwargs)
    for mode in (("--check",), ()):
        result = _release(root, "v9.9.9", *mode)
        assert result.returncode != 0, f"release.sh {mode} accepted a version the sources contradict: {result.stdout}"
        assert named in result.stderr and "9.9.8" in result.stderr, result.stderr
        assert "Refusing to tag" in result.stderr
        assert _tags(root) == [], "a refused release must leave no tag behind"


def test_release_refuses_a_dirty_tree(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    with (root / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write("# uncommitted\n")
    result = _release(root, "v9.9.9", "--check")
    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr, result.stderr
    assert _tags(root) == []


def test_release_refuses_a_tag_that_points_elsewhere(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    first = _fixture_git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "CHANGES").write_text("second\n", encoding="utf-8")
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "second")
    _fixture_git(root, "tag", "v9.9.9", first)
    result = _release(root, "v9.9.9", "--check")
    assert result.returncode != 0
    assert "points at" in result.stderr, result.stderr


def test_release_accepts_a_local_tag_already_at_head(tmp_path: Path) -> None:
    """The documented flow tags by hand, builds, signs, then publishes with
    --skip-build; the preflight must recognise that tag rather than refuse it."""
    root = _fixture(tmp_path)
    _fixture_git(root, "tag", "v9.9.9")
    result = _release(root, "v9.9.9", "--check")
    assert result.returncode == 0, result.stderr
    assert "exists locally at HEAD" in result.stdout


def test_release_rejects_a_malformed_version(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    for bad in ("9.9.9", "v9.9", "v9.9.9-rc1"):
        result = _release(root, bad, "--check")
        assert result.returncode != 0, f"{bad!r} was accepted"
    assert _tags(root) == []


# ── The tag lifecycle: created only for a build that carries it ─────────────

NEEDS_DITTO = pytest.mark.skipif(sys.platform != "darwin", reason="the zip fallback uses ditto")


def test_release_removes_its_tag_when_the_build_leaves_no_apps(tmp_path: Path) -> None:
    """Preflight passes, the tag is created, the (no-op) build produces no
    bundles: the explicit `exit 1` that follows must still take the tag with
    it. An ERR trap did not fire there, and the tag survived."""
    root = _fixture(tmp_path)
    result = _release(root, "v9.9.9", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert "Built apps not found" in result.stdout + result.stderr, result.stdout + result.stderr
    assert "removed the local tag v9.9.9" in result.stderr
    assert _tags(root) == [], "the tag release.sh created outlived the failed release"


def test_release_skip_build_without_a_tag_refuses(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _fake_output(root)
    result = _release(root, "v9.9.9", "--skip-build", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert "does not exist" in result.stderr, result.stderr
    assert _tags(root) == []


@pytest.mark.parametrize(
    ("output", "says"),
    [
        ({"num": "2"}, "carries version 9.9.9 (2)"),  # built before the tag existed
        ({"short": "9.9.8", "num": "0"}, "carries version 9.9.8 (0)"),  # another release's bundle
        ({"stamp": "9.9.9+0.gabc1234.dirty"}, "stamped '9.9.9+0.gabc1234.dirty'"),  # dirty tree
    ],
    ids=["distance", "other-version", "dirty-stamp"],
)
def test_release_refuses_a_bundle_that_does_not_carry_the_tag(tmp_path: Path, output: dict, says: str) -> None:
    """The hand flow tags first and publishes with --skip-build; a bundle that
    says anything but X.Y.Z (0) is refused, and a tag the script did not create
    is left alone — it is the owner's."""
    root = _fixture(tmp_path)
    _fixture_git(root, "tag", "v9.9.9")
    _fake_output(root, **output)
    result = _release(root, "v9.9.9", "--skip-build", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert says in result.stderr, result.stderr
    assert _tags(root) == ["v9.9.9"], "a tag release.sh did not create must not be deleted"


def test_release_removes_its_tag_when_verify_build_fails_inside_the_function(tmp_path: Path) -> None:
    """PlistBuddy failing on a plist without the key aborts under `set -e`
    inside verify_build; the EXIT trap must still remove the tag."""
    root = _fixture(tmp_path)
    _fake_output(root)
    for app in ("HarborClerkServer", "HarborClerk"):
        (root / "macos" / "build" / "output" / f"{app}.app" / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict></dict></plist>\n', encoding="utf-8"
        )
    result = _release(root, "v9.9.9", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert _tags(root) == [], "the tag outlived a verify_build failure"


def _fake_dmg(root: Path) -> None:
    (root / "macos" / "build" / "output" / "HarborClerk.dmg").write_bytes(b"not really a disk image\n")


def _publishing_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = _fixture(tmp_path)
    remote = _add_origin(root)
    _fixture_git(root, "push", "-q", "-u", "origin", "HEAD")
    _fake_output(root)
    _fake_dmg(root)
    return root, remote, _stub_gh(tmp_path), tmp_path / "gh.log"


def _creates(log: Path) -> list[str]:
    # The stub logs the whole argument list, release notes included; count the calls.
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("release create ")]


def _origin_tag_object(remote: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "refs/tags/v9.9.9"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_release_publishes_then_resumes_after_a_failed_upload(tmp_path: Path) -> None:
    """Happy path against a bare origin and a stub gh, then the v0.9.2 case:
    the tag is pushed, the upload fails, and a re-run must resume rather than
    refuse 'already on origin'; once the release exists it is refused."""
    root, remote, gh, log = _publishing_fixture(tmp_path)

    failed = _release(root, "v9.9.9", gh=gh, FIXTURE_GH_LOG=str(log), FIXTURE_GH_CREATE_RC="1")
    assert failed.returncode != 0
    assert "resume with" in failed.stderr and "--skip-build" in failed.stderr, failed.stderr
    assert _tags(root) == ["v9.9.9"] and _remote_tags(remote) == ["v9.9.9"], "a pushed tag must stay"

    resumed = _release(root, "v9.9.9", "--skip-build", gh=gh, FIXTURE_GH_LOG=str(log))
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "resuming the publish" in resumed.stdout and "not pushing again" in resumed.stdout
    assert "Release v9.9.9 published" in resumed.stdout
    creates = _creates(log)
    assert len(creates) == 2 and all("HarborClerk-v9.9.9.dmg" in line for line in creates), creates

    exists = _release(root, "v9.9.9", "--skip-build", gh=gh, FIXTURE_GH_RELEASE_VIEW="exists")
    assert exists.returncode != 0
    assert "already has release" in exists.stderr, exists.stderr


def test_release_resumes_from_a_clone_that_lost_the_local_tag(tmp_path: Path) -> None:
    """An interrupted run, another clone or a hand `git tag -d`: the tag is on
    origin at HEAD and absent locally. Minting a new annotated tag under that
    name would make the next `git fetch --tags` refuse to clobber origin's;
    the script must fetch origin's instead, in both modes, and publish."""
    root, remote, gh, log = _publishing_fixture(tmp_path)
    failed = _release(root, "v9.9.9", gh=gh, FIXTURE_GH_LOG=str(log), FIXTURE_GH_CREATE_RC="1")
    assert failed.returncode != 0 and _remote_tags(remote) == ["v9.9.9"]
    origin_object = _origin_tag_object(remote)

    _fixture_git(root, "tag", "-d", "v9.9.9")
    check = _release(root, "v9.9.9", "--check", gh=gh)
    assert check.returncode == 0, check.stderr
    assert "release pending (not local yet; a run fetches it)" in check.stdout
    assert _tags(root) == [], "--check is a preflight and must not touch local refs"

    for mode in (("--skip-build",), ()):
        if _tags(root):  # the previous run fetched it; --check above did not
            _fixture_git(root, "tag", "-d", "v9.9.9")
        resumed = _release(root, "v9.9.9", *mode, gh=gh, FIXTURE_GH_LOG=str(log))
        assert resumed.returncode == 0, f"{mode}: {resumed.stderr}{resumed.stdout}"
        assert "Release v9.9.9 published" in resumed.stdout
        assert "Fetched tag v9.9.9 from origin" in resumed.stdout
        local_object = _fixture_git(root, "rev-parse", "refs/tags/v9.9.9").stdout.strip()
        assert local_object == origin_object, f"{mode}: a new tag object was minted under origin's name"
        assert _origin_tag_object(remote) == origin_object
    assert len(_creates(log)) == 3


def test_release_keeps_the_local_tag_when_the_push_fails(tmp_path: Path) -> None:
    """created_tag is cleared *before* the push, so a tag that did not make it
    upstream is the owner's to push or delete; it is never deleted under a tag
    origin may already hold after a half-finished push."""
    root = _fixture(tmp_path)
    remote = _add_origin(root)
    _fixture_git(root, "push", "-q", "-u", "origin", "HEAD")
    _fake_output(root)
    _fake_dmg(root)
    gh = _stub_gh(tmp_path)
    # Reject every push on the remote side; ls-remote still works.
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refused by fixture' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result = _release(root, "v9.9.9", gh=gh)
    assert result.returncode != 0
    assert "refused by fixture" in result.stderr, result.stderr
    assert _tags(root) == ["v9.9.9"], "a tag that failed to push was deleted"
    assert "removed the local tag" not in result.stderr


def test_release_refuses_when_gh_cannot_say_whether_the_release_exists(tmp_path: Path) -> None:
    """Expired auth or no network must not read as 'no release yet': the publish
    would fail with 'already exists' and the resume hint would loop."""
    root, remote, gh, log = _publishing_fixture(tmp_path)
    failed = _release(root, "v9.9.9", gh=gh, FIXTURE_GH_LOG=str(log), FIXTURE_GH_CREATE_RC="1")
    assert failed.returncode != 0 and _remote_tags(remote) == ["v9.9.9"]
    result = _release(root, "v9.9.9", "--skip-build", gh=gh, FIXTURE_GH_RELEASE_VIEW="error")
    assert result.returncode != 0
    assert "could not tell whether release v9.9.9 exists" in result.stderr, result.stderr
    assert "Bad credentials" in result.stderr
    assert len(_creates(log)) == 1, "nothing may be published while the release state is unknown"


def test_release_refuses_a_tag_on_origin_at_another_commit(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    remote = _add_origin(root)
    first = _fixture_git(root, "rev-parse", "HEAD").stdout.strip()
    _fixture_git(root, "tag", "v9.9.9")
    _fixture_git(root, "push", "-q", "origin", "HEAD", "v9.9.9")
    _fixture_git(root, "tag", "-d", "v9.9.9")
    (root / "CHANGES").write_text("second\n", encoding="utf-8")
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "second")
    result = _release(root, "v9.9.9", "--check", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert f"is on origin at {first[:7]}, and HEAD is" in result.stderr, result.stderr
    assert "git checkout v9.9.9 && bash macos/scripts/release.sh v9.9.9 --skip-build" in result.stderr
    assert _remote_tags(remote) == ["v9.9.9"] and _tags(root) == []


def test_release_refuses_when_origin_is_unreachable(tmp_path: Path) -> None:
    """Offline, an empty ls-remote answer used to read as 'tag is free'; the
    preflight must fail instead of tagging and building for an hour."""
    root = _fixture(tmp_path)
    _fixture_git(root, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    for mode in (("--check",), ()):
        result = _release(root, "v9.9.9", *mode, gh=_stub_gh(tmp_path))
        assert result.returncode != 0, f"{mode}: {result.stdout}"
        assert "cannot reach origin" in result.stderr, result.stderr
        assert "does not appear to be a git repository" in result.stderr, "git's own reason must be shown"
        assert _tags(root) == []


def test_release_resumes_over_a_leftover_draft_release(tmp_path: Path) -> None:
    """`gh release create` uploads into a draft and publishes last; when the
    upload dies it may fail to delete the draft too. `gh release view` finds
    drafts, so a draft must read as 'no release yet' and be deleted before the
    publish — otherwise the resume path refuses forever in exactly the case it
    exists for."""
    root, remote, gh, log = _publishing_fixture(tmp_path)
    failed = _release(root, "v9.9.9", gh=gh, FIXTURE_GH_LOG=str(log), FIXTURE_GH_CREATE_RC="1")
    assert failed.returncode != 0 and _remote_tags(remote) == ["v9.9.9"]
    resumed = _release(root, "v9.9.9", "--skip-build", gh=gh, FIXTURE_GH_LOG=str(log), FIXTURE_GH_RELEASE_VIEW="draft")
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "DRAFT release v9.9.9" in resumed.stdout and "Release v9.9.9 published" in resumed.stdout
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("release ")]
    assert calls[-2].startswith("release delete v9.9.9 --yes"), calls
    assert calls[-1].startswith("release create v9.9.9"), calls


def test_release_refuses_a_second_release_tag_at_head(tmp_path: Path) -> None:
    """Two v[0-9]* tags at HEAD: `git describe` picks by tagger date, so the
    build could be stamped with the other one and fail an hour later."""
    root = _fixture(tmp_path)
    _fixture_git(root, "tag", "v9.9.8")
    result = _release(root, "v9.9.9", "--check", gh=_stub_gh(tmp_path))
    assert result.returncode != 0
    assert "also carries release tag(s) v9.9.8" in result.stderr, result.stderr
    assert _tags(root) == ["v9.9.8"]
    # The version being released, already at HEAD, is not a stray.
    _fixture_git(root, "tag", "-d", "v9.9.8")
    _fixture_git(root, "tag", "v9.9.9")
    assert _release(root, "v9.9.9", "--check", gh=_stub_gh(tmp_path)).returncode == 0


@NEEDS_DITTO
def test_release_zips_both_apps_when_there_is_no_dmg(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _add_origin(root)
    _fixture_git(root, "push", "-q", "-u", "origin", "HEAD")
    _fake_output(root)
    gh, log = _stub_gh(tmp_path), tmp_path / "gh.log"
    result = _release(root, "v9.9.9", gh=gh, FIXTURE_GH_LOG=str(log))
    assert result.returncode == 0, result.stderr + result.stdout
    (create,) = _creates(log)
    assert "HarborClerkServer-v9.9.9.zip" in create and "HarborClerk-v9.9.9.zip" in create and ".dmg" not in create
