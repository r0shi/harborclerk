# scripts/test_corpora/tests/test_openai_provider.py
"""OpenAIProvider — gpt-4o + Harbor Clerk MCP via OpenAI's chat/completions
tool_calls loop. Mirrors AnthropicProvider's shape but speaks OpenAI's API.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.test_corpora.runner.providers.base import BaselineResult
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider


def _mk_resp(*, finish_reason: str, text: str = "", tool_calls=None):
    """Build a minimal OpenAI-style ChatCompletion response object.

    OpenAI SDK returns `resp.choices[0].finish_reason` and
    `resp.choices[0].message.{content, tool_calls}`. tool_calls is a list of
    objects with `.id`, `.type='function'`, `.function.name`, `.function.arguments`
    (JSON string).
    """
    msg = SimpleNamespace(content=text, tool_calls=tool_calls)
    choice = SimpleNamespace(finish_reason=finish_reason, message=msg)
    return SimpleNamespace(choices=[choice])


def _mk_tool_call(*, id: str, name: str, args: dict):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=id, type="function", function=fn)


def test_openai_provider_parses_simple_end_turn_answer():
    """A single response with finish_reason='stop' returns its content as the final answer."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(finish_reason="stop", text="The answer is 42.")
    p = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client)
    res = p.run_question(question="What is the answer?", question_id="q1", corpus="cuad")
    assert isinstance(res, BaselineResult)
    assert res.answer == "The answer is 42."
    assert res.tool_call_count == 0
    assert res.cited_doc_ids == []
    assert res.model == "gpt-4o"


def test_openai_provider_tool_loop_captures_cited_docs():
    """A tool_calls round followed by an end-turn round — cited_doc_ids
    are harvested from the MCP result and the final answer is the second
    response's content."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        SimpleNamespace(name="kb_search", description="search", inputSchema={"type": "object"})
    ]
    # First MCP call returns one block with JSON containing a doc_id+doc_title pair
    mcp.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"doc_id": "doc-abc", "doc_title": "Acme Contract", "snippet": "..."}')]
    )

    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="call-1", name="kb_search", args={"query": "acme"})],
        ),
        _mk_resp(finish_reason="stop", text="Per doc-abc, the answer is found."),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="Find acme.", question_id="q2", corpus="cuad")
    assert res.tool_call_count == 1
    assert res.cited_doc_ids == ["doc-abc"]
    assert res.cited_doc_titles == ["Acme Contract"]
    assert res.answer == "Per doc-abc, the answer is found."


def test_openai_provider_transcript_records_each_call():
    """tool_transcript has one entry per tool_call with tool, args, result_summary."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [SimpleNamespace(name="kb_search", description="", inputSchema={"type": "object"})]
    mcp.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "x"}')])
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="c1", name="kb_search", args={"query": "alpha"})],
        ),
        _mk_resp(finish_reason="stop", text="done"),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q3", corpus="cuad")
    assert len(res.tool_transcript) == 1
    entry = res.tool_transcript[0]
    assert entry["tool"] == "kb_search"
    assert entry["args"] == {"query": "alpha"}
    assert entry["result_summary"].startswith('{"doc_id":')


def test_openai_provider_treats_length_finish_as_end_turn():
    """finish_reason='length' (response truncated) returns the partial answer
    with a logged warning rather than crashing. PR-B's _score() in the judge
    set this defensive precedent — Anthropic's API rarely hits this; OpenAI's
    max_tokens semantics are subtly different and we may hit it."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(
        finish_reason="length", text="The answer begins with the very long preamble"
    )
    p = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q4", corpus="cuad")
    assert res.answer.startswith("The answer begins")
    assert res.tool_call_count == 0


def test_openai_provider_handles_multiple_parallel_tool_calls_in_one_turn():
    """GPT-4o sometimes emits multiple tool_calls in a single assistant turn.
    Each call MUST get a matching role='tool' message keyed by tool_call_id —
    OpenAI rejects the next request with invalid_request_error otherwise.
    Also: tool_call_count and tool_transcript should reflect ALL the calls."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [SimpleNamespace(name="kb_search", description="", inputSchema={"type": "object"})]
    # Each tool call returns a different doc
    mcp.call_tool.side_effect = [
        SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "doc-a", "doc_title": "Alpha"}')]),
        SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "doc-b", "doc_title": "Beta"}')]),
        SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "doc-c", "doc_title": "Gamma"}')]),
    ]
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[
                _mk_tool_call(id="call-1", name="kb_search", args={"query": "alpha"}),
                _mk_tool_call(id="call-2", name="kb_search", args={"query": "beta"}),
                _mk_tool_call(id="call-3", name="kb_search", args={"query": "gamma"}),
            ],
        ),
        _mk_resp(finish_reason="stop", text="See doc-a, doc-b, doc-c."),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="qpar", corpus="cuad")

    # All three tools executed
    assert res.tool_call_count == 3
    assert len(res.tool_transcript) == 3
    assert [e["tool"] for e in res.tool_transcript] == ["kb_search", "kb_search", "kb_search"]
    assert [e["args"]["query"] for e in res.tool_transcript] == ["alpha", "beta", "gamma"]

    # All three doc_ids harvested
    assert set(res.cited_doc_ids) == {"doc-a", "doc-b", "doc-c"}

    # Second request's messages MUST contain a `tool` message per tool_call_id;
    # OpenAI's API enforces this — missing any tool_call_id raises
    # invalid_request_error mid-eval.
    second_call_messages = client.chat.completions.create.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"call-1", "call-2", "call-3"}


def test_openai_provider_falls_back_when_mcp_returns_empty_content():
    """When a tool result has no text content, the loop sends '(empty)' so the
    model doesn't choke on a literal empty string in the tool response."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [SimpleNamespace(name="kb_search", description="", inputSchema={"type": "object"})]
    mcp.call_tool.return_value = SimpleNamespace(content=[])  # empty
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="c1", name="kb_search", args={"q": "x"})],
        ),
        _mk_resp(finish_reason="stop", text="nothing found"),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q5", corpus="cuad")
    assert res.tool_transcript[0]["result_summary"] == "(empty)"
    # Verify the tool result message sent back to the model contained "(empty)"
    call_args_list = client.chat.completions.create.call_args_list
    assert len(call_args_list) == 2
    second_call_messages = call_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    assert tool_msg["content"] == "(empty)"
