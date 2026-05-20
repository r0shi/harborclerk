# Research-engine defect fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three research-engine defects from issue #368 — garbage corpus-seeded queries, a "no relevant findings" verdict that wrongly becomes terminal, and an always-empty `result.citations`.

**Architecture:** All three fixes live in `src/harbor_clerk/llm/research.py`; Fix 3 also adds a `research_state.citations` column (Alembic migration 0023) and updates `api/routes/research.py`. Fixes are additive and independent; tested with mocked LLM calls.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, pytest + pytest-asyncio, httpx.

**Spec:** `docs/superpowers/specs/2026-05-22-research-engine-defect-fixes-design.md`

---

## File structure

- `src/harbor_clerk/llm/research.py` — all three fixes' engine logic.
- `src/harbor_clerk/models/research_state.py` — add `citations` column (Fix 3).
- `alembic/versions/0023_research_state_citations.py` — new migration (Fix 3).
- `src/harbor_clerk/api/routes/research.py` — read citations from state (Fix 3).
- `src/harbor_clerk/api/schemas/research.py` — stale-comment fix (Fix 3).
- `tests/test_research_engine.py` — new; unit tests for Fixes 1, 2, 3a.
- `tests/test_api_research_citations.py` — existing; extend for Fix 3.

---

## Task 1: Fix 1 — drop corpus-seeded queries

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`
- Test: `tests/test_research_engine.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_research_engine.py`:

```python
"""Unit tests for research engine internals (harbor_clerk.llm.research)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.llm.research import _plan_queries

_DEPTH = {"max_queries": 15, "k_per_query": 20, "max_passages": 60, "gap_round": True, "paginate": True}


@pytest.mark.asyncio
async def test_plan_queries_returns_only_llm_queries_no_seeded_garbage():
    """After dropping corpus seeding, _plan_queries returns exactly the
    LLM-planned queries. The old code concatenated corpus entities with the
    question into ungrammatical seeded queries ('Globex What major vendor
    relationships'); none of those may appear."""
    llm_response = json.dumps({"queries": ["governing law clauses", "termination notice periods"]})
    # entity_overview is what the deleted seeding path called; patching it
    # proves the new code never seeds even when entities are available.
    entity_response = json.dumps({"top_entities": [{"entity_type": "ORG", "entity_text": "Globex"}]})
    with (
        patch("harbor_clerk.llm.research._llm_complete", new=AsyncMock(return_value=llm_response)),
        patch("harbor_clerk.llm.research.execute_tool", new=AsyncMock(return_value=entity_response)),
    ):
        queries = await _plan_queries(
            client=None,
            url="http://llm.test",
            user_question="What are the major vendor relationships?",
            topic_hint=None,
            depth_config=_DEPTH,
            doc_list=None,
            user_id=None,
        )
    assert queries == ["governing law clauses", "termination notice periods"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py::test_plan_queries_returns_only_llm_queries_no_seeded_garbage -v`
Expected: FAIL — the result includes a seeded entry like `"Globex What major vendor relationships"`.

- [ ] **Step 3: Delete the seeding code**

In `src/harbor_clerk/llm/research.py`:
1. Delete the entire `_seed_queries_from_corpus` function (currently `async def _seed_queries_from_corpus(...)` through its `return seeded`).
2. Delete the `_SEED_QUERY_BLOCKED_ENTITY_TYPES` constant and its preceding comment block (the comment near line 59 about `CARDINAL`/`ORDINAL` polluting seeded queries).
3. If `execute_tool` is now unused in the module, leave its import — it's used elsewhere in research.py (`_search_fan_out` etc.); only remove the import if a lint run flags it unused.

- [ ] **Step 4: Update `_plan_queries`**

In `_plan_queries`, change the LLM query target and remove the seeded merge.

Change this line:
```python
    llm_target = max(5, depth_config["max_queries"] // 2)
```
to:
```python
    # The LLM is the sole query source now (corpus seeding removed); ask it
    # for the full budget — it is a far better generator than the deleted
    # entity-concatenation, and _is_plausible_query still filters junk.
    llm_target = depth_config["max_queries"]
```

Replace this block:
```python
    # Corpus-seeded queries (model-independent)
    seeded = await _seed_queries_from_corpus(user_question, user_id, topic_hint)

    # Merge: LLM queries first (higher intent), then seeded (breadth), dedupe
    seen_lower: set[str] = set()
    merged: list[str] = []
    for q in llm_queries + seeded:
        q = q.strip()
        if q and q.lower() not in seen_lower:
            seen_lower.add(q.lower())
            merged.append(q)

    return merged[: depth_config["max_queries"]]
```
with:
```python
    # Dedupe (case-insensitive), preserving order.
    seen_lower: set[str] = set()
    merged: list[str] = []
    for q in llm_queries:
        q = q.strip()
        if q and q.lower() not in seen_lower:
            seen_lower.add(q.lower())
            merged.append(q)

    return merged[: depth_config["max_queries"]]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -v`
Expected: PASS.

- [ ] **Step 6: Run ruff and the existing research tests**

Run: `uv run ruff check src/harbor_clerk/llm/research.py && DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_api_research_citations.py tests/test_llm_citations.py -q`
Expected: ruff clean; existing tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/llm/research.py tests/test_research_engine.py
git commit -m "fix(research): drop garbage corpus-seeded queries (#368)"
```

---

## Task 2: Fix 2 — sentinel detection + forceful note-extraction prompt

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`
- Test: `tests/test_research_engine.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_research_engine.py`:

```python
from harbor_clerk.llm.research import _is_no_findings_sentinel


def test_is_no_findings_sentinel_matches_the_sentinel():
    assert _is_no_findings_sentinel("No relevant findings in this passage set.")
    # Tolerant of trailing whitespace / case / wrapping the engine may add.
    assert _is_no_findings_sentinel("  no relevant findings in this passage set  ")
    assert _is_no_findings_sentinel("No relevant findings in this passage set")


def test_is_no_findings_sentinel_rejects_real_notes_and_empty_corpus_string():
    # A real note must not be treated as the bail sentinel.
    assert not _is_no_findings_sentinel("- Acme signed a 30-day termination clause [Doc, page 2]")
    # _extract_notes's genuinely-empty return is a DIFFERENT string and must
    # not match — that case is truly empty and should not trigger a retry.
    assert not _is_no_findings_sentinel("No relevant passages were found in the corpus.")
    assert not _is_no_findings_sentinel("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -k sentinel -v`
Expected: FAIL with `ImportError: cannot import name '_is_no_findings_sentinel'`.

- [ ] **Step 3: Add the sentinel helper, the forceful prompt, and the relevance floor**

In `src/harbor_clerk/llm/research.py`, add the forceful note-extraction system prompt next to `_NOTE_EXTRACTION_SYSTEM`:

```python
_NOTE_EXTRACTION_SYSTEM_FORCEFUL = (
    "You are extracting research notes from search results. These passages "
    "are the TOP-RANKED retrieval matches for the research question and are "
    "very likely relevant — a prior extraction attempt wrongly dismissed "
    "them.\n\n"
    "Rules:\n"
    "- Cite every finding as [Document Title, page X] — exactly as shown in the passages\n"
    "- Write one note per distinct finding\n"
    "- Skip irrelevant or redundant passages\n"
    "- Preserve factual details — names, numbers, dates\n"
    "- If a passage contradicts another, note both with their citations\n"
    "- Write in plain text with citations, not JSON\n"
    "- Extract the relevant findings. Return `No relevant findings in this "
    "passage set.` ONLY if the passages genuinely contain nothing on-topic "
    "— do not use it as a shortcut. Do not invent content from titles or "
    "general knowledge."
)
```

Add the relevance-floor constant next to the `_DEPTH_CONFIGS` block (near line 139):

```python
# When note extraction returns the "no relevant findings" sentinel, retry it
# forcefully only if retrieval actually found strong matches — i.e. the top
# coverage score clears this floor. Below it, the corpus genuinely lacks the
# information and the honest "no findings" answer is correct. Hybrid-retrieval
# scores in observed research runs ranged ~0.5-1.7; 1.0 separates a solid
# match from weak noise. Tunable.
_RETRIEVAL_RELEVANCE_FLOOR = 1.0
```

Add the sentinel helper near `_extract_notes`:

```python
# The exact sentinel _NOTE_EXTRACTION_SYSTEM tells the model to return when a
# passage set is off-topic. Distinct from _extract_notes's genuinely-empty
# return ("No relevant passages were found in the corpus.").
_NO_FINDINGS_SENTINEL_PREFIX = "no relevant findings in this passage set"


def _is_no_findings_sentinel(notes_text: str) -> bool:
    """True if note extraction bailed with the 'no relevant findings'
    sentinel. Tolerant of case and surrounding whitespace."""
    return notes_text.strip().lower().startswith(_NO_FINDINGS_SENTINEL_PREFIX)
```

- [ ] **Step 4: Add the `forceful` parameter to `_extract_notes`**

Change the `_extract_notes` signature:
```python
async def _extract_notes(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    passages_text: str,
) -> str:
```
to:
```python
async def _extract_notes(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    passages_text: str,
    *,
    forceful: bool = False,
) -> str:
```

Inside `_extract_notes`, change the system-prompt line in the `messages` list:
```python
        {"role": "system", "content": _NOTE_EXTRACTION_SYSTEM},
```
to:
```python
        {"role": "system", "content": _NOTE_EXTRACTION_SYSTEM_FORCEFUL if forceful else _NOTE_EXTRACTION_SYSTEM},
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -k sentinel -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/llm/research.py tests/test_research_engine.py
git commit -m "fix(research): add forceful note-extraction prompt + sentinel helper (#368)"
```

---

## Task 3: Fix 2 — retry-then-fallback orchestrator, wired into research_stream

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`
- Test: `tests/test_research_engine.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_research_engine.py`:

```python
from harbor_clerk.llm.research import _extract_notes_with_retry

_SENTINEL = "No relevant findings in this passage set."
_PASSAGES = "\n---\n**[0265_quarterly_report, page 1]**\nQ1 consulting revenue up 14%.\n"


def _coverage(top_score: float) -> dict:
    return {"c1": {"doc_id": "d1", "doc_title": "0265_quarterly_report", "page": 1, "score": top_score}}


@pytest.mark.asyncio
async def test_retry_not_triggered_when_extraction_succeeds():
    """A normal (non-sentinel) extraction result passes straight through."""
    real_notes = "- Q1 consulting revenue up 14% [0265_quarterly_report, page 1]"
    with patch("harbor_clerk.llm.research._extract_notes", new=AsyncMock(return_value=real_notes)) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out == real_notes
    assert m.await_count == 1  # no retry


@pytest.mark.asyncio
async def test_retry_recovers_when_forceful_extraction_succeeds():
    """Sentinel + high retrieval score → forceful retry; if the retry yields
    real notes, those are used."""
    real_notes = "- Q1 consulting revenue up 14% [0265_quarterly_report, page 1]"
    with patch(
        "harbor_clerk.llm.research._extract_notes",
        new=AsyncMock(side_effect=[_SENTINEL, real_notes]),
    ) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out == real_notes
    assert m.await_count == 2
    assert m.await_args_list[1].kwargs.get("forceful") is True


@pytest.mark.asyncio
async def test_retry_falls_back_to_raw_passages_when_retry_also_bails():
    """Sentinel twice → raw passages become the notes."""
    with patch(
        "harbor_clerk.llm.research._extract_notes",
        new=AsyncMock(side_effect=[_SENTINEL, _SENTINEL]),
    ):
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(1.7))
    assert out.startswith("## Raw passages")
    assert "0265_quarterly_report" in out


@pytest.mark.asyncio
async def test_low_score_sentinel_is_trusted_no_retry():
    """Sentinel + low retrieval score → the sentinel is kept; the corpus
    genuinely lacks the information. No retry."""
    with patch("harbor_clerk.llm.research._extract_notes", new=AsyncMock(return_value=_SENTINEL)) as m:
        out = await _extract_notes_with_retry(None, "http://x", "q", _PASSAGES, _coverage(0.3))
    assert _is_no_findings_sentinel(out)
    assert m.await_count == 1  # no retry below the relevance floor
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -k retry -v`
Expected: FAIL with `ImportError: cannot import name '_extract_notes_with_retry'`.

- [ ] **Step 3: Add the `_extract_notes_with_retry` orchestrator**

In `src/harbor_clerk/llm/research.py`, add directly after `_extract_notes`:

```python
async def _extract_notes_with_retry(
    client: httpx.AsyncClient,
    url: str,
    user_question: str,
    passages_text: str,
    coverage: dict[str, dict],
) -> str:
    """Extract notes, but don't let a weak model's 'no relevant findings'
    verdict be terminal when retrieval clearly succeeded.

    If extraction returns the sentinel AND the top coverage score clears
    _RETRIEVAL_RELEVANCE_FLOOR, retry once with the forceful prompt; if the
    retry also bails, fall back to handing the raw passages to synthesis.
    Below the floor the sentinel is trusted — the corpus genuinely lacks
    the information.
    """
    notes_text = await _extract_notes(client, url, user_question, passages_text)
    if not _is_no_findings_sentinel(notes_text):
        return notes_text

    top_score = max((c.get("score", 0) for c in coverage.values()), default=0)
    if top_score < _RETRIEVAL_RELEVANCE_FLOOR:
        logger.info("Note extraction returned no-findings; top score %.2f below floor — trusting it", top_score)
        return notes_text

    logger.info("Note extraction bailed despite top score %.2f — retrying forcefully", top_score)
    notes_text = await _extract_notes(client, url, user_question, passages_text, forceful=True)
    if not _is_no_findings_sentinel(notes_text):
        return notes_text

    logger.warning("Forceful note extraction also bailed — passing raw passages to synthesis")
    return f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"
```

- [ ] **Step 4: Wire it into `research_stream`**

In `research_stream`, find the round-1 note-extraction block. Replace this:
```python
                    else:
                        try:
                            notes_text = await _extract_notes(client, llm_url, user_question, passages_text)
                        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                            logger.error("LLM error during note extraction: %s", exc)
                            # Fallback: use raw passages as notes
                            notes_text = f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"
```
with:
```python
                    else:
                        try:
                            notes_text = await _extract_notes_with_retry(
                                client, llm_url, user_question, passages_text, coverage
                            )
                        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                            logger.error("LLM error during note extraction: %s", exc)
                            # Fallback: use raw passages as notes
                            notes_text = f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"
```

Leave the gap-round note extraction (the later `_extract_notes` call) unchanged — gap rounds are supplementary.

- [ ] **Step 5: Run tests to verify they pass**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -v`
Expected: all PASS.

- [ ] **Step 6: Run ruff**

Run: `uv run ruff check src/harbor_clerk/llm/research.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/llm/research.py tests/test_research_engine.py
git commit -m "fix(research): retry note extraction before accepting 'no findings' (#368)"
```

---

## Task 4: Fix 3a — `_read_evidence` returns the docs that informed the answer

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`
- Test: `tests/test_research_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_research_engine.py`:

```python
from harbor_clerk.llm.research import _read_evidence


@pytest.mark.asyncio
async def test_read_evidence_returns_passages_and_evidence_docs():
    """_read_evidence returns (passages_text, evidence_docs); evidence_docs
    lists the distinct docs whose passages were read into passages_text."""
    coverage = {
        "c1": {"doc_id": "d1", "doc_title": "alpha", "page": 1, "score": 2.0, "snippet": "alpha text"},
        "c2": {"doc_id": "d2", "doc_title": "beta", "page": 3, "score": 1.5, "snippet": "beta text"},
    }
    read_result = json.dumps(
        {
            "passages": [
                {"chunk_id": "c1", "text": "alpha body", "doc_title": "alpha", "page": 1},
                {"chunk_id": "c2", "text": "beta body", "doc_title": "beta", "page": 3},
            ]
        }
    )
    with patch("harbor_clerk.llm.research.execute_tool", new=AsyncMock(return_value=read_result)):
        passages_text, evidence_docs = await _read_evidence(
            coverage, user_id=None, max_passages=10, context_budget_chars=100_000
        )
    assert "alpha body" in passages_text and "beta body" in passages_text
    by_id = {d["doc_id"]: d for d in evidence_docs}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"]["doc_title"] == "alpha"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py::test_read_evidence_returns_passages_and_evidence_docs -v`
Expected: FAIL — `_read_evidence` currently returns a bare string, so the tuple-unpack raises `ValueError`.

- [ ] **Step 3: Make `_read_evidence` return `(passages_text, evidence_docs)`**

In `_read_evidence`, change the return type annotation `-> str:` to `-> tuple[str, list[dict]]:`.

Find the early-return for no selected chunks:
```python
    if not selected:
        return ""
```
change to:
```python
    if not selected:
        return "", []
```

Initialize an accumulator next to `passages_text = ""`:
```python
    passages_text = ""
    total_chars = 0
```
change to:
```python
    passages_text = ""
    total_chars = 0
    evidence_docs: list[dict] = []
    seen_evidence_docs: set[str] = set()
```

Inside the passage loop, immediately after the line `passages_text += entry` and `total_chars += len(entry)`, record the doc — only for passages that actually made it into `passages_text` (i.e. after the budget check that `break`s). The existing lines are:
```python
                if total_chars + len(entry) > context_budget_chars:
                    break
                passages_text += entry
                total_chars += len(entry)
```
change to:
```python
                if total_chars + len(entry) > context_budget_chars:
                    break
                passages_text += entry
                total_chars += len(entry)
                # Record the doc behind this passage — the "informed the
                # answer" set used to populate result.citations (Fix 3).
                info = coverage.get(cid) if cid else None
                if info and info.get("doc_id") and info["doc_id"] not in seen_evidence_docs:
                    seen_evidence_docs.add(info["doc_id"])
                    evidence_docs.append(
                        {"doc_id": info["doc_id"], "doc_title": info.get("doc_title", ""), "page": passage.get("page")}
                    )
```

Change the final `return passages_text` to:
```python
    return passages_text, evidence_docs
```

- [ ] **Step 4: Update both `_read_evidence` call sites in `research_stream`**

`_read_evidence` is called twice in `research_stream` — the round-1 read and the gap-round read. Find each `... = await _read_evidence(...)` call.

Round-1 call — change:
```python
                    passages_text = await _read_evidence(...)
```
to:
```python
                    passages_text, evidence_docs = await _read_evidence(...)
```

Gap-round call — change:
```python
                        gap_passages = await _read_evidence(...)
```
to:
```python
                        gap_passages, gap_evidence_docs = await _read_evidence(...)
```

(The keyword/positional args inside each call are unchanged — only the assignment target changes. `evidence_docs` / `gap_evidence_docs` are consumed in Task 6; an unused local is fine until then but Task 6 lands in the same PR.)

- [ ] **Step 5: Run test + ruff**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py -v && uv run ruff check src/harbor_clerk/llm/research.py`
Expected: all PASS; ruff clean (ruff may warn `evidence_docs`/`gap_evidence_docs` unused — acceptable, consumed in Task 6; if ruff errors on it, prefix the gap one with `_` and rename back in Task 6).

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/llm/research.py tests/test_research_engine.py
git commit -m "fix(research): _read_evidence returns the docs that informed the answer (#368)"
```

---

## Task 5: Fix 3b — `research_state.citations` column + migration 0023

**Files:**
- Modify: `src/harbor_clerk/models/research_state.py`
- Create: `alembic/versions/0023_research_state_citations.py`
- Test: `tests/test_research_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_research_engine.py`:

```python
import uuid as _uuid


@pytest.mark.asyncio
async def test_research_state_citations_column_roundtrips(db_session):
    """ResearchState persists a citations JSON list."""
    from harbor_clerk.models import Conversation
    from harbor_clerk.models.research_state import ResearchState

    conv = Conversation(title="t")
    db_session.add(conv)
    await db_session.flush()

    cites = [{"doc_id": str(_uuid.uuid4()), "doc_title": "alpha", "page": 1}]
    db_session.add(
        ResearchState(
            conversation_id=conv.conversation_id,
            strategy="search",
            status="completed",
            max_rounds=500,
            citations=cites,
        )
    )
    await db_session.commit()

    row = (await db_session.execute(select(ResearchState))).scalar_one()
    assert row.citations == cites
```

Add `from sqlalchemy import select` to the test file's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py::test_research_state_citations_column_roundtrips -v`
Expected: FAIL — `ResearchState` has no `citations` attribute / column.

- [ ] **Step 3: Add the `citations` column to the model**

In `src/harbor_clerk/models/research_state.py`, add after the `depth` column line:
```python
    citations: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
```
(`Any`, `JSONB`, and `Mapped`/`mapped_column` are already imported in this file.)

- [ ] **Step 4: Create the Alembic migration**

Create `alembic/versions/0023_research_state_citations.py`:

```python
"""Add research_state.citations — docs that informed the research answer.

The research engine does retrieval internally and never writes tool-result
messages, so the read-time citation extraction in routes/research.py always
produced an empty list. The engine now persists the "docs that informed the
answer" set here instead.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-22

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_state",
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_state", "citations")
```

- [ ] **Step 5: Run test to verify it passes**

The `db_session` fixture runs `alembic upgrade head`, which applies 0023.

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_research_engine.py::test_research_state_citations_column_roundtrips -v`
Expected: PASS.

- [ ] **Step 6: Verify the migration reverts cleanly**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run alembic downgrade 0022 && DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run alembic upgrade head`
Expected: both succeed with no error.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/models/research_state.py alembic/versions/0023_research_state_citations.py tests/test_research_engine.py
git commit -m "fix(research): add research_state.citations column + migration 0023 (#368)"
```

---

## Task 6: Fix 3c — persist citations from the engine, surface them in the API

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`
- Modify: `src/harbor_clerk/api/routes/research.py`
- Modify: `src/harbor_clerk/api/schemas/research.py`
- Test: `tests/test_api_research_citations.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_api_research_citations.py` and read its existing fixtures/helpers (it already builds a research conversation + `ResearchState` and calls the research-detail endpoint). Append a test in the same style:

```python
@pytest.mark.asyncio
async def test_research_detail_surfaces_state_citations(db_session, client, admin_user):
    """result.citations is populated from research_state.citations."""
    from harbor_clerk.models import Conversation
    from harbor_clerk.models.research_state import ResearchState

    conv = Conversation(title="cites")
    db_session.add(conv)
    await db_session.flush()
    cites = [{"doc_id": "d-1", "doc_title": "alpha", "page": 2}]
    db_session.add(
        ResearchState(
            conversation_id=conv.conversation_id,
            strategy="search",
            status="completed",
            max_rounds=500,
            citations=cites,
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/research/{conv.conversation_id}")
    assert resp.status_code == 200
    assert resp.json()["citations"] == cites
```

Match the auth/headers pattern the other tests in this file use (e.g. an `admin_user` token header) — copy it from an existing test in the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_api_research_citations.py::test_research_detail_surfaces_state_citations -v`
Expected: FAIL — `citations` comes back `[]` (route still derives it from tool-result messages).

- [ ] **Step 3: Persist citations from `research_stream`**

In `src/harbor_clerk/llm/research.py`, in `research_stream`, after the round-1 read assigns `evidence_docs` (Task 4), seed a run-level accumulator. Just before the round-1 `_read_evidence` call, add:
```python
                    research_citations: list[dict] = []
```
After the round-1 `passages_text, evidence_docs = await _read_evidence(...)` line, add:
```python
                    research_citations = list(evidence_docs)
```
After the gap-round `gap_passages, gap_evidence_docs = await _read_evidence(...)` line, add:
```python
                        seen_cite_ids = {c["doc_id"] for c in research_citations}
                        research_citations.extend(
                            d for d in gap_evidence_docs if d["doc_id"] not in seen_cite_ids
                        )
```

In the synthesis-completion block, where `state.status = "completed"` is set (research.py ~1368), add `state.citations`:
```python
            state.status = "completed"
            state.notes = notes
            state.citations = research_citations
            state.current_round = step_count
            state.completed_at = datetime.now(UTC)
            state.progress = {"step": step_count}
            await session.commit()
```

Note: `research_citations` is initialized inside the search-strategy branch. If a code path reaches the synthesis block without entering that branch, `state.citations` must still be safe to set — initialize `research_citations: list[dict] = []` once near the top of `research_stream` (with the other run-level locals) instead of inside the branch, so it is always defined.

- [ ] **Step 4: Surface citations from state in the route**

In `src/harbor_clerk/api/routes/research.py`, replace this block:
```python
    tool_results_by_id: dict[str, tuple[str, str]] = {}
    # Citations derived from every tool result emitted during the research
    # turn. Computed at read-time so research detail surfaces citations
    # without needing a new persisted column. ``dedupe_citations`` collapses
    # the same (doc_id, chunk_id) seen across multiple searches and keeps
    # the highest score.
    citations_acc: list[dict] = []
    for m in all_msgs:
        if m.role == "tool" and m.tool_call_id and m.content:
            tool_results_by_id[m.tool_call_id] = (_summarize_tool_result(m.content), m.content)
            citations_acc.extend(extract_citations_from_tool_result(m.content))
    citations = dedupe_citations(citations_acc)
```
with:
```python
    tool_results_by_id: dict[str, tuple[str, str]] = {}
    for m in all_msgs:
        if m.role == "tool" and m.tool_call_id and m.content:
            tool_results_by_id[m.tool_call_id] = (_summarize_tool_result(m.content), m.content)
    # Citations are persisted on research_state by the research engine — the
    # docs whose passages informed the synthesized report. (The engine does
    # retrieval internally and emits no tool-result messages, so the old
    # read-time extraction always produced an empty list.)
    citations = dedupe_citations(state.citations or [])
```

In the same file, the import line:
```python
    from harbor_clerk.llm.citations import dedupe_citations, extract_citations_from_tool_result
```
becomes:
```python
    from harbor_clerk.llm.citations import dedupe_citations
```

- [ ] **Step 5: Fix the stale schema comment**

In `src/harbor_clerk/api/schemas/research.py`, update the comment above the `citations` field (currently "Deduped citation records derived from the tool-result messages of the research turn ... so historical research surfaces citations too.") to:
```python
    # Deduped citation records — the documents whose passages informed the
    # synthesized report, persisted on research_state.citations by the
    # research engine.
    citations: list[dict] = []
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_api_research_citations.py tests/test_research_engine.py -v`
Expected: all PASS.

- [ ] **Step 7: Run ruff + the full research-adjacent suite**

Run: `uv run ruff check src/harbor_clerk/llm/research.py src/harbor_clerk/api/routes/research.py src/harbor_clerk/models/research_state.py && uv run ruff format --check src/harbor_clerk/llm/research.py src/harbor_clerk/api/routes/research.py && DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest tests/test_api_research_citations.py tests/test_llm_citations.py tests/test_research_engine.py -q`
Expected: ruff clean; all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/llm/research.py src/harbor_clerk/api/routes/research.py src/harbor_clerk/api/schemas/research.py tests/test_api_research_citations.py
git commit -m "fix(research): populate result.citations from research_state (#368)"
```

---

## Task 7: Final verification + fresh-eyes review

**Files:** none (verification only)

- [ ] **Step 1: Full lint, format, and test pass**

Run: `uv run ruff check . && uv run ruff format --check . && DATABASE_URL='postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test' uv run pytest -q`
Expected: all clean / PASS. Investigate and fix any failure before proceeding.

- [ ] **Step 2: Fresh-eyes code review**

Per the standing directive, dispatch a `feature-dev:code-reviewer` subagent against the branch tip with a minimal prompt (no focus areas, no carve-outs). Address every finding at confidence ≥ 80 before opening the PR.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin fix/research-engine-defects
```
Open the PR with `gh pr create`, base `main`, titled `fix(research): query expansion, premature abandonment, missing citations (#368)`, body summarizing the three fixes and linking issue #368. Mark the issue's three defects as addressed.

---

## Self-review notes

- **Spec coverage:** Fix 1 → Task 1; Fix 2 → Tasks 2-3; Fix 3 → Tasks 4 (`_read_evidence`), 5 (column + migration), 6 (persist + surface + comment). Testing section → tests in each task + Task 7. Packaging (one PR, fresh-eyes review) → Task 7.
- **`_RETRIEVAL_RELEVANCE_FLOOR`:** provisional 1.0, justified inline from observed score ranges and flagged tunable — a deliberate value, not a placeholder.
- **Type consistency:** `_read_evidence` returns `tuple[str, list[dict]]` (Task 4), consumed as `passages_text, evidence_docs` / `gap_passages, gap_evidence_docs` (Task 4) and accumulated into `research_citations` (Task 6). `_extract_notes` `forceful` kwarg (Task 2) is used by `_extract_notes_with_retry` (Task 3). `_is_no_findings_sentinel` (Task 2) used in Task 3. `research_state.citations` (Task 5) read in Task 6.
