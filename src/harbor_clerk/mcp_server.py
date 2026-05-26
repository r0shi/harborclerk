"""MCP server — tools for Claude to query the knowledge base."""

import contextvars
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from harbor_clerk.api.deps import Principal
from harbor_clerk.auth import API_KEY_PREFIXES, decode_token, hash_api_key
from harbor_clerk.config import get_settings, refresh_cli_access_setting
from harbor_clerk.db import async_session_factory
from harbor_clerk.mcp_discriminator import _compute_discriminator_hint
from harbor_clerk.mcp_lookup_tools import (
    documents_by_date as _documents_by_date_impl,
)
from harbor_clerk.mcp_lookup_tools import (
    verify_identifier as _verify_identifier_impl,
)
from harbor_clerk.models import (
    ApiKey,
    Chunk,
    Document,
    DocumentHeading,
    DocumentLink,
    DocumentPage,
    Entity,
    IngestionJob,
)
from harbor_clerk.models.enums import JobStage, PipelineStatus
from harbor_clerk.oauth import validate_access_token as validate_oauth_access_token
from harbor_clerk.search import SearchHit, SearchResult, hybrid_search
from harbor_clerk.sql_escape import escape_ilike

logger = logging.getLogger(__name__)

# Runtime stats for kb_search usage monitoring
_search_stats = {
    "calls": 0,
    "total_k": 0,
    "max_k": 0,
    "cap_hits": 0,
    "pagination_calls": 0,
    "faceted_calls": 0,
    "detail_full": 0,
    "detail_brief": 0,
    "detail_compact": 0,
}
_STATS_LOG_INTERVAL = 50

# Context variable set by auth middleware before tool execution
_mcp_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "_mcp_principal",
    default=None,
)

# True when the current request comes from harbor-clerk-cli (UA prefix match)
_mcp_is_cli: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_mcp_is_cli",
    default=False,
)

_CLI_USER_AGENT_PREFIX = "harbor-clerk-cli/"


def _request_type_for_ua() -> str:
    """Return 'cli_tool' if the request is from harbor-clerk-cli, else 'mcp_tool'."""
    return "cli_tool" if _mcp_is_cli.get() else "mcp_tool"


