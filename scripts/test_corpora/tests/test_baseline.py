from unittest.mock import MagicMock

from scripts.test_corpora.runner.claude_baseline import BaselineGenerator, BaselineResult


def test_baseline_generator_collects_citations_from_tool_calls():
    fake = MagicMock()
    # Anthropic tool-use response: the model produced a final text block.
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(text="The answer references doc-a and doc-b.")],
        stop_reason="end_turn",
    )

    gen = BaselineGenerator(client=fake, mcp_session=None, doc_ids_seen=["doc-a", "doc-b"])
    res = gen.run_question(question="What?", question_id="q1", corpus="cuad")
    assert isinstance(res, BaselineResult)
    assert res.cited_doc_ids == ["doc-a", "doc-b"]
    assert "doc-a" in res.answer or res.answer
    # doc_ids_seen pre-seed carries no titles
    assert res.cited_doc_titles == ["", ""]


def test_collect_doc_ids_pairs_doc_id_with_doc_title():
    """_collect_doc_ids must harvest doc_title alongside doc_id so
    citation_overlap can be computed on stable titles instead of
    per-ingest random UUIDs. Mirrors a kb_search MCP result shape."""
    gen = BaselineGenerator(client=MagicMock(), mcp_session=None)
    # kb_search emits doc_id + doc_title together (mcp_server.py).
    gen._collect_doc_ids(
        {
            "results": [
                {"doc_id": "uuid-1", "doc_title": "0042_vendor_contract", "score": 0.9},
                {"doc_id": "uuid-2", "doc_title": "skilling-j_inbox_17", "score": 0.8},
            ]
        }
    )
    assert gen._cited == {"uuid-1": "0042_vendor_contract", "uuid-2": "skilling-j_inbox_17"}


def test_collect_doc_ids_falls_back_to_title_then_empty():
    """If a tool result uses 'title' instead of 'doc_title', use it; if it
    has neither, store an empty string (citation_overlap then just can't
    match that one doc — better than crashing)."""
    gen = BaselineGenerator(client=MagicMock(), mcp_session=None)
    gen._collect_doc_ids({"doc_id": "uuid-a", "title": "fallback name"})
    gen._collect_doc_ids({"doc_id": "uuid-b"})  # no title at all
    assert gen._cited == {"uuid-a": "fallback name", "uuid-b": ""}


def test_collect_doc_ids_upgrades_empty_title_on_later_mention():
    """A doc first seen without a title, then again with one, should end
    up with the title — kb_read_passages might omit it where kb_search
    includes it."""
    gen = BaselineGenerator(client=MagicMock(), mcp_session=None)
    gen._collect_doc_ids({"doc_id": "uuid-x"})  # titleless first
    gen._collect_doc_ids({"doc_id": "uuid-x", "doc_title": "real title"})  # titled later
    assert gen._cited == {"uuid-x": "real title"}


def test_run_question_records_tool_transcript():
    """Each tool call is recorded in BaselineResult.tool_transcript."""
    from unittest.mock import MagicMock

    from scripts.test_corpora.runner.claude_baseline import BaselineGenerator

    fake = MagicMock()
    tool_block = MagicMock(type="tool_use", id="t1", input={"query": "x"})
    tool_block.name = "kb_search"  # MagicMock(name=...) sets display name, not .name attr
    fake.messages.create.side_effect = [
        MagicMock(content=[tool_block], stop_reason="tool_use"),
        MagicMock(content=[MagicMock(text="final answer", type="text")], stop_reason="end_turn"),
    ]
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.return_value = MagicMock(content=[MagicMock(text='{"doc_id": "d1"}')])

    gen = BaselineGenerator(client=fake, mcp_session=mcp)
    res = gen.run_question(question="q", question_id="q1", corpus="cuad")

    assert len(res.tool_transcript) == 1
    call = res.tool_transcript[0]
    assert call["tool"] == "kb_search"
    assert call["args"] == {"query": "x"}
    assert "doc_id" in call["result_summary"]


def test_run_question_extracts_last_text_block_not_first():
    """The final answer is the last text block of the assistant turn, even when
    the turn's first content block is a non-text block."""
    from unittest.mock import MagicMock

    from scripts.test_corpora.runner.claude_baseline import BaselineGenerator

    non_text = MagicMock(spec=["type", "id"])  # spec'd: has no .text attribute
    non_text.type = "tool_use"
    text_block = MagicMock(text="the real answer")
    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(content=[non_text, text_block], stop_reason="end_turn")

    gen = BaselineGenerator(client=fake, mcp_session=None)
    res = gen.run_question(question="q", question_id="q1", corpus="cuad")

    assert res.answer == "the real answer"


def _asks_for_a_tool_forever():
    """A Claude that never stops searching, which is what `cuad-research-6` did for 24 calls (#682)."""
    from unittest.mock import MagicMock

    def respond(**kwargs):
        if kwargs.get("tool_choice") == {"type": "none"}:
            return MagicMock(content=[MagicMock(text="what I have so far", type="text")], stop_reason="end_turn")
        block = MagicMock(type="tool_use", id=f"t{respond.calls}", input={"query": "x"})
        block.name = "kb_search"
        respond.calls += 1
        return MagicMock(content=[block], stop_reason="tool_use")

    respond.calls = 0
    fake = MagicMock()
    fake.messages.create.side_effect = respond
    mcp = MagicMock()
    mcp.list_tools.return_value = [MagicMock(description="search", inputSchema={"type": "object"})]
    mcp.list_tools.return_value[0].name = "kb_search"
    mcp.call_tool.return_value = MagicMock(content=[MagicMock(text='{"doc_id": "d1"}')])
    return fake, mcp


