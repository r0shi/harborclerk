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


# ── selector matching ──


def test_rerun_status_selector_matches_enum_value(tmp_path: Path):
    """Regression for the Enum-vs-string mismatch: --rerun 'status=error'
    used to silently flip 0 units because str(Status.ERROR) returns
    'Status.ERROR', not 'error'. The fix compares against .value for
    Enum fields. Critical for recovering chat-422 / model-warmup ERROR
    cells without manual state.json surgery."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard"),
            Unit(phase=4, corpus="cuad", model="m", question_id="q2", depth="standard"),
            Unit(phase=4, corpus="cuad", model="m", question_id="q3", depth="standard"),
        ]
    )
    sf.set_status(4, "cuad", "m", "q1", "standard", Status.DONE)
    sf.set_status(4, "cuad", "m", "q2", "standard", Status.ERROR)
    sf.set_status(4, "cuad", "m", "q3", "standard", Status.ERROR)

    n = sf.rerun({"status": "error"})
    assert n == 2

    # Both ERROR units flipped to PENDING; DONE stayed DONE
    statuses = {u.question_id: u.status for u in sf.units()}
    assert statuses["q1"] == Status.DONE
    assert statuses["q2"] == Status.PENDING
    assert statuses["q3"] == Status.PENDING


def test_rerun_int_selector_still_matches_phase(tmp_path: Path):
    """Regression: the selector fix must not break int comparisons.
    --rerun 'phase=4' has always worked and keeps working."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard"),
            Unit(phase=5, corpus="cuad", model="m", question_id="q1", depth="standard"),
        ]
    )
    sf.set_status(4, "cuad", "m", "q1", "standard", Status.DONE)
    sf.set_status(5, "cuad", "m", "q1", "standard", Status.DONE)

    n = sf.rerun({"phase": "4"})
    assert n == 1
    statuses = {u.phase: u.status for u in sf.units()}
    assert statuses[4] == Status.PENDING
    assert statuses[5] == Status.DONE


def test_rerun_combines_status_and_other_selectors(tmp_path: Path):
    """Selectors AND together: --rerun 'status=error,corpus=cuad' should
    match only ERROR cells from cuad, leaving ERROR cells from other
    corpora untouched."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard"),
            Unit(phase=4, corpus="enron", model="m", question_id="q1", depth="standard"),
        ]
    )
    sf.set_status(4, "cuad", "m", "q1", "standard", Status.ERROR)
    sf.set_status(4, "enron", "m", "q1", "standard", Status.ERROR)

    n = sf.rerun({"status": "error", "corpus": "cuad"})
    assert n == 1
    cuad_unit = sf.get(4, "cuad", "m", "q1", "standard")
    enron_unit = sf.get(4, "enron", "m", "q1", "standard")
    assert cuad_unit is not None and cuad_unit.status == Status.PENDING
    assert enron_unit is not None and enron_unit.status == Status.ERROR


def test_rerun_substring_selector_matches_error_text(tmp_path: Path):
    """A `~` prefix turns the selector into a substring-contains match. The
    original case: retry only the units that aborted with the SSE-silence
    watchdog message, leaving other failure modes alone."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard"),
            Unit(phase=4, corpus="cuad", model="m", question_id="q2", depth="standard"),
            Unit(phase=4, corpus="cuad", model="m", question_id="q3", depth="standard"),
        ]
    )
    sf.set_status(
        4, "cuad", "m", "q1", "standard", Status.ERROR, error="harness aborted research: no SSE events for 311s"
    )
    sf.set_status(
        4, "cuad", "m", "q2", "standard", Status.ERROR, error="harness aborted research: no SSE events for 240s"
    )
    sf.set_status(4, "cuad", "m", "q3", "standard", Status.ERROR, error="HC unreachable after 3 consecutive failures")

    n = sf.rerun({"error": "~no SSE events"})
    assert n == 2
    assert sf.get(4, "cuad", "m", "q1", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "m", "q2", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "m", "q3", "standard").status == Status.ERROR


