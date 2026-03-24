"""smolagents Tool subclasses wrapping existing execute_tool() dispatch.

Each tool delegates to the same MCP tool functions used by the hand-rolled
research loop and chat, preserving all existing logic unchanged. smolagents
calls forward() synchronously from its own thread, so we create a fresh
event loop per call.
"""

import asyncio

from smolagents import Tool

from harbor_clerk.llm.tools import execute_tool

# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class _ResearchTool(Tool):
    """Base class for research tools wrapping execute_tool()."""

    output_type = "string"
    _tool_name: str = ""  # override in subclasses

    def __init__(self, user_id=None):
        super().__init__()
        self.user_id = user_id

    def _call_tool(self, args: dict) -> str:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(execute_tool(self._tool_name, args, self.user_id, mode="research"))
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class SearchDocumentsTool(_ResearchTool):
    name = "search_documents"
    description = (
        "Search the knowledge base for passages matching a query. Uses hybrid "
        "keyword + semantic search. Returns brief previews with document titles, "
        "page numbers, scores, and section headings. Use many varied queries to "
        "cover different angles. After finding promising results, use read_passages "
        "with the chunk_ids to get full text."
    )
    inputs = {
        "query": {"type": "string", "description": "Search query text"},
        "k": {"type": "integer", "description": "Number of results (default 10, max 100)", "nullable": True},
        "offset": {"type": "integer", "description": "Skip first N results for pagination", "nullable": True},
        "doc_id": {"type": "string", "description": "Restrict search to a specific document ID", "nullable": True},
    }
    _tool_name = "search_documents"

    def forward(self, query: str, k: int | None = None, offset: int | None = None, doc_id: str | None = None) -> str:
        args: dict = {"query": query, "k": min(k or 10, 100), "detail": "brief"}
        if doc_id is not None:
            args["doc_id"] = doc_id
        if offset is not None:
            args["offset"] = offset
        return self._call_tool(args)


class ReadPassagesTool(_ResearchTool):
    name = "read_passages"
    description = (
        "Read full text of specific passages by chunk IDs. Use after "
        "search_documents to fetch complete text of interesting results."
    )
    inputs = {
        "chunk_ids": {"type": "string", "description": "Comma-separated chunk IDs"},
        "include_context": {
            "type": "boolean",
            "description": "Include surrounding text",
            "nullable": True,
        },
    }
    _tool_name = "read_passages"

    def forward(self, chunk_ids: str, include_context: bool | None = None) -> str:
        ids = chunk_ids.split(",") if isinstance(chunk_ids, str) else chunk_ids
        return self._call_tool({"chunk_ids": ids, "include_context": include_context or False})


class ExpandContextTool(_ResearchTool):
    name = "expand_context"
    description = "Get more surrounding text around a specific passage."
    inputs = {
        "chunk_id": {"type": "string", "description": "Chunk ID to expand context around"},
        "n": {
            "type": "integer",
            "description": "Number of chunks before/after to include (default 2)",
            "nullable": True,
        },
    }
    _tool_name = "expand_context"

    def forward(self, chunk_id: str, n: int | None = None) -> str:
        args: dict = {"chunk_id": chunk_id}
        if n is not None:
            args["n"] = n
        return self._call_tool(args)


class GetDocumentTool(_ResearchTool):
    name = "get_document"
    description = (
        "Get full metadata for a document: title, status, summary, MIME type, "
        "file size, and version history with pipeline status."
    )
    inputs = {
        "doc_id": {"type": "string", "description": "Document ID"},
    }
    _tool_name = "get_document"

    def forward(self, doc_id: str) -> str:
        return self._call_tool({"doc_id": doc_id})


class ListDocumentsTool(_ResearchTool):
    name = "list_documents"
    description = (
        "Browse documents ordered by most recently updated. Returns title, summary, "
        "status, version count, and update timestamp. Useful for surveying the "
        "corpus or finding documents by browsing."
    )
    inputs = {
        "limit": {
            "type": "integer",
            "description": "Maximum number of documents to return (default 20, max 100)",
            "nullable": True,
        },
    }
    _tool_name = "list_documents"

    def forward(self, limit: int | None = None) -> str:
        return self._call_tool({"limit": limit or 20})


class CorpusOverviewTool(_ResearchTool):
    name = "corpus_overview"
    description = (
        "Get collection statistics and document list: document count, languages, "
        "file types, date range, plus titles and summaries."
    )
    inputs = {
        "limit": {
            "type": "integer",
            "description": "Max documents to list (default 20, max 50)",
            "nullable": True,
        },
    }
    _tool_name = "corpus_overview"

    def forward(self, limit: int | None = None) -> str:
        return self._call_tool({"limit": min(limit or 20, 50)})


class DocumentOutlineTool(_ResearchTool):
    name = "document_outline"
    description = (
        "Get a document's heading hierarchy (h1-h6), page count, and chunk count. "
        "Use to understand document structure before reading specific sections."
    )
    inputs = {
        "doc_id": {"type": "string", "description": "Document ID"},
    }
    _tool_name = "document_outline"

    def forward(self, doc_id: str) -> str:
        return self._call_tool({"doc_id": doc_id})


