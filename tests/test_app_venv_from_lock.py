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


def _pip_installs(script: Path) -> list[list[str]]:
    return [argv for argv in _commands(script) if "pip" in argv and "install" in argv[argv.index("pip") :]]


def test_the_lock_is_exported_frozen_and_with_hashes() -> None:
    exports = [argv for argv in _commands(BUILD_VENV) if "uv" in argv and "export" in argv]
    assert len(exports) == 1, exports
    export = exports[0]
    assert "--frozen" in export, "without --frozen, uv re-resolves and the app ships a lock nobody reviewed"
    assert "--no-dev" in export and "--no-emit-project" in export
    assert "--no-hashes" not in export


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
    assert ".lock-sha256" in code and "rm -rf $VENV_DIR" in code
    checks = [argv for argv in _commands(BUILD_VENV) if "pip" in argv and "check" in argv[argv.index("pip") :]]
    assert len(checks) == 1, "pip check is where a requirement the root lock cannot meet would show"
