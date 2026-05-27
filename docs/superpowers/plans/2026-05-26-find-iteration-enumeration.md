# kb_find_all Unified Enumeration Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `kb_find_all` MCP tool (plus `find_all_documents` chat-tool mirror) that returns documents deduped by `doc_id` with server-side iteration, a `text_contains` literal-substring filter, three sort modes, and per-model max_results override.

**Architecture:** New `find_all()` function in `src/harbor_clerk/search.py` calls existing `hybrid_search()` with a larger internal k, applies a chunk-level `text_contains` filter, aggregates chunks → docs (GROUP BY doc_id, MAX score), sorts, paginates with offset. MCP and chat layers are thin dispatchers. The `text_contains` filter is added to `hybrid_search` (signature change, opt-in default `None`) so both `kb_search` and `kb_find_all` can use it.

**Tech Stack:** FastAPI + MCP (mcp-streamable-http), SQLAlchemy 2.0 async, asyncpg, pgvector, `pg_trgm` GIN index on `chunks.chunk_text` (new), pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-05-26-find-iteration-enumeration-design.md`

**File map (created or modified):**

- Modify: `src/harbor_clerk/config.py` — add `find_all_max_results_cap: int = 500` setting
- Modify: `src/harbor_clerk/llm/models.py` — add `find_all_default_max_results: int | None = None` to `ModelInfo`
- Create: `alembic/versions/<rev>_chunks_chunk_text_trgm_index.py` — add GIN trgm index
- Modify: `src/harbor_clerk/search.py` — add `text_contains` param to `hybrid_search`; add new `find_all()` function and result type
- Modify: `src/harbor_clerk/search_types.py` — add `FindAllResult` dataclass
- Modify: `src/harbor_clerk/mcp_server.py` — add `kb_find_all` MCP tool; add `text_contains` to `kb_search`
- Modify: `src/harbor_clerk/llm/tools.py` — add `find_all_documents` to `_BASE_CHAT_TOOLS`, `_map_args_find_all`, mapping entries, `execute_tool` dispatch
- Modify: `src/harbor_clerk/llm/chat.py` — plumb per-model `find_all_default_max_results` into tool schema
- Modify: `src/harbor_clerk/llm/research.py` — same plumbing for research mode
- Create: `tests/test_find_all.py` — unit tests for `find_all()` (dedupe, sort, offset, text_contains, scope)
- Create: `tests/test_kb_find_all_mcp.py` — MCP-layer test (signature, response shape, clamp)
- Modify: `tests/test_search_filtered.py` — add `text_contains` cases for `hybrid_search`
- Modify: `tests/test_mcp_tool_descriptions.py` — add `kb_find_all` expectations + assert `text_contains` mentioned in `kb_search` description

**Conventions:**
- All work happens on the existing branch `feat/kb-find-all` (already created, holds the spec commit).
- Tests run via `uv run pytest tests/<file>.py -v`.
- Each task ends with a commit; commit messages follow `feat(<scope>): <what>` per repo convention.
- All new ILIKE call sites use `escape_ilike()` from `src/harbor_clerk/sql_escape.py` (PR-H established this rule).

---

## Task 1: Add `find_all_max_results_cap` setting

**Files:**
- Modify: `src/harbor_clerk/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Read the existing Settings class to find a clean insertion point**

Run: `grep -n "mcp_max_k\|mcp_brief_chars" src/harbor_clerk/config.py`

Note the line number for the MCP settings section. The new field goes adjacent to `mcp_max_k`.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_find_all_max_results_cap_default():
    from harbor_clerk.config import Settings
    s = Settings()
    assert s.find_all_max_results_cap == 500
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_find_all_max_results_cap_default -v`
Expected: FAIL with `AttributeError` or default mismatch.

- [ ] **Step 4: Add the setting**

In `src/harbor_clerk/config.py`, alongside the other MCP search defaults (near `mcp_max_k`):

```python
    # kb_find_all: server-side cap on max_results. Clamps any tool argument
    # above this value. 500 chosen as a hard ceiling — enumeration is
    # bounded by token economy of the model receiving it, not server cost.
    find_all_max_results_cap: int = Field(default=500)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_find_all_max_results_cap_default -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_config.py
git commit -m "feat(config): add find_all_max_results_cap setting (default 500)

Hard server-side cap for the new kb_find_all tool. Clamps tool args
above this value. Part of the kb_find_all rollout — see
docs/superpowers/specs/2026-05-26-find-iteration-enumeration-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add `find_all_default_max_results` to ModelInfo

**Files:**
- Modify: `src/harbor_clerk/llm/models.py`
- Test: `tests/test_llm_models.py` (create if absent)

- [ ] **Step 1: Locate the ModelInfo dataclass**

Run: `grep -n "class ModelInfo\|supports_research" src/harbor_clerk/llm/models.py`

The new field goes after `supports_research` in the dataclass.

- [ ] **Step 2: Write failing test**

Add to `tests/test_llm_models.py`:

```python
from harbor_clerk.llm.models import ModelInfo, MODELS

def test_modelinfo_has_find_all_default_max_results():
    """New optional field; defaults to None on all curated models."""
    info = ModelInfo(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
    )
    assert info.find_all_default_max_results is None

def test_all_curated_models_default_find_all_max_to_none():
    for m in MODELS.values():
        assert m.find_all_default_max_results is None, m.id
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm_models.py -v`
Expected: FAIL (`find_all_default_max_results` does not exist).

- [ ] **Step 4: Add the field to ModelInfo**

In `src/harbor_clerk/llm/models.py`, inside the `ModelInfo` dataclass after `supports_research`:

