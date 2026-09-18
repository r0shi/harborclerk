"""One llama.cpp for both deployments, and a build that cannot ship another.

macOS builds llama-server from a pinned tag; Docker Compose pulls an image.
They drifted for months (a pinned `b9018` beside a floating `:server`), and
the build script reused whatever clone an older pin had left behind.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.model_survey.llamacpp import pinned_tag

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "macos" / "scripts"
ENSURE = SCRIPTS / "ensure-llama-source.sh"
BUILD = SCRIPTS / "build-llama.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def test_compose_runs_the_release_macos_builds() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["llama-server"]["image"] == f"ghcr.io/ggml-org/llama.cpp:server-{pinned_tag()}"


def test_the_pin_is_a_stable_release_not_a_rolling_build() -> None:
    assert re.fullmatch(r"v\d+\.\d+\.\d+", pinned_tag()), pinned_tag()


def _code(script: Path) -> str:
    """The script without its comments, so a guard cannot be satisfied by one."""
    return "\n".join(
        line for line in script.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )


def test_the_build_script_gets_its_source_from_the_helper_and_nowhere_else() -> None:
    code = _code(BUILD)
    assert "ensure-llama-source.sh" in code and "git clone" not in code


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> str:
    """A stand-in for llama.cpp with two releases."""
    src = tmp_path / "upstream"
    src.mkdir()
    _git(src, "init", "-q", "-b", "master")
    (src / ".gitignore").write_text("/build*\n")  # as upstream's .gitignore does
    _git(src, "add", ".gitignore")
    for tag in ("v0.4.0", "v0.4.1"):
        (src / "VERSION").write_text(tag)
        _git(src, "add", "VERSION")
        _git(src, "commit", "-q", "-m", tag)
        _git(src, "tag", tag)
    return f"file://{src}"


def _ensure(clone: Path, tag: str, repo: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV, "LLAMA_CPP_REPO": repo}
    return subprocess.run(["bash", str(ENSURE), str(clone), tag], env=env, capture_output=True, text=True)


def test_a_clone_left_by_an_older_pin_is_replaced_and_a_current_one_is_kept(tmp_path: Path, upstream: str) -> None:
    clone = tmp_path / "build" / "llama.cpp"
    clone.parent.mkdir()
    assert _ensure(clone, "v0.4.0", upstream).returncode == 0
    assert (clone / "VERSION").read_text() == "v0.4.0"
    (clone / "build").mkdir()
    (clone / "build" / "cache").write_text("an old build tree")

    bumped = _ensure(clone, "v0.4.1", upstream)
    assert bumped.returncode == 0, bumped.stderr
    assert "existing clone is at v0.4.0" in bumped.stdout
    assert (clone / "VERSION").read_text() == "v0.4.1" and not (clone / "build").exists(), (
        "the old version's build tree went too"
    )

    (clone / "build").mkdir()
    (clone / "build" / "cache").write_text("this version's build tree")
    again = _ensure(clone, "v0.4.1", upstream)
    assert again.returncode == 0 and "using existing clone at v0.4.1" in again.stdout
    assert (clone / "build" / "cache").read_text() == "this version's build tree", "an incremental build must survive"

    (clone / "VERSION").write_text("patched by hand")
    refused = _ensure(clone, "v0.4.1", upstream)
    assert refused.returncode == 1 and "modified files: yes" in refused.stderr
    assert (clone / "VERSION").read_text() == "patched by hand", "a modified checkout may be someone's work"


def test_a_full_clone_at_the_older_tag_is_replaced_even_though_it_knows_the_newer_one(
    tmp_path: Path, upstream: str
) -> None:
    """A shallow clone of v0.4.0 has no v0.4.1 ref, so the tag check alone
    decides it. A full clone has both tags and only the commit comparison
    tells them apart; a mutation that dropped it passed every test."""
    clone = tmp_path / "llama.cpp"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "v0.4.0", upstream, str(clone)], env={**os.environ, **GIT_ENV}, check=True
    )
    assert _git(clone, "rev-parse", "-q", "--verify", "refs/tags/v0.4.1^{commit}")
    bumped = _ensure(clone, "v0.4.1", upstream)
    assert bumped.returncode == 0, bumped.stderr
    assert "existing clone is at v0.4.0" in bumped.stdout and (clone / "VERSION").read_text() == "v0.4.1"


def test_the_helper_removes_nothing_it_did_not_clone(tmp_path: Path, upstream: str) -> None:
    """The old script never deleted anything. This one deletes only what it
    made: a person's clone of llama.cpp, with a branch and unpushed work, sat
    where BUILD_DIR pointed and would have been removed on an origin-URL check."""
    mine = tmp_path / "mine"
    subprocess.run(["git", "clone", "-q", upstream, str(mine)], env={**os.environ, **GIT_ENV}, check=True)
    _git(mine, "checkout", "-q", "-b", "my-patch")
    (mine / "patch.c").write_text("unpushed work")
    _git(mine, "add", "patch.c")
    _git(mine, "commit", "-q", "-m", "wip")
    refused = _ensure(mine, "v0.4.1", upstream)
    assert refused.returncode == 1 and "my-patch" in refused.stderr and "rm -rf" in refused.stderr
    assert (mine / "patch.c").exists() and _git(mine, "rev-parse", "--abbrev-ref", "HEAD") == "my-patch"

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "precious").write_text("x")
    refused = _ensure(other, "v0.4.1", upstream)
    assert refused.returncode == 1 and "not touching it" in refused.stderr and (other / "precious").exists()

    # A clean, detached clone at a tag looks exactly like this script's own output, except it is of another repository.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "master")
    (elsewhere / "precious").write_text("y")
    _git(elsewhere, "add", "precious")
    _git(elsewhere, "commit", "-q", "-m", "v0.4.1")
    _git(elsewhere, "tag", "v0.4.1")
    foreign = tmp_path / "foreign"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "v0.4.1", f"file://{elsewhere}", str(foreign)],
        env={**os.environ, **GIT_ENV},
        check=True,
    )
    refused = _ensure(foreign, "v0.4.1", upstream)
    assert refused.returncode == 1 and "not touching it" in refused.stderr and (foreign / "precious").read_text() == "y"

    # The helper's own output, plus a local branch someone made in it: detached HEAD, so only the branch rule says no.
    own = tmp_path / "own"
    assert _ensure(own, "v0.4.0", upstream).returncode == 0
    _git(own, "branch", "my-wip")
    refused = _ensure(own, "v0.4.1", upstream)
    assert refused.returncode == 1 and "my-wip" in refused.stderr and (own / "VERSION").read_text() == "v0.4.0"

    branch = _ensure(tmp_path / "from-branch", "master", upstream)
    assert branch.returncode == 1 and "is a branch, not a tag" in branch.stderr
    assert not (tmp_path / "from-branch").exists(), (
        "its own clone of a moment ago; left behind, the next run refuses it"
    )

    # Un-added work in a clone that otherwise looks like the helper's own is still work.
    stash = tmp_path / "stash"
    assert _ensure(stash, "v0.4.0", upstream).returncode == 0
    (stash / "experiment.patch").write_text("not yet added")
    refused = _ensure(stash, "v0.4.1", upstream)
    assert refused.returncode == 1 and (stash / "experiment.patch").exists()

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "precious").write_text("x")
    refused = _ensure(plain, "v0.4.1", upstream)
    assert refused.returncode == 1 and "not a git clone" in refused.stderr and (plain / "precious").exists()

    missing_tag = _ensure(tmp_path / "fresh", "v9.9.9", upstream)
    assert missing_tag.returncode != 0


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


HOMEBREW_SSL = "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib"


@pytest.mark.parametrize(
    ("reported", "links", "failure"),
    [
        ("real", "@rpath/libllama.0.dylib /usr/lib/libc++.1.dylib", None),
        ("0000000", "@rpath/libllama.0.dylib", "does not report commit"),
        ("real", f"@rpath/libllama.0.dylib {HOMEBREW_SSL}", "would not load on a Mac without this machine's libraries"),
    ],
)
def test_the_build_fails_on_the_wrong_commit_and_on_a_library_the_bundle_does_not_carry(
    tmp_path: Path, upstream: str, reported: str, links: str, failure: str | None
) -> None:
    """cmake, otool, codesign and the rest are stubs: the stub "builds" a
    llama-server that prints the commit, and links what, the test tells it to.
    Homebrew's OpenSSL shipped this way for months: both build machines have it."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(
        stubs,
        "cmake",
        'if [ "$1" = "--build" ]; then mkdir -p build/bin; touch build/bin/libllama.0.4.1.dylib\n'
        "ln -sf libllama.0.4.1.dylib build/bin/libllama.0.dylib; ln -sf libllama.0.dylib build/bin/libllama.dylib\n"
        'printf \'#!/usr/bin/env bash\\necho "version: 1 (%s)"\\n\' "$FAKE_COMMIT" > build/bin/llama-server\n'
        "chmod +x build/bin/llama-server; fi\n",
    )
    for name in ("install_name_tool", "codesign"):
        _stub(stubs, name, "exit 0\n")
    _stub(stubs, "sysctl", "echo 4\n")
    # `otool -D` prints the file and its install name; `otool -L` the file, then one dependency per line.
    _stub(
        stubs,
        "otool",
        'echo "$2:"\n'
        'if [ "$1" = "-D" ]; then case "$2" in *.dylib) echo "@rpath/$(basename "$2")";; esac; exit 0; fi\n'
        'case "$2" in *.dylib) echo "\t@rpath/$(basename "$2") (compatibility version 0.0.0)";; esac\n'
        'for l in $FAKE_LINKS; do echo "\t$l (compatibility version 1.0.0)"; done\n',
    )

    commit = _git(Path(upstream.removeprefix("file://")), "rev-parse", "--short=7", "v0.4.1")
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "LLAMA_CPP_REPO": upstream,
        "LLAMA_CPP_TAG": "v0.4.1",
        "DEST_DIR": str(tmp_path / "dest"),
        "BUILD_DIR": str(tmp_path / "work"),
        "FAKE_COMMIT": commit if reported == "real" else reported,
        "FAKE_LINKS": links,
    }
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "libllama.0.0.1.dylib").write_text("left by the previous version, which named its dylibs differently")
    r = subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)
    if failure is None:
        assert r.returncode == 0, r.stderr
        assert f"llama-server built at v0.4.1 ({commit})" in r.stdout
        assert not (dest / "libllama.0.0.1.dylib").exists(), "a stale dylib would have shipped in the bundle"
        assert (dest / "libllama.dylib").is_symlink() and (
            dest / "libllama.dylib"
        ).resolve().name == "libllama.0.4.1.dylib"
    else:
        assert r.returncode == 1 and failure in r.stderr, r.stderr
        if "would not load" in failure:
            assert HOMEBREW_SSL in r.stderr


def test_the_build_does_not_link_whatever_openssl_the_build_machine_has() -> None:
    assert "-DLLAMA_OPENSSL=OFF" in _code(BUILD)
