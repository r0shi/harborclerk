"""Research engine — deterministic plan→search→read→extract→synthesize pipeline.

Replaces the previous smolagents-based agent loop with a structured workflow
where the LLM is used only for bounded tasks (query planning, note extraction,
synthesis) and all retrieval is driven by Python code with measurable coverage.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.health import report_llm_error, report_llm_success
from harbor_clerk.llm.models import get_model
from harbor_clerk.llm.tools import execute_tool
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.document import Document
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 3.5
_SYNTHESIS_TIMEOUT = 600.0
_LLM_TIMEOUT = 120.0

_SYNTHESIS_SYSTEM = (
    "You are writing a research report for Harbor Clerk. Based on the research "
    "notes below, write a clear, well-organized report answering the user's question.\n\n"
    "## Guidelines\n"
    "- Every claim from the corpus must cite its source: [Document Title, page X]\n"
    "- Group findings by theme, not by document\n"
    "- Be thorough but concise — include all relevant findings, skip filler\n"
    "- If the evidence is contradictory or incomplete, say so\n"
    "- You may provide brief contextual framing from general knowledge to help "
    "the reader understand the findings, but clearly distinguish this from "
    "corpus-sourced material (e.g. 'The corpus discusses X in the context of Y')\n"
    "- Never fabricate corpus citations"
)

_QUERY_PLANNING_SYSTEM = (
    "You are a search query planner for a document knowledge base. "
    "Given a research question, generate diverse search queries that together "
    "cover all angles of the topic.\n\n"
    "Rules:\n"
    "- Generate 5-15 queries depending on question complexity\n"
    "- Vary phrasing: use synonyms, related terms, specific entities\n"
    "- Include both broad and narrow queries\n"
    "- For comparative questions, generate queries for each side\n"
    "- For questions about specific entities, include name variants\n"
    '- Return ONLY a JSON object: {"queries": ["query1", "query2", ...]}\n'
    "- No explanation, no markdown fences — just the JSON object"
)

_NOTE_EXTRACTION_SYSTEM = (
    "You are extracting research notes from search results. For each relevant "
    "finding, write a concise note with an exact citation.\n\n"
    "Rules:\n"
    "- Cite every finding as [Document Title, page X] — exactly as shown in the passages\n"
    "- Write one note per distinct finding\n"
    "- Skip irrelevant or redundant passages\n"
    "- Preserve factual details — names, numbers, dates\n"
    "- If a passage contradicts another, note both with their citations\n"
    "- Write in plain text with citations, not JSON"
)

_GAP_ANALYSIS_SYSTEM = (
    "You are checking research coverage. Given the original question and notes "
    "gathered so far, identify any obvious gaps.\n\n"
    "Rules:\n"
    '- If the notes adequately cover the question, return: {"gaps": []}\n'
    "- If there are clear gaps, return up to 5 additional queries: "
    '{"gaps": ["query1", "query2"]}\n'
    "- Only suggest queries for genuinely missing angles, not minor refinements\n"
    "- Return ONLY the JSON object, no explanation"
)

# Depth → query count and search parameters
_DEPTH_CONFIG = {
    "light": {"max_queries": 8, "k_per_query": 15, "max_passages": 40, "gap_round": False, "paginate": False},
    "standard": {"max_queries": 15, "k_per_query": 20, "max_passages": 60, "gap_round": True, "paginate": True},
    "thorough": {"max_queries": 25, "k_per_query": 30, "max_passages": 100, "gap_round": True, "paginate": True},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN)


def _get_context_budget() -> int:
    """Get the model's context window in tokens."""
    settings = get_settings()
    model = get_model(settings.llm_model_id) if settings.llm_model_id else None
    if model and settings.llm_yarn_enabled and model.yarn:
        return model.yarn.extended_context
    return model.context_window if model else 32768