```python
    # Per-model override for kb_find_all's default max_results. None means
    # "use the tool's static default of 100". The MCP server still clamps
    # at settings.find_all_max_results_cap regardless. This is the per-model
    # experimentation surface — small local models may want 30, larger
    # ones 150. All 8 curated models start at None.
    find_all_default_max_results: int | None = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm_models.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/llm/models.py tests/test_llm_models.py
git commit -m "feat(llm): add find_all_default_max_results to ModelInfo

Per-model experimentation surface for the kb_find_all rollout.
Defaults to None on all curated models (no per-model tuning yet);
chat/research paths use this value to override the tool default.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Alembic migration — `chunks.chunk_text` trgm GIN index

**Files:**
- Create: `alembic/versions/<auto-generated-rev>_chunks_chunk_text_trgm_index.py`
- Test: `tests/test_alembic_chunk_text_trgm_index.py` (create new test)

- [ ] **Step 1: Check current alembic head**

Run: `uv run alembic heads`
Note the current head revision id.

- [ ] **Step 2: Check if index already exists** (defensive)

Run: `grep -rn "chunks.*chunk_text.*trgm\|chunk_text.*gin" alembic/versions/ | head`
Expected: no results (we're adding it). If results appear, the migration is a no-op — stop and confirm with the user.

- [ ] **Step 3: Create the migration scaffold**

Run:
```bash
uv run alembic revision -m "chunks chunk_text trgm gin index"
```

This creates a file like `alembic/versions/<rev>_chunks_chunk_text_trgm_gin_index.py`. Note its filename.

- [ ] **Step 4: Write the migration body**

Replace the generated body with:

```python
"""chunks chunk_text trgm gin index

Adds a GIN trigram index on chunks.chunk_text to support fast
case-insensitive ILIKE filtering used by kb_find_all's text_contains
parameter (and kb_search's same parameter). Without this, text_contains
forces a seq scan on every search.

The pg_trgm extension is already enabled (see 0001_initial.py).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "<rev_from_filename>"
down_revision = "<previous_head_rev>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_chunk_text_trgm "
        "ON public.chunks USING gin (chunk_text public.gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_chunks_chunk_text_trgm")
```

**Note:** `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. Alembic by default wraps migrations in transactions. Add at the TOP of the migration file (above the imports if needed):

```python
"""..."""
from alembic import op

# Tell Alembic to skip the transaction wrapper so CONCURRENTLY can run.
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chunks_chunk_text_trgm "
            "ON public.chunks USING gin (chunk_text public.gin_trgm_ops)"
        )
```

(The `autocommit_block` is the cleaner way — keeps the migration interface stable.)

- [ ] **Step 5: Write the test**

Add to `tests/test_alembic_chunk_text_trgm_index.py`:

```python
"""Verify the chunks.chunk_text trgm index exists after migrate-up."""

import pytest
from sqlalchemy import text


async def test_chunks_chunk_text_trgm_index_present(db_session):
    """ix_chunks_chunk_text_trgm exists on the chunks table."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='chunks' "
            "AND indexname='ix_chunks_chunk_text_trgm'"
        )
    )
    row = result.first()
    assert row is not None, "ix_chunks_chunk_text_trgm not found"


async def test_chunks_chunk_text_trgm_index_is_gin_trgm(db_session):
    """Index uses gin + gin_trgm_ops (so ILIKE %x% is index-eligible)."""
    result = await db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='ix_chunks_chunk_text_trgm'"
        )
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
```

- [ ] **Step 6: Run migrations against test DB**

Run: `uv run pytest tests/test_alembic_chunk_text_trgm_index.py -v`
Expected: PASS (migration is applied automatically by the test fixture).

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/<rev>_chunks_chunk_text_trgm_gin_index.py tests/test_alembic_chunk_text_trgm_index.py
git commit -m "feat(db): add trgm GIN index on chunks.chunk_text

Supports the text_contains ILIKE filter being added to kb_search and
kb_find_all. CONCURRENTLY-created via autocommit_block to avoid
blocking ingest workers on large corpora.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add `text_contains` parameter to `hybrid_search()`

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_search_filtered.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search_filtered.py`:

```python
async def test_hybrid_search_text_contains_filters_chunks(db_session):
    """text_contains restricts candidates to chunks whose text contains
    the substring (case-insensitive). Match the spec wording: 'a document
    is a candidate iff at least one of its chunks contains the substring'."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    # Doc A contains "off-balance-sheet" exactly
    doc_a = Document(title="A", status="active",
                     sha256=b"sha_a_0000000000000000000000000",
                     pipeline_status=PipelineStatus.ready)
    db_session.add(doc_a)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc_a.doc_id, chunk_num=0,
                         chunk_text="The off-balance-sheet entity was disclosed.",
                         language="en"))

    # Doc B is semantically similar but lacks the literal phrase
    doc_b = Document(title="B", status="active",
                     sha256=b"sha_b_0000000000000000000000000",
                     pipeline_status=PipelineStatus.ready)
    db_session.add(doc_b)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc_b.doc_id, chunk_num=0,
                         chunk_text="The unconsolidated subsidiary was disclosed.",
                         language="en"))
    await db_session.flush()

    # No text_contains: both candidates eligible
    result = await hybrid_search(db_session, "entity disclosed", k=10)
    assert {h.doc_id for h in result.hits} == {doc_a.doc_id, doc_b.doc_id}

    # With text_contains="off-balance-sheet": only Doc A
    result = await hybrid_search(
        db_session, "entity disclosed", k=10,
        text_contains="off-balance-sheet",
    )
    assert {h.doc_id for h in result.hits} == {doc_a.doc_id}


async def test_hybrid_search_text_contains_case_insensitive(db_session):
    """Case differences in the substring match."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    doc = Document(title="A", status="active",
                   sha256=b"sha_x_0000000000000000000000000",
                   pipeline_status=PipelineStatus.ready)
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                         chunk_text="The OFF-BALANCE-SHEET entity.",
                         language="en"))
    await db_session.flush()

    result = await hybrid_search(
        db_session, "entity", k=10, text_contains="off-balance-sheet",
    )
    assert result.hits and result.hits[0].doc_id == doc.doc_id


async def test_hybrid_search_text_contains_escapes_special_chars(db_session):
    """% and _ in the substring are matched literally, not as wildcards."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    doc1 = Document(title="A", status="active",
                    sha256=b"sha_p_0000000000000000000000000",
                    pipeline_status=PipelineStatus.ready)
    db_session.add(doc1)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc1.doc_id, chunk_num=0,
                         chunk_text="Revenue grew 50% YoY.", language="en"))

    doc2 = Document(title="B", status="active",
                    sha256=b"sha_q_0000000000000000000000000",
                    pipeline_status=PipelineStatus.ready)
    db_session.add(doc2)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc2.doc_id, chunk_num=0,
                         chunk_text="Revenue grew significantly.", language="en"))
    await db_session.flush()

    # "50%" should match only Doc 1; the % must NOT act as a wildcard
    result = await hybrid_search(
        db_session, "revenue", k=10, text_contains="50%",
    )
    assert {h.doc_id for h in result.hits} == {doc1.doc_id}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_search_filtered.py::test_hybrid_search_text_contains_filters_chunks -v`
Expected: FAIL with `TypeError: unexpected keyword 'text_contains'`.

- [ ] **Step 3: Add `text_contains` to `hybrid_search` signature**

In `src/harbor_clerk/search.py`, modify the `hybrid_search` signature (currently line 49) to add `text_contains` as a kw-only arg:

```python
async def hybrid_search(
    session: AsyncSession,
    query: str,
    k: int = 10,
    doc_id: uuid.UUID | None = None,
    offset: int = 0,
    *,
    doc_ids: list[uuid.UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
    text_contains: str | None = None,
) -> SearchResult:
```

- [ ] **Step 4: Apply the `text_contains` filter inside `hybrid_search`**

Locate the section in `hybrid_search` that builds `scope_filters` (the chunk-level filters list). Add at the end of that block, before the filter is applied to candidate generation:

```python
    if text_contains:
        from harbor_clerk.sql_escape import escape_ilike
        escaped = escape_ilike(text_contains)
        scope_filters.append(
            Chunk.chunk_text.ilike(f"%{escaped}%", escape="\\")
        )
```

