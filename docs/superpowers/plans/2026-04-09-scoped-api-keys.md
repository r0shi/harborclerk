# Scoped API Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-API-key access controls (document scoping by topic/folder, MCP tool tier with overrides, expiry dates, snippet size limits) so admins can give external agentic harnesses controlled, narrow access to a subset of the corpus.

**Architecture:** Extend the `api_keys` table with 6 new columns. Carry scope data on the `Principal` dataclass (loaded once at auth time, no per-call DB hits). New `apply_key_scope()` helper modifies SQLAlchemy queries to filter documents. MCP tool gating via `list_tools` / `call_tool` overrides on a FastMCP subclass — silently omit tools the key isn't allowed to use.

**Tech Stack:** Python (FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic), FastMCP, React/TypeScript

**Spec:** `docs/superpowers/specs/2026-04-07-scoped-api-keys-design.md`

---

## File Map

### New Files

| File | Responsibility |
|---|---|
| `alembic/versions/0013_scoped_api_keys.py` | Migration: 6 new columns on `api_keys` |
| `src/harbor_clerk/api/scope.py` | `apply_key_scope()` helper, tier→tools mapping, KeyScope dataclass |
| `tests/test_scoped_api_keys.py` | Auth, scope filtering, expiry, JSON parsing tests |
| `tests/test_mcp_tool_filtering.py` | FastMCP tool filtering tests |

### Modified Files

| File | Changes |
|---|---|
| `src/harbor_clerk/models/api_key.py` | Add 6 columns |
| `src/harbor_clerk/api/deps.py` | Extend `Principal` with `key_scope`, populate at auth, expiry check |
| `src/harbor_clerk/api/schemas/api_keys.py` | New request/response models for create + patch + scope-preview |
| `src/harbor_clerk/api/routes/api_keys.py` | Extend POST, add PATCH, add scope-preview, enrich GET response |
| `src/harbor_clerk/mcp_server.py` | FastMCP subclass with filtered list_tools/call_tool, expiry check in `_resolve_principal` |
| `src/harbor_clerk/api/routes/documents.py` | Apply scope to GET /docs and GET /docs/{id} |
| `src/harbor_clerk/api/routes/search.py` | Apply scope to search results |
| `src/harbor_clerk/llm/research.py` (chunk reads) | Chunk-level scope filter for kb_read_passages |
| `frontend/src/pages/ApiKeysPage.tsx` | Expanded create form, edit panel, scope preview |

---

## Permission Tiers (Reference)

Computed by `compute_effective_tools(tier, overrides)` in `scope.py`:

| Tier | Base Tools |
|---|---|
| `search` | kb_search, kb_batch_search, kb_corpus_overview, kb_list_recent |
| `read` | search + kb_read_passages, kb_expand_context, kb_document_outline, kb_get_document |
| `full` | read + kb_read_document, kb_find_related, kb_entity_search, kb_entity_overview, kb_entity_cooccurrence, kb_ingest_status |

**Admin-only** (excluded from all tiers): `kb_system_health`, `kb_reprocess`

`tool_overrides` is a dict of `{tool_name: bool}` — `true` adds, `false` removes. Admin-only tools are always excluded.

---

## Task 1: Database Migration

**Files:**
- Create: `alembic/versions/0013_scoped_api_keys.py`

- [ ] **Step 1: Write the migration**

```python
"""Add scoping fields to api_keys.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("permission_tier", sa.Text(), nullable=False, server_default="full"),
    )
    op.add_column(
        "api_keys",
        sa.Column("tool_overrides", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column("api_keys", sa.Column("scope_topic_ids", postgresql.JSONB(), nullable=True))
    op.add_column("api_keys", sa.Column("scope_folder_ids", postgresql.JSONB(), nullable=True))
    op.add_column("api_keys", sa.Column("max_snippet_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "max_snippet_chars")
    op.drop_column("api_keys", "scope_folder_ids")
    op.drop_column("api_keys", "scope_topic_ids")
    op.drop_column("api_keys", "tool_overrides")
    op.drop_column("api_keys", "permission_tier")
    op.drop_column("api_keys", "expires_at")
```

- [ ] **Step 2: Lint**

```bash
cd /Users/alex/mcp-gateway/.worktrees/scoped-api-keys
uv run ruff check alembic/versions/0013_scoped_api_keys.py
```

Expected: All checks passed!

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/0013_scoped_api_keys.py
git commit -m "feat(scoped-keys): migration adding 6 scope columns to api_keys"
```

---

## Task 2: Update ApiKey Model

**Files:**
- Modify: `src/harbor_clerk/models/api_key.py`

- [ ] **Step 1: Read the current model**

```bash
cat src/harbor_clerk/models/api_key.py
```

- [ ] **Step 2: Add the new fields**

Add these fields after `last_used_at`. Update imports as needed:

```python
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB

# ... existing imports