async def _resolve_principal(token: str) -> Principal | None:
    """Validate a Bearer token (JWT or API key) and return a Principal."""
    if not token.startswith(API_KEY_PREFIXES):
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                return None
            return Principal(
                type="user",
                id=uuid.UUID(payload["sub"]),
                role=payload["role"],
            )
        except Exception:
            pass

        # JWT failed — try OAuth access token
        try:
            async with async_session_factory() as db:
                result = await validate_oauth_access_token(db, token)
                if result is not None:
                    await db.commit()  # persist last_used_at update
                    user_id, role = result
                    return Principal(type="oauth", id=user_id, role=role)
        except Exception:
            logger.debug("OAuth token validation failed", exc_info=True)
        return None

    # API key lookup
    key_hash = hash_api_key(token)
    async with async_session_factory() as session:
        result = await session.execute(
            select(ApiKey).where(
                ApiKey.key_hash == key_hash,
                ApiKey.is_active.is_(True),
            )
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            return None
        # Expiry check
        if api_key.expires_at is not None and api_key.expires_at < datetime.now(UTC):
            return None
        await session.execute(
            update(ApiKey).where(ApiKey.key_id == api_key.key_id).values(last_used_at=datetime.now(UTC))
        )
        await session.commit()
        from harbor_clerk.api.scope import KeyScope

        scope = KeyScope(
            scope_topic_ids=api_key.scope_topic_ids,
            scope_folder_ids=api_key.scope_folder_ids,
            permission_tier=api_key.permission_tier,
            tool_overrides=api_key.tool_overrides or {},
            max_snippet_chars=api_key.max_snippet_chars,
            rate_limit_rpm=api_key.rate_limit_rpm,
            rate_limit_rph=api_key.rate_limit_rph,
        )
        return Principal(type="api_key", id=api_key.key_id, role="user", key_scope=scope)


class MCPAuthMiddleware:
    """ASGI middleware that extracts Bearer token and sets _mcp_principal."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Pass through lifespan events untouched so the MCP session manager initializes
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            principal = await _resolve_principal(token)
            if principal is not None:
                # Detect whether this is a harbor-clerk-cli request
                ua = headers.get(b"user-agent", b"").decode()
                is_cli = ua.startswith(_CLI_USER_AGENT_PREFIX)

                # Gate: CLI traffic blocked unless enable_cli_access is True.
                # Refresh from native config.json on every CLI request so
                # toggling in macOS Preferences takes effect without restart.
                if is_cli:
                    refresh_cli_access_setting()
                if is_cli and not get_settings().enable_cli_access:
                    from harbor_clerk.api.request_log import log_api_request

                    try:
                        async with async_session_factory() as log_session:
                            await log_api_request(
                                log_session,
                                api_key_id=principal.id if principal.type == "api_key" else None,
                                request_type="cli_tool",
                                endpoint="<gate>",
                                status="denied",
                                status_detail="cli_access_disabled",
                            )
                            await log_session.commit()
                    except Exception:
                        logger.debug("Failed to log CLI gate denial", exc_info=True)

                    body = json.dumps(
                        {
                            "error": "cli_access_disabled",
                            "hint": "Enable in System Settings → Integrations",
                        }
                    ).encode()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [
                                [b"content-type", b"application/json"],
                                [b"content-length", str(len(body)).encode()],
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return

                reset_token = _mcp_principal.set(principal)
                reset_cli_token = _mcp_is_cli.set(is_cli)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _mcp_principal.reset(reset_token)
                    _mcp_is_cli.reset(reset_cli_token)
                return

        # No valid auth — return 401 JSON
        body = json.dumps({"error": "Unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class MCPTokenPathAuth:
    """ASGI middleware that extracts an API key from the URL path.

    Intended for authless MCP clients (Claude.ai, ChatGPT) that cannot
    send custom Authorization headers.  The user pastes a URL like
    ``https://tunnel.example.com/t/hc_abc123...`` into the client's
    connector settings.  This middleware extracts the key from the first
    path segment, validates it, and rewrites the path before forwarding
    to the inner MCP ASGI app.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        parts = path.strip("/").split("/", 1)
        token = parts[0] if parts else ""

        if not token or not token.startswith(API_KEY_PREFIXES):
            await self._send_401(send)
            return

        principal = await _resolve_principal(token)
        if principal is None:
            await self._send_401(send)
            return

        # Rewrite path: strip the token, keep the rest (if any).
        # Token and MCP paths (/mcp, /sse) are always ASCII, so encoding
        # the decoded remaining path is equivalent to slicing raw_path.
        remaining = "/" + parts[1] if len(parts) > 1 else "/"
        scope = dict(scope)
        scope["path"] = remaining
        scope["raw_path"] = remaining.encode("ascii")

        # NOTE: This path-auth middleware (used by Claude.ai / ChatGPT URL-paste
        # connectors) intentionally does NOT apply the CLI access gate or set the
        # `_mcp_is_cli` contextvar. The `harbor-clerk` CLI is expected to use
        # Bearer-header auth on /mcp, which is gated by MCPAuthMiddleware. If a
        # CLI client is ever wired to use URL-token auth, the gate logic from
        # MCPAuthMiddleware must be replicated here.
        reset_token = _mcp_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _mcp_principal.reset(reset_token)

    @staticmethod
    async def _send_401(send):
        body = json.dumps({"error": "Invalid or missing API key in URL path"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _get_principal() -> Principal:
    """Get the current MCP principal or raise."""
    p = _mcp_principal.get()
    if p is None:
        raise PermissionError("Not authenticated")
    return p


def _filter_tools_for_principal(tool_names, principal):
    """Return only the tool names this principal is allowed to use."""
    if principal is None:
        return []
    if principal.type != "api_key" or principal.key_scope is None:
        # Human users see everything (admin checks happen inside individual tools)
        return list(tool_names)
    allowed = principal.key_scope.effective_tools
    return [t for t in tool_names if t in allowed]


def _require_admin() -> Principal:
    p = _get_principal()
    if p.role != "admin":
        raise PermissionError("Admin access required")
    return p


async def _visible_doc_ids(session: AsyncSession, principal: Principal) -> set[uuid.UUID] | None:
    """Get the set of doc_ids visible to this principal, or None if unrestricted.

    Returns None for human users and unrestricted API keys (no filter needed).
    Returns the explicit set for scoped keys — callers use this to filter results.
    Only active (non-removed) documents are included.
    """
    if principal.type != "api_key" or principal.key_scope is None or principal.key_scope.is_unrestricted:
        return None
    from harbor_clerk.api.scope import apply_key_scope

    query = apply_key_scope(select(Document.doc_id).where(Document.status == "active"), principal)
    result = await session.execute(query)
    return {row[0] for row in result.all()}


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402


class ScopedFastMCP(FastMCP):
    """FastMCP subclass that filters tool listing and calls by API key scope.

    Security: list_tools is a UX layer; the real enforcement is call_tool +
    the apply_key_scope filters in each tool body. So even if list_tools is
    bypassed (e.g. via the lowlevel server's tool cache), tool calls are
    still rejected.
    """

    async def list_tools(self):
        all_tools = await super().list_tools()
        try:
            principal = _get_principal()
        except PermissionError:
            # No principal context (e.g. internal tool-cache refresh).
            # Return [] rather than all_tools so we don't pollute caches with
            # full visibility. Real per-call enforcement happens in call_tool.
            return []
        if principal.type != "api_key" or principal.key_scope is None:
            return all_tools
        allowed = principal.key_scope.effective_tools
        return [t for t in all_tools if t.name in allowed]

    async def call_tool(self, name, arguments):
        try:
            principal = _get_principal()
        except PermissionError:
            return await super().call_tool(name, arguments)

        # Only instrument API key calls
        if principal.type != "api_key":
            return await super().call_tool(name, arguments)

        import time

        from harbor_clerk.api.request_log import log_api_request

        # --- Rate limit check ---
        if principal.key_scope is not None:
            from harbor_clerk.api.rate_limiter import rate_limiter
            from harbor_clerk.config import get_settings as _get_settings

            _s = _get_settings()
            _rpm = (
                principal.key_scope.rate_limit_rpm
                if principal.key_scope.rate_limit_rpm is not None
                else _s.default_rate_limit_rpm
            )
            _rph = (
                principal.key_scope.rate_limit_rph
                if principal.key_scope.rate_limit_rph is not None
                else _s.default_rate_limit_rph
            )
            allowed, retry_after = await rate_limiter.check(principal.id, _rpm, _rph)
            if not allowed:
                logger.warning(
                    "Rate limit exceeded for key %s on MCP tool %s (retry_after=%ds)",
                    principal.id,
                    name,
                    retry_after,
                )
                try:
                    async with async_session_factory() as log_session:
                        await log_api_request(
                            log_session,
                            api_key_id=principal.id,
                            request_type=_request_type_for_ua(),
                            endpoint=name,
                            parameters=dict(arguments) if arguments else None,
                            status="rate_limited",
                            status_detail=f"retry_after={retry_after}s",
                            duration_ms=0,
                        )
                        await log_session.commit()
                except Exception:
                    logger.debug("Failed to log rate-limited MCP call", exc_info=True)

                from mcp.server.fastmcp.exceptions import ToolError

                raise ToolError(f"Rate limit exceeded. Try again in {retry_after} seconds.")

        # --- Denial check ---
        if principal.key_scope is not None and name not in principal.key_scope.effective_tools:
            try:
                async with async_session_factory() as log_session:
                    await log_api_request(
                        log_session,
                        api_key_id=principal.id,
                        request_type=_request_type_for_ua(),
                        endpoint=name,
                        parameters=arguments if arguments else None,
                        status="denied",
                        status_detail="tool not in key scope",
                    )
                    await log_session.commit()
            except Exception:
                logger.debug("Failed to log denied MCP tool call", exc_info=True)

            from mcp.server.fastmcp.exceptions import ToolError

            raise ToolError(f"Unknown tool: {name}")

        # --- Execute tool ---
        t0 = time.monotonic()
        try:
            result = await super().call_tool(name, arguments)
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                async with async_session_factory() as log_session:
                    await log_api_request(
                        log_session,
                        api_key_id=principal.id,
                        request_type=_request_type_for_ua(),
                        endpoint=name,
                        parameters=arguments if arguments else None,
                        status="error",
                        status_detail=str(exc)[:500],
                        duration_ms=duration_ms,
                    )
                    await log_session.commit()
            except Exception:
                logger.debug("Failed to log errored MCP tool call", exc_info=True)
            raise

        # --- Success logging ---
        duration_ms = int((time.monotonic() - t0) * 1000)
        try:
            # Extract result summary from CallToolResult
            result_summary: dict | None = None
            if result and hasattr(result, "content") and result.content:
                for item in result.content:
                    if hasattr(item, "text") and item.text:
                        try:
                            parsed = json.loads(item.text)
                            if isinstance(parsed, dict):
                                summary: dict = {}
                                for key in ("total_candidates", "count", "has_more", "would_match_unscoped"):
                                    if key in parsed:
                                        summary[key] = parsed[key]
                                if "hits" in parsed and isinstance(parsed["hits"], list):
                                    summary["hits"] = len(parsed["hits"])
                                if summary:
                                    result_summary = summary
                        except (json.JSONDecodeError, TypeError):
                            pass
                        break

            async with async_session_factory() as log_session:
                await log_api_request(
                    log_session,
                    api_key_id=principal.id,
                    request_type=_request_type_for_ua(),
                    endpoint=name,
                    parameters=arguments if arguments else None,
                    status="ok",
                    result_summary=result_summary,
                    duration_ms=duration_ms,
                )
                await log_session.commit()
        except Exception:
            logger.debug("Failed to log successful MCP tool call", exc_info=True)

        return result


mcp = ScopedFastMCP(
    "Harbor Clerk",
    # DNS rebinding protection is unnecessary — we run behind Caddy with
    # our own Bearer-token auth middleware wrapping the MCP ASGI app.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    # Stateless mode: each request spawns a fresh run_server task, so contextvars
    # (including _mcp_principal) are captured per-request instead of at session
    # establishment. Required for scoped API keys — otherwise all tool calls in a
    # session would use the first request's principal snapshot.
    stateless_http=True,
)


async def _resolve_headings(
    session: AsyncSession,
    hits: list[SearchHit],
) -> dict[tuple[str, int], str]:
    """Bulk-resolve the nearest heading at or before each hit's page.

    Returns a map of (doc_id_str, page_start) → heading title.
    """
    # Collect unique (doc_id, page_start) pairs that have pages
    pairs: set[tuple[uuid.UUID, int]] = set()
    for h in hits:
        if h.page_start is not None:
            pairs.add((uuid.UUID(h.doc_id), h.page_start))

    if not pairs:
        return {}

    # Load all headings for the relevant documents
    doc_ids = list({did for did, _ in pairs})
    heading_result = await session.execute(
        select(DocumentHeading)
        .where(DocumentHeading.doc_id.in_(doc_ids))
        .order_by(DocumentHeading.doc_id, DocumentHeading.position)
    )
    headings = heading_result.scalars().all()

    # Group headings by doc_id
    headings_by_doc: dict[uuid.UUID, list[DocumentHeading]] = {}
    for heading in headings:
        headings_by_doc.setdefault(heading.doc_id, []).append(heading)

    # For each (doc_id, page), find the nearest heading at or before that page
    result_map: dict[tuple[str, int], str] = {}
    for did, page in pairs:
        doc_headings = headings_by_doc.get(did, [])
        best: DocumentHeading | None = None
        for heading in doc_headings:
            if heading.page_num is not None and heading.page_num <= page:
                best = heading
            elif heading.page_num is not None and heading.page_num > page:
                break
        if best is not None:
            result_map[(str(did), page)] = best.title

    return result_map


def _format_search_response(
    result: SearchResult,
    detail: str,
    effective_brief_chars: int,
    k: int,
    offset: int,
    heading_map: dict[tuple[str, int], str] | None = None,
) -> dict:
    """Build the non-faceted search response dict from a SearchResult.

    Shared between kb_search and kb_batch_search.
    """
    hits = []
    for h in result.hits:
        hit: dict = {
            "chunk_id": h.chunk_id,
            "doc_id": h.doc_id,
            "doc_title": h.doc_title,
            "score": h.score,
            "language": h.language,
        }
        if h.page_start is not None:
            hit["pages"] = f"{h.page_start}-{h.page_end}" if h.page_end != h.page_start else str(h.page_start)
            # Include nearest heading for document context
            if heading_map:
                heading = heading_map.get((h.doc_id, h.page_start))
                if heading:
                    hit["section"] = heading
        # Detail mode formatting
        if detail == "full":
            hit["text"] = h.chunk_text
        elif detail == "brief":
            text = h.chunk_text
            if len(text) > effective_brief_chars:
                hit["text"] = text[:effective_brief_chars] + "\u2026"
            else:
                hit["text"] = text
        # compact: no text field
        hits.append(hit)

    has_more = offset + k < result.total_candidates

    resp: dict = {
        "hits": hits,
        "total_candidates": result.total_candidates,
        "has_more": has_more,
    }
    if result.possible_conflict:
        resp["possible_conflict"] = True
        resp["conflict_sources"] = [{"doc_id": cs.doc_id, "title": cs.title} for cs in result.conflict_sources]
    return resp


@mcp.tool()
async def kb_search(
    query: str,
    k: int = 10,
    offset: int = 0,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict | None = None,
    faceted: bool = False,
) -> str:
    """Search the knowledge base by topic, keyword, or question. Hybrid FTS + vector retrieval.

    Use this as the PRIMARY tool to find information in the corpus.

    What you get back:
      - `hits`: ranked chunks with score, doc_id, doc_title, pages, section heading
      - `total_candidates`: how many chunks matched before pagination
      - `has_more`: true if more results exist beyond your window — paginate via `offset`
        or refine your query if you don't yet have the answer
      - `discriminator_hint` (when present): top hits span multiple docs that differ on
        a structured metadata field. Use `metadata_filter` with the suggested key/value
        to pin the right doc. The hint includes `ambiguous_doc_titles`,
        `differing_metadata` (per-doc values), and a `suggestion` string.

    When to iterate:
      - One query returned ambiguous results across multiple docs → call `kb_batch_search`
        with varied query angles, OR use `metadata_filter` (from the discriminator_hint)
        to pin the right doc
      - `has_more` is true and you don't have the answer yet → paginate with `offset`
      - The top hit's chunk text doesn't fully answer the question → call
        `kb_read_passages` on the chunk_id to verify the surrounding text

    How to decline:
      - If retrieved chunks DON'T contain the answer the question asks for (e.g. the
        question mentions an invoice number / contract / person that doesn't appear in
        any retrieved doc), the information is NOT in the corpus — say so plainly. Do
        NOT report a "closest match" as a substitute. Adjacent or partial matches are
        not answers.

    Filters (all optional):
      doc_id: restrict to a single document (mutually exclusive with doc_ids)
      doc_ids: restrict to multiple documents (list of UUIDs, max 50)
      after: only documents updated at or after this ISO datetime
      before: only documents updated before this ISO datetime
      language: chunk language filter ("english" or "french")
      mime_type: document MIME type filter (e.g. "application/pdf")
      metadata_filter: dict of {"namespace.key": value} pairs to match against
        Document.metadata. Use to disambiguate when multiple candidate docs share
        text but differ on a structured field. Example:
        metadata_filter={"sidecar.vendor": "Acme", "sidecar.term_months": 24}.
        Inspect a document's available metadata via kb_get_document.

    detail levels control how much text is returned per hit:
      "full" (default): complete chunk text — best for reading a small
        number of high-confidence results carefully
      "brief": first ~200 characters per chunk (adjustable via brief_chars) —
        use when scanning 20-50 results to identify which are worth
        reading in full via kb_read_passages
      "compact": metadata only (chunk_id, doc_id, doc_title, score, pages,
        language — no text) — use when surveying a broad result set (50+) to
        understand score distribution and document coverage before narrowing down

    faceted: if true, groups hits by document with per-document top_score
      and hit_count — useful for understanding which documents are most
      relevant at a glance
    """
    principal = _get_principal()
    settings = get_settings()

    # Mutual exclusion check
    if doc_id is not None and doc_ids is not None:
        return json.dumps({"error": "Cannot specify both doc_id and doc_ids"})

    did = uuid.UUID(doc_id) if doc_id else None

    # Parse doc_ids
    parsed_doc_ids = None
    if doc_ids is not None:
        if len(doc_ids) > 50:
            return json.dumps({"error": "doc_ids limited to 50 entries"})
        try:
            parsed_doc_ids = [uuid.UUID(d) for d in doc_ids]
        except ValueError:
            return json.dumps({"error": "Invalid UUID in doc_ids"})

    # Parse dates
    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for after: {after}"})
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for before: {before}"})

    # Clamp parameters
    k = max(1, min(k, settings.mcp_max_k))
    offset = max(0, offset)
    if detail not in ("full", "brief", "compact"):
        detail = "full"

    is_scoped = False
    async with async_session_factory() as session:
        # Per-API-key scope: intersect with visible doc_ids for scoped keys.
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None:
            is_scoped = True
            if not visible_ids:
                # Check if unscoped search would have results
                unscoped_count = 0
                try:
                    unscoped_result = await hybrid_search(session, query, k=1)
                    unscoped_count = unscoped_result.total_candidates
                except Exception:
                    pass
                resp_empty: dict = {"hits": [], "total_candidates": 0, "has_more": False}
                if unscoped_count > 0:
                    resp_empty["would_match_unscoped"] = unscoped_count
                return json.dumps(resp_empty, indent=2)
            if did is not None and did not in visible_ids:
                return json.dumps({"hits": [], "total_candidates": 0, "has_more": False}, indent=2)
            if parsed_doc_ids is not None:
                parsed_doc_ids = [d for d in parsed_doc_ids if d in visible_ids]
                if not parsed_doc_ids:
                    return json.dumps({"hits": [], "total_candidates": 0, "has_more": False}, indent=2)
            elif did is None:
                parsed_doc_ids = list(visible_ids)

        result = await hybrid_search(
            session,
            query,
            k=k,
            doc_id=did,
            offset=offset,
            doc_ids=parsed_doc_ids,
            after=parsed_after,
            before=parsed_before,
            language=language,
            mime_type=mime_type,
            metadata_filter=metadata_filter,
        )
        heading_map = await _resolve_headings(session, result.hits)
        # Compute discriminator_hint if applicable. Cheap post-processing:
        # one indexed SELECT for top-K candidate docs' metadata. Skips when
        # fewer than 2 hits.
        discriminator_hint = await _compute_discriminator_hint(result.hits, session)

    # Resolve brief_chars for brief mode
    effective_brief_chars = brief_chars if brief_chars > 0 else settings.mcp_brief_chars

    base_resp = _format_search_response(result, detail, effective_brief_chars, k, offset, heading_map)

    if faceted:
        # Group hits by doc_id
        groups: dict[str, list[dict]] = {}
        for h in base_resp["hits"]:
            groups.setdefault(h["doc_id"], []).append(h)
        doc_groups = []
        for did_str, group_hits in groups.items():
            doc_groups.append(
                {
                    "doc_id": did_str,
                    "doc_title": group_hits[0].get("doc_title"),
                    "top_score": max(h["score"] for h in group_hits),
                    "hit_count": len(group_hits),
                    "hits": group_hits,
                }
            )
        doc_groups.sort(key=lambda g: g["top_score"], reverse=True)
        resp: dict = {
            "documents": doc_groups,
            "total_candidates": result.total_candidates,
            "has_more": base_resp["has_more"],
        }
    else:
        resp = base_resp

    # Annotate scope-filtered empty results
    if is_scoped and not resp.get("hits") and not resp.get("documents") and resp.get("total_candidates", 0) == 0:
        try:
            async with async_session_factory() as unscoped_session:
                unscoped_result = await hybrid_search(unscoped_session, query, k=1)
                if unscoped_result.total_candidates > 0:
                    resp["would_match_unscoped"] = unscoped_result.total_candidates
        except Exception:
            pass

    # Runtime stats
    _search_stats["calls"] += 1
    _search_stats["total_k"] += k
    _search_stats["max_k"] = max(_search_stats["max_k"], k)
    if k >= settings.mcp_max_k:
        _search_stats["cap_hits"] += 1
    if offset > 0:
        _search_stats["pagination_calls"] += 1
    if faceted:
        _search_stats["faceted_calls"] += 1
    _search_stats[f"detail_{detail}"] += 1

    if _search_stats["calls"] % _STATS_LOG_INTERVAL == 0:
        n = _search_stats["calls"]
        logger.info(
            "kb_search stats (%d calls): avg_k=%.0f, max_k=%d, cap_hit_rate=%.0f%%, "
            "pagination_rate=%.0f%%, faceted_rate=%.0f%%, detail: full=%d brief=%d compact=%d",
            n,
            _search_stats["total_k"] / n,
            _search_stats["max_k"],
            100 * _search_stats["cap_hits"] / n,
            100 * _search_stats["pagination_calls"] / n,
            100 * _search_stats["faceted_calls"] / n,
            _search_stats["detail_full"],
            _search_stats["detail_brief"],
            _search_stats["detail_compact"],
        )

    if discriminator_hint is not None:
        resp["discriminator_hint"] = discriminator_hint

    return json.dumps(resp, indent=2)


@mcp.tool()
async def kb_batch_search(
    queries: list[str],
    k: int = 10,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict | None = None,
) -> str:
    """Run multiple search queries in one call (max 5), grouped per query.

    PREFER THIS OVER multiple sequential kb_search calls when you need to:
      - Triangulate a single answer from multiple angles ("does this contract
        mention X, Y, or Z?")
      - Compare relevance of related concepts in one round-trip
      - Disambiguate ambiguous results from a single kb_search by probing
        with varied query phrasings

    When to use it:
      - One kb_search returned ambiguous results from multiple docs → run 2-3
        varied queries here to see which doc consistently ranks first across
        angles (docs appearing in multiple batch queries are strongly
        corroborated as the right match)
      - You need to check several related facts in one go without serial
        round-trips

    What you get back:
      Per-query result dicts (hits, total_candidates, has_more) plus the same
      `discriminator_hint` field on each query's response when applicable.
      Treat each query's response the same way you'd treat a single kb_search
      response — pagination, iteration, and decline rules are identical.

    How to decline:
      Same as kb_search — if NONE of your queries returned a doc matching the
      question's identifier (invoice number / contract / person / etc.), the
      information is NOT in the corpus. Say so plainly rather than reporting
      adjacent matches as substitutes.

    All filters (doc_id, doc_ids, after, before, language, mime_type,
    metadata_filter) are shared across queries — see kb_search for documentation.
    """
    principal = _get_principal()
    settings = get_settings()

    # Validate query count
    if not queries:
        return json.dumps({"error": "queries must contain at least 1 query"})
    if len(queries) > 5:
        return json.dumps({"error": "queries limited to max 5 per call"})

    # Mutual exclusion check
    if doc_id is not None and doc_ids is not None:
        return json.dumps({"error": "Cannot specify both doc_id and doc_ids"})

    try:
        did = uuid.UUID(doc_id) if doc_id else None
    except ValueError:
        return json.dumps({"error": f"Invalid UUID for doc_id: {doc_id}"})

    # Parse doc_ids
    parsed_doc_ids = None
    if doc_ids is not None:
        if len(doc_ids) > 50:
            return json.dumps({"error": "doc_ids limited to 50 entries"})
        try:
            parsed_doc_ids = [uuid.UUID(d) for d in doc_ids]
        except ValueError:
            return json.dumps({"error": "Invalid UUID in doc_ids"})

    # Parse dates
    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for after: {after}"})
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for before: {before}"})

    # Clamp parameters
    k = max(1, min(k, settings.mcp_max_k))
    if detail not in ("full", "brief", "compact"):
        detail = "full"

    effective_brief_chars = brief_chars if brief_chars > 0 else settings.mcp_brief_chars

    results = []
    async with async_session_factory() as session:
        # Per-API-key scope: intersect with visible doc_ids for scoped keys.
        visible_ids = await _visible_doc_ids(session, principal)
        scope_blocks_all = False
        if visible_ids is not None:
            if not visible_ids or (did is not None and did not in visible_ids):
                scope_blocks_all = True
            elif parsed_doc_ids is not None:
                parsed_doc_ids = [d for d in parsed_doc_ids if d in visible_ids]
                if not parsed_doc_ids:
                    scope_blocks_all = True
            elif did is None:
                parsed_doc_ids = list(visible_ids)

        for query in queries:
            if scope_blocks_all:
                resp: dict = {"hits": [], "total_candidates": 0, "has_more": False, "query": query}
                results.append(resp)
                continue
            result = await hybrid_search(
                session,
                query,
                k=k,
                doc_id=did,
                offset=0,
                doc_ids=parsed_doc_ids,
                after=parsed_after,
                before=parsed_before,
                language=language,
                mime_type=mime_type,
                metadata_filter=metadata_filter,
            )
            heading_map = await _resolve_headings(session, result.hits)
            resp = _format_search_response(result, detail, effective_brief_chars, k, 0, heading_map)
            resp["query"] = query
            hint = await _compute_discriminator_hint(result.hits, session)
            if hint is not None:
                resp["discriminator_hint"] = hint
            results.append(resp)

            # Runtime stats per query
            _search_stats["calls"] += 1
            _search_stats["total_k"] += k
            _search_stats["max_k"] = max(_search_stats["max_k"], k)
            if k >= settings.mcp_max_k:
                _search_stats["cap_hits"] += 1
            _search_stats[f"detail_{detail}"] += 1

    if _search_stats["calls"] % _STATS_LOG_INTERVAL == 0 and _search_stats["calls"] > 0:
        n = _search_stats["calls"]
        logger.info(
            "kb_search stats (%d calls): avg_k=%.0f, max_k=%d, cap_hit_rate=%.0f%%, "
            "detail: full=%d brief=%d compact=%d",
            n,
            _search_stats["total_k"] / n,
            _search_stats["max_k"],
            100 * _search_stats["cap_hits"] / n,
            _search_stats["detail_full"],
            _search_stats["detail_brief"],
            _search_stats["detail_compact"],
        )

    return json.dumps({"results": results}, indent=2)


@mcp.tool()
async def kb_read_passages(
    chunk_ids: list[str],
    include_context: bool = False,
) -> str:
    """Read specific passages by chunk_id. Use to verify content before answering.

    Use this after kb_search to:
      - Verify the top hit actually contains the answer the question asks for
        (the chunk text in kb_search results is sometimes truncated or
        out-of-context — read the full passage before committing)
      - Read a few high-confidence hits in full when the chunk text in
        kb_search wasn't enough context to answer
      - Confirm that a specific named entity / number / clause is present
        in the cited chunk before claiming it (the verify-before-answer
        pattern — protects against hallucinating from adjacent text)

    Output: list of passages with full chunk_text, doc_id, doc_title, pages,
    and section heading. Set include_context=True to also get the chunks
    immediately before and after each requested chunk (useful when the
    target chunk references "as discussed above" or similar).

    Take this seriously: before reporting a specific number, date, name, or
    clause as an answer, READ THE CHUNK that supposedly contains it. If the
    chunk doesn't actually contain it, the kb_search hit was a near-miss
    rather than a real match — search with different queries or decline.
    """
    principal = _get_principal()
    uuids = [uuid.UUID(cid) for cid in chunk_ids]

    async with async_session_factory() as session:
        result = await session.execute(select(Chunk).where(Chunk.chunk_id.in_(uuids)))
        chunks = {c.chunk_id: c for c in result.scalars().all()}

        # Per-API-key scope: drop chunks from documents the key can't see.
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None:
            chunks = {cid: c for cid, c in chunks.items() if c.doc_id in visible_ids}

        # Doc titles
        doc_ids = {c.doc_id for c in chunks.values()}
        docs_result = await session.execute(select(Document).where(Document.doc_id.in_(list(doc_ids))))
        docs = {d.doc_id: d for d in docs_result.scalars().all()}

        # Snippet truncation limit from key scope, if any
        snippet_limit: int | None = None
        if principal.key_scope and principal.key_scope.max_snippet_chars is not None:
            snippet_limit = principal.key_scope.max_snippet_chars

        passages = []
        for cid in uuids:
            chunk = chunks.get(cid)
            if chunk is None:
                continue
            doc = docs.get(chunk.doc_id)
            text = chunk.chunk_text
            if snippet_limit is not None:
                text = text[:snippet_limit]
            p: dict = {
                "chunk_id": str(cid),
                "doc_title": doc.title if doc else None,
                "text": text,
                "language": chunk.language,
            }
            if chunk.page_start is not None:
                p["pages"] = (
                    f"{chunk.page_start}-{chunk.page_end}"
                    if chunk.page_end != chunk.page_start
                    else str(chunk.page_start)
                )

            if include_context:
                prev = await session.execute(
                    select(Chunk.chunk_text).where(
                        Chunk.doc_id == chunk.doc_id,
                        Chunk.chunk_num == chunk.chunk_num - 1,
                    )
                )
                prev_text = prev.scalar_one_or_none()
                if prev_text:
                    if snippet_limit is not None:
                        prev_text = prev_text[:snippet_limit]
                    p["context_before"] = prev_text

                nxt = await session.execute(
                    select(Chunk.chunk_text).where(
                        Chunk.doc_id == chunk.doc_id,
                        Chunk.chunk_num == chunk.chunk_num + 1,
                    )
                )
                nxt_text = nxt.scalar_one_or_none()
                if nxt_text:
                    if snippet_limit is not None:
                        nxt_text = nxt_text[:snippet_limit]
                    p["context_after"] = nxt_text

            passages.append(p)

    return json.dumps({"passages": passages}, indent=2)


@mcp.tool()
async def kb_expand_context(chunk_id: str, n: int = 2) -> str:
    """Read N chunks immediately before/after a given chunk_id.

    Use after kb_search or kb_read_passages when the chunk you got back
    doesn't show enough surrounding context to fully understand it —
    e.g. when the chunk text references "as described above" or "see
    section 4" or the answer spans a chunk boundary.

    Pair with kb_read_passages: kb_read_passages reads SPECIFIC chunks you
    already know about; kb_expand_context fetches the surrounding CONTEXT
    (n chunks before + n after the target).

    n controls the window size (default 2 chunks each direction = 5 total).
    Returns the chunks in order with the target chunk marked is_target=True.
    """
    principal = _get_principal()
    n = max(1, min(n, 10))
    target_id = uuid.UUID(chunk_id)

    async with async_session_factory() as session:
        target = (await session.execute(select(Chunk).where(Chunk.chunk_id == target_id))).scalar_one_or_none()
        if target is None:
            return json.dumps({"error": "Chunk not found"})

        # Per-API-key scope: block access if the target doc isn't visible.
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and target.doc_id not in visible_ids:
            return json.dumps({"error": "Chunk not found"})

        result = await session.execute(
            select(Chunk)
            .where(
                Chunk.doc_id == target.doc_id,
                Chunk.chunk_num.between(target.chunk_num - n, target.chunk_num + n),
            )
            .order_by(Chunk.chunk_num)
        )
        neighbours = result.scalars().all()

        doc = (await session.execute(select(Document).where(Document.doc_id == target.doc_id))).scalar_one_or_none()

        # Snippet truncation limit from key scope, if any
        snippet_limit: int | None = None
        if principal.key_scope and principal.key_scope.max_snippet_chars is not None:
            snippet_limit = principal.key_scope.max_snippet_chars

        chunks = []
        for c in neighbours:
            text = c.chunk_text
            if snippet_limit is not None:
                text = text[:snippet_limit]
            entry: dict = {
                "chunk_id": str(c.chunk_id),
                "chunk_num": c.chunk_num,
                "text": text,
                "language": c.language,
            }
            if c.page_start is not None:
                entry["pages"] = f"{c.page_start}-{c.page_end}" if c.page_end != c.page_start else str(c.page_start)
            if c.chunk_id == target_id:
                entry["is_target"] = True
            chunks.append(entry)

    return json.dumps(
        {
            "doc_id": str(target.doc_id),
            "doc_title": doc.title if doc else None,
            "target_chunk_num": target.chunk_num,
            "chunks": chunks,
        },
        indent=2,
    )


@mcp.tool()
async def kb_get_document(doc_id: str) -> str:
    """Get a document's metadata + summary by doc_id. Use to inspect structure before deeper queries.

    What you get back:
      - Title, mime_type, summary (LLM-generated 1-paragraph overview), section
        headings outline, ingestion status, chunk count
      - `metadata`: the document's structured metadata extracted at ingest
        (sidecar facts, Tika fields, frontmatter, etc.). The keys here are
        EXACTLY the filter keys you can pass to kb_search via metadata_filter
        — e.g. `metadata.sidecar.vendor` becomes
        `metadata_filter={"sidecar.vendor": "..."}`

    When to use it:
      - You want to inspect what filter keys exist on a doc before crafting
        a metadata_filter for kb_search
      - You need the summary + structure of a doc to decide whether it's
        worth reading in full via kb_read_document
      - You got a doc_id from kb_search or kb_find_related and want quick
        context before reading chunks

    Output shape: a single dict with the doc's metadata + headings; does NOT
    include chunk text (use kb_read_document or kb_read_passages for that).
    """
    principal = _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and did not in visible_ids:
            return json.dumps({"error": "Document not found"})

        result = await session.execute(select(Document).where(Document.doc_id == did))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        jobs_result = await session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == did).order_by(IngestionJob.created_at)
        )
        jobs = [
            {"stage": j.stage.value, "status": j.status.value, "error": j.error} for j in jobs_result.scalars().all()
        ]

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "status": doc.status,
            "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
            "summary": doc.summary,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "extracted_chars": doc.extracted_chars,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            "metadata": doc.doc_metadata,
            "jobs": jobs,
        },
        indent=2,
    )


