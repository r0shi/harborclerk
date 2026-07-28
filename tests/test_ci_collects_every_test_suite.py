"""Every test directory in the repo must be collected by some CI job.

Three suites have shipped uncollected, each for the same reason — nothing
asserts the link between "tests exist here" and "CI runs them":

  - `embedder/tests` — a separate uv project, invisible to `testpaths =
    ["tests"]`. Its #553 guard (encode must not block the event loop) was
    decorative for as long as it existed; a PR deleting the fix would have
    merged green.
  - `frontend` vitest — `"test": "vitest"` was defined in package.json and run
    by no workflow, so a react-router major upgrade had `tsc --noEmit` as its
    only automated signal.
  - `scripts/test_corpora/tests` — 35 files, ~191 tests, still uncollected. It
    is its own uv project with its own dependencies, so a root-venv run cannot
    even import 19 of them; that is a wiring gap, not rotted code.

The failure mode is silent by construction: a test that never runs cannot fail,
so the suite looks green precisely because the coverage is missing. Same shape
as `test_docker_compose_queues.py` — a queue nobody subscribes to swallows work
without an error.

Adding a suite is fine. Adding one CI does not run is not, and this test is
where you say so: either wire it into a workflow, or record it in
`UNCOLLECTED_WITH_REASON` with a reason and an issue.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

# Directories that legitimately hold no CI-run tests. Each needs a reason and,
# where it is a gap rather than a decision, an issue number.
UNCOLLECTED_WITH_REASON: dict[str, str] = {
    # Its own uv project (scripts/test_corpora/pyproject.toml) with its own
    # dependencies, so it needs a job of its own the way `embedder` does — a
    # root-venv run cannot import 19 of the 35 files. The harness is also
    # destructive by design (it wipes and re-ingests corpora), so the CI shape
    # needs a decision, not just a `run:` line.
    "scripts/test_corpora/tests": "separate uv project, needs its own CI job like embedder — see issue #587",
}

_SKIP_PARTS = {".venv", "node_modules", "site-packages", "build", ".git", ".claude", ".worktrees", "dist"}


def _our_test_dirs() -> set[str]:
    """Directories under the repo that contain `test_*.py` we authored."""
    found: set[str] = set()
    for path in REPO.rglob("test_*.py"):
        rel = path.relative_to(REPO)
        if _SKIP_PARTS & set(rel.parts):
            continue
        found.add(str(rel.parent))
    return found


def _collected_dirs() -> set[str]:
    """Directories some workflow step actually points pytest at.

    Resolves `working-directory` so `pytest tests/` inside `embedder` counts as
    `embedder/tests`, not `tests`.
    """
    collected: set[str] = set()
    for wf in WORKFLOWS.glob("*.yml"):
        doc = yaml.safe_load(wf.read_text()) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "pytest" not in run:
                    continue
                cwd = step.get("working-directory") or job.get("defaults", {}).get("run", {}).get("working-directory")
                for target in re.findall(r"pytest\s+((?:[\w./-]+\s*)+)", run):
                    for token in target.split():
                        if token.startswith("-"):
                            continue
                        rel = f"{cwd.rstrip('/')}/{token}" if cwd else token
                        collected.add(rel.rstrip("/"))
    return collected


def _is_collected(test_dir: str, collected: set[str]) -> bool:
    """A parent target collects its children: `tests/` covers `tests/mail`."""
    return any(test_dir == c or test_dir.startswith(f"{c}/") for c in collected)


def test_every_test_directory_is_collected_by_ci():
    collected = _collected_dirs()
    assert collected, "no pytest invocation found in any workflow — parser is broken, not the repo"

    missing = sorted(
        d for d in _our_test_dirs() if not _is_collected(d, collected) and d not in UNCOLLECTED_WITH_REASON
    )

    assert not missing, (
        "these test directories are not collected by any CI job, so their tests "
        f"cannot fail and their coverage is imaginary: {missing}. "
        "Wire them into a workflow, or add them to UNCOLLECTED_WITH_REASON with a reason."
    )


def test_exemptions_still_exist():
    """Stale exemptions are worse than none — they read as deliberate."""
    for path in UNCOLLECTED_WITH_REASON:
        assert (REPO / path).is_dir(), f"{path} is exempted but no longer exists; drop the entry"


def test_exemptions_are_justified():
    for path, reason in UNCOLLECTED_WITH_REASON.items():
        assert len(reason) > 20, f"{path} needs a real reason, not {reason!r}"
