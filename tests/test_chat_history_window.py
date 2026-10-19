"""The history window never sends a tool result without its call (#839).

``chat_stream`` reads the last ``max_history_messages`` rows of a conversation, a slice of rows rather than of
exchanges, so the window could start on a tool result whose assistant call was the row before it. ``_trim_to_budget``
dropped such a result only when it trimmed; a history that fit was sent as it was, the result's ``tool_call_id``
naming no call in the prompt. gpt-oss's chat template raises on it ("Message has tool role, but there was no previous
assistant message with a tool call!") and llama-server v0.4.1, the pin, answers the prompt with a 500, so every turn of
such a conversation failed on that model. ``_whole_tool_exchanges`` keeps every exchange whole, in the window and in
the trim.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from harbor_clerk.config import get_settings
from harbor_clerk.llm import chat as chat_module
from harbor_clerk.models.chat_message import ChatMessage
from tests import test_chat_message_order as _order
from tests.test_chat_message_order import (
    _answer_client,
    _assert_pairs_intact,
    _committed_user,
    _conversation,
    _first_prompt,
    _shape,
    _turn_rows,
    _write_turn,
)

# The ordering tests' fixtures (#809): chat_stream on a sequential-scan engine, no model, no topics, fresh ratios.
seq_engine = _order.seq_engine
seq_factory = _order.seq_factory
chat_on_seq_engine = _order.chat_on_seq_engine


def _calls(*ids: str) -> list[dict]:
    return [
        {"id": i, "type": "function", "function": {"name": "search_documents", "arguments": json.dumps({"query": i})}}
        for i in ids
    ]


def _parallel_turn(tag: str) -> list[dict]:
    """A turn whose one assistant row made two calls, then their two results, then the answer."""
    return [
        {"role": "user", "content": f"question {tag}"},
        {"role": "assistant", "content": "", "tool_calls": _calls(f"call_{tag}_1", f"call_{tag}_2")},
        {"role": "tool", "content": json.dumps({"results": [f"{tag} 1"]}), "tool_call_id": f"call_{tag}_1"},
        {"role": "tool", "content": json.dumps({"results": [f"{tag} 2"]}), "tool_call_id": f"call_{tag}_2"},
        {"role": "assistant", "content": f"answer {tag}"},
    ]


def _assert_leads_on_user_or_assistant(prompt: list[dict]) -> None:
    assert prompt, "the prompt holds no history and no question"
    assert prompt[0]["role"] in ("user", "assistant"), f"the history leads on a {prompt[0]['role']} message"


# ---------------------------------------------------------------------------
# _whole_tool_exchanges
# ---------------------------------------------------------------------------


def test_a_leading_result_whose_call_was_cut_is_dropped():
    turn = _turn_rows("t")  # question, call a, result a, call b, result b, answer
    assert chat_module._whole_tool_exchanges(turn[2:]) == turn[3:]


def test_the_results_of_a_parallel_call_cut_between_them_are_dropped():
    turn = _parallel_turn("p")  # question, call (1, 2), result 1, result 2, answer
    assert chat_module._whole_tool_exchanges(turn[2:]) == turn[4:]
    assert chat_module._whole_tool_exchanges(turn[3:]) == turn[4:]


def test_a_call_whose_results_were_cut_after_it_is_dropped_with_what_remains():
    """The other direction: a slice ending between a call and its results. ``chat_stream``'s window cannot (it ends on
    the question just stored), but the helper keeps the invariant whichever side was cut."""
    turn = _parallel_turn("p")
    assert chat_module._whole_tool_exchanges(turn[:3]) == turn[:1]  # the call and its first result, cut there
    assert chat_module._whole_tool_exchanges(turn[:2]) == turn[:1]  # the call alone
    partly = [*turn[:3], {"role": "user", "content": "next"}]  # a call answered once, then a question
    assert chat_module._whole_tool_exchanges(partly) == [turn[0], partly[-1]]


def test_an_id_less_call_whose_results_were_cut_is_dropped_with_what_remains():
    """As above for a model that sends no ids: its calls are stored with empty ones and its results' messages carry
    none, so there are no ids to match and the count alone says a result is missing."""
    question = {"role": "user", "content": "q"}
    call = {"role": "assistant", "content": "", "tool_calls": _calls("", "")}
    first = {"role": "tool", "content": "{}"}
    after = {"role": "user", "content": "next"}
    assert chat_module._whole_tool_exchanges([question, call, first]) == [question]  # cut after the first result
    assert chat_module._whole_tool_exchanges([question, call, first, after]) == [question, after]
    whole = [question, call, first, {"role": "tool", "content": "{}"}, after]
    assert chat_module._whole_tool_exchanges(whole) == whole


def test_a_result_answering_another_call_is_dropped_with_the_call():
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": _calls("call_x")},
        {"role": "tool", "content": "{}", "tool_call_id": "call_y"},
        {"role": "assistant", "content": "a"},
    ]
    assert chat_module._whole_tool_exchanges(history) == [history[0], history[3]]


def test_whole_exchanges_pass_unchanged():
    history = [*_turn_rows("t1"), *_parallel_turn("p"), *_turn_rows("t2"), {"role": "user", "content": "q"}]
    assert chat_module._whole_tool_exchanges(history) == history


def test_a_call_stored_without_an_id_keeps_its_result():
    """A model that sent no id is stored with an empty one, and its result's message carries none (``chat_stream``
    sets ``tool_call_id`` only when the row's is non-empty): the exchange is whole, not a mismatch."""
    call = _calls("")
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": call},
        {"role": "tool", "content": "{}"},
        {"role": "assistant", "content": "a"},
    ]
    assert chat_module._whole_tool_exchanges(history) == history


