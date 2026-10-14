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


def test_openai_provider_retries_on_429_rate_limit(monkeypatch):
    """openai.RateLimitError (429) inside run_question is absorbed with
    exponential backoff — the SDK's own retry budget is too short for TPM
    limit windows (60s) and would otherwise propagate up and kill the eval."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    rate_limit_err = openai.RateLimitError("rate limited", response=response, body=None)

    client = MagicMock()
    # Throw 429 twice, then succeed
    client.chat.completions.create.side_effect = [
        rate_limit_err,
        rate_limit_err,
        _mk_resp(finish_reason="stop", text="finally got through"),
    ]

    # Patch the provider module's time.sleep so the test runs in ms not minutes
    sleeps: list[float] = []
    import scripts.test_corpora.runner.providers.openai_provider as openai_mod

    monkeypatch.setattr(openai_mod.time, "sleep", sleeps.append)

    p = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q-rl", corpus="cuad")

    assert res.answer == "finally got through"
    assert client.chat.completions.create.call_count == 3
    # First two attempts hit 429; backoff schedule is 30s, 60s, 120s, 240s,
    # so we should see exactly [30.0, 60.0] sleeps.
    assert sleeps == [30.0, 60.0]


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


def test_openai_provider_makes_a_bounded_number_of_model_calls_and_the_last_has_no_tools():
    """The same bound as the Anthropic loop (#682): a model that never stops searching is stopped."""
    from scripts.test_corpora.runner.providers.base import MAX_MODEL_CALLS

    mcp = MagicMock()
    mcp.list_tools.return_value = [SimpleNamespace(name="kb_search", description="s", inputSchema={"type": "object"})]
    mcp.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "d"}')])

    def respond(**kwargs):
        if kwargs["tool_choice"] == "none":
            return _mk_resp(finish_reason="stop", text="what I have so far")
        return _mk_resp(finish_reason="tool_calls", tool_calls=[_mk_tool_call(id="c", name="kb_search", args={})])

    client = MagicMock()
    client.chat.completions.create.side_effect = respond
    res = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client).run_question(
        question="q", question_id="q", corpus="cuad"
    )
    from scripts.test_corpora.runner.providers.base import ANSWER_NOW

    calls = client.chat.completions.create.call_args_list
    assert [c.kwargs["tool_choice"] for c in calls] == ["auto"] * (MAX_MODEL_CALLS - 1) + ["none"]
    # The fake records the message list by reference, and the assistant's reply is appended to it afterwards.
    assert calls[-1].kwargs["messages"][-2:] == [
        {"role": "user", "content": ANSWER_NOW},
        {"role": "assistant", "content": "what I have so far"},
    ]
    assert res.tool_call_count == MAX_MODEL_CALLS - 1
    assert res.answer == "what I have so far" and res.stopped_by == "model_call_limit"


def test_openai_provider_says_when_it_finished_on_its_own():
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(finish_reason="stop", text="done")
    res = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client).run_question(
        question="q", question_id="q", corpus="cuad"
    )
    assert res.stopped_by == "end_turn"


def test_openai_provider_records_a_truncation_as_a_truncation():
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(finish_reason="length", text="half an ans")
    res = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client).run_question(
        question="q", question_id="q", corpus="cuad"
    )
    assert res.stopped_by == "length"


# ── GPT-6: the Responses API ──


def _mk_response(*, output, status="completed", incomplete_reason=None):
    """A minimal Responses API object: `output` items, `status`, `incomplete_details`."""
    details = SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
    return SimpleNamespace(status=status, output=list(output), incomplete_details=details)


def _message(text: str, *, refusal: bool = False):
    parts = (
        [SimpleNamespace(type="refusal", refusal=text)] if refusal else [SimpleNamespace(type="output_text", text=text)]
    )
    return SimpleNamespace(type="message", role="assistant", content=parts)


def _function_call(call_id: str, name: str, args: dict):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=json.dumps(args))


def _reasoning(item_id: str):
    return SimpleNamespace(type="reasoning", id=item_id, summary=[])


