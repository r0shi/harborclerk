"""Tests for /api/system/* endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFolder
from tests.conftest import auth_header

_READY_GATE_STAGES = (JobStage.extract, JobStage.chunk, JobStage.entities, JobStage.embed, JobStage.finalize)


def _add_done_gate_jobs(db_session, doc_id, *, exclude: set[JobStage] | None = None):
    exclude = exclude or set()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=doc_id,
                stage=stage,
                status=JobStatus.done,
            )
            for stage in _READY_GATE_STAGES
            if stage not in exclude
        ]
    )


async def test_setup_status_no_users(client):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is True


async def test_setup_status_with_users(client, admin_user):
    resp = await client.get("/api/system/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is False


async def test_ping_liveness_is_unauthenticated_and_dependency_free(client):
    """The liveness probe returns 200 {"status": "ok"} with no auth and without
    touching Postgres/Tika/storage — the macOS HealthChecker relies on this to
    distinguish a zombied API listener from a degraded backend."""
    resp = await client.get("/api/system/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_check(client):
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["checks"]["postgres"] == "ok"


@pytest.fixture(autouse=True)
async def _fresh_embedder_evidence(monkeypatch):
    """Health reports the embedder evidence search and the background probe leave
    (query_embed_health); no test may read another's, or inherit its probe."""
    import asyncio
    import contextlib

    from harbor_clerk import query_embed_health
    from harbor_clerk.api.routes import system

    monkeypatch.setattr(system, "_embedder_probe", None)
    query_embed_health.reset()
    yield
    task = system._embedder_probe_task()
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    query_embed_health.reset()


def _stub_embedder(
    monkeypatch,
    *,
    health_status: int = 200,
    embed_status: int = 200,
    embed_json=None,
    embed_delay: float = 0.0,
    others_ok: bool = False,
):
    """Answer the embedder's /health and /embed; every other request goes through.

    others_ok also answers Tika and the reranker 200, so the overall status
    depends on the embedder alone. Returns the list of embedder URLs requested,
    in order. Resolved from config: CI overrides EMBEDDER_URL (see
    test_health_check_names_a_timing_out_embedder).
    """
    import httpx

    from harbor_clerk.config import get_settings

    settings = get_settings()
    base = settings.embedder_url
    calls: list[str] = []
    real_get = httpx.AsyncClient.get
    real_post = httpx.AsyncClient.post
    others = (f"{settings.tika_url}/tika", f"{settings.reranker_url}/health")

    async def _get(self, url, *args, **kwargs):
        if str(url) == f"{base}/health":
            calls.append(str(url))
            return httpx.Response(health_status, json={"status": "ok"}, request=httpx.Request("GET", str(url)))
        if others_ok and str(url) in others:
            return httpx.Response(200, json={}, request=httpx.Request("GET", str(url)))
        return await real_get(self, url, *args, **kwargs)

    async def _post(self, url, *args, **kwargs):
        if str(url) == f"{base}/embed":
            calls.append(str(url))
            if embed_delay:
                import asyncio

                await asyncio.sleep(embed_delay)
            body = embed_json if embed_json is not None else {"embeddings": [[0.1] * 4], "dimensions": 4}
            if embed_status != 200:
                body = {"detail": "Internal Server Error"}
            return httpx.Response(embed_status, json=body, request=httpx.Request("POST", str(url)))
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return calls


async def _await_probe():
    """Wait for the background probe health started, bounded."""
    import asyncio

    from harbor_clerk.api.routes import system

    task = system._embedder_probe_task()
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)


async def _settled_health(client) -> dict:
    """Health as it reads once its background probe has landed: fetch, wait, fetch."""
    await client.get("/api/system/health")
    await _await_probe()
    return (await client.get("/api/system/health")).json()


async def test_health_check_reports_embedder(client, monkeypatch):
    calls = _stub_embedder(monkeypatch)

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == "ok"
    assert any(c.endswith("/embed") for c in calls), "health must exercise /embed, not only /health"


async def test_health_never_waits_on_a_slow_probe(client, monkeypatch):
    """The route is polled every 2 s and unauthenticated; it reports what it knows
    and probes in the background. Before the first verdict that is "checking",
    which does not degrade the overall status."""
    import time

    from harbor_clerk.api.routes import system

    calls = _stub_embedder(monkeypatch, embed_delay=5.0, others_ok=True)

    started = time.monotonic()
    data = (await client.get("/api/system/health")).json()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"health waited {elapsed:.2f}s on the embedder"
    assert data["checks"]["embedder"] == "checking"
    assert data["notes"]["embedder"].startswith("checking")
    assert data["status"] == "healthy", "a probe still running is not a failure"
    assert system._embedder_probe_task() is not None and not system._embedder_probe_task().done()
    assert calls, "the background probe never started"


async def test_health_check_degrades_when_embed_fails_though_health_answers(client, monkeypatch):
    """#685: the embedder's /health said 200 all night while every /embed was a 500.

    The app looked healthy while search ran keyword-only. Health must run a real
    embed, so a failing /embed reads as degraded.
    """
    _stub_embedder(monkeypatch, health_status=200, embed_status=500)

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == "error: /embed returned HTTP 500"
    assert data["status"] == "degraded"


async def test_health_check_degrades_when_embed_returns_no_vector(client, monkeypatch):
    _stub_embedder(monkeypatch, embed_json={"embeddings": []})

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == "error: /embed returned no vector"
    assert data["status"] == "degraded"


async def test_fresh_evidence_needs_no_further_probe(client, monkeypatch):
    """The probe runs a real encode; a burst of health fetches must cost one."""
    calls = _stub_embedder(monkeypatch)

    await _settled_health(client)
    for _ in range(3):
        assert (await client.get("/api/system/health")).json()["checks"]["embedder"] == "ok"
    await _await_probe()

    assert len([c for c in calls if c.endswith("/embed")]) == 1, f"probed again on fresh evidence: {calls}"


async def test_concurrent_health_calls_share_one_probe(monkeypatch):
    """The route is unauthenticated and the Mac app polls it every 2 s: N concurrent
    callers must cost one encode, not N (a reviewer reproduced 5 from 5)."""
    import asyncio

    from harbor_clerk.api.routes import system
    from harbor_clerk.config import get_settings

    calls = _stub_embedder(monkeypatch, embed_delay=0.3)

    results = await asyncio.wait_for(
        asyncio.gather(*(system._embedder_check(get_settings().embedder_url) for _ in range(5))), timeout=10
    )
    await _await_probe()

    assert [status for status, _ in results] == ["checking"] * 5
    assert len([c for c in calls if c.endswith("/embed")]) == 1, f"{calls}"
    assert await system._embedder_check(get_settings().embedder_url) == ("ok", None)


async def test_a_busy_embedder_is_ok_not_degraded(client, monkeypatch, caplog):
    """During a heavy ingest the probe queues behind batches in the one-at-a-time
    embedder. Timing out there is the embedder working, not failing."""
    import logging

    monkeypatch.setattr("harbor_clerk.api.routes.system.EMBEDDER_PROBE_EMBED_TIMEOUT_SECONDS", 0.2)
    _stub_embedder(monkeypatch, embed_delay=3.0)

    with caplog.at_level(logging.INFO, logger="harbor_clerk.api.routes.system"):
        data = await _settled_health(client)

    assert data["checks"]["embedder"] == "ok"
    assert data["notes"]["embedder"].startswith("busy")
    embedder_errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "Embedder" in r.getMessage()]
    assert not embedder_errors, f"a busy embedder was logged as a failure: {embedder_errors}"


