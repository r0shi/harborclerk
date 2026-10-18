"""Chat and research messages are read in insertion order (#809).

``chat_messages.created_at`` is ``now()``, the start of the transaction, and ``chat_stream`` writes a whole turn in one
transaction, so a turn's rows tie on it. Every reader orders by ``(created_at, seq)``, ``seq`` being an identity the
database hands out as each row is inserted.

Each scenario writes turns of six rows (the question, a tool call and its result, a second call and its result, the
answer) in one transaction each, then rewrites them in reverse order (``UPDATE ... SET content = content``), which puts
their new tuple versions in the heap in that reverse order. The queries run on an engine with index scans off, so a
reader ordering by ``created_at`` alone reads the ties in heap order. Each test first checks that ``ORDER BY
created_at`` alone does return them out of order (``_assert_ties_scrambled``): a test cannot pass by heap-order luck,
because if the heap happened to hold the rows in insertion order, it fails instead.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from harbor_clerk.config import get_settings
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState
from tests.conftest import auth_header
from tests.test_chat_time_budget import _FIXED_SYSTEM_PROMPT, _fixed_chat_tools

# Index scans off: the readers' ordering, not an index that happens to hold seq, decides the order of ties.
_SEQ_SCAN_SETTINGS = {"enable_indexscan": "off", "enable_indexonlyscan": "off", "enable_bitmapscan": "off"}


@pytest.fixture
async def seq_engine(db_session):
    """An engine whose sessions read tables by sequential scan. Depends on ``db_session`` for its cleanup."""
    engine = create_async_engine(
        os.environ["DATABASE_URL"], poolclass=NullPool, connect_args={"server_settings": _SEQ_SCAN_SETTINGS}
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def seq_factory(seq_engine):
    return async_sessionmaker(seq_engine, class_=AsyncSession, expire_on_commit=False)


def _call(call_id: str, query: str) -> list[dict]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": "search_documents", "arguments": json.dumps({"query": query})},
        }
    ]


def _turn_rows(tag: str, result_chars: int = 200) -> list[dict]:
    """One turn as ``chat_stream`` stores it: question, call, result, call, result, answer."""
    filler = "x" * result_chars
    return [
        {"role": "user", "content": f"question {tag}"},
        {"role": "assistant", "content": "", "tool_calls": _call(f"call_{tag}_a", f"a {tag}")},
        {
            "role": "tool",
            "content": json.dumps({"results": [f"result {tag} a {filler}"]}),
            "tool_call_id": f"call_{tag}_a",
        },
        {"role": "assistant", "content": "", "tool_calls": _call(f"call_{tag}_b", f"b {tag}")},
        {
            "role": "tool",
            "content": json.dumps({"results": [f"result {tag} b {filler}"]}),
            "tool_call_id": f"call_{tag}_b",
        },
        {"role": "assistant", "content": f"answer {tag}"},
    ]


async def _write_turn(factory, conversation_id: uuid.UUID, rows: list[dict]) -> list[uuid.UUID]:
    """``rows`` added in order and committed in one transaction (so they share ``created_at``), then rewritten in
    reverse order in a second one. Returns their ids in insertion order."""
    async with factory() as session:
        msgs = [ChatMessage(conversation_id=conversation_id, **row) for row in rows]
        session.add_all(msgs)
        await session.commit()
        ids = [m.message_id for m in msgs]
    async with factory() as session:
        for message_id in reversed(ids):
            await session.execute(
                update(ChatMessage).where(ChatMessage.message_id == message_id).values(content=ChatMessage.content)
            )
        await session.commit()
    return ids


async def _assert_ties_scrambled(factory, conversation_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    """The scenario's precondition: one turn's rows share ``created_at``, and ``ORDER BY created_at`` alone does not
    read them in insertion order. Without it, a reader that lost its tie-break could still pass."""
    async with factory() as session:
        stamps = (
            (await session.execute(select(ChatMessage.created_at).where(ChatMessage.message_id.in_(ids))))
            .scalars()
            .all()
        )
        plain = (
            (
                await session.execute(
                    select(ChatMessage.message_id)
                    .where(ChatMessage.message_id.in_(ids))
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(set(stamps)) == 1, "the turn's rows were meant to share one created_at"
    assert list(plain) != ids, (
        "the heap holds the turn in insertion order: the scenario would not catch a lost tie-break"
    )


async def _conversation(factory, user_id, *, mode: str = "chat") -> uuid.UUID:
    async with factory() as session:
        conv = Conversation(user_id=user_id, title="Ordered chat", mode=mode)
        session.add(conv)
        await session.commit()
        return conv.conversation_id


async def _seed(factory, user_id, tags: list[str], **turn_kwargs) -> tuple[uuid.UUID, list[list[uuid.UUID]]]:
    """A conversation of one tied, heap-scrambled turn per tag."""
    conv_id = await _conversation(factory, user_id)
    turns = []
    for tag in tags:
        ids = await _write_turn(factory, conv_id, _turn_rows(tag, **turn_kwargs))
        await _assert_ties_scrambled(factory, conv_id, ids)
        turns.append(ids)
    return conv_id, turns


async def _committed_user(db_session, admin_user):
    await db_session.commit()
    return admin_user.user_id


# ---------------------------------------------------------------------------
# chat_stream: the history the model is sent
# ---------------------------------------------------------------------------


def _answer_client(sent: list[list[dict]]):
    """An httpx.AsyncClient that answers every prompt with plain text and records each prompt's messages."""

    def build_request(method, url, json=None, **_kw):
        sent.append(copy.deepcopy(json["messages"]))
        return MagicMock()

    async def send(_request, stream=False):
        resp = MagicMock()
        resp.status_code = 200

        async def aiter_lines():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "The answer."}}]})
            yield "data: [DONE]"

        resp.aiter_lines = aiter_lines
        resp.aclose = AsyncMock()
        resp.aread = AsyncMock()
        return resp

    client = MagicMock()
    client.build_request = MagicMock(side_effect=build_request)
    client.send = AsyncMock(side_effect=send)
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def chat_on_seq_engine(seq_factory, monkeypatch):
    """chat_stream's sessions on the sequential-scan engine; no model, no corpus topics, fresh ratio tables."""
    from harbor_clerk.llm import chat as chat_module

    @asynccontextmanager
    async def _factory():
        async with seq_factory() as session:
            yield session

    async def _no_topics():
        return None

    # The fixed tool schema and system prompt of the chat tests (#826): the trim budgets below measure the history,
    # not the length of the production descriptions.
    monkeypatch.setattr(chat_module, "get_chat_tools", _fixed_chat_tools)
    monkeypatch.setattr(chat_module, "SYSTEM_PROMPT", _FIXED_SYSTEM_PROMPT)
    monkeypatch.setattr("harbor_clerk.llm.chat.async_session_factory", _factory)
    monkeypatch.setattr("harbor_clerk.topics.async_session_factory", _factory)
    monkeypatch.setattr("harbor_clerk.topics.get_topic_summary", _no_topics)
    monkeypatch.setattr(chat_module, "_estimate_ratios", {})
    monkeypatch.setattr(chat_module, "_prose_ratios", {})
    monkeypatch.setattr(chat_module, "refresh_llm_settings", lambda: None)
    monkeypatch.setattr(get_settings(), "llm_model_id", "")
    monkeypatch.setattr(get_settings(), "chat_time_budget_seconds", 0)
    monkeypatch.setattr(get_settings(), "max_history_messages", 100)


