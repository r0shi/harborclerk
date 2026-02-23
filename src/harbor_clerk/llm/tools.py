"""Chat tool definitions and executor for local LLM tool-calling."""

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Chunk, Document
from harbor_clerk.search import hybrid_search

logger = logging.getLogger(__name__)

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search the knowledge base for relevant document passages. Use this to find information before answering questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results to return (default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_passages",
            "description": "Read the full text of specific passages by their chunk IDs. Use this to get more context on search results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of chunk IDs to read",
                    },
                },
                "required": ["chunk_ids"],
            },
        },
    },
]


async def execute_tool(
    name: str, arguments: dict, session: AsyncSession,
) -> str:
    """Execute a chat tool and return the result as a JSON string."""
    try:
        if name == "search_documents":
            return await _search_documents(arguments, session)
        elif name == "read_passages":
            return await _read_passages(arguments, session)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        logger.exception("Tool execution error: %s", name)
        return json.dumps({"error": str(e)})


async def _search_documents(args: dict, session: AsyncSession) -> str:
    query = args.get("query", "")
    k = min(args.get("k", 5), 10)

    result = await hybrid_search(session, query, k=k)

    hits = []
    for h in result.hits:
        hits.append({
            "chunk_id": h.chunk_id,
            "doc_title": h.doc_title or "Untitled",
            "page_start": h.page_start,
            "page_end": h.page_end,
            "text": h.chunk_text[:500],
            "score": h.score,
        })

    return json.dumps({"results": hits, "count": len(hits)})


async def _read_passages(args: dict, session: AsyncSession) -> str:
    raw_ids = args.get("chunk_ids", [])
    chunk_uuids = []
    for cid in raw_ids[:10]:  # cap at 10
        try:
            chunk_uuids.append(uuid.UUID(cid))
        except ValueError:
            continue

    if not chunk_uuids:
        return json.dumps({"passages": []})

    result = await session.execute(
        select(Chunk).where(Chunk.chunk_id.in_(chunk_uuids))
    )
    chunks = {c.chunk_id: c for c in result.scalars().all()}

    # Load doc titles
    doc_ids = {c.doc_id for c in chunks.values()}
    docs_result = await session.execute(
        select(Document).where(Document.doc_id.in_(list(doc_ids)))
    )
    docs_by_id = {d.doc_id: d for d in docs_result.scalars().all()}

    passages = []
    for cid in chunk_uuids:
        chunk = chunks.get(cid)
        if chunk is None:
            continue
        doc = docs_by_id.get(chunk.doc_id)
        passages.append({
            "chunk_id": str(cid),
            "doc_title": doc.title if doc else "Untitled",
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "text": chunk.chunk_text,
        })

    return json.dumps({"passages": passages})
