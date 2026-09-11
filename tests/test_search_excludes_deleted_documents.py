"""A deleted document must not be retrievable — through any surface.

`DELETE /api/docs/{id}` only flips `documents.status`; the chunks and their
embeddings stay. `hybrid_search` used to build its document-level subquery only
when a caller passed a filter, so an unfiltered query never looked at
`documents.status` and deleted content came straight back. Every retrieval
surface routes through `hybrid_search` (`find_all`, the MCP tools, the chat
tool), so this one test covers all of them.
"""

from __future__ import annotations

import uuid

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search

# No anyio marker: pyproject sets asyncio_mode = "auto"; the marker routes the test
# through a second event loop and breaks the session-scoped engine in CI (#598).


async def _doc_with_chunk(session, *, title: str, text: str, status: str = "active") -> Document:
    doc = Document(title=title, status=status, sha256=uuid.uuid4().bytes * 2, pipeline_status=PipelineStatus.ready)
    session.add(doc)
    await session.flush()
    session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="english", embedding=[0.01] * 768))
    await session.flush()
    return doc


async def test_deleted_document_is_excluded_from_hybrid_search(db_session, monkeypatch):
    """The marker phrase appears only in these two documents; only the live one may return."""
    marker = f"zebrafish-{uuid.uuid4().hex[:8]} covenant"
    live = await _doc_with_chunk(db_session, title="live", text=f"The {marker} survives termination.")
    dead = await _doc_with_chunk(db_session, title="dead", text=f"The {marker} survives termination.", status="deleted")

    # The vector leg needs a query embedding; keep it deterministic and offline.
    async def _fake_embed(query):
        return [0.01] * 768

    import harbor_clerk.search as search_mod

    monkeypatch.setattr(search_mod, "_embed_query", _fake_embed)

    result = await hybrid_search(db_session, marker, k=10)
    hit_docs = {h.doc_id for h in result.hits}  # SearchHit.doc_id is a str

    assert str(live.doc_id) in hit_docs, "the live document should be found — otherwise this test proves nothing"
    assert str(dead.doc_id) not in hit_docs, "a deleted document's chunks were returned by search"
