"""Tests for the reranker FastAPI app, using a stub CrossEncoder."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_reranker_globals():
    """`embedder.reranker` keeps `_model` at module scope, and every TestClient
    here mutates it through lifespan. Without a reset, a test's outcome depends
    on what ran before it — which is how a passing assertion in this file can
    mean nothing. Restore it around each test.
    """
    import embedder.reranker as rr

    saved = rr._model
    yield
    rr._model = saved


@pytest.fixture
def stub_cross_encoder():
    """Stub CrossEncoder that returns predictable scores: higher index → higher score."""
    ce = MagicMock()

    def predict(pairs):
        # pairs is [[query, passage], ...]; score = passage index / total
        # We inject the index via a control character so the stub knows the order
        return [float(i) / max(1, len(pairs) - 1) for i in range(len(pairs))]

    ce.predict.side_effect = predict
    return ce


@pytest.fixture
def client(stub_cross_encoder):
    with patch("embedder.reranker.CrossEncoder", return_value=stub_cross_encoder):
        from embedder.reranker import app

        with TestClient(app) as c:
            yield c


def test_rerank_returns_top_k_sorted_desc(client):
    r = client.post(
        "/rerank",
        json={
            "query": "what is the termination clause",
            "passages": ["passage 0", "passage 1", "passage 2", "passage 3"],
            "top_k": 2,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "BAAI/bge-reranker-v2-m3"
    assert len(body["scores"]) == 2
    # Scores must be sorted desc
    assert body["scores"][0]["score"] >= body["scores"][1]["score"]
    # With our stub (score = i/3), top should be index 3
    assert body["scores"][0]["index"] == 3
    assert body["scores"][1]["index"] == 2


def test_rerank_empty_passages_returns_empty(client):
    r = client.post("/rerank", json={"query": "anything", "passages": [], "top_k": 10})
    assert r.status_code == 200
    assert r.json()["scores"] == []


def test_rerank_top_k_greater_than_len(client):
    r = client.post(
        "/rerank",
        json={"query": "x", "passages": ["a", "b"], "top_k": 100},
    )
    assert r.status_code == 200
    assert len(r.json()["scores"]) == 2


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_rerank_releases_the_gpu_cache(monkeypatch):
    """The reranker has the same variable-length exposure as the embedder, and
    until now no call-site test — so the regression below could not be caught.
    """
    from unittest.mock import patch

    import numpy as np
    from fastapi.testclient import TestClient

    calls = []
    model = MagicMock()
    model.predict.side_effect = lambda pairs: np.array([0.5] * len(pairs))

    with (
        patch("embedder.reranker.CrossEncoder", return_value=model),
        patch("embedder.reranker.release_gpu_cache", side_effect=lambda: calls.append(1)),
    ):
        from embedder.reranker import app

        with TestClient(app) as c:
            r = c.post("/rerank", json={"query": "q", "passages": ["a", "b"], "top_k": 2})
            assert r.status_code == 200

    assert len(calls) == 1, f"expected one release per rerank, got {len(calls)}"


def test_rerank_releases_the_cache_even_when_predict_raises():
    """The mirror of the embedder's test, which the first pass omitted.

    reranker.py makes the identical OOM argument for its `finally`, and mutating
    it to release only on success left all 25 tests green.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    calls = []
    model = MagicMock()
    model.predict.side_effect = RuntimeError("MPS backend out of memory")

    with (
        patch("embedder.reranker.CrossEncoder", return_value=model),
        patch("embedder.reranker.release_gpu_cache", side_effect=lambda: calls.append(1)),
    ):
        from embedder.reranker import app

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/rerank", json={"query": "q", "passages": ["a"], "top_k": 1})
            assert r.status_code >= 500

    assert calls == [1], "cache was not released when predict raised"


def test_rerank_survives_shutdown_between_the_guard_and_the_executor_hop():
    """Behavioural replacement for a test that inspected source text.

    The old one asserted `"model = _model" in inspect.getsource(...)`, so a
    semantically identical rename failed it and matching-but-wrong code passed.
    This drives the actual race: null the module global while `predict` is in
    flight, and assert the request does not become an AttributeError 500.
    """
    from unittest.mock import patch

    import embedder.reranker as rr
    import numpy as np
    from fastapi.testclient import TestClient

    model = MagicMock()

    def _predict_then_shutdown(pairs):
        # Lifespan shutdown lands while this worker thread is mid-predict.
        rr._model = None
        return np.array([0.5] * len(pairs))

    model.predict.side_effect = _predict_then_shutdown

    with patch("embedder.reranker.CrossEncoder", return_value=model):
        with TestClient(rr.app, raise_server_exceptions=False) as c:
            r = c.post("/rerank", json={"query": "q", "passages": ["a"], "top_k": 1})

    assert r.status_code == 200, (
        f"got {r.status_code}: the model was resolved on the worker thread after the guard, "
        "so shutdown turned a valid request into a failure"
    )