async def test_a_saturated_single_slot_embedder_never_reads_as_down(monkeypatch):
    """The sixth review's repro, in fake time: a one-slot embedder (like
    embedder/app.py's Semaphore(1)) kept saturated by three ingest workers, polled
    by health for 300 s. Every probe queues behind batches and times out, while
    every batch succeeds. That is an embedder at work: the verdict must stay "ok"
    with a busy note throughout, never an error. Escalating timeouts to "not
    answering" made exactly this ingest read as an outage."""
    import asyncio

    import httpx

    from harbor_clerk import query_embed_health
    from harbor_clerk.api.routes import system
    from harbor_clerk.config import get_settings

    base = get_settings().embedder_url
    slot = asyncio.Semaphore(1)  # one encode at a time
    batches_ok = 0
    clock = [10_000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    monkeypatch.setattr(system, "EMBEDDER_PROBE_EMBED_TIMEOUT_SECONDS", 0.05)
    real_get = httpx.AsyncClient.get

    async def _get(self, url, *args, **kwargs):
        if str(url) == f"{base}/health":  # served on the loop, never blocked by encodes
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", str(url)))
        return await real_get(self, url, *args, **kwargs)

    async def _post(self, url, *args, json=None, **kwargs):
        async with slot:  # the probe's one text waits here behind the batches
            await asyncio.sleep(0.02)
        return httpx.Response(200, json={"embeddings": [[0.1] * 4]}, request=httpx.Request("POST", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    async def _ingest_worker():
        nonlocal batches_ok
        while True:
            async with slot:
                await asyncio.sleep(0.02)  # a 64-text batch
            batches_ok += 1

    workers = [asyncio.create_task(_ingest_worker()) for _ in range(3)]
    verdicts: list[tuple[str, str | None]] = []
    try:
        for _ in range(150):  # 300 s of fake time, health polled every 2 s
            clock[0] += 2
            verdicts.append(await system._embedder_check(base))
            task = system._embedder_probe_task()
            if task is not None and not task.done():
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    assert batches_ok > 0, "premise: the ingest kept the embedder busy and succeeding"
    assert not [v for v in verdicts if v[0].startswith("error")], [v for v in verdicts if v[0].startswith("error")][:3]
    busy = [note for status, note in verdicts if status == "ok" and note and note.startswith("busy")]
    assert busy, "the probe never timed out behind the batches: the scenario did not saturate"
    assert "busy for" in busy[-1], f"the note should say for how long: {busy[-1]}"


def test_isolated_busy_events_are_not_one_busy_spell(monkeypatch):
    """Two timeouts an hour apart, with nothing between, are not an hour of being busy."""
    from harbor_clerk import query_embed_health

    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])

    query_embed_health.record_busy()
    clock[0] += 3600
    query_embed_health.record_busy()

    assert query_embed_health.verdict() == ("ok", query_embed_health.BUSY_NOTE)


def test_busy_events_inside_the_gap_say_for_how_long_and_stay_ok(monkeypatch):
    from harbor_clerk import query_embed_health

    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    gap = query_embed_health.BUSY_STREAK_MAX_GAP_SECONDS

    query_embed_health.record_busy()
    for _ in range(10):  # ten minutes of consecutive timeouts
        clock[0] += gap - 1
        query_embed_health.record_busy()

    status, note = query_embed_health.verdict()
    assert status == "ok", status
    assert note == f"{query_embed_health.BUSY_NOTE}; busy for {10 * (gap - 1):.0f}s", note


def _closed_port_url() -> str:
    """An http URL on localhost where nothing listens: every connection is refused."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def test_a_dead_embedder_polled_every_90s_reads_as_an_error_every_time(monkeypatch):
    """The ninth review's repro: a refused connection, polled less than once a minute.
    Every poll found the evidence over the maximum age, which read "checking"
    (healthy) while the fresh probe ran in the background: a dead embedder
    reported healthy on every call. Old evidence now waits for its probe."""
    from harbor_clerk import query_embed_health
    from harbor_clerk.api.routes import system

    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    dead = _closed_port_url()

    assert (await system._embedder_check(dead))[0] == "checking"  # no evidence yet: answers at once
    await _await_probe()

    for poll in range(4):
        clock[0] += 90
        status, note = await system._embedder_check(dead)
        assert status.startswith("error"), f"poll {poll}: a dead embedder read {status!r} ({note})"
        assert note is None, f"poll {poll}: not this poll's probe but old evidence: {note}"


async def test_an_old_failure_stays_an_error_while_a_slow_probe_runs(client, monkeypatch):
    """Past the wait the probe is still running: an old failure is reported as the
    error it was, with its age, never as "checking"."""
    from harbor_clerk import query_embed_health

    monkeypatch.setattr("harbor_clerk.api.routes.system.EMBEDDER_STALE_PROBE_WAIT_SECONDS", 0.2)
    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    query_embed_health.record_failure("/embed returned HTTP 500")
    _stub_embedder(monkeypatch, embed_delay=1.0)

    clock[0] += query_embed_health.EMBEDDER_MAX_EVIDENCE_AGE_SECONDS + 1
    data = (await client.get("/api/system/health")).json()
    await _await_probe()

    assert data["checks"]["embedder"] == "error: /embed returned HTTP 500", data["checks"]["embedder"]
    assert "61s old" in data["notes"]["embedder"], data["notes"]
    assert data["status"] == "degraded"


async def test_an_old_ok_reads_checking_while_a_slow_probe_runs(client, monkeypatch):
    """/health served an hour-old "ok" for a dead embedder. Past the maximum
    evidence age it waits for the fresh probe; one still running past the wait
    turns an old "ok" into "checking", with the age, and its verdict lands next."""
    from harbor_clerk import query_embed_health

    monkeypatch.setattr("harbor_clerk.api.routes.system.EMBEDDER_STALE_PROBE_WAIT_SECONDS", 0.2)
    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    query_embed_health.record_success()
    calls = _stub_embedder(monkeypatch, embed_status=500, embed_delay=1.0)

    clock[0] += query_embed_health.EMBEDDER_MAX_EVIDENCE_AGE_SECONDS + 1
    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "checking", data["checks"]["embedder"]
    assert "61s old" in data["notes"]["embedder"], data["notes"]
    await _await_probe()
    assert calls, "no fresh probe was started"
    settled = (await client.get("/api/system/health")).json()
    assert settled["checks"]["embedder"] == "error: /embed returned HTTP 500"


async def test_old_evidence_reports_the_probe_that_lands_within_the_wait(client, monkeypatch):
    """An old "ok" and an embedder that now fails fast: the answer is the fresh
    failure, on this call, not "checking"."""
    from harbor_clerk import query_embed_health

    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    query_embed_health.record_success()
    _stub_embedder(monkeypatch, embed_status=500)

    clock[0] += 90
    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "error: /embed returned HTTP 500", data["checks"]["embedder"]
    assert "notes" not in data or "embedder" not in data["notes"], data.get("notes")


async def test_a_verdict_within_the_maximum_age_is_still_served(client, monkeypatch):
    from harbor_clerk import query_embed_health

    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    query_embed_health.record_success()
    _stub_embedder(monkeypatch)

    clock[0] += query_embed_health.EMBEDDER_MAX_EVIDENCE_AGE_SECONDS - 1
    data = (await client.get("/api/system/health")).json()
    await _await_probe()

    assert data["checks"]["embedder"] == "ok"


@pytest.mark.parametrize("timeout_exc", ["PoolTimeout", "ConnectTimeout"])
async def test_a_probe_that_never_reaches_embed_is_degraded_not_busy(client, monkeypatch, timeout_exc):
    """A pool or connect timeout means the request never landed: unavailable, as
    classify_embed_error has it for search, not the busy of a read timeout."""
    import httpx

    from harbor_clerk.config import get_settings

    _stub_embedder(monkeypatch)
    real_post = httpx.AsyncClient.post
    embed_url = f"{get_settings().embedder_url}/embed"

    async def _post(self, url, *args, **kwargs):
        if str(url) == embed_url:
            raise getattr(httpx, timeout_exc)("")
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == f"error: {timeout_exc}"
    assert "embedder" not in data["notes"]


async def test_a_503_from_embed_is_degraded(client, monkeypatch):
    _stub_embedder(monkeypatch, embed_status=503)

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == "error: /embed returned HTTP 503"
    assert data["status"] == "degraded"
    assert "embedder" not in data["notes"]


def _raise_from(cause: BaseException, exc_type=None, message: str = "embedder call failed", status_code=None):
    from harbor_clerk.embedder_client import EmbedderError

    async def _failing(texts, **kwargs):
        raise (exc_type or EmbedderError)(message, status_code=status_code) from cause

    return _failing


async def test_a_recent_failed_query_embed_is_degraded_even_if_the_probe_passes(client, monkeypatch):
    """Search's own failure is better evidence than a one-text probe that happens to pass."""
    import httpx

    import harbor_clerk.search as search_mod
    from harbor_clerk.embedder_client import EmbedderUnavailableError

    monkeypatch.setattr(
        search_mod,
        "embed_texts_async",
        _raise_from(
            httpx.ConnectError("refused"), EmbedderUnavailableError, "embedder call failed: HTTP 500", status_code=500
        ),
    )
    with pytest.raises(EmbedderUnavailableError):
        await search_mod._embed_query("lighthouse")
    calls = _stub_embedder(monkeypatch)  # the probe would pass

    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"].startswith("error: search's query embed failed"), data["checks"]["embedder"]
    assert "EmbedderUnavailableError (HTTP 500)" in data["checks"]["embedder"]  # type and status only
    assert data["status"] == "degraded"
    assert not calls, "with search's fresh evidence in hand, no probe is needed"


async def test_a_timed_out_query_embed_is_busy_not_degraded(client, monkeypatch):
    import httpx

    import harbor_clerk.search as search_mod
    from harbor_clerk.embedder_client import EmbedderUnavailableError

    monkeypatch.setattr(
        search_mod, "embed_texts_async", _raise_from(httpx.ReadTimeout("slow"), EmbedderUnavailableError)
    )
    with pytest.raises(EmbedderUnavailableError):
        await search_mod._embed_query("lighthouse")
    calls = _stub_embedder(monkeypatch)

    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "ok"
    assert data["notes"]["embedder"].startswith("busy")
    assert not calls


async def test_a_rejected_query_embed_is_not_evidence(client, monkeypatch):
    """A 4xx says the query text was refused, nothing about the service."""
    import httpx

    import harbor_clerk.search as search_mod
    from harbor_clerk.embedder_client import EmbedderError

    request = httpx.Request("POST", "http://embedder/embed")
    rejected = httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))
    monkeypatch.setattr(search_mod, "embed_texts_async", _raise_from(rejected, EmbedderError))
    with pytest.raises(EmbedderError):
        await search_mod._embed_query("lighthouse")
    _stub_embedder(monkeypatch)

    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "checking", "a rejected query was taken as evidence"