# ---------------------------------------------------------------------------
# _trim_to_budget keeps parallel calls whole too
# ---------------------------------------------------------------------------


_TRIM_BUDGETS = range(2_000, 20_001, 500)


def _with_filler(rows: list[dict], chars: int) -> list[dict]:
    return [
        {**r, "content": r["content"] + " " + "x" * chars} if r["role"] in ("tool", "user") else dict(r) for r in rows
    ]


@pytest.mark.parametrize("context_tokens", _TRIM_BUDGETS)
def test_trim_leaves_whole_exchanges_at_every_budget(context_tokens):
    """Popped from the front, a call goes with its results, a parallel call with both. Over budgets that cut at every
    position the trimmed history leads on a user or assistant message and holds whole exchanges."""
    history = _with_filler([*_parallel_turn("p1"), *_turn_rows("t1"), *_parallel_turn("p2"), *_turn_rows("t2")], 1_200)
    messages = [{"role": "system", "content": "s"}, *history, {"role": "user", "content": "the question"}]
    trimmed = chat_module._trim_to_budget(messages, context_tokens, None, 2.0, chat_module._PROSE_RATIO_DEFAULT)
    assert trimmed[0]["role"] == "system" and trimmed[-1]["content"] == "the question"
    _assert_leads_on_user_or_assistant(trimmed[1:])
    _assert_pairs_intact(trimmed[1:])
    assert trimmed[1:] == messages[len(messages) - len(trimmed) + 1 :], "the trim kept something other than the tail"


def test_trim_budgets_cut_at_many_positions():
    """The parametrized budgets above leave many different lengths, not all or nothing."""
    history = _with_filler([*_parallel_turn("p1"), *_turn_rows("t1"), *_parallel_turn("p2"), *_turn_rows("t2")], 1_200)
    messages = [{"role": "system", "content": "s"}, *history, {"role": "user", "content": "q"}]
    lengths = {
        len(chat_module._trim_to_budget(messages, c, None, 2.0, chat_module._PROSE_RATIO_DEFAULT))
        for c in _TRIM_BUDGETS
    }
    assert len(lengths) >= 5 and len(messages) in lengths and 2 in lengths, sorted(lengths)


# ---------------------------------------------------------------------------
# chat_stream: the window
# ---------------------------------------------------------------------------


async def _seed_rows(factory, user_id, chunks: list[list[dict]]) -> uuid.UUID:
    """A conversation of ``chunks``, each written in one transaction as ``chat_stream`` writes a turn."""
    conv_id = await _conversation(factory, user_id)
    for rows in chunks:
        await _write_turn(factory, conv_id, rows)
    return conv_id


async def _stored(factory, conv_id) -> list[ChatMessage]:
    async with factory() as session:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.created_at, ChatMessage.seq)
        )
        return list(result.scalars().all())


