"""The embedder evidence health reports: what counts as busy, and what it publishes.

/system/health is unauthenticated, so a failure's detail is bounded wherever it
comes from; and a timeout counts as busy only when the request reached the
embedder (a read or write timeout), not when it never got there.
"""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from harbor_clerk import query_embed_health
from harbor_clerk.embedder_client import EmbedderError, embed_texts_async
from harbor_clerk.error_text import HEALTH_DETAIL_LIMIT

EMBED_URL = "http://embedder:8000/embed"


@pytest.fixture(autouse=True)
def _fresh_evidence(monkeypatch):
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("harbor_clerk.embedder_client.asyncio.sleep", _no_sleep)
    query_embed_health.reset()
    yield
    query_embed_health.reset()


async def _classify(httpx_mock: HTTPXMock, exc: Exception) -> str:
    httpx_mock.add_exception(exc, url=EMBED_URL)
    with pytest.raises(EmbedderError) as excinfo:
        await embed_texts_async(["q"], task="query", max_attempts=1)
    return query_embed_health.classify_embed_error(excinfo.value)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout("slow"), "busy"),
        (httpx.WriteTimeout("slow"), "busy"),
        (httpx.PoolTimeout("no connection free"), "failure"),
        (httpx.ConnectTimeout("unreachable"), "failure"),
        (httpx.ConnectError("refused"), "failure"),
    ],
    ids=["read-timeout", "write-timeout", "pool-timeout", "connect-timeout", "connect-error"],
)
async def test_only_a_landed_request_that_timed_out_is_busy(httpx_mock: HTTPXMock, exc, expected):
    """Through the real client: a read or write timeout means the embedder has the
    request and is still working; a pool or connect timeout means it was never
    reached, which is unavailable, not busy."""
    assert await _classify(httpx_mock, exc) == expected


async def test_a_4xx_is_rejected_not_evidence(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=EMBED_URL, status_code=422, json={"detail": "bad"})
    with pytest.raises(EmbedderError) as excinfo:
        await embed_texts_async(["q"], task="query", max_attempts=1)

    assert query_embed_health.classify_embed_error(excinfo.value) == "rejected"


def test_a_failure_detail_is_bounded_for_the_unauthenticated_health_route():
    long_detail = "ConnectError: " + "x" * 5000

    query_embed_health.record_query_embed_outcome(httpx.ConnectError("x"), long_detail)
    status, _note = query_embed_health.verdict()

    assert status.startswith("error: search's query embed failed")
    assert len(status) < HEALTH_DETAIL_LIMIT + 50, f"unbounded detail: {len(status)} chars"
    assert status.endswith("…")


def test_every_failure_path_is_bounded():
    query_embed_health.record_failure("y" * 5000)

    status, _ = query_embed_health.verdict()
    assert len(status) <= len("error: ") + HEALTH_DETAIL_LIMIT + 1


async def test_searchs_failure_reaches_health_as_type_and_status_only(httpx_mock: HTTPXMock):
    """search records a failed query embed on the unauthenticated /system/health.
    It recorded the error's whole text, which carries the embedder's detail: an
    exception message that can quote the request. Now the type and status only."""
    import harbor_clerk.search as search_mod
    from harbor_clerk.api.routes import system

    secret = "Privileged: settlement figure 4.2M"
    httpx_mock.add_response(
        url=EMBED_URL, status_code=500, json={"detail": f"encode failed: RuntimeError: {secret}"}, is_reusable=True
    )

    with pytest.raises(EmbedderError):
        await search_mod._embed_query("what was the settlement")

    status, note = await system._embedder_check("http://embedder:8000")  # fresh evidence: no probe
    assert status == "error: search's query embed failed: EmbedderUnavailableError (HTTP 500)", status
    assert "Privileged" not in status and "settlement" not in status and not note
