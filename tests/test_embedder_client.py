"""Tests for embedder_client — bounded retry around the embedder service (#552).

The contract these lock down:
  - transient failures (connection, timeout, 5xx, 429) are retried and recover
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


def _ok(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=EMBED_URL, json={"embeddings": [VECTOR]})


# --------------------------------------------------------------------------
# sync path (embed stage)
# --------------------------------------------------------------------------


def test_succeeds_without_retry(httpx_mock: HTTPXMock):
    _ok(httpx_mock)
    assert embed_texts(["hello"], task="passage") == [VECTOR]
    assert len(httpx_mock.get_requests()) == 1


def test_retries_connection_error_then_succeeds(httpx_mock: HTTPXMock):
    """The #552 reproduction: N-1 transient failures, then success."""
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=EMBED_URL)
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=EMBED_URL)
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
    _ok(httpx_mock)
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
