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
