"""Tests for the embedder FastAPI app.

These tests use a stub model loader to avoid downloading Granite-R2 weights.
Real-model tests live in tests/test_embedder_granite.py behind the
``requires_models`` marker.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_model():
    """Stub SentenceTransformer that returns deterministic 768-dim vectors."""
    model = MagicMock()
    model.get_sentence_embedding_dimension.return_value = 768

    def encode(texts, normalize_embeddings=True):
        # Return a deterministic 768-dim vector per text
        return np.stack([np.full(768, 0.1, dtype=np.float32) for _ in texts])

    model.encode.side_effect = encode
    return model


@pytest.fixture
def client(stub_model):
    with patch("embedder.app.SentenceTransformer", return_value=stub_model):
        from embedder.app import app

        with TestClient(app) as c:
            yield c


def test_embed_returns_768_dim(client):
    r = client.post("/embed", json={"texts": ["hello world"]})
    assert r.status_code == 200
    body = r.json()
    assert body["dimensions"] == 768
    assert len(body["embeddings"][0]) == 768


def test_embed_ignores_task_param(client, stub_model):
    """task=query must NOT add 'query: ' prefix when embed_needs_prefix is False (Granite default)."""
    client.post("/embed", json={"texts": ["hello"], "task": "query"})
    encoded_texts = stub_model.encode.call_args[0][0]
    assert encoded_texts == ["hello"], f"task param should not modify texts; got {encoded_texts}"


def test_embed_task_query_still_accepted(client):
    """Backward compat: workers send task=passage; must not 422."""
    r = client.post("/embed", json={"texts": ["hello"], "task": "passage"})
    assert r.status_code == 200
