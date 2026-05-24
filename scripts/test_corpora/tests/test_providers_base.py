# scripts/test_corpora/tests/test_providers_base.py
"""Smoke tests for the Provider Protocol + BaselineResult dataclass."""

import dataclasses

from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
    Provider,
)


def test_baseline_result_is_a_dataclass_with_expected_fields():
    """BaselineResult is the universal return shape from every Provider."""
    assert dataclasses.is_dataclass(BaselineResult)
    fields = {f.name for f in dataclasses.fields(BaselineResult)}
    assert fields == {
        "question_id",
        "question",
        "answer",
        "cited_doc_ids",
        "cited_doc_titles",
        "tool_call_count",
        "tool_transcript",
        "elapsed_seconds",
        "model",
        "timestamp",
    }


def test_default_system_prompt_mentions_corpus_and_mcp_tools():
    """DEFAULT_SYSTEM_PROMPT scopes the model to MCP-only retrieval."""
    assert "MCP" in DEFAULT_SYSTEM_PROMPT or "mcp" in DEFAULT_SYSTEM_PROMPT
    assert "corpus" in DEFAULT_SYSTEM_PROMPT.lower()


def test_provider_is_a_protocol_with_run_question():
    """Provider is a Protocol; any class with run_question matching the
    signature satisfies it (duck-typed at runtime via @runtime_checkable)."""

    class _FakeProvider:
        def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
            return BaselineResult(
                question_id=question_id,
                question=question,
                answer="",
                cited_doc_ids=[],
                cited_doc_titles=[],
                tool_call_count=0,
                tool_transcript=[],
                elapsed_seconds=0.0,
                model="fake",
                timestamp="2026-05-23T00:00:00Z",
            )

    # @runtime_checkable Protocol allows isinstance() against duck-typed classes
    assert isinstance(_FakeProvider(), Provider)


def test_anthropic_provider_class_is_importable_from_new_path():
    """AnthropicProvider lives at providers.anthropic_provider — this is the
    canonical path; claude_baseline.py becomes a shim in Task 3."""
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider

    assert AnthropicProvider.__name__ == "AnthropicProvider"
    # AnthropicProvider needs a real client+mcp_session to instantiate, so we
    # use a structural check via hasattr rather than isinstance against an
    # instance. Provider is @runtime_checkable, but structural duck-typing
    # against the class itself reads more clearly.
    assert hasattr(AnthropicProvider, "run_question")
    assert callable(AnthropicProvider.run_question)
    _ = Provider  # silence unused-import warning


def test_providers_package_exports_canonical_api():
    """The providers package re-exports the canonical API for callers."""
    from scripts.test_corpora.runner import providers

    assert providers.BaselineResult.__name__ == "BaselineResult"
    assert providers.DEFAULT_SYSTEM_PROMPT  # non-empty string
    assert callable(providers.make_provider)
    assert providers.Provider.__name__ == "Provider"
    assert providers.AnthropicProvider.__name__ == "AnthropicProvider"
    assert providers.OpenAIProvider.__name__ == "OpenAIProvider"