def test_rerun_substring_selector_handles_none_error(tmp_path: Path):
    """A None error field must not crash substring matching; just doesn't match."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard")])
    # PENDING unit, error is None
    n = sf.rerun({"error": "~no SSE events"})
    assert n == 0


def test_rerun_multi_value_id_selector_matches_any_listed_id(tmp_path: Path):
    """Regression: the CLI string ``id=A,id=B,id=C`` used to collapse into a
    single dict entry (``{"id": "C"}``) and silently match zero units,
    because (a) the parser called ``dict(...)`` over repeated keys and
    (b) Unit has no ``id`` attribute. The runbook's documented
    cuad-ask-1/2/3 + cuad-research-1 + enron-research-1 regen recipe never
    actually worked. After the fix, all five listed ids flip to PENDING.
    """
    sf = StateFile(tmp_path / "state.json")
    qids = ["cuad-ask-1", "cuad-ask-2", "cuad-ask-3", "cuad-research-1", "cuad-ask-10"]
    sf.register([Unit(phase=1, corpus="cuad", model="claude-baseline", question_id=q, depth="standard") for q in qids])
    for q in qids:
        sf.set_status(1, "cuad", "claude-baseline", q, "standard", Status.DONE)

    n = sf.rerun({"id": ["cuad-ask-1", "cuad-ask-2", "cuad-ask-3", "cuad-research-1"]})
    assert n == 4

    statuses = {u.question_id: u.status for u in sf.units()}
    assert statuses["cuad-ask-1"] == Status.PENDING
    assert statuses["cuad-ask-2"] == Status.PENDING
    assert statuses["cuad-ask-3"] == Status.PENDING
    assert statuses["cuad-research-1"] == Status.PENDING
    # cuad-ask-10 was not in the selector list — must stay DONE despite the
    # "cuad-ask-1" substring overlap (proves we're doing exact equality, not
    # accidental substring matching).
    assert statuses["cuad-ask-10"] == Status.DONE


def test_rerun_id_alias_maps_to_question_id(tmp_path: Path):
    """``id`` is the runbook-documented shorthand for ``question_id``.
    Without an alias, ``getattr(u, "id", "")`` returns "" and matches
    nothing — which is exactly the bug that made the documented regen
    recipe silently no-op."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=1, corpus="cuad", model="claude-baseline", question_id="cuad-ask-1", depth="standard")])
    sf.set_status(1, "cuad", "claude-baseline", "cuad-ask-1", "standard", Status.DONE)

    n = sf.rerun({"id": "cuad-ask-1"})
    assert n == 1
    u = sf.get(1, "cuad", "claude-baseline", "cuad-ask-1", "standard")
    assert u is not None and u.status == Status.PENDING


def test_rerun_combines_multi_id_and_phase_selectors(tmp_path: Path):
    """AND across keys, OR within a key: ``--rerun 'id=A,id=B,phase=4'``
    should match units whose phase==4 AND (question_id==A OR ==B)."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=1, corpus="cuad", model="claude-baseline", question_id="A", depth="standard"),
            Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="A", depth="standard"),
            Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="B", depth="standard"),
            Unit(phase=4, corpus="cuad", model="qwen3-8b", question_id="C", depth="standard"),
        ]
    )
    for u in sf.units():
        sf.set_status(u.phase, u.corpus, u.model, u.question_id, u.depth, Status.DONE)

    n = sf.rerun({"id": ["A", "B"], "phase": "4"})
    assert n == 2  # phase-4 A + phase-4 B; phase-1 A excluded, phase-4 C excluded

    assert sf.get(1, "cuad", "claude-baseline", "A", "standard").status == Status.DONE
    assert sf.get(4, "cuad", "qwen3-8b", "A", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "qwen3-8b", "B", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "qwen3-8b", "C", "standard").status == Status.DONE


def test_skip_status_selector_matches_enum_value(tmp_path: Path):
    """Same selector fix applies to --skip; verify it works end-to-end."""
    sf = StateFile(tmp_path / "state.json")
    sf.register([Unit(phase=4, corpus="cuad", model="deepseek-r1-0528-8b", question_id="q1", depth="standard")])
    sf.set_status(4, "cuad", "deepseek-r1-0528-8b", "q1", "standard", Status.ERROR)

    n = sf.skip({"status": "error"})
    assert n == 1
    u = sf.get(4, "cuad", "deepseek-r1-0528-8b", "q1", "standard")
    assert u is not None and u.status == Status.SKIPPED


def test_recover_stale_with_threshold_zero_flips_all_in_progress(tmp_path: Path):
    """recover_stale(0) is what sweep startup uses to clean up units left
    IN_PROGRESS by a crashed prior run. Must flip every IN_PROGRESS unit
    regardless of how recently its heartbeat was set."""
    sf = StateFile(tmp_path / "state.json")
    sf.register(
        [
            Unit(phase=4, corpus="cuad", model="m", question_id="q1", depth="standard"),
            Unit(phase=4, corpus="cuad", model="m", question_id="q2", depth="standard"),
        ]
    )
    sf.set_status(4, "cuad", "m", "q1", "standard", Status.IN_PROGRESS)
    # q2 stays PENDING

    n = sf.recover_stale(stale_threshold_seconds=0)
    assert n == 1
    u1 = sf.get(4, "cuad", "m", "q1", "standard")
    assert u1 is not None and u1.status == Status.PENDING
    assert u1.heartbeat is None
    assert u1.started_at is None
