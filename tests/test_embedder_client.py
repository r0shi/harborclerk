"""Tests for embedder_client — bounded retry around the embedder service (#552).

The contract these lock down:
  - transient failures (connection lost, 5xx, 429) are retried and recover
  - a read timeout is NOT retried — the request landed, so retrying duplicates work
  - a 4xx is NOT retried — it will never succeed, so fail fast
  - exhausting the budget raises EmbedderError, never the raw httpx error
"""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from harbor_clerk.embedder_client import EmbedderError, embed_texts, embed_texts_async

EMBED_URL = "http://embedder:8000/embed"
VECTOR = [0.1, 0.2, 0.3]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Collapse backoff so the suite doesn't actually wait ~7s."""
    monkeypatch.setattr("harbor_clerk.embedder_client.time.sleep", lambda _s: None)

    async def _fake_async_sleep(_s):
        return None

    monkeypatch.setattr("harbor_clerk.embedder_client.asyncio.sleep", _fake_async_sleep)


def _ok(httpx_mock: HTTPXMock, n: int = 1) -> None:
    """A success response for `n` texts. The count matters — the client now
    rejects a response whose length doesn't match the request."""
    httpx_mock.add_response(url=EMBED_URL, json={"embeddings": [VECTOR] * n})


# --------------------------------------------------------------------------
# sync path (embed stage)
# --------------------------------------------------------------------------


def test_succeeds_without_retry(httpx_mock: HTTPXMock):
    _ok(httpx_mock)
    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 1


def test_retries_connection_error_then_succeeds(httpx_mock: HTTPXMock):
    """The #552 reproduction: N-1 transient failures, then success."""
    # ReadError/ConnectError, not ReadTimeout: a read timeout means the request
    # landed and is still being worked on, so it is deliberately not retried.
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=EMBED_URL)
    httpx_mock.add_exception(httpx.ReadError("connection reset"), url=EMBED_URL)
    _ok(httpx_mock)

    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 3


def test_retries_5xx_then_succeeds(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=EMBED_URL, status_code=503)
    _ok(httpx_mock)

    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 2


def test_retries_429_then_succeeds(httpx_mock: HTTPXMock):
    """429 means 'later', not 'never' — it belongs in the retryable set."""
    httpx_mock.add_response(url=EMBED_URL, status_code=429)
    _ok(httpx_mock)

    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 2


def test_does_not_retry_4xx(httpx_mock: HTTPXMock):
    """A malformed request will never succeed — one attempt, then give up."""
    httpx_mock.add_response(url=EMBED_URL, status_code=422, json={"detail": "bad"})

    with pytest.raises(EmbedderError, match="rejected request: HTTP 422"):
        embed_texts(["hello"], task="passage")
    assert len(httpx_mock.get_requests()) == 1


