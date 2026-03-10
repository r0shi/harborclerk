# Research Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a Research tab where users submit a question and the LLM autonomously iterates through the corpus, accumulating findings and producing a monolithic report with citations.

**Architecture:** Reuses the existing conversation/message infrastructure with a new `mode` column. A `research_stream()` async generator (modeled on `chat_stream()`) runs the iteration loop, checkpointing notes to a `research_state` table. A fresh-context synthesis pass produces the final report. Two strategies (search-driven and systematic sweep) with per-model defaults stored in a `model_settings` table.

**Tech Stack:** FastAPI SSE streaming, SQLAlchemy async, Alembic, React + TypeScript, Tailwind CSS 4

**Design doc:** `docs/plans/2026-03-09-research-mode-design.md`

---

## Task 1: Database Migration

Add `mode` column to `conversations`, create `research_state` and `model_settings` tables.

**Files:**
- Create: `alembic/versions/0005_research_mode.py`
- Modify: `src/harbor_clerk/models/conversation.py`
- Create: `src/harbor_clerk/models/research_state.py`
- Create: `src/harbor_clerk/models/model_settings.py`
- Modify: `src/harbor_clerk/models/__init__.py`

**Step 1: Create the migration**

Create `alembic/versions/0005_research_mode.py`:

```python
"""Add research mode tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("mode", sa.String(10), nullable=False, server_default="chat"),
    )

    op.create_table(
        "research_state",
        sa.Column(
            "conversation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("strategy", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("current_round", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_rounds", sa.Integer, nullable=False),
        sa.Column("progress", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("error", sa.Text, nullable=True),
    )

    op.create_table(
        "model_settings",
        sa.Column("model_id", sa.String(50), primary_key=True),
        sa.Column(
            "settings",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_settings")
    op.drop_table("research_state")
    op.drop_column("conversations", "mode")
```

**Step 2: Add `mode` to the Conversation model**

In `src/harbor_clerk/models/conversation.py`, add after the `title` column:

```python
mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="chat")
```

Import `String` from sqlalchemy if not already imported.

**Step 3: Create ResearchState model**

Create `src/harbor_clerk/models/research_state.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class ResearchState(Base):
    __tablename__ = "research_state"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    progress: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**Step 4: Create ModelSettings model**

Create `src/harbor_clerk/models/model_settings.py`:

```python
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class ModelSettings(Base):
    __tablename__ = "model_settings"

    model_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    settings: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="{}")
```

**Step 5: Export new models**

In `src/harbor_clerk/models/__init__.py`, add imports:

```python
from harbor_clerk.models.model_settings import ModelSettings
from harbor_clerk.models.research_state import ResearchState
```

Add `"ModelSettings"` and `"ResearchState"` to the `__all__` list.

**Step 6: Run migration and verify**

```bash
cd /Users/alex/mcp-gateway
uv run alembic upgrade head
```

Expected: Migration applies cleanly.

**Step 7: Lint check**

```bash
uv run ruff check src/harbor_clerk/models/research_state.py src/harbor_clerk/models/model_settings.py src/harbor_clerk/models/conversation.py alembic/versions/0005_research_mode.py
uv run ruff format --check src/harbor_clerk/models/ alembic/versions/0005_research_mode.py
```

Expected: No errors.

**Step 8: Commit**

```bash
git add alembic/versions/0005_research_mode.py src/harbor_clerk/models/research_state.py src/harbor_clerk/models/model_settings.py src/harbor_clerk/models/conversation.py src/harbor_clerk/models/__init__.py
git commit -m "feat: add research_state and model_settings tables, mode column on conversations"
```

---

## Task 2: Per-Model Settings Helper

Add a helper to look up per-model settings with global fallback, and define default research strategies per model tier.

**Files:**
- Modify: `src/harbor_clerk/llm/models.py`

**Step 1: Add research tier defaults to models.py**

Add after the `MODELS` dict at the bottom of `src/harbor_clerk/llm/models.py`:

```python
# Default research settings per model tier.
# Per-model overrides stored in model_settings DB table.
_LARGE_MODEL_THRESHOLD_BYTES = 4_000_000_000  # ~4GB → 8B+ params


def default_research_strategy(model_id: str) -> str:
    """Return 'search' for large models, 'sweep' for small."""
    model = MODELS.get(model_id)
    if model is None:
        return "search"
    return "search" if model.size_bytes >= _LARGE_MODEL_THRESHOLD_BYTES else "sweep"


