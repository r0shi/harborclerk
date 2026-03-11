"""Chat tool definitions and executor — delegates to MCP tool functions.

Chat tools are simplified versions of the full MCP tools, tailored for
small local LLMs (4-8B params). Not all MCP tools are exposed here:

  Omitted (MCP-only):
    - kb_batch_search: multi-query patterns unlikely from small models
    - kb_reprocess / kb_system_health: admin-only, not useful in chat

  Simplified:
    - entity_cooccurrence: hardcodes scope="chunk" (no document scope)
    - search_documents: caps k=10, no pagination/faceted/detail modes

  TODO: revisit these simplifications when upgrading LLM models.
  Larger models may benefit from kb_batch_search, document-scope
  co-occurrence, and richer search parameters.
"""

import json
import logging
import uuid

logger = logging.getLogger(__name__)

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search the knowledge base for passages matching a query. Uses hybrid keyword + semantic search. Returns ranked results with document titles, page numbers, scores, and section headings. This is your primary tool for finding information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results (default 5, max 10)",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Restrict search to a specific document ID",
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
            "description": "Read full text of specific passages by chunk IDs. Use after search_documents to fetch complete text of interesting results. Set include_context=true to also get surrounding text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of chunk IDs to read",
                    },
                    "include_context": {
                        "type": "boolean",
                        "description": "Include surrounding text for each passage (default false)",
                    },
                },
                "required": ["chunk_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_context",
            "description": "Get more surrounding text around a specific passage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": "Chunk ID to expand context around",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of chunks before/after to include (default 2)",
                    },
                },
                "required": ["chunk_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": "Get full metadata for a document: title, status, summary, MIME type, file size, and version history with pipeline status. Use to inspect a document after finding it via search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "Browse documents ordered by most recently updated. Returns title, summary, status, version count, and update timestamp. Shows a paginated subset — check total_count and truncated in the response to know if more documents exist. NOT for finding documents by topic — use search_documents instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of documents to return (default 20, max 100)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corpus_overview",
            "description": "Get collection statistics and document list: document count, languages, file types, date range, plus titles and summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max documents to list (default 20, max 50)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "document_outline",
            "description": "Get a document's heading hierarchy (h1-h6), page count, and chunk count. Use to understand document structure before reading specific sections with read_document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_related",
            "description": "Find documents most similar to a given document by content similarity. Returns related documents with titles, summaries, and similarity scores. Use to discover related content or topic clusters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID to find related documents for",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of related documents (default 5)",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "entity_search",
            "description": "Search for named entities (people, organizations, places, dates, etc.) by name. Returns entity mentions with document and chunk references. Filter by entity_type (PERSON, ORG, GPE, LOC, DATE) and/or doc_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Entity name or partial name to search for",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "Filter by type: PERSON, ORG, GPE, LOC, DATE, etc.",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Restrict search to a specific document",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "entity_overview",
            "description": "Get entity statistics: type distribution (PERSON, ORG, GPE, etc.), total/unique counts, and top 20 most-mentioned entities. Omit doc_id for corpus-wide overview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID (omit for corpus-wide overview)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "entity_cooccurrence",
            "description": "Find entities that appear alongside a given entity in the same text chunk. Reveals relationships (e.g., which people are mentioned with an organization). Filter co-occurring entities by type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_text": {
                        "type": "string",
                        "description": "Entity name or partial name to find co-occurrences for",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "Filter source entity by type: PERSON, ORG, GPE, LOC, DATE, etc.",
                    },
                    "cooccur_type": {
                        "type": "string",
                        "description": "Filter co-occurring entities by type",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Restrict to a specific document",
                    },
                },
                "required": ["entity_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read a document's text by page range. CAUTION: full documents can be very large and exhaust context. Always prefer search_documents + read_passages for targeted retrieval. Only use this for specific page ranges after checking document_outline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID",
                    },
                    "page_start": {
                        "type": "integer",
                        "description": "First page to read (default: first page)",
                    },
                    "page_end": {
                        "type": "integer",
                        "description": "Last page to read (default: last page)",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_status",
            "description": "Check ingestion pipeline progress for a document: shows each stage (extract→ocr→chunk→entities→embed→summarize→finalize) with status, progress counts, and errors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID to check",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
]


# Map chat tool names → (MCP function import path, arg mapper)
def _map_args_search(args: dict) -> dict:
    return {
        "query": args["query"],
        "k": min(args.get("k", 5), 10),
        "doc_id": args.get("doc_id"),
        "detail": "full",
    }


