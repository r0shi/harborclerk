"""Tests for search helpers: _normalize_scores."""

import uuid

from harbor_clerk.search import _normalize_scores


def test_normalize_empty():
    assert _normalize_scores({}) == {}


def test_normalize_single():
    uid = uuid.uuid4()
    result = _normalize_scores({uid: 5.0})
    assert result[uid] == 1.0


def test_normalize_all_same():
    ids = [uuid.uuid4() for _ in range(3)]
    scores = {uid: 3.0 for uid in ids}
    result = _normalize_scores(scores)
    for uid in ids:
        assert result[uid] == 1.0


def test_normalize_spread():
    low = uuid.uuid4()
    mid = uuid.uuid4()
    high = uuid.uuid4()
    result = _normalize_scores({low: 0.0, mid: 5.0, high: 10.0})
    assert result[low] == 0.0
    assert result[mid] == 0.5
    assert result[high] == 1.0


def test_normalize_preserves_keys():
    ids = [uuid.uuid4() for _ in range(5)]
    scores = {uid: float(i) for i, uid in enumerate(ids)}
    result = _normalize_scores(scores)
    assert set(result.keys()) == set(ids)