async def test_a_recent_successful_query_embed_skips_the_probe(client, monkeypatch):
    import harbor_clerk.search as search_mod

    async def _up(texts, **kwargs):
        return [[0.01] * 768]

    monkeypatch.setattr(search_mod, "embed_texts_async", _up)
    await search_mod._embed_query("lighthouse")
    calls = _stub_embedder(monkeypatch, embed_status=500)  # a probe would say degraded

    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "ok"
    assert not calls, f"probed despite a fresh successful query embed: {calls}"


@pytest.fixture
async def mcp_admin(db_session, admin_user, monkeypatch):
    """kb_system_health as the admin, on the test connection."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.mcp_server import _mcp_principal

    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    yield
    _mcp_principal.reset(token)


async def test_kb_system_health_reports_the_embedder_verdict(mcp_admin):
    """It said it answers "is the embedder up" and checked only Postgres and storage."""
    import json

    from harbor_clerk import query_embed_health
    from harbor_clerk.mcp_server import kb_system_health

    query_embed_health.record_failure("/embed returned HTTP 500")
    failing = json.loads(await kb_system_health())
    assert failing["checks"]["embedder"] == "error: /embed returned HTTP 500"
    assert failing["status"] == "degraded"

    query_embed_health.record_busy()
    busy = json.loads(await kb_system_health())
    assert busy["checks"]["embedder"] == "ok"
    assert busy["notes"]["embedder"].startswith("busy")


async def test_kb_system_health_reads_a_dead_embedder_as_an_error_when_polled_rarely(mcp_admin, monkeypatch):
    """The same stale-evidence wait as /system/health: a refused connection polled
    every 90 s is an error on every call, not "checking"."""
    import json

    import httpx

    from harbor_clerk import query_embed_health
    from harbor_clerk.config import get_settings
    from harbor_clerk.mcp_server import kb_system_health

    base = get_settings().embedder_url
    real_get = httpx.AsyncClient.get

    async def _refused(self, url, *args, **kwargs):
        if str(url) == f"{base}/health":
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _refused)
    clock = [1000.0]
    monkeypatch.setattr(query_embed_health, "_now", lambda: clock[0])
    query_embed_health.record_failure("ConnectError: [Errno 61] Connection refused")

    for poll in range(3):
        clock[0] += 90
        data = json.loads(await kb_system_health())
        assert data["checks"]["embedder"].startswith("error"), f"poll {poll}: {data['checks']['embedder']}"
        assert data["status"] == "degraded"


async def test_kb_system_health_does_not_wait_for_the_first_probe(mcp_admin, monkeypatch):
    import json
    import time

    from harbor_clerk.mcp_server import kb_system_health

    _stub_embedder(monkeypatch, embed_delay=5.0)

    started = time.monotonic()
    data = json.loads(await kb_system_health())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"kb_system_health waited {elapsed:.2f}s on the embedder"
    assert data["checks"]["embedder"] == "checking"
    assert data["status"] == "healthy", "a probe still running is not a failure"


async def test_a_success_after_a_failure_clears_the_failure(client, monkeypatch):
    import harbor_clerk.search as search_mod

    outcomes = iter([RuntimeError("HTTP 500"), [[0.01] * 768]])

    async def _flaky(texts, **kwargs):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(search_mod, "embed_texts_async", _flaky)
    with pytest.raises(RuntimeError):
        await search_mod._embed_query("a")
    await search_mod._embed_query("b")
    _stub_embedder(monkeypatch)

    data = (await client.get("/api/system/health")).json()

    assert data["checks"]["embedder"] == "ok"


async def test_health_check_exposes_allow_source_download_default_false(client):
    """The health endpoint surfaces the allow_source_download capability so the
    frontend can decide whether to render the Download button. Default must be
    False on every deployment — see the setting docstring for why."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "allow_source_download" in data
    assert data["allow_source_download"] is False


