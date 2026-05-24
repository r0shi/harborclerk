# scripts/test_corpora/runner/providers/factory.py
"""make_provider — single dispatch point that maps model name → Provider.

String matching by prefix:
  claude-*           -> AnthropicProvider
  gpt-* / o1-* / o3-* -> OpenAIProvider

Unknown prefixes raise ValueError with the supported list. The factory
lazily constructs the underlying client (anthropic.Anthropic() or
openai.OpenAI()) inside the matched branch via the provider's own
constructor — both read API keys from the standard env vars
(ANTHROPIC_API_KEY, OPENAI_API_KEY).
"""

from __future__ import annotations

from typing import Any

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.base import Provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-")


def make_provider(
    model: str,
    *,
    mcp_session: Any,
    doc_ids_seen: list[str] | None = None,
) -> Provider:
    """Construct a Provider for `model` wired to `mcp_session`.

    `doc_ids_seen` pre-seeds the cited-doc map (used by tests + by callers
    that want to anchor citations to a known starting set).
    """
    if model.startswith(_ANTHROPIC_PREFIXES):
        return AnthropicProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    if model.startswith(_OPENAI_PREFIXES):
        return OpenAIProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    supported = ", ".join(_ANTHROPIC_PREFIXES + _OPENAI_PREFIXES)
    raise ValueError(f"unknown model {model!r}; supported prefixes: {supported}")