async def _llm_complete(
    client: httpx.AsyncClient,
    url: str,
    messages: list[dict],
    *,
    timeout: float = _LLM_TIMEOUT,
) -> str:
    """Non-streaming LLM call with retry. Returns content string."""
    payload = {"messages": messages, "temperature": 0.3}

    for attempt in range(2):
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            if resp.status_code >= 500 and attempt == 0:
                report_llm_error(resp.status_code)
                logger.warning("LLM returned %d, retrying in 2s", resp.status_code)
                await asyncio.sleep(2)
                continue
            resp.raise_for_status()
            report_llm_success()
            data = resp.json()
            content = data["choices"][0]["message"].get("content", "")
            # Strip thinking tags from reasoning models
            if content.startswith("<think>") and "</think>" in content:
                content = content[content.index("</think>") + len("</think>") :].strip()
            return content
        except (httpx.ConnectError, httpx.TimeoutException):
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            raise

    return ""


async def _stream_llm_tokens(
    client: httpx.AsyncClient,
    url: str,
    messages: list[dict],
    *,
    timeout: float = _SYNTHESIS_TIMEOUT,
) -> AsyncGenerator[str, None]:
    """Stream LLM response tokens. Retries once on 5xx."""
    payload = {"messages": messages, "stream": True, "temperature": 0.3}

    response_obj = None
    for _attempt in range(2):
        response_obj = await client.send(
            client.build_request(
                "POST",
                url,
                json=payload,
                extensions={"timeout": {"connect": 10.0, "read": timeout, "write": 10.0, "pool": 10.0}},
            ),
            stream=True,
        )
        if response_obj.status_code < 500 or _attempt == 1:
            break
        await response_obj.aclose()
        report_llm_error(response_obj.status_code)
        logger.warning("LLM returned %d in synthesis, retrying in 2s", response_obj.status_code)
        await asyncio.sleep(2)

    try:
        response_obj.raise_for_status()
        report_llm_success()
        async for line in response_obj.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                yield delta["content"]
    finally:
        await response_obj.aclose()


def _parse_json_from_llm(text: str) -> dict:
    """Extract JSON from LLM response, tolerating markdown fences and preamble."""
    # Strip markdown code fences
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Find first { ... } block
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass
    return {}