@mcp.tool()
async def kb_list_recent(limit: int = 20) -> str:
    """List the most recently-added documents in the corpus.

    Use for temporal queries — "what was added last week", "show me the
    newest contracts", "any recent meeting minutes about X". Quick way to
    see what's NEW without running a content search.

    Returns up to `limit` docs (default 20) sorted by created_at descending.
    Use kb_search with `after` / `before` filters if you need a specific
    date range rather than just "most recent".
    """
    principal = _get_principal()

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)

        count_q = select(func.count()).select_from(Document).where(Document.status == "active")
        list_q = (
            select(Document)
            .where(Document.status == "active")
            .order_by(Document.updated_at.desc())
            .limit(min(limit, 100))
        )
        if visible_ids is not None:
            if not visible_ids:
                return json.dumps({"total_count": 0, "truncated": False, "documents": []}, indent=2)
            count_q = count_q.where(Document.doc_id.in_(visible_ids))
            list_q = list_q.where(Document.doc_id.in_(visible_ids))

        total_result = await session.execute(count_q)
        total_count = total_result.scalar() or 0

        result = await session.execute(list_q)
        docs = result.scalars().all()

    items = []
    for doc in docs:
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": doc.summary,
                "status": doc.status,
                "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
                "updated_at": doc.updated_at.isoformat(),
            }
        )

    return json.dumps(
        {"total_count": total_count, "truncated": total_count > len(items), "documents": items},
        indent=2,
    )


