"""Document summarization — adaptive LLM-based with extractive fallback.

Three tiers based on document length:
- Short (<20 chunks): single pass with all content
- Medium (20-100 chunks): strategic sampling (beginning + middle + end)
- Long (100+ chunks): map-reduce (group summaries → final summary)
"""

from __future__ import annotations

import logging
from enum import Enum

import httpx

from harbor_clerk.config import get_settings
from harbor_clerk.llm.models import get_model

logger = logging.getLogger(__name__)

# --- Thresholds ---
_SHORT_THRESHOLD = 20
_LONG_THRESHOLD = 100
_MAX_INPUT_CHARS = 80_000  # hard cap per LLM call
_DEFAULT_CONTEXT_WINDOW = 32_768


class _Tier(Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


# --- System prompts (all end with /no_think) ---
_PROMPT_SHORT = (
    "Summarize this document in 2-3 concise sentences. "
    "Cover the main topic, key conclusions, and document type. /no_think"
)
_PROMPT_MEDIUM = (
    "You are reading representative excerpts from a longer document. "
    "Summarize the full document in 2-3 concise sentences based on these excerpts. /no_think"
)
_PROMPT_MAP = (
    "Summarize this section of a longer document in 2-3 sentences. "
    "Focus on the key points and any conclusions. /no_think"
)
_PROMPT_REDUCE = (
    "Below are summaries of different sections of a single document. "
    "Write a unified 2-3 sentence summary of the entire document. /no_think"
)


# --- Helpers ---


def _compute_max_input_chars(context_window: int | None) -> int:
    """Compute max input chars from model context window, capped at 80K."""
    cw = context_window or _DEFAULT_CONTEXT_WINDOW
    # ~75% of context for input, ~3.5 chars per token
    return min(int(cw * 0.75 * 3.5), _MAX_INPUT_CHARS)


def _select_tier(num_chunks: int) -> _Tier:
    if num_chunks < _SHORT_THRESHOLD:
        return _Tier.SHORT
    elif num_chunks < _LONG_THRESHOLD:
        return _Tier.MEDIUM
    return _Tier.LONG


def _call_llm(
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int = 250,
    timeout: float = 60.0,
) -> str | None:
    """Make a single LLM call. Returns response text or None on failure."""
    settings = get_settings()
    try:
        resp = httpx.post(
            f"{settings.llama_server_url}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return content if content else None
    except Exception:
        logger.warning("LLM call failed", exc_info=True)
        return None


def _sample_chunks(chunks: list[str], max_chars: int) -> str:
    """Select representative chunks: first 3 + last 2 + evenly-spaced middle, within char budget."""
    if not chunks:
        return ""

    n = len(chunks)
    if n <= 5:
        text = "\n\n".join(chunks)
        return text[:max_chars]

    # Always include first 3 and last 2
    head = list(range(min(3, n)))
    tail = list(range(max(n - 2, 0), n))

    # Evenly-spaced middle indices (excluding head/tail)
    middle_start = len(head)
    middle_end = n - len(tail)
    if middle_end > middle_start:
        # Pick up to 10 evenly-spaced middle chunks
        num_middle = min(10, middle_end - middle_start)
        step = (middle_end - middle_start) / (num_middle + 1)
        middle = [int(middle_start + step * (i + 1)) for i in range(num_middle)]
    else:
        middle = []

    indices = sorted(set(head + middle + tail))

    # Build text within char budget
    parts: list[str] = []
    total = 0
    for idx in indices:
        chunk = chunks[idx]
        addition = len(chunk) + 2  # +2 for \n\n separator
        if total + addition > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                parts.append(chunk[:remaining])
            break
        parts.append(chunk)
        total += addition

    return "\n\n".join(parts)


def _group_chunks_for_mapreduce(chunks: list[str], chars_per_group: int) -> list[str]:
    """Group sequential chunks into groups respecting char limits."""
    groups: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for chunk in chunks:
        addition = len(chunk) + (2 if current_parts else 0)
        if current_parts and current_len + addition > chars_per_group:
            groups.append("\n\n".join(current_parts))
            current_parts = [chunk]
            current_len = len(chunk)
        else:
            current_parts.append(chunk)
            current_len += addition

    if current_parts:
        groups.append("\n\n".join(current_parts))

    return groups


def _extractive_fallback(chunks: list[str], max_chars: int) -> str:
    """Take first substantial paragraph from initial chunks as summary."""
    text = "\n\n".join(chunks[:5])
    paragraphs = text.split("\n\n")
    for p in paragraphs:
        p = p.strip()
        if len(p) >= 80:
            return p[:max_chars]
    return text[:max_chars].strip()


# --- Tier implementations ---


def _summarize_short(chunks: list[str], max_chars: int, max_input_chars: int) -> str | None:
    """Short docs: concat all chunks, single LLM call."""
    text = "\n\n".join(chunks)[:max_input_chars]
    return _call_llm(_PROMPT_SHORT, text, max_tokens=250, timeout=60.0)


def _summarize_medium(chunks: list[str], max_chars: int, max_input_chars: int) -> str | None:
    """Medium docs: strategic sampling, single LLM call."""
    text = _sample_chunks(chunks, max_input_chars)
    return _call_llm(_PROMPT_MEDIUM, text, max_tokens=250, timeout=60.0)


def _summarize_long(chunks: list[str], max_chars: int, max_input_chars: int) -> str | None:
    """Long docs: map-reduce — group summaries then final summary."""
    groups = _group_chunks_for_mapreduce(chunks, max_input_chars)
    logger.info("Map-reduce summarization: %d groups from %d chunks", len(groups), len(chunks))

    # Map step: summarize each group
    section_summaries: list[str] = []
    for group in groups:
        result = _call_llm(_PROMPT_MAP, group, max_tokens=150, timeout=45.0)
        if result:
            section_summaries.append(result[:300])
        else:
            # Extractive snippet for failed group
            snippet = group[:200].strip()
            if snippet:
                section_summaries.append(snippet)

    if not section_summaries:
        return None

    # Reduce step: combine section summaries into final
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(section_summaries))
    reduce_input = numbered[:max_input_chars]
    return _call_llm(_PROMPT_REDUCE, reduce_input, max_tokens=250, timeout=60.0)


# --- Main entry point ---


def generate_summary(chunks: list[str], max_chars: int | None = None) -> tuple[str, str]:
    """Generate a summary for a document from its chunks.

    Uses an adaptive strategy based on document length:
    - Short (<20 chunks): single pass with all content
    - Medium (20-100 chunks): strategic sampling + single LLM call
    - Long (100+ chunks): map-reduce (group summaries → final)

    Falls back to extractive heuristic when no LLM is available.
    Never raises — returns best-effort summary.

    Returns (summary_text, model_used) where model_used is the LLM model id
    or "extractive" for the heuristic fallback.
    """
    settings = get_settings()
    if max_chars is None:
        max_chars = settings.summary_max_chars

    # Filter out empty/whitespace-only chunks
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return "", ""

    # Try LLM if a model is active
    if settings.llm_model_id:
        model = get_model(settings.llm_model_id)
        context_window = model.context_window if model else None
        max_input_chars = _compute_max_input_chars(context_window)

        tier = _select_tier(len(chunks))
        logger.info(
            "Summarizing %d chunks via %s tier (model=%s, max_input=%d)",
            len(chunks),
            tier.value,
            settings.llm_model_id,
            max_input_chars,
        )

        if tier == _Tier.SHORT:
            result = _summarize_short(chunks, max_chars, max_input_chars)
        elif tier == _Tier.MEDIUM:
            result = _summarize_medium(chunks, max_chars, max_input_chars)
        else:
            result = _summarize_long(chunks, max_chars, max_input_chars)

        if result:
            return result[:max_chars], settings.llm_model_id
        logger.warning("LLM summarization returned no result — falling back to extractive")
    else:
        logger.warning(
            "No language model active — document summary will be lower quality. "
            "Activate a model in System Settings > Models."
        )

    return _extractive_fallback(chunks, max_chars), "extractive"