DEFAULT_RESEARCH_MAX_ROUNDS = 20
```

**Step 2: Add model_settings lookup helper**

Create `src/harbor_clerk/llm/model_settings.py`:

```python
"""Per-model settings with DB override and global fallback."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.model_settings import ModelSettings

logger = logging.getLogger(__name__)


async def get_model_setting(
    session: AsyncSession,
    model_id: str,
    key: str,
    default: int | str | bool | None = None,
) -> int | str | bool | None:
    """Look up a per-model setting, falling back to default."""
    result = await session.execute(
        select(ModelSettings.settings).where(ModelSettings.model_id == model_id)
    )
    row = result.scalar_one_or_none()
    if row is not None and key in row:
        return row[key]
    return default
```

**Step 3: Lint check**

```bash
uv run ruff check src/harbor_clerk/llm/models.py src/harbor_clerk/llm/model_settings.py
uv run ruff format --check src/harbor_clerk/llm/models.py src/harbor_clerk/llm/model_settings.py
```

**Step 4: Commit**

```bash
git add src/harbor_clerk/llm/models.py src/harbor_clerk/llm/model_settings.py
git commit -m "feat: per-model settings helper and research tier defaults"
```

---

## Task 3: Research Schemas

Add Pydantic request/response schemas for the research API.

**Files:**
- Create: `src/harbor_clerk/api/schemas/research.py`

**Step 1: Create the schemas file**

```python
"""Pydantic schemas for research mode API."""

from datetime import datetime

from pydantic import BaseModel, Field


class StartResearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=10000)
    strategy: str | None = Field(
        default=None, pattern="^(search|sweep)$", description="Override default strategy"
    )


class ResearchProgress(BaseModel):
    """Progress snapshot from research_state."""

    conversation_id: str
    question: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    progress: dict | None = None
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class ResearchSummary(BaseModel):
    """List item for research history."""

    conversation_id: str
    title: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    created_at: datetime
    completed_at: datetime | None = None


class ResearchDetail(BaseModel):
    """Full research task with messages."""

    conversation_id: str
    title: str
    question: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    progress: dict | None = None
    report: str | None = None
    model_id: str | None = None
    messages: list[dict]
    created_at: datetime
    completed_at: datetime | None = None


class ResearchActiveCheck(BaseModel):
    active: bool
    research_id: str | None = None
```

**Step 2: Lint check**

```bash
uv run ruff check src/harbor_clerk/api/schemas/research.py
uv run ruff format --check src/harbor_clerk/api/schemas/research.py
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/api/schemas/research.py
git commit -m "feat: research mode API schemas"
```

---

## Task 4: Research Engine — Core Loop

The main research_stream() async generator. This is the largest task.

**Files:**
- Create: `src/harbor_clerk/llm/research.py`

**Step 1: Create the research engine**

Create `src/harbor_clerk/llm/research.py`. This file contains:

1. System prompt constants (iteration + synthesis, for both strategies)
2. `_parse_notes(response: str) -> str | None` — extracts `<notes>...</notes>` from model response
3. `_detect_report_signal(response: str) -> bool` — checks for `<report>` tag
4. `_build_iteration_messages(...)` — constructs the message list for each iteration
5. `_build_synthesis_messages(...)` — constructs messages for the final report pass
6. `research_stream(conversation_id, user_id)` — the main async generator

```python
"""Research mode engine — autonomous corpus exploration with report synthesis.

Runs an iterative loop where the LLM searches the corpus, accumulates findings
in a scratchpad (<notes>), then does a fresh-context synthesis pass to produce
the final report. Two strategies:

  - search: model-driven iteration, decides what to search next (larger models)
  - sweep: system feeds document batches, model extracts findings (smaller models)

Progress is streamed via SSE. Notes are checkpointed to research_state after
each iteration for restart on interruption.
"""

import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.model_settings import get_model_setting
from harbor_clerk.llm.models import (
    DEFAULT_RESEARCH_MAX_ROUNDS,
    MODELS,
    default_research_strategy,
)
from harbor_clerk.llm.tools import CHAT_TOOLS, execute_tool
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_ITERATION_PROMPT_SEARCH = """\
You are a research assistant for Harbor Clerk. Your task is to systematically \
search the knowledge base to thoroughly answer the user's question.

## How to work
- Search broadly first, then drill into promising results
- Use different search queries to cover different angles of the topic
- Read passages to verify and gather detail from search hits
- Use entity_search to find people, organizations, and places related to the topic
- Use corpus_overview or list_documents to discover what's available

## Notes rules
- Maintain your accumulated findings in a <notes> section at the end of every response
- Every finding MUST include its source: document title, page number, and chunk ID \
in parentheses
- When condensing notes, you may rephrase findings but NEVER remove citations
- Citations are the most important part of your notes — the final report depends on them

## Finishing
When you are confident you have thoroughly covered the topic, stop calling tools \
and write ONLY a <report> tag (empty is fine). A separate synthesis step will \
produce the final report from your notes.
"""

_ITERATION_PROMPT_SWEEP_EXTRA = """
## Current batch
Focus on the following documents this round. Search within them (use doc_id \
parameter), read relevant passages, and add any findings to your notes. Not every \
document will be relevant — skip irrelevant ones quickly.

Documents for this round:
{batch_list}
"""

_SYNTHESIS_PROMPT = """\
You are writing a research report for Harbor Clerk. Based on the research notes \
below, write a clear, well-organized report answering the user's question.

## Guidelines
- Every claim must cite a source from the notes (document title, page number)
- If a finding has no citation, omit it
- Group findings by theme, not by document
- Be thorough but concise — include all relevant findings, skip filler
- If the evidence is contradictory or incomplete, say so
- Do not invent information not present in the notes
"""

_CHARS_PER_TOKEN = 3.5
_RESPONSE_RESERVE = 0.20

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_notes(text: str) -> str | None:
    """Extract the last <notes>...</notes> block from model output."""
    matches = list(re.finditer(r"<notes>(.*?)</notes>", text, re.DOTALL))
    if matches:
        return matches[-1].group(1).strip()
    return None


def _detect_report_signal(text: str) -> bool:
    """Check if model emitted a <report> tag, signalling it's done."""
    return "<report>" in text.lower()


def _estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + 4


def _build_iteration_messages(
    system_prompt: str,
    user_question: str,
    notes: str | None,
    sweep_batch: str | None = None,
) -> list[dict]:
    """Build message list for one iteration round."""
    prompt = system_prompt
    if sweep_batch:
        prompt += _ITERATION_PROMPT_SWEEP_EXTRA.format(batch_list=sweep_batch)

    messages = [{"role": "system", "content": prompt}]
    messages.append({"role": "user", "content": user_question})

    if notes:
        messages.append({
            "role": "assistant",
            "content": f"Here are my research notes so far:\n\n<notes>\n{notes}\n</notes>\n\n"
            "I'll continue researching.",
        })
        messages.append({
            "role": "user",
            "content": "Continue your research. Call tools to gather more evidence, "
            "then update your <notes> at the end of your response. "
            "If you have enough evidence, emit <report> instead.",
        })

    return messages


