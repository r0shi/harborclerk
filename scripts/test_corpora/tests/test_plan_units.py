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
        models_filter={"qwen3-8b", "phi-4-mini"},
    )
    models = {u.model for u in units}
    assert models == {"qwen3-8b", "phi-4-mini"}


def test_models_filter_restricts_phase5_to_top_subset():
    """Phase 5 normally includes both TOP_MODELS. A filter that excludes
    qwen3.6-35b should leave only gemma-26b in Phase 5."""
    units = _plan_units(
        _qbc(["cuad"]),
        phases={5},
        depth="standard",
        models_filter={"gemma-26b"},
    )
    models = {u.model for u in units}
    assert models == {"gemma-26b"}


def test_models_filter_excludes_phase2_smoke_when_qwen35_filtered_out():
    """Phase 2 hard-codes qwen3.6-35b. If that model isn't in the filter, no
    Phase 2 unit should be planned."""
    units = _plan_units(
        _qbc(["cuad"]),
        phases={2},
        depth="standard",
        models_filter={"phi-4-mini"},
    )
    assert units == []


def test_no_models_filter_means_all_models():
    units = _plan_units(_qbc(["cuad"]), phases={4}, depth="standard", models_filter=None)
    models = {u.model for u in units}
    # Should be exactly the 8 ALL_MODELS
    assert len(models) == 8


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
        models_filter={"qwen3-8b", "phi-4-mini"},
    )
    # Walk the unit list and count corpus transitions
    transitions = sum(1 for a, b in zip(units, units[1:]) if a.corpus != b.corpus)
    # 2 corpora → exactly 1 transition (cuad block, then enron block)
    assert transitions == 1