async def test_health_check_exposes_enable_cli_access_default_false(client):
    """The health endpoint surfaces the enable_cli_access capability so the
    frontend Integrations page can show whether CLI access is enabled. Default
    must be False on every deployment — it is an opt-in feature."""
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "enable_cli_access" in data
    assert data["enable_cli_access"] is False


async def test_health_check_exposes_and_probes_local_mcp_url_when_set(client, monkeypatch):
    import asyncio

    from harbor_clerk.config import get_settings

    class _Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def _fake_open_connection(host, port):
        assert host == "localhost"
        assert port == 8443
        return object(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)

    s = get_settings()
    original = s.local_mcp_url
    s.local_mcp_url = "https://localhost:8443"
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["local_mcp_url"] == "https://localhost:8443"
        assert data["checks"]["local_https_gateway"] == "ok"
    finally:
        s.local_mcp_url = original


async def test_health_check_marks_non_loopback_local_mcp_url_not_probed(client, monkeypatch):
    import asyncio

    from harbor_clerk.config import get_settings

    opened_gateway_socket = False

    async def _fake_open_connection(host, port):
        nonlocal opened_gateway_socket
        opened_gateway_socket = True
        raise AssertionError(f"unexpected gateway socket probe for {host}:{port}")

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)

    s = get_settings()
    original = s.local_mcp_url
    s.local_mcp_url = "https://example.com"
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["local_mcp_url"] == "https://example.com"
        assert data["checks"]["local_https_gateway"] == "not_probed"
        assert opened_gateway_socket is False
    finally:
        s.local_mcp_url = original


async def test_native_service_logs_label_gateway_llm_and_worker_queues(
    client, admin_user, admin_token, monkeypatch, tmp_path
):
    config_file = tmp_path / "config.json"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    config_file.write_text("{}", encoding="utf-8")
    for name in (
        "api.log",
        "gateway.log",
        "llm.log",
        "worker-io.log",
        "worker-cpu.log",
        "worker-llm.log",
        "watcher.log",
        "tika-launchd.log",
        "postgres-Mon.log",
    ):
        (logs_dir / name).write_text("hello\n", encoding="utf-8")

    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(config_file))

    resp = await client.get("/api/system/logs", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "native"
    assert data["logs_dir"] == str(logs_dir)

    labels = {item["name"]: item["service"] for item in data["files"]}
    assert labels["api.log"] == "API Server"
    assert labels["gateway.log"] == "HTTPS Gateway"
    assert labels["llm.log"] == "LLM"
    assert labels["worker-io.log"] == "Worker IO"
    assert labels["worker-cpu.log"] == "Worker CPU"
    assert labels["worker-llm.log"] == "Worker LLM"
    assert labels["watcher.log"] == "Watcher"
    assert labels["tika-launchd.log"] == "Tika"
    assert labels["postgres-Mon.log"] == "PostgreSQL"


async def test_health_check_reflects_allow_source_download_when_set(client):
    """Toggling the setting at runtime should be visible immediately on the
    next health fetch. Tests use the same Settings singleton; mutating it on
    the fly is the production-equivalent of an admin flipping the env var
    and restarting (the singleton is read fresh on each request)."""
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.allow_source_download
    s.allow_source_download = True
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        assert resp.json()["allow_source_download"] is True
    finally:
        s.allow_source_download = original


async def test_health_check_cli_shim_absent_on_docker(client):
    """On Docker (native_config_file not set), cli_shim_install_status must be
    None so the frontend omits the macOS-only shim section."""
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    s.native_config_file = ""  # simulate Docker
    try:
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("cli_shim_install_status") is None
    finally:
        s.native_config_file = original


async def test_status_summary_surfaces_recovery_attention(client, admin_user, admin_token, db_session):
    now = datetime.now(UTC)
    failed_doc = Document(
        title="Broken scan",
        status="active",
        sha256=b"f" * 32,
        pipeline_status=PipelineStatus.error,
        error="Tika failed",
    )
    processing_doc = Document(
        title="Still embedding",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.embedding,
    )
    ner_doc = Document(
        title="No entities",
        status="active",
        sha256=b"n" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add_all([failed_doc, processing_doc, ner_doc])
    await db_session.flush()

    db_session.add_all(
        [
            IngestionJob(
                doc_id=failed_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.error,
                error="Tika failed",
            ),
            IngestionJob(
                doc_id=processing_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
                started_at=now - timedelta(hours=3),
            ),
            IngestionJob(
                doc_id=ner_doc.doc_id,
                stage=JobStage.entities,
                status=JobStatus.done,
                metrics={"skipped": True, "reason": "spacy_unavailable"},
            ),
        ]
    )
    db_session.add(WatchedFolder(path="/missing", unavailable_reason="permission denied"))
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 1
    assert data["counts"]["processing_documents"] == 1
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["stuck_jobs"] == 1
    assert data["counts"]["unavailable_folders"] == 1
    assert data["counts"]["ner_skipped_documents"] == 1

    issue_kinds = {issue["kind"] for issue in data["needs_attention"]}
    assert {
        "failed_documents",
        "stuck_jobs",
        "folder_access",
        "entity_extraction_skipped",
    }.issubset(issue_kinds)
    entity_issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "entity_extraction_skipped")
    assert entity_issue["title"] == "Entity extraction skipped some documents"
    assert "open Maintenance and reprocess" in entity_issue["detail"]
    assert entity_issue["action_label"] == "Open maintenance"
    assert entity_issue["action_href"] == "/settings/maintenance"
    assert data["recent_failed_documents"][0]["title"] == "Broken scan"
    assert data["recent_failed_documents"][0]["failed_stage"] == "extract"
    assert data["recent_processing_documents"][0]["title"] == "Still embedding"
    assert data["recent_processing_documents"][0]["processing_stage"] == "embed"
    assert data["recent_processing_documents"][0]["job_status"] == "running"


async def test_status_summary_surfaces_failed_summarize_jobs(client, admin_user, admin_token, db_session):
    doc = Document(
        title="Searchable but unsummarized",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.error,
            error="AppleIntelligenceUnavailableError: Apple Intelligence summaries are enabled but unavailable",
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_jobs"] == 1
    assert data["counts"]["failed_summarize_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "summary_generation_failed")
    assert issue["severity"] == "warning"
    assert issue["title"] == "Summaries failed to generate"
    assert issue["count"] == 1
    assert "Documents remain searchable" in issue["detail"]
    assert issue["action_href"] == "/settings/maintenance"


async def test_status_summary_surfaces_blocked_summarize_jobs(client, admin_user, admin_token, db_session):
    retry_at = datetime.now(UTC) + timedelta(minutes=10)
    doc = Document(
        title="Waiting on Apple Intelligence",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.queued,
            pipeline_seq=doc.pipeline_seq,
            metrics={
                "blocked": True,
                "reason": "apple_intelligence_unavailable",
                "retry_attempts": 2,
                "retry_after": retry_at.isoformat(),
            },
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_summarize_jobs"] == 0
    assert data["counts"]["blocked_summarize_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "summary_generation_blocked")
    assert issue["severity"] == "warning"
    assert issue["title"] == "Apple Intelligence summaries are paused"
    assert issue["count"] == 1
    assert "retry automatically with backoff" in issue["detail"]
    assert issue["action_href"] == "/settings/models"


async def test_status_summary_surfaces_embeds_waiting_on_the_embedder(client, admin_user, admin_token, db_session):
    """A deferred embed is `queued`; without this it reads as ordinary processing
    while the embedding service is down (#619, #685)."""
    from harbor_clerk.worker.pipeline import EMBED_RETRY_REASON

    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    doc = Document(
        title="Waiting on the embedder",
        status="active",
        sha256=b"e" * 32,
        pipeline_status=PipelineStatus.embedding,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.embed,
            status=JobStatus.queued,
            metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": 2, "retry_after": retry_at.isoformat()},
        )
    )
    await db_session.flush()

    data = (await client.get("/api/system/status-summary", headers=auth_header(admin_token))).json()

    assert data["state"] == "needs_attention"
    assert data["counts"]["blocked_embed_jobs"] == 1
    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "embedding_blocked")
    assert issue["severity"] == "warning"
    assert issue["count"] == 1
    assert "keyword only" in issue["detail"]
    assert retry_at.isoformat() not in issue["detail"], "a raw ISO timestamp is not for humans"
    assert "The next retry is in about 5 minutes (" in issue["detail"], issue["detail"]
    assert retry_at.strftime("%H:%M UTC") in issue["detail"]


async def test_an_embed_being_retried_still_counts_as_waiting_on_the_embedder(
    client, admin_user, admin_token, db_session
):
    """While a deferred embed is being retried its row is running, still carrying its
    retry state; counting only queued rows made the item flicker away each retry."""
    from harbor_clerk.worker.pipeline import EMBED_RETRY_REASON

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    doc = Document(title="Retrying", status="active", sha256=b"r" * 32, pipeline_status=PipelineStatus.embedding)
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.embed,
            status=JobStatus.running,
            heartbeat_at=datetime.now(UTC),
            metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": 2, "retry_after": past},
        )
    )
    await db_session.flush()

    data = (await client.get("/api/system/status-summary", headers=auth_header(admin_token))).json()

    assert data["counts"]["blocked_embed_jobs"] == 1
    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "embedding_blocked")
    assert "next retry" not in issue["detail"].lower(), "a running retry has no next retry time to show"
    # Every deferred row is running: a retry in progress, which may be succeeding.
    assert issue["title"] == "Embedding is retrying after an embedder outage", issue["title"]
    assert "failing" not in (issue["title"] + issue["detail"]).lower()


