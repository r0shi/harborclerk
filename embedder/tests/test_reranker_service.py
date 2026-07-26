"""Tests for the reranker FastAPI app, using a stub CrossEncoder."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


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


def test_rerank_binds_the_model_before_the_executor_hop(monkeypatch):
    """Shutdown between the guard and the worker thread must not 500.

    `run_in_executor(None, _model.predict, pairs)` bound the method eagerly on
    the event loop. A closure that reads the global instead resolves it on the
    worker thread — after an await — so lifespan shutdown can null it in
    between, turning the intended 503 into AttributeError.
    """
    import inspect

    import embedder.reranker as rr

    src = inspect.getsource(rr.rerank)
    assert "model = _model" in src, "rerank must bind _model before handing work to the executor"
    assert "_model.predict(" not in src, "predict must be called on the bound local, not the global"