def _build_synthesis_messages(
    user_question: str,
    notes: str,
) -> list[dict]:
    """Build message list for the synthesis pass (no tools)."""
    return [
        {"role": "system", "content": _SYNTHESIS_PROMPT},
        {
            "role": "user",
            "content": f"## Original question\n{user_question}\n\n"
            f"## Research notes\n{notes}\n\n"
            "Write the report now.",
        },
    ]


async def _fetch_document_list(user_id: uuid.UUID | None) -> list[dict]:
    """Fetch corpus document list for sweep strategy."""
    result_str = await execute_tool("corpus_overview", {"limit": 500}, user_id)
    try:
        data = json.loads(result_str)
        return data.get("documents", [])
    except (json.JSONDecodeError, AttributeError):
        return []


# ---------------------------------------------------------------------------
# Main streaming generator
# ---------------------------------------------------------------------------


async def research_stream(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    resume: bool = False,
) -> AsyncGenerator[str, None]:
    """Run the research loop, yielding SSE events.

    Events:
      {"type": "progress", "round": N, "max_rounds": M, ...}
      {"type": "tool_call", "name": str, "arguments": dict}
      {"type": "tool_result", "name": str, "summary": str}
      {"type": "synthesis", "status": "started"}
      {"type": "token", "content": str}
      {"type": "done", "conversation_id": str, "model_id": str}
      {"type": "error", "message": str}
    """
    settings = get_settings()
    model = MODELS.get(settings.llm_model_id)
    if model is None:
        yield f"data: {json.dumps({'type': 'error', 'message': 'No model configured'})}\n\n"
        return

    context_tokens = model.context_window
    if settings.llm_yarn_enabled and model.yarn:
        context_tokens = model.yarn.extended_context

    async with async_session_factory() as session:
        # Load research state
        state = await session.get(ResearchState, conversation_id)
        if state is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Research task not found'})}\n\n"
            return

        # Load conversation for the user question
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Conversation not found'})}\n\n"
            return

        # Get user's original question (first user message)
        msg_result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.role == "user")
            .order_by(ChatMessage.created_at)
            .limit(1)
        )
        user_msg = msg_result.scalar_one_or_none()
        if user_msg is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No user question found'})}\n\n"
            return

        user_question = user_msg.content
        strategy = state.strategy
        notes = state.notes
        current_round = state.current_round
        max_rounds = state.max_rounds

        # Mark as running
        state.status = "running"
        await session.commit()

        # For sweep: get document list
        doc_list: list[dict] = []
        sweep_batch_size = 15
        if strategy == "sweep":
            doc_list = await _fetch_document_list(user_id)
            if not doc_list:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Could not fetch document list for sweep'})}\n\n"
                state.status = "failed"
                state.error = "Could not fetch document list"
                await session.commit()
                return

        system_prompt = _ITERATION_PROMPT_SEARCH

        # ----- Iteration loop -----
        try:
            for _round in range(current_round, max_rounds):
                # Build sweep batch if needed
                sweep_batch: str | None = None
                if strategy == "sweep":
                    batch_start = _round * sweep_batch_size
                    batch_end = min(batch_start + sweep_batch_size, len(doc_list))
                    if batch_start >= len(doc_list):
                        # Covered all documents
                        break
                    batch_docs = doc_list[batch_start:batch_end]
                    sweep_batch = "\n".join(
                        f"- {d.get('title', 'Untitled')} (doc_id: {d.get('doc_id', 'unknown')})"
                        for d in batch_docs
                    )

                messages = _build_iteration_messages(
                    system_prompt, user_question, notes, sweep_batch
                )

                # Yield progress event
                progress_data: dict = {
                    "type": "progress",
                    "round": _round + 1,
                    "max_rounds": max_rounds,
                    "strategy": strategy,
                }
                if strategy == "sweep" and doc_list:
                    reviewed = min((_round + 1) * sweep_batch_size, len(doc_list))
                    progress_data["reviewed"] = reviewed
                    progress_data["total"] = len(doc_list)
                yield f"data: {json.dumps(progress_data)}\n\n"

                # Call LLM with tools
                assistant_content = ""
                tool_calls_raw: list[dict] = []

                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(300.0, connect=10.0)
                    ) as client:
                        payload = {
                            "model": settings.llm_model_id,
                            "messages": messages,
                            "tools": CHAT_TOOLS,
                            "temperature": 0.3,
                            "stream": True,
                        }
                        async with client.stream(
                            "POST",
                            f"{settings.llama_server_url}/v1/chat/completions",
                            json=payload,
                        ) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                data = line[6:].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    continue

                                delta = chunk.get("choices", [{}])[0].get("delta", {})

                                # Accumulate text
                                if delta.get("content"):
                                    assistant_content += delta["content"]

                                # Accumulate tool calls
                                if delta.get("tool_calls"):
                                    for tc in delta["tool_calls"]:
                                        idx = tc.get("index", 0)
                                        while len(tool_calls_raw) <= idx:
                                            tool_calls_raw.append(
                                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                                            )
                                        if tc.get("id"):
                                            tool_calls_raw[idx]["id"] = tc["id"]
                                        func = tc.get("function", {})
                                        if func.get("name"):
                                            tool_calls_raw[idx]["function"]["name"] = func["name"]
                                        if func.get("arguments"):
                                            tool_calls_raw[idx]["function"]["arguments"] += func["arguments"]

                except httpx.HTTPStatusError as e:
                    error_msg = f"LLM error ({e.response.status_code})"
                    try:
                        error_msg += f": {e.response.text[:500]}"
                    except Exception:
                        pass
                    logger.error("Research LLM error round %d: %s", _round, error_msg)
                    state.status = "interrupted"
                    state.current_round = _round
                    state.progress = progress_data
                    await session.commit()
                    yield f"data: {json.dumps({'type': 'error', 'message': error_msg})}\n\n"
                    return
                except (httpx.ConnectError, httpx.ReadTimeout) as e:
                    logger.error("Research connection error round %d: %s", _round, e)
                    state.status = "interrupted"
                    state.current_round = _round
                    state.progress = progress_data
                    await session.commit()
                    yield f"data: {json.dumps({'type': 'error', 'message': f'LLM connection error: {e}'})}\n\n"
                    return

                # Execute any tool calls
                if tool_calls_raw:
                    # Save assistant message with tool calls
                    asst_msg = ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=assistant_content,
                        tool_calls=tool_calls_raw,
                        model_id=settings.llm_model_id,
                    )
                    session.add(asst_msg)
                    await session.flush()

                    # Add assistant message to context for tool results
                    messages.append({
                        "role": "assistant",
                        "content": assistant_content,
                        "tool_calls": tool_calls_raw,
                    })

                    for tc in tool_calls_raw:
                        fn_name = tc["function"]["name"]
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except (json.JSONDecodeError, TypeError):
                            fn_args = {}

                        yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'arguments': fn_args})}\n\n"

                        result_str = await execute_tool(fn_name, fn_args, user_id)

                        # Save tool result
                        tool_msg = ChatMessage(
                            conversation_id=conversation_id,
                            role="tool",
                            content=result_str,
                            tool_call_id=tc.get("id", ""),
                        )
                        session.add(tool_msg)

                        # Summarize for progress
                        summary = _summarize_tool_result(fn_name, result_str)
                        yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summary})}\n\n"

                        messages.append({
                            "role": "tool",
                            "content": result_str,
                            "tool_call_id": tc.get("id", ""),
                        })

                    # After tool calls, do another LLM call to get notes update
                    # (the model needs to see the tool results and update its notes)
                    try:
                        followup_content = ""
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(300.0, connect=10.0)
                        ) as client:
                            payload = {
                                "model": settings.llm_model_id,
                                "messages": messages,
                                "tools": CHAT_TOOLS,
                                "temperature": 0.3,
                                "stream": True,
                            }
                            async with client.stream(
                                "POST",
                                f"{settings.llama_server_url}/v1/chat/completions",
                                json=payload,
                            ) as response:
                                response.raise_for_status()
                                async for line in response.aiter_lines():
                                    if not line.startswith("data: "):
                                        continue
                                    data = line[6:].strip()
                                    if data == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue
                                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                                    if delta.get("content"):
                                        followup_content += delta["content"]

                        assistant_content = followup_content
                    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as e:
                        logger.error("Research followup error round %d: %s", _round, e)
                        state.status = "interrupted"
                        state.current_round = _round
                        await session.commit()
                        yield f"data: {json.dumps({'type': 'error', 'message': f'LLM error during notes update: {e}'})}\n\n"
                        return

                # Parse notes from response
                new_notes = _parse_notes(assistant_content)
                if new_notes:
                    notes = new_notes

                # Save assistant notes message
                notes_msg = ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=assistant_content,
                    model_id=settings.llm_model_id,
                )
                session.add(notes_msg)

                # Checkpoint
                state.current_round = _round + 1
                state.notes = notes
                state.progress = progress_data
                await session.commit()

                # Check for report signal
                if _detect_report_signal(assistant_content):
                    break

                # For sweep: check if we've covered all docs
                if strategy == "sweep" and doc_list:
                    next_batch_start = (_round + 1) * sweep_batch_size
                    if next_batch_start >= len(doc_list):
                        break

        except Exception as e:
            logger.exception("Research loop error")
            state.status = "failed"
            state.error = str(e)
            await session.commit()
            yield f"data: {json.dumps({'type': 'error', 'message': f'Research failed: {e}'})}\n\n"
            return

        # ----- Synthesis pass -----
        if not notes:
            state.status = "completed"
            state.completed_at = datetime.now(timezone.utc)
            await session.commit()
            yield f"data: {json.dumps({'type': 'error', 'message': 'No research notes gathered — nothing to synthesize'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'synthesis', 'status': 'started'})}\n\n"

        synth_messages = _build_synthesis_messages(user_question, notes)
        report_content = ""

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=10.0)
            ) as client:
                payload = {
                    "model": settings.llm_model_id,
                    "messages": synth_messages,
                    "temperature": 0.3,
                    "stream": True,
                }
                async with client.stream(
                    "POST",
                    f"{settings.llama_server_url}/v1/chat/completions",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            report_content += delta["content"]
                            yield f"data: {json.dumps({'type': 'token', 'content': delta['content']})}\n\n"

        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as e:
            logger.error("Synthesis error: %s", e)
            state.status = "interrupted"
            await session.commit()
            yield f"data: {json.dumps({'type': 'error', 'message': f'Synthesis failed: {e}'})}\n\n"
            return

        # Save report as assistant message
        report_msg = ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=report_content,
            model_id=settings.llm_model_id,
        )
        session.add(report_msg)

        # Update state
        state.status = "completed"
        state.completed_at = datetime.now(timezone.utc)

        # Update conversation title from question
        conv.title = user_question[:80] if len(user_question) > 80 else user_question
        conv.updated_at = datetime.now(timezone.utc)

        await session.commit()

        yield f"data: {json.dumps({'type': 'done', 'conversation_id': str(conversation_id), 'model_id': settings.llm_model_id})}\n\n"