def test_raises_after_exhausting_attempts(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    with pytest.raises(EmbedderError, match="after 3/3 attempts"):
        embed_texts(["hello"], task="passage", max_attempts=3)
    assert len(httpx_mock.get_requests()) == 3


def test_sends_expected_payload(httpx_mock: HTTPXMock):
    _ok(httpx_mock, n=2)
    embed_texts(["a", "b"], task="passage")

    import json

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body == {"texts": ["a", "b"], "task": "passage"}


# --------------------------------------------------------------------------
# async path (query embedding in search)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_succeeds_without_retry(httpx_mock: HTTPXMock):
    _ok(httpx_mock)
    assert await embed_texts_async(["q"], task="query") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_async_retries_then_succeeds(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=EMBED_URL)
    httpx_mock.add_response(url=EMBED_URL, status_code=502)
    _ok(httpx_mock)

    assert await embed_texts_async(["q"], task="query") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_async_does_not_retry_4xx(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=EMBED_URL, status_code=400)

    with pytest.raises(EmbedderError):
        await embed_texts_async(["q"], task="query")
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_async_raises_after_exhausting_attempts(httpx_mock: HTTPXMock):
    for _ in range(2):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    with pytest.raises(EmbedderError, match="after 2/2 attempts"):
        await embed_texts_async(["q"], task="query", max_attempts=2)
    assert len(httpx_mock.get_requests()) == 2


def test_retry_window_outlasts_an_embedder_restart():
    """The budget must cover a full restart, which is the failure it exists for.

    Measured on the Mac mini: the embedder is unreachable for ~7.2s across a
    restart. The original 4-attempt default gave a 1.75-3.5s window and a live
    ingest duly showed a document failing across a restart it should have
    absorbed.

    The requirement carries margin rather than sitting just above the one
    measurement, so a slower disk or a larger model doesn't silently make the
    budget too small again.
    """
    from harbor_clerk.embedder_client import (
        _BACKOFF_BASE_SECONDS,
        _BACKOFF_CAP_SECONDS,
        DEFAULT_MAX_ATTEMPTS,
    )

    OBSERVED_RESTART_SECONDS = 7.2
    REQUIRED_WINDOW = OBSERVED_RESTART_SECONDS * 1.5

    # Jitter is half-to-full, so the guaranteed window is the low end.
    minimum_window = sum(
        min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))) * 0.5
        for attempt in range(1, DEFAULT_MAX_ATTEMPTS)
    )

    assert minimum_window >= REQUIRED_WINDOW, (
        f"guaranteed retry window is {minimum_window:.2f}s; an embedder restart "
        f"takes ~{OBSERVED_RESTART_SECONDS}s and this needs {REQUIRED_WINDOW:.2f}s of margin"
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)  # spare mocks prove the retry did NOT happen
async def test_query_path_does_not_retry(httpx_mock: HTTPXMock):
    """search._embed_query must fall back instantly, not sit through a backoff.

    hybrid_search already degrades to lexical-only on any embedder failure, so
    retrying returns an identical result more slowly. Measured against an
    unreachable embedder before this was fixed: 11.14s per search with the stage
    budget vs 0.05s with none — on /api/search, kb_search and find_all alike.

    Asserts the request count, not elapsed time: the autouse _no_sleep fixture
    no-ops asyncio.sleep, so a timing assertion here could never fail.
    """
    from harbor_clerk.search import _embed_query

    for _ in range(10):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    with pytest.raises(EmbedderError):
        await _embed_query("anything")

    assert len(httpx_mock.get_requests()) == 1, (
        f"query path made {len(httpx_mock.get_requests())} requests; it must not retry"
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)  # spare mocks prove the retry did NOT happen
async def test_hybrid_search_still_degrades_to_lexical_when_embedding_fails(db_session, httpx_mock: HTTPXMock):
    """The contract that makes max_attempts=1 safe.

    Everything above depends on hybrid_search swallowing the embedder failure
    and returning FTS-only results. Nothing asserted it, so narrowing that
    `except` later would break search with no failing test.
    """
    from harbor_clerk.search import hybrid_search

    for _ in range(10):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    result = await hybrid_search(db_session, "anything", k=5)
    assert result is not None  # returned rather than raised — that is the contract


def test_read_timeout_is_not_retried(httpx_mock: HTTPXMock):
    """A read timeout means the request landed and the server is still working.

    The embedder holds one model at a concurrency of 1, and a client-side
    timeout neither cancels the in-flight encode nor lets the server see the
    disconnect — so a retry queues a *second* encode behind the first rather
    than replacing it. With a 7-attempt budget a batch that merely runs long
    would cost seven duplicated encodes, turning a slow embedder into an
    overloaded one.
    """
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=EMBED_URL)

    with pytest.raises(EmbedderError, match="ReadTimeout"):
        embed_texts(["hello"], task="passage")
    assert len(httpx_mock.get_requests()) == 1


def test_connection_errors_are_still_retried(httpx_mock: HTTPXMock):
    """The restart case must keep its full budget — that is the whole point."""
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=EMBED_URL)
    httpx_mock.add_exception(httpx.ReadError("reset"), url=EMBED_URL)
    _ok(httpx_mock)

    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 3


