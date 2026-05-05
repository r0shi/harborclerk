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