(`escape_ilike` returns a `\`-escaped string; the `escape="\\"` arg tells SQLAlchemy/Postgres that backslash is the escape character so `\%` and `\_` are literals.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_search_filtered.py -v`
Expected: 3 new tests PASS, all existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/search.py tests/test_search_filtered.py
git commit -m "feat(search): add text_contains filter to hybrid_search

Case-insensitive literal-substring filter on chunks.chunk_text,
backed by the trgm GIN index. A doc is eligible iff at least one
of its chunks contains the substring. Special chars (% _) escaped
via shared escape_ilike() helper.

Closes the Shape-1 gap from the find-iteration spec: 'find emails
containing X' can now match by literal phrase rather than rely on
relevance ranking of the phrase.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Add `FindAllResult` dataclass

**Files:**
- Modify: `src/harbor_clerk/search_types.py`
- Test: covered by Task 6 (no separate test file — dataclass is just a shape).

- [ ] **Step 1: Inspect the existing search types file**

Run: `cat src/harbor_clerk/search_types.py`
Note the `SearchResult` and `SearchHit` shapes.

- [ ] **Step 2: Add the new result type**

Append to `src/harbor_clerk/search_types.py`:

```python
@dataclass
class FindAllHit:
    """One row in a FindAllResult — a document, with its best chunk's score."""
    doc_id: uuid.UUID
    doc_title: str
    mime_type: str
    language: str | None
    score: float                    # max chunk score for this doc
    ingested_at: datetime           # documents.created_at
    page_range: str | None          # e.g. "1-12"; None if unknown
    top_chunk_id: uuid.UUID | None  # for presentation="full"
    top_chunk_text: str | None      # for presentation="full"
    top_chunk_page: int | None
    top_chunk_heading: str | None


@dataclass
class FindAllResult:
    """Result of find_all() — doc-level deduped, sortable, paginated."""
    hits: list[FindAllHit]
    total_matches: int    # post-dedupe, post-filter count of unique docs
    offset: int           # echo of the input
    truncated: bool       # total_matches > offset + len(hits)
    sort_by: str          # echo of the input
    presentation: str     # echo of the input
```

- [ ] **Step 3: Commit**

```bash
git add src/harbor_clerk/search_types.py
git commit -m "feat(search): add FindAllResult + FindAllHit types

Result types for the new find_all() function. FindAllHit is doc-level
(not chunk-level like SearchHit) and carries an optional top_chunk_*
payload for presentation='full' callers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Implement `find_all()` — query + doc-level dedup (no sort/offset yet)

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_find_all.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_find_all.py`:

```python
"""Unit tests for find_all() — doc-level enumeration."""

import pytest
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import find_all


async def _seed_docs_with_chunks(db_session, n_docs: int, chunks_per_doc: int,
                                 text: str = "off-balance-sheet entity"):
    """Seed n_docs documents, each with chunks_per_doc chunks all containing `text`."""
    doc_ids = []
    for i in range(n_docs):
        sha = f"sha_seed_{i:020d}".encode()
        doc = Document(title=f"Doc {i}", status="active", sha256=sha,
                       pipeline_status=PipelineStatus.ready,
                       mime_type="text/plain")
        db_session.add(doc)
        await db_session.flush()
        for j in range(chunks_per_doc):
            db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=j,
                                 chunk_text=f"{text} chunk {j} of doc {i}",
                                 language="en"))
        doc_ids.append(doc.doc_id)
    await db_session.flush()
    return doc_ids


async def test_find_all_dedupes_by_doc_id(db_session):
    """A doc with 5 matching chunks shows up exactly ONCE in results."""
    doc_ids = await _seed_docs_with_chunks(db_session, n_docs=3, chunks_per_doc=5)

    result = await find_all(db_session, "off-balance-sheet", max_results=10)

    assert len(result.hits) == 3, f"expected 3 docs, got {len(result.hits)}"
    assert {h.doc_id for h in result.hits} == set(doc_ids)
    assert result.total_matches == 3
    assert result.truncated is False


async def test_find_all_total_matches_unaffected_by_max_results(db_session):
    """total_matches reflects ALL matches, even when max_results truncates."""
    await _seed_docs_with_chunks(db_session, n_docs=10, chunks_per_doc=2)

    result = await find_all(db_session, "off-balance-sheet", max_results=3)

    assert len(result.hits) == 3
    assert result.total_matches == 10
    assert result.truncated is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_find_all.py -v`
Expected: FAIL — `find_all` does not exist.

- [ ] **Step 3: Implement minimal `find_all()`**

In `src/harbor_clerk/search.py`, add a new function below `hybrid_search`:

```python
async def find_all(
    session: AsyncSession,
    query: str,
    *,
    max_results: int = 100,
    offset: int = 0,
    sort_by: str = "relevance",
    text_contains: str | None = None,
    doc_id: uuid.UUID | None = None,
    doc_ids: list[uuid.UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
    presentation: str = "brief",
) -> "FindAllResult":
    """Doc-level enumeration. Aggregates chunks → docs, sorts, paginates.

    Reuses hybrid_search for candidate generation, then projects chunk-level
    hits to doc-level with score = max(chunk_score per doc). See
    docs/superpowers/specs/2026-05-26-find-iteration-enumeration-design.md.
    """
    from harbor_clerk.search_types import FindAllHit, FindAllResult

    # Pull a generous candidate pool so dedupe doesn't starve the result set.
    # Internal k = 5x the request, capped at 1000.
    internal_k = min(max_results * 5, 1000)

    inner = await hybrid_search(
        session, query, k=internal_k, offset=0,
        doc_id=doc_id, doc_ids=doc_ids, after=after, before=before,
        language=language, mime_type=mime_type,
        metadata_filter=metadata_filter, text_contains=text_contains,
    )

    # Group hits by doc_id; keep the max-score chunk per doc.
    by_doc: dict[uuid.UUID, SearchHit] = {}
    for hit in inner.hits:
        existing = by_doc.get(hit.doc_id)
        if existing is None or hit.score > existing.score:
            by_doc[hit.doc_id] = hit

    total_matches = len(by_doc)
    docs_sorted = sorted(by_doc.values(), key=lambda h: h.score, reverse=True)

    # Slice offset..offset+max_results
    window = docs_sorted[offset : offset + max_results]

    # Build FindAllHit rows — needs Document.mime_type / created_at, which
    # the SearchHit doesn't carry. Fetch in one round-trip.
    if window:
        doc_id_list = [h.doc_id for h in window]
        rows = (await session.execute(
            select(Document.doc_id, Document.title, Document.mime_type,
                   Document.created_at).where(Document.doc_id.in_(doc_id_list))
        )).all()
        doc_meta = {r.doc_id: r for r in rows}
    else:
        doc_meta = {}

    hits: list[FindAllHit] = []
    for sh in window:
        meta = doc_meta.get(sh.doc_id)
        if meta is None:
            continue
        hits.append(FindAllHit(
            doc_id=sh.doc_id,
            doc_title=meta.title or sh.doc_title,
            mime_type=meta.mime_type or "",
            language=sh.language,
            score=sh.score,
            ingested_at=meta.created_at,
            page_range=str(sh.page) if sh.page else None,
            top_chunk_id=sh.chunk_id if presentation == "full" else None,
            top_chunk_text=sh.chunk_text if presentation == "full" else None,
            top_chunk_page=sh.page if presentation == "full" else None,
            top_chunk_heading=sh.heading if presentation == "full" else None,
        ))

    return FindAllResult(
        hits=hits,
        total_matches=total_matches,
        offset=offset,
        truncated=total_matches > offset + len(hits),
        sort_by=sort_by,           # sorting logic added in next task
        presentation=presentation,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_find_all.py -v`
Expected: PASS for both new tests. Existing tests should still pass.

Run: `uv run pytest tests/test_search_filtered.py tests/test_hybrid_search_with_rerank.py -v`
Expected: ALL PASS (no regressions in hybrid_search).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/search.py tests/test_find_all.py
git commit -m "feat(search): add find_all() — doc-level enumeration v1

Reuses hybrid_search at higher internal k, dedupes by doc_id keeping
max chunk score per doc. Initial cut: relevance sort only, offset works
but sort_by is not yet wired (lands in next commit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `find_all()` — sort_by modes

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_find_all.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_find_all.py`:

```python
import datetime as dt

async def test_find_all_sort_by_date_desc(db_session):
    """sort_by='date_desc' returns docs newest-first."""
    from harbor_clerk.search import find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    # Seed 3 docs with explicit created_at timestamps
    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}", status="active",
            sha256=f"sha_dt_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet",
                            max_results=10, sort_by="date_desc")

    # Expected order: 5 days, 30 days, 60 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: x[1])]
    assert got_order == expected_order