def _map_args_read_passages(args: dict) -> dict:
    return {
        "chunk_ids": args["chunk_ids"],
        "include_context": args.get("include_context", False),
    }


def _map_args_expand_context(args: dict) -> dict:
    return {
        "chunk_id": args["chunk_id"],
        "n": args.get("n", 2),
    }


def _map_args_get_document(args: dict) -> dict:
    return {"doc_id": args["doc_id"]}


def _map_args_list_documents(args: dict) -> dict:
    return {"limit": args.get("limit", 20)}


def _map_args_corpus_overview(args: dict) -> dict:
    # Chat LLMs get a smaller default to avoid context overflow
    return {"limit": min(args.get("limit", 20), 50)}


def _map_args_document_outline(args: dict) -> dict:
    return {"doc_id": args["doc_id"]}


def _map_args_find_related(args: dict) -> dict:
    return {"doc_id": args["doc_id"], "k": args.get("k", 5)}


def _map_args_entity_search(args: dict) -> dict:
    mapped = {"query": args["query"]}
    if args.get("entity_type"):
        mapped["entity_type"] = args["entity_type"]
    if args.get("doc_id"):
        mapped["doc_id"] = args["doc_id"]
    return mapped


def _map_args_entity_overview(args: dict) -> dict:
    mapped: dict = {}
    if args.get("doc_id"):
        mapped["doc_id"] = args["doc_id"]
    return mapped


def _map_args_entity_cooccurrence(args: dict) -> dict:
    mapped: dict = {"entity_text": args["entity_text"], "scope": "chunk"}
    if args.get("entity_type"):
        mapped["entity_type"] = args["entity_type"]
    if args.get("cooccur_type"):
        mapped["cooccur_type"] = args["cooccur_type"]
    if args.get("doc_id"):
        mapped["doc_id"] = args["doc_id"]
    return mapped


def _map_args_read_document(args: dict) -> dict:
    mapped: dict = {"doc_id": args["doc_id"]}
    if args.get("page_start") is not None:
        mapped["page_start"] = args["page_start"]
    if args.get("page_end") is not None:
        mapped["page_end"] = args["page_end"]
    return mapped


def _map_args_ingest_status(args: dict) -> dict:
    return {"doc_id": args["doc_id"]}


_TOOL_DISPATCH: dict[str, tuple[str, callable]] = {
    "search_documents": ("kb_search", _map_args_search),
    "read_passages": ("kb_read_passages", _map_args_read_passages),
    "expand_context": ("kb_expand_context", _map_args_expand_context),
    "get_document": ("kb_get_document", _map_args_get_document),
    "list_documents": ("kb_list_recent", _map_args_list_documents),
    "corpus_overview": ("kb_corpus_overview", _map_args_corpus_overview),
    "document_outline": ("kb_document_outline", _map_args_document_outline),
    "find_related": ("kb_find_related", _map_args_find_related),
    "entity_search": ("kb_entity_search", _map_args_entity_search),
    "entity_overview": ("kb_entity_overview", _map_args_entity_overview),
    "entity_cooccurrence": ("kb_entity_cooccurrence", _map_args_entity_cooccurrence),
    "read_document": ("kb_read_document", _map_args_read_document),
    "ingest_status": ("kb_ingest_status", _map_args_ingest_status),
}


async def execute_tool(name: str, arguments: dict, user_id: uuid.UUID | None = None) -> str:
    """Execute a chat tool by delegating to the corresponding MCP function."""
    from harbor_clerk.api.deps import Principal
    from harbor_clerk.mcp_server import _mcp_principal

    entry = _TOOL_DISPATCH.get(name)
    if entry is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    mcp_func_name, arg_mapper = entry
    mapped_args = arg_mapper(arguments)

    # Set MCP auth context so the tool function sees the chat user
    token = None
    if user_id is not None:
        principal = Principal(type="user", id=user_id, role="user")
        token = _mcp_principal.set(principal)

    try:
        # Import the MCP function by name
        import harbor_clerk.mcp_server as mcp_mod

        func = getattr(mcp_mod, mcp_func_name)
        return await func(**mapped_args)
    except PermissionError as e:
        logger.warning("Tool permission error: %s - %s", name, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("Tool execution error: %s", name)
        return json.dumps({"error": str(e)})
    finally:
        if token is not None:
            _mcp_principal.reset(token)