def test_gpt_6_takes_its_tools_through_the_responses_api():
    """OpenAI's function-calling guide (2026-10-14): GPT-6 Astra and GPT-6.1 Sol "require the Responses API for
    tool calling". Flat tools, `instructions` for the system prompt, `max_output_tokens` for the limit, and the
    model's whole output (its reasoning items included) echoed back before the function_call_output items."""
    from scripts.test_corpora.runner.providers.base import DEFAULT_SYSTEM_PROMPT
    from scripts.test_corpora.runner.providers.openai_provider import MAX_OUTPUT_TOKENS, uses_responses_api

    assert [uses_responses_api(m) for m in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-luna", "gpt-4o")] == [
        True, True, True, False, False,
    ]  # fmt: skip
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        SimpleNamespace(name="kb_search", description="search", inputSchema={"type": "object"})
    ]
    mcp.call_tool.side_effect = [
        SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "doc-a", "doc_title": "Alpha"}')]),
        SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "doc-b", "doc_title": "Beta"}')]),
    ]
    client = MagicMock()
    client.responses.create.side_effect = [
        _mk_response(
            output=[
                _reasoning("rs_1"),
                _function_call("call-1", "kb_search", {"query": "alpha"}),
                _function_call("call-2", "kb_search", {"query": "beta"}),
            ]
        ),
        _mk_response(output=[_reasoning("rs_2"), _message("Per doc-a and doc-b.")]),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-6-astra", client=client)
    res = p.run_question(question="Find both.", question_id="q6", corpus="cuad")

    assert res.answer == "Per doc-a and doc-b." and res.model == "gpt-6-astra" and res.stopped_by == "end_turn"
    assert res.tool_call_count == 2 and res.model_call_count == 2
    assert res.cited_doc_ids == ["doc-a", "doc-b"] and res.cited_doc_titles == ["Alpha", "Beta"]
    assert [e["args"]["query"] for e in res.tool_transcript] == ["alpha", "beta"]
    assert not client.chat.completions.create.called

    first, second = (c.kwargs for c in client.responses.create.call_args_list)
    assert first["model"] == "gpt-6-astra" and first["instructions"] == DEFAULT_SYSTEM_PROMPT
    assert first["max_output_tokens"] == MAX_OUTPUT_TOKENS and "max_completion_tokens" not in first
    assert first["tools"] == [
        {"type": "function", "name": "kb_search", "description": "search", "parameters": {"type": "object"}}
    ]
    assert first["tool_choice"] == "auto" and "temperature" not in first and "reasoning" not in first
    assert first["input"] == [{"role": "user", "content": "Find both."}]
    # The second request carries the first's whole output, then one output item per call_id.
    items = second["input"]
    assert items[0] == {"role": "user", "content": "Find both."}
    assert items[1] == {"type": "reasoning", "id": "rs_1", "summary": []}
    assert [i["type"] for i in items[2:4]] == ["function_call", "function_call"]
    assert items[4:] == [
        {"type": "function_call_output", "call_id": "call-1", "output": '{"doc_id": "doc-a", "doc_title": "Alpha"}'},
        {"type": "function_call_output", "call_id": "call-2", "output": '{"doc_id": "doc-b", "doc_title": "Beta"}'},
    ]


def test_the_responses_loop_is_bounded_and_the_last_call_has_no_tools():
    from scripts.test_corpora.runner.providers.base import ANSWER_NOW, MAX_MODEL_CALLS

    mcp = MagicMock()
    mcp.list_tools.return_value = [SimpleNamespace(name="kb_search", description="s", inputSchema={"type": "object"})]
    mcp.call_tool.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"doc_id": "d"}')])

    def respond(**kwargs):
        if kwargs["tool_choice"] == "none":
            return _mk_response(output=[_message("what I have so far")])
        return _mk_response(output=[_function_call("c", "kb_search", {})])

    client = MagicMock()
    client.responses.create.side_effect = respond
    res = OpenAIProvider(mcp_session=mcp, model="gpt-6.1-sol", client=client).run_question("q", "q1", "cuad")
    calls = client.responses.create.call_args_list
    assert len(calls) == MAX_MODEL_CALLS == res.model_call_count
    assert [c.kwargs["tool_choice"] for c in calls] == ["auto"] * 15 + ["none"]
    assert calls[-1].kwargs["input"][-1] == {"role": "user", "content": ANSWER_NOW}
    assert res.tool_call_count == 15 and res.answer == "what I have so far" and res.stopped_by == "model_call_limit"


def test_a_responses_answer_that_was_cut_short_or_refused_says_so():
    for response, expected in (
        (
            _mk_response(output=[_message("half an ans")], status="incomplete", incomplete_reason="max_output_tokens"),
            "max_output_tokens",
        ),
        (_mk_response(output=[_message("no", refusal=True)]), "refusal"),
        (_mk_response(output=[_message("done")]), "end_turn"),
        (_mk_response(output=[_reasoning("rs")]), "end_turn"),
    ):
        client = MagicMock()
        client.responses.create.return_value = response
        res = OpenAIProvider(mcp_session=None, model="gpt-6-astra", client=client).run_question("q", "q", "c")
        assert res.stopped_by == expected, expected
    assert "tools" not in client.responses.create.call_args.kwargs, "no MCP session: no tools, no tool_choice"
    client.responses.create.return_value = _mk_response(output=[_message("no", refusal=True)])
    assert OpenAIProvider(mcp_session=None, model="gpt-6-astra", client=client).run_question("q", "q", "c").answer == ""


def test_the_responses_loop_retries_a_429_like_the_chat_loop(monkeypatch):
    import httpx
    import openai

    import scripts.test_corpora.runner.providers.openai_provider as openai_mod

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    err = openai.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)
    client = MagicMock()
    client.responses.create.side_effect = [err, _mk_response(output=[_message("through")])]
    sleeps: list[float] = []
    monkeypatch.setattr(openai_mod.time, "sleep", sleeps.append)
    res = OpenAIProvider(mcp_session=None, model="gpt-6-astra", client=client).run_question("q", "q", "c")
    assert res.answer == "through" and sleeps == [30.0]


def test_a_real_sdk_output_item_goes_back_as_the_dict_it_dumps_to():
    """By alias: the SDK's function-call item spells the API's `async` field `async_`, and a dump by field name
    would send an unknown parameter back on the next call (second review of #811)."""
    from openai.types.responses import ResponseFunctionToolCall

    from scripts.test_corpora.runner.providers.openai_provider import _as_item

    class Item:
        def model_dump(self, exclude_none=False, by_alias=False):
            return {"type": "reasoning", "id": "rs", "summary": [], "by_alias": by_alias}

    assert _as_item(Item()) == {"type": "reasoning", "id": "rs", "summary": [], "by_alias": True}
    wire = {"type": "function_call", "call_id": "c", "name": "kb_search", "arguments": "{}", "async": False}
    back = _as_item(ResponseFunctionToolCall.model_validate(wire))
    assert back == wire and "async_" not in back
    assert _as_item({"type": "message"}) == {"type": "message"}
    assert _as_item(SimpleNamespace(type="function_call", call_id="c", status=None)) == {
        "type": "function_call",
        "call_id": "c",
    }