@mcp.tool()
async def kb_corpus_overview(limit: int = 50) -> str:
    """Survey the corpus: doc types, date ranges, sample titles.

    Use FIRST when you don't know the corpus shape — what kinds of documents
    exist, what time periods are covered, what topics dominate. This is the
    right starting tool when the user's question is broad ("what's in this
    corpus?") or when you're not sure what to search for.

    What you get back:
      - Document count by type (invoice, contract, policy, etc.)
      - Date range of documents in the corpus
      - Sample titles (first `limit` docs by recency)
      - Top entities / topics if available

    When to use:
      - First call in a new conversation when you don't know the corpus
      - User asks "what kinds of docs do you have?" / "what topics?"
      - You want to scope a search ("are there any 2024 contracts?") before
        running kb_search

    Does NOT return chunk text — for actual content, follow up with
    kb_search or kb_list_recent.

    Args:
        limit: Maximum number of documents to include in the list
            (default 50, max 500). Increase if you need to scan more
            documents; decrease for faster responses with large corpora.
            The response includes a 'truncated' flag and total
            'document_count' so you know if more exist."""
    principal = _get_principal()

    limit = max(1, min(limit, 500))

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and not visible_ids:
            return json.dumps(
                {
                    "document_count": 0,
                    "total_chunks": 0,
                    "total_pages": 0,
                    "languages": {},
                    "mime_types": {},
                    "date_range": {"oldest": None, "newest": None},
                    "documents": [],
                    "truncated": False,
                },
                indent=2,
            )

        def _scoped(q):
            if visible_ids is not None:
                return q.where(Document.doc_id.in_(visible_ids))
            return q

        doc_count_result = await session.execute(
            _scoped(select(func.count()).select_from(Document).where(Document.status == "active"))
        )
        doc_count = doc_count_result.scalar() or 0

        chunk_count_result = await session.execute(
            _scoped(
                select(func.count())
                .select_from(Chunk)
                .join(Document, Document.doc_id == Chunk.doc_id)
                .where(Document.status == "active")
            )
        )
        chunk_count = chunk_count_result.scalar() or 0

        page_count_result = await session.execute(
            _scoped(
                select(func.count())
                .select_from(DocumentPage)
                .join(Document, Document.doc_id == DocumentPage.doc_id)
                .where(Document.status == "active")
            )
        )
        total_pages = page_count_result.scalar() or 0

        # Language distribution from chunks (scoped to active docs)
        lang_rows = (
            await session.execute(
                _scoped(
                    select(Chunk.language, func.count())
                    .join(Document, Document.doc_id == Chunk.doc_id)
                    .where(Document.status == "active")
                )
                .group_by(Chunk.language)
                .order_by(func.count().desc())
            )
        ).all()
        languages = {row[0]: row[1] for row in lang_rows if row[0]}

        # Mime type breakdown from active docs
        mime_rows = (
            await session.execute(
                _scoped(
                    select(Document.mime_type, func.count()).where(
                        Document.status == "active", Document.mime_type.isnot(None)
                    )
                )
                .group_by(Document.mime_type)
                .order_by(func.count().desc())
            )
        ).all()
        mime_types = {row[0]: row[1] for row in mime_rows if row[0]}

        # Date range
        date_result = await session.execute(
            _scoped(
                select(func.min(Document.updated_at), func.max(Document.updated_at)).where(Document.status == "active")
            )
        )
        date_row = date_result.one()
        oldest = date_row[0].isoformat() if date_row[0] else None
        newest = date_row[1].isoformat() if date_row[1] else None

        list_q = select(Document).where(Document.status == "active").order_by(Document.updated_at.desc()).limit(limit)
        if visible_ids is not None:
            list_q = list_q.where(Document.doc_id.in_(visible_ids))
        result = await session.execute(list_q)
        docs = result.scalars().all()

    items = []
    for doc in docs:
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": doc.summary,
                "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
                "updated_at": doc.updated_at.isoformat(),
            }
        )

    return json.dumps(
        {
            "document_count": doc_count,
            "total_chunks": chunk_count,
            "total_pages": total_pages,
            "languages": languages,
            "mime_types": mime_types,
            "date_range": {"oldest": oldest, "newest": newest},
            "documents": items,
            "truncated": doc_count > len(items),
        },
        indent=2,
    )