@pytest.mark.parametrize("embedder_failing", [True, False], ids=["embedder-failing", "embedder-back"])
async def test_a_waiting_deferred_embed_says_failing_only_while_the_embedder_is(
    client, admin_user, admin_token, db_session, embedder_failing
):
    """It said "the embedding service is failing" whenever a deferred embed waited,
    though a deferral outlives the outage that caused it. "Failing" now follows
    the embedder's current verdict."""
    from harbor_clerk import query_embed_health

    if embedder_failing:
        query_embed_health.record_failure("/embed returned HTTP 500")
    else:
        query_embed_health.record_success()
    # One retrying and one still waiting.
    from harbor_clerk.worker.pipeline import EMBED_RETRY_REASON

    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    for n, (status, retry_after) in enumerate([(JobStatus.running, past), (JobStatus.queued, future)]):
        doc = Document(
            title=f"Deferred {n}",
            status="active",
            sha256=bytes([n + 40]) * 32,
            pipeline_status=PipelineStatus.embedding,
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(
            IngestionJob(
                doc_id=doc.doc_id,
                stage=JobStage.embed,
                status=status,
                heartbeat_at=datetime.now(UTC),
                metrics={"reason": EMBED_RETRY_REASON, "retry_attempts": 2, "retry_after": retry_after},
            )
        )
    await db_session.flush()

    data = (await client.get("/api/system/status-summary", headers=auth_header(admin_token))).json()

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "embedding_blocked")
    assert issue["count"] == 2
    if embedder_failing:
        assert issue["title"] == "Embedding is paused: the embedding service is failing", issue["title"]
    else:
        assert issue["title"] == "Embedding of 2 documents is being retried", issue["title"]
        assert "failing" not in issue["detail"], issue["detail"]
    # The next retry is the waiting row's, not the running row's past retry_after.
    assert "The next retry is in about 5 minutes" in issue["detail"], issue["detail"]


def test_retry_times_read_as_durations():
    from harbor_clerk.api.routes.system import _humanize_retry_at

    now = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
    assert _humanize_retry_at((now + timedelta(minutes=1)).isoformat(), now) == "in about 1 minute (14:01 UTC)"
    assert _humanize_retry_at((now + timedelta(seconds=20)).isoformat(), now) == "in under a minute (14:00 UTC)"
    assert _humanize_retry_at((now - timedelta(minutes=3)).isoformat(), now) == "due now"
    assert _humanize_retry_at("not a time", now) is None
    assert _humanize_retry_at(None, now) is None


async def test_stats_report_embedding_coverage(client, admin_user, admin_token, db_session):
    """After #685 the only way to learn 29% of a corpus had no vectors was to query Postgres."""
    from harbor_clerk.models import Chunk

    full = Document(title="Full", status="active", sha256=b"f" * 32, pipeline_status=PipelineStatus.ready)
    partial = Document(title="Partial", status="active", sha256=b"q" * 32, pipeline_status=PipelineStatus.ready)
    deleted = Document(title="Deleted", status="deleted", sha256=b"d" * 32, pipeline_status=PipelineStatus.ready)
    db_session.add_all([full, partial, deleted])
    await db_session.flush()
    vec = [0.01] * 768
    db_session.add_all(
        [
            Chunk(doc_id=full.doc_id, chunk_num=0, chunk_text="a", language="english", embedding=vec),
            Chunk(doc_id=full.doc_id, chunk_num=1, chunk_text="b", language="english", embedding=vec),
            Chunk(doc_id=partial.doc_id, chunk_num=0, chunk_text="c", language="english", embedding=vec),
            Chunk(doc_id=partial.doc_id, chunk_num=1, chunk_text="d", language="english"),
            Chunk(doc_id=deleted.doc_id, chunk_num=0, chunk_text="e", language="english"),
        ]
    )
    await db_session.flush()

    resp = await client.get("/api/system/stats", headers=auth_header(admin_token))

    assert resp.status_code == 200
    assert resp.json()["embeddings"] == {
        "chunks_total": 4,
        "chunks_embedded": 3,
        "chunks_missing": 1,
        "documents_with_chunks": 2,
        "documents_fully_embedded": 1,
        "documents_missing_embeddings": 1,
    }


async def test_status_summary_separates_stranded_pipeline_state(client, admin_user, admin_token, db_session):
    stranded_doc = Document(
        title="Marked chunking",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add(stranded_doc)
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 1
    assert data["recent_processing_documents"] == []

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "stranded_pipeline_state")
    assert issue["severity"] == "warning"
    assert issue["count"] == 1
    assert "no queued or running ingest job" in issue["detail"]


