"""Tests for the unit-planning logic in sweep.py — particularly the --models filter
and --corpora filter interaction with phase ranges."""

from __future__ import annotations

from scripts.test_corpora.runner.sweep import _plan_units


def _qbc(corpora: list[str]) -> dict[str, dict]:
    """Minimal questions_by_corpus stub: 1 research + 1 ask question per corpus,
    no cross-language variants."""
    return {
        c: {
            "research": [{"id": f"{c}-research-1", "text": "r1"}],
            "ask": [{"id": f"{c}-ask-1", "text": "a1"}],
        }
        for c in corpora
    }


def test_models_filter_restricts_phase4_loop():
    units = _plan_units(
        _qbc(["cuad"]),
        phases={4},
        depth="standard",
        models_filter={"qwen3-8b", "qwen3-4b"},
    )
    models = {u.model for u in units}
    assert models == {"qwen3-8b", "qwen3-4b"}


def test_models_filter_restricts_phase5_to_top_subset():
    """Phase 5 normally includes both TOP_MODELS. A filter that excludes
    qwen36-35b-a3b should leave only gemma4-26b-a4b in Phase 5."""
    units = _plan_units(
        _qbc(["cuad"]),
        phases={5},
        depth="standard",
        models_filter={"gemma4-26b-a4b"},
    )
    models = {u.model for u in units}
    assert models == {"gemma4-26b-a4b"}


def test_models_filter_excludes_phase2_smoke_when_qwen35_filtered_out():
    """Phase 2 hard-codes qwen36-35b-a3b. If that model isn't in the filter, no
    Phase 2 unit should be planned."""
    units = _plan_units(
        _qbc(["cuad"]),
        phases={2},
        depth="standard",
        models_filter={"qwen3-4b"},
    )
    assert units == []


def test_no_models_filter_means_all_models():
    units = _plan_units(_qbc(["cuad"]), phases={4}, depth="standard", models_filter=None)
    models = {u.model for u in units}
    # Should be exactly the 6 ALL_MODELS
    assert len(models) == 6


def test_corpora_filter_phase0_only_lists_provided_corpora():
    units = _plan_units(_qbc(["cuad"]), phases={0}, depth="standard")
    assert {u.corpus for u in units} == {"cuad"}


def test_phase4_corpus_then_model_ordering_minimizes_db_wipes():
    """Critical for sweep cost: corpus is the outer loop so each corpus
    re-ingest happens only once per phase, regardless of how many models
    we sweep through. A model-outer/corpus-inner ordering would force a
    DB wipe + ingest between every model, blowing up wall-clock by ~24x."""
    units = _plan_units(
        _qbc(["cuad", "enron"]),
        phases={4},
        depth="standard",
        models_filter={"qwen3-8b", "qwen3-4b"},
    )
    # Walk the unit list and count corpus transitions
    transitions = sum(1 for a, b in zip(units, units[1:]) if a.corpus != b.corpus)
    # 2 corpora → exactly 1 transition (cuad block, then enron block)
    assert transitions == 1


def test_phase_planning_is_additive_after_prior_phase(tmp_path):
    """Regression: invoking the harness with --phases 4 after --phases 0-1
    has already populated state.json must still register phase 4 units.
    The original gate was ``if not sf.units()`` which silently skipped
    planning for every later phase. Now we register only the missing
    phases additively."""
    from scripts.test_corpora.runner.state import StateFile, Unit

    sf = StateFile(tmp_path / "state.json")
    # Simulate: phase 0/1 already done from a prior run
    sf.register(
        [
            Unit(phase=0, corpus="cuad", model="-", question_id="-", depth="-"),
            Unit(phase=1, corpus="cuad", model="claude-baseline", question_id="cuad-research-1", depth="n/a"),
        ]
    )
    sf.save()

    # Sanity check: state has phases {0, 1}, no phase 4
    existing_phases = {u.phase for u in sf.units()}
    assert existing_phases == {0, 1}

    # The new gate: register units for any requested phase that's not present
    requested = {4}
    missing_phases = requested - existing_phases
    assert missing_phases == {4}

    new_units = _plan_units(
        _qbc(["cuad"]),
        missing_phases,
        "standard",
        models_filter={"qwen36-35b-a3b"},
    )
    assert len(new_units) > 0, "missing-phase planning produced zero units"
    assert all(u.phase == 4 for u in new_units), "all new units must be phase 4"

    sf.register(new_units)
    sf.save()

    # Reload and verify both phases coexist
    sf2 = StateFile(tmp_path / "state.json")
    sf2.load()
    phases_seen = {u.phase for u in sf2.units()}
    assert phases_seen == {0, 1, 4}, f"expected phases {{0,1,4}}, got {phases_seen}"
