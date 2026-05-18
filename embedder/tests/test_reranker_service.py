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