async def test_status_summary_surfaces_completed_status_cleanup(client, admin_user, admin_token, db_session):
    stale_doc = Document(
        title="Finished but marked queued",
        status="active",
        sha256=b"q" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(stale_doc)
    await db_session.flush()
    _add_done_gate_jobs(db_session, stale_doc.doc_id)
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["ready_documents"] == 1
    assert data["counts"]["stored_ready_documents"] == 0
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["pipeline_processing_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["completed_status_stale_documents"] == 1
    assert data["recent_processing_documents"] == []

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "completed_status_stale")
    assert issue["severity"] == "warning"
    assert issue["count"] == 1
    assert issue["action_label"] == "Repair statuses"
    assert issue["action_kind"] == "repair_completed_statuses"
    assert "completed ingest" in issue["detail"]


async def test_status_summary_reports_summarize_backlog_separately(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready but summarizing",
        status="active",
        sha256=b"z" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
    )
    db_session.add(ready_doc)
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=ready_doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.queued,
            pipeline_seq=ready_doc.pipeline_seq,
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "processing"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["summarizing_documents"] == 1
    assert data["counts"]["queued_jobs"] == 0
    assert data["counts"]["running_jobs"] == 0
    assert data["counts"]["summarizing_queued_jobs"] == 1
    assert data["counts"]["summarizing_running_jobs"] == 0
    assert data["counts"]["total_queued_jobs"] == 1
    assert data["recent_processing_documents"] == []
    assert data["needs_attention"] == []


async def test_system_stats_reports_worker_queue_rollups(client, admin_user, admin_token, db_session):
    io_doc = Document(
        title="IO queued",
        status="active",
        sha256=b"i" * 32,
        pipeline_status=PipelineStatus.extracting,
        pipeline_seq=1,
    )
    cpu_doc = Document(
        title="CPU running",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.embedding,
        pipeline_seq=1,
    )
    llm_doc = Document(
        title="LLM queued",
        status="active",
        sha256=b"l" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
    )
    stale_doc = Document(
        title="Stale job",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.chunking,
        pipeline_seq=2,
    )
    inactive_doc = Document(
        title="Inactive job",
        status="inactive",
        sha256=b"x" * 32,
        pipeline_status=PipelineStatus.ocr_running,
        pipeline_seq=1,
    )
    db_session.add_all([io_doc, cpu_doc, llm_doc, stale_doc, inactive_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=io_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.queued,
                pipeline_seq=io_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=cpu_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
                pipeline_seq=cpu_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=llm_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=llm_doc.pipeline_seq,
            ),
            IngestionJob(
                doc_id=stale_doc.doc_id,
                stage=JobStage.chunk,
                status=JobStatus.queued,
                pipeline_seq=1,
            ),
            IngestionJob(
                doc_id=inactive_doc.doc_id,
                stage=JobStage.ocr,
                status=JobStatus.queued,
                pipeline_seq=inactive_doc.pipeline_seq,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.get("/api/system/stats", headers=auth_header(admin_token))
    assert resp.status_code == 200
    queues = resp.json()["queues"]
    assert queues == {
        "io_queued": 1,
        "io_running": 0,
        "cpu_queued": 0,
        "cpu_running": 1,
        "llm_queued": 1,
        "llm_running": 0,
    }


async def test_status_summary_summarize_jobs_do_not_hide_completed_status_cleanup(
    client, admin_user, admin_token, db_session
):
    stale_doc = Document(
        title="Foreground complete with background summary",
        status="active",
        sha256=b"u" * 32,
        pipeline_status=PipelineStatus.finalizing,
        pipeline_seq=0,
    )
    db_session.add(stale_doc)
    await db_session.flush()
    _add_done_gate_jobs(db_session, stale_doc.doc_id)
    db_session.add(
        IngestionJob(
            doc_id=stale_doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.running,
            pipeline_seq=stale_doc.pipeline_seq,
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "needs_attention"
    assert data["counts"]["processing_documents"] == 0
    assert data["counts"]["summarizing_documents"] == 1
    assert data["counts"]["completed_status_stale_documents"] == 1
    assert data["counts"]["stranded_documents"] == 0
    assert data["counts"]["running_jobs"] == 0
    assert data["counts"]["summarizing_running_jobs"] == 1

    issue = next(issue for issue in data["needs_attention"] if issue["kind"] == "completed_status_stale")
    assert issue["count"] == 1


async def test_repair_completed_statuses_marks_finalized_stale_docs_ready(client, admin_user, admin_token, db_session):
    repairable_doc = Document(
        title="Repairable",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.chunking,
        error="old transient state",
    )
    still_running_doc = Document(
        title="Still running",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    unfinished_doc = Document(
        title="Unfinished",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    finalize_only_doc = Document(
        title="Finalize row only",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.chunking,
    )
    db_session.add_all([repairable_doc, still_running_doc, unfinished_doc, finalize_only_doc])
    await db_session.flush()
    _add_done_gate_jobs(db_session, repairable_doc.doc_id)
    _add_done_gate_jobs(db_session, still_running_doc.doc_id, exclude={JobStage.embed})
    db_session.add_all(
        [
            IngestionJob(
                doc_id=still_running_doc.doc_id,
                stage=JobStage.embed,
                status=JobStatus.running,
            ),
            IngestionJob(
                doc_id=finalize_only_doc.doc_id,
                stage=JobStage.finalize,
                status=JobStatus.done,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/repair-completed-statuses", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"repaired": 1}

    await db_session.refresh(repairable_doc)
    await db_session.refresh(still_running_doc)
    await db_session.refresh(unfinished_doc)
    await db_session.refresh(finalize_only_doc)
    assert repairable_doc.pipeline_status == PipelineStatus.ready
    assert repairable_doc.error is None
    assert still_running_doc.pipeline_status == PipelineStatus.chunking
    assert unfinished_doc.pipeline_status == PipelineStatus.chunking
    assert finalize_only_doc.pipeline_status == PipelineStatus.chunking


async def test_reprocess_all_bulk_resets_active_docs_and_queues_extract(client, admin_user, admin_token, db_session):
    active_doc = Document(
        title="Active",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=4,
        summary="Existing summary that should be regenerated by a full reprocess.",
        error="old failure",
    )
    inactive_doc = Document(
        title="Inactive",
        status="inactive",
        sha256=b"i" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=7,
    )
    db_session.add_all([active_doc, inactive_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.done,
                metrics={"old": True},
            ),
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.chunk,
                status=JobStatus.done,
            ),
            IngestionJob(
                doc_id=active_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.done,
                pipeline_seq=4,
                metrics={"skipped": True, "reason": "old_cleanup"},
            ),
            IngestionJob(
                doc_id=inactive_doc.doc_id,
                stage=JobStage.extract,
                status=JobStatus.done,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/reprocess-all", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"reprocessed": 1}

    await db_session.refresh(active_doc)
    await db_session.refresh(inactive_doc)
    assert active_doc.pipeline_status == PipelineStatus.extracting
    assert active_doc.pipeline_seq == 5
    assert active_doc.error is None
    assert inactive_doc.pipeline_status == PipelineStatus.ready
    assert inactive_doc.pipeline_seq == 7

    active_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == active_doc.doc_id))).scalars().all()
    )
    assert len(active_jobs) == 1
    assert active_jobs[0].stage == JobStage.extract
    assert active_jobs[0].status == JobStatus.queued
    assert active_jobs[0].metrics == {}
    assert active_jobs[0].started_at is None
    assert active_jobs[0].finished_at is None

    inactive_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == inactive_doc.doc_id)))
        .scalars()
        .all()
    )
    assert len(inactive_jobs) == 1
    assert inactive_jobs[0].stage == JobStage.extract
    assert inactive_jobs[0].status == JobStatus.done


async def test_resummarize_all_bulk_upserts_ready_docs_only(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=1,
        summary="Existing summary that should be regenerated by explicit resummarize.",
        error="old summary error",
    )
    processing_doc = Document(
        title="Processing",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.chunking,
        pipeline_seq=1,
    )
    stale_queued_doc = Document(
        title="Stale queued",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
    )
    db_session.add_all([ready_doc, processing_doc, stale_queued_doc])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=ready_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.done,
                metrics={"skipped": True},
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            ),
            IngestionJob(
                doc_id=stale_queued_doc.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
                metrics={"old": True},
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/resummarize-all", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"resummarized": 2}

    await db_session.refresh(ready_doc)
    await db_session.refresh(processing_doc)
    assert ready_doc.pipeline_status == PipelineStatus.ready
    assert ready_doc.error is None
    assert processing_doc.pipeline_status == PipelineStatus.chunking

    ready_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == ready_doc.doc_id))).scalars().all()
    )
    assert len(ready_jobs) == 1
    summarize_job = ready_jobs[0]
    assert summarize_job.stage == JobStage.summarize
    assert summarize_job.status == JobStatus.queued
    assert summarize_job.pipeline_seq == ready_doc.pipeline_seq
    assert summarize_job.metrics == {}
    assert summarize_job.started_at is None
    assert summarize_job.finished_at is None

    stale_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == stale_queued_doc.doc_id)))
        .scalars()
        .all()
    )
    assert len(stale_jobs) == 1
    stale_job = stale_jobs[0]
    assert stale_job.stage == JobStage.summarize
    assert stale_job.status == JobStatus.queued
    assert stale_job.pipeline_seq == stale_queued_doc.pipeline_seq
    assert stale_job.metrics == {}

    processing_jobs = (
        (await db_session.execute(select(IngestionJob).where(IngestionJob.doc_id == processing_doc.doc_id)))
        .scalars()
        .all()
    )
    assert processing_jobs == []


