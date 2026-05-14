"""Static guard: no production code in src/harbor_clerk/mail/ may call
an IMAP mutating method by name.

This complements the runtime defenses (ReadOnlyIMAP4_SSL, FakeIMAP)
by catching the case where a developer reaches around the wrapper to
construct or grab a raw aioimaplib client.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Method names whose invocation on ANY object in mail/ is forbidden.
# We accept the over-broad match — there's no benign reason for a
# Python module under harbor_clerk.mail to call .store / .copy / .move
# / .expunge on any object.
MUTATING_NAMES = frozenset(
    {
        "store",
        "expunge",
        "uid_store",
        "uid_expunge",
    }
)

# Bare select() is forbidden — examine() is the sanctioned form.
# We allow conn.examine, FakeIMAP.examine; we don't allow .select()
# anywhere in production code.
FORBIDDEN_BARE = frozenset({"select"})

MAIL_DIR = Path(__file__).resolve().parents[2] / "src" / "harbor_clerk" / "mail"


def _scan(source: str) -> list[tuple[str, int]]:
    """Return (method_name, line) for every Attribute call in source
    whose method name is in MUTATING_NAMES or FORBIDDEN_BARE."""
    tree = ast.parse(source)
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in MUTATING_NAMES or name in FORBIDDEN_BARE:
                violations.append((name, node.lineno))
    return violations


@pytest.mark.parametrize("path", sorted(MAIL_DIR.rglob("*.py")))
def test_no_mutating_imap_calls(path):
    """No mail module may call a mutating IMAP method or bare select()."""
    # readonly_imap.py defines overrides; their bodies call the parent
    # method via super() — that's the one sanctioned exception. We skip
    # the file entirely.
    if path.name == "readonly_imap.py":
        pytest.skip("subclass is allowed to reference parent mutating methods")
    source = path.read_text(encoding="utf-8")
    violations = _scan(source)
    assert not violations, (
        f"{path}: found forbidden IMAP calls: {violations}. "
        f"Use IMAPConnection methods (examine, fetch, uid, ...) instead."
    )