@mcp.tool()
async def kb_ingest_status(doc_id: str) -> str:
    """Inspect a document's ingestion pipeline status (operator-facing).

    Use when troubleshooting why a doc isn't appearing in searches or
    when investigating ingestion failures. Returns the stage-by-stage
    status (extract, ocr, chunk, embed, entities, summarize, finalize)
    plus any error messages.

    Rarely needed during normal query flow — models should reach for this
    only when the user is asking about ingestion state, not content.
    """
    principal = _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and did not in visible_ids:
            return json.dumps({"error": "Document not found"})

        result = await session.execute(select(Document).where(Document.doc_id == did))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        jobs_result = await session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == did).order_by(IngestionJob.created_at)
        )
        jobs = [
            {
                "stage": j.stage.value,
                "status": j.status.value,
                "progress": f"{j.progress_current}/{j.progress_total}" if j.progress_total else None,
                "error": j.error,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in jobs_result.scalars().all()
        ]

    return json.dumps(
        {
            "doc_id": str(did),
            "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
            "jobs": jobs,
        },
        indent=2,
    )


@mcp.tool()
async def kb_reprocess(doc_id: str) -> str:
    """Re-run the ingestion pipeline for a specific document. ADMIN-ONLY.

    Use when a previously-ingested doc has issues (bad summary, missing
    entities, OCR errors) and the operator wants to reprocess from
    scratch without re-uploading. Re-runs the full extract → ocr → chunk →
    embed → entities → summarize → finalize chain.

    Rarely useful during normal query flow. Requires admin permissions.
    """
    _require_admin()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        doc.pipeline_status = PipelineStatus.queued
        doc.pipeline_seq = (doc.pipeline_seq or 0) + 1
        doc.error = None
        await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage, reset_jobs

    reset_jobs(did)
    enqueue_stage(did, JobStage.extract)

    return json.dumps(
        {
            "doc_id": str(did),
            "status": "reprocessing",
        }
    )


@mcp.tool()
async def kb_document_outline(doc_id: str) -> str:
    """Get a document's section structure (table of contents).

    Use to navigate inside a document by section heading. Pair with
    kb_read_passages to read specific sections you've identified.

    When to use:
      - You have a doc_id and want to know WHERE in the doc to look
        (e.g. "the contract has a Termination section — let me read just
        that")
      - You're answering a question about doc structure ("how many
        sections does this report have?")

    Returns the heading hierarchy with chunk_ids per heading, so you can
    follow up with kb_read_passages([chunk_id, ...]) to read a specific
    section without dumping the full document.
    """
    principal = _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and did not in visible_ids:
            return json.dumps({"error": "Document not found"})

        result = await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        headings_result = await session.execute(
            select(DocumentHeading).where(DocumentHeading.doc_id == did).order_by(DocumentHeading.position)
        )
        headings = headings_result.scalars().all()

        page_count = (
            await session.execute(select(func.count()).select_from(DocumentPage).where(DocumentPage.doc_id == did))
        ).scalar_one()

        chunk_count = (
            await session.execute(select(func.count()).select_from(Chunk).where(Chunk.doc_id == did))
        ).scalar_one()

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "headings": [
                {
                    "level": h.level,
                    "title": h.title,
                    "page_num": h.page_num,
                }
                for h in headings
            ],
        },
        indent=2,
    )