async def _fetch_document_list(user_id: uuid.UUID | None) -> list[dict]:
    """Get corpus document list for sweep strategy."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Document.doc_id, Document.title).where(Document.status == "ready").order_by(Document.title)
        )
        return [{"doc_id": str(row.doc_id), "title": row.title} for row in result.all()]


def _build_synthesis_messages(user_question: str, notes: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYNTHESIS_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Original question\n{user_question}\n\n"
                f"## Research notes\n<notes>\n{notes}\n</notes>\n\n"
                "Write your final report with citations."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------


async def _seed_queries_from_corpus(
    user_question: str,
    user_id: uuid.UUID | None,
    topic_hint: str | None,
) -> list[str]:
    """Generate model-independent queries from corpus metadata.

    Pulls top entities and topic keywords, combines them with question
    keywords to produce queries that don't depend on LLM quality.
    """
    seeded: list[str] = []

    # Extract a few keywords from the question for cross-referencing
    q_words = [w for w in user_question.split() if len(w) > 3]
    q_short = " ".join(q_words[:4]) if q_words else user_question

    # Seed from entities
    try:
        entity_json = await execute_tool("entity_overview", {}, user_id, mode="research")
        entity_data = json.loads(entity_json)
        for ent in entity_data.get("top_entities", [])[:10]:
            name = ent.get("entity_text", "")
            if name and name.lower() not in user_question.lower():
                seeded.append(f"{name} {q_short}")
    except Exception:
        logger.debug("entity_overview failed for seed queries", exc_info=True)

    # Seed from topic keywords
    if topic_hint:
        for line in topic_hint.split("\n"):
            # Topic lines look like "- Topic Name: keyword1, keyword2, ..."
            if ":" in line:
                keywords_part = line.split(":", 1)[1].strip()
                keywords = [kw.strip() for kw in keywords_part.split(",") if kw.strip()]
                for kw in keywords[:2]:
                    if kw.lower() not in user_question.lower():
                        seeded.append(f"{kw} {q_short}")

    return seeded


async def _plan_queries(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    topic_hint: str | None,
    depth_config: dict,
    doc_list: list[dict] | None,
    user_id: uuid.UUID | None,
) -> list[str]:
    """Phase 1: LLM generates diverse search queries, supplemented by corpus-seeded queries."""
    # LLM-planned queries
    user_content = f"Research question: {user_question}"
    if topic_hint:
        user_content += f"\n\nCorpus topics:\n{topic_hint}"
    if doc_list:
        doc_text = "\n".join(f"- {d['title']}" for d in doc_list[:50])
        user_content += f"\n\nDocuments in corpus:\n{doc_text}"
    llm_target = max(5, depth_config["max_queries"] // 2)
    user_content += f"\n\nGenerate {llm_target} search queries."

    messages = [
        {"role": "system", "content": _QUERY_PLANNING_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    content = await _llm_complete(client, url, messages)
    parsed = _parse_json_from_llm(content)
    llm_queries = parsed.get("queries", [])

    if not llm_queries:
        llm_queries = [user_question]
        words = user_question.split()
        if len(words) > 3:
            llm_queries.append(" ".join(words[: len(words) // 2]))
            llm_queries.append(" ".join(words[len(words) // 2 :]))

    # Corpus-seeded queries (model-independent)
    seeded = await _seed_queries_from_corpus(user_question, user_id, topic_hint)

    # Merge: LLM queries first (higher intent), then seeded (breadth), dedupe
    seen_lower: set[str] = set()
    merged: list[str] = []
    for q in llm_queries + seeded:
        q = q.strip()
        if q and q.lower() not in seen_lower:
            seen_lower.add(q.lower())
            merged.append(q)

    return merged[: depth_config["max_queries"]]


def _ingest_hits(coverage: dict[str, dict], hits: list[dict], query_text: str) -> None:
    """Merge search hits into the coverage dict, deduping by chunk_id."""
    for hit in hits:
        cid = hit.get("chunk_id", "")
        if not cid:
            continue
        if cid in coverage:
            coverage[cid]["score"] = max(coverage[cid]["score"], hit.get("score", 0))
            coverage[cid]["queries"].add(query_text)
        else:
            coverage[cid] = {
                "doc_id": hit.get("doc_id", ""),
                "doc_title": hit.get("doc_title", ""),
                "page": hit.get("page"),
                "score": hit.get("score", 0),
                "snippet": hit.get("text", hit.get("snippet", ""))[:500],
                "section": hit.get("section", ""),
                "queries": {query_text},
            }


async def _search_fan_out(
    queries: list[str],
    user_id: uuid.UUID | None,
    k_per_query: int,
    *,
    paginate: bool = False,
) -> dict[str, dict]:
    """Phase 2: Run all queries, dedupe by chunk_id.

    Returns {chunk_id: {doc_id, doc_title, page, score, snippet, section, queries}}.
    When paginate=True, follows has_more on high-scoring queries for up to 2 extra pages.
    """
    coverage: dict[str, dict] = {}
    queries_with_more: list[tuple[str, int]] = []  # (query, next_offset)

    # Batch queries 5 at a time via kb_batch_search
    for i in range(0, len(queries), 5):
        batch = queries[i : i + 5]
        try:
            result_json = await execute_tool(
                "batch_search",
                {"queries": batch, "k": k_per_query},
                user_id,
                mode="research",
            )
            result = json.loads(result_json)

            for query_result in result.get("results", []):
                query_text = query_result.get("query", "")
                _ingest_hits(coverage, query_result.get("hits", []), query_text)
                if paginate and query_result.get("has_more") and query_result.get("total_candidates", 0) > k_per_query:
                    queries_with_more.append((query_text, k_per_query))
        except Exception:
            logger.exception("batch_search failed for queries: %s", batch)

    # Pagination pass: follow has_more for queries that had many candidates
    if paginate and queries_with_more:
        for query_text, offset in queries_with_more[:10]:  # cap pagination effort
            for _page in range(2):  # up to 2 extra pages
                try:
                    page_json = await execute_tool(
                        "search_documents",
                        {"query": query_text, "k": k_per_query, "offset": offset},
                        user_id,
                        mode="research",
                    )
                    page_result = json.loads(page_json)
                    hits = page_result.get("hits", [])
                    _ingest_hits(coverage, hits, query_text)
                    offset += len(hits)
                    if not page_result.get("has_more"):
                        break
                except Exception:
                    logger.debug("Pagination failed for query: %s offset=%d", query_text, offset)
                    break

    # Convert sets to lists for JSON serialization
    for entry in coverage.values():
        entry["queries"] = list(entry["queries"])

    return coverage


async def _read_evidence(
    coverage: dict[str, dict],
    user_id: uuid.UUID | None,
    max_passages: int,
    context_budget_chars: int,
) -> str:
    """Phase 3: Read full passages for top-scoring chunks.

    Returns formatted passage text for note extraction, bounded by context budget.
    """
    # Sort by score descending, pick diverse docs
    sorted_chunks = sorted(coverage.items(), key=lambda x: x[1]["score"], reverse=True)

    # Prioritize: take top chunk per doc first, then fill
    seen_docs: set[str] = set()
    selected: list[str] = []
    remaining: list[str] = []

    for cid, info in sorted_chunks:
        if info["doc_id"] not in seen_docs:
            selected.append(cid)
            seen_docs.add(info["doc_id"])
        else:
            remaining.append(cid)

    selected.extend(remaining)
    selected = selected[:max_passages]

    if not selected:
        return ""

    # Read in batches of 20
    passages_text = ""
    total_chars = 0

    for i in range(0, len(selected), 20):
        batch_ids = selected[i : i + 20]
        try:
            result_json = await execute_tool(
                "read_passages",
                {"chunk_ids": batch_ids, "include_context": False},
                user_id,
                mode="research",
            )
            result = json.loads(result_json)

            for passage in result.get("passages", []):
                text = passage.get("text", "")
                doc_title = passage.get("doc_title", "Unknown")
                page = passage.get("page", "?")
                section = passage.get("section", "")

                entry = f"\n---\n**[{doc_title}, page {page}]**"
                if section:
                    entry += f" — {section}"
                entry += f"\n{text}\n"

                if total_chars + len(entry) > context_budget_chars:
                    break
                passages_text += entry
                total_chars += len(entry)

        except Exception:
            logger.exception("read_passages failed for batch starting at %d", i)

        if total_chars >= context_budget_chars:
            break

    return passages_text


async def _extract_notes(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    passages_text: str,
) -> str:
    """Phase 4: LLM extracts cited notes from retrieved passages."""
    if not passages_text.strip():
        return "No relevant passages were found in the corpus."

    messages = [
        {"role": "system", "content": _NOTE_EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Research question\n{user_question}\n\n"
                f"## Retrieved passages\n{passages_text}\n\n"
                "Extract research notes with citations for each relevant finding."
            ),
        },
    ]

    return await _llm_complete(client, url, messages, timeout=180.0)


async def _check_gaps(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    notes: str,
    coverage_summary: str,
) -> list[str]:
    """Phase 5: LLM checks for gaps and suggests additional queries."""
    messages = [
        {"role": "system", "content": _GAP_ANALYSIS_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Research question\n{user_question}\n\n"
                f"## Coverage\n{coverage_summary}\n\n"
                f"## Notes so far\n{notes[:5000]}\n\n"
                "Are there obvious gaps? Return JSON."
            ),
        },
    ]

    content = await _llm_complete(client, url, messages)
    parsed = _parse_json_from_llm(content)
    return parsed.get("gaps", [])[:5]


# ---------------------------------------------------------------------------
# Main research engine
# ---------------------------------------------------------------------------


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def research_stream(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    resume: bool = False,
    depth: str = "standard",
) -> AsyncGenerator[str, None]:
    """Stream research progress as SSE events.

    Pipeline: plan queries → fan-out search → read passages → extract notes
    → (optional gap round) → synthesis.

    Each LLM call is bounded and context-managed. Search/read steps are
    pure Python with no LLM involvement.
    """
    settings = get_settings()
    active_model_id = settings.llm_model_id or None
    llm_url = f"{settings.llama_server_url}/v1/chat/completions"
    depth_config = _DEPTH_CONFIG.get(depth, _DEPTH_CONFIG["standard"])

    async with async_session_factory() as session:
        # Load research state
        state = await session.get(ResearchState, conversation_id)
        if state is None:
            yield _sse({"type": "error", "message": "Research state not found"})
            return

        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            yield _sse({"type": "error", "message": "Conversation not found"})
            return

        q_result = await session.execute(
            select(ChatMessage.content)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.role == "user")
            .order_by(ChatMessage.created_at)
            .limit(1)
        )
        user_question = q_result.scalar_one_or_none()
        if not user_question:
            yield _sse({"type": "error", "message": "No user question found"})
            return

        strategy = state.strategy
        resume_from_synthesis = resume and state.notes and len(state.notes) > 200

        state.status = "running"
        state.heartbeat_at = datetime.now(UTC)
        await session.commit()

        start_time = datetime.now(UTC)
        step_count = 0
        collected_notes: list[str] = []

        try:
            if resume_from_synthesis:
                logger.info(
                    "Resuming research %s from synthesis (notes len=%d)",
                    conversation_id,
                    len(state.notes or ""),
                )
            else:
                # Compute context budget for passage reading
                context_tokens = _get_context_budget()
                # Reserve 40% of context for passages in note extraction
                # (rest: system prompt, question, response)
                passage_budget_chars = int(context_tokens * 0.4 * _CHARS_PER_TOKEN)
                passage_budget_chars = min(passage_budget_chars, 80_000)

                async with httpx.AsyncClient(timeout=httpx.Timeout(_LLM_TIMEOUT)) as client:
                    # -------------------------------------------------------
                    # Phase 1: Query planning
                    # -------------------------------------------------------
                    yield _sse(
                        {"type": "progress", "step": 1, "phase": "planning", "elapsed_seconds": 0, "strategy": strategy}
                    )
                    yield _sse({"type": "notes", "content": "Planning search queries..."})

                    from harbor_clerk.topics import get_topic_summary

                    topic_hint = await get_topic_summary()
                    doc_list = await _fetch_document_list(user_id) if strategy == "sweep" else None

                    try:
                        queries = await _plan_queries(
                            client,
                            llm_url,
                            user_question,
                            topic_hint,
                            depth_config,
                            doc_list,
                            user_id,
                        )
                    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                        logger.error("LLM error during query planning: %s", exc)
                        # Fallback: seeded queries only (no LLM)
                        queries = [user_question]
                        try:
                            seeded = await _seed_queries_from_corpus(user_question, user_id, topic_hint)
                            queries.extend(seeded)
                        except Exception:
                            pass

                    step_count = 1
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                    query_list_text = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(queries))
                    collected_notes.append(f"## Planned queries\n{query_list_text}")
                    yield _sse({"type": "notes", "content": f"Planned {len(queries)} queries:\n{query_list_text}"})

                    # -------------------------------------------------------
                    # Phase 2: Deterministic search fan-out
                    # -------------------------------------------------------
                    elapsed = int((datetime.now(UTC) - start_time).total_seconds())
                    yield _sse(
                        {
                            "type": "progress",
                            "step": 2,
                            "phase": "searching",
                            "elapsed_seconds": elapsed,
                            "strategy": strategy,
                        }
                    )
                    yield _sse({"type": "notes", "content": "Searching corpus..."})

                    # Emit tool_call events for frontend display
                    for i in range(0, len(queries), 5):
                        batch = queries[i : i + 5]
                        yield _sse(
                            {
                                "type": "tool_call",
                                "name": "batch_search",
                                "arguments": {"queries": batch, "k": depth_config["k_per_query"]},
                            }
                        )

                    coverage = await _search_fan_out(
                        queries,
                        user_id,
                        depth_config["k_per_query"],
                        paginate=depth_config.get("paginate", False),
                    )

                    # Build coverage summary
                    unique_docs = {}
                    for info in coverage.values():
                        did = info["doc_id"]
                        if did not in unique_docs:
                            unique_docs[did] = {"title": info["doc_title"], "chunks": 0, "best_score": 0}
                        unique_docs[did]["chunks"] += 1
                        unique_docs[did]["best_score"] = max(unique_docs[did]["best_score"], info["score"])

                    coverage_summary = f"Found {len(coverage)} unique passages across {len(unique_docs)} documents.\n"
                    for _did, dinfo in sorted(unique_docs.items(), key=lambda x: x[1]["best_score"], reverse=True):
                        coverage_summary += (
                            f"- {dinfo['title']}: {dinfo['chunks']} passages (best score: {dinfo['best_score']:.2f})\n"
                        )

                    collected_notes.append(f"## Search coverage\n{coverage_summary}")
                    yield _sse(
                        {
                            "type": "tool_result",
                            "name": "batch_search",
                            "summary": f"Found {len(coverage)} passages in {len(unique_docs)} documents",
                            "raw_result": coverage_summary,
                        }
                    )
                    yield _sse({"type": "notes", "content": coverage_summary})

                    step_count = 2
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                    # -------------------------------------------------------
                    # Phase 3: Read evidence
                    # -------------------------------------------------------
                    elapsed = int((datetime.now(UTC) - start_time).total_seconds())
                    yield _sse(
                        {
                            "type": "progress",
                            "step": 3,
                            "phase": "reading",
                            "elapsed_seconds": elapsed,
                            "strategy": strategy,
                        }
                    )
                    yield _sse({"type": "notes", "content": "Reading top passages..."})

                    chunk_ids_to_read = sorted(
                        coverage.keys(),
                        key=lambda cid: coverage[cid]["score"],
                        reverse=True,
                    )[: depth_config["max_passages"]]

                    yield _sse(
                        {
                            "type": "tool_call",
                            "name": "read_passages",
                            "arguments": {"chunk_ids": chunk_ids_to_read[:10], "count": len(chunk_ids_to_read)},
                        }
                    )

                    passages_text = await _read_evidence(
                        coverage,
                        user_id,
                        depth_config["max_passages"],
                        passage_budget_chars,
                    )

                    yield _sse(
                        {
                            "type": "tool_result",
                            "name": "read_passages",
                            "summary": f"Read {len(passages_text)} chars of evidence",
                            "raw_result": passages_text[:2000],
                        }
                    )

                    step_count = 3
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                    # -------------------------------------------------------
                    # Phase 4: Extract notes
                    # -------------------------------------------------------
                    elapsed = int((datetime.now(UTC) - start_time).total_seconds())
                    yield _sse(
                        {
                            "type": "progress",
                            "step": 4,
                            "phase": "analyzing",
                            "elapsed_seconds": elapsed,
                            "strategy": strategy,
                        }
                    )
                    yield _sse({"type": "notes", "content": "Extracting findings from passages..."})

                    try:
                        notes_text = await _extract_notes(client, llm_url, user_question, passages_text)
                    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                        logger.error("LLM error during note extraction: %s", exc)
                        # Fallback: use raw passages as notes
                        notes_text = f"## Raw passages\n{passages_text[:30000]}"

                    collected_notes.append(f"## Research notes (round 1)\n{notes_text}")
                    yield _sse({"type": "notes", "content": notes_text[:3000]})

                    step_count = 4
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    state.notes = "\n\n".join(collected_notes)
                    await session.commit()

                    # -------------------------------------------------------
                    # Phase 5: Gap analysis (optional)
                    # -------------------------------------------------------
                    if depth_config["gap_round"] and len(coverage) > 0:
                        elapsed = int((datetime.now(UTC) - start_time).total_seconds())
                        time_limit_s = (state.time_limit_minutes or 30) * 60
                        if elapsed < time_limit_s * 0.7:  # only if we have time
                            yield _sse(
                                {
                                    "type": "progress",
                                    "step": 5,
                                    "phase": "gap_analysis",
                                    "elapsed_seconds": elapsed,
                                    "strategy": strategy,
                                }
                            )
                            yield _sse({"type": "notes", "content": "Checking for gaps in coverage..."})

                            try:
                                gap_queries = await _check_gaps(
                                    client,
                                    llm_url,
                                    user_question,
                                    notes_text,
                                    coverage_summary,
                                )
                            except Exception:
                                logger.exception("Gap analysis failed")
                                gap_queries = []

                            if gap_queries:
                                gap_list = ", ".join(gap_queries)
                                collected_notes.append(f"## Gap queries\n{gap_list}")
                                yield _sse({"type": "notes", "content": f"Found gaps — searching: {gap_list}"})

                                # Run gap queries
                                gap_coverage = await _search_fan_out(
                                    gap_queries,
                                    user_id,
                                    depth_config["k_per_query"],
                                )
                                # Filter out already-seen chunks
                                new_chunks = {k: v for k, v in gap_coverage.items() if k not in coverage}

                                if new_chunks:
                                    yield _sse(
                                        {"type": "notes", "content": f"Gap search found {len(new_chunks)} new passages"}
                                    )

                                    gap_passages = await _read_evidence(
                                        new_chunks,
                                        user_id,
                                        20,
                                        passage_budget_chars // 3,
                                    )
                                    if gap_passages.strip():
                                        try:
                                            gap_notes = await _extract_notes(
                                                client,
                                                llm_url,
                                                user_question,
                                                gap_passages,
                                            )
                                            collected_notes.append(f"## Research notes (gap round)\n{gap_notes}")
                                            yield _sse({"type": "notes", "content": gap_notes[:2000]})
                                        except Exception:
                                            logger.exception("Gap note extraction failed")
                                else:
                                    yield _sse({"type": "notes", "content": "No new material found in gap search"})

                            step_count = 5
                            state.current_round = step_count
                            state.heartbeat_at = datetime.now(UTC)
                            state.notes = "\n\n".join(collected_notes)
                            await session.commit()

            # ---------------------------------------------------------------
            # Synthesis pass
            # ---------------------------------------------------------------
            if resume_from_synthesis:
                notes = state.notes or "No relevant findings were discovered during the research."
            else:
                notes = "\n\n".join(collected_notes)
                if not notes.strip():
                    notes = "No relevant findings were discovered during the research."

            # Cap notes to fit the model's context, reserving space for
            # system prompt (~300 tokens), question, and output (~35% of context).
            ctx_tokens = _get_context_budget()
            output_reserve = int(ctx_tokens * 0.35)
            overhead_tokens = 400 + _estimate_tokens(user_question)
            max_notes_tokens = ctx_tokens - output_reserve - overhead_tokens
            max_notes_chars = int(max(4000, max_notes_tokens * _CHARS_PER_TOKEN))
            if len(notes) > max_notes_chars:
                logger.info(
                    "Truncating synthesis notes from %d to %d chars (model context %d tokens)",
                    len(notes),
                    max_notes_chars,
                    ctx_tokens,
                )
                notes = notes[:max_notes_chars] + f"\n... [truncated — {len(notes)} chars total]"

            yield _sse({"type": "synthesis", "status": "started"})

            synthesis_messages = _build_synthesis_messages(user_question, notes)
            report_content = ""

            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(_SYNTHESIS_TIMEOUT)) as client:
                    async for token in _stream_llm_tokens(
                        client,
                        llm_url,
                        synthesis_messages,
                        timeout=_SYNTHESIS_TIMEOUT,
                    ):
                        report_content += token
                        yield _sse({"type": "token", "content": token})
            except httpx.HTTPStatusError as exc:
                logger.error("LLM HTTP error during synthesis: %s", exc)
                if exc.response.status_code >= 500:
                    report_llm_error(exc.response.status_code)
                state.status = "interrupted"
                state.error = f"Synthesis failed: LLM error ({exc.response.status_code})"
                state.notes = notes
                await session.commit()
                yield _sse({"type": "error", "message": f"Synthesis failed: LLM error ({exc.response.status_code})"})
                return
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                logger.error("LLM connection/timeout error during synthesis: %s: %r", type(exc).__name__, exc)
                state.status = "interrupted"
                state.error = "Synthesis failed: LLM server not reachable"
                state.notes = notes
                await session.commit()
                yield _sse(
                    {"type": "error", "message": "LLM server is not running. Select and activate a model in Settings."}
                )
                return

            # Save report
            session.add(
                ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=report_content,
                    model_id=active_model_id,
                )
            )

            state.status = "completed"
            state.notes = notes
            state.current_round = step_count
            state.completed_at = datetime.now(UTC)
            state.progress = {"step": step_count}
            await session.commit()

            done_payload: dict = {"type": "done", "conversation_id": str(conversation_id)}
            if active_model_id:
                done_payload["model_id"] = active_model_id
            yield _sse(done_payload)

        except Exception:
            logger.exception("Unexpected error in research_stream (conversation=%s)", conversation_id)
            state.status = "failed"
            state.error = "Unexpected internal error"
            state.current_round = step_count
            try:
                await session.commit()
            except Exception:
                logger.exception("Failed to save error state")
            yield _sse({"type": "error", "message": "An unexpected error occurred during research."})

        finally:
            try:
                await session.refresh(state)
                if state.status == "running":
                    logger.info("Research stream disconnected, marking interrupted (conversation=%s)", conversation_id)
                    state.status = "interrupted"
                    state.current_round = step_count
                    await session.commit()
            except Exception:
                logger.exception("Failed to mark research as interrupted on disconnect")
