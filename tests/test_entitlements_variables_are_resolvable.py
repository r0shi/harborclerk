"""Every build variable in a .entitlements file must be one notarize.sh resolves.

Xcode expands `$(AppIdentifierPrefix)` when *it* signs. `codesign` does not, so
`scripts/notarize.sh` — which signs the shipped build — has to do the expansion
itself. It previously passed the source file straight through, and the app
shipped declaring a keychain access group literally named
`$(AppIdentifierPrefix)com.harborclerk.shared`.

Nothing catches that downstream. A bogus entitlement signs cleanly, passes
`codesign --verify --deep --strict`, and notarizes; it only surfaces at runtime
as SecItemAdd returning errSecMissingEntitlement, which MasterKeyManager
deliberately does not treat as fatal — it logs and carries on with an in-memory
key, so the visible symptom is mail credentials quietly needing re-entry after a
restart.

So the failure is silent at every stage that could plausibly catch it. Hence a
test on the input instead: if someone adds a variable notarize.sh doesn't know
how to expand, fail here rather than ship it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MACOS = REPO / "macos"
NOTARIZE = MACOS / "scripts" / "notarize.sh"

# Variables notarize.sh explicitly substitutes. Adding one here without also
# teaching notarize.sh to expand it just moves the bug.
RESOLVED_BY_NOTARIZE = {"AppIdentifierPrefix"}


def _entitlements_files() -> list[Path]:
    return sorted(MACOS.rglob("*.entitlements"))


def test_every_entitlements_variable_is_resolved_at_signing():
    files = _entitlements_files()
    assert files, "found no .entitlements files — the glob is broken, not the repo"

    unresolvable: list[str] = []
    for path in files:
        for var in re.findall(r"\$\((\w+)\)", path.read_text()):
            if var not in RESOLVED_BY_NOTARIZE:
                unresolvable.append(f"{path.relative_to(REPO)}: $({var})")

    assert not unresolvable, (
        f"these entitlements use build variables notarize.sh does not expand: {unresolvable}. "
        "codesign embeds them literally, which signs and notarizes cleanly and then "
        "fails at runtime as errSecMissingEntitlement. Teach resolve_entitlements() "
        "to substitute them, then add them to RESOLVED_BY_NOTARIZE."
    )


def test_notarize_actually_substitutes_what_this_test_claims():
    """Otherwise the exemption list above is just an assertion about itself."""
    body = "\n".join(re.sub(r"#.*$", "", line) for line in NOTARIZE.read_text().splitlines())
    for var in RESOLVED_BY_NOTARIZE:
        assert var in body, (
            f"RESOLVED_BY_NOTARIZE lists ${{{var}}} but notarize.sh never mentions it outside comments, "
            "so nothing expands it and the guard above is passing on a false premise."
        )