@mcp.tool()
async def kb_find_related(doc_id: str, k: int = 5) -> str:
    """Find documents related to a given doc_id by semantic overlap.

    Use to EXPAND a relevance set: you have one good hit from kb_search and
    want to find docs that cover the same topic / cite each other / share
    entities. Complement to kb_search — kb_search starts from a QUERY,
    kb_find_related starts from a KNOWN DOC.

    When to use:
      - Found one relevant doc; need to see what else in the corpus is
        similar (e.g. "this is the Q3 board minutes — what other meeting
        minutes discuss the same topics?")
      - Need to triangulate a fact: read related docs to confirm a claim
      - Mapping a corpus around a known anchor doc

    Returns top-K related docs with their titles + relevance scores. To
    actually read the related docs' content, follow up with kb_get_document
    or kb_search.

    Args:
        doc_id: The document to find related documents for.
        k: Number of related documents to return (1-20, default 5).
    """
    principal = _get_principal()
    k = max(1, min(k, 20))

    try:
        target_id = uuid.UUID(doc_id)
    except ValueError:
        return json.dumps({"error": f"Invalid doc_id: {doc_id}"})

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and target_id not in visible_ids:
            return json.dumps({"error": "Document not found"})

        # Verify document exists
        doc = (
            await session.execute(select(Document).where(Document.doc_id == target_id, Document.status == "active"))
        ).scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        # Explicit wikilink graph: docs this doc links to OR docs that link
        # to this doc, both via resolved DocumentLink rows. Computed up front
        # so linked docs can be returned even when the source doc has no
        # embeddings yet (e.g. while the embed stage is still pending).
        link_rows = (
            await session.execute(
                select(DocumentLink.src_doc_id, DocumentLink.target_doc_id).where(
                    DocumentLink.resolved.is_(True),
                    or_(
                        DocumentLink.src_doc_id == target_id,
                        DocumentLink.target_doc_id == target_id,
                    ),
                )
            )
        ).all()
        linked_ids: list[uuid.UUID] = []
        seen_linked: set[uuid.UUID] = set()
        for src, tgt in link_rows:
            other = tgt if src == target_id else src
            if other is None or other == target_id or other in seen_linked:
                continue
            if visible_ids is not None and other not in visible_ids:
                continue
            seen_linked.add(other)
            linked_ids.append(other)

        # Get all embeddings for this document's chunks
        rows = (
            await session.execute(
                select(Chunk.embedding).where(
                    Chunk.doc_id == target_id,
                    Chunk.embedding.isnot(None),
                )
            )
        ).all()

        nearest: list = []
        distances: dict[uuid.UUID, float] = {}
        if rows:
            # Compute average embedding in Python
            dim = len(rows[0][0])
            avg = [0.0] * dim
            for (emb,) in rows:
                for i, v in enumerate(emb):
                    avg[i] += v
            n = len(rows)
            avg = [v / n for v in avg]

            # Find nearest chunks from OTHER active documents
            distance = Chunk.embedding.cosine_distance(avg)
            nearest_q = (
                select(
                    Chunk.doc_id,
                    func.min(distance).label("min_distance"),
                )
                .join(Document, Document.doc_id == Chunk.doc_id)
                .where(
                    Document.status == "active",
                    Chunk.embedding.isnot(None),
                    Chunk.doc_id != target_id,
                )
            )
            if visible_ids is not None:
                nearest_q = nearest_q.where(Chunk.doc_id.in_(visible_ids))
            nearest = (
                await session.execute(nearest_q.group_by(Chunk.doc_id).order_by(func.min(distance)).limit(k))
            ).all()
            distances = {row[0]: float(row[1]) for row in nearest}

        # Embedding-nearest results (deduplicated against linked).
        nearest_ids = [row[0] for row in nearest if row[0] not in seen_linked]

        # Merge — linked first, then embedding-nearest — capped at k.
        merged_ids: list[uuid.UUID] = (linked_ids + nearest_ids)[:k]
        if not merged_ids:
            payload: dict = {"doc_id": doc_id, "related": []}
            if not rows:
                payload["note"] = "No embeddings available"
            return json.dumps(payload)

        docs_result = await session.execute(
            select(Document).where(Document.doc_id.in_(merged_ids), Document.status == "active")
        )
        related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in merged_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        if rid in seen_linked:
            similarity = 1.0
            source = "linked"
        else:
            similarity = round(1.0 - distances[rid], 4)
            source = "embedding"
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": rdoc.summary,
                "similarity": similarity,
                "source": source,
                "doc_type": rdoc.doc_type,
                "mime_type": rdoc.mime_type,
                "canonical_filename": rdoc.canonical_filename,
            }
        )

    return json.dumps(
        {"doc_id": doc_id, "related": items},
        indent=2,
    )