def _summarize_tool_result(name: str, result_str: str) -> str:
    """Produce a short human-readable summary of a tool result."""
    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return f"{len(result_str)} chars"

    if isinstance(data, dict):
        for key in ("hits", "results"):
            if key in data and isinstance(data[key], list):
                return f"Found {len(data[key])} results"
        if "documents" in data and isinstance(data["documents"], list):
            return f"{len(data['documents'])} documents"
        if "passages" in data and isinstance(data["passages"], list):
            return f"Read {len(data['passages'])} passages"
        if "error" in data:
            return f"Error: {str(data['error'])[:80]}"

    return f"{len(result_str)} chars"
```

**Step 2: Lint and format**

```bash
uv run ruff check src/harbor_clerk/llm/research.py
uv run ruff format --check src/harbor_clerk/llm/research.py
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: research engine — iteration loop with scratchpad and synthesis pass"
```

---

## Task 5: Research API Routes

**Files:**
- Create: `src/harbor_clerk/api/routes/research.py`
- Modify: `src/harbor_clerk/api/app.py` (register router)
- Modify: `src/harbor_clerk/api/routes/chat.py` (filter by mode, 409 on busy)

**Step 1: Create the research router**

Create `src/harbor_clerk/api/routes/research.py`:

```python
"""Research mode API routes."""

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, get_session, require_user
from harbor_clerk.api.routes.chat import _enrich_tool_calls
from harbor_clerk.api.schemas.research import (
    ResearchActiveCheck,
    ResearchDetail,
    ResearchSummary,
    StartResearchRequest,
)
from harbor_clerk.config import get_settings
from harbor_clerk.llm.model_settings import get_model_setting
from harbor_clerk.llm.models import DEFAULT_RESEARCH_MAX_ROUNDS, default_research_strategy
from harbor_clerk.llm.research import research_stream
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)
router = APIRouter(tags=["research"])


