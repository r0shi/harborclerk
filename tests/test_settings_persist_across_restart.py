"""`Settings` fields persisted on macOS must survive a restart.

The companion to `test_env_vars_are_reachable.py`, which covers the ~17 raw
`os.environ` reads. This covers the 59 pydantic fields — the larger and more
consequential half, and the ones that guard structurally cannot see.

The bug (#592) was an asymmetry. Writes were generic: `sync_native_config(key,
value)` accepted any key and put it in config.json. Reads were two
hand-maintained lists naming eleven keys between them. So eleven fields an admin
could change through the UI were written to disk, never read back, and silently
reverted to their defaults on the next restart — no error, no log, nothing to
notice except a setting that would not stick.

The fix makes reads as generic as writes. That also means "which fields are
reachable on macOS?" stops being an interesting question: config.json now
applies *any* key naming a real field, so the answer is all of them.

An earlier draft of this file asserted the opposite — a list of fields with "no
macOS route", exempting the ones judged fine. It failed with 18 names, and every
one of them was a false positive created by the fix it was meant to guard. A
guard whose premise its own change invalidates is worse than no guard: it
teaches the next person to add names to an exemption dict. So the list is gone
and what remains is behavioural — write the keys, rebuild Settings the way a
restart does, and check the values came back.

The one thing this cannot see is Swift: `_swift_env_keys` matched text, and #588
shipped a reference to a property that did not exist. The macOS build job added
alongside this closes that; compilability is not a question a regex can answer.
"""

from __future__ import annotations

import re
from pathlib import Path

from harbor_clerk.config import Settings

REPO = Path(__file__).resolve().parents[1]
SWIFT_DIR = REPO / "macos" / "HarborClerkServer" / "HarborClerkServer"
SRC = REPO / "src" / "harbor_clerk"


def _keys_persisted_by_admin_routes() -> set[str]:
    """Keys reaching `sync_native_config`, from literals and from the generic
    loop in `update_retrieval_settings` (which forwards a request model's
    fields, so the model's own fields are the key set)."""
    keys: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        keys |= set(re.findall(r'sync_native_config\(\s*["\']([a-z][a-z0-9_]*)["\']', text))

    # api/routes/system.py::update_retrieval_settings forwards every set field
    # of RetrievalSettingsUpdate; those names are the keys it writes.
    try:
        from harbor_clerk.api.schemas.system import RetrievalSettingsUpdate

        keys |= set(RetrievalSettingsUpdate.model_fields)
    except Exception:  # schema moved or renamed — the literal scan still applies
        pass
    return keys


def _restart_with(config_json: dict, tmp_path, monkeypatch):
    """Rebuild Settings the way a process restart does, with this config.json.

    Deliberately does *not* call `apply_native_config` — the whole claim of #592
    is that startup reads the file on its own. An earlier draft of these tests
    called it explicitly, and so removing the call from `get_settings()`
    entirely left all of them green: they proved the overlay worked when
    invoked, never that anything invoked it.

    The path arrives by env var because `native_config_file` is itself a
    Settings field, so it has to be resolvable before the overlay can run.
    """
    import json

    from harbor_clerk import config as cfg

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(config_json))
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(cfg_file))
    monkeypatch.setattr(cfg, "_settings", None)
    return cfg.get_settings()


def test_config_json_is_a_universal_route(tmp_path, monkeypatch):
    """Any config.json key naming a real field must be applied — no allowlist.

    Before the fix, reads were two hand-maintained lists while writes were
    generic, so adding a field to a PUT route was silently only half the work.
    An allowlist here would recreate exactly that: the next field added would be
    persisted and unread, and nothing would say so. Both keys below were on
    neither list.
    """
    settings = _restart_with({"reranker_timeout_seconds": 5.0, "mcp_max_k": 7}, tmp_path, monkeypatch)
    assert settings.reranker_timeout_seconds == 5.0, "config.json key naming a real field was not applied at startup"
    assert settings.mcp_max_k == 7


def test_an_invalid_value_is_skipped_not_fatal(tmp_path, monkeypatch):
    """A hand-edited config.json must not stop the process starting, and must not
    put a string where an int belongs to fail confusingly somewhere later."""
    from harbor_clerk.config import Settings

    default = Settings.model_fields["mcp_max_k"].default
    settings = _restart_with({"mcp_max_k": "not-a-number"}, tmp_path, monkeypatch)  # must not raise
    assert settings.mcp_max_k == default, "an invalid value was applied instead of being skipped"


def test_persisted_keys_survive_a_restart(tmp_path, monkeypatch):
    """The #592 bug itself, driven rather than inspected.

    Every key an admin route persists must still be there after a restart.
    Asserting only that the key *names* a real field would be vacuous — the
    eleven broken ones always did; what was missing was anything reading them.
    """
    from harbor_clerk import config as cfg

    persisted = _keys_persisted_by_admin_routes() & set(Settings.model_fields)
    assert persisted, "found no persisted keys — the scan is broken, not the code"

    # A value distinguishable from the default, per field type.
    current = cfg.get_settings()
    probe: dict[str, object] = {}
    for key in sorted(persisted):
        value = getattr(current, key, None)
        if isinstance(value, bool):
            probe[key] = not value
        elif isinstance(value, int):
            probe[key] = value + 7
        elif isinstance(value, float):
            probe[key] = value + 7.0
        elif isinstance(value, str):
            probe[key] = f"{value}-probe"

    settings = _restart_with(probe, tmp_path, monkeypatch)
    reverted = [k for k, v in probe.items() if getattr(settings, k) != v]
    assert not reverted, (
        f"these keys are persisted by admin routes but do not survive a restart: {reverted}. "
        "They are written to config.json and never read back, so every change an admin "
        "makes to them silently reverts to the default."
    )


def test_every_key_swift_writes_is_classified():
    """A key the menubar writes must be a Settings field or explicitly native-only.

    This is #592 generalised. That bug was writes being generic while reads were
    a list, so a new key was persisted and silently never read. The same shape
    exists one level up: Swift can add a `data["new_key"]` write, and if Python
    neither has a field for it nor names it native-only, it lands in config.json
    and quietly does nothing — which looks identical to working.

    Being wrong here is cheap and loud (add a name), whereas the failure it
    replaces is silent and only shows up as "we changed it and nothing
    happened".
    """
    from harbor_clerk.config import _NATIVE_ONLY_KEYS

    settings_swift = SWIFT_DIR / "Settings.swift"
    assert settings_swift.exists(), f"expected {settings_swift} — the guard cannot run"

    written = set(re.findall(r'data\["([a-z][a-z0-9_]*)"\]', settings_swift.read_text()))
    assert written, "found no data[...] writes — the pattern is broken, not the Swift"

    unclassified = sorted(written - set(Settings.model_fields) - _NATIVE_ONLY_KEYS)
    assert not unclassified, (
        f"the menubar writes these config.json keys but Python neither has a Settings "
        f"field for them nor lists them as native-only: {unclassified}. They are "
        "persisted and then ignored. Add the field, or add the key to "
        "_NATIVE_ONLY_KEYS with a comment saying what consumes it."
    )


def test_native_only_keys_are_not_settings_fields():
    """A name in both places reads as native-only while actually being applied."""
    from harbor_clerk.config import _NATIVE_ONLY_KEYS

    both = sorted(_NATIVE_ONLY_KEYS & set(Settings.model_fields))
    assert not both, f"listed as native-only but really are Settings fields, so they are applied: {both}"
