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
    sf.register([
        Unit(phase=5, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
        Unit(phase=5, corpus="enron", model="qwen3.6-35b", question_id="q1", depth="standard"),
    ])
    sf.set_status(5, "cuad", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.set_status(5, "enron", "qwen3.6-35b", "q1", "standard", Status.DONE)
    sf.rerun({"corpus": "cuad"})
    rows = {(u.corpus, u.status) for u in sf.units()}
    assert ("cuad", Status.PENDING) in rows
    assert ("enron", Status.DONE) in rows


def test_register_does_not_collide_across_phases(tmp_path: Path):
    sf = StateFile(tmp_path / "state.json")
    sf.register([
        Unit(phase=4, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
        Unit(phase=5, corpus="cuad", model="qwen3.6-35b", question_id="q1", depth="standard"),
    ])
    assert len(sf.units()) == 2