async def _first_prompt(conv_id, user_id, question: str, monkeypatch, context_tokens: int) -> list[dict]:
    """The messages of the first prompt ``chat_stream`` sends for ``question``, the system prompt dropped."""
    from harbor_clerk.llm import chat as chat_module

    monkeypatch.setattr(chat_module, "context_budget", lambda model, yarn: context_tokens)
    sent: list[list[dict]] = []
    with patch("httpx.AsyncClient", return_value=_answer_client(sent)):
        async for _ in chat_module.chat_stream(conv_id, question, user_id):
            pass
    assert sent, "chat_stream sent no prompt"
    assert sent[0][0]["role"] == "system"
    return sent[0][1:]


def _expected_messages(tags: list[str]) -> list[tuple]:
    return [
        (row["role"], row["content"] if row["role"] != "tool" else row["tool_call_id"])
        for tag in tags
        for row in _turn_rows(tag)
    ]


def _shape(messages: list[dict]) -> list[tuple]:
    return [(m["role"], m["content"] if m["role"] != "tool" else m["tool_call_id"]) for m in messages]


async def test_chat_stream_sends_the_history_in_insertion_order(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch
):
    user_id = await _committed_user(db_session, admin_user)
    tags = ["t1", "t2", "t3"]
    conv_id, _ = await _seed(seq_factory, user_id, tags)

    prompt = await _first_prompt(conv_id, user_id, "question t4", monkeypatch, context_tokens=200_000)

    assert _shape(prompt) == [*_expected_messages(tags), ("user", "question t4")]


