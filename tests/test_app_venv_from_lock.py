"""The Mac app ships what `uv.lock` pins, not whatever its build venv first received.

`build-venv.sh` used to `pip install --upgrade` the two projects, which leaves every dependency where it was.
The signed build of 2026-09-20 carried torch 2.11.0 against a lock at 2.13.0 (on MPS that torch keeps memory for
every new input shape: 43 GB in six minutes of ingest, #685) and anyio 4.13.0 after the lock had moved past a
critical advisory. CI tests the lock and `dependency-audit` audits it, so neither could see what shipped.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD_VENV = REPO / "macos" / "scripts" / "build-venv.sh"
DOCKERFILES = sorted((REPO / "docker").glob("*.Dockerfile"))


def _commands(script: Path) -> list[list[str]]:
    """The script's commands as argv lists: comments gone, continuation lines joined, so a guard reads what runs."""
    text = re.sub(r"\\\n", " ", script.read_text(encoding="utf-8"))
    lines = [line.split(" #")[0].strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
    out = []
    for line in lines:
        try:
            out.append(shlex.split(line))
        except ValueError:  # a heredoc body or an unbalanced quote: not a command line
            continue
    return [argv for argv in out if argv]


def _is_pip(token: str) -> bool:
    """`pip`, `pip3`, `$VENV_DIR/bin/pip`, `pip3.12`: a path to pip is pip. Review got `"$VENV_DIR/bin/pip"
    install striprtf` past a guard that wanted the bare word."""
    return re.fullmatch(r"pip[\d.]*", token.rsplit("/", 1)[-1]) is not None


def _pip(argv: list[str], subcommand: str) -> bool:
    at = next((i for i, token in enumerate(argv) if _is_pip(token)), None)
    return at is not None and subcommand in argv[at:]


def _pip_installs(script: Path) -> list[list[str]]:
    return [argv for argv in _commands(script) if _pip(argv, "install")]


def test_a_path_to_pip_is_pip() -> None:
    assert all(_is_pip(t) for t in ("pip", "pip3", "pip3.12", "$VENV_DIR/bin/pip", "/usr/bin/pip3"))
    assert not any(_is_pip(t) for t in ("pipx", "--disable-pip-version-check", "ensurepip", "$PIP_FLAGS"))
    assert _pip(["$VENV_DIR/bin/pip", "install", "striprtf"], "install")


def test_the_lock_is_exported_frozen_and_with_hashes() -> None:
    exports = [argv for argv in _commands(BUILD_VENV) if "uv" in argv and "export" in argv]
    assert len(exports) == 1, exports
    export = exports[0]
    assert "--frozen" in export, "without --frozen, uv re-resolves and the app ships a lock nobody reviewed"
    assert "--no-dev" in export and "--no-emit-project" in export
    assert "--no-hashes" not in export
    assert "--no-header" in export, "the header records the output path: the hash would follow the build directory"
    # The root lock decides (one venv, and it is the superset), and only what the app runs on is exported: an
    # extra or a group would ship pytest and ruff inside the app.
    assert export[: export.index("uv")] == ["(cd", "$PROJECT_ROOT", "&&"], export
    assert not [a for a in export if a.startswith(("--extra", "--all-extras", "--group", "--all-groups", "--only"))]


def test_dependencies_are_installed_from_that_export_and_only_with_hashes() -> None:
    from_lock = [argv for argv in _pip_installs(BUILD_VENV) if "-r" in argv]
    assert len(from_lock) == 1, from_lock
    assert "--require-hashes" in from_lock[0]
    assert from_lock[0][from_lock[0].index("-r") + 1] == "$REQUIREMENTS"


def test_nothing_else_is_installed_with_dependency_resolution() -> None:
    """Every other `pip install` is a local project with --no-deps. A bare `pip install <name>` is how an
    unpinned package ships: `striprtf` did for years, imported by nothing."""
    others = [argv for argv in _pip_installs(BUILD_VENV) if "-r" not in argv]
    assert others, "the local packages are installed somewhere"
    for argv in others:
        assert "--no-deps" in argv, argv
        targets = [a for a in argv[argv.index("install") + 1 :] if not a.startswith("-")]
        assert targets and all(t.startswith("$PROJECT_ROOT") for t in targets), argv


def test_a_venv_from_another_lock_is_rebuilt_and_the_result_is_checked() -> None:
    code = "\n".join(" ".join(argv) for argv in _commands(BUILD_VENV))
    gate = [line for line in code.splitlines() if line.startswith("if ") and ".lock-sha256" in line]
    # Whole, not by substring: `... && false` appended to it got past a substring check in review.
    assert gate == [
        "if [ ! -x $VENV_DIR/bin/python3 ] || [ $(cat $VENV_DIR/.lock-sha256 2>/dev/null) != $LOCK_SHA ]; then"
    ]
    after = code[code.index(gate[0]) :]
    assert after.index("rm -rf $VENV_DIR") < after.index("fi"), "and the rebuild must sit inside that branch"
    assert code.index("echo $LOCK_SHA >") > code.index("pip check"), "a failed install must not be recorded as built"
    checks = [argv for argv in _commands(BUILD_VENV) if _pip(argv, "check")]
    assert len(checks) == 1, "pip check is where a requirement the root lock cannot meet would show"
    assert not {"||", "|", ";", "&&"} & set(checks[0]), "and its failure must stop the build, not be waved through"


def _run_steps(dockerfile: Path) -> list[str]:
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    return [line for line in text.splitlines() if line.startswith("RUN ")]


def test_every_image_installs_its_project_from_a_lock() -> None:
    """The reranker image ran `pip install /app/embedder`, which resolves afresh on every build."""
    assert {d.name for d in DOCKERFILES} >= {"app.Dockerfile", "embedder.Dockerfile", "reranker.Dockerfile"}
    for dockerfile in DOCKERFILES:
        steps = _run_steps(dockerfile)
        assert any("uv sync --locked" in step for step in steps), dockerfile.name
        for step in steps:
            for command in re.split(r"&&|;", step.removeprefix("RUN ")):
                argv = command.split()
                if "pip" in argv and "install" in argv:
                    rest = argv[argv.index("install") + 1 :]
                    # `--python <interpreter>` takes a value, and that value is a path without being a target.
                    targets = [
                        a for i, a in enumerate(rest) if not a.startswith("-") and rest[i - 1 : i] != ["--python"]
                    ]
                    assert not any(t.startswith(("/", ".")) for t in targets), f"{dockerfile.name}: {command.strip()}"


def test_a_service_in_a_venv_is_on_the_path_it_is_started_from() -> None:
    """The reranker's CMD, its HEALTHCHECK and compose's healthcheck all find Python through this one line."""
    for dockerfile, venv in (("reranker.Dockerfile", "/app/embedder/.venv"), ("embedder.Dockerfile", "/app/.venv")):
        text = (REPO / "docker" / dockerfile).read_text(encoding="utf-8")
        assert re.search(rf'^ENV PATH="{re.escape(venv)}/bin:\$PATH"$', text, re.M), dockerfile
        assert text.index(f'ENV PATH="{venv}/bin') < text.index("\nCMD "), dockerfile


def _locked(lock: Path) -> dict[str, str]:
    import tomllib

    return {p["name"]: p["version"] for p in tomllib.loads(lock.read_text(encoding="utf-8"))["package"]}


def test_both_locks_agree_on_the_libraries_the_embedder_runs_on() -> None:
    """The Mac app runs the embedder on the ROOT lock (one venv); the embedder's CI job and both images run it on
    `embedder/uv.lock`. Where they differ, what ships on a Mac is a combination no test has run: the two were a
    sentence-transformers minor apart (5.6.0 and 5.2.2) when this was written. A bump to one of these in either
    lock is a bump to both, in one PR."""
    root, embedder = _locked(REPO / "uv.lock"), _locked(REPO / "embedder" / "uv.lock")
    for name in ("torch", "transformers", "sentence-transformers", "tokenizers", "numpy"):
        assert root[name] == embedder[name], f"{name}: root {root[name]}, embedder {embedder[name]}"
