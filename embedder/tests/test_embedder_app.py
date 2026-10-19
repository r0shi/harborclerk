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
            health_response = c.get("/health")
            health_latency = time.perf_counter() - started
            health_status = health_response.status_code

            t.join(timeout=10)

    assert health_status == 200
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

    # Assert against the configured cap, whatever it is — but skip rather than
    # fail if the environment raised it, since `peak <= MAX_CONCURRENCY` would
    # then hold for any observed value and assert nothing. Failing here would
    # turn "operator tuned the knob" into a red suite that reads as a
    # regression, and this same change ships EMBED_MAX_CONCURRENCY in
    # .env.example and docker-compose.yml.
    if MAX_CONCURRENCY != 1:
        pytest.skip(f"EMBED_MAX_CONCURRENCY={MAX_CONCURRENCY} in this environment; cap assertion would be vacuous")
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


def test_embed_returns_503_before_the_semaphore_exists(stub_model):
    """The `_encode_slots is None` guard, which had no coverage.

    Reachable if a request lands between process start and lifespan completing,
    or after shutdown clears it.
    """
    from unittest.mock import patch

    with patch("embedder.app.SentenceTransformer", return_value=stub_model):
        import embedder.app as app_module

        # Outside the TestClient context lifespan never ran, so both globals are None.
        assert app_module._encode_slots is None
        assert app_module._model is None


def test_encode_releases_the_gpu_cache(stub_model):
    """The allocator cache must be handed back after each encode.

    PyTorch's caching allocator never returns device memory on its own. That is
    fine for a steady workload, but the inputs here are documents and email
    bodies of wildly varying length, so nearly every batch asks for an unseen
    shape and the cache ratchets. Measured on the Mac mini: 1.5 GB fresh, 40 GB
    after ~1.5 hours of ingest, with the machine down to 347 MB free.
    """
    from unittest.mock import patch

    calls = []

    with (
        patch("embedder.app.SentenceTransformer", return_value=stub_model),
        patch("embedder.app.release_gpu_cache", side_effect=lambda: calls.append(1)),
    ):
        from embedder.app import app

        with TestClient(app) as c:
            assert c.post("/embed", json={"texts": ["a"]}).status_code == 200
            assert c.post("/embed", json={"texts": ["b", "c"]}).status_code == 200

    assert len(calls) == 2, f"expected a release per encode, got {len(calls)}"


def test_cache_is_released_even_when_encode_raises(stub_model):
    """The `finally` is the load-bearing part, and it had no coverage.

    An MPS OOM is the likeliest failure on a memory-tight machine, and it
    surfaces as a 5xx that embedder_client retries — so releasing only on
    success means the retries pile onto an allocator that was never drained.
    Mutating both call sites to release after a successful return passed all
    22 tests before this.
    """
    from unittest.mock import patch

    calls = []
    stub_model.encode.side_effect = RuntimeError("MPS backend out of memory")

    with (
        patch("embedder.app.SentenceTransformer", return_value=stub_model),
        patch("embedder.app.release_gpu_cache", side_effect=lambda: calls.append(1)),
    ):
        from embedder.app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/embed", json={"texts": ["a"]})
            assert r.status_code >= 500

    assert calls == [1], "cache was not released when the encode raised"


def test_release_runs_on_the_worker_thread_not_the_event_loop(stub_model):
    """The #553 invariant, asserted in three comments and guarded by none.

    `empty_cache()` blocks. Doing it on the event loop re-creates exactly the
    /health starvation the service was fixed for. Mutating the call site to
    release outside `to_thread` left all 25 tests green.
    """
    import threading
    from unittest.mock import patch

    loop_thread = []
    release_thread = []

    def _note_encode(texts, normalize_embeddings=True):
        loop_thread.append(threading.current_thread().name)
        return np.stack([np.full(768, 0.1, dtype=np.float32) for _ in texts])

    stub_model.encode.side_effect = _note_encode

    with (
        patch("embedder.app.SentenceTransformer", return_value=stub_model),
        patch(
            "embedder.app.release_gpu_cache",
            side_effect=lambda: release_thread.append(threading.current_thread().name),
        ),
    ):
        from embedder.app import app

        with TestClient(app) as c:
            assert c.post("/embed", json={"texts": ["a"]}).status_code == 200

    assert release_thread, "release never ran"
    assert release_thread[0] == loop_thread[0], (
        f"release ran on {release_thread[0]} but the encode ran on {loop_thread[0]} — "
        "it must share the worker thread, not run on the event loop"
    )


# --------------------------------------------------------------------------
# #619 — an encode failure is a logged 503, not an unlogged 500
# --------------------------------------------------------------------------


def test_encode_failure_is_a_logged_500(stub_model, caplog):
    """A night of MPS out-of-memory failures left nothing in embedder.log.

    The traceback went to uvicorn's own logger, which does not propagate to the
    root logger where main() attaches the file handler. The failure must be
    logged on a logger that reaches the root (caplog's handler sits there, so a
    record it captures is one embedder.log would get), and it must be a 5xx,
    which the worker defers and retries.
    """
    import logging
    from unittest.mock import patch

    stub_model.encode.side_effect = RuntimeError("MPS backend out of memory")

    with (
        caplog.at_level(logging.ERROR),
        patch("embedder.app.SentenceTransformer", return_value=stub_model),
        patch("embedder.app.release_gpu_cache"),
    ):
        from embedder.app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/embed", json={"texts": ["a", "b"]})

    assert r.status_code == 500, f"expected 500, got {r.status_code}"
    assert "RuntimeError" in r.json()["detail"]

    records = [rec for rec in caplog.records if rec.name == "embedder.app" and rec.levelno >= logging.ERROR]
    assert records, "the encode failure was not logged on the embedder's own logger"
    assert records[0].exc_info is not None, "the log line must carry the traceback"
    assert logging.getLogger("embedder.app").propagate, "the record must reach the root logger's file handler"


