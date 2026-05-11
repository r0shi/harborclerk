"""Research engine — deterministic plan→search→read→extract→synthesize pipeline.

Replaces the previous smolagents-based agent loop with a structured workflow
where the LLM is used only for bounded tasks (query planning, note extraction,
synthesis) and all retrieval is driven by Python code with measurable coverage.
"""

import asyncio
import hashlib
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

# Per-phase output caps. Without these, models like Gemma 26B routinely
# produce 5,000+ tokens for what should be a ~200-token JSON object, which
# wastes the entire research budget on a single call. See research-debugging/
# findings.md for the runaway-generation diagnosis.
#
# Sized for big models that "think" before answering. llama-server's default
# `reasoning_format: deepseek` puts thinking in `reasoning_content` (separate
# from `content`), so the cap is consumed by thinking before the actual
# answer is emitted. Diagnosed by direct probe: Gemma 26B used ~1500–2000
# tokens of thinking just for planning, and similar for note extraction. Caps
# below the thinking budget reliably starve out the answer (content==""). All
# caps are sized to comfortably accommodate thinking + the actual answer.
# Cost: more tokens generated per call. Mitigated by tighter prompt caps and
# the deadline guard around note extraction. See research-debugging/findings.md.
_MAX_TOKENS_PLANNING = 5000
_MAX_TOKENS_NOTES = 8000
_MAX_TOKENS_GAP = 5000
_MAX_TOKENS_SYNTHESIS = 10000

# spaCy entity types that are useless as search seeds. CARDINAL/ORDINAL
# dominate the corpus's top-entity list ("one", "first", "two", "1", "2") and
# polluted seeded queries with garbage like "1 Please compare different wine".
# DATE/TIME/MONEY/PERCENT/QUANTITY similarly don't represent topical content.
_SEED_QUERY_BLOCKED_ENTITY_TYPES = frozenset({"CARDINAL", "ORDINAL", "DATE", "TIME", "MONEY", "PERCENT", "QUANTITY"})

# Cap the prefill-heavy note-extraction prompt. 30K chars ≈ 8.5K tokens — keeps
# Gemma-26B-class prefill under ~45 s. Was 80K (≈22K tokens, ~110 s prefill).
_NOTE_PROMPT_CHAR_CAP = 30_000

