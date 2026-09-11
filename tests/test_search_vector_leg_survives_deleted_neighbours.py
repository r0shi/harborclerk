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


async def test_live_neighbours_survive_a_deleted_document_nearest_to_the_query(db_session, monkeypatch):
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
