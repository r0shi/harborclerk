import json
import time
from pathlib import Path

import pytest

from scripts.test_corpora.runner.state import (
    StateFile,
    Status,
    Unit,
)


def test_load_creates_empty_state_when_missing(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.load()
    assert sf.units() == []


def test_register_units_persists(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="q1", depth="standard")])
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    rows = sf2.units()
    assert len(rows) == 1
    assert rows[0].status == Status.PENDING


def test_atomic_write_no_partial_state(tmp_path: Path, monkeypatch):
    """If save() crashes mid-write, state.json must not be corrupted."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="m", question_id="q", depth="standard")])
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    sf2.set_status(4, "cuad", "m", "q", "standard", Status.IN_PROGRESS)

    # Simulate a crash by writing a half-state directly to the temp path
    tmp_target = tmp_path / "state.json.tmp"
    tmp_target.write_text("{ partial")
    # Real save should still succeed and leave state.json valid
    sf2.save()
    assert json.loads((tmp_path / "state.json").read_text())["units"][0]["status"] == "in_progress"


def test_stale_in_progress_reverts_to_pending(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="m", question_id="q", depth="standard")])
    sf.set_status(4, "cuad", "m", "q", "standard", Status.IN_PROGRESS, heartbeat=time.time() - 7200)
    sf.save()

    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    sf2.recover_stale(stale_threshold_seconds=3600)

    rows = sf2.units()
    assert rows[0].status == Status.PENDING


def test_lock_prevents_concurrent_runs(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.acquire_lock()
    try:
        sf2 = StateFile(tmp_path / "state.json")
        with pytest.raises(RuntimeError, match="locked"):
            sf2.acquire_lock()
    finally:
        sf.release_lock()


def test_rerun_selector_flips_matching_to_pending(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=5, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
            Unit(phase=5, corpus="enron", model="qwen3.6-35b", question_id="q1", depth="standard"),
        ]
    )
    sf.set_status(5, "cuad", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.set_status(5, "enron", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.rerun({"corpus": "cuad"})
    rows = {(u.corpus, u.status) for u in sf.units()}
    assert ("cuad", Status.PENDING) in rows
    assert ("enron", Status.DONE) in rows


def test_register_does_not_collide_across_phases(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
            Unit(phase=5, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
        ]
    )
    assert len(sf.units()) == 2


def test_rename_model_ids_rewrites_unit_keys_and_preserves_status(tmp_path: Path):
    """Migration helper: state.json units written before PR #300 keep
    using informal labels like 'gemma-26b'. ``rename_model_ids`` must
    rewrite both the unit's ``model`` field AND its position in the
    keyed dict (model is part of the key) without losing per-unit
    status fields."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="gemma-26b", question_id="q1", depth="standard"),
            Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="q1", depth="standard"),
        ]
    )
    sf.set_status(4, "cuad", "gemma-26b", "q1", "standard", Status.DONE)

    n = sf.rename_model_ids({"gemma-26b": "gemma4-26b-a4b"})
    assert n == 1

    # Old model id no longer addressable
    assert sf.get(4, "cuad", "gemma-26b", "q1", "standard") is None

    # New model id reachable, with status preserved
    migrated = sf.get(4, "cuad", "gemma4-26b-a4b", "q1", "standard")
    assert migrated is not None
    assert migrated.status == Status.DONE
    assert migrated.model == "gemma4-26b-a4b"

    # Untouched models stay untouched
    assert sf.get(4, "cuad", "qwen3-8b", "q1", "standard") is not None


def test_rename_model_ids_is_idempotent(tmp_path: Path):
    """Calling the migration twice should be a no-op the second time —
    important because sweep.py runs it on every load."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="gemma-26b", question_id="q1", depth="standard")])
    rename = {"gemma-26b": "gemma4-26b-a4b"}
    assert sf.rename_model_ids(rename) == 1
    assert sf.rename_model_ids(rename) == 0  # already migrated, nothing left to rename
    assert len(sf.units()) == 1


def test_rename_model_ids_returns_zero_on_clean_state(tmp_path: Path):
    """Fresh state file with all-canonical ids should be untouched."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="q1", depth="standard")])
    n = sf.rename_model_ids({"gemma-26b": "gemma4-26b-a4b"})
    assert n == 0
    assert sf.get(4, "cuad", "qwen3-8b", "q1", "standard") is not None
