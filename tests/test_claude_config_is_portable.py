"""Tracked files under `.claude/` must work on any clone, and the hooks must do
what they say.

`.claude/` was gitignored until ADR 0001, so every skill and hook hardcoded
`/Users/alex/mcp-gateway` and nobody noticed: the only machine that ran them
was the one they were written on. They are tracked now so a fresh clone and
the second Mac get the same tools, which holds only while nothing in them
names a path that exists on one machine.

The ignore rules are checked with `git check-ignore --no-index`, not by
reading the index: once a file is committed, `git ls-files` lists it whatever
`.gitignore` says, so an index-based assertion passed with the negation
deleted. The hooks are run the way Claude Code runs them, with a JSON
`tool_input` on stdin and the project venv *not* on PATH, because a hook that
silently exits 0 when `jq`, `python3`, `ruff` or `uv` is missing looks
identical to one that works.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.json"


def _tracked(prefix: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", prefix], check=True, capture_output=True, text=True
    ).stdout
    return set(out.splitlines())


def _ignored(rel: str) -> bool:
    """What `.gitignore` says about `rel`, independent of the index."""
    result = subprocess.run(["git", "-C", str(REPO), "check-ignore", "--no-index", "-q", rel], capture_output=True)
    assert result.returncode in (0, 1), result.stderr.decode()
    return result.returncode == 0


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/settings.json",
        ".claude/launch.json",
        ".claude/skills/verify/SKILL.md",
        # Not present yet: the rules must be a denylist so new material under
        # `.claude/` is tracked by default rather than silently dropped.
        ".claude/commands/example.md",
        ".claude/agents/example.md",
        ".claude/hooks/example.sh",
    ],
)
def test_claude_material_is_not_ignored(rel: str) -> None:
    assert not _ignored(rel), (
        f"{rel} is ignored by .gitignore; the .claude rules must be a denylist of per-machine state"
    )


def test_claude_config_is_tracked() -> None:
    tracked = _tracked(".claude")
    for required in (".claude/settings.json", ".claude/launch.json", ".claude/skills/verify/SKILL.md"):
        assert required in tracked, f"{required} is not in the index"
    assert ".claude/settings.local.json" not in tracked, "per-machine permission grants are in the index"


@pytest.mark.parametrize(
    "rel",
    [".claude/settings.local.json", ".claude/worktrees/some-branch/AGENTS.md", ".claude/scheduled_tasks.lock"],
)
def test_per_machine_state_is_ignored(rel: str) -> None:
    assert _ignored(rel), f"{rel} is per-machine state and must be ignored"


@pytest.mark.parametrize("rel", sorted(_tracked(".claude")))
def test_no_machine_specific_paths(rel: str) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    hits = re.findall(r"(?:/Users/|/home/)[\w.-]+", text)
    assert not hits, f"{rel} names a machine-specific path: {sorted(set(hits))}"


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
    """`AGENTS.md` tells agents to run skills by name; each must be present.

    Matches the phrasing "the `x` skill" and the Map row "Skills: `a`, `b`".
    """
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"the `([a-z0-9-]+)` skill", text))
    for row in re.findall(r"^\| Skills: ([^|]+)\|", text, re.MULTILINE):
        named.update(re.findall(r"`([a-z0-9-]+)`", row))
    assert named, "AGENTS.md no longer names any skill; update this test's patterns or the file"
    missing = sorted(n for n in named if not (REPO / ".claude" / "skills" / n / "SKILL.md").exists())
    assert not missing, f"AGENTS.md names skills that do not exist: {missing}"


def test_adrs_are_well_named_and_numbered_without_gaps() -> None:
    adr_dir = REPO / "docs" / "adr"
    files = sorted(p.name for p in adr_dir.glob("*.md") if p.name != "README.md")
    assert files, "docs/adr/ has no ADRs"
    pattern = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
    bad = [f for f in files if not pattern.match(f)]
    assert not bad, f"ADR files must be named NNNN-kebab-slug.md: {bad}"
    numbers = [int(pattern.match(f).group(1)) for f in files]
    assert numbers == list(range(1, len(numbers) + 1)), f"ADR numbers must run 0001..N without gaps: {numbers}"


# ── Hooks, run as Claude Code runs them ─────────────────────────────────────


def _hook_commands(event: str) -> list[str]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands: list[str] = []
    for entry in settings["hooks"][event]:
        if entry.get("matcher") in ("Edit|Write", "Write|Edit"):
            commands.extend(h["command"] for h in entry["hooks"] if h.get("type") == "command")
    assert commands, f"no {event} command hooks for Edit|Write in {SETTINGS}"
    return commands


def _guard() -> str:
    (guard,) = _hook_commands("PreToolUse")
    return guard


def _ruff_hook() -> str:
    (hook,) = [c for c in _hook_commands("PostToolUse") if "ruff" in c]
    return hook


def _hook_env(*, project_dir: bool = True, shadow: Path | None = None) -> dict[str, str]:
    """The environment Claude Code runs hooks in: the user's shell, where the
    project venv is *not* activated. Under `uv run pytest`, `.venv/bin` is on
    PATH, so a bare `ruff` resolves here and nowhere else; drop exactly that
    directory so the hook must go through `uv`, which stays on PATH. `shadow`
    is prepended to PATH to stand in for a missing tool."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    venv_bin = (REPO / ".venv" / "bin").resolve()
    path = [d for d in env.get("PATH", "").split(os.pathsep) if d and Path(d).resolve() != venv_bin]
    if shadow is not None:
        path.insert(0, str(shadow))
    env["PATH"] = os.pathsep.join(path)
    assert shutil.which("uv", path=env["PATH"]), "uv must be on PATH for these tests (it is what the hooks call)"
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = str(REPO)
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _shadow(tmp_path: Path, name: str) -> Path:
    """A PATH directory whose `name` is a stub that fails like a missing tool."""
    d = tmp_path / "shadow-bin"
    d.mkdir(exist_ok=True)
    stub = d / name
    stub.write_text("#!/bin/sh\nexit 127\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def _run(command: str, payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", command], input=payload, capture_output=True, text=True, env=env, timeout=120)