async def test_find_all_sort_by_date_asc(db_session):
    """sort_by='date_asc' returns docs oldest-first."""
    from harbor_clerk.search import find_all
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus

    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}", status="active",
            sha256=f"sha_da_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet",
                            max_results=10, sort_by="date_asc")

    # Expected order: 60 days, 30 days, 5 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: -x[1])]
    assert got_order == expected_order


async def test_find_all_sort_by_invalid_raises(db_session):
    """Invalid sort_by raises ValueError."""
    from harbor_clerk.search import find_all
    with pytest.raises(ValueError, match="sort_by"):
        await find_all(db_session, "query", sort_by="title")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_find_all.py -v -k "sort_by"`
Expected: All 3 new tests FAIL (sort_by is ignored today).

- [ ] **Step 3: Implement sort_by**

In `src/harbor_clerk/search.py`, replace the `docs_sorted = sorted(...)` line in `find_all` with:

```python
    _VALID_SORTS = {"relevance", "date_desc", "date_asc"}
    if sort_by not in _VALID_SORTS:
        raise ValueError(
            f"sort_by must be one of {sorted(_VALID_SORTS)}, got {sort_by!r}"
        )

    # Sort the doc-level dict. For date sorts, fetch created_at up front.
    if sort_by in ("date_desc", "date_asc"):
        date_rows = (await session.execute(
            select(Document.doc_id, Document.created_at)
            .where(Document.doc_id.in_(list(by_doc.keys())))
        )).all()
        date_map = {r.doc_id: r.created_at for r in date_rows}
        docs_sorted = sorted(
            by_doc.values(),
            key=lambda h: date_map.get(h.doc_id, dt.datetime.min.replace(tzinfo=dt.UTC)),
            reverse=(sort_by == "date_desc"),
        )
    else:  # relevance (default)
        docs_sorted = sorted(by_doc.values(), key=lambda h: h.score, reverse=True)
```

(Make sure `import datetime as dt` is at the top of the file; if not, add it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_find_all.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/search.py tests/test_find_all.py
git commit -m "feat(search): find_all sort_by — relevance | date_desc | date_asc

Adds explicit sort selection. Date sorts do one extra SELECT for
documents.created_at across the deduped doc set (small, deduped set).
Invalid sort raises ValueError surfaced to the caller.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `find_all()` — offset semantics + presentation="full"

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_find_all.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_find_all.py`:

```python
async def test_find_all_offset_paginates_stably(db_session):
    """Calling find_all twice with consecutive offsets yields the full set
    in stable order with no overlaps."""
    from harbor_clerk.search import find_all
    await _seed_docs_with_chunks(db_session, n_docs=12, chunks_per_doc=1)

    page1 = await find_all(db_session, "off-balance-sheet",
                           max_results=5, offset=0)
    page2 = await find_all(db_session, "off-balance-sheet",
                           max_results=5, offset=5)

    assert len(page1.hits) == 5
    assert len(page2.hits) == 5
    overlap = {h.doc_id for h in page1.hits} & {h.doc_id for h in page2.hits}
    assert overlap == set(), f"unexpected overlap: {overlap}"
    assert page1.total_matches == page2.total_matches == 12
    assert page1.truncated is True
    assert page2.truncated is True   # 10 < 12


async def test_find_all_presentation_brief_omits_chunk_text(db_session):
    """presentation='brief' (default) leaves top_chunk_text None."""
    from harbor_clerk.search import find_all
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet",
                            max_results=10, presentation="brief")

    for h in result.hits:
        assert h.top_chunk_text is None
        assert h.top_chunk_id is None


async def test_find_all_presentation_full_includes_chunk_text(db_session):
    """presentation='full' populates the top_chunk_* fields."""
    from harbor_clerk.search import find_all
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet",
                            max_results=10, presentation="full")

    for h in result.hits:
        assert h.top_chunk_text is not None
        assert h.top_chunk_id is not None


async def test_find_all_presentation_full_clamps_max_results(db_session):
    """presentation='full' clamps max_results to 30 (token economy).

    The clamp is applied server-side and total_matches still reflects
    the true count; only `returned` (len(hits)) is bounded.
    """
    from harbor_clerk.search import find_all
    await _seed_docs_with_chunks(db_session, n_docs=50, chunks_per_doc=1)

    # Request 100 with presentation=full → server clamps to 30
    result = await find_all(db_session, "off-balance-sheet",
                            max_results=100, presentation="full")

    assert len(result.hits) <= 30
    assert result.total_matches == 50
    assert result.truncated is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_find_all.py -v -k "offset or presentation"`