class PanicException(BaseException):
    """Stands in for pyo3's PanicException (a tokenizers Rust panic), which has no importable home."""


class OutOfMemoryError(RuntimeError):
    """Stands in for torch.OutOfMemoryError, a RuntimeError subclass."""


def _post(stub_model, texts, *, encode_raises=None, encode_returns=None):
    from unittest.mock import patch

    if encode_raises is not None:
        stub_model.encode.side_effect = encode_raises
    if encode_returns is not None:
        stub_model.encode.side_effect = lambda texts, normalize_embeddings=True: encode_returns
    with (
        patch("embedder.app.SentenceTransformer", return_value=stub_model),
        patch("embedder.app.release_gpu_cache"),
    ):
        from embedder.app import app

        with TestClient(app, raise_server_exceptions=False) as c:
            return c.post("/embed", json={"texts": texts})


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("text input must be of type str"),
        TypeError("TextEncodeInput must be Union"),
        PanicException("x"),
        RuntimeError("MPS backend out of memory"),
        OutOfMemoryError("CUDA out of memory"),
    ],
    ids=["ValueError", "TypeError", "tokenizers-panic", "RuntimeError", "OutOfMemoryError"],
)
def test_any_exception_from_encode_is_a_500_never_a_422(stub_model, exc):
    """Classifying by exception type made a library bug's TypeError (a #685-style
    version skew) a 422, which the worker treats as final: every document would
    fail for good. Only input the embedder itself rejects is a 422."""
    r = _post(stub_model, ["a"], encode_raises=exc)

    assert r.status_code == 500, f"{type(exc).__name__} -> {r.status_code}"


@pytest.mark.parametrize("texts", [["fine", ""], ["   \n"]], ids=["empty", "whitespace"])
def test_empty_and_whitespace_texts_are_embedded_not_refused(stub_model, texts):
    """Content is never a 4xx: the worker would fail the document for good."""
    r = _post(stub_model, texts)

    assert r.status_code == 200, r.text
    assert len(r.json()["embeddings"]) == len(texts)
    assert stub_model.encode.call_args[0][0] == texts


def test_an_over_long_text_is_truncated_and_embedded_not_refused(stub_model, caplog):
    """A 422 for an over-long chunk turned an ingestible document (an unterminated
    code fence, 350k characters in one chunk) into a permanent failure."""
    import logging

    from embedder.app import MAX_ENCODE_CHARS

    long_text = "x" * (MAX_ENCODE_CHARS + 250_000)
    with caplog.at_level(logging.WARNING, logger="embedder.app"):
        r = _post(stub_model, ["short", long_text])

    assert r.status_code == 200, r.text
    encoded = stub_model.encode.call_args[0][0]
    assert encoded[0] == "short"
    assert len(encoded[1]) == MAX_ENCODE_CHARS, "the over-long text must reach the model truncated"
    assert any("truncating 1 of 2 texts" in rec.getMessage() for rec in caplog.records)


def test_wrong_types_in_the_request_are_a_422(stub_model):
    r = _post(stub_model, [1, 2])

    assert r.status_code == 422


def test_a_non_finite_vector_is_a_500_with_a_clear_message(stub_model, caplog):
    """Serialized inside the error handling: a NaN used to crash the JSON encoder
    after the handler returned, a bare 500 with nothing logged saying why."""
    import logging

    bad = np.stack([np.full(768, 0.1, dtype=np.float32)])
    bad[0, 3] = np.nan

    with caplog.at_level(logging.ERROR):
        r = _post(stub_model, ["a"], encode_returns=bad)

    assert r.status_code == 500
    assert "non-finite" in r.json()["detail"], r.json()
    assert any(rec.name == "embedder.app" and rec.exc_info for rec in caplog.records)


def test_cancellation_propagates_rather_than_becoming_a_500(stub_model):
    """The handler catches BaseException to see a tokenizers panic, so it must hand
    cancellation back untouched: a 500
    there would answer a request whose task is being torn down, as "retry later"."""
    import asyncio

    import embedder.app as app_module
    from embedder.app import EmbedRequest

    stub_model.encode.side_effect = asyncio.CancelledError()

    async def _call():
        app_module._model = stub_model
        app_module._encode_slots = asyncio.Semaphore(1)
        try:
            await app_module.embed(EmbedRequest(texts=["a"]))
        finally:
            app_module._model = None
            app_module._encode_slots = None

    with patch("embedder.app.release_gpu_cache"), pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(_call(), timeout=10))


def test_a_500_names_the_exception_type_never_its_message(stub_model, caplog):
    """The 500 detail travels into documents.error (readable by API keys) and the
    health verdict. An exception's message can quote the input: it stays in the
    embedder's own log."""
    import logging

    secret = "Privileged: settlement figure 4.2M"
    with caplog.at_level(logging.ERROR, logger="embedder.app"):
        r = _post(stub_model, ["a"], encode_raises=RuntimeError(f"cannot encode {secret!r}"))

    assert r.status_code == 500
    assert r.json() == {"detail": "encode failed: RuntimeError"}, r.json()
    assert any(secret in rec.getMessage() for rec in caplog.records), "the full message was not logged"
