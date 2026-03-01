"""Document summarization — LLM-based with extractive fallback."""

import logging

import httpx

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = "Summarize the following document excerpt in 1-2 sentences. Be concise. /no_think"


def _extractive_fallback(text: str, max_chars: int) -> str:
    """Take first substantial paragraph as summary."""
    paragraphs = text.split("\n\n")
    for p in paragraphs:
        p = p.strip()
        if len(p) >= 80:
            return p[:max_chars]
    # No qualifying paragraph — take raw text
    return text[:max_chars].strip()


def generate_summary(chunks_text: str, max_chars: int | None = None) -> tuple[str, str]:
    """Generate a summary for a document from its chunk text.

    Uses the local LLM when available, falls back to extractive heuristic.
    Never raises — returns best-effort summary.

    Returns (summary_text, model_used) where model_used is the LLM model id
    or "extractive" for the heuristic fallback.
    """
    settings = get_settings()
    if max_chars is None:
        max_chars = settings.summary_max_chars

    if not chunks_text.strip():
        return "", ""

    # Truncate input to ~3000 chars for LLM context
    input_text = chunks_text[:3000]

    # Try LLM if a model is active
    if settings.llm_model_id:
        try:
            resp = httpx.post(
                f"{settings.llama_server_url}/v1/chat/completions",
                json={
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": input_text},
                    ],
                    "stream": False,
                    "temperature": 0.3,
                    "max_tokens": 200,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                return content[:max_chars], settings.llm_model_id
            logger.warning("LLM returned empty summary — falling back to extractive")
        except Exception:
            logger.warning(
                "LLM summary failed — falling back to extractive summary",
                exc_info=True,
            )

    else:
        logger.warning(
            "No language model active — document summary will be lower quality. "
            "Activate a model in System Settings > Models."
        )

    return _extractive_fallback(input_text, max_chars), "extractive"
