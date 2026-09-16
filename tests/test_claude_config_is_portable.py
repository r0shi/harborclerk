"""Tracked files under `.claude/` must work on any clone.

`.claude/` was gitignored until ADR 0001, so every skill and hook hardcoded
`/Users/alex/mcp-gateway` and nobody noticed: the only machine that ran them
was the one they were written on. They are tracked now so a fresh clone and
the second Mac get the same tools, which holds only while nothing in them
names a path that exists on one machine.

The same file guards the two conventions the ADR introduced beside it: the
ADR sequence has no gaps, and a skill that `AGENTS.md` tells an agent to run
actually exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _tracked(prefix: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", prefix],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line]


def test_claude_config_is_tracked() -> None:
    """The `.gitignore` negations must actually let the files through."""
    tracked = {p.relative_to(REPO).as_posix() for p in _tracked(".claude")}
    for required in (".claude/settings.json", ".claude/launch.json", ".claude/skills/verify/SKILL.md"):
        assert required in tracked, f"{required} is not tracked; check the `.claude/*` negations in .gitignore"


def test_local_permission_grants_are_not_tracked() -> None:
    tracked = {p.relative_to(REPO).as_posix() for p in _tracked(".claude")}
    assert ".claude/settings.local.json" not in tracked, "settings.local.json is per-machine and must stay ignored"
    assert not any(t.startswith(".claude/worktrees/") for t in tracked), "worktrees must stay ignored"


@pytest.mark.parametrize("path", _tracked(".claude"), ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_machine_specific_paths(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = re.findall(r"(?:/Users/|/home/)[\w.-]+", text)
    assert not hits, f"{path.relative_to(REPO)} names a machine-specific path: {sorted(set(hits))}"


def test_every_skill_directory_has_a_matching_skill_md() -> None:
    skills_root = REPO / ".claude" / "skills"
    dirs = sorted(d for d in skills_root.iterdir() if d.is_dir())
    assert dirs, "no skills found; the directory should not be empty"
    for d in dirs:
        skill = d / "SKILL.md"
        assert skill.exists(), f"{d.relative_to(REPO)} has no SKILL.md"
        m = re.search(r"^name:\s*(\S+)\s*$", skill.read_text(encoding="utf-8"), re.MULTILINE)
        assert m, f"{skill.relative_to(REPO)} has no `name:` in its frontmatter"
        assert m.group(1) == d.name, f"{skill.relative_to(REPO)} is named {m.group(1)!r} but lives in {d.name!r}"


def test_skills_named_in_agents_md_exist() -> None:
    """`AGENTS.md` tells agents to run skills by name; each must be present."""
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"the `([a-z0-9-]+)` skill", text))
    assert named, "AGENTS.md no longer names any skill; update this test's pattern or the file"
    missing = sorted(n for n in named if not (REPO / ".claude" / "skills" / n / "SKILL.md").exists())
    assert not missing, f"AGENTS.md names skills that do not exist: {missing}"


def test_adrs_are_numbered_without_gaps() -> None:
    adr_dir = REPO / "docs" / "adr"
    numbers = sorted(
        int(m.group(1)) for p in adr_dir.glob("*.md") if (m := re.match(r"(\d{4})-[a-z0-9-]+\.md$", p.name))
    )
    assert numbers, "docs/adr/ has no numbered ADRs"
    assert numbers == list(range(1, len(numbers) + 1)), f"ADR numbers must run 0001..N without gaps: {numbers}"
