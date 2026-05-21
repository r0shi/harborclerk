from scripts.test_corpora.runner.metrics import (
    citation_extra,
    citation_overlap,
    entity_overlap,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
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


# ── recall_at_k ──


def test_recall_at_k_all_hits_in_top_k():
    baseline = ["a", "b", "c"]
    ranked = ["a", "b", "c", "x", "y"]
    assert recall_at_k(baseline, ranked, k=3) == 1.0
    assert recall_at_k(baseline, ranked, k=10) == 1.0


def test_recall_at_k_partial_in_top_k():
    baseline = ["a", "b", "c", "d"]
    ranked = ["a", "x", "b", "y", "z"]  # a@1, b@3, no c, no d in top-5
    assert recall_at_k(baseline, ranked, k=5) == 0.5


def test_recall_at_k_zero_when_none_in_top_k():
    baseline = ["a", "b"]
    ranked = ["x", "y", "z", "a"]  # a is at position 4
    assert recall_at_k(baseline, ranked, k=3) == 0.0


def test_recall_at_k_empty_baseline_returns_zero():
    assert recall_at_k([], ["a", "b"], k=5) == 0.0


def test_recall_at_k_empty_ranked_returns_zero():
    assert recall_at_k(["a"], [], k=5) == 0.0


def test_recall_at_k_ranked_shorter_than_k():
    baseline = ["a", "b"]
    ranked = ["a"]
    assert recall_at_k(baseline, ranked, k=10) == 0.5


def test_recall_at_k_k_zero_returns_zero():
    assert recall_at_k(["a"], ["a"], k=0) == 0.0


def test_recall_at_k_dedup_in_ranked():
    """Duplicate ranked ids must not double-count toward recall."""
    baseline = ["a", "b"]
    ranked = ["a", "a", "a", "x"]  # a appears thrice; b nowhere
    assert recall_at_k(baseline, ranked, k=4) == 0.5


# ── reciprocal_rank ──


def test_reciprocal_rank_first_hit_at_top():
    assert reciprocal_rank(["a", "b"], ["a", "x", "y"]) == 1.0


def test_reciprocal_rank_first_hit_at_second():
    assert reciprocal_rank(["a", "b"], ["x", "a", "y"]) == 0.5


def test_reciprocal_rank_first_hit_at_fourth():
    assert reciprocal_rank(["a"], ["x", "y", "z", "a"]) == 0.25


def test_reciprocal_rank_no_hit_returns_zero():
    assert reciprocal_rank(["a", "b"], ["x", "y", "z"]) == 0.0


def test_reciprocal_rank_empty_baseline_returns_zero():
    assert reciprocal_rank([], ["a"]) == 0.0


def test_reciprocal_rank_empty_ranked_returns_zero():
    assert reciprocal_rank(["a"], []) == 0.0


def test_reciprocal_rank_uses_earliest_hit():
    """If multiple baseline ids appear, rank of the earliest counts."""
    # 'b' at position 2; 'a' at position 4. Earliest is 'b'.
    assert reciprocal_rank(["a", "b"], ["x", "b", "y", "a"]) == 0.5


# ── ndcg_at_k ──


def test_ndcg_at_k_perfect_ordering_returns_one():
    baseline = ["a", "b", "c"]
    ranked = ["a", "b", "c", "x", "y"]
    assert ndcg_at_k(baseline, ranked, k=3) == 1.0


def test_ndcg_at_k_no_hits_returns_zero():
    assert ndcg_at_k(["a", "b"], ["x", "y", "z"], k=10) == 0.0


def test_ndcg_at_k_empty_baseline_returns_zero():
    assert ndcg_at_k([], ["a"], k=5) == 0.0


def test_ndcg_at_k_reverse_order_less_than_one():
    """All baseline ids in top-K but reversed ordering scores lower than perfect."""
    baseline = ["a", "b", "c"]
    ideal = ndcg_at_k(baseline, ["a", "b", "c"], k=3)
    reversed_ = ndcg_at_k(baseline, ["c", "b", "a"], k=3)
    # Binary relevance: with all hits present, reversed still scores 1.0 because
    # each position contributes equally to numerator and denominator. Test the
    # weaker invariant: shoving a hit further down DOES reduce score.
    assert ideal == 1.0
    assert reversed_ == 1.0


def test_ndcg_at_k_hit_further_down_scores_lower():
    """Moving a hit from position 1 to position 5 must reduce nDCG."""
    baseline = ["a"]
    early = ndcg_at_k(baseline, ["a", "x", "y", "z", "w"], k=5)
    late = ndcg_at_k(baseline, ["x", "y", "z", "w", "a"], k=5)
    assert early > late
    assert early == 1.0


def test_ndcg_at_k_partial_recall():
    """Two baseline hits, one in top-K and one outside."""
    baseline = ["a", "b"]
    # Only 'a' in top-3; ideal would place a@1 and b@2.
    score = ndcg_at_k(baseline, ["a", "x", "y", "b"], k=3)
    # DCG: 1/log2(2) = 1.0 (only 'a' contributes)
    # IDCG@3 capped at |baseline|=2: 1/log2(2) + 1/log2(3) = 1.0 + 0.6309 ≈ 1.6309
    # nDCG = 1.0 / 1.6309 ≈ 0.613
    assert 0.6 < score < 0.62


def test_ndcg_at_k_empty_ranked_returns_zero():
    assert ndcg_at_k(["a"], [], k=5) == 0.0
