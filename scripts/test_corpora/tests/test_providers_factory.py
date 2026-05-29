# scripts/test_corpora/tests/test_providers_factory.py
"""make_provider() — string-prefix dispatch on model name."""

from unittest.mock import MagicMock

import pytest

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.factory import make_provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider


def test_factory_dispatches_claude_to_anthropic():
    """Any name starting with 'claude-' → AnthropicProvider."""
    p = make_provider("claude-sonnet-4-6", mcp_session=MagicMock())
    assert isinstance(p, AnthropicProvider)


def test_factory_dispatches_claude_3_5_to_anthropic():
    """Legacy claude- names still dispatch correctly."""
    p = make_provider("claude-3-5-sonnet-20241022", mcp_session=MagicMock())
    assert isinstance(p, AnthropicProvider)


def test_factory_dispatches_gpt_to_openai():
    """Any name starting with 'gpt-' → OpenAIProvider."""
    p = make_provider("gpt-4o", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_dispatches_o1_to_openai():
    """Any name starting with 'o1-' → OpenAIProvider."""
    p = make_provider("o1-preview", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_dispatches_o3_to_openai():
    """Any name starting with 'o3-' → OpenAIProvider."""
    p = make_provider("o3-mini", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_raises_value_error_on_local_model_without_hc_client():
    """Non-cloud prefixes fall through to LocalProvider, which requires
    hc_client. Without it the factory raises ValueError naming the missing
    keyword — callers must wire up HC auth for local models."""
    with pytest.raises(ValueError, match=r"is not a cloud model.*LocalProvider needs hc_client"):
        make_provider("llama-3.1-70b", mcp_session=MagicMock())


def test_factory_forwards_doc_ids_seen_kwarg_to_anthropic():
    """doc_ids_seen pre-seeds the provider's cited-doc map for test scenarios."""
    p = make_provider("claude-sonnet-4-6", mcp_session=MagicMock(), doc_ids_seen=["doc-a", "doc-b"])
    assert isinstance(p, AnthropicProvider)
    # cited starts pre-seeded with the two doc ids (titles default to "")
    assert list(p._cited.keys()) == ["doc-a", "doc-b"]


def test_factory_forwards_doc_ids_seen_kwarg_to_openai():
    """doc_ids_seen works the same way for the OpenAI provider."""
    p = make_provider("gpt-4o", mcp_session=MagicMock(), doc_ids_seen=["doc-x"])
    assert isinstance(p, OpenAIProvider)
    assert list(p._cited.keys()) == ["doc-x"]