def _assert_pairs_intact(messages: list[dict]) -> None:
    """Every tool result directly follows its call (or a sibling result of that call), and every call is followed by
    a result for each of its ids."""
    open_ids: set[str] = set()
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, f"tool result {m['tool_call_id']} at {i} without its call before it"
            open_ids.discard(m["tool_call_id"])
            continue
        assert not open_ids, f"call(s) {sorted(open_ids)} not answered before message {i} ({m['role']})"
        if m["role"] == "assistant" and m.get("tool_calls"):
            open_ids = {tc["id"] for tc in m["tool_calls"]}
    assert not open_ids, f"call(s) {sorted(open_ids)} not answered at the end"


@pytest.mark.parametrize("context_tokens", [3_000, 7_000, 8_000, 10_000, 11_000, 12_000, 14_000, 20_000])
async def test_trim_never_splits_a_call_from_its_result_on_a_tied_history(
    db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch, context_tokens
):
    """``_trim_to_budget`` drops history from the front, a call together with the results after it; on a history read
    out of order (an answer or a result before its call) it dropped a call and kept its result, or the reverse. Over a
    range of budgets the first prompt holds whole pairs only, and what it keeps is the tail of the stored order."""
    user_id = await _committed_user(db_session, admin_user)
    tags = ["t1", "t2", "t3"]
    conv_id, _ = await _seed(seq_factory, user_id, tags, result_chars=1_500)

    prompt = await _first_prompt(conv_id, user_id, "question t4", monkeypatch, context_tokens=context_tokens)

    _assert_pairs_intact(prompt)
    full = [*_expected_messages(tags), ("user", "question t4")]
    shape = _shape(prompt)
    assert shape == full[len(full) - len(shape) :], "the trimmed prompt is not the tail of the stored history"


async def test_trim_budgets_cover_partial_trims(db_session, admin_user, seq_factory, chat_on_seq_engine, monkeypatch):
    """The parametrized budgets above trim to several different lengths, not all or nothing."""
    user_id = await _committed_user(db_session, admin_user)
    lengths = set()
    for context_tokens in (3_000, 10_000, 20_000):
        conv_id, _ = await _seed(seq_factory, user_id, ["t1", "t2", "t3"], result_chars=1_500)
        lengths.add(len(await _first_prompt(conv_id, user_id, "q", monkeypatch, context_tokens=context_tokens)))
    full = 3 * 6 + 1
    assert len(lengths) == 3 and max(lengths) == full and min(lengths) < full, lengths


# ---------------------------------------------------------------------------
# The REST readers
# ---------------------------------------------------------------------------