_SYNTHESIS_SYSTEM = (
    "You are writing a research report for Harbor Clerk. Base your report ONLY "
    "on the research notes between <notes>...</notes> markers in the user "
    "message. Do not introduce facts that are not in those notes.\n\n"
    "## Citation rule\n"
    "- Every substantive claim must end with a citation in the form "
    "[Document Title, page X], copied exactly as the citation appears in the "
    "notes.\n"
    "- Never invent a citation. If a fact does not appear in the notes with a "
    "citation, do not include the fact.\n\n"
    "## Coverage rule (especially for comparative questions)\n"
    "- For comparative questions, write a dedicated section per region, "
    "tradition, producer, or case that the notes discuss. Do not collapse "
    "multiple distinct subjects into a single sentence.\n"
    "- If the original question or planned queries mentioned a topic that the "
    "notes do not cover, briefly state that the corpus did not yield evidence "
    "for it (one short sentence). Do NOT substitute general knowledge for the "
    "missing material.\n\n"
    "## Style\n"
    "- Group findings by theme, not by document.\n"
    "- Be thorough but concise — include all relevant findings, skip filler.\n"
    "- If the evidence is contradictory or incomplete, say so.\n"
    "- You may include one short paragraph of general-knowledge framing at the "
    "very start, only if it helps the reader interpret the findings — and mark "
    "it explicitly as background, not as a finding."
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
    "- No explanation, no preamble, no thinking aloud, no markdown fences — "
    "your response must START with `{` and END with `}` and contain nothing else.\n"
    '- Begin your response with `{"queries": [` immediately.'
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
    "- Write in plain text with citations, not JSON\n"
    "- If the passages do NOT contain information relevant to the research "
    "question, return ONLY the line: `No relevant findings in this passage "
    "set.` Do not invent on-topic content from the passage titles or from "
    "general knowledge."
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
    max_tokens: int | None = None,
    phase: str = "unknown",
    json_mode: bool = False,
) -> str:
    """Non-streaming LLM call with retry. Returns content string.

    json_mode=True asks llama-server to constrain output to valid JSON via
    grammar-guided sampling. This is essential for verbose models (Gemma,
    DeepSeek-R1) that otherwise emit a long preamble before the JSON and
    blow the max_tokens budget without ever closing the object.
    """
    payload: dict = {
        "messages": messages,
        "temperature": 0.3,
        # Disable per-call "thinking" on chat templates that support it
        # (Gemma 4, Qwen3, etc.). Without this, big models burn most of
        # max_tokens on a `<|channel>thought` block before producing the
        # actual answer; with it, Gemma 26B produces clean direct output
        # in ~6× fewer tokens. Templates that don't recognise this kwarg
        # ignore it harmlessly.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    started = datetime.now(UTC)
    for attempt in range(2):
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
            if resp.status_code >= 500 and attempt == 0:
                report_llm_error(resp.status_code)
                logger.warning("LLM returned %d in phase=%s, retrying in 2s", resp.status_code, phase)
                await asyncio.sleep(2)
                continue
            resp.raise_for_status()
            report_llm_success()
            data = resp.json()
            message = data["choices"][0]["message"]
            content = message.get("content", "") or ""
            # Fallback: if `content` is empty but `reasoning_content` has text,
            # the model exhausted max_tokens during its thinking channel before
            # producing the actual answer. Use the reasoning content rather
            # than returning "" — partial structured thought is more useful
            # than nothing for downstream synthesis or parsing.
            reasoning_content = message.get("reasoning_content") or ""
            if not content.strip() and reasoning_content.strip():
                # Confirmed via the cross-topic sweep that this is a
                # normal path on reasoning-tuned models (GPT-OSS 20B in
                # particular routes the entire response through the
                # reasoning channel even with enable_thinking=False).
                # Logged at INFO rather than WARNING so it doesn't show
                # up as an alert-worthy event in normal operation.
                logger.info(
                    "LLM phase=%s: content empty, falling back to reasoning_content (%d chars).",
                    phase,
                    len(reasoning_content),
                )
                content = reasoning_content
            usage = data.get("usage") or {}
            elapsed = (datetime.now(UTC) - started).total_seconds()
            logger.info(
                "LLM call phase=%s elapsed=%.1fs prompt_tokens=%s completion_tokens=%s max_tokens=%s",
                phase,
                elapsed,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                max_tokens,
            )
            if (
                max_tokens is not None
                and isinstance(usage.get("completion_tokens"), int)
                and usage["completion_tokens"] >= max_tokens
            ):
                logger.warning(
                    "LLM phase=%s hit max_tokens cap (%d). Output may be truncated.",
                    phase,
                    max_tokens,
                )
            # Strip thinking tags from reasoning models
            if content.startswith("<think>") and "</think>" in content:
                content = content[content.index("</think>") + len("</think>") :].strip()
            return content
        except (httpx.ConnectError, httpx.TimeoutException):
            if attempt == 0:
                logger.warning("LLM timeout in phase=%s, retrying", phase)
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
    max_tokens: int | None = None,
) -> AsyncGenerator[str, None]:
    """Stream LLM response tokens. Retries once on 5xx."""
    payload: dict = {
        "messages": messages,
        "stream": True,
        "temperature": 0.3,
        # Same rationale as in `_llm_complete`: suppress thinking so the
        # streamed `delta.content` carries the actual answer rather than
        # going silent while the model fills `delta.reasoning_content`.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    response_obj = None
    for _attempt in range(2):
        try:
            response_obj = await client.send(
                client.build_request(
                    "POST",
                    url,
                    json=payload,
                    extensions={"timeout": {"connect": 10.0, "read": timeout, "write": 10.0, "pool": 10.0}},
                ),
                stream=True,
            )
        except httpx.ConnectError as exc:
            # Transient: llama-server briefly unreachable between requests
            # (observed once on cheese × Qwen3.6 35B-A3B during the
            # cross-topic sweep — succeeded on retry). Mirror the 5xx
            # branch's semantics: one extra attempt with 2s backoff. Read
            # and write timeouts are intentionally NOT retried — those
            # mean the model itself stalled mid-stream and starting over
            # would discard partial output for no gain.
            if _attempt == 1:
                raise
            logger.warning("LLM connect error in synthesis: %r, retrying in 2s", exc)
            await asyncio.sleep(2)
            continue
        if response_obj.status_code < 500 or _attempt == 1:
            break
        await response_obj.aclose()
        report_llm_error(response_obj.status_code)
        logger.warning("LLM returned %d in synthesis, retrying in 2s", response_obj.status_code)
        await asyncio.sleep(2)

    content_emitted_chars = 0
    reasoning_buffer: list[str] = []
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
                content_emitted_chars += len(delta["content"])
                yield delta["content"]
            elif delta.get("reasoning_content"):
                # Some models (GPT-OSS, DeepSeek-R1) ignore the
                # enable_thinking template kwarg and route the entire
                # response through the reasoning channel. Buffer those
                # tokens so we can fall back to them if the stream ends
                # without ever producing content. Streaming them live
                # would mix raw thinking into the user-visible report.
                reasoning_buffer.append(delta["reasoning_content"])
        # Fallback: if no content was streamed but we collected reasoning,
        # emit it as the report. Better partial-thinking-as-report than
        # empty report.
        if content_emitted_chars == 0 and reasoning_buffer:
            joined = "".join(reasoning_buffer)
            # Same rationale as the fallback in `_llm_complete`: confirmed
            # to be a normal path on GPT-OSS / DeepSeek-R1 streaming;
            # logged at INFO so a normal run doesn't look alarming.
            logger.info(
                "Synthesis stream emitted no content tokens; falling back to %d chars of reasoning_content.",
                len(joined),
            )
            yield joined
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


# Reject "queries" that are clearly tokens spilled out of a runaway generation.
# Models like Gemma 26B without max_tokens routinely produce 5000+ tokens of
# rambling continuation; the JSON extractor then pulls fragments like
# "one Please compare different wine" or "1 Please compare different wine"
# back into the queries list. These match the leading "Please compare" of the
# user question because the model is essentially echoing it with token-prefix
# variants. We filter on minimum length and on requiring at least one
# alphabetic word ≥ 4 chars that isn't part of the user-question prefix.
_RUNAWAY_PREFIX_RE = re.compile(
    r"^(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth)\s+",
    re.IGNORECASE,
)


def _is_plausible_query(q: object) -> bool:
    if not isinstance(q, str):
        return False
    q = q.strip()
    if len(q) < 8:
        return False
    # Drop "1 Please compare ...", "one Please compare ..." etc. that are
    # symptomatic of runaway generation parsed back into the JSON.
    if _RUNAWAY_PREFIX_RE.match(q):
        return False
    # Require at least one substantive word (≥4 alpha chars).
    words = re.findall(r"[A-Za-zÀ-ÿ]{4,}", q)
    return len(words) >= 1


async def _fetch_document_list(user_id: uuid.UUID | None) -> list[dict]:
    """Get corpus document list for sweep strategy."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Document.doc_id, Document.title).where(Document.status == "ready").order_by(Document.title)
        )
        return [{"doc_id": str(row.doc_id), "title": row.title} for row in result.all()]


_COMPARATIVE_QUESTION_RE = re.compile(
    r"\b(compare|comparison|contrast|differences?|vs\.?|versus|across\s+\w+|"
    r"different\s+\w+\s+and|how\s+do(?:es)?\s+\w+\s+differ)\b",
    re.IGNORECASE,
)


def _is_comparative_question(question: str) -> bool:
    return bool(_COMPARATIVE_QUESTION_RE.search(question))


def _build_synthesis_messages(user_question: str, notes: str) -> list[dict]:
    user_content = f"## Original question\n{user_question}\n\n"
    user_content += f"## Research notes\n<notes>\n{notes}\n</notes>\n\n"
    if _is_comparative_question(user_question):
        # Steer toward tabular structure for compare-style questions. GPT-OSS
        # uses tables natively and they read 2-3× better than paragraphs for
        # multi-region/producer comparisons; nudging the other models toward
        # them when the question shape calls for it makes the cross-model
        # output more uniformly useful. See research-debugging/cross-topic-
        # analysis.md ("GPT-OSS 20B's table format is the gold standard").
        user_content += (
            "Format guidance: this is a comparative question. Where it fits, "
            "use markdown tables with one row per region/tradition/producer/case "
            "and a Source column with the citation. Use prose for the "
            "introduction and synthesis sections, tables for the substantive "
            "comparisons.\n\n"
        )
    user_content += "Write your final report with citations."
    return [
        {"role": "system", "content": _SYNTHESIS_SYSTEM},
        {"role": "user", "content": user_content},
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

    # Seed from entities — skip useless types (CARDINAL/ORDINAL/DATE/etc.)
    # which dominate the top-entity list with values like "one", "first", "1"
    # that produce nonsense queries when concatenated with the question.
    try:
        entity_json = await execute_tool("entity_overview", {}, user_id, mode="research")
        entity_data = json.loads(entity_json)
        kept = 0
        for ent in entity_data.get("top_entities", []):
            if kept >= 10:
                break
            etype = (ent.get("entity_type") or "").upper()
            if etype in _SEED_QUERY_BLOCKED_ENTITY_TYPES:
                continue
            name = (ent.get("entity_text") or "").strip()
            # Defense-in-depth: also drop pure-numeric / single-letter / very short tokens
            if not name or len(name) < 3 or name.replace(".", "").replace(",", "").isdigit():
                continue
            if name.lower() in user_question.lower():
                continue
            seeded.append(f"{name} {q_short}")
            kept += 1
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

    content = await _llm_complete(
        client,
        url,
        messages,
        max_tokens=_MAX_TOKENS_PLANNING,
        phase="planning",
        json_mode=True,
    )
    parsed = _parse_json_from_llm(content)
    llm_queries = [q for q in parsed.get("queries", []) if _is_plausible_query(q)]

    if not llm_queries:
        logger.warning("Planning produced no plausible queries; falling back to question keywords")
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


def _doc_family_key(title: str) -> str:
    """Group near-duplicate document drafts by leading-letter prefix.

    Strips digits, whitespace, punctuation, and version suffixes, then takes
    the first 6 letters. Examples (all return the same key):
    - "painaulevain4", "painaulevain5WY", "painaulevainrecipeAoE1"  → "painau"
    - "Yarra Valley 8WY", "Yarra-4-30", "YarraValley2KKedits"        → "yarrav"
    - "Christoffel-1", "christoffel-6-11", "Christoffel 9"           → "christ"
    """
    letters_only = "".join(c.lower() for c in title if c.isalpha())
    return letters_only[:6]


def _content_hash(snippet: str) -> str:
    """Hash the normalized leading content of a snippet, for near-duplicate
    detection. Lowercases, drops all non-alphanumerics (so whitespace and
    punctuation differences don't matter), takes the first 300 normalized
    chars. Sufficiently fuzzy to catch drafts of the same article that have
    minor copy edits, sharp enough not to false-collide unrelated text."""
    norm = re.sub(r"[^a-z0-9]+", "", snippet.lower())[:300]
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _dedup_near_duplicate_chunks(
    sorted_chunks: list[tuple[str, dict]],
) -> tuple[list[tuple[str, dict]], dict[str, list[str]]]:
    """Drop chunks that are near-duplicates of an already-selected chunk.

    Two chunks are near-duplicates when they share BOTH a doc-family-key
    (drafts of the same article) AND a content hash (same opening text).
    Input must be score-descending so the highest-scored chunk per group is
    the one kept.

    Returns (deduped_list, dups_by_kept_cid) where dups_by_kept_cid maps the
    kept chunk_id to the list of dropped doc titles that were merged into it.
    """
    seen: dict[tuple[str, str], str] = {}  # (family, content_hash) → kept cid
    dups_by_kept: dict[str, list[str]] = {}
    deduped: list[tuple[str, dict]] = []

    for cid, info in sorted_chunks:
        family = _doc_family_key(info.get("doc_title", ""))
        snippet = info.get("snippet", "")
        # If snippet is empty (unusual, but possible), don't risk false-collisions.
        if not snippet or not family:
            deduped.append((cid, info))
            continue
        chash = _content_hash(snippet)
        key = (family, chash)
        if key in seen:
            kept_cid = seen[key]
            dup_title = info.get("doc_title", "?")
            dups_by_kept.setdefault(kept_cid, []).append(dup_title)
            continue
        seen[key] = cid
        deduped.append((cid, info))

    return deduped, dups_by_kept


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
    sorted_chunks_all = sorted(coverage.items(), key=lambda x: x[1]["score"], reverse=True)

    # Drop near-duplicate drafts (same doc-family + same opening content) so
    # we don't burn note-extraction budget summarizing the same fact from
    # `painaulevain4` and `painaulevain5WY` and `painaulevainrecipeAoE1`. The
    # kept representative carries an `also_in` list for citation transparency.
    sorted_chunks, dups_by_kept = _dedup_near_duplicate_chunks(sorted_chunks_all)
    if dups_by_kept:
        total_dropped = sum(len(v) for v in dups_by_kept.values())
        sample = next(iter(dups_by_kept.values()))[:3]
        logger.info(
            "Dropped %d near-duplicate draft passages (sample: %s)",
            total_dropped,
            sample,
        )

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
                cid = passage.get("chunk_id")

                entry = f"\n---\n**[{doc_title}, page {page}]**"
                if section:
                    entry += f" — {section}"
                # Note other drafts that were merged into this representative
                # so the model knows the finding is corroborated across drafts
                # (and so a fact-checker can trace the dropped duplicates).
                also_in = dups_by_kept.get(cid, []) if cid else []
                if also_in:
                    truncated = also_in[:3]
                    extra = f"; also in: {', '.join(truncated)}" + (
                        f" (+{len(also_in) - 3} more)" if len(also_in) > 3 else ""
                    )
                    entry += extra
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

    # Hard-cap the prompt to keep prefill bounded. The caller may have already
    # respected `passage_budget_chars`, but on small-context models that budget
    # can still be larger than what fits within a reasonable prefill time.
    if len(passages_text) > _NOTE_PROMPT_CHAR_CAP:
        truncated = passages_text[:_NOTE_PROMPT_CHAR_CAP]
        # Cut at the last passage boundary ("\n---\n") so we don't end mid-passage
        last_boundary = truncated.rfind("\n---\n")
        if last_boundary > _NOTE_PROMPT_CHAR_CAP // 2:
            truncated = truncated[:last_boundary]
        logger.info(
            "Capping note-extraction prompt: %d → %d chars",
            len(passages_text),
            len(truncated),
        )
        passages_text = truncated

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

    return await _llm_complete(
        client,
        url,
        messages,
        timeout=180.0,
        max_tokens=_MAX_TOKENS_NOTES,
        phase="notes",
    )


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

    content = await _llm_complete(
        client,
        url,
        messages,
        max_tokens=_MAX_TOKENS_GAP,
        phase="gap_analysis",
        json_mode=True,
    )
    parsed = _parse_json_from_llm(content)
    gaps = [g for g in parsed.get("gaps", []) if _is_plausible_query(g)]
    return gaps[:5]


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

                    # Reserve time for synthesis; if we've already burned most
                    # of the budget on planning + reading, skip note extraction
                    # and pass raw passages through to synthesis.
                    time_limit_s = (state.time_limit_minutes or 30) * 60
                    synthesis_reserve_s = max(180, int(time_limit_s * 0.25))
                    if elapsed > time_limit_s - synthesis_reserve_s:
                        logger.warning(
                            "Skipping note extraction: elapsed=%ds, budget=%ds, reserving %ds for synthesis",
                            elapsed,
                            time_limit_s,
                            synthesis_reserve_s,
                        )
                        notes_text = f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"
                        yield _sse(
                            {
                                "type": "notes",
                                "content": "Time-budget tight — skipping note extraction, going straight to synthesis.",
                            }
                        )
                    else:
                        try:
                            notes_text = await _extract_notes(client, llm_url, user_question, passages_text)
                        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                            logger.error("LLM error during note extraction: %s", exc)
                            # Fallback: use raw passages as notes
                            notes_text = f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"

                    collected_notes.append(f"## Research notes (round 1)\n{notes_text}")
                    yield _sse({"type": "notes", "content": notes_text[:3000]})

                    step_count = 4
                    state.current_round = step_count
                    state.heartbeat_at = datetime.now(UTC)
                    state.notes = "\n\n".join(collected_notes)
                    await session.commit()

                    # -------------------------------------------------------
                    # Phase 5: Gap analysis (optional, up to 2 rounds)
                    #
                    # Round 1 fires if the depth config enables gap rounds and
                    # we still have ≥30% of the time budget. Round 2 fires
                    # only if round 1 actually added new content AND we still
                    # have ≥50% of the time budget remaining — so it can't
                    # come at the expense of synthesis. Most standard runs
                    # finish well under budget, leaving room for a second
                    # round to push coverage further on the niche misses
                    # documented in the cross-topic analysis.
                    # -------------------------------------------------------
                    gap_round_max = 2 if depth_config["gap_round"] else 0
                    last_round_added_content = True  # bootstrap so round 1 can run
                    for gap_round_n in range(gap_round_max):
                        if not (len(coverage) > 0 and last_round_added_content):
                            break
                        elapsed = int((datetime.now(UTC) - start_time).total_seconds())
                        time_limit_s = (state.time_limit_minutes or 30) * 60
                        # First round needs ≤70% used (matches prior behavior);
                        # second round needs ≤50% used so synthesis still has
                        # plenty of headroom.
                        threshold = 0.7 if gap_round_n == 0 else 0.5
                        if elapsed >= time_limit_s * threshold:
                            break
                        round_label = "" if gap_round_n == 0 else f" (round {gap_round_n + 1})"
                        yield _sse(
                            {
                                "type": "progress",
                                "step": 5,
                                "phase": "gap_analysis",
                                "round": gap_round_n + 1,
                                "elapsed_seconds": elapsed,
                                "strategy": strategy,
                            }
                        )
                        yield _sse({"type": "notes", "content": f"Checking for gaps in coverage{round_label}..."})

                        # Pass the LATEST notes (including the previous gap
                        # round, if any) so the model doesn't re-suggest
                        # gaps the previous round already filled.
                        gap_input_notes = "\n\n".join(collected_notes)
                        try:
                            gap_queries = await _check_gaps(
                                client,
                                llm_url,
                                user_question,
                                gap_input_notes,
                                coverage_summary,
                            )
                        except Exception:
                            logger.exception("Gap analysis failed (round %d)", gap_round_n + 1)
                            gap_queries = []

                        if not gap_queries:
                            last_round_added_content = False
                            yield _sse({"type": "notes", "content": f"No further gaps identified{round_label}."})
                            continue

                        gap_list = ", ".join(gap_queries)
                        collected_notes.append(f"## Gap queries{round_label}\n{gap_list}")
                        yield _sse({"type": "notes", "content": f"Found gaps — searching: {gap_list}"})

                        # Run gap queries
                        gap_coverage = await _search_fan_out(
                            gap_queries,
                            user_id,
                            depth_config["k_per_query"],
                        )
                        # Filter out already-seen chunks
                        new_chunks = {k: v for k, v in gap_coverage.items() if k not in coverage}

                        if not new_chunks:
                            last_round_added_content = False
                            yield _sse(
                                {"type": "notes", "content": f"No new material found in gap search{round_label}"}
                            )
                            continue

                        yield _sse(
                            {
                                "type": "notes",
                                "content": f"Gap search found {len(new_chunks)} new passages{round_label}",
                            }
                        )
                        # Fold these into coverage so a subsequent round
                        # doesn't re-discover the same chunks.
                        coverage.update(new_chunks)

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
                                collected_notes.append(f"## Research notes (gap round {gap_round_n + 1})\n{gap_notes}")
                                yield _sse({"type": "notes", "content": gap_notes[:2000]})
                                last_round_added_content = True
                            except Exception:
                                logger.exception("Gap note extraction failed (round %d)", gap_round_n + 1)
                                last_round_added_content = False
                        else:
                            last_round_added_content = False

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
                        max_tokens=_MAX_TOKENS_SYNTHESIS,
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
                    # Default reason so the API surfaces *something* on the
                    # ``error`` field rather than ``null``. The other two
                    # interrupt paths (synthesis HTTP error, synthesis
                    # connect/timeout) already set a specific reason above.
                    if not state.error:
                        state.error = "Research stream disconnected before completion"
                    await session.commit()
            except Exception:
                logger.exception("Failed to mark research as interrupted on disconnect")