class ApiKey(Base):
    # ... existing columns ...
    
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    permission_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default="full")
    tool_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    scope_topic_ids: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    scope_folder_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    max_snippet_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/harbor_clerk/models/api_key.py
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/models/api_key.py
git commit -m "feat(scoped-keys): ApiKey model fields for scope/tier/expiry"
```

---

## Task 3: KeyScope dataclass and tier mapping

**Files:**
- Create: `src/harbor_clerk/api/scope.py`
- Test: `tests/test_scoped_api_keys.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scoped_api_keys.py`:

```python
"""Tests for scoped API keys."""
from harbor_clerk.api.scope import (
    KeyScope,
    compute_effective_tools,
    SEARCH_TIER_TOOLS,
    READ_TIER_TOOLS,
    FULL_TIER_TOOLS,
    ADMIN_ONLY_TOOLS,
)


def test_search_tier_has_search_tools():
    tools = compute_effective_tools("search", {})
    assert "kb_search" in tools
    assert "kb_batch_search" in tools
    assert "kb_corpus_overview" in tools
    assert "kb_list_recent" in tools
    # No read tools
    assert "kb_read_passages" not in tools
    # No admin tools
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_read_tier_extends_search():
    tools = compute_effective_tools("read", {})
    assert "kb_search" in tools  # search tier inherited
    assert "kb_read_passages" in tools
    assert "kb_expand_context" in tools
    assert "kb_document_outline" in tools
    assert "kb_get_document" in tools
    # Not in this tier
    assert "kb_find_related" not in tools
    assert "kb_entity_search" not in tools


def test_full_tier_has_most_tools():
    tools = compute_effective_tools("full", {})
    assert "kb_read_document" in tools
    assert "kb_entity_search" in tools
    assert "kb_find_related" in tools
    # Admin tools never included
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_overrides_can_remove_tool():
    tools = compute_effective_tools("full", {"kb_read_document": False})
    assert "kb_read_document" not in tools
    assert "kb_search" in tools  # other tools unchanged


def test_overrides_can_add_tool():
    tools = compute_effective_tools("search", {"kb_read_passages": True})
    assert "kb_read_passages" in tools


def test_overrides_cannot_grant_admin_tools():
    tools = compute_effective_tools("full", {"kb_system_health": True})
    assert "kb_system_health" not in tools
    tools = compute_effective_tools("full", {"kb_reprocess": True})
    assert "kb_reprocess" not in tools


def test_unknown_tier_treated_as_full():
    # Defensive — unknown tier shouldn't crash
    tools = compute_effective_tools("nonsense", {})
    assert "kb_search" in tools


def test_keyscope_default_no_restrictions():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is True


