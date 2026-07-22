"""Every AGENTS.md must have a CLAUDE.md symlink beside it, and vice versa.

`AGENTS.md` is the canonical instruction file — it is what Codex and most
harnesses read. Claude Code reads only `CLAUDE.md`, so each directory carries a
symlink of that name pointing at its sibling. One source of truth, two names.

Without this test a new scoped instruction file ships half-wired: readable by
one of the two harnesses actually used on this project, silently invisible to
the other. That failure is undetectable at runtime — the agent simply doesn't
know a rule exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
IGNORED = {"node_modules", ".git", "build", "dist", ".venv", "__pycache__"}


def _instruction_dirs() -> list[Path]:
    """Directories containing either instruction filename."""
    found: set[Path] = set()
    for name in ("AGENTS.md", "CLAUDE.md"):
        for path in REPO_ROOT.rglob(name):
            if IGNORED & set(path.relative_to(REPO_ROOT).parts):
                continue
            found.add(path.parent)
    return sorted(found)


def test_instruction_dirs_are_discovered() -> None:
    dirs = _instruction_dirs()
    assert dirs, "no instruction files found at all — the search is broken"
    assert REPO_ROOT in dirs, "the repo root must carry instruction files"


@pytest.mark.parametrize("directory", _instruction_dirs(), ids=lambda p: str(p.relative_to(REPO_ROOT)) or ".")
def test_agents_md_is_real_and_claude_md_symlinks_to_it(directory: Path) -> None:
    agents = directory / "AGENTS.md"
    claude = directory / "CLAUDE.md"
    rel = directory.relative_to(REPO_ROOT)

    assert agents.exists(), f"{rel}/CLAUDE.md exists without a sibling AGENTS.md — Codex cannot see these rules"
    assert not agents.is_symlink(), f"{rel}/AGENTS.md must be the real file, not a symlink"

    assert claude.exists(), f"{rel}/AGENTS.md exists without a sibling CLAUDE.md — Claude Code cannot see these rules"
    assert claude.is_symlink(), f"{rel}/CLAUDE.md must be a symlink to AGENTS.md, not a copy that can drift"

    target = claude.readlink()
    assert str(target) == "AGENTS.md", f"{rel}/CLAUDE.md points at {target}, expected the sibling AGENTS.md"
    assert claude.read_text() == agents.read_text(), f"{rel}: symlink does not resolve to its sibling's content"


def test_instruction_files_stay_short() -> None:
    """Adherence degrades past roughly 200 lines per file."""
    too_long = {}
    for directory in _instruction_dirs():
        agents = directory / "AGENTS.md"
        if not agents.exists():
            continue
        lines = len(agents.read_text().splitlines())
        if lines > 200:
            too_long[str(directory.relative_to(REPO_ROOT)) or "."] = lines
    assert not too_long, f"instruction files over 200 lines (adherence degrades): {too_long}"