async def _check_no_active_research(session: AsyncSession) -> ResearchState | None:
    """Return any running research task, or None."""
    result = await session.execute(
        select(ResearchState).where(ResearchState.status == "running")
    )
    return result.scalar_one_or_none()


@router.get("/research/active", response_model=ResearchActiveCheck)
async def check_active(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Check if a research task is currently running."""
    active = await _check_no_active_research(session)
    if active:
        return ResearchActiveCheck(active=True, research_id=str(active.conversation_id))
    return ResearchActiveCheck(active=False)


@router.get("/research", response_model=list[ResearchSummary])
async def list_research(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """List research tasks for the current user."""
    result = await session.execute(
        select(Conversation, ResearchState)
        .join(ResearchState, Conversation.conversation_id == ResearchState.conversation_id)
        .where(Conversation.user_id == principal.id, Conversation.mode == "research")
        .order_by(Conversation.updated_at.desc())
    )
    rows = result.all()
    return [
        ResearchSummary(
            conversation_id=str(conv.conversation_id),
            title=conv.title,
            strategy=state.strategy,
            status=state.status,
            current_round=state.current_round,
            max_rounds=state.max_rounds,
            created_at=conv.created_at,
            completed_at=state.completed_at,
        )
        for conv, state in rows
    ]


@router.get("/research/{conv_id}", response_model=ResearchDetail)
async def get_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Get full research task detail with messages."""
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=404, detail="Research task not found")

    state = await session.get(ResearchState, conv_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Research state not found")

    # Load messages
    msg_result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at)
    )
    all_msgs = msg_result.scalars().all()

    # Find user question and report
    question = ""
    report = None
    model_id = None

    # Build tool results lookup
    tool_results_by_id: dict[str, str] = {}
    for m in all_msgs:
        if m.role == "user" and not question:
            question = m.content
        if m.role == "tool" and m.tool_call_id:
            try:
                data = json.loads(m.content)
                tool_results_by_id[m.tool_call_id] = _summarize_tool_result_brief(data)
            except (json.JSONDecodeError, TypeError):
                tool_results_by_id[m.tool_call_id] = m.content[:100]

    # The report is the last assistant message (after synthesis)
    assistant_msgs = [m for m in all_msgs if m.role == "assistant" and m.content]
    if assistant_msgs and state.status == "completed":
        report = assistant_msgs[-1].content
        model_id = assistant_msgs[-1].model_id

    # Build enriched message list (non-tool messages only)
    messages_out = []
    for m in all_msgs:
        if m.role == "tool":
            continue
        entry: dict = {
            "message_id": str(m.message_id),
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        if m.tool_calls:
            entry["tool_calls"] = _enrich_tool_calls(m.tool_calls, tool_results_by_id)
        if m.model_id:
            entry["model_id"] = m.model_id
        messages_out.append(entry)

    return ResearchDetail(
        conversation_id=str(conv.conversation_id),
        title=conv.title,
        question=question,
        strategy=state.strategy,
        status=state.status,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        progress=state.progress,
        report=report,
        model_id=model_id,
        messages=messages_out,
        created_at=conv.created_at,
        completed_at=state.completed_at,
    )


@router.post("/research")
async def start_research(
    body: StartResearchRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Start a new research task. Returns SSE stream."""
    settings = get_settings()
    if not settings.llm_model_id:
        raise HTTPException(status_code=403, detail="No model configured")

    # Check for active research
    active = await _check_no_active_research(session)
    if active:
        raise HTTPException(
            status_code=409,
            detail="Another research task is already running",
        )

    # Determine strategy
    strategy = body.strategy or default_research_strategy(settings.llm_model_id)

    # Get max rounds from per-model settings or global default
    max_rounds = await get_model_setting(
        session, settings.llm_model_id, "research_max_rounds", DEFAULT_RESEARCH_MAX_ROUNDS
    )

    # Create conversation
    conv = Conversation(
        user_id=principal.id,
        title=body.question[:80] if len(body.question) > 80 else body.question,
        mode="research",
    )
    session.add(conv)
    await session.flush()

    # Save user message
    user_msg = ChatMessage(
        conversation_id=conv.conversation_id,
        role="user",
        content=body.question,
    )
    session.add(user_msg)

    # Create research state
    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy=strategy,
        status="running",
        max_rounds=max_rounds,
    )
    session.add(state)
    await session.commit()

    return StreamingResponse(
        research_stream(conv.conversation_id, user_id=principal.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/research/{conv_id}/resume")
async def resume_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Resume an interrupted research task."""
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=404, detail="Research task not found")

    state = await session.get(ResearchState, conv_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Research state not found")
    if state.status != "interrupted":
        raise HTTPException(status_code=409, detail=f"Cannot resume: status is {state.status}")

    # Check no other research running
    active = await _check_no_active_research(session)
    if active and active.conversation_id != conv_id:
        raise HTTPException(status_code=409, detail="Another research task is already running")

    return StreamingResponse(
        research_stream(conv.conversation_id, user_id=principal.id, resume=True),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/research/{conv_id}")
async def delete_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a research task and all its data."""
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=404, detail="Research task not found")

    await session.delete(conv)  # CASCADE handles research_state + messages
    await session.commit()
    return {"ok": True}


def _summarize_tool_result_brief(data: dict) -> str:
    """Brief summary for tool result display."""
    for key in ("hits", "results"):
        if key in data and isinstance(data[key], list):
            return f"Found {len(data[key])} results"
    if "documents" in data and isinstance(data["documents"], list):
        return f"{len(data['documents'])} documents"
    if "passages" in data and isinstance(data["passages"], list):
        return f"Read {len(data['passages'])} passages"
    return "Done"
```

**Step 2: Register router in app.py**

In `src/harbor_clerk/api/app.py`, add alongside the other router imports:

```python
from harbor_clerk.api.routes.research import router as research_router
```

And in the `include_router` section:

```python
app.include_router(research_router, prefix="/api")
```

**Step 3: Filter chat routes by mode**

In `src/harbor_clerk/api/routes/chat.py`, in the `list_conversations` endpoint, add a filter:

```python
.where(Conversation.user_id == principal.id, Conversation.mode == "chat")
```

Also in the `send_message` endpoint, after loading the conversation, add a check for active research:

```python
# Check if research is running (LLM busy)
from harbor_clerk.models.research_state import ResearchState
research_result = await session.execute(
    select(ResearchState).where(ResearchState.status == "running")
)
if research_result.scalar_one_or_none():
    raise HTTPException(
        status_code=409,
        detail="Research task in progress — chat unavailable until complete",
    )
```

**Step 4: Lint and format**

```bash
uv run ruff check src/harbor_clerk/api/routes/research.py src/harbor_clerk/api/routes/chat.py src/harbor_clerk/api/app.py
uv run ruff format --check src/harbor_clerk/api/routes/research.py src/harbor_clerk/api/routes/chat.py src/harbor_clerk/api/app.py
```

**Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/research.py src/harbor_clerk/api/schemas/research.py src/harbor_clerk/api/app.py src/harbor_clerk/api/routes/chat.py
git commit -m "feat: research API routes — start, resume, delete, list, detail, active check"
```

---

## Task 6: Update Chat Fallback Message

Update the "I used all tool calls" message to suggest Research mode.

**Files:**
- Modify: `src/harbor_clerk/llm/chat.py`

**Step 1: Update the fallback message**

Find the max_tool_rounds exhaustion message in `chat.py` (around line 377) and update it:

```python
assistant_content = (
    "I used all available tool calls but wasn't able to formulate a complete response. "
    "You can try rephrasing your question or asking something more specific. "
    "For broader questions that need to cover many documents, try the Research tab."
)
```

**Step 2: Lint check**

```bash
uv run ruff check src/harbor_clerk/llm/chat.py
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/llm/chat.py
git commit -m "feat: suggest Research tab in chat tool-rounds-exhausted message"
```

---

## Task 7: Frontend — useResearch Hook

SSE streaming hook for research progress and report.

**Files:**
- Create: `frontend/src/hooks/useResearch.ts`

**Step 1: Create the hook**

```typescript
import { useCallback, useRef, useState } from 'react'
import { useAuth } from '../auth'

export interface ToolCallEntry {
  name: string
  arguments: Record<string, unknown>
  summary?: string
}

export interface ResearchProgress {
  round: number
  maxRounds: number
  strategy: string
  reviewed?: number
  total?: number
  toolCalls: ToolCallEntry[]
}

export interface ResearchTask {
  conversationId: string
  question: string
  strategy: string
  status: 'running' | 'interrupted' | 'completed' | 'failed'
  currentRound: number
  maxRounds: number
  progress: Record<string, unknown> | null
  report: string | null
  modelId: string | null
  createdAt: string
  completedAt: string | null
  error?: string
}

export function useResearch() {
  const { token } = useAuth()
  const [isRunning, setIsRunning] = useState(false)
  const [progress, setProgress] = useState<ResearchProgress | null>(null)
  const [report, setReport] = useState<string>('')
  const [isSynthesizing, setIsSynthesizing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const startResearch = useCallback(
    async (question: string, strategy?: string) => {
      setIsRunning(true)
      setProgress({ round: 0, maxRounds: 0, strategy: strategy || 'search', toolCalls: [] })
      setReport('')
      setError(null)
      setIsSynthesizing(false)

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const res = await fetch('/api/research', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ question, strategy: strategy || undefined }),
          signal: controller.signal,
        })

        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }))
          setError(body.detail || `Error ${res.status}`)
          setIsRunning(false)
          return
        }

        setConversationId(null) // Will be set from done event

        const reader = res.body!.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            try {
              const event = JSON.parse(line.slice(6))
              switch (event.type) {
                case 'progress':
                  setProgress((prev) => ({
                    round: event.round,
                    maxRounds: event.max_rounds,
                    strategy: event.strategy,
                    reviewed: event.reviewed,
                    total: event.total,
                    toolCalls: prev?.toolCalls || [],
                  }))
                  break
                case 'tool_call':
                  setProgress((prev) =>
                    prev
                      ? {
                          ...prev,
                          toolCalls: [...prev.toolCalls, { name: event.name, arguments: event.arguments }],
                        }
                      : prev,
                  )
                  break
                case 'tool_result':
                  setProgress((prev) => {
                    if (!prev) return prev
                    const updated = [...prev.toolCalls]
                    // Find last matching tool call without a summary
                    for (let i = updated.length - 1; i >= 0; i--) {
                      if (updated[i].name === event.name && !updated[i].summary) {
                        updated[i] = { ...updated[i], summary: event.summary }
                        break
                      }
                    }
                    return { ...prev, toolCalls: updated }
                  })
                  break
                case 'synthesis':
                  setIsSynthesizing(true)
                  break
                case 'token':
                  setReport((prev) => prev + event.content)
                  break
                case 'done':
                  setConversationId(event.conversation_id)
                  setIsRunning(false)
                  setIsSynthesizing(false)
                  break
                case 'error':
                  setError(event.message)
                  setIsRunning(false)
                  setIsSynthesizing(false)
                  break
              }
            } catch {
              // Skip malformed events
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setError((err as Error).message)
        }
        setIsRunning(false)
      }
    },
    [token],
  )

  const resumeResearch = useCallback(
    async (convId: string) => {
      setIsRunning(true)
      setError(null)
      setIsSynthesizing(false)
      setReport('')

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const res = await fetch(`/api/research/${convId}/resume`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        })

        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }))
          setError(body.detail || `Error ${res.status}`)
          setIsRunning(false)
          return
        }

        setConversationId(convId)

        const reader = res.body!.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        // Same SSE parsing loop as startResearch — factor out if it gets unwieldy
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            try {
              const event = JSON.parse(line.slice(6))
              switch (event.type) {
                case 'progress':
                  setProgress((prev) => ({
                    round: event.round,
                    maxRounds: event.max_rounds,
                    strategy: event.strategy,
                    reviewed: event.reviewed,
                    total: event.total,
                    toolCalls: prev?.toolCalls || [],
                  }))
                  break
                case 'tool_call':
                  setProgress((prev) =>
                    prev
                      ? { ...prev, toolCalls: [...prev.toolCalls, { name: event.name, arguments: event.arguments }] }
                      : prev,
                  )
                  break
                case 'tool_result':
                  setProgress((prev) => {
                    if (!prev) return prev
                    const updated = [...prev.toolCalls]
                    for (let i = updated.length - 1; i >= 0; i--) {
                      if (updated[i].name === event.name && !updated[i].summary) {
                        updated[i] = { ...updated[i], summary: event.summary }
                        break
                      }
                    }
                    return { ...prev, toolCalls: updated }
                  })
                  break
                case 'synthesis':
                  setIsSynthesizing(true)
                  break
                case 'token':
                  setReport((prev) => prev + event.content)
                  break
                case 'done':
                  setIsRunning(false)
                  setIsSynthesizing(false)
                  break
                case 'error':
                  setError(event.message)
                  setIsRunning(false)
                  break
              }
            } catch {
              // Skip malformed
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setError((err as Error).message)
        }
        setIsRunning(false)
      }
    },
    [token],
  )

  const cancelResearch = useCallback(() => {
    abortRef.current?.abort()
    setIsRunning(false)
  }, [])

  return {
    isRunning,
    isSynthesizing,
    progress,
    report,
    error,
    conversationId,
    startResearch,
    resumeResearch,
    cancelResearch,
  }
}
```

**Step 2: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check
```

**Step 3: Commit**

```bash
git add frontend/src/hooks/useResearch.ts
git commit -m "feat: useResearch SSE hook for research progress and report streaming"
```

---

## Task 8: Frontend — ResearchPage Component

The main Research tab page with four states: idle, running, completed, interrupted.

**Files:**
- Create: `frontend/src/pages/ResearchPage.tsx`

**Step 1: Create ResearchPage.tsx**

This is a large component. Key sections:

1. **State management**: uses `useResearch` hook + local state for history list and selected task
2. **Idle state**: thinking octopus, explanation text, input + strategy toggle, history list
3. **Running state**: smaller octopus, progress indicator (round-based or document-based), tool call log, cancel button
4. **Completed state**: question header, markdown-rendered report, metadata, collapsible tool history
5. **Interrupted state**: resume/discard buttons with two-click confirmation

The image for the thinking octopus should be placed at `frontend/public/research-octopus.png`. The user has already provided it.

The component fetches `/api/research` on mount for the history list, and `/api/research/{id}` when viewing a completed/interrupted task.

Strategy toggle: two radio-style buttons ("Search-driven" / "Systematic sweep") styled as a segmented control.

Progress bar for sweep mode: a simple div with width percentage.

Tool call log: reuse the visual pattern from `ToolCallCard` in ChatPage but simplified — just a stacking list of tool name + summary.

Report rendering: reuse the same `ReactMarkdown` + `remarkGfm` setup from ChatPage's `MessageBubble`.

Two-click discard: first click changes button text to "Confirm discard?" in red, second click fires the DELETE. Resets after 3 seconds if not confirmed.

Full implementation is too large to include inline — the implementer should build it following the patterns in `ChatPage.tsx` for styling (text sizes, colors, dark mode, card styling) and `useChat.ts` for the fetch/auth pattern.

**Key markup structure:**

```tsx
export default function ResearchPage() {
  // If URL has :id param, show detail view
  // Otherwise show list + input view

  return (
    <div className="flex h-[calc(100vh-7rem)] gap-4">
      {/* Sidebar: history list */}
      <aside className="w-64 shrink-0 ...">
        {/* Research task history */}
      </aside>

      {/* Main area */}
      <div className="flex-1 flex flex-col ...">
        {isRunning ? <RunningView /> :
         selectedTask?.status === 'completed' ? <CompletedView /> :
         selectedTask?.status === 'interrupted' ? <InterruptedView /> :
         <IdleView />}
      </div>
    </div>
  )
}
```

**Step 2: Add the thinking octopus image**

Save the user-provided thinking octopus image to `frontend/public/research-octopus.png`.

**Step 3: Register route in App.tsx**

Add to `frontend/src/App.tsx`:

```tsx
import ResearchPage from './pages/ResearchPage'

// Inside Routes, within ProtectedRoute > Layout:
<Route path="/research" element={<ResearchPage />} />
<Route path="/research/:researchId" element={<ResearchPage />} />
```

**Step 4: Add Research tab to Layout.tsx**

In `frontend/src/components/Layout.tsx`, add after the Ask NavLink:

```tsx
<TabLink to="/research">Research</TabLink>
```

**Step 5: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check
```

**Step 6: Commit**

```bash
git add frontend/src/pages/ResearchPage.tsx frontend/src/App.tsx frontend/src/components/Layout.tsx frontend/public/research-octopus.png
git commit -m "feat: Research tab — idle, running, completed, interrupted states"
```

---

## Task 9: Frontend — Ask Tab Busy Blocker

Show the thinking octopus overlay when research is running.

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx`
- Modify: `frontend/src/hooks/useChat.ts`

**Step 1: Add research-active check to ChatPage**

At the top of the `ChatPage` component, add a check:

```typescript
const [researchActive, setResearchActive] = useState(false)
const [researchId, setResearchId] = useState<string | null>(null)

useEffect(() => {
  const check = async () => {
    try {
      const res = await fetch('/api/research/active', {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.ok) {
        const data = await res.json()
        setResearchActive(data.active)
        setResearchId(data.research_id || null)
      }
    } catch {
      // Ignore — research endpoint may not exist yet
    }
  }
  check()
}, [token])
```

**Step 2: Add overlay**

When `researchActive` is true, render an overlay over the chat area:

```tsx
{researchActive && (
  <div className="absolute inset-0 z-30 flex flex-col items-center justify-center bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm">
    <img src="/research-octopus.png" alt="" className="h-32 w-32 mb-4 opacity-80" />
    <p className="text-[15px] font-medium text-gray-700 dark:text-gray-300 mb-2">
      Harbor Clerk is working on a research task
    </p>
    <a
      href="/research"
      className="text-[13px] text-amber-600 dark:text-amber-400 hover:underline"
    >
      View in Research tab
    </a>
  </div>
)}
```

**Step 3: Disable input when research is active**

In the input area, add `disabled={researchActive}` to the textarea and submit button.

**Step 4: Lint and type-check**

```bash
cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check
```

**Step 5: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx
git commit -m "feat: Ask tab busy blocker when research task is running"
```

---

## Task 10: Full Verification

Run all linting and type checks to ensure nothing is broken.

**Step 1: Backend checks**

```bash
cd /Users/alex/mcp-gateway
uv run ruff check src/harbor_clerk/
uv run ruff format --check src/harbor_clerk/
```

**Step 2: Frontend checks**

```bash
cd /Users/alex/mcp-gateway/frontend
npm run lint
npm run type-check
npm run format:check
```

**Step 3: Build macOS apps**

```bash
cd /Users/alex/mcp-gateway/macos && make apps
```

**Step 4: Manual testing checklist**

1. Start the app, verify Research tab appears in nav between Ask and Upload
2. Research tab idle state: shows thinking octopus, explanation, input, strategy toggle
3. Start a research task — progress events appear (round counter + tool calls)
4. Let it complete — report renders with markdown
5. Navigate away and back — completed task loads from history
6. Start a new research task, close the browser tab mid-way — reopen, see interrupted state
7. Resume interrupted task — continues from checkpoint
8. Discard interrupted task — two-click confirmation works
9. While research is running, switch to Ask tab — busy blocker overlay shows
10. After research completes, Ask tab is usable again
11. In Ask, exhaust tool rounds — message suggests Research tab

**Step 5: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: address verification issues"
```

---

## Summary

| Task | What | Key files |
|------|------|-----------|
| 1 | Database migration | `alembic/versions/0005_research_mode.py`, 3 model files |
| 2 | Per-model settings helper | `src/harbor_clerk/llm/model_settings.py`, `models.py` |
| 3 | Research schemas | `src/harbor_clerk/api/schemas/research.py` |
| 4 | Research engine (core loop) | `src/harbor_clerk/llm/research.py` |
| 5 | Research API routes | `src/harbor_clerk/api/routes/research.py`, `app.py`, `chat.py` |
| 6 | Chat fallback message | `src/harbor_clerk/llm/chat.py` |
| 7 | useResearch hook | `frontend/src/hooks/useResearch.ts` |
| 8 | ResearchPage component | `frontend/src/pages/ResearchPage.tsx`, `App.tsx`, `Layout.tsx` |
| 9 | Ask tab busy blocker | `frontend/src/pages/ChatPage.tsx` |
| 10 | Full verification | All files, manual testing |