@pytest.fixture
async def seq_client(seq_factory):
    """ASGI client whose request sessions come from the sequential-scan engine."""
    from httpx import ASGITransport, AsyncClient

    from harbor_clerk.api.app import create_app
    from harbor_clerk.db import get_session

    app = create_app()

    async def _session():
        async with seq_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_get_conversation_returns_messages_in_insertion_order(
    db_session, admin_user, admin_token, seq_factory, seq_client
):
    user_id = await _committed_user(db_session, admin_user)
    conv_id, turns = await _seed(seq_factory, user_id, ["t1", "t2"])

    resp = await seq_client.get(f"/api/chat/conversations/{conv_id}", headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    assert [m["message_id"] for m in resp.json()["messages"]] == [str(i) for t in turns for i in t]


async def test_export_writes_the_transcript_in_insertion_order(
    db_session, admin_user, admin_token, seq_factory, seq_client
):
    user_id = await _committed_user(db_session, admin_user)
    conv_id, _ = await _seed(seq_factory, user_id, ["t1", "t2"])

    resp = await seq_client.get(f"/api/chat/conversations/{conv_id}/export", headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    body = resp.text
    positions = [body.index(s) for s in ("question t1", "answer t1", "question t2", "answer t2")]
    assert positions == sorted(positions), body


async def _research(seq_factory, user_id) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A completed research conversation whose question, draft and report tie on created_at."""
    conv_id = await _conversation(seq_factory, user_id, mode="research")
    async with seq_factory() as session:
        session.add(ResearchState(conversation_id=conv_id, strategy="search", status="completed", max_rounds=2))
        await session.commit()
    ids = await _write_turn(
        seq_factory,
        conv_id,
        [
            {"role": "user", "content": "the research question"},
            {"role": "user", "content": "a later follow-up"},
            {"role": "assistant", "content": "the draft", "model_id": "draft-model"},
            {"role": "assistant", "content": "the report", "model_id": "report-model"},
        ],
    )
    await _assert_ties_scrambled(seq_factory, conv_id, ids)
    return conv_id, ids


async def test_research_detail_reads_question_report_and_messages_in_insertion_order(
    db_session, admin_user, admin_token, seq_factory, seq_client
):
    user_id = await _committed_user(db_session, admin_user)
    conv_id, ids = await _research(seq_factory, user_id)

    resp = await seq_client.get(f"/api/research/{conv_id}", headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["message_id"] for m in body["messages"]] == [str(i) for i in ids]
    assert body["question"] == "the research question"
    assert body["report"] == "the report" and body["model_id"] == "report-model"


async def test_research_engine_reads_the_first_question(db_session, admin_user, seq_factory):
    from harbor_clerk.llm.research import _first_user_question

    user_id = await _committed_user(db_session, admin_user)
    conv_id, _ = await _research(seq_factory, user_id)

    async with seq_factory() as session:
        assert await _first_user_question(session, conv_id) == "the research question"


# ---------------------------------------------------------------------------
# New rows, and the sweep
# ---------------------------------------------------------------------------


async def test_seq_increases_strictly_in_insertion_order(db_session, admin_user, seq_factory):
    """One flush of many rows (the ORM batches them into one INSERT) numbers them in the order they were added."""
    user_id = await _committed_user(db_session, admin_user)
    conv_id = await _conversation(seq_factory, user_id)
    async with seq_factory() as session:
        msgs = [ChatMessage(conversation_id=conv_id, role="tool", content=str(n), tool_call_id="c") for n in range(50)]
        session.add_all(msgs)
        await session.commit()
        seqs = [m.seq for m in msgs]
    assert all(a < b for a, b in zip(seqs, seqs[1:], strict=False)), seqs


async def test_seq_cannot_be_written(db_session, admin_user, seq_factory):
    """GENERATED ALWAYS: no writer supplies a value out of order."""
    import sqlalchemy.exc

    user_id = await _committed_user(db_session, admin_user)
    conv_id = await _conversation(seq_factory, user_id)
    async with seq_factory() as session:
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="seq"):
            await session.execute(
                text("INSERT INTO chat_messages (conversation_id, role, seq) VALUES (:c, 'user', 1)"),
                {"c": conv_id},
            )


_SRC = Path(__file__).resolve().parents[1] / "src" / "harbor_clerk"


def _chat_message_orderings() -> list[tuple[str, int, list[str]]]:
    """(file, line, the ChatMessage columns it orders by) for every ``order_by(...)`` call in src naming a
    ChatMessage column. Read from the AST, so comments and strings never match."""
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_by"
            ):
                continue
            columns = [
                sub.attr
                for arg in node.args
                for sub in ast.walk(arg)
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "ChatMessage"
            ]
            if columns:
                found.append((str(path.relative_to(_SRC)), node.lineno, columns))
    return found


def test_every_chat_message_ordering_breaks_created_at_ties_on_seq():
    """A new reader of chat_messages must not order by created_at alone: a turn's rows tie on it."""
    orderings = _chat_message_orderings()
    assert len(orderings) >= 5, orderings  # the five readers of #809: the guard sees them
    wrong = [o for o in orderings if "created_at" in o[2] and "seq" not in o[2]]
    assert not wrong, f"ordered by created_at without seq: {wrong}"


# ORDER BY created_at (qualified or not, ASC/DESC) not followed by a seq tie-break.
_SQL_ORDER_BY_CREATED_AT = re.compile(
    r"order\s+by\s+(?:\w+\.)?created_at\b(?!\s*(?:asc|desc)?\s*,\s*(?:\w+\.)?seq\b)", re.IGNORECASE
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _other_created_at_orderings(source: str) -> list[tuple[int, str]]:
    """(line, what) for each raw-SQL ``ORDER BY created_at`` without seq in a string naming chat_messages, and each
    ``sorted(...)`` / ``.sort(...)`` keyed on ``.created_at`` without ``.seq`` where the call or its file is about
    messages. From the AST: comments never match, and docstrings are skipped."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    about_messages = "ChatMessage" in source or "chat_messages" in source
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and "chat_messages" in node.value.lower()
            and _SQL_ORDER_BY_CREATED_AT.search(node.value)
        ):
            found.append((node.lineno, "raw SQL ORDER BY created_at"))
        if not isinstance(node, ast.Call):
            continue
        is_sort = (isinstance(node.func, ast.Name) and node.func.id == "sorted") or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "sort"
        )
        key = next((kw.value for kw in node.keywords if kw.arg == "key"), None)
        if not is_sort or key is None:
            continue
        attrs = {sub.attr for sub in ast.walk(key) if isinstance(sub, ast.Attribute)}
        call_text = (ast.get_source_segment(source, node) or "").lower()
        if (
            "created_at" in attrs
            and "seq" not in attrs
            and (about_messages or "msg" in call_text or "message" in call_text)
        ):
            found.append((node.lineno, "sort keyed on created_at"))
    return sorted(found)


def test_no_raw_sql_or_python_sort_orders_messages_by_created_at_alone():
    found = [
        (str(path.relative_to(_SRC)), line, what)
        for path in sorted(_SRC.rglob("*.py"))
        for line, what in _other_created_at_orderings(path.read_text())
    ]
    assert not found, f"ordered by created_at without seq: {found}"


def test_the_raw_sql_and_sort_scan_catches_what_it_should():
    """The scan above finds nothing in src today, so it is checked here on samples."""
    flagged = """
q1 = text("SELECT * FROM chat_messages WHERE conversation_id = :c ORDER BY created_at")
q2 = "SELECT content FROM public.chat_messages m " "ORDER BY m.created_at DESC LIMIT 1"
rows = sorted(msgs, key=lambda m: m.created_at)
history.sort(key=lambda message: message.created_at)
"""
    assert [what for _, what in _other_created_at_orderings(flagged)] == [
        "raw SQL ORDER BY created_at",
        "raw SQL ORDER BY created_at",
        "sort keyed on created_at",
        "sort keyed on created_at",
    ]
    clean = '''
"""A docstring may say: SELECT * FROM chat_messages ORDER BY created_at."""
# A comment too: SELECT * FROM chat_messages ORDER BY created_at
q1 = text("SELECT * FROM chat_messages ORDER BY created_at, seq")
q2 = text("SELECT * FROM chat_messages m ORDER BY m.created_at ASC, m.seq")
q3 = text("SELECT * FROM ingestion_jobs ORDER BY created_at")
rows = sorted(msgs, key=lambda m: (m.created_at, m.seq))
'''
    assert _other_created_at_orderings(clean) == []
    # A file that is not about messages may sort other rows by created_at.
    assert _other_created_at_orderings("jobs = sorted(jobs, key=lambda j: j.created_at)\n") == []
