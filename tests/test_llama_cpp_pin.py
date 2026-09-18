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
    assert _ensure(clone, "v0.4.1", upstream).returncode == 0
    assert (clone / "VERSION").read_text() == "v0.4.1", "a modified checkout is not the pinned release"


def test_the_helper_removes_nothing_it_did_not_clone(tmp_path: Path, upstream: str) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "precious").write_text("x")
    refused = _ensure(other, "v0.4.1", upstream)
    assert refused.returncode == 1 and "not touching it" in refused.stderr and (other / "precious").exists()

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
        'if [ "$1" = "--build" ]; then mkdir -p build/bin; touch build/bin/libllama.dylib\n'
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
    r = subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)
    if failure is None:
        assert r.returncode == 0, r.stderr
        assert f"llama-server built at v0.4.1 ({commit})" in r.stdout
    else:
        assert r.returncode == 1 and failure in r.stderr, r.stderr
        if "would not load" in failure:
            assert HOMEBREW_SSL in r.stderr


def test_the_build_does_not_link_whatever_openssl_the_build_machine_has() -> None:
    assert "-DLLAMA_OPENSSL=OFF" in _code(BUILD)
