"""The vector leg must not come back short because deleted chunks sit nearest.

pgvector post-filters an HNSW scan: the index hands back `hnsw.ef_search`
candidates (40 by default) and only then is the `documents.status = 'active'`
subquery applied. Deleting a large document that dominates a topic therefore
starves the vector leg for every later query on that topic — the nearest 40 are
all its chunks, the filter drops them, and live neighbours never surface. On the
live corpus, with 21% of chunks belonging to deleted documents, 10 of 60 probe
queries came back short. `hybrid_search` now sets
`hnsw.iterative_scan = relaxed_order` for the transaction so the scan continues
until the LIMIT is met.

The test rebuilds that shape: one deleted document with three times `ef_search`
chunks, every one nearer to the query than any live chunk, and a query sharing
no token with any chunk so the FTS leg cannot mask a starved vector leg.

Two things about the data are load-bearing. The planner is steered onto the
HNSW index with a post-filter join, because on a table this small it would
otherwise drive from `documents` and never touch the index — the bug lives in
the index scan. And the vectors carry gaussian noise rather than being
axis-aligned near-duplicates: with 120 near-identical neighbours the graph's
pruning left the live chunks unreachable from the entry point, and the iterative
scan legitimately never found them.
"""

from __future__ import annotations

import random
import uuid

import pytest
from sqlalchemy import text

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search

# No anyio marker: pyproject sets asyncio_mode = "auto" (#598).

DIM = 768
EF_SEARCH_DEFAULT = 40


def _near(rng: random.Random, spread: float) -> list[float]:
    """A vector along axis 0 with gaussian noise; larger spread = further away."""
    v = [rng.gauss(0.0, spread) for _ in range(DIM)]
    v[0] = 1.0
    return v


async def _doc(session, *, status: str) -> Document:
    doc = Document(title=status, status=status, sha256=uuid.uuid4().bytes * 2, pipeline_status=PipelineStatus.ready)
    session.add(doc)
    await session.flush()
    return doc


async def _require_iterative_scan(session) -> None:
    """Fail with the real reason on a pgvector too old for iterative scans, not the assertion below."""
    version = (await session.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar()
    major, minor = (int(x) for x in version.split(".")[:2])
    if (major, minor) < (0, 8):
        pytest.skip(f"pgvector {version} has no hnsw.iterative_scan; both deploy paths ship 0.8+")


async def test_live_neighbours_survive_a_deleted_document_nearest_to_the_query(db_session, monkeypatch):
    await _require_iterative_scan(db_session)
    rng = random.Random(7)
    query_vec = _near(rng, 0.0)
    dead = await _doc(db_session, status="deleted")
    live = await _doc(db_session, status="active")

    for i in range(3 * EF_SEARCH_DEFAULT):
        db_session.add(
            Chunk(
                doc_id=dead.doc_id,
                chunk_num=i,
                chunk_text="qqxz deleted",
                language="english",
                embedding=_near(rng, 0.02),
            )
        )
    for i in range(5):
        db_session.add(
            Chunk(
                doc_id=live.doc_id, chunk_num=i, chunk_text="qqxz live", language="english", embedding=_near(rng, 0.3)
            )
        )
    await db_session.flush()

    # Steer the planner onto the HNSW index with the status subquery applied as a
    # post-filter join — the plan the live corpus actually takes.
    for knob in ("seqscan", "sort", "hashjoin", "mergejoin", "nestloop", "bitmapscan"):
        await db_session.execute(text(f"SET LOCAL enable_{knob} = off"))

    async def _fake_embed(query):
        return query_vec

    import harbor_clerk.search as search_mod

    monkeypatch.setattr(search_mod, "_embed_query", _fake_embed)

    # No token of the query appears in any chunk, so a hit can only come from the vector leg.
    result = await hybrid_search(db_session, "wvptk", k=5)
    hit_docs = {h.doc_id for h in result.hits}

    assert str(dead.doc_id) not in hit_docs
    assert str(live.doc_id) in hit_docs, (
        "the vector leg returned nothing: the HNSW scan's first ef_search candidates were all "
        "deleted chunks and the post-filter dropped them, so live neighbours were never reached"
    )


async def test_an_unknown_iterative_scan_guc_does_not_take_the_search_down(db_session, monkeypatch):
    """On a pgvector without the GUC the SET fails; that must degrade, not abort.

    Without the savepoint the failed SET poisons the transaction: the vector leg
    logs a misleading "embedder unavailable", and the chunk load that follows
    dies with InFailedSQLTransactionError — every search on that server fails,
    lexical fallback included. Reproduced with a name pgvector rejects outright,
    since `hnsw.` is a reserved prefix and an unknown GUC there is a hard error.
    """
    live = await _doc(db_session, status="active")
    marker = f"kestrel-{uuid.uuid4().hex[:8]} clause"
    db_session.add(
        Chunk(
            doc_id=live.doc_id,
            chunk_num=0,
            chunk_text=f"the {marker} applies",
            language="english",
            embedding=[0.01] * DIM,
        )
    )
    await db_session.flush()

    import harbor_clerk.search as search_mod

    monkeypatch.setattr(search_mod, "_HNSW_ITERATIVE_SCAN_SQL", "SET LOCAL hnsw.no_such_guc = relaxed_order")

    async def _fake_embed(query):
        return [0.01] * DIM

    monkeypatch.setattr(search_mod, "_embed_query", _fake_embed)

    result = await hybrid_search(db_session, marker, k=5)
    assert str(live.doc_id) in {h.doc_id for h in result.hits}, "search failed outright instead of degrading"
    # And the transaction is still usable afterwards.
    assert (await db_session.execute(text("SELECT 1"))).scalar() == 1