def test_a_question_makes_a_bounded_number_of_model_calls_and_the_last_has_no_tools():
    from scripts.test_corpora.runner.providers.base import MAX_MODEL_CALLS

    fake, mcp = _asks_for_a_tool_forever()
    res = BaselineGenerator(client=fake, mcp_session=mcp).run_question(question="q", question_id="q1", corpus="cuad")

    from scripts.test_corpora.runner.providers.base import ANSWER_NOW

    calls = fake.messages.create.call_args_list
    # Per question on CUAD: 3 to 10 calls for thirteen of them, then 12, 14 and a runaway 24 (#682).
    assert len(calls) == MAX_MODEL_CALLS == 16
    assert [c.kwargs.get("tool_choice") for c in calls] == [None] * 15 + [{"type": "none"}]
    assert calls[-1].kwargs["tools"], "the transcript holds tool_use blocks: the definitions must still be sent"
    assert res.tool_call_count == 15 and len(res.tool_transcript) == 15
    # With tools off and nothing said, a model mid-search answers "let me look further". It is told, after the
    # tool results it was waiting for (the API wants those first in the message), and on no earlier call.
    final = calls[-1].kwargs["messages"][-1]["content"]
    assert final[-1] == {"type": "text", "text": ANSWER_NOW} and final[0]["type"] == "tool_result"
    assert not any(ANSWER_NOW in str(c.kwargs["messages"]) for c in calls[:-1])
    assert res.answer == "what I have so far"
    assert res.stopped_by == "model_call_limit", "a reference cut short must say so"


def test_a_question_that_finishes_on_its_own_says_so():
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(content=[MagicMock(text="done", type="text")], stop_reason="end_turn")
    res = BaselineGenerator(client=fake, mcp_session=None).run_question(question="q", question_id="q1", corpus="cuad")
    assert fake.messages.create.call_count == 1 and res.stopped_by == "end_turn"
    assert "tool_choice" not in fake.messages.create.call_args.kwargs


def test_every_call_reads_the_previous_calls_prefix_from_the_prompt_cache():
    """Without breakpoints the 16 CUAD baselines sent 6.0 M input tokens at full price (#682). Each call carries
    three: the system prompt, the last tool definition, and the last block of the last message."""
    fake, mcp = _asks_for_a_tool_forever()
    BaselineGenerator(client=fake, mcp_session=mcp).run_question(question="the question", question_id="q1", corpus="c")

    ephemeral = {"type": "ephemeral"}
    *calls, final = fake.messages.create.call_args_list
    # The last call writes nothing to the cache: no later call would read it, and a write costs more than plain
    # input. It still reads the system prompt and the tools.
    assert "cache_control" not in str(final.kwargs["messages"])
    assert "cache_control" in str(final.kwargs["system"]) and "cache_control" in str(final.kwargs["tools"])
    for n, call in enumerate(calls):
        kw = call.kwargs
        assert [b.get("cache_control") for b in kw["system"]] == [ephemeral]
        assert [t.get("cache_control") for t in kw["tools"]] == [ephemeral]
        marked = [
            (i, j)
            for i, m in enumerate(kw["messages"])
            if isinstance(m["content"], list)
            for j, b in enumerate(m["content"])
            if isinstance(b, dict) and "cache_control" in b
        ]
        last = len(kw["messages"]) - 1
        assert marked == [(last, len(kw["messages"][last]["content"]) - 1)], f"call {n}: one moving breakpoint"
        assert kw["messages"][last]["role"] == "user"
        assert len(kw["messages"]) == 1 + 2 * n, "and the whole transcript so far rides with it"
    first = fake.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
    assert first == [{"type": "text", "text": "the question", "cache_control": ephemeral}]


def test_a_truncated_or_refused_answer_is_not_recorded_as_the_model_finishing():
    """`max_tokens` at 8000 output tokens is half an answer. Review of #693: it was recorded as "end_turn"."""
    from unittest.mock import MagicMock

    for reason in ("max_tokens", "refusal", "pause_turn", "end_turn"):
        fake = MagicMock()
        fake.messages.create.return_value = MagicMock(content=[MagicMock(text="...", type="text")], stop_reason=reason)
        res = BaselineGenerator(client=fake, mcp_session=None).run_question(question="q", question_id="q", corpus="c")
        assert res.stopped_by == reason


def test_calls_and_units_are_booked_under_the_kind_the_caller_names(monkeypatch):
    """answer-eval and rerun_pr_j answer other questions than a sweep baseline and are priced apart (spend.yaml)."""
    from unittest.mock import MagicMock

    from scripts.test_corpora.runner import spend
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
    from scripts.test_corpora.runner.providers.factory import make_provider

    built, counted = [], []
    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(content=[MagicMock(text="a", type="text")], stop_reason="end_turn")
    monkeypatch.setattr(spend, "anthropic_client", lambda kind: built.append(kind) or fake)
    monkeypatch.setattr(spend, "get_meter", lambda: MagicMock(count_unit=counted.append))
    make_provider("claude-sonnet-4-6", mcp_session=None, spend_kind="candidate_answer").run_question("q", "q", "c")
    AnthropicProvider(mcp_session=None).run_question("q", "q", "c")
    assert built == counted == ["candidate_answer", "baseline_question"]


def test_a_spend_kind_nobody_priced_is_refused_when_the_provider_is_built():
    import pytest

    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
    from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

    for provider in (AnthropicProvider, OpenAIProvider):
        with pytest.raises(ValueError, match="cannot be planned for"):
            provider(mcp_session=None, spend_kind="baseline")
