"""No .entitlements file may declare a restricted entitlement without a profile.

`keychain-access-groups`, `application-identifier`, and everything under
`com.apple.developer.*` are *restricted*: Apple only honours them when an
embedded provisioning profile authorises them. A Developer ID build that
declares one without a profile is SIGKILLed by the kernel at exec — before
`main()`, with no stderr, exit status 137.

Every stage that could plausibly catch that passes it through. `codesign
--verify --deep --strict` succeeds; `spctl --assess` says accepted; the notary
service returns Accepted and staples the ticket. None of them execute the
binary. That is how a notarized, stapled DMG shipped whose app died on launch.

It hid for months because Xcode's automatic signing embeds a *development*
profile, so every dev build was authorised and the release path — Developer ID,
no profile — was never launched by anyone until it was.

The only fix that keeps the key is shipping a provisioning profile. Until the
build does that (grep below), the key is banned outright.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MACOS = REPO / "macos"
SIGNING_SCRIPTS = (MACOS / "scripts" / "package.sh", MACOS / "scripts" / "notarize.sh")

RESTRICTED_EXACT = {
    "keychain-access-groups",
    "application-identifier",
    "com.apple.application-identifier",
    "aps-environment",
}
RESTRICTED_PREFIXES = ("com.apple.developer.",)


def _restricted(key: str) -> bool:
    return key in RESTRICTED_EXACT or key.startswith(RESTRICTED_PREFIXES)


def _build_embeds_a_profile() -> bool:
    """Comments stripped first: a `# TODO: embed embedded.provisionprofile` must
    not switch this guard off while the entitlements go unchecked."""
    for p in SIGNING_SCRIPTS:
        if not p.exists():
            continue
        code = "\n".join(re.sub(r"#.*$", "", line) for line in p.read_text().splitlines())
        # The embedding idiom specifically — a copy *into* Contents/. A strip step
        # such as `find … -name embedded.provisionprofile -delete` also mentions
        # the file and ships no profile.
        if re.search(r"\b(cp|ditto|install)\b[^\n]*provisionprofile[^\n]*Contents/", code):
            return True
    return False


def test_no_restricted_entitlement_without_a_provisioning_profile():
    files = sorted(MACOS.rglob("*.entitlements"))
    assert files, "found no .entitlements files — the glob is broken, not the repo"
    if _build_embeds_a_profile():
        return  # a profile can authorise these; the notary service checks the match

    offenders = [
        f"{p.relative_to(REPO) if p.is_relative_to(REPO) else p}: {k}"
        for p in files
        for k in plistlib.loads(p.read_bytes())
        if _restricted(k)
    ]
    assert not offenders, (
        f"restricted entitlements with no provisioning profile in the build: {offenders}. "
        "A Developer ID app carrying these is killed at exec (exit 137) and every "
        "pre-launch check — codesign, spctl, notarization — passes it anyway. Either "
        "embed a provisioning profile in package.sh/notarize.sh or remove the key."
    )