Expected: offset test PASSES (offset already works); presentation_brief PASSES; presentation_full FAILS (returns None text because we didn't wire the flag correctly); presentation_full_clamps FAILS (no clamp in place yet).

- [ ] **Step 3: Add the presentation=full clamp**

In `find_all`, before computing `internal_k`, add:

```python
    # presentation='full' payloads include chunk text, ~500 chars/doc.
    # Clamp max_results to keep total payload under ~20 KB.
    _FULL_MAX_RESULTS = 30
    if presentation == "full" and max_results > _FULL_MAX_RESULTS:
        max_results = _FULL_MAX_RESULTS
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_find_all.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/search.py tests/test_find_all.py
git commit -m "feat(search): find_all offset pagination + full-presentation clamp

Offset semantics: stable order across pages, truncated stays true
while offset+returned < total_matches. presentation='full' inlines
the top chunk per doc and clamps max_results to 30 server-side to
keep the payload under ~20 KB.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Add `kb_find_all` MCP tool

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Test: `tests/test_kb_find_all_mcp.py` (create) + `tests/test_mcp_tool_descriptions.py`

- [ ] **Step 1: Locate the kb_search MCP tool definition**

Run: `grep -n "^async def kb_search\|^async def kb_batch_search" src/harbor_clerk/mcp_server.py`

The new tool goes alongside these, with a matching `@mcp.tool` decorator and docstring.

- [ ] **Step 2: Write failing tests**

Create `tests/test_kb_find_all_mcp.py`:

```python
"""kb_find_all MCP tool — signature, response shape, server-side clamp."""

import json
import pytest


async def test_kb_find_all_returns_dedupe_by_doc_id(db_session, monkeypatch):
    """End-to-end through the MCP tool path."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.mcp_server import kb_find_all

    # Seed 3 docs with 4 matching chunks each
    for i in range(3):
        doc = Document(title=f"D{i}", status="active",
                       sha256=f"sha_mcp_{i:020d}".encode(),
                       pipeline_status=PipelineStatus.ready,
                       mime_type="text/plain")
        db_session.add(doc)
        await db_session.flush()
        for j in range(4):
            db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=j,
                                 chunk_text="off-balance-sheet entity",
                                 language="en"))
    await db_session.flush()

    raw = await kb_find_all(query="off-balance-sheet", max_results=10)
    payload = json.loads(raw)

    assert len(payload["results"]) == 3
    assert payload["total_matches"] == 3
    assert payload["truncated"] is False
    assert payload["sort_by"] == "relevance"
    assert payload["presentation"] == "brief"
    seen = {r["doc_id"] for r in payload["results"]}
    assert len(seen) == 3


async def test_kb_find_all_clamps_at_settings_cap(db_session, monkeypatch):
    """max_results above settings.find_all_max_results_cap is clamped."""
    from harbor_clerk.mcp_server import kb_find_all
    from harbor_clerk.config import get_settings
    # Lower the cap to 10 for this test
    monkeypatch.setattr(get_settings(), "find_all_max_results_cap", 10)

    raw = await kb_find_all(query="x", max_results=99999)
    payload = json.loads(raw)
    # The returned set will be capped (regardless of corpus size).
    assert len(payload["results"]) <= 10
```

- [ ] **Step 3: Add expectation to the tool-description test suite**

Modify `tests/test_mcp_tool_descriptions.py` — find the list of expected tool names and add `kb_find_all`:

```python
EXPECTED_TOOLS = {
    "kb_search", "kb_batch_search", "kb_read_passages", "kb_expand_context",
    "kb_get_document", "kb_list_recent", "kb_corpus_overview",
    "kb_document_outline", "kb_find_related", "kb_entity_search",
    "kb_entity_overview", "kb_entity_cooccurrence", "kb_read_document",
    "kb_verify_identifier", "kb_documents_by_date", "kb_system_health",
    "kb_ingest_status", "kb_reprocess",
    "kb_find_all",   # NEW
}
```

(Adjust to match the actual set in the file — the exact set is whatever already passes there.)

Add a docstring-content test:

```python
def test_kb_find_all_docstring_signals_enumeration_intent():
    """kb_find_all's description must guide model to use it for list/find-all."""
    from harbor_clerk.mcp_server import kb_find_all
    desc = (kb_find_all.__doc__ or "").lower()
    # Must distinguish from kb_search
    assert "list" in desc or "enumerate" in desc or "find all" in desc
    assert "dedupe" in desc or "deduplicat" in desc or "per document" in desc
    # Must mention text_contains
    assert "text_contains" in desc
    # Must mention sort_by options
    assert "date_desc" in desc and "date_asc" in desc


def test_kb_search_docstring_mentions_text_contains():
    """text_contains is shared with kb_search — its docstring must mention it."""
    from harbor_clerk.mcp_server import kb_search
    desc = (kb_search.__doc__ or "").lower()
    assert "text_contains" in desc
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_kb_find_all_mcp.py tests/test_mcp_tool_descriptions.py -v`
Expected: FAIL (kb_find_all doesn't exist; docstring doesn't mention text_contains).

- [ ] **Step 5: Implement the MCP tool**

In `src/harbor_clerk/mcp_server.py`, alongside the other `kb_*` async functions, add:

```python
@mcp.tool(structured_output=False)
async def kb_find_all(
    query: str,
    *,
    text_contains: str | None = None,
    max_results: int = 100,
    offset: int = 0,
    presentation: str = "brief",
    sort_by: str = "relevance",
    after: str | None = None,
    before: str | None = None,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> str:
    """Enumerate documents matching a query — deduped by document, with
    optional literal-substring filtering. Use this when the question asks
    to LIST or FIND ALL matching documents (rather than the single best hit).

    Differences from kb_search:
      - Returns DOCUMENTS deduped by doc_id (not chunks). Each doc appears
        once with its best chunk's score.
      - Server iterates internally — you get all matches (up to max_results)
        in ONE call rather than paginating with kb_search yourself.
      - Adds the text_contains filter for literal-substring matching.

    When to use it:
      - The user says "list all", "find every", "show me all", "enumerate",
        or similar.
      - The question names a literal phrase that must appear ("emails
        containing X", "contracts that mention Y") — pass text_contains=X
        so semantic ranking doesn't dilute exact matches.

    When NOT to use it:
      - You just want the single best match (use kb_search with k=1).
      - You want to triangulate from multiple angles (use kb_batch_search).

    Parameters:
      query: relevance query (FTS + vector). Required even when using
        text_contains — relevance still orders the result set.
      text_contains: optional case-insensitive literal substring on chunk
        text. A doc is eligible iff at least one of its chunks contains the
        substring. Special chars (% _) are matched literally.
      max_results: cap on documents returned (default 100; server clamps
        at settings.find_all_max_results_cap, default 500).
      offset: pagination offset over the sorted result set. Default 0.
      presentation: "brief" (default; title + score + page range, no chunk
        text) or "full" (adds the top chunk text per doc; clamps
        max_results to 30 to keep the payload bounded).
      sort_by: "relevance" (default, by max chunk score per doc),
        "date_desc" (newest first by ingested_at), or "date_asc".

    All other filters (after, before, doc_id, doc_ids, language, mime_type,
    metadata_filter) work the same as kb_search.

    Response:
      results: list of {doc_id, doc_title, mime_type, language, score,
        ingested_at, page_range, top_chunk_* (if presentation=full)}
      total_matches: total docs matching (post-dedupe, scope-aware)
      returned: len(results)
      offset: echoed back
      truncated: true if total_matches > offset + returned
      sort_by, presentation: echoed back
    """
    settings = get_settings()
    # Clamp max_results at the server cap
    max_results = max(1, min(max_results, settings.find_all_max_results_cap))

    # Parse date strings if provided
    after_dt = _parse_iso(after) if after else None
    before_dt = _parse_iso(before) if before else None

    # Convert string doc IDs to uuids
    doc_id_u = uuid.UUID(doc_id) if doc_id else None
    doc_ids_u = [uuid.UUID(d) for d in doc_ids] if doc_ids else None

    # Apply scope filter from API key
    scope = _get_scope_from_request()  # existing helper used by kb_search
    if scope and scope.folder_ids:
        # Translate folder scope to doc_ids by querying watched_files
        # (use the same helper kb_search uses)
        scoped_doc_ids = await _resolve_folder_scope_to_doc_ids(
            scope.folder_ids,
        )
        # AND with caller-supplied doc_ids if any
        if doc_ids_u:
            doc_ids_u = list(set(doc_ids_u) & set(scoped_doc_ids))
        else:
            doc_ids_u = scoped_doc_ids

    async with get_session() as session:
        result = await find_all(
            session, query,
            max_results=max_results,
            offset=offset,
            sort_by=sort_by,
            text_contains=text_contains,
            doc_id=doc_id_u,
            doc_ids=doc_ids_u,
            after=after_dt, before=before_dt,
            language=language, mime_type=mime_type,
            metadata_filter=metadata_filter,
            presentation=presentation,
        )

    payload = {
        "results": [
            {
                "doc_id": str(h.doc_id),
                "doc_title": h.doc_title,
                "mime_type": h.mime_type,
                "language": h.language,
                "score": round(h.score, 4),
                "ingested_at": h.ingested_at.isoformat(),
                "page_range": h.page_range,
                **(
                    {"top_chunk": {
                        "chunk_id": str(h.top_chunk_id),
                        "text": h.top_chunk_text,
                        "page": h.top_chunk_page,
                        "heading": h.top_chunk_heading,
                    }}
                    if presentation == "full" and h.top_chunk_id else {}
                ),
            }
            for h in result.hits
        ],
        "total_matches": result.total_matches,
        "returned": len(result.hits),
        "offset": result.offset,
        "truncated": result.truncated,
        "sort_by": result.sort_by,
        "presentation": result.presentation,
    }
    return json.dumps(payload)
```

**Note on existing helpers:** `_parse_iso`, `_get_scope_from_request`, `_resolve_folder_scope_to_doc_ids`, `get_session`, and the `@mcp.tool` decorator pattern all exist already — copy the pattern from `kb_search` (which already does the date parsing + scope resolution dance). If a helper name differs (e.g. `_get_scope` instead of `_get_scope_from_request`), use whatever `kb_search` actually uses.

- [ ] **Step 6: Add `text_contains` to `kb_search` MCP tool**

In the same file, locate `kb_search`'s signature and docstring. Add the `text_contains` parameter (mirror what kb_find_all has) and pass it through to `hybrid_search`. Add a one-sentence docstring section:

```
      text_contains: optional case-insensitive literal-substring filter
        on chunk text. Use when the question names a literal phrase that
        must appear ("documents mentioning X"). Special chars (% _) are
        matched literally. Combine with `query` for relevance ranking
        among the literal-match subset.
```

- [ ] **Step 7: Run all tests**

Run: `uv run pytest tests/test_kb_find_all_mcp.py tests/test_mcp_tool_descriptions.py tests/test_mcp_tools.py -v`
Expected: ALL PASS.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_kb_find_all_mcp.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): add kb_find_all + text_contains on kb_search

kb_find_all is a doc-deduped enumeration tool with server-side iteration,
text_contains literal-substring filter, sort_by relevance|date_desc|date_asc,
and offset pagination. Server clamps max_results at
settings.find_all_max_results_cap (default 500).

text_contains is also added to kb_search as a cheap win for the same
shape of question — uses the new chunks.chunk_text trgm GIN index.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Add `find_all_documents` chat tool + executor wiring

**Files:**
- Modify: `src/harbor_clerk/llm/tools.py`
- Test: `tests/test_chat_tools.py` (create if absent, or extend existing chat-tool test)

- [ ] **Step 1: Inspect existing chat tool patterns**

Run: `grep -n "search_documents\|_map_args_search\|_TOOL_NAME_MAP\|execute_tool" src/harbor_clerk/llm/tools.py | head -20`

Find the `_BASE_CHAT_TOOLS` list (~line 22), the `_map_args_*` functions (~line 337+), the `_TOOL_NAME_MAP` dict (~line 433), and `execute_tool` (~line 548). The new tool needs entries in all four.

- [ ] **Step 2: Write failing test**

Create `tests/test_chat_tools.py` (or extend existing):

```python
"""find_all_documents chat tool — wiring + arg mapping."""

import pytest


def test_find_all_documents_in_base_chat_tools():
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS
    names = {t["function"]["name"] for t in _BASE_CHAT_TOOLS}
    assert "find_all_documents" in names


def test_find_all_documents_schema_has_text_contains():
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS
    fa = next(t for t in _BASE_CHAT_TOOLS
              if t["function"]["name"] == "find_all_documents")
    props = fa["function"]["parameters"]["properties"]
    assert "query" in props
    assert "text_contains" in props
    assert "max_results" in props
    assert "sort_by" in props


def test_map_args_find_all_passes_through_known_keys():
    from harbor_clerk.llm.tools import _map_args_find_all
    out = _map_args_find_all({
        "query": "X",
        "text_contains": "Y",
        "max_results": 50,
        "sort_by": "date_desc",
    })
    assert out["query"] == "X"
    assert out["text_contains"] == "Y"
    assert out["max_results"] == 50
    assert out["sort_by"] == "date_desc"


def test_map_args_find_all_drops_unknown_keys():
    """Unknown keys (e.g. `k` left over from search_documents) are filtered."""
    from harbor_clerk.llm.tools import _map_args_find_all
    out = _map_args_find_all({"query": "X", "k": 99})
    assert "k" not in out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_chat_tools.py -v`
Expected: FAIL.

- [ ] **Step 4: Add the chat tool to `_BASE_CHAT_TOOLS`**

In `src/harbor_clerk/llm/tools.py`, inside the `_BASE_CHAT_TOOLS` list (alongside `search_documents`), add:

```python
    {
        "type": "function",
        "function": {
            "name": "find_all_documents",
            "description": (
                "Enumerate all documents matching a query. Use for 'list all' "
                "/ 'find every' / 'show me all' questions. Returns DOCUMENTS "
                "(not chunks) deduped by doc_id with server-side iteration — "
                "you get all matches in one call. Optional text_contains for "
                "literal-substring filtering ('emails containing X')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Relevance query (FTS + vector).",
                    },
                    "text_contains": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive literal substring. "
                            "Document is eligible iff at least one chunk "
                            "contains this phrase. Special chars (% _) match "
                            "literally."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on documents returned (default 100).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0).",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "date_desc", "date_asc"],
                        "description": "Sort order. Default 'relevance'.",
                    },
                    "presentation": {
                        "type": "string",
                        "enum": ["brief", "full"],
                        "description": (
                            "'brief' (default) returns title + score per doc. "
                            "'full' includes top chunk text (max 30 docs)."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
```

- [ ] **Step 5: Add `_map_args_find_all`**

In the same file, alongside the other `_map_args_*` functions:

```python
def _map_args_find_all(args: dict) -> dict:
    """Map chat-tool args to find_all() kwargs. Drops unknown keys."""
    allowed = {
        "query", "text_contains", "max_results", "offset",
        "sort_by", "presentation",
    }
    return {k: v for k, v in args.items() if k in allowed}
```

- [ ] **Step 6: Register the tool in the name + arg-map dicts**

In the `_TOOL_NAME_MAP` dict (~line 433), add:

```python
    "find_all_documents": ("kb_find_all", _map_args_find_all),
```

In the research-tools mapping (~line 543) — `_TOOL_NAME_MAP_RESEARCH` or equivalent — add the same entry so research mode can use it.

- [ ] **Step 7: Wire `execute_tool` to call `find_all`**

In `execute_tool` (~line 548), the dispatch table maps `kb_find_all` to a function call. Follow the existing pattern (search_documents → kb_search → calls `mcp_server.kb_search` indirectly). The cleanest path: have the chat-tool dispatcher call the same MCP function `kb_find_all` (importing from `mcp_server`). This is exactly what `search_documents` does — look at how `execute_tool` calls `kb_search` and mirror that exactly.

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/test_chat_tools.py -v`
Expected: ALL PASS.

Run full test suite to catch regressions:
`uv run pytest tests/ -x --ignore=tests/integration`
Expected: ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/llm/tools.py tests/test_chat_tools.py
git commit -m "feat(chat): add find_all_documents chat tool

Parallel to kb_find_all for local-model chat path. Same shape (query +
text_contains + max_results + sort_by + offset + presentation),
simplified description aimed at 4-8B local models. Routes through the
same kb_find_all MCP function — single source of truth.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Plumb per-model `find_all_default_max_results` into chat + research

**Files:**
- Modify: `src/harbor_clerk/llm/tools.py` (likely the `_apply_search_settings` or `get_chat_tools` function)
- Modify: `src/harbor_clerk/llm/chat.py` (callers of `get_chat_tools()`)
- Modify: `src/harbor_clerk/llm/research.py` (callers of `get_research_tools()`)
- Test: `tests/test_chat_tools.py`

- [ ] **Step 1: Inspect `_apply_search_settings` for the pattern**

Run: `grep -n "_apply_search_settings\|get_chat_tools\|get_research_tools" src/harbor_clerk/llm/tools.py | head`

`_apply_search_settings` already mutates the `search_documents` schema based on settings. We'll add a similar `_apply_find_all_settings` that sets the `max_results` default field.

- [ ] **Step 2: Write failing test**

Append to `tests/test_chat_tools.py`:

```python
def test_get_chat_tools_uses_model_find_all_default(monkeypatch):
    """When called with a model whose find_all_default_max_results is set,
    the find_all_documents schema's max_results.default matches."""
    from harbor_clerk.llm.tools import get_chat_tools
    from harbor_clerk.llm.models import ModelInfo

    custom_model = ModelInfo(
        id="custom-test", name="Custom", huggingface_repo="x", filename="x.gguf",
        size_bytes=1, context_window=4096, supports_tools=True,
        find_all_default_max_results=30,
    )

    tools = get_chat_tools(model=custom_model)
    fa = next(t for t in tools if t["function"]["name"] == "find_all_documents")
    assert fa["function"]["parameters"]["properties"]["max_results"]["default"] == 30


def test_get_chat_tools_falls_back_to_100_when_model_default_none():
    """find_all_default_max_results=None ⇒ schema uses tool default of 100."""
    from harbor_clerk.llm.tools import get_chat_tools
    from harbor_clerk.llm.models import ModelInfo

    null_model = ModelInfo(
        id="null-test", name="Null", huggingface_repo="x", filename="x.gguf",
        size_bytes=1, context_window=4096, supports_tools=True,
        find_all_default_max_results=None,
    )
    tools = get_chat_tools(model=null_model)
    fa = next(t for t in tools if t["function"]["name"] == "find_all_documents")
    assert fa["function"]["parameters"]["properties"]["max_results"]["default"] == 100
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_chat_tools.py -v -k "find_all_default"`
Expected: FAIL.

- [ ] **Step 4: Modify `get_chat_tools` to accept a `model` kwarg**

In `src/harbor_clerk/llm/tools.py`:

```python
def get_chat_tools(*, model: ModelInfo | None = None) -> list[dict]:
    """Return chat tool schemas, optionally tuned for a specific model."""
    tools = copy.deepcopy(_BASE_CHAT_TOOLS)
    settings = get_settings()
    _apply_search_settings(tools, ...)   # existing
    _apply_find_all_settings(tools, model=model)   # NEW
    return tools


def _apply_find_all_settings(tools: list[dict], *, model: "ModelInfo | None") -> None:
    """Set the find_all_documents.max_results default per active model."""
    default_max = 100
    if model is not None and model.find_all_default_max_results is not None:
        default_max = model.find_all_default_max_results

    for tool in tools:
        if tool["function"]["name"] != "find_all_documents":
            continue
        tool["function"]["parameters"]["properties"]["max_results"]["default"] = default_max
```

Add the same logic to `get_research_tools` (mirror for research mode).

- [ ] **Step 5: Update call sites in chat.py / research.py**

In `src/harbor_clerk/llm/chat.py`, find every call to `get_chat_tools()` and update to pass the active model:

```python
from harbor_clerk.llm.models import get_active_model  # whatever the existing helper is

tools = get_chat_tools(model=get_active_model())
```

(If `get_active_model` isn't the helper name, find the equivalent — every existing chat path already looks up the active model for other reasons.)

Same change in `src/harbor_clerk/llm/research.py` for `get_research_tools()`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_chat_tools.py -v`
Expected: ALL PASS.

Run regression suite:
`uv run pytest tests/ -x --ignore=tests/integration`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/llm/tools.py src/harbor_clerk/llm/chat.py src/harbor_clerk/llm/research.py tests/test_chat_tools.py
git commit -m "feat(chat): per-model find_all_documents.max_results default

Chat and research tool builders accept the active ModelInfo and use its
find_all_default_max_results to set the schema's max_results default
before sending to the local model. None ⇒ falls back to tool default
of 100. The experimentation surface called out in the spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Run full verification + manual MCP smoke test

**Files:** none — verification only.

- [ ] **Step 1: Run Python lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 2: Run the full Python test suite**

Run: `uv run pytest tests/ -x --ignore=tests/integration`
Expected: ALL PASS.

- [ ] **Step 3: Manual MCP smoke test**

Start HC (macOS native menubar OR `uv run harbor-clerk-api`). Verify `kb_find_all` shows in `tools/list`:

```bash
curl -ks -X POST http://localhost:8100/mcp/ \
  -H "Authorization: Bearer <hc_api_key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | grep -oE 'kb_find_all|text_contains' | sort -u
```

Expected output: `kb_find_all` and `text_contains` both appear.

Run a kb_find_all call against the loaded corpus:

```bash
curl -ks -X POST http://localhost:8100/mcp/ \
  -H "Authorization: Bearer <hc_api_key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"kb_find_all",
                 "arguments":{"query":"off-balance-sheet",
                              "text_contains":"off-balance-sheet",
                              "max_results":50}}}'
```

Expected: a JSON payload with `results`, `total_matches`, `truncated`. On the Enron-loaded corpus, `total_matches` should be close to 17 (the ground-truth count for `enron-find-offbalancesheet`).

- [ ] **Step 4: Commit any final fixes from manual testing**

If manual testing surfaces issues, fix and commit:

```bash
git add ...
git commit -m "fix(mcp): ... (caught in manual kb_find_all smoke test)"
```

If no fixes needed, skip this step.

---

## Task 13: Eval impact measurement (BEFORE / AFTER)

**Files:** results land in `pr_followups.md` / PR description; no code changes.

- [ ] **Step 1: Re-run the three Enron find items against the new HC**

Use the existing `scripts/test_corpora/runner` infrastructure with `--refresh` on the three find qids. From the repo root:

```bash
cd /Users/alex/mcp-gateway
export ANTHROPIC_API_KEY=<key>
export HC_API_KEY=<enron-scoped-or-full-key>
export HC_API_BASE=http://localhost:8100
export HC_MCP_URL=http://localhost:8100/mcp/

uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
  --mode answer-eval \
  --corpora enron \
  --refresh enron-find-offbalancesheet enron-find-ferc enron-find-layforwarded2001 \
  --label after-kb-find-all
```

The captures land under `<workdir>/answer-eval/captures/<label>/enron/claude-sonnet-4-6/`.

- [ ] **Step 2: Compare to the 2026-05-05-prod baselines**

The pre-change captures are at:
`<workdir>/answer-eval/captures/enron/claude-sonnet-4-6/enron-find-{offbalancesheet,ferc,layforwarded2001}.json`

Diff cited count vs truth count for each item. The expected outcomes per the spec:
- `offbalancesheet`: lift on cited (Shape 1 fixed by either `kb_find_all` enumeration OR `kb_search`-level `text_contains` use).
- `layforwarded2001`: lift on cited (Shape 2 — model should reach for `text_contains` on "forwarded by Lay").
- `ferc`: roughly flat (Shape 3, null hypothesis).

- [ ] **Step 3: Write up the deltas**

Generate a deltas table and put it in the PR description (when the PR is opened) AND as a `pr_followups.md` entry referencing this PR. Markdown table shape:

```markdown
| qid | before-cited / truth | after-cited / truth | shape | result |
|---|---|---|---|---|
| enron-find-offbalancesheet | 13 / 17 | X / 17 | one-shot stop | + Y |
| enron-find-layforwarded2001 | 71 / 126 | X / 126 | structured filter | + Y |
| enron-find-ferc | 40 / 50 | X / 50 | coverage cap (null) | ± Y |
```

- [ ] **Step 4: Memory write-up**

Update `pr_followups.md` PR #385 entry from open to ✅ DONE with the result and a brief note. If new gaps surface (e.g. the model doesn't reach for `text_contains` despite the docstring), capture them as new entries.

If the result is a clear win (Shapes 1 + 2), no new memory file needed. If it's a partial win, write a new project memory describing the residual gap.

---

## Final commit / PR

Once Task 13 wraps:

```bash
git push -u origin feat/kb-find-all

gh pr create --title "feat(mcp): kb_find_all unified enumeration tool" --body-file <(cat <<'EOF'
## Summary

Closes the find-iteration gap from PR #385 / PR #387 with a new MCP tool plus a literal-substring filter shared with kb_search.

(See `docs/superpowers/specs/2026-05-26-find-iteration-enumeration-design.md` for the design + the three failure shapes that motivated it.)

## What's new

- **`kb_find_all` MCP tool** + `find_all_documents` chat-tool mirror — returns documents deduped by doc_id, server-side iteration, three sort modes, offset pagination.
- **`text_contains` filter** added to both `kb_find_all` AND `kb_search` — case-insensitive literal substring on chunk text, backed by a new `pg_trgm` GIN index on `chunks.chunk_text`.
- **`ModelInfo.find_all_default_max_results`** — per-model experimentation surface for local-model max-results tuning.
- **`settings.find_all_max_results_cap`** (default 500) — server-side hard ceiling.

## Eval

<paste deltas table from Task 13 here>

## Test plan

- [x] `uv run pytest tests/test_find_all.py tests/test_kb_find_all_mcp.py tests/test_chat_tools.py -v` — N/N pass
- [x] `uv run pytest tests/test_search_filtered.py tests/test_hybrid_search_with_rerank.py tests/test_mcp_tool_descriptions.py -v` — N/N pass (regressions checked)
- [x] `uv run ruff check . && uv run ruff format --check .` — clean
- [x] Manual `tools/list` shows `kb_find_all` + `text_contains` in tool descriptions
- [x] Manual kb_find_all call against Enron corpus — total_matches sensible vs ground truth
EOF
)
```

---

## Self-Review (built in)

**Spec coverage:**
- §1 Tool surface (kb_find_all + find_all_documents) — Tasks 9, 10 ✓
- §2 Semantics (dedupe, total_matches, max_results clamp, offset, sort, presentation) — Tasks 6, 7, 8, 9 ✓
- §3 text_contains filter — Tasks 4, 9 ✓
- §4 Per-model override — Tasks 2, 11 ✓
- §5 Implementation sketch — Task 6 ✓
- §6 Testing — Tasks 6–11 (unit + description) + Task 13 (eval) ✓
- §7 Out of scope — not in plan (correct) ✓

**Placeholder scan:** None — every step has either code or a concrete command.

**Type consistency:** `FindAllHit` / `FindAllResult` defined in Task 5, referenced by Task 6. `find_all` signature in Task 6 matches the kw-only params used in Task 7's sort tests and Task 8's offset/presentation tests. `_map_args_find_all` signature in Task 10 matches keys used in Task 11's per-model schema mutation.