def test_keyscope_with_topic_filter_is_restricted():
    scope = KeyScope(
        scope_topic_ids=[1, 2],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is False
```

- [ ] **Step 2: Run test, expect failure**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

Expected: `ModuleNotFoundError: No module named 'harbor_clerk.api.scope'`

- [ ] **Step 3: Create the module**

Create `src/harbor_clerk/api/scope.py`:

```python
"""Per-API-key scope computation: tiers, tool sets, and query filters."""

from dataclasses import dataclass

# Tier -> base tool set
SEARCH_TIER_TOOLS: frozenset[str] = frozenset(
    {"kb_search", "kb_batch_search", "kb_corpus_overview", "kb_list_recent"}
)
READ_TIER_TOOLS: frozenset[str] = SEARCH_TIER_TOOLS | frozenset(
    {"kb_read_passages", "kb_expand_context", "kb_document_outline", "kb_get_document"}
)
FULL_TIER_TOOLS: frozenset[str] = READ_TIER_TOOLS | frozenset(
    {
        "kb_read_document",
        "kb_find_related",
        "kb_entity_search",
        "kb_entity_overview",
        "kb_entity_cooccurrence",
        "kb_ingest_status",
    }
)

# Admin-only tools — never available to API keys regardless of tier or overrides
ADMIN_ONLY_TOOLS: frozenset[str] = frozenset({"kb_system_health", "kb_reprocess"})

_TIERS: dict[str, frozenset[str]] = {
    "search": SEARCH_TIER_TOOLS,
    "read": READ_TIER_TOOLS,
    "full": FULL_TIER_TOOLS,
}


def compute_effective_tools(tier: str, overrides: dict[str, bool]) -> frozenset[str]:
    """Compute the set of tools available to an API key.

    Starts with the tier's base set, applies overrides (true=add, false=remove),
    then strips admin-only tools.
    """
    base = _TIERS.get(tier, FULL_TIER_TOOLS)  # default to full for unknown tier
    effective = set(base)
    for tool_name, enabled in (overrides or {}).items():
        if enabled:
            effective.add(tool_name)
        else:
            effective.discard(tool_name)
    return frozenset(effective - ADMIN_ONLY_TOOLS)


@dataclass
class KeyScope:
    """Snapshot of an API key's scope, attached to Principal at auth time.

    Avoids per-tool-call DB lookups for scope info.
    """

    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    max_snippet_chars: int | None

    @property
    def is_unrestricted(self) -> bool:
        """True if no document-level scope filter applies."""
        return self.scope_topic_ids is None and self.scope_folder_ids is None

    @property
    def effective_tools(self) -> frozenset[str]:
        """The set of tool names this key may call."""
        return compute_effective_tools(self.permission_tier, self.tool_overrides)
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

Expected: 9 passed

- [ ] **Step 5: Lint**

```bash
uv run ruff check src/harbor_clerk/api/scope.py tests/test_scoped_api_keys.py
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/scope.py tests/test_scoped_api_keys.py
git commit -m "feat(scoped-keys): KeyScope dataclass + tier→tool computation"
```

---

## Task 4: Extend Principal with key_scope, populate at auth, expiry check

**Files:**
- Modify: `src/harbor_clerk/api/deps.py`
- Modify: `src/harbor_clerk/mcp_server.py` (`_resolve_principal`)
- Test: `tests/test_scoped_api_keys.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_scoped_api_keys.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import KeyScope


def test_principal_default_no_key_scope():
    p = Principal(type="user", id=uuid.uuid4(), role="admin")
    assert p.key_scope is None


def test_principal_with_key_scope():
    scope = KeyScope(
        scope_topic_ids=[1],
        scope_folder_ids=None,
        permission_tier="search",
        tool_overrides={},
        max_snippet_chars=500,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    assert p.key_scope.scope_topic_ids == [1]
    assert p.key_scope.permission_tier == "search"
```

- [ ] **Step 2: Run tests, expect failure**

```bash
uv run pytest tests/test_scoped_api_keys.py::test_principal_default_no_key_scope -v
```

Expected: `TypeError` because Principal doesn't accept `key_scope`

- [ ] **Step 3: Update Principal dataclass**

In `src/harbor_clerk/api/deps.py`, replace the Principal class:

```python
from harbor_clerk.api.scope import KeyScope


@dataclass
class Principal:
    """Authenticated caller identity."""

    type: str  # "user" or "api_key"
    id: uuid.UUID  # user_id or key_id
    role: str  # "admin" or "user"
    key_scope: KeyScope | None = None  # populated for api_key principals only
```

- [ ] **Step 4: Update `get_current_principal` to populate key_scope and check expiry**

In `src/harbor_clerk/api/deps.py`, find the API key branch (around line 70-82) and replace:

```python
    # API key lookup
    key_hash = hash_api_key(token)
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True)))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    # Expiry check
    if api_key.expires_at is not None and api_key.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key expired",
        )
    # Update last_used_at
    await session.execute(update(ApiKey).where(ApiKey.key_id == api_key.key_id).values(last_used_at=datetime.now(UTC)))
    await session.commit()
    scope = KeyScope(
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        max_snippet_chars=api_key.max_snippet_chars,
    )
    return Principal(type="api_key", id=api_key.key_id, role="user", key_scope=scope)
```

- [ ] **Step 5: Update `_resolve_principal` in mcp_server.py the same way**

Find the API key branch in `_resolve_principal` (around line 84-98) and apply the same changes — load scope into KeyScope, check expiry, populate `key_scope` on Principal.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

Expected: all pass.

- [ ] **Step 7: Lint**

```bash
uv run ruff check src/harbor_clerk/api/deps.py src/harbor_clerk/mcp_server.py tests/test_scoped_api_keys.py
```

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/api/deps.py src/harbor_clerk/mcp_server.py tests/test_scoped_api_keys.py
git commit -m "feat(scoped-keys): Principal carries KeyScope, expiry enforced at auth"
```

---

## Task 5: apply_key_scope() query filter

**Files:**
- Modify: `src/harbor_clerk/api/scope.py`
- Test: `tests/test_scoped_api_keys.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_scoped_api_keys.py`:

```python
from sqlalchemy import select as sa_select
from harbor_clerk.api.scope import apply_key_scope
from harbor_clerk.models.document import Document


def test_apply_scope_user_principal_unchanged():
    user = Principal(type="user", id=uuid.uuid4(), role="admin")
    query = sa_select(Document)
    result = apply_key_scope(query, user)
    # Should return the same query unmodified
    assert str(result) == str(query)


def test_apply_scope_unrestricted_key_unchanged():
    scope = KeyScope(
        scope_topic_ids=None, scope_folder_ids=None,
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    assert str(result) == str(query)


def test_apply_scope_topic_filter_adds_where():
    scope = KeyScope(
        scope_topic_ids=[1, 2], scope_folder_ids=None,
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    # Compiled SQL should contain a topic_id IN clause
    sql = str(result.compile(compile_kwargs={"literal_binds": True}))
    assert "topic_id" in sql.lower()
    assert "in (1, 2)" in sql.lower() or "in ('1', '2')" in sql.lower()


def test_apply_scope_folder_filter_subquery():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=["00000000-0000-0000-0000-000000000001"],
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    sql = str(result.compile(compile_kwargs={"literal_binds": True}))
    assert "watched_files" in sql.lower()
    assert "doc_id" in sql.lower()
```

- [ ] **Step 2: Run tests, expect failure**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

Expected: `ImportError: cannot import name 'apply_key_scope'`

- [ ] **Step 3: Implement apply_key_scope**

Add to `src/harbor_clerk/api/scope.py`. Note: `Principal` is imported lazily under `TYPE_CHECKING` to avoid a circular dep with `deps.py` (which imports `KeyScope` from this module). The model imports are top-level — they don't import deps.py.

```python
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

if TYPE_CHECKING:
    from harbor_clerk.api.deps import Principal


def apply_key_scope(query: Select, principal: "Principal") -> Select:
    """Filter a Document query by the API key's scope.

    No-op for human users (type='user') and unrestricted API keys.
    For scoped keys, adds a WHERE clause filtering documents to those
    matching the topic OR folder scope.

    Pure query builder — no DB access (scope is on Principal from auth time).
    """
    if principal.type != "api_key" or principal.key_scope is None:
        return query
    scope = principal.key_scope
    if scope.is_unrestricted:
        return query

    conditions = []
    if scope.scope_topic_ids is not None:
        conditions.append(Document.topic_id.in_(scope.scope_topic_ids))
    if scope.scope_folder_ids is not None:
        try:
            folder_uuids = [uuid.UUID(fid) for fid in scope.scope_folder_ids]
        except (ValueError, AttributeError):
            folder_uuids = []
        if folder_uuids:
            watched_doc_ids = select(WatchedFile.doc_id).where(
                WatchedFile.folder_id.in_(folder_uuids),
                WatchedFile.status == WatchedFileStatus.active,
            )
            conditions.append(Document.doc_id.in_(watched_doc_ids))

    if not conditions:
        # Both axes empty — explicitly nothing visible
        return query.where(Document.doc_id == uuid.UUID(int=0))

    return query.where(or_(*conditions))
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

- [ ] **Step 5: Add integration tests with real DB data**

The unit tests above check query string generation. Add integration tests that exercise the actual filtering against the test DB. These run in CI via the pgvector service container.

Append to `tests/test_scoped_api_keys.py`:

```python
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models.document import Document
from harbor_clerk.models.api_key import ApiKey
from harbor_clerk.auth import generate_api_key, hash_api_key


@pytest_asyncio.fixture
async def two_topic_docs(db_session: AsyncSession):
    """Create 2 documents in topic 1 and 1 document in topic 2."""
    docs = [
        Document(title="Doc A1", canonical_filename="a1.txt", topic_id=1, status="active"),
        Document(title="Doc A2", canonical_filename="a2.txt", topic_id=1, status="active"),
        Document(title="Doc B1", canonical_filename="b1.txt", topic_id=2, status="active"),
    ]
    for d in docs:
        db_session.add(d)
    await db_session.commit()
    yield docs
    for d in docs:
        await db_session.delete(d)
    await db_session.commit()


@pytest.mark.asyncio
async def test_scope_filters_by_topic_in_db(db_session, two_topic_docs):
    """Real DB: key scoped to topic 1 sees only those documents."""
    scope = KeyScope(
        scope_topic_ids=[1], scope_folder_ids=None,
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    assert len(docs) == 2
    assert all(d.topic_id == 1 for d in docs)


@pytest.mark.asyncio
async def test_unrestricted_key_sees_all_in_db(db_session, two_topic_docs):
    """Real DB: unrestricted key sees all documents."""
    scope = KeyScope(
        scope_topic_ids=None, scope_folder_ids=None,
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    assert len(docs) == 3


@pytest.mark.asyncio
async def test_empty_topic_list_blocks_all_in_db(db_session, two_topic_docs):
    """Real DB: scope_topic_ids=[] blocks everything (no topics match)."""
    scope = KeyScope(
        scope_topic_ids=[], scope_folder_ids=None,
        permission_tier="full", tool_overrides={}, max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    assert len(docs) == 0
```

Note: this assumes a `db_session` fixture exists in conftest.py. If not, follow the pattern in existing test_*.py files (e.g. test_watch_pipeline.py for a simpler version, or test_watch_api.py if API integration tests already exist).

- [ ] **Step 6: Run integration tests**

```bash
uv run pytest tests/test_scoped_api_keys.py -v
```

Expected: all unit AND integration tests pass.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/scope.py tests/test_scoped_api_keys.py
git add src/harbor_clerk/api/scope.py tests/test_scoped_api_keys.py
git commit -m "feat(scoped-keys): apply_key_scope query filter (topic OR folder)"
```

---

## Task 6: Apply scope to REST endpoints

**Files:**
- Modify: `src/harbor_clerk/api/routes/documents.py`
- Modify: `src/harbor_clerk/api/routes/search.py`

- [ ] **Step 1: Add scope to GET /api/docs**

In `src/harbor_clerk/api/routes/documents.py`, find the documents list endpoint (the function that calls `select(Document)` and returns `PaginatedDocuments`). After building the base query but before executing, apply scope:

```python
from harbor_clerk.api.scope import apply_key_scope

# Inside list_documents (or whatever it's called):
query = apply_key_scope(query, principal)
```

The query needs to be modified BEFORE the count subquery and BEFORE pagination. Apply to both the count query and the items query — total should also reflect scoped count.

- [ ] **Step 2: Add scope to GET /api/docs/{doc_id}**

For the single-document fetch, change the query from `Document.doc_id == doc_id` to also pass through `apply_key_scope`. Easiest pattern:

```python
query = select(Document).where(Document.doc_id == doc_id, Document.status == "active")
query = apply_key_scope(query, principal)
result = await session.execute(query)
doc = result.scalar_one_or_none()
if doc is None:
    raise HTTPException(404, "Document not found")
```

This makes scoped-out docs return 404 — invisible to the agent.

- [ ] **Step 3: Apply to search endpoints**

In `src/harbor_clerk/api/routes/search.py`, find the search query construction. The search joins chunks → documents. Apply `apply_key_scope` to a Document subquery and use it as a filter on the chunks query.

If search builds raw SQL, the simplest fix is to compute visible doc_ids first:

```python
from harbor_clerk.api.scope import apply_key_scope

if principal.type == "api_key" and principal.key_scope and not principal.key_scope.is_unrestricted:
    visible_q = apply_key_scope(select(Document.doc_id), principal)
    visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}
    # Filter search results to only visible_ids
```

- [ ] **Step 4: Lint**

```bash
uv run ruff check src/harbor_clerk/api/routes/documents.py src/harbor_clerk/api/routes/search.py
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/documents.py src/harbor_clerk/api/routes/search.py
git commit -m "feat(scoped-keys): apply_key_scope to docs list, get, and search"
```

---

## Task 7: Apply scope to MCP tools

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`

This is the largest single task — there are 14 MCP tools that read documents/chunks/jobs. We need to apply scope to each. The pattern depends on what the tool fetches:

- **Document-based tools** (kb_get_document, kb_list_recent, kb_find_related, kb_document_outline): pass `principal` to internal helpers and use `apply_key_scope`.
- **Aggregate stats** (kb_corpus_overview): see Step 3 below — counts/languages/types must reflect the scoped subset, not the full corpus.
- **Search tools** (kb_search, kb_batch_search): compute visible doc_ids and filter results.
- **Chunk-based tools** (kb_read_passages, kb_expand_context): fetch chunks first, then filter to chunks whose `doc_id` is in the visible set. **This is the chunk-level enforcement.**
- **Entity tools** (kb_entity_search, kb_entity_overview, kb_entity_cooccurrence): join via chunk → document → scope.
- **Job status** (kb_ingest_status): join `IngestionJob → DocumentVersion → Document` to filter to versions whose document is in the visible set. Otherwise scoped agents can discover document existence via ingestion status.

- [ ] **Step 1: Add a helper at the top of mcp_server.py (after _get_principal)**

```python
async def _visible_doc_ids(session: AsyncSession, principal: Principal) -> set[uuid.UUID] | None:
    """Get the set of doc_ids visible to this principal, or None if unrestricted."""
    if principal.type != "api_key" or principal.key_scope is None or principal.key_scope.is_unrestricted:
        return None
    from harbor_clerk.api.scope import apply_key_scope
    from harbor_clerk.models.document import Document
    query = apply_key_scope(select(Document.doc_id), principal)
    result = await session.execute(query)
    return {row[0] for row in result.all()}
```

- [ ] **Step 2: Wire scope into each MCP tool**

For each `@mcp.tool()` function in `mcp_server.py`:
1. Call `principal = _get_principal()` (already done)
2. Compute `visible_ids = await _visible_doc_ids(session, principal)`
3. If `visible_ids is not None`, add `Document.doc_id.in_(visible_ids)` to the query, OR filter the result rows in Python.

The 14 tools to update (find them with `grep -n '@mcp.tool()' src/harbor_clerk/mcp_server.py`):
- kb_search, kb_batch_search
- kb_read_passages, kb_expand_context
- kb_get_document, kb_list_recent
- kb_corpus_overview, kb_document_outline
- kb_find_related, kb_read_document
- kb_entity_search, kb_entity_overview, kb_entity_cooccurrence
- kb_ingest_status

For `kb_ingest_status`, the join path is `IngestionJob.version_id → DocumentVersion.doc_id → Document.doc_id`. Filter the query so only jobs for documents in `visible_ids` are returned.

- [ ] **Step 2a: Special case — kb_corpus_overview aggregate stats**

`kb_corpus_overview` returns counts (document_count, total_chunks, total_pages), language histogram, mime_type histogram, date_range, and a list of CorpusDocumentSummary. ALL of these must reflect the scoped subset.

Concretely: in the `corpus_overview` MCP tool, compute `visible_ids = await _visible_doc_ids(session, principal)` once. Then:
- For every aggregate query (document_count, language histogram, mime_type histogram, date_range, total_chunks, total_pages), add `WHERE Document.doc_id.in_(visible_ids)` (or join Chunk → Document and filter there for chunk/page counts).
- For the document list, add the same filter.
- If `visible_ids is None`, no filter — full corpus stats.

This is non-trivial because the existing function likely builds 5+ separate queries. Each one needs the filter. Walk through them carefully.

For `kb_read_passages` and `kb_expand_context` specifically (chunk-based), filter chunks AFTER fetching:

```python
# Inside kb_read_passages, after loading chunks:
visible_ids = await _visible_doc_ids(session, principal)
if visible_ids is not None:
    chunks = [c for c in chunks if c.doc_id in visible_ids]
```

For `kb_corpus_overview`, also recompute counts (languages, mime_types, etc.) using the scoped doc set.

- [ ] **Step 3: Apply max_snippet_chars truncation in passage tools**

In `kb_read_passages` and `kb_expand_context`, after filtering, truncate passage text:

```python
if principal.key_scope and principal.key_scope.max_snippet_chars is not None:
    limit = principal.key_scope.max_snippet_chars
    for passage in passages:
        passage.text = passage.text[:limit]
```

(Adapt the field name to whatever the actual schema uses.)

- [ ] **Step 4: Lint and run existing MCP tests if any**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py
uv run pytest tests/ -k mcp -v 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mcp_server.py
git commit -m "feat(scoped-keys): apply scope to all 13 read MCP tools"
```

---

## Task 8: MCP tool gating (silent omission)

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Test: `tests/test_mcp_tool_filtering.py`

We need to override `FastMCP.list_tools` and `FastMCP.call_tool` so that the tool list and call dispatch respect the current principal's `effective_tools`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_tool_filtering.py`:

```python
"""Tests for MCP tool filtering by API key scope."""
import uuid

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import KeyScope


def make_scoped_key(tier="search", overrides=None):
    return Principal(
        type="api_key",
        id=uuid.uuid4(),
        role="user",
        key_scope=KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=None,
            permission_tier=tier,
            tool_overrides=overrides or {},
            max_snippet_chars=None,
        ),
    )


def test_human_user_sees_all_tools():
    from harbor_clerk.mcp_server import _filter_tools_for_principal
    from harbor_clerk.api.deps import Principal as P

    user = P(type="user", id=uuid.uuid4(), role="admin")
    all_tool_names = ["kb_search", "kb_read_passages", "kb_system_health", "kb_reprocess"]
    filtered = _filter_tools_for_principal(all_tool_names, user)
    assert set(filtered) == set(all_tool_names)


def test_search_tier_filters_to_search_tools():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("search")
    all_tool_names = ["kb_search", "kb_read_passages", "kb_system_health"]
    filtered = _filter_tools_for_principal(all_tool_names, p)
    assert "kb_search" in filtered
    assert "kb_read_passages" not in filtered
    assert "kb_system_health" not in filtered


def test_overrides_remove_tool():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("full", overrides={"kb_read_document": False})
    filtered = _filter_tools_for_principal(["kb_read_document", "kb_search"], p)
    assert "kb_read_document" not in filtered
    assert "kb_search" in filtered


def test_admin_only_tools_never_for_api_key_even_with_override():
    from harbor_clerk.mcp_server import _filter_tools_for_principal

    p = make_scoped_key("full", overrides={"kb_system_health": True, "kb_reprocess": True})
    filtered = _filter_tools_for_principal(["kb_system_health", "kb_reprocess", "kb_search"], p)
    assert "kb_system_health" not in filtered
    assert "kb_reprocess" not in filtered
    assert "kb_search" in filtered
```

- [ ] **Step 2: Run test, expect failure**

```bash
uv run pytest tests/test_mcp_tool_filtering.py -v
```

Expected: `ImportError: cannot import name '_filter_tools_for_principal'`

- [ ] **Step 3: Add the helper and override list_tools/call_tool**

In `src/harbor_clerk/mcp_server.py`, add this helper near the top (after imports, before `mcp = FastMCP(...)`):

```python
def _filter_tools_for_principal(tool_names: list[str] | set[str], principal: Principal | None) -> list[str]:
    """Return only the tool names this principal is allowed to use."""
    if principal is None:
        return []
    if principal.type != "api_key" or principal.key_scope is None:
        # Human users see everything (admin checks happen inside individual tools)
        return list(tool_names)
    allowed = principal.key_scope.effective_tools
    return [t for t in tool_names if t in allowed]
```

Then subclass FastMCP. Replace `mcp = FastMCP(...)` with:

```python
class ScopedFastMCP(FastMCP):
    """FastMCP subclass that filters tool listing and calls by API key scope.

    Security: list_tools is a UX layer; the real enforcement is call_tool +
    the apply_key_scope filters in each tool body. So even if list_tools is
    bypassed (e.g. via the lowlevel server's tool cache), tool calls are
    still rejected.
    """

    async def list_tools(self):
        all_tools = await super().list_tools()
        try:
            principal = _get_principal()
        except PermissionError:
            # No principal context (e.g. internal tool-cache refresh).
            # Return [] rather than all_tools so we don't pollute caches with
            # full visibility. Real per-call enforcement happens in call_tool.
            return []
        if principal.type != "api_key" or principal.key_scope is None:
            return all_tools
        allowed = principal.key_scope.effective_tools
        return [t for t in all_tools if t.name in allowed]

    async def call_tool(self, name, arguments):
        try:
            principal = _get_principal()
        except PermissionError:
            # No principal — let the underlying handler decide
            # (this should not happen on a real call path; auth middleware sets it)
            return await super().call_tool(name, arguments)
        if principal.type == "api_key" and principal.key_scope is not None:
            if name not in principal.key_scope.effective_tools:
                # Tool not in this key's set. McpError gets converted to an
                # isError tool result by the lowlevel handler, which is fine —
                # the agent just sees a clean error and the tool is effectively
                # invisible.
                from mcp.server.fastmcp.exceptions import ToolError

                raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments)


mcp = ScopedFastMCP(
    "Harbor Clerk",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/test_mcp_tool_filtering.py -v
```

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_filtering.py
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_filtering.py
git commit -m "feat(scoped-keys): MCP tool filtering via ScopedFastMCP subclass"
```

---

## Task 9: API endpoints — extend POST, add PATCH, scope-preview, enrich GET

**Files:**
- Modify: `src/harbor_clerk/api/schemas/api_keys.py`
- Modify: `src/harbor_clerk/api/routes/api_keys.py`

- [ ] **Step 1: Extend the schemas**

In `src/harbor_clerk/api/schemas/api_keys.py`:

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] = "full"
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None


class PatchApiKeyRequest(BaseModel):
    name: str | None = None
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] | None = None
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None


class ApiKeyOut(BaseModel):
    key_id: str
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    max_snippet_chars: int | None
    scope_summary: str  # human-readable: "Full access", "3 topics", etc.


class ScopePreviewResponse(BaseModel):
    accessible_documents: int
    total_documents: int


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    name: str
    raw_key: str
    mcp_path: str
    created_at: datetime
```

- [ ] **Step 2: Add scope_summary helper and update routes**

In `src/harbor_clerk/api/routes/api_keys.py`:

```python
def _scope_summary(api_key: ApiKey) -> str:
    parts = []
    if api_key.permission_tier != "full":
        parts.append(api_key.permission_tier.capitalize())
    if api_key.scope_topic_ids:
        parts.append(f"{len(api_key.scope_topic_ids)} topic{'s' if len(api_key.scope_topic_ids) != 1 else ''}")
    if api_key.scope_folder_ids:
        parts.append(f"{len(api_key.scope_folder_ids)} folder{'s' if len(api_key.scope_folder_ids) != 1 else ''}")
    if api_key.expires_at:
        parts.append(f"expires {api_key.expires_at.date().isoformat()}")
    return ", ".join(parts) if parts else "Full access"


def _api_key_to_out(api_key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        key_id=str(api_key.key_id),
        name=api_key.name,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        max_snippet_chars=api_key.max_snippet_chars,
        scope_summary=_scope_summary(api_key),
    )
```

Then update the `POST /api-keys` endpoint to accept the new fields:

```python
@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    raw_key = generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_hash=hash_api_key(raw_key),
        expires_at=body.expires_at,
        permission_tier=body.permission_tier,
        tool_overrides=body.tool_overrides or {},
        scope_topic_ids=body.scope_topic_ids,
        scope_folder_ids=body.scope_folder_ids,
        max_snippet_chars=body.max_snippet_chars,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    await log_audit(
        session,
        user_id=admin.id,
        action="create_api_key",
        target_type="api_key",
        target_id=api_key.key_id,
    )
    await session.commit()
    return ApiKeyCreatedResponse(
        key_id=str(api_key.key_id),
        name=api_key.name,
        raw_key=raw_key,
        mcp_path=f"/t/{raw_key}",
        created_at=api_key.created_at,
    )
```

Add the PATCH endpoint:

```python
@router.patch("/api-keys/{key_id}", response_model=ApiKeyOut)
async def patch_api_key(
    key_id: uuid.UUID,
    body: PatchApiKeyRequest,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(404, "API key not found")
    # Use model_fields_set to distinguish "field absent" from "field=null"
    patch = body.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(api_key, field, value)
    await log_audit(
        session,
        user_id=admin.id,
        action="patch_api_key",
        target_type="api_key",
        target_id=key_id,
        detail={"fields": list(patch.keys())},
    )
    await session.commit()
    await session.refresh(api_key)
    return _api_key_to_out(api_key)
```

Add the scope-preview endpoint:

```python
@router.get("/api-keys/{key_id}/scope-preview", response_model=ScopePreviewResponse)
async def scope_preview(
    key_id: uuid.UUID,
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from harbor_clerk.api.scope import KeyScope, apply_key_scope
    from harbor_clerk.models.document import Document
    from sqlalchemy import func

    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(404, "API key not found")

    total = (await session.execute(select(func.count(Document.doc_id)).where(Document.status == "active"))).scalar_one()

    # Build a synthetic principal with this key's scope to reuse apply_key_scope
    scope = KeyScope(
        scope_topic_ids=api_key.scope_topic_ids,
        scope_folder_ids=api_key.scope_folder_ids,
        permission_tier=api_key.permission_tier,
        tool_overrides=api_key.tool_overrides or {},
        max_snippet_chars=api_key.max_snippet_chars,
    )
    fake_principal = Principal(type="api_key", id=key_id, role="user", key_scope=scope)
    visible_query = apply_key_scope(
        select(func.count(Document.doc_id)).where(Document.status == "active"),
        fake_principal,
    )
    visible = (await session.execute(visible_query)).scalar_one()

    return ScopePreviewResponse(accessible_documents=visible, total_documents=total)
```

Update GET /api-keys to use `_api_key_to_out`:

```python
@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    admin: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return [_api_key_to_out(k) for k in result.scalars().all()]
```

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/harbor_clerk/api/schemas/api_keys.py src/harbor_clerk/api/routes/api_keys.py
```

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/api/schemas/api_keys.py src/harbor_clerk/api/routes/api_keys.py
git commit -m "feat(scoped-keys): API endpoints for create/patch/list/scope-preview"
```

---

## Task 10: Frontend — API Keys page

**Files:**
- Modify: `frontend/src/pages/ApiKeysPage.tsx`

- [ ] **Step 1: Read the current page**

```bash
cat frontend/src/pages/ApiKeysPage.tsx | head -100
```

Note the existing component structure (interfaces, state, render).

- [ ] **Step 2: Extend the form and table**

This is a UI task — make the changes incrementally. The minimum needed:

1. **TypeScript interface** — add the new fields to the `ApiKey` interface used in the list:
```typescript
interface ApiKey {
  key_id: string
  name: string
  is_active: boolean
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  permission_tier: 'search' | 'read' | 'full'
  tool_overrides: Record<string, boolean>
  scope_topic_ids: number[] | null
  scope_folder_ids: string[] | null
  max_snippet_chars: number | null
  scope_summary: string
}
```

2. **Create form** — add fields below the Name input:
   - Expires (date picker, optional)
   - Permission tier (radio: Search/Read/Full)
   - `<details>` element with summary "Tool overrides" → grid of tool checkboxes
   - `<details>` element with summary "Document scope" → topic multi-select + folder multi-select
   - Max snippet chars (number input)

3. **List table** — add columns:
   - Scope (display `scope_summary`)
   - Expires (display `expires_at` formatted, or "Never")

4. **Edit panel** — clicking a key opens a modal with the same form fields, populated from current values, calls PATCH on save

5. **Scope preview** — after setting scopes, debounced fetch of `/api-keys/{key_id}/scope-preview` showing "X of Y documents accessible"

This task is about UI polish — exact code is left to the implementer. Match the existing styling patterns in the file (dark mode classes, button styles).

- [ ] **Step 3: Lint and typecheck**

```bash
cd frontend && npx eslint src/pages/ApiKeysPage.tsx && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/scoped-api-keys
git add frontend/src/pages/ApiKeysPage.tsx
git commit -m "feat(scoped-keys): API Keys page UI with scope/tier/expiry controls"
```

---

## Task 11: Run all tests and final verification

- [ ] **Step 1: Run all Python tests**

```bash
cd /Users/alex/mcp-gateway/.worktrees/scoped-api-keys
uv run pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass, including the new scope tests.

- [ ] **Step 2: Lint**

```bash
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 3: Frontend lint and typecheck**

```bash
cd frontend && npx eslint src/ && npx tsc --noEmit
```

- [ ] **Step 4: Run the migration locally to verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/scoped-api-keys
# Use the macOS app's PostgreSQL (port 5433, user lka, db lka)
DATABASE_URL="postgresql+asyncpg://lka@localhost:5433/lka" uv run alembic upgrade head 2>&1 | tail -5
```

Expected: migration 0013 applied successfully.

- [ ] **Step 5: Manual smoke test**

1. Create a scoped API key via `POST /api/api-keys` with curl using a JWT
2. Use that key to GET /api/docs — verify it only sees scoped documents
3. Use it on the MCP endpoint — verify only allowed tools appear in `tools/list`
4. Try calling a disallowed tool — verify it returns "unknown tool"
5. Set `expires_at` to a past date via PATCH — verify auth fails with 401

- [ ] **Step 6: Push the branch and open a PR**

```bash
git push -u origin feat/scoped-api-keys
gh pr create --title "feat: scoped API keys" --body "Implements per-key access controls per spec docs/superpowers/specs/2026-04-07-scoped-api-keys-design.md"
```
