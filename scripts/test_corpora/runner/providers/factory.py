# scripts/test_corpora/runner/providers/factory.py
"""make_provider — single dispatch point that maps model name → Provider.

String matching by prefix:
  claude-*           -> AnthropicProvider (cloud, direct MCP)
  gpt-* / o1-* / o3-* -> OpenAIProvider (cloud, direct MCP)
  anything else      -> LocalProvider (HC-hosted llama-server via chat API)

The local fallback is intentional: local HC-hosted model ids vary widely
(qwen3-8b, deepseek-r1-0528-8b, gemma4-26b-a4b, ...) and don't share a
stable prefix. The factory accepts them all and lets HC's chat model
registry decide whether the activate call succeeds.

Cloud providers lazily construct their underlying client (anthropic.Anthropic()
or openai.OpenAI()) inside the matched branch via the provider's own
constructor — both read API keys from the standard env vars (ANTHROPIC_API_KEY,
OPENAI_API_KEY). LocalProvider requires ``hc_client`` (a HarborClerkClient
already authenticated against HC's admin API for the model-activate call).
"""

from __future__ import annotations

from typing import Any

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.base import Provider
from scripts.test_corpora.runner.providers.local_provider import LocalProvider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-")
# Models whose name starts with an "OpenAI-looking" prefix but are actually
# HC-hosted local llama-server models. They must be excluded from the cloud
# routing decision so make_provider returns LocalProvider for them.
_LOCAL_OVERRIDES = ("gpt-oss-",)


def model_is_cloud(model: str) -> bool:
    """True for cloud-routed models (claude-*, gpt-*, o1-*, o3-*).

    `gpt-oss-*` is explicitly excluded — it's a HC-hosted local model whose
    name happens to start with `gpt-`. Without this carve-out the factory
    would dispatch it to the OpenAI API and get a 404.

    Callers use this to pick between the cloud path (open an MCP session
    against HC) and the local path (drive HC's chat API for HC-hosted
    llama-server models).
    """
    if model.startswith(_LOCAL_OVERRIDES):
        return False
    return model.startswith(_ANTHROPIC_PREFIXES + _OPENAI_PREFIXES)


def make_provider(
    model: str,
    *,
    mcp_session: Any = None,
    hc_client: Any = None,
    doc_ids_seen: list[str] | None = None,
) -> Provider:
    """Construct a Provider for `model`.

    Cloud models (claude-*, gpt-*, o1-*, o3-*) require ``mcp_session``.
    Anything else is treated as an HC-hosted local model and requires
    ``hc_client`` (admin-authed, since model activation needs JWT).

    ``doc_ids_seen`` pre-seeds the cited-doc map (used by tests + by callers
    that want to anchor citations to a known starting set).
    """
    if model.startswith(_ANTHROPIC_PREFIXES):
        return AnthropicProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    if model.startswith(_OPENAI_PREFIXES) and not model.startswith(_LOCAL_OVERRIDES):
        return OpenAIProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    if hc_client is None:
        raise ValueError(f"model {model!r} is not a cloud model; LocalProvider needs hc_client=")
    return LocalProvider(
        hc_client=hc_client,
        model=model,
        mcp_session=mcp_session,
        doc_ids_seen=doc_ids_seen,
    )