async def test_clear_redundant_summary_backlog_only_skips_safe_queued_jobs(client, admin_user, admin_token, db_session):
    with_summary = Document(
        title="Has summary",
        status="active",
        sha256=b"a" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    no_summary = Document(
        title="Missing summary",
        status="active",
        sha256=b"b" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary=None,
    )
    running = Document(
        title="Running summary",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    stale_generation = Document(
        title="Stale generation",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=3,
        summary="Already summarized.",
    )
    inactive = Document(
        title="Inactive",
        status="inactive",
        sha256=b"e" * 32,
        pipeline_status=PipelineStatus.ready,
        pipeline_seq=2,
        summary="Already summarized.",
    )
    db_session.add_all([with_summary, no_summary, running, stale_generation, inactive])
    await db_session.flush()
    db_session.add_all(
        [
            IngestionJob(
                doc_id=with_summary.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
                metrics={"existing": True},
            ),
            IngestionJob(
                doc_id=no_summary.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=running.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.running,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=stale_generation.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
            IngestionJob(
                doc_id=inactive.doc_id,
                stage=JobStage.summarize,
                status=JobStatus.queued,
                pipeline_seq=2,
            ),
        ]
    )
    await db_session.flush()

    resp = await client.post("/api/system/clear-redundant-summary-backlog", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1}

    jobs = (
        (
            await db_session.execute(
                select(IngestionJob).where(
                    IngestionJob.stage == JobStage.summarize,
                )
            )
        )
        .scalars()
        .all()
    )
    by_doc = {job.doc_id: job for job in jobs}

    cleared = by_doc[with_summary.doc_id]
    assert cleared.status == JobStatus.done
    assert cleared.started_at is not None
    assert cleared.finished_at is not None
    assert cleared.heartbeat_at is None
    assert cleared.error is None
    assert cleared.metrics["existing"] is True
    assert cleared.metrics["skipped"] is True
    assert cleared.metrics["reason"] == "existing_summary_cleanup"

    assert by_doc[no_summary.doc_id].status == JobStatus.queued
    assert by_doc[running.doc_id].status == JobStatus.running
    assert by_doc[stale_generation.doc_id].status == JobStatus.queued
    assert by_doc[inactive.doc_id].status == JobStatus.queued


async def test_status_summary_ready_when_no_attention_items(client, admin_user, admin_token, db_session):
    ready_doc = Document(
        title="Ready",
        status="active",
        sha256=b"r" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    deleted_failed_doc = Document(
        title="Deleted failure",
        status="deleted",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.error,
    )
    db_session.add_all([ready_doc, deleted_failed_doc])
    db_session.add(WatchedFolder(path="/docs"))
    await db_session.flush()
    db_session.add(
        IngestionJob(
            doc_id=deleted_failed_doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.error,
            error="old failure",
        )
    )
    await db_session.flush()

    resp = await client.get("/api/system/status-summary", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ready"
    assert data["counts"]["ready_documents"] == 1
    assert data["counts"]["failed_documents"] == 0
    assert data["counts"]["failed_jobs"] == 0
    assert data["needs_attention"] == []


# ---------------------------------------------------------------------------
# Unit tests for _cli_shim_install_status() using tmp_path.
# We pass _shim= directly to avoid touching ~/.local/bin on the test machine.
# ---------------------------------------------------------------------------

_SHIM_MARKER = "# harbor-clerk — installed by Harbor Clerk Server"


def _make_shim(bundle: str, default_url: str = "https://localhost:8443") -> str:
    return (
        "#!/bin/sh\n"
        f"{_SHIM_MARKER}\n"
        f'BUNDLE_RESOURCES="{bundle}"\n'
        f'export HARBOR_CLERK_URL="${{HARBOR_CLERK_URL:-{default_url}}}"\n'
        'exec "$BUNDLE_RESOURCES/venv/bin/python" -m harbor_clerk.cli.main "$@"\n'
    )


def test_cli_shim_not_installed_when_file_absent(tmp_path):
    """When the shim file doesn't exist, return 'not_installed'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    original_local_url = s.local_mcp_url
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)
    s.local_mcp_url = "https://localhost:8443"
    try:
        absent = tmp_path / "harbor-clerk"  # does not exist
        assert _cli_shim_install_status(_shim=absent) == "not_installed"
    finally:
        s.native_config_file = original
        s.local_mcp_url = original_local_url


def test_cli_shim_installed_outdated_when_url_differs(tmp_path, monkeypatch):
    """When shim URL doesn't match local_mcp_url, return 'installed_outdated'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    original_local_url = s.local_mcp_url
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)
    s.local_mcp_url = "https://localhost:8443"

    fake_bundle = "/fake/Harbor Clerk Server.app/Contents/Resources"
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim(fake_bundle, default_url="http://localhost:8100"))
    monkeypatch.setenv("BUNDLE_RESOURCES", fake_bundle)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed_outdated"
    finally:
        s.native_config_file = original
        s.local_mcp_url = original_local_url


def test_cli_shim_installed_when_bundle_matches(tmp_path, monkeypatch):
    """When shim bundle matches BUNDLE_RESOURCES env var, return 'installed'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    original_local_url = s.local_mcp_url
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)
    s.local_mcp_url = "https://localhost:8443"

    fake_bundle = "/fake/Harbor Clerk Server.app/Contents/Resources"
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim(fake_bundle))
    monkeypatch.setenv("BUNDLE_RESOURCES", fake_bundle)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed"
    finally:
        s.native_config_file = original
        s.local_mcp_url = original_local_url


def test_cli_shim_installed_outdated_when_bundle_differs(tmp_path, monkeypatch):
    """When shim bundle doesn't match BUNDLE_RESOURCES, return 'installed_outdated'."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    original_local_url = s.local_mcp_url
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)
    s.local_mcp_url = "https://localhost:8443"

    stale_bundle = "/old/Harbor Clerk Server.app/Contents/Resources"
    current_bundle = "/new/Harbor Clerk Server.app/Contents/Resources"
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim(stale_bundle))
    monkeypatch.setenv("BUNDLE_RESOURCES", current_bundle)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed_outdated"
    finally:
        s.native_config_file = original
        s.local_mcp_url = original_local_url


def test_cli_shim_foreign_script_treated_as_not_installed(tmp_path):
    """A harbor-clerk script without our marker must be left alone (not_installed)."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    foreign = '#!/bin/sh\n# installed by homebrew\nexec /opt/homebrew/bin/hc "$@"\n'
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(foreign)
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "not_installed"
    finally:
        s.native_config_file = original


def test_cli_shim_no_bundle_resources_env_treated_as_installed(tmp_path, monkeypatch):
    """When BUNDLE_RESOURCES env var is absent (unusual), treat as installed
    rather than reporting false outdated status."""
    from harbor_clerk.api.routes.system import _cli_shim_install_status
    from harbor_clerk.config import get_settings

    s = get_settings()
    original = s.native_config_file
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    s.native_config_file = str(config_file)

    monkeypatch.delenv("BUNDLE_RESOURCES", raising=False)
    shim_file = tmp_path / "harbor-clerk"
    shim_file.write_text(_make_shim("/some/bundle"))
    try:
        assert _cli_shim_install_status(_shim=shim_file) == "installed"
    finally:
        s.native_config_file = original


async def test_summary_backlog_endpoint_returns_all_four_fields(client, admin_user, admin_token):
    """The Observatory Summary Backlog widget needs depth, throughput,
    p50, and depth-over-time history. Endpoint must return all four."""
    response = await client.get("/api/system/summary-backlog", headers=auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()
    assert "queue_depth" in data
    assert "throughput_per_min" in data
    assert "p50_seconds" in data
    assert "depth_history" in data
    assert isinstance(data["depth_history"], list)
    if data["depth_history"]:
        assert len(data["depth_history"][0]) == 2
    # 5-minute samples over the last hour = 13 buckets
    assert len(data["depth_history"]) == 13
    # Type + range checks: regressions like queue_depth=null, p50="N/A",
    # or throughput as a string would all pass mere presence checks.
    assert isinstance(data["queue_depth"], int) and data["queue_depth"] >= 0
    assert isinstance(data["throughput_per_min"], (int, float)) and data["throughput_per_min"] >= 0
    assert isinstance(data["p50_seconds"], (int, float)) and data["p50_seconds"] >= 0
    assert all(isinstance(ts, (int, float)) for ts, _ in data["depth_history"])
    assert all(isinstance(d, int) and d >= 0 for _, d in data["depth_history"])


# --------------------------------------------------------------------------
# Health-check error rendering — the live box reported `"embedder": "error: "`
# --------------------------------------------------------------------------


async def test_health_check_names_a_timing_out_embedder(client, monkeypatch):
    """A timeout must name itself, not render as a bare "error: ".

    This drives the endpoint rather than the helper, so reverting the call site
    at system.py fails here. `httpx.ReadTimeout("")` carries an empty message —
    which is what the Mac mini actually hit.
    """
    import httpx

    from harbor_clerk.config import get_settings

    real_get = httpx.AsyncClient.get
    # Resolve from config, never hardcode: CI overrides TIKA_URL/EMBEDDER_URL,
    # so a literal that matches the local .env silently stops matching there —
    # the shim falls through to a real request and the assertion compares
    # against whatever the runner's network did.
    embedder_health = f"{get_settings().embedder_url}/health"

    async def _selective_get(self, url, *args, **kwargs):
        if str(url) == embedder_health:
            raise httpx.ReadTimeout("")
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _selective_get)

    data = await _settled_health(client)

    assert data["checks"]["embedder"] == "error: ReadTimeout"
    assert data["status"] == "degraded"


async def test_health_check_keeps_the_message_when_there_is_one(client, monkeypatch):
    import httpx

    from harbor_clerk.config import get_settings

    real_get = httpx.AsyncClient.get
    tika_probe = f"{get_settings().tika_url}/tika"

    async def _selective_get(self, url, *args, **kwargs):
        if str(url) == tika_probe:
            raise httpx.ConnectError("connection refused")
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _selective_get)

    resp = await client.get("/api/system/health")
    assert resp.json()["checks"]["tika"] == "error: ConnectError: connection refused"


async def test_health_check_bounds_the_detail_it_publishes(client, monkeypatch):
    """/api/system/health is unauthenticated, and a driver error can stringify
    with the failing statement and connection detail. Bound what goes out."""
    import httpx

    from harbor_clerk.config import get_settings
    from harbor_clerk.error_text import HEALTH_DETAIL_LIMIT

    real_get = httpx.AsyncClient.get
    embedder_health = f"{get_settings().embedder_url}/health"

    async def _selective_get(self, url, *args, **kwargs):
        if str(url) == embedder_health:
            raise httpx.ConnectError("x" * 5000)
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _selective_get)

    rendered = (await _settled_health(client))["checks"]["embedder"]

    assert len(rendered) < HEALTH_DETAIL_LIMIT + 100, f"unbounded detail published: {len(rendered)} chars"
    assert rendered.endswith("…")


async def test_no_health_check_renders_a_dangling_prefix(client, monkeypatch):
    """Whatever fails and however it stringifies, a reader learns the type."""
    import httpx

    from harbor_clerk.config import get_settings

    settings = get_settings()
    real_get = httpx.AsyncClient.get
    # Must stay selective: the test client is itself an httpx.AsyncClient, so a
    # blanket raiser kills the request before it reaches the endpoint. Resolved
    # from config — hardcoded hosts silently stop matching under CI's env and
    # the test keeps passing while covering less.
    probed = (settings.tika_url, settings.embedder_url, settings.reranker_url)

    async def _selective_get(self, url, *args, **kwargs):
        if str(url).startswith(probed):
            raise httpx.ConnectTimeout("")
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", _selective_get)

    checks = (await _settled_health(client))["checks"]
    assert checks["embedder"] == "error: ConnectTimeout"
    for name, value in checks.items():
        assert value.strip() != "error:", f"{name} rendered a bare prefix"
        assert not value.endswith(": "), f"{name} rendered a dangling colon: {value!r}"


async def test_health_check_reports_the_stamped_build_version(client, monkeypatch):
    """A packaged app exports BUILD_VERSION from venv/build-version.txt, which
    package.sh derives from the release tag; health surfaces it verbatim next
    to the commit so a development build (`+N.g<hash>[.dirty]`) is never
    mistaken for a release."""
    monkeypatch.setenv("BUILD_VERSION", "0.9.3+2.g887c3fb.dirty")
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "0.9.3+2.g887c3fb.dirty"
    assert "build" in data


@pytest.mark.parametrize("stamp", [None, "", "   "], ids=["unset", "empty", "blank"])
async def test_health_check_falls_back_to_the_package_version_without_a_stamp(client, monkeypatch, stamp):
    """Docker, `uv run`, and a bundle assembled before the stamp existed export
    no BUILD_VERSION (or nothing useful); health must still answer with the
    installed package's version — pyproject's — not fail or report ''. It is
    marked `+unstamped`: the package version is the same for every commit after
    the bump, so a bare number stays something only a stamped release reports."""
    import importlib.metadata
    import re
    import tomllib
    from pathlib import Path

    if stamp is None:
        monkeypatch.delenv("BUILD_VERSION", raising=False)
    else:
        monkeypatch.setenv("BUILD_VERSION", stamp)
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    version = resp.json()["version"]
    assert version == f"{importlib.metadata.version('harbor-clerk')}+unstamped"
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert version == f"{pyproject['project']['version']}+unstamped"
    assert not re.fullmatch(r"\d+\.\d+\.\d+", version), "a bare X.Y.Z is reserved for a stamped release build"