def test_non_json_200_becomes_an_embedder_error(httpx_mock: HTTPXMock):
    """A proxy error page with a 200 used to escape as a raw JSONDecodeError."""
    httpx_mock.add_response(url=EMBED_URL, status_code=200, text="<html>gateway</html>")

    with pytest.raises(EmbedderError, match="non-JSON"):
        embed_texts(["hello"], task="passage")


def test_unexpected_json_shape_becomes_an_embedder_error(httpx_mock: HTTPXMock):
    """...and an unexpected shape used to escape as a raw KeyError."""
    httpx_mock.add_response(url=EMBED_URL, json={"vectors": []})

    with pytest.raises(EmbedderError, match="embeddings"):
        embed_texts(["hello"], task="passage")


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)  # spare mocks prove the retry did NOT happen
def test_deadline_stops_retrying_and_reports_the_real_attempt_count(httpx_mock: HTTPXMock, monkeypatch):
    """Exiting via the deadline must not claim the full attempt budget was used.

    That string lands in `ingestion_jobs.error`; reporting "7/7 attempts" for 2
    sends an operator looking for a retry storm that never happened.
    """
    for _ in range(10):
        httpx_mock.add_exception(httpx.ConnectError("down"), url=EMBED_URL)

    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr("harbor_clerk.embedder_client.time.monotonic", lambda: next(ticks))

    with pytest.raises(EmbedderError) as excinfo:
        embed_texts(["hello"], task="passage", deadline_seconds=10.0)

    assert "after 2/7 attempts" in str(excinfo.value), str(excinfo.value)
    assert len(httpx_mock.get_requests()) == 2


def test_short_embeddings_list_fails_loudly(httpx_mock: HTTPXMock):
    """A wrong-length response must not silently lose chunks.

    `run_embed` zips the result with its batch, and zip truncates. A short list
    would leave the unpaired chunks at embedding=NULL while progress counted the
    whole batch and the document went `ready` — invisible to vector search
    forever, with nothing recorded to say a reprocess is needed.
    """
    httpx_mock.add_response(url=EMBED_URL, json={"embeddings": [VECTOR]})

    with pytest.raises(EmbedderError, match="1 vectors for 3 texts"):
        embed_texts(["a", "b", "c"], task="passage")


def test_null_embeddings_becomes_an_embedder_error(httpx_mock: HTTPXMock):
    """`{"embeddings": null}` used to pass through and raise a raw TypeError
    out of the stage, defeating the module's only-EmbedderError contract."""
    httpx_mock.add_response(url=EMBED_URL, json={"embeddings": None})

    with pytest.raises(EmbedderError, match="expected a list"):
        embed_texts(["a"], task="passage")


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_errors_never_render_a_dangling_colon(httpx_mock: HTTPXMock):
    """Every retryable transport exception stringifies empty when raised bare.

    `f"{type(exc).__name__}: {exc}"` then yields "ConnectTimeout: ", and that
    string reaches `ingestion_jobs.error` and `documents.error`, which the
    document detail page renders verbatim. This module was written in the same
    window as error_text and was the one file its migration missed.
    """
    for exc in (
        httpx.ConnectTimeout(""),
        httpx.ConnectError(""),
        httpx.PoolTimeout(""),
        httpx.ReadError(""),
        httpx.RemoteProtocolError(""),
    ):
        httpx_mock.reset()
        for _ in range(8):
            httpx_mock.add_exception(exc, url=EMBED_URL)
        with pytest.raises(EmbedderError) as excinfo:
            embed_texts(["hello"], task="passage", max_attempts=2)
        rendered = str(excinfo.value)
        assert type(exc).__name__ in rendered, rendered
        assert ": " not in rendered.rstrip().removesuffix(type(exc).__name__) or not rendered.rstrip().endswith(":"), (
            f"dangling colon in {rendered!r}"
        )
        assert not rendered.rstrip().endswith(":"), f"dangling colon in {rendered!r}"