class FindRelatedTool(_ResearchTool):
    name = "find_related"
    description = (
        "Find documents most similar to a given document by content similarity. "
        "Returns related documents with titles, summaries, and similarity scores."
    )
    inputs = {
        "doc_id": {"type": "string", "description": "Document ID to find related documents for"},
        "k": {
            "type": "integer",
            "description": "Number of related documents (default 5)",
            "nullable": True,
        },
    }
    _tool_name = "find_related"

    def forward(self, doc_id: str, k: int | None = None) -> str:
        args: dict = {"doc_id": doc_id}
        if k is not None:
            args["k"] = k
        return self._call_tool(args)


class EntitySearchTool(_ResearchTool):
    name = "entity_search"
    description = (
        "Search for named entities (people, organizations, places, dates, etc.) by name. "
        "Returns entity mentions with document and chunk references."
    )
    inputs = {
        "query": {"type": "string", "description": "Entity name or partial name to search for"},
        "entity_type": {
            "type": "string",
            "description": "Filter by type: PERSON, ORG, GPE, LOC, DATE, etc.",
            "nullable": True,
        },
        "doc_id": {
            "type": "string",
            "description": "Restrict search to a specific document",
            "nullable": True,
        },
    }
    _tool_name = "entity_search"

    def forward(self, query: str, entity_type: str | None = None, doc_id: str | None = None) -> str:
        args: dict = {"query": query}
        if entity_type is not None:
            args["entity_type"] = entity_type
        if doc_id is not None:
            args["doc_id"] = doc_id
        return self._call_tool(args)


class EntityOverviewTool(_ResearchTool):
    name = "entity_overview"
    description = (
        "Get entity statistics: type distribution (PERSON, ORG, GPE, etc.), "
        "total/unique counts, and top 20 most-mentioned entities."
    )
    inputs = {
        "doc_id": {
            "type": "string",
            "description": "Document ID (omit for corpus-wide overview)",
            "nullable": True,
        },
    }
    _tool_name = "entity_overview"

    def forward(self, doc_id: str | None = None) -> str:
        args: dict = {}
        if doc_id is not None:
            args["doc_id"] = doc_id
        return self._call_tool(args)


class EntityCooccurrenceTool(_ResearchTool):
    name = "entity_cooccurrence"
    description = (
        "Find entities that appear alongside a given entity in the same text chunk. "
        "Reveals relationships (e.g., which people are mentioned with an organization)."
    )
    inputs = {
        "entity_text": {"type": "string", "description": "Entity name to find co-occurrences for"},
        "entity_type": {
            "type": "string",
            "description": "Filter source entity by type: PERSON, ORG, GPE, LOC, DATE, etc.",
            "nullable": True,
        },
        "cooccur_type": {
            "type": "string",
            "description": "Filter co-occurring entities by type",
            "nullable": True,
        },
        "doc_id": {
            "type": "string",
            "description": "Restrict to a specific document",
            "nullable": True,
        },
    }
    _tool_name = "entity_cooccurrence"

    def forward(
        self,
        entity_text: str,
        entity_type: str | None = None,
        cooccur_type: str | None = None,
        doc_id: str | None = None,
    ) -> str:
        args: dict = {"entity_text": entity_text, "scope": "chunk"}
        if entity_type is not None:
            args["entity_type"] = entity_type
        if cooccur_type is not None:
            args["cooccur_type"] = cooccur_type
        if doc_id is not None:
            args["doc_id"] = doc_id
        return self._call_tool(args)


class ReadDocumentTool(_ResearchTool):
    name = "read_document"
    description = (
        "Read a document's text by page range. Use after checking document_outline "
        "to target specific sections. Returns page-level text with OCR metadata."
    )
    inputs = {
        "doc_id": {"type": "string", "description": "Document ID"},
        "page_start": {
            "type": "integer",
            "description": "First page to read (default: first page)",
            "nullable": True,
        },
        "page_end": {
            "type": "integer",
            "description": "Last page to read (default: last page)",
            "nullable": True,
        },
    }
    _tool_name = "read_document"

    def forward(self, doc_id: str, page_start: int | None = None, page_end: int | None = None) -> str:
        args: dict = {"doc_id": doc_id}
        if page_start is not None:
            args["page_start"] = page_start
        if page_end is not None:
            args["page_end"] = page_end
        return self._call_tool(args)


class CorpusTopicsTool(Tool):
    """Special case: calls get_topics_for_tool() directly instead of execute_tool()."""

    name = "corpus_topics"
    description = "List the main topics in the knowledge base with keywords and document counts."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        from harbor_clerk.topics import get_topics_for_tool

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(get_topics_for_tool())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_research_tools(user_id=None) -> list[Tool]:
    """Build and return all research tool instances."""
    return [
        SearchDocumentsTool(user_id),
        ReadPassagesTool(user_id),
        ExpandContextTool(user_id),
        GetDocumentTool(user_id),
        ListDocumentsTool(user_id),
        CorpusOverviewTool(user_id),
        DocumentOutlineTool(user_id),
        FindRelatedTool(user_id),
        EntitySearchTool(user_id),
        EntityOverviewTool(user_id),
        EntityCooccurrenceTool(user_id),
        ReadDocumentTool(user_id),
        CorpusTopicsTool(),
    ]