def _payload(file_path: str | Path) -> str:
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}})


@pytest.mark.parametrize(
    ("rel", "blocked"),
    [
        (".env", True),
        (".env.local", True),
        ("uv.lock", True),
        ("embedder/uv.lock", True),
        (".env.example", False),  # checked-in template with placeholder values
        ("src/harbor_clerk/secrets/cipher.py", False),  # a source package, not secret material
        ("tests/secrets/test_cipher.py", False),
        ("docs/secrets-and-keys.md", False),
        ("README.md", False),
    ],
)
def test_protected_file_guard(rel: str, blocked: bool) -> None:
    result = _run(_guard(), _payload(REPO / rel), _hook_env())
    if blocked:
        assert result.returncode == 2, f"{rel} should be blocked (exit 2), got {result.returncode}: {result.stderr}"
        # Claude Code shows the exit-2 reason from stderr only; stdout is dropped.
        assert result.stderr.strip(), f"{rel} was blocked without a reason on stderr"
    else:
        assert result.returncode == 0, f"{rel} should be allowed (exit 0), got {result.returncode}: {result.stderr}"


def test_guard_blocks_without_project_dir_and_with_relative_paths() -> None:
    """The anchoring must not depend on `CLAUDE_PROJECT_DIR` being set or on
    the path being absolute."""
    assert _run(_guard(), _payload(REPO / ".env"), _hook_env(project_dir=False)).returncode == 2
    assert _run(_guard(), _payload(REPO / "README.md"), _hook_env(project_dir=False)).returncode == 0
    assert _run(_guard(), _payload(".env"), _hook_env()).returncode == 2
    assert _run(_guard(), _payload("README.md"), _hook_env()).returncode == 0


def test_guard_allows_when_payload_has_no_file_path() -> None:
    result = _run(_guard(), json.dumps({"tool_name": "Edit", "tool_input": {}}), _hook_env())
    assert result.returncode == 0, result.stderr


def test_guard_fails_closed_when_parser_is_missing(tmp_path: Path) -> None:
    """A guard that cannot read its input must refuse, not allow. The previous
    `jq` version exited 0 with an empty FILE when jq was absent."""
    result = _run(_guard(), _payload(REPO / "README.md"), _hook_env(shadow=_shadow(tmp_path, "python3")))
    assert result.returncode == 2, f"guard allowed an edit it could not inspect: exit {result.returncode}"
    assert result.stderr.strip(), "refusal without a reason on stderr"


def test_guard_fails_closed_on_malformed_payload() -> None:
    result = _run(_guard(), "not json {", _hook_env())
    assert result.returncode == 2, f"guard allowed an edit on an unparseable payload: exit {result.returncode}"
    assert result.stderr.strip()


def test_ruff_hook_actually_formats(tmp_path: Path) -> None:
    """A bare `ruff` is not on PATH on a machine that only has `uv`; the hook
    swallowed that with `|| true` and formatted nothing."""
    target = tmp_path / "unformatted.py"
    target.write_text("x = { 'a':1 ,'b':2 }\n", encoding="utf-8")
    result = _run(_ruff_hook(), _payload(target), _hook_env())
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == 'x = {"a": 1, "b": 2}\n', "the ruff hook did not format the file"


def test_ruff_hook_reports_failure_instead_of_swallowing_it(tmp_path: Path) -> None:
    target = tmp_path / "unformatted.py"
    target.write_text("x = { 'a':1 }\n", encoding="utf-8")
    result = _run(_ruff_hook(), _payload(target), _hook_env(shadow=_shadow(tmp_path, "uv")))
    assert result.returncode == 2, f"ruff hook exited {result.returncode} with uv missing; it must say so, not exit 0"
    assert result.stderr.strip(), "failure without a reason on stderr"
    assert target.read_text(encoding="utf-8") == "x = { 'a':1 }\n"


def test_ruff_hook_never_touches_the_lockfile() -> None:
    """`uv run` performs an implicit lock and sync, so with `pyproject.toml`
    one dependency ahead of `uv.lock` a *formatter* hook would rewrite the
    lockfile the PreToolUse guard exists to protect, with all output sent to
    /dev/null. Reproducing that needs a stale lock and network, so this checks
    the flags rather than the behaviour: `--frozen` never updates the lock,
    `--no-sync` never installs anything from inside a hook."""
    hook = _ruff_hook()
    assert "--frozen" in hook, "the ruff hook must run `uv run --frozen ...` so it can never rewrite uv.lock"
    assert "--no-sync" in hook, "the ruff hook must run `uv run --no-sync ...`; a formatter never installs packages"