@mcp.tool()
async def kb_entity_search(
    query: str,
    entity_type: str | None = None,
    doc_id: str | None = None,
    deduplicate: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find documents that mention a specific named entity (person, organization, place).

    Use when:
      - The question is about a SPECIFIC named entity (e.g. "What did Alice
        Johnson say in board meetings?", "Which contracts mention Acme Corp?")
      - You want to disambiguate between similarly-named entities
      - kb_search by free-text returned too many false matches because the
        entity name is also a common word

    Returns docs containing the entity, with mention count + sample chunks.
    Pair with kb_read_passages to read the specific mentions in context.

    For broader entity surveys (which entities appear most, which are linked),
    use kb_entity_overview instead.
    """
    principal = _get_principal()
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # Escape ILIKE metacharacters in query (helper at src/harbor_clerk/sql_escape.py)
    escaped_query = escape_ilike(query)

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and not visible_ids:
            return json.dumps({"entities": [], "total": 0, "has_more": False})

        # Base filter: active docs, latest version
        base_filter = [
            Entity.entity_text.ilike(f"%{escaped_query}%"),
        ]

        if entity_type:
            base_filter.append(Entity.entity_type == entity_type)

        if doc_id:
            did = uuid.UUID(doc_id)
            if visible_ids is not None and did not in visible_ids:
                return json.dumps({"error": "Document not found"})
            # Scope to the document
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            base_filter.append(Entity.doc_id == did)
        else:
            # Scope to active docs
            base_filter.append(Entity.doc_id.in_(select(Document.doc_id).where(Document.status == "active")))
            if visible_ids is not None:
                base_filter.append(Entity.doc_id.in_(visible_ids))

        if deduplicate:
            count_q = (
                select(
                    Entity.entity_text,
                    Entity.entity_type,
                    func.count().label("mention_count"),
                )
                .where(*base_filter)
                .group_by(Entity.entity_text, Entity.entity_type)
            )
            # Total
            total_q = select(func.count()).select_from(count_q.subquery())
            total = (await session.execute(total_q)).scalar() or 0

            rows = (await session.execute(count_q.order_by(func.count().desc()).offset(offset).limit(limit))).all()
            entities = [
                {
                    "entity_text": r[0],
                    "entity_type": r[1],
                    "mention_count": r[2],
                }
                for r in rows
            ]
        else:
            # Total count
            total = (await session.execute(select(func.count()).select_from(Entity).where(*base_filter))).scalar() or 0

            rows = (
                (
                    await session.execute(
                        select(Entity).where(*base_filter).order_by(Entity.entity_text).offset(offset).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            entities = [
                {
                    "entity_id": str(e.entity_id),
                    "entity_text": e.entity_text,
                    "entity_type": e.entity_type,
                    "doc_id": str(e.doc_id),
                    "chunk_id": str(e.chunk_id),
                    "start_char": e.start_char,
                    "end_char": e.end_char,
                }
                for e in rows
            ]

    return json.dumps(
        {
            "entities": entities,
            "total": total,
            "has_more": offset + limit < total,
        },
        indent=2,
    )


@mcp.tool()
async def kb_entity_overview(doc_id: str | None = None) -> str:
    """Survey entities in the corpus (or scoped to a single doc).

    Use when:
      - The user asks "who/what is mentioned in this corpus?"
      - You want to find the most-discussed entities by category
        (people, organizations, places, dates)
      - You want a quick entity inventory for a specific document
        (pass doc_id to scope)

    Returns top entities by mention count, grouped by type. Pair with
    kb_entity_search to find docs mentioning a specific entity you spot
    in the overview, or kb_entity_cooccurrence to see which entities
    appear together.
    """
    principal = _get_principal()

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        empty_resp = {
            "total_entities": 0,
            "unique_entities": 0,
            "type_distribution": {},
            "top_entities": [],
        }
        if visible_ids is not None and not visible_ids:
            return json.dumps(empty_resp)

        # Build doc filter
        if doc_id:
            did = uuid.UUID(doc_id)
            if visible_ids is not None and did not in visible_ids:
                return json.dumps({"error": "Document not found"})
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            version_filter = [Entity.doc_id == did]
        else:
            version_filter = [Entity.doc_id.in_(select(Document.doc_id).where(Document.status == "active"))]
            if visible_ids is not None:
                version_filter.append(Entity.doc_id.in_(visible_ids))

        # Total entities
        total = (await session.execute(select(func.count()).select_from(Entity).where(*version_filter))).scalar() or 0

        # Unique entities
        unique_q = select(Entity.entity_text, Entity.entity_type).where(*version_filter).distinct()
        unique = (await session.execute(select(func.count()).select_from(unique_q.subquery()))).scalar() or 0

        # Type distribution
        type_rows = (
            await session.execute(
                select(Entity.entity_type, func.count())
                .where(*version_filter)
                .group_by(Entity.entity_type)
                .order_by(func.count().desc())
            )
        ).all()
        type_distribution = {row[0]: row[1] for row in type_rows}

        # Top entities by mention count (deduplicated)
        top_rows = (
            await session.execute(
                select(
                    Entity.entity_text,
                    Entity.entity_type,
                    func.count().label("mention_count"),
                )
                .where(*version_filter)
                .group_by(Entity.entity_text, Entity.entity_type)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
        top_entities = [
            {
                "entity_text": r[0],
                "entity_type": r[1],
                "mention_count": r[2],
            }
            for r in top_rows
        ]

    return json.dumps(
        {
            "total_entities": total,
            "unique_entities": unique,
            "type_distribution": type_distribution,
            "top_entities": top_entities,
        },
        indent=2,
    )


@mcp.tool()
async def kb_entity_cooccurrence(
    entity_text: str,
    entity_type: str | None = None,
    scope: str = "chunk",
    cooccur_type: str | None = None,
    doc_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find which entities appear together in the same documents or chunks.

    Use when:
      - You want to understand relationships between entities
        (e.g. "which executives are mentioned alongside Project X?",
        "who works with whom?")
      - The question is about WHO/WHAT shares context with a known entity
      - You're mapping a network of related people/orgs across the corpus

    Returns pairs of co-occurring entities with their joint frequency.
    Pair with kb_entity_search on a specific entity from the result to
    see the actual documents where the co-occurrence happens.
    """
    principal = _get_principal()

    if scope not in ("chunk", "document"):
        return json.dumps({"error": f"Invalid scope '{scope}'. Must be 'chunk' or 'document'."})

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    escaped_text = escape_ilike(entity_text)

    e1 = aliased(Entity, name="e1")
    e2 = aliased(Entity, name="e2")

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        empty_resp = {"entity_text": entity_text, "scope": scope, "cooccurrences": [], "total": 0, "has_more": False}
        if visible_ids is not None and not visible_ids:
            return json.dumps(empty_resp)

        # Doc filter: active docs
        if doc_id:
            did = uuid.UUID(doc_id)
            if visible_ids is not None and did not in visible_ids:
                return json.dumps({"error": "Document not found"})
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            version_filter_e1 = [e1.doc_id == did]
            version_filter_e2 = [e2.doc_id == did]
        else:
            active_doc_ids = select(Document.doc_id).where(Document.status == "active")
            version_filter_e1 = [e1.doc_id.in_(active_doc_ids)]
            version_filter_e2 = [e2.doc_id.in_(active_doc_ids)]
            if visible_ids is not None:
                version_filter_e1.append(e1.doc_id.in_(visible_ids))
                version_filter_e2.append(e2.doc_id.in_(visible_ids))

        # Source entity filter
        source_filter = [e1.entity_text.ilike(f"%{escaped_text}%"), *version_filter_e1]
        if entity_type:
            source_filter.append(e1.entity_type == entity_type)

        # Join condition based on scope
        if scope == "chunk":
            join_cond = e2.chunk_id == e1.chunk_id
        else:
            join_cond = e2.doc_id == e1.doc_id

        # Co-occurring entity filters
        cooccur_filter = [*version_filter_e2]
        if cooccur_type:
            cooccur_filter.append(e2.entity_type == cooccur_type)

        # Exclude self-matches
        self_exclude = ~(
            (func.lower(e2.entity_text) == func.lower(e1.entity_text)) & (e2.entity_type == e1.entity_type)
        )

        # Build the query
        base_q = (
            select(
                e2.entity_text,
                e2.entity_type,
                func.count().label("cooccurrence_count"),
                func.array_agg(func.distinct(e2.chunk_id)).label("sample_chunk_ids"),
                func.array_agg(func.distinct(e2.doc_id)).label("sample_doc_ids"),
            )
            .join(e1, join_cond)
            .where(*source_filter, *cooccur_filter, self_exclude)
            .group_by(e2.entity_text, e2.entity_type)
        )

        # Total count
        total = (await session.execute(select(func.count()).select_from(base_q.subquery()))).scalar() or 0

        # Paginated results
        rows = (
            await session.execute(base_q.order_by(func.count().desc(), e2.entity_text).offset(offset).limit(limit))
        ).all()

        cooccurrences = [
            {
                "entity_text": r.entity_text,
                "entity_type": r.entity_type,
                "cooccurrence_count": r.cooccurrence_count,
                "sample_chunk_ids": [str(cid) for cid in r.sample_chunk_ids[:3]],
                "sample_doc_ids": [str(did) for did in list(dict.fromkeys(r.sample_doc_ids))[:3]],
            }
            for r in rows
        ]

    return json.dumps(
        {
            "entity_text": entity_text,
            "scope": scope,
            "cooccurrences": cooccurrences,
            "total": total,
            "has_more": offset + limit < total,
        },
        indent=2,
    )


@mcp.tool()
async def kb_read_document(
    doc_id: str,
    page_start: int | None = None,
    page_end: int | None = None,
    max_chars: int = 50000,
) -> str:
    """Read the full text of a document by doc_id.

    Use when you need the COMPLETE document content — not just metadata or
    a few chunks. Different from kb_get_document, which returns metadata +
    summary + structure but no chunk text.

    CAUTION: full documents can be very large. Prefer kb_search +
    kb_read_passages for targeted reading of specific sections. Use this
    tool only when:
      - The document is short enough to read whole (check kb_get_document
        first to see size)
      - The question requires synthesizing across the entire document
        rather than locating a specific fact
      - kb_search couldn't pin a specific chunk and you need to scan more
        broadly

    Returns the full text in chunk order, with optional pagination.
    """
    principal = _get_principal()
    try:
        did = uuid.UUID(doc_id)
    except ValueError:
        return json.dumps({"error": f"Invalid doc_id: {doc_id}"})
    max_chars = max(1, min(max_chars, 100_000))

    # Per-key snippet cap (applied on top of max_chars)
    if principal.key_scope and principal.key_scope.max_snippet_chars is not None:
        max_chars = min(max_chars, principal.key_scope.max_snippet_chars)

    async with async_session_factory() as session:
        visible_ids = await _visible_doc_ids(session, principal)
        if visible_ids is not None and did not in visible_ids:
            return json.dumps({"error": "Document not found"})

        result = await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        # Total page count
        page_count = (
            await session.execute(select(func.count()).select_from(DocumentPage).where(DocumentPage.doc_id == did))
        ).scalar_one()

        if page_count == 0:
            # Fallback: concatenate chunks (page_start/page_end not applicable)
            chunk_rows = (
                (await session.execute(select(Chunk).where(Chunk.doc_id == did).order_by(Chunk.chunk_num)))
                .scalars()
                .all()
            )

            note = None
            if page_start is not None or page_end is not None:
                note = "page_start/page_end ignored: document has no page records, returning chunk text"

            pages_out: list[dict] = []
            total_chars = 0
            truncated = False
            for c in chunk_rows:
                text = c.chunk_text
                if total_chars + len(text) > max_chars:
                    text = text[: max_chars - total_chars]
                    pages_out.append({"chunk_num": c.chunk_num, "text": text})
                    total_chars += len(text)
                    truncated = True
                    break
                pages_out.append({"chunk_num": c.chunk_num, "text": text})
                total_chars += len(text)

            resp: dict = {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "source": "chunks",
                "page_count": 0,
                "pages_returned": len(pages_out),
                "total_chars": total_chars,
                "truncated": truncated,
                "pages": pages_out,
            }
            if note:
                resp["note"] = note
            return json.dumps(resp, indent=2)

        # Query pages with optional range filter
        query = select(DocumentPage).where(DocumentPage.doc_id == did).order_by(DocumentPage.page_num)
        if page_start is not None:
            query = query.where(DocumentPage.page_num >= page_start)
        if page_end is not None:
            query = query.where(DocumentPage.page_num <= page_end)

        page_rows = (await session.execute(query)).scalars().all()

        pages_out = []
        total_chars = 0
        truncated = False
        for p in page_rows:
            text = p.page_text
            if total_chars + len(text) > max_chars:
                text = text[: max_chars - total_chars]
                pages_out.append(
                    {
                        "page_num": p.page_num,
                        "text": text,
                        "ocr_used": p.ocr_used,
                        "ocr_confidence": p.ocr_confidence,
                    }
                )
                total_chars += len(text)
                truncated = True
                break
            pages_out.append(
                {
                    "page_num": p.page_num,
                    "text": text,
                    "ocr_used": p.ocr_used,
                    "ocr_confidence": p.ocr_confidence,
                }
            )
            total_chars += len(text)

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "source": "pages",
            "page_count": page_count,
            "pages_returned": len(pages_out),
            "total_chars": total_chars,
            "truncated": truncated,
            "pages": pages_out,
        },
        indent=2,
    )


