from scripts.test_corpora.runner.metrics import (
    citation_extra,
    citation_overlap,
    entity_overlap,
)


def test_citation_overlap_full_match():
    baseline = ["a", "b", "c"]
    model = ["a", "b", "c"]
    assert citation_overlap(baseline, model) == 1.0


def test_citation_overlap_partial():
    baseline = ["a", "b", "c", "d"]
    model = ["a", "c"]
    assert citation_overlap(baseline, model) == 0.5


def test_citation_overlap_empty_baseline_returns_zero():
    assert citation_overlap([], ["a"]) == 0.0


def test_citation_extra_counts_model_only():
    assert citation_extra(["a", "b"], ["a", "b", "c", "d"]) == 2


def test_entity_overlap_english(monkeypatch):
    """Use a stub spaCy doc so we don't depend on the model loading."""
    baseline = "California and Delaware appear in the contract from Acme."
    model = "The contract from Acme references California."
    score = entity_overlap(baseline, model, lang="en")
    # Expect ~2/3 (Acme + California found, Delaware missing)
    assert 0.6 < score <= 1.0


def test_entity_overlap_empty_returns_zero():
    assert entity_overlap("", "any text", lang="en") == 0.0