def _row_shape(rows) -> list[tuple]:
    return [(r.role, r.content if r.role != "tool" else r.tool_call_id) for r in rows]


# 40 rows before the question: the tail of a parallel-call turn (its call, both results, its answer), then six turns.
# With the question, 41 rows; the default window of 40 starts on the first result, its call the one row outside.
_FORTY_ROWS = [_parallel_turn("t0")[1:], *[_turn_rows(f"t{n}") for n in range(1, 7)]]


async def test_a_window_starting_on_a_tool_result_sends_no_orphan(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_history_messages", 40)
    user_id = await _committed_user(db_session, admin_user)
    conv_id = await _seed_rows(seq_factory, user_id, _FORTY_ROWS)
    assert sum(len(c) for c in _FORTY_ROWS) == 40

    # A context the history fits whole: the trim returns early, the path that sent the orphan.
    prompt = await _first_prompt(conv_id, user_id, "question t7", monkeypatch, context_tokens=200_000)

    stored = await _stored(seq_factory, conv_id)
    read = stored[:41]  # what the turn read: the 40 rows and its question (its answer came after)
    assert len(read) == 41 and read[-1].content == "question t7"
    window = read[-40:]
    assert window[0].role == "tool" and read[0].tool_calls, "precondition: the window starts on a result, call cut"
    _assert_leads_on_user_or_assistant(prompt)
    _assert_pairs_intact(prompt)
    # Both results of the cut call are dropped, nothing else is.
    assert _shape(prompt) == _row_shape(window[2:])


@pytest.mark.parametrize("window_size", range(30, 42))
async def test_every_window_landing_holds_whole_exchanges(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch, window_size
):
    """The window lands on a call, between a parallel call's results, on an answer, on a question: whichever, the
    prompt leads on a user or assistant message and every result follows its call. What it keeps is the window less
    any results at its head."""
    monkeypatch.setattr(get_settings(), "max_history_messages", window_size)
    user_id = await _committed_user(db_session, admin_user)
    chunks = [_parallel_turn("p0"), _turn_rows("t1"), _parallel_turn("p2"), _turn_rows("t3"), _parallel_turn("p4")]
    chunks += [_turn_rows("t5"), _parallel_turn("p6"), _turn_rows("t7")]
    conv_id = await _seed_rows(seq_factory, user_id, chunks)

    prompt = await _first_prompt(conv_id, user_id, "the last question", monkeypatch, context_tokens=200_000)

    read = (await _stored(seq_factory, conv_id))[:-1]  # less the answer to this turn
    window = read[-window_size:]
    head = 0
    while window[head].role == "tool":
        head += 1
    _assert_leads_on_user_or_assistant(prompt)
    _assert_pairs_intact(prompt)
    assert _shape(prompt) == _row_shape(window[head:])


def test_the_window_landings_cover_both_results_of_a_parallel_call():
    """The parametrized windows above start on a parallel call's first result and on its second, and on rows of
    every other kind: the scenario exercises what it claims."""
    chunks = [_parallel_turn("p0"), _turn_rows("t1"), _parallel_turn("p2"), _turn_rows("t3"), _parallel_turn("p4")]
    chunks += [_turn_rows("t5"), _parallel_turn("p6"), _turn_rows("t7")]
    rows = [r for c in chunks for r in c] + [{"role": "user", "content": "the last question"}]
    starts = [rows[-w] for w in range(30, 42)]
    firsts = {r["tool_call_id"][-1] for r in starts if r["role"] == "tool" and r["tool_call_id"].startswith("call_p")}
    assert firsts == {"1", "2"}, firsts
    assert any(r.get("tool_calls") for r in starts)
    assert any(r["role"] == "user" for r in starts)
    assert any(r["role"] == "assistant" and not r.get("tool_calls") for r in starts)


# ---------------------------------------------------------------------------
# chat_stream against a server whose template refuses an orphan result
# ---------------------------------------------------------------------------

# llama-server v0.4.1's answer to a prompt gpt-oss's template refuses, as the pin returned it for an orphan result
# (the GGUF's own template, rendered by the server; probed 2026-10-18).
_ORPHAN_REFUSAL = json.dumps(
    {
        "error": {
            "code": 500,
            "message": "\n------------\nWhile executing CallExpression at line 328, column 32 in source:\n... none %}"
            '\n            {{- raise_exception("Message has tool role, but there was n...\n'
            "Error: Jinja Exception: Message has tool role, but there was no previous assistant message with a tool "
            "call!",
            "type": "server_error",
        }
    }
)


def _template_refuses(messages: list[dict]) -> bool:
    """Whether a strict template refuses ``messages``: gpt-oss's rule (a tool message needs an assistant tool call
    before it, a plain assistant message ending the call), and a result's id must name a call of that message."""
    open_ids: set[str] | None = None
    for m in messages:
        if m["role"] == "assistant":
            open_ids = {tc.get("id") for tc in m["tool_calls"]} if m.get("tool_calls") else None
        elif m["role"] == "tool" and (open_ids is None or m.get("tool_call_id") not in open_ids):
            return True
    return False


def _strict_client(sent: list[list[dict]]):
    """``_answer_client``, except that a prompt the template refuses gets llama-server's 500."""
    accepting = _answer_client([])

    def build_request(method, url, json=None, **_kw):
        sent.append(copy.deepcopy(json["messages"]))
        return {"messages": json["messages"]}

    async def send(request, stream=False):
        if _template_refuses(request["messages"]):
            resp = MagicMock()
            resp.status_code = 500
            resp.aread = AsyncMock()
            resp.text = _ORPHAN_REFUSAL
            resp.aclose = AsyncMock()
            return resp
        return await accepting.send(request, stream=stream)

    client = MagicMock()
    client.build_request = MagicMock(side_effect=build_request)
    client.send = AsyncMock(side_effect=send)
    client.aclose = AsyncMock()
    return client


def test_the_strict_server_refuses_an_orphan_and_accepts_whole_exchanges():
    system = {"role": "system", "content": "s"}
    assert _template_refuses([system, *_turn_rows("t")[2:], {"role": "user", "content": "q"}])
    assert _template_refuses([system, *_parallel_turn("p")[3:]])
    assert not _template_refuses([system, *_turn_rows("t"), *_parallel_turn("p"), {"role": "user", "content": "q"}])


async def test_chat_stream_answers_where_a_strict_template_refuses_an_orphan(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_history_messages", 40)
    monkeypatch.setattr(chat_module, "context_budget", lambda model, yarn: 200_000)
    user_id = await _committed_user(db_session, admin_user)
    conv_id = await _seed_rows(seq_factory, user_id, _FORTY_ROWS)

    sent: list[list[dict]] = []
    events = []
    with patch("httpx.AsyncClient", return_value=_strict_client(sent)):
        async for line in chat_module.chat_stream(conv_id, "question t7", user_id):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))

    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, errors
    assert len(sent) == 1 and not _template_refuses(sent[0]), "the prompt was refused and retried"
    stored = await _stored(seq_factory, conv_id)
    assert stored[-1].role == "assistant" and stored[-1].content == "The answer."