@mcp.tool()
async def kb_verify_identifier(identifier: str) -> str:
    """Verify a document identifier resolves to exactly one document.

    Use this BEFORE quoting a specific document — checks the identifier
    against title, filename, and identifier-like metadata fields. Sharp
    affordance for fabrication-prevention.

    When to call:
      - Before claiming "the X contract says…" — verify X is a real, unique
        identifier first
      - When kb_search returns hits whose titles all share a substring you
        used as an identifier — verify whether you're looking at one doc or
        N variants
      - When the user asks about a specific named document

    What you get back:
      - status="not_found": no document matches — say so plainly; do NOT
        fall back to "closest match"
      - status="unique": one match — its doc_id, title, canonical_filename
      - status="ambiguous": multiple matches — each candidate carries
        discriminating_fields (the metadata that distinguishes them) and
        a suggestion for how to pick one

    When the response is ambiguous, the discriminating_fields per candidate
    tell you what differs — pick the one matching the user's intent, or ask
    a clarifying question. With overflow=true (more than 100 candidates),
    refine the identifier with a more specific substring.

    How to decline:
      - status="not_found" means the identifier is NOT in the corpus. Do
        NOT search by similarity as a substitute — tell the user the
        identifier doesn't exist.
      - status="ambiguous" with no discriminating_fields means the
        candidates are indistinguishable by metadata; inspect with
        kb_get_document if you must.
    """
    async with async_session_factory() as session:
        result = await _verify_identifier_impl(session, identifier)
    return json.dumps(result, default=str)


@mcp.tool()
async def kb_documents_by_date(
    direction: str = "earliest",
    query: str | None = None,
    metadata_filter: dict | None = None,
    after: str | None = None,
    before: str | None = None,
    date_field: str | None = None,
    limit: int = 10,
) -> str:
    """Return documents sorted by their effective date.

    Use this when the user asks for the earliest / latest / first / last
    document matching some criteria — similarity-ranked search (kb_search)
    will not surface boundary docs reliably.

    When to call:
      - "What's the earliest email about X" / "Find the oldest invoice"
      - "What's the latest version of Y" / "Most recent contract with Z"
      - "Show me everything before/after a specific date"
      - When you need chronological ordering, not similarity ordering

    What you get back:
      - direction: echoes the input ("earliest" or "latest")
      - count: number of results returned
      - results[]: each carries doc_id, title, canonical_filename, date
        (ISO 8601 UTC), and date_source — which field the date came from:
          "tika.created_at"  → Tika-extracted email-send / file date
          "frontmatter.date" → markdown frontmatter
          "sidecar.date"     → JSON sidecar
          "ingest"           → fallback to documents.created_at when no
                               metadata date is available

    Parameters:
      direction (default "earliest"): "earliest" | "latest"
      query: optional FTS-only filter (no vector). Returns docs whose chunks
        match the query, sorted by date.
      metadata_filter: dict of {"namespace.key": value} pairs — same shape
        as kb_search's metadata_filter, applied as JSONB containment.
      after, before: ISO 8601 date or datetime strings; bound the
        effective-date range.
      date_field: explicit override for the effective-date source. Accepted
        values: "tika.created_at", "frontmatter.date", "sidecar.date",
        "ingest". When set, no fallback is consulted (docs missing that
        field sort to the end as NULLs).
      limit (default 10): max results.

    How to decline:
      - If results is empty, no docs match the date bounds / query / filter
        — say so plainly. Do NOT broaden the bounds and re-run unless the
        user asks.
      - If the user asks for "the earliest" and the top result's
        date_source is "ingest", note that no metadata date was available
        — the result is sorted by ingest time, not by document content date.
    """
    async with async_session_factory() as session:
        result = await _documents_by_date_impl(
            session,
            direction=direction,
            query=query,
            metadata_filter=metadata_filter,
            after=after,
            before=before,
            date_field=date_field,
            limit=limit,
        )
    return json.dumps(result, default=str)


@mcp.tool()
async def kb_system_health() -> str:
    """Check HC's system health (PostgreSQL, storage, Tika, reranker). DIAGNOSTIC.

    Use when investigating system issues — "why are searches slow", "is the
    embedder up", "is storage reachable". Returns per-component status.

    Rarely useful during normal query flow — models should reach for this
    only when the user is troubleshooting infrastructure.
    """
    _require_admin()

    from sqlalchemy import text

    checks: dict = {}

    async with async_session_factory() as session:
        try:
            await session.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as e:
            checks["postgres"] = f"error: {e}"

    try:
        from harbor_clerk.storage import get_storage

        storage = get_storage()
        storage.bucket_exists("originals")
        checks["storage"] = "ok"
    except Exception as e:
        checks["storage"] = f"error: {e}"

    overall = all(v == "ok" for v in checks.values())
    return json.dumps(
        {
            "status": "healthy" if overall else "degraded",
            "checks": checks,
        },
        indent=2,
    )


def create_mcp_app():
    """Create MCP ASGI apps with auth middleware.

    Returns (header_auth_app, token_path_app, session_manager).

    - header_auth_app: expects ``Authorization: Bearer <key>`` header (mounted at ``/mcp``)
    - token_path_app: expects API key in URL path (mounted at ``/t`` for authless MCP clients)
    - session_manager: must be started via ``async with session_manager.run():``
      in the host application's lifespan
    """
    mcp_http = mcp.streamable_http_app()
    # Dig out the session manager so the host can run it
    session_manager = None
    for route in mcp_http.routes:
        inner = getattr(route, "app", None)
        if hasattr(inner, "session_manager"):
            session_manager = inner.session_manager
            break
    return MCPAuthMiddleware(mcp_http), MCPTokenPathAuth(mcp_http), session_manager
