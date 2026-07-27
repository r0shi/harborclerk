"""Every env var the Python reads must be settable on some deployment.

Three knobs have shipped unreachable, all for the same reason:
`ServiceManager.pythonEnvironment()` builds a *closed* dict and never inherits
the process environment, so on macOS a variable is invisible unless a Service's
`extraEnvironment` lists it explicitly — and Compose has no equivalent either,
each service opting in one at a time.

  - `EMBED_MAX_CONCURRENCY` — shipped with a defensive parser guarding an
    empty-string case that could not occur, because nothing could set it.
  - `GPU_CACHE_HIGH_WATER_MB` — the documented kill switch for the GPU cache
    release did not exist, on macOS least of all, which is the only platform
    where that code runs at all.
  - `EMBED_NEEDS_PREFIX` — its own comment says it exists "so the e5 rollback
    path keeps working". That path could not be enabled.

The failure mode is quiet: the code reads the variable and takes its default
forever, so everything works — just never differently. It surfaces as "we
tuned it and nothing changed", usually during an incident.

Adding a knob is fine. Adding one nobody can turn is not: either plumb it, or
record it here with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (REPO / "src", REPO / "embedder" / "src")
COMPOSE = REPO / "docker-compose.yml"
SWIFT_DIR = REPO / "macos" / "HarborClerkServer" / "HarborClerkServer"

# Matches a direct read *and* the helper indirections the codebase uses —
# `_positive_int_env("NAME", ...)`, `_int_env("NAME", ...)`. Literal-only
# matching missed EMBED_MAX_CONCURRENCY entirely, which is how a mutation test
# of this file caught its own blind spot.
_READ_PATTERN = re.compile(r"""(?:environ\.get|getenv|\w*_env)\(\s*["']([A-Z][A-Z0-9_]+)["']""")

# Read by Python but deliberately not plumbed by us. Reason required.
NOT_PLUMBED_WITH_REASON: dict[str, str] = {
    # Set by the operator in their own shell before invoking the CLI; there is
    # no service to plumb them through.
    "HARBOR_CLERK_URL": "CLI client config, set by the operator's shell",
    "HARBOR_CLERK_API_KEY": "CLI client config, set by the operator's shell",
    "HARBOR_CLERK_INSECURE_SKIP_VERIFY": "CLI client config, set by the operator's shell",
    # Set by the macOS app via NATIVE_CONFIG_FILE-adjacent bundle paths rather
    # than the environment; Docker uses the image's own layout.
    "APPLE_SUMMARIZE_PATH": "resolved from the app bundle, not configured",
    "LANG_PACKS_DIR": "resolved from the app bundle / image layout, not configured",
    "RQ_QUEUES": "legacy; worker queues come from --queues, see worker/entry.py",
}


def _read_by_python() -> set[str]:
    found: set[str] = set()
    for root in PYTHON_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            found |= set(_READ_PATTERN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def _settable() -> set[str]:
    """Names any deployment can actually set.

    Compose: `NAME: ${NAME:-default}` under a service's `environment`.
    macOS:   a `"NAME":` key in any Service's `extraEnvironment` dict, or
             anywhere in the shared environment builder.
    Both are matched textually rather than parsed — this is a reachability
    check, not a config validator, and a false *positive* here would only
    weaken the guard, never break a build.
    """
    names: set[str] = set()
    if COMPOSE.exists():
        names |= set(re.findall(r"^\s+([A-Z][A-Z0-9_]+)\s*:", COMPOSE.read_text(), flags=re.MULTILINE))
    if SWIFT_DIR.exists():
        for path in SWIFT_DIR.rglob("*.swift"):
            names |= set(re.findall(r'"([A-Z][A-Z0-9_]+)"\s*:', path.read_text(encoding="utf-8", errors="replace")))
    return names


def test_every_env_var_the_python_reads_can_be_set():
    read = _read_by_python()
    assert read, "found no os.environ reads — the pattern is broken, not the repo"

    unreachable = sorted(read - _settable() - set(NOT_PLUMBED_WITH_REASON))

    assert not unreachable, (
        "these variables are read by the Python but cannot be set on any "
        f"deployment, so they are permanently stuck at their defaults: {unreachable}. "
        "Plumb them through docker-compose.yml and the relevant Service's "
        "extraEnvironment (pythonEnvironment() does not inherit the process "
        "environment), or add them to NOT_PLUMBED_WITH_REASON."
    )


def test_exemptions_are_still_read():
    """An exemption for a variable nobody reads is stale — drop it."""
    read = _read_by_python()
    stale = sorted(name for name in NOT_PLUMBED_WITH_REASON if name not in read)
    assert not stale, f"exempted but no longer read by any Python: {stale}"


def test_exemptions_are_justified():
    for name, reason in NOT_PLUMBED_WITH_REASON.items():
        assert len(reason) > 20, f"{name} needs a real reason, not {reason!r}"
