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
    exact = _git("describe", "--tags", "--match", "v*", "--exact-match", "HEAD")
    if exact.returncode != 0:
        nearest = _git("describe", "--tags", "--match", "v*", "--long", "--always").stdout.strip() or "no tag reachable"
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


def test_build_version_rejects_what_it_cannot_parse() -> None:
    result = _derive("release-candidate")
    assert result.returncode != 0, "an unparseable describe must fail the build, not stamp something"
    assert result.stderr.strip()


def test_build_version_describes_this_checkout_through_the_same_parser() -> None:
    """The no-argument path runs `git describe` itself; its answer must equal the
    fixed-input path fed the same string, and must be a usable version."""
    describe = _git("describe", "--tags", "--match", "v*", "--long", "--dirty", "--always").stdout.strip()
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
    recipes = [line for line in (MACOS / "Makefile").read_text(encoding="utf-8").splitlines() if line.startswith("\t")]
    assert not [r for r in recipes if "build-hash.txt" in r], "the Makefile writes build-hash.txt; package.sh owns it"
    assert any("build-version.sh" in r and "xcodebuild" in r for r in recipes) or any(
        "build-version.sh" in r for r in recipes
    ), "make dev no longer derives the version"


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
    _fixture_git(root, "init", "-q")
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "fixture")
    return root


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


def _release(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
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