async def test_a_window_whose_only_result_was_the_orphan_is_read_as_tool_less(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch
):
    """The orphan is dropped before the history's tool rows are looked for: a window holding no other result is the
    tool-less prompt it is sent as, and the conversation's own prose ratio (#810) is kept for it, not forgotten."""
    monkeypatch.setattr(get_settings(), "max_history_messages", 11)
    user_id = await _committed_user(db_session, admin_user)
    plain = [[{"role": "user", "content": f"q{n}"}, {"role": "assistant", "content": f"a{n}"}] for n in range(4)]
    one_call = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": _calls("call_only")},
        {"role": "tool", "content": "{}", "tool_call_id": "call_only"},
        {"role": "assistant", "content": "a"},
    ]
    # 3 + 8 rows and the question: the window of 11 starts on the result, its call the row before.
    conv_id = await _seed_rows(seq_factory, user_id, [one_call[1:], *plain])
    monkeypatch.setattr(chat_module, "_conversation_prose_ratios", OrderedDict())
    chat_module._conversation_prose_ratios[(str(conv_id), "")] = 1.7

    prompt = await _first_prompt(conv_id, user_id, "the question", monkeypatch, context_tokens=200_000)

    assert not any(m["role"] == "tool" for m in prompt)
    assert chat_module._conversation_prose_ratios.get((str(conv_id), "")) == 1.7
