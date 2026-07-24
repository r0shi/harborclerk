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


# --------------------------------------------------------------------------
# #553 — the encode must not block the event loop
# --------------------------------------------------------------------------


def test_health_stays_responsive_while_encode_is_running(stub_model):
    """The regression that caused the restart storm.

    `SentenceTransformer.encode` is blocking and GPU-bound. When it ran
    directly on the event loop, /health could not be served for the whole
    encode — measured at 3.3s for a 64-text batch against the real model. The
    macOS supervisor probes with a 3s timeout and restarts after six
    consecutive failures, so sustained ingest restarted the embedder
    mid-flight and failed every in-flight request.

    Here the stub blocks for 1.5s; /health must still answer promptly.
    """
    import threading
    import time

    encode_started = threading.Event()

    def slow_encode(texts, normalize_embeddings=True):
        encode_started.set()
        time.sleep(1.5)
        return np.stack([np.full(768, 0.1, dtype=np.float32) for _ in texts])

    stub_model.encode.side_effect = slow_encode

    with patch("embedder.app.SentenceTransformer", return_value=stub_model):
        from embedder.app import app

        with TestClient(app) as c:
            embed_done = threading.Event()

            def do_embed():
                c.post("/embed", json={"texts": ["a", "b"]})
                embed_done.set()

            t = threading.Thread(target=do_embed)
            t.start()
            assert encode_started.wait(timeout=5), "encode never started"

            started = time.perf_counter()
            r = c.get("/health")
            health_latency = time.perf_counter() - started

            t.join(timeout=10)

    assert r.status_code == 200
    # The supervisor's probe timeout is 3s; anything near the encode duration
    # means the loop was blocked again.
    assert health_latency < 1.0, f"/health blocked for {health_latency:.2f}s — event loop is blocked by encode"


def test_concurrent_encodes_are_bounded(stub_model):
    """Concurrency is gated so N workers degrade to slower, not unstable.

    One shared model on one GPU: admitting every caller at once multiplies
    memory without improving throughput.
    """
    import threading

    in_flight = 0
    peak = 0
    completed = 0
    lock = threading.Lock()

    def counting_encode(texts, normalize_embeddings=True):
        nonlocal in_flight, peak, completed
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            import time

            time.sleep(0.2)
            return np.stack([np.full(768, 0.1, dtype=np.float32) for _ in texts])
        finally:
            with lock:
                in_flight -= 1
                completed += 1

    stub_model.encode.side_effect = counting_encode

    statuses = []

    with patch("embedder.app.SentenceTransformer", return_value=stub_model):
        from embedder.app import app

        with TestClient(app) as c:

            def _post():
                statuses.append(c.post("/embed", json={"texts": ["x"]}).status_code)

            threads = [threading.Thread(target=_post) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

    from embedder.app import MAX_CONCURRENCY

    # Without these, six requests that all 503 look identical to six that queue
    # politely: peak stays 0 and the cap assertion passes vacuously.
    assert statuses == [200] * 6, f"expected six successful encodes, got {statuses}"
    assert completed == 6, f"only {completed} encodes ran"  # counted under the lock, not via MagicMock
    assert peak >= 1, "no encode was observed; the test proved nothing"

    # Pin the cap rather than comparing against whatever the environment set:
    # with EMBED_MAX_CONCURRENCY>1 in the env, `peak <= MAX_CONCURRENCY` would
    # hold for any observed value and assert nothing.
    assert MAX_CONCURRENCY == 1, "test assumes the default cap; EMBED_MAX_CONCURRENCY is set in this environment"
    assert peak == 1, f"{peak} concurrent encodes exceeded the cap of 1"


def test_max_concurrency_env_is_total():
    """A junk value must not stop the service binding.

    MAX_CONCURRENCY is read at import, so `int()` raising there means uvicorn
    never binds — on the very service this module's fix exists to keep alive.
    An unset compose passthrough and a bare `NAME=` in .env both arrive as "".
    """
    import os
    from unittest.mock import patch

    from embedder.app import _positive_int_env

    for raw in ("", "auto", "  ", "-3", "0"):
        with patch.dict(os.environ, {"EMBED_MAX_CONCURRENCY": raw}):
            assert _positive_int_env("EMBED_MAX_CONCURRENCY", 1) >= 1

    with patch.dict(os.environ, {"EMBED_MAX_CONCURRENCY": "4"}):
        assert _positive_int_env("EMBED_MAX_CONCURRENCY", 1) == 4

    # The module-level value must come from the helper. A refactor back to a
    # bare int(os.environ[...]) would keep the cases above green.
    import embedder.app

    assert embedder.app.MAX_CONCURRENCY >= 1
    assert isinstance(embedder.app.MAX_CONCURRENCY, int)
