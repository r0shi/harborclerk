# scripts/test_corpora/runner/providers/__init__.py
"""Multi-provider abstraction for the answer-eval baseline.

Public API:
  Provider              — typing.Protocol every provider satisfies
  BaselineResult        — universal dataclass returned by every provider
  DEFAULT_SYSTEM_PROMPT — shared system prompt across providers
  AnthropicProvider     — Sonnet (or any claude-* model) + MCP
  OpenAIProvider        — gpt-4o (or any gpt-*/o1-*/o3-* model) + MCP
  make_provider         — factory: dispatch by model-name prefix

See docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md.
"""

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
    Provider,
)
from scripts.test_corpora.runner.providers.factory import make_provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "BaselineResult",
    "DEFAULT_SYSTEM_PROMPT",
    "OpenAIProvider",
    "Provider",
    "make_provider",
]
