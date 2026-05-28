# Folder-Scope Filter for Local Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-driven folder filter to Ask, Research, and Search — same semantics as `KeyScope.scope_folder_ids` on API keys — with a forward-compatible `scope: {folder_ids: [...]}` wrapper that admits future collection axes additively.

**Architecture:** A new `apply_folder_scope()` query helper (extracted from the existing `apply_key_scope`) is called by both the API-key path and a new user-scope path. A `UserScope` dataclass parallels `KeyScope` and rides on the `Principal` ContextVar through chat / research tool dispatch. Conversations and research_state get a `scope JSONB NOT NULL DEFAULT '{}'` column; search reads scope per-request only.

**Tech Stack:** FastAPI + SQLAlchemy 2 async, Pydantic v2, Alembic, PostgreSQL 18 (JSONB), React 19 + TanStack Query, vitest/RTL on frontend.

**Spec:** `docs/superpowers/specs/2026-05-27-folder-scope-for-local-tools-design.md`

---

## Pre-flight

- [ ] **Step 0.1: Confirm branch state**

Run: `git rev-parse --abbrev-ref HEAD && git log --oneline -1`
Expected: branch `feat/folder-scope-local-tools`, HEAD is the spec commit (`43b92ff docs(spec): folder-scope filter for Ask/Research/Search`).

If not on that branch, run `git checkout feat/folder-scope-local-tools`.

- [ ] **Step 0.2: Confirm uncommitted state is empty (besides the deferred CLI experiment spec)**

Run: `git status -s`
Expected: only `?? docs/superpowers/specs/2026-05-27-cli-vs-tool-call-small-model-experiment-design.md` (intentionally untracked from earlier).

---

## Task 1: Alembic migration — `conversations.scope` and `research_state.scope`

**Why:** Both Ask and Research persist the user's scope choice on their natural container.

**Files:**
- Create: `alembic/versions/0005_user_scope_columns.py`
- Test: covered by Task 2 model tests (the migration upgrade/downgrade is verified by `alembic upgrade head` + `alembic downgrade -1` round-trip).

- [ ] **Step 1.1: Identify current head revision**

Run: `uv run alembic heads`
Expected: one head, currently `d65a570f594c` (email metadata trgm/GIN indexes, from PR #414). If you see a different revision, confirm the branch has been rebased onto current `origin/main` before continuing — otherwise the migration chain will fork.

- [ ] **Step 1.2: Create the migration file**

Create `alembic/versions/0005_user_scope_columns.py` with:

```python
"""user scope columns on conversations and research_state

Adds a `scope` JSONB NOT NULL DEFAULT '{}' column to both tables, holding
the user's folder-filter selection per conversation/run. Forward-compatible
wrapper object — new scope axes (collection_ids, doc_ids, topic_ids) are
additive JSON keys with no further DDL.

Revision ID: 0005_user_scope_columns
Revises: d65a570f594c
Create Date: 2026-05-27 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0005_user_scope_columns"
down_revision = "d65a570f594c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "research_state",
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_state", "scope")
    op.drop_column("conversations", "scope")
```

- [ ] **Step 1.3: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade d65a570f594c -> 0005_user_scope_columns, user scope columns on conversations and research_state`.

- [ ] **Step 1.4: Verify the columns exist with the correct default**

Run:
```bash
psql "$DATABASE_URL" -c "\d+ conversations" | grep scope
psql "$DATABASE_URL" -c "\d+ research_state" | grep scope
```
Expected: both rows show `scope | jsonb | not null | '{}'::jsonb`.

- [ ] **Step 1.5: Verify downgrade round-trip**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: downgrade drops both columns cleanly, upgrade restores them.

- [ ] **Step 1.6: Commit**

```bash
git add alembic/versions/0005_user_scope_columns.py
git commit -m "feat(scope): alembic migration for conversations.scope and research_state.scope"
```

---

## Task 2: ORM model updates — add `scope` field to `Conversation` and `ResearchState`

**Files:**
- Modify: `src/harbor_clerk/models/conversation.py`
- Modify: `src/harbor_clerk/models/research_state.py`
- Test: `tests/test_user_scope_models.py` (new)

- [ ] **Step 2.1: Write the failing model test**

Create `tests/test_user_scope_models.py`:

```python
"""Tests for the new scope JSONB column on Conversation and ResearchState."""

import uuid

import pytest
from sqlalchemy import select

from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState
from harbor_clerk.models.user import User


@pytest.mark.asyncio
async def test_conversation_scope_defaults_to_empty_dict(async_session, admin_user):
    """A new conversation has scope == {} when not explicitly set."""
    conv = Conversation(user_id=admin_user.user_id, title="Test")
    async_session.add(conv)
    await async_session.commit()
    await async_session.refresh(conv)

    assert conv.scope == {}


@pytest.mark.asyncio
async def test_conversation_scope_round_trips_folder_ids(async_session, admin_user):
    """Setting scope persists the JSONB shape correctly."""
    folder_id = str(uuid.uuid4())
    conv = Conversation(
        user_id=admin_user.user_id,
        title="Scoped",
        scope={"folder_ids": [folder_id]},
    )
    async_session.add(conv)
    await async_session.commit()
    await async_session.refresh(conv)

    assert conv.scope == {"folder_ids": [folder_id]}

    # Round-trip via fresh query
    fetched = (await async_session.execute(
        select(Conversation).where(Conversation.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert fetched.scope == {"folder_ids": [folder_id]}


@pytest.mark.asyncio
async def test_research_state_scope_defaults_to_empty_dict(async_session, admin_user):
    """A new research_state row has scope == {} when not explicitly set."""
    conv = Conversation(user_id=admin_user.user_id, title="Research conv", mode="research")
    async_session.add(conv)
    await async_session.flush()
    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy="search",
        status="queued",
        max_rounds=5,
    )
    async_session.add(state)
    await async_session.commit()
    await async_session.refresh(state)

    assert state.scope == {}
```

- [ ] **Step 2.2: Run the test, verify it fails on the missing attribute**

Run: `uv run pytest tests/test_user_scope_models.py -v`
Expected: FAIL with `AttributeError: 'Conversation' object has no attribute 'scope'` (or similar).

- [ ] **Step 2.3: Add `scope` to `Conversation`**

Modify `src/harbor_clerk/models/conversation.py`. Add the import and the column:

```python
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        server_default="New conversation",
    )
    mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="chat")
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
        default=dict,
    )
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)
```

- [ ] **Step 2.4: Add `scope` to `ResearchState`**

Modify `src/harbor_clerk/models/research_state.py`. Add the column alongside the other JSONB columns:

```python
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
        default=dict,
    )
```

Place it directly above `citations: Mapped[Any | None] = ...` so related JSON columns sit together.

- [ ] **Step 2.5: Run the test, verify it passes**

Run: `uv run pytest tests/test_user_scope_models.py -v`
Expected: PASS — all three tests.

- [ ] **Step 2.6: Run the full test suite to catch regressions**

Run: `uv run pytest -x --ignore=tests/integration`
Expected: PASS — no regressions from the model change.

- [ ] **Step 2.7: Commit**

```bash
git add src/harbor_clerk/models/conversation.py src/harbor_clerk/models/research_state.py tests/test_user_scope_models.py
git commit -m "feat(scope): add scope JSONB column to Conversation and ResearchState"
```

---

## Task 3: Extract `apply_folder_scope` helper from `apply_key_scope`

**Why:** Single source of truth for folder-axis filtering. Both `apply_key_scope` (API-key path) and the new user-scope path call it.

**Files:**
- Modify: `src/harbor_clerk/api/scope.py`
- Test: `tests/test_folder_scope_helper.py` (new)

- [ ] **Step 3.1: Write the failing test**

Create `tests/test_folder_scope_helper.py`:

```python
"""Tests for apply_folder_scope — the pure query builder shared by
apply_key_scope and the user-side folder filter."""

import uuid

import pytest
from sqlalchemy import select

from harbor_clerk.api.scope import apply_folder_scope
from harbor_clerk.models.document import Document
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus


@pytest.mark.asyncio
async def test_apply_folder_scope_none_is_noop(async_session, two_folder_corpus):
    """folder_ids=None returns the query unchanged — all docs visible."""
    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, None)

    all_ids = {r[0] for r in (await async_session.execute(query)).all()}
    scoped_ids = {r[0] for r in (await async_session.execute(scoped)).all()}

    assert scoped_ids == all_ids


@pytest.mark.asyncio
async def test_apply_folder_scope_empty_list_is_noop(async_session, two_folder_corpus):
    """folder_ids=[] also means no restriction."""
    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [])

    all_ids = {r[0] for r in (await async_session.execute(query)).all()}
    scoped_ids = {r[0] for r in (await async_session.execute(scoped)).all()}

    assert scoped_ids == all_ids


@pytest.mark.asyncio
async def test_apply_folder_scope_restricts_to_named_folder(async_session, two_folder_corpus):
    """folder_ids=[folder_a] returns only docs in folder_a."""
    folder_a, folder_b, docs_in_a, docs_in_b = two_folder_corpus

    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [folder_a.folder_id])

    scoped_ids = {r[0] for r in (await async_session.execute(scoped)).all()}
    expected_ids = {d.doc_id for d in docs_in_a}
    assert scoped_ids == expected_ids


@pytest.mark.asyncio
async def test_apply_folder_scope_excludes_removed_watched_files(async_session, two_folder_corpus, mark_one_removed):
    """A WatchedFile in `removed` status no longer surfaces its document."""
    folder_a, _, _, _ = two_folder_corpus
    removed_doc_id = await mark_one_removed(folder_a)

    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [folder_a.folder_id])
    scoped_ids = {r[0] for r in (await async_session.execute(scoped)).all()}

    assert removed_doc_id not in scoped_ids
```

(The `two_folder_corpus` and `mark_one_removed` fixtures are added in step 3.2.)

- [ ] **Step 3.2: Add the fixtures to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
import pytest
from harbor_clerk.models.watched import WatchedFile, WatchedFolder, WatchedFileStatus
from harbor_clerk.models.document import Document


@pytest.fixture
async def two_folder_corpus(async_session, admin_user):
    """Create two watched folders, each with 2 active documents."""
    folder_a = WatchedFolder(label="Folder A", path="/a", auto_discovered=False)
    folder_b = WatchedFolder(label="Folder B", path="/b", auto_discovered=False)
    async_session.add_all([folder_a, folder_b])
    await async_session.flush()

    docs_in_a, docs_in_b = [], []
    for i, folder in [(0, folder_a), (1, folder_a), (2, folder_b), (3, folder_b)]:
        d = Document(
            title=f"Doc {i}",
            canonical_filename=f"doc{i}.txt",
            status="active",
            sha256=bytes(f"{i:032d}", "utf-8")[:32],
        )
        async_session.add(d)
        await async_session.flush()
        wf = WatchedFile(
            folder_id=folder.folder_id,
            doc_id=d.doc_id,
            relative_path=f"doc{i}.txt",
            sha256=d.sha256,
            status=WatchedFileStatus.active,
        )
        async_session.add(wf)
        (docs_in_a if folder is folder_a else docs_in_b).append(d)

    await async_session.commit()
    return folder_a, folder_b, docs_in_a, docs_in_b


@pytest.fixture
async def mark_one_removed(async_session):
    """Helper to mark one document's WatchedFile as removed, returning its doc_id."""
    async def _mark(folder):
        from sqlalchemy import select
        wf = (await async_session.execute(
            select(WatchedFile).where(WatchedFile.folder_id == folder.folder_id).limit(1)
        )).scalar_one()
        wf.status = WatchedFileStatus.removed
        await async_session.commit()
        return wf.doc_id
    return _mark
```

(If `admin_user` and `async_session` fixtures don't already exist in conftest, copy them from `tests/test_scoped_api_keys.py` which has the same pattern.)

- [ ] **Step 3.3: Run the test, verify it fails on missing import**

Run: `uv run pytest tests/test_folder_scope_helper.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply_folder_scope' from 'harbor_clerk.api.scope'`.

- [ ] **Step 3.4: Extract `apply_folder_scope` in `scope.py`**

Replace the body of `apply_key_scope` to delegate. Edit `src/harbor_clerk/api/scope.py`:

```python
def apply_folder_scope(query: Select, folder_ids: list[uuid.UUID] | None) -> Select:
    """Filter a Document query to documents whose active WatchedFile lives in any
    of the given folders. No-op when folder_ids is None or empty.

    Pure query builder — no DB access. Safe to compose with other WHERE clauses.
    """
    if not folder_ids:
        return query
    watched_doc_ids = select(WatchedFile.doc_id).where(
        WatchedFile.folder_id.in_(folder_ids),
        WatchedFile.status == WatchedFileStatus.active,
    )
    return query.where(Document.doc_id.in_(watched_doc_ids))


def apply_key_scope(query: Select, principal: "Principal") -> Select:
    """Filter a Document query by the API key's scope (topic OR folder)."""
    if principal.type != "api_key" or principal.key_scope is None:
        return query
    scope = principal.key_scope
    if scope.is_unrestricted:
        return query

    conditions = []
    if scope.scope_topic_ids:
        conditions.append(Document.topic_id.in_(scope.scope_topic_ids))
    if scope.scope_folder_ids:
        try:
            folder_uuids = [uuid.UUID(fid) for fid in scope.scope_folder_ids]
        except (ValueError, AttributeError):
            folder_uuids = []
        if folder_uuids:
            # Reuse the folder helper for the folder branch.
            folder_filter = apply_folder_scope(select(Document.doc_id), folder_uuids)
            conditions.append(Document.doc_id.in_(folder_filter))

    if not conditions:
        return query.where(Document.doc_id == uuid.UUID(int=0))

    return query.where(or_(*conditions))
```

- [ ] **Step 3.5: Run both the new test and the existing key-scope tests**

Run: `uv run pytest tests/test_folder_scope_helper.py tests/test_scoped_api_keys.py -v`
Expected: PASS — all tests, including the existing key-scope ones (no regression).

- [ ] **Step 3.6: Commit**

```bash
git add src/harbor_clerk/api/scope.py tests/test_folder_scope_helper.py tests/conftest.py
git commit -m "feat(scope): extract apply_folder_scope helper from apply_key_scope"
```

---

## Task 4: `UserScope` dataclass + `Principal.user_scope`

**Why:** The chat / research tool dispatch path runs under `Principal(type="user")` today; we need a place to attach the user's folder selection so each MCP tool body can read it from the current Principal.

**Files:**
- Modify: `src/harbor_clerk/api/scope.py` (add `UserScope`)
- Modify: `src/harbor_clerk/api/deps.py` (extend `Principal`)
- Test: `tests/test_user_scope_dataclass.py` (new)

- [ ] **Step 4.1: Write the failing test**

Create `tests/test_user_scope_dataclass.py`:

```python
"""Tests for UserScope and Principal.user_scope."""

import uuid

import pytest

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import UserScope


def test_user_scope_defaults():
    """Empty UserScope means no restriction (matches KeyScope semantics)."""
    scope = UserScope()
    assert scope.folder_ids is None
    assert scope.is_unrestricted is True


def test_user_scope_is_unrestricted_with_empty_list():
    """folder_ids=[] also unrestricted (matches KeyScope)."""
    scope = UserScope(folder_ids=[])
    assert scope.is_unrestricted is True


def test_user_scope_with_folders_is_restricted():
    folder_id = uuid.uuid4()
    scope = UserScope(folder_ids=[folder_id])
    assert scope.is_unrestricted is False
    assert scope.folder_ids == [folder_id]


def test_principal_user_scope_defaults_to_none():
    """API-key principals carry key_scope; user principals carry user_scope. Both default None."""
    p = Principal(type="user", id=uuid.uuid4(), role="user")
    assert p.user_scope is None
    assert p.key_scope is None


def test_principal_can_carry_user_scope():
    folder_id = uuid.uuid4()
    p = Principal(
        type="user",
        id=uuid.uuid4(),
        role="user",
        user_scope=UserScope(folder_ids=[folder_id]),
    )
    assert p.user_scope is not None
    assert p.user_scope.folder_ids == [folder_id]
```

- [ ] **Step 4.2: Run the test, verify it fails on missing import**

Run: `uv run pytest tests/test_user_scope_dataclass.py -v`
Expected: FAIL with `ImportError: cannot import name 'UserScope'`.

- [ ] **Step 4.3: Add `UserScope` to `scope.py`**

Append to `src/harbor_clerk/api/scope.py`:

```python
@dataclass
class UserScope:
    """Per-request scope for human-user tool calls.

    Distinct from KeyScope: no permission_tier / tool_overrides / rate_limit —
    human users authenticate via JWT and aren't tool-gated. Only document-axis
    filters live here. Forward-compatible: collection_ids, doc_ids, topic_ids
    will be added here additively when Collections ships.
    """

    folder_ids: list[uuid.UUID] | None = None

    @property
    def is_unrestricted(self) -> bool:
        """True when no document-axis filter applies. Both None and [] count."""
        return not self.folder_ids
```

- [ ] **Step 4.4: Extend `Principal` in `deps.py`**

Modify `src/harbor_clerk/api/deps.py`:

```python
from harbor_clerk.api.scope import KeyScope, UserScope


@dataclass
class Principal:
    type: str
    id: uuid.UUID
    role: str
    key_scope: KeyScope | None = None    # populated for api_key principals only
    user_scope: UserScope | None = None  # populated for user principals when a scope is active
```

- [ ] **Step 4.5: Run the test, verify it passes**

Run: `uv run pytest tests/test_user_scope_dataclass.py -v`
Expected: PASS — all 5 tests.

- [ ] **Step 4.6: Run the full suite to catch regressions**

Run: `uv run pytest -x --ignore=tests/integration`
Expected: PASS.

- [ ] **Step 4.7: Commit**

```bash
git add src/harbor_clerk/api/scope.py src/harbor_clerk/api/deps.py tests/test_user_scope_dataclass.py
git commit -m "feat(scope): add UserScope dataclass and Principal.user_scope field"
```

---

## Task 5: `ScopeSpec` Pydantic schema

**Why:** Single schema reused by chat / research / search request bodies. `extra="ignore"` makes future axes additive without endpoint version bumps.

**Files:**
- Create: `src/harbor_clerk/api/schemas/scope.py`
- Test: `tests/test_scope_spec_schema.py` (new)

- [ ] **Step 5.1: Write the failing test**

Create `tests/test_scope_spec_schema.py`:

```python
"""Tests for the ScopeSpec Pydantic schema."""

import uuid

import pytest
from pydantic import ValidationError

from harbor_clerk.api.schemas.scope import ScopeSpec


def test_empty_scope_spec_round_trips():
    """ScopeSpec() = empty = no restriction. Round-trips to/from {}."""
    spec = ScopeSpec()
    assert spec.folder_ids is None
    assert spec.model_dump(exclude_none=True) == {}


def test_scope_spec_with_folder_ids():
    folder_id = uuid.uuid4()
    spec = ScopeSpec(folder_ids=[folder_id])
    assert spec.folder_ids == [folder_id]


def test_scope_spec_accepts_empty_folder_ids_list():
    """An explicit empty list is valid and equivalent to None per the spec."""
    spec = ScopeSpec(folder_ids=[])
    assert spec.folder_ids == []


def test_scope_spec_ignores_unknown_keys():
    """Future axes (collection_ids, doc_ids, etc.) sent by newer clients to an older
    server must parse without error. extra='ignore' guarantees forward-compat."""
    spec = ScopeSpec.model_validate(
        {"folder_ids": [str(uuid.uuid4())], "collection_ids": ["future-key"], "extra_weirdness": 42}
    )
    assert spec.folder_ids is not None
    # The unknown keys are silently dropped.
    assert "collection_ids" not in spec.model_dump()


def test_scope_spec_rejects_non_uuid_folder_ids():
    """Bad UUIDs should fail validation cleanly with 422."""
    with pytest.raises(ValidationError):
        ScopeSpec(folder_ids=["not-a-uuid"])
```

- [ ] **Step 5.2: Run the test, verify it fails on missing module**

Run: `uv run pytest tests/test_scope_spec_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harbor_clerk.api.schemas.scope'`.

- [ ] **Step 5.3: Create the `ScopeSpec` schema**

Create `src/harbor_clerk/api/schemas/scope.py`:

```python
"""Shared scope schema for Ask / Research / Search request bodies.

Forward-compatible wrapper: today only `folder_ids` is honored. Future axes
(collection_ids, doc_ids, topic_ids) can be added as new optional fields with
no migration and no endpoint version bump. extra='ignore' on the model lets
older servers tolerate newer clients sending unknown keys.
"""

import uuid

from pydantic import BaseModel, ConfigDict


class ScopeSpec(BaseModel):
    """A user-driven document-visibility scope.

    Empty (`{}`) or all fields None/[] means no restriction — all active
    documents are visible. When fields are populated, scoping mirrors
    KeyScope's OR-across-axes semantics (today: only folder_ids).
    """

    model_config = ConfigDict(extra="ignore")

    folder_ids: list[uuid.UUID] | None = None
    # Future axes will be added here additively. Examples (not yet active):
    #   collection_ids: list[uuid.UUID] | None = None
    #   doc_ids: list[uuid.UUID] | None = None
    #   topic_ids: list[int] | None = None
```

- [ ] **Step 5.4: Run the test, verify it passes**

Run: `uv run pytest tests/test_scope_spec_schema.py -v`
Expected: PASS — all 5 tests.

- [ ] **Step 5.5: Commit**

```bash
git add src/harbor_clerk/api/schemas/scope.py tests/test_scope_spec_schema.py
git commit -m "feat(scope): add ScopeSpec Pydantic schema with forward-compat extra='ignore'"
```

---

## Task 6: Extend `_visible_doc_ids` to honor `user_scope`

**Why:** `_visible_doc_ids()` in `mcp_server.py` is the central function that every MCP tool body calls (directly or via `apply_key_scope`) to determine visibility. Extending it to also recognize `user_scope` gives all 19 MCP tools user-side folder scoping in one place.

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py:316-329`
- Test: `tests/test_visible_doc_ids_user_scope.py` (new)

- [ ] **Step 6.1: Write the failing test**

Create `tests/test_visible_doc_ids_user_scope.py`:

```python
"""Tests for _visible_doc_ids honoring user_scope alongside key_scope."""

import uuid

import pytest

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import UserScope
from harbor_clerk.mcp_server import _visible_doc_ids


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_no_scope_returns_none(async_session, admin_user, two_folder_corpus):
    """User principal with no user_scope → None (unrestricted)."""
    p = Principal(type="user", id=admin_user.user_id, role="user")
    visible = await _visible_doc_ids(async_session, p)
    assert visible is None


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_with_empty_scope_returns_none(async_session, admin_user, two_folder_corpus):
    """user_scope with folder_ids=[] is unrestricted."""
    p = Principal(
        type="user", id=admin_user.user_id, role="user",
        user_scope=UserScope(folder_ids=[]),
    )
    visible = await _visible_doc_ids(async_session, p)
    assert visible is None


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_with_folder_scope_restricts(async_session, admin_user, two_folder_corpus):
    """user_scope with folder_ids returns only docs in those folders."""
    folder_a, _, docs_in_a, _ = two_folder_corpus
    p = Principal(
        type="user", id=admin_user.user_id, role="user",
        user_scope=UserScope(folder_ids=[folder_a.folder_id]),
    )
    visible = await _visible_doc_ids(async_session, p)
    expected = {d.doc_id for d in docs_in_a}
    assert visible == expected
```

- [ ] **Step 6.2: Run the test, verify it fails (returns None when it shouldn't)**

Run: `uv run pytest tests/test_visible_doc_ids_user_scope.py -v`
Expected: FAIL on `test_visible_doc_ids_user_principal_with_folder_scope_restricts` — `visible` will be `None` because today's `_visible_doc_ids` only handles API-key principals.

- [ ] **Step 6.3: Extend `_visible_doc_ids`**

Modify `src/harbor_clerk/mcp_server.py` (the function around line 316):

```python
async def _visible_doc_ids(session: AsyncSession, principal: Principal) -> set[uuid.UUID] | None:
    """Get the set of doc_ids visible to this principal, or None if unrestricted.

    Returns None when no scope filter applies (API key with no scope, OR human
    user with no user_scope). Returns the explicit set for scoped keys/users —
    callers use this to filter results. Only active (non-removed) documents
    are included.
    """
    # API-key path (today's behavior, unchanged)
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        from harbor_clerk.api.scope import apply_key_scope

        query = apply_key_scope(select(Document.doc_id).where(Document.status == "active"), principal)
        result = await session.execute(query)
        return {row[0] for row in result.all()}

    # User-scope path (new)
    if principal.type == "user" and principal.user_scope is not None and not principal.user_scope.is_unrestricted:
        from harbor_clerk.api.scope import apply_folder_scope

        query = apply_folder_scope(
            select(Document.doc_id).where(Document.status == "active"),
            principal.user_scope.folder_ids,
        )
        result = await session.execute(query)
        return {row[0] for row in result.all()}

    return None
```

- [ ] **Step 6.4: Run the test, verify it passes**

Run: `uv run pytest tests/test_visible_doc_ids_user_scope.py -v`
Expected: PASS — all 3 tests.

- [ ] **Step 6.5: Run the broader MCP test suite to confirm no regression in the API-key path**

Run: `uv run pytest tests/test_scoped_api_keys.py tests/test_visible_doc_ids_user_scope.py -v`
Expected: PASS — both suites pass.

- [ ] **Step 6.6: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_visible_doc_ids_user_scope.py
git commit -m "feat(scope): extend _visible_doc_ids to honor Principal.user_scope"
```

---

## Task 7: `execute_tool` accepts `user_scope` and attaches it to `Principal`

**Why:** Chat and research tool dispatch flow through `execute_tool()`. Once it forwards `user_scope` onto the Principal stored in `_mcp_principal`, every MCP tool body sees the scope (via `_visible_doc_ids` from Task 6) with no further changes.

**Files:**
- Modify: `src/harbor_clerk/llm/tools.py` (the `execute_tool` function around line 648)
- Test: `tests/test_execute_tool_user_scope.py` (new)

- [ ] **Step 7.1: Write the failing test**

Create `tests/test_execute_tool_user_scope.py`:

```python
"""Tests for execute_tool forwarding user_scope onto Principal."""

import json
import uuid

import pytest

from harbor_clerk.api.scope import UserScope
from harbor_clerk.llm.tools import execute_tool


@pytest.mark.asyncio
async def test_execute_tool_search_with_no_scope_returns_all(async_session, admin_user, two_folder_corpus):
    """No user_scope = no filter = all docs from both folders visible."""
    result_str = await execute_tool(
        "search_documents",
        {"query": "doc"},
        user_id=admin_user.user_id,
    )
    result = json.loads(result_str)
    assert "hits" in result
    titles = {hit["doc_title"] for hit in result["hits"] if hit.get("doc_title")}
    # Both folders' docs reachable
    assert any("Doc 0" in t or "Doc 1" in t for t in titles)
    assert any("Doc 2" in t or "Doc 3" in t for t in titles)


@pytest.mark.asyncio
async def test_execute_tool_search_with_folder_scope_restricts(async_session, admin_user, two_folder_corpus):
    """user_scope=folder_a → only folder_a docs visible."""
    folder_a, _, docs_in_a, _ = two_folder_corpus
    scope = UserScope(folder_ids=[folder_a.folder_id])

    result_str = await execute_tool(
        "search_documents",
        {"query": "doc"},
        user_id=admin_user.user_id,
        user_scope=scope,
    )
    result = json.loads(result_str)
    expected_ids = {str(d.doc_id) for d in docs_in_a}
    returned_ids = {hit["doc_id"] for hit in result.get("hits", [])}
    # Every returned hit is in folder_a
    assert returned_ids.issubset(expected_ids)
```

(This test depends on the chunks for `two_folder_corpus` being indexable. If chunk/embedding generation isn't fixture-friendly, restrict the test to a tool that returns documents by ID directly — `list_documents` or `corpus_overview` — which doesn't need indexed chunks. Update the fixture accordingly.)

- [ ] **Step 7.2: Run the test, verify it fails on unexpected `user_scope` kwarg**

Run: `uv run pytest tests/test_execute_tool_user_scope.py::test_execute_tool_search_with_folder_scope_restricts -v`
Expected: FAIL — `TypeError: execute_tool() got an unexpected keyword argument 'user_scope'`.

- [ ] **Step 7.3: Modify `execute_tool` to accept `user_scope`**

Modify `src/harbor_clerk/llm/tools.py` (around line 648):

```python
async def execute_tool(
    name: str,
    arguments: dict,
    user_id: uuid.UUID | None = None,
    *,
    mode: str = "chat",
    user_scope: "UserScope | None" = None,
) -> str:
    """Execute a tool by delegating to the corresponding MCP function.

    mode: "chat" (conservative limits) or "research" (permissive limits).
    user_scope: optional folder-level scope to apply to all retrievals in
        this tool call. Mirrors KeyScope.scope_folder_ids semantics.
    """
    from harbor_clerk.api.deps import Principal
    from harbor_clerk.api.scope import UserScope  # noqa: F401 (for forward ref)
    from harbor_clerk.mcp_server import _mcp_principal

    if name == "corpus_topics":
        from harbor_clerk.topics import get_topics_for_tool

        return await get_topics_for_tool()

    dispatch = _RESEARCH_TOOL_DISPATCH if mode == "research" else _TOOL_DISPATCH
    entry = dispatch.get(name)
    if entry is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    mcp_func_name, arg_mapper = entry
    mapped_args = arg_mapper(arguments)

    # Set MCP auth context so the tool function sees the chat user and scope
    token = None
    if user_id is not None:
        principal = Principal(type="user", id=user_id, role="user", user_scope=user_scope)
        token = _mcp_principal.set(principal)

    try:
        import harbor_clerk.mcp_server as mcp_mod

        func = getattr(mcp_mod, mcp_func_name)
        return await func(**mapped_args)
    except PermissionError as e:
        logger.warning("Tool permission error: %s - %s", name, e)
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("Tool execution error: %s", name)
        return json.dumps({"error": str(e)})
    finally:
        if token is not None:
            _mcp_principal.reset(token)
```

- [ ] **Step 7.4: Run the test, verify it passes**

Run: `uv run pytest tests/test_execute_tool_user_scope.py -v`
Expected: PASS — both tests.

- [ ] **Step 7.5: Confirm existing tool-execution tests still pass**

Run: `uv run pytest tests/ -k "execute_tool or tool_dispatch" -v`
Expected: PASS — no regressions.

- [ ] **Step 7.6: Commit**

```bash
git add src/harbor_clerk/llm/tools.py tests/test_execute_tool_user_scope.py
git commit -m "feat(scope): execute_tool accepts user_scope and forwards to Principal"
```

---

## Task 8: Chat route — accept and persist `scope` on conversation creation

**Files:**
- Modify: `src/harbor_clerk/api/schemas/chat.py` (extend `CreateConversationRequest`, `ConversationSummary`, `ConversationDetail`)
- Modify: `src/harbor_clerk/api/routes/chat.py` (`create_conversation`, `list_conversations`, `get_conversation`)
- Test: `tests/test_chat_route_scope.py` (new)

- [ ] **Step 8.1: Write the failing test**

Create `tests/test_chat_route_scope.py`:

```python
"""Tests for the scope field on POST /api/chat/conversations and GET responses."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_create_conversation_with_no_scope_field_defaults_to_empty(async_client, admin_token):
    """No scope → conversation.scope == {} in DB, scope == {} in response."""
    r = await async_client.post(
        "/api/chat/conversations",
        json={"title": "Unscoped"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {}


@pytest.mark.asyncio
async def test_create_conversation_with_folder_scope_persists(async_client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    r = await async_client.post(
        "/api/chat/conversations",
        json={"title": "Scoped", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}


@pytest.mark.asyncio
async def test_create_conversation_rejects_unknown_folder_id(async_client, admin_token):
    """Unknown UUID → 422."""
    r = await async_client.post(
        "/api/chat/conversations",
        json={"title": "Bad", "scope": {"folder_ids": [str(uuid.uuid4())]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_conversation_rejects_unavailable_folder(async_client, admin_token, unavailable_folder):
    """Folder with unavailable_reason set → 422."""
    r = await async_client.post(
        "/api/chat/conversations",
        json={"title": "Bad", "scope": {"folder_ids": [str(unavailable_folder.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_conversation_returns_scope(async_client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    create = await async_client.post(
        "/api/chat/conversations",
        json={"title": "S", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    conv_id = create.json()["conversation_id"]

    r = await async_client.get(
        f"/api/chat/conversations/{conv_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}
```

(Add an `unavailable_folder` fixture to `tests/conftest.py` — a `WatchedFolder` with `unavailable_reason="test-disabled"`.)

- [ ] **Step 8.2: Run the test, verify failures**

Run: `uv run pytest tests/test_chat_route_scope.py -v`
Expected: most tests FAIL — either 422 (scope not in schema, rejected by extra="forbid" if set) or 200 with response missing `scope`.

- [ ] **Step 8.3: Extend chat schemas**

Modify `src/harbor_clerk/api/schemas/chat.py`:

```python
"""Pydantic schemas for chat and model management endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from harbor_clerk.api.schemas.scope import ScopeSpec

# --- Conversations ---


class CreateConversationRequest(BaseModel):
    title: str = "New conversation"
    scope: ScopeSpec | None = None


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    scope: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    tool_calls: Any | None = None
    tool_call_id: str | None = None
    rag_context: Any | None = None
    tokens_used: int | None = None
    model_id: str | None = None
    context_pct: int | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    conversation_id: str
    title: str
    scope: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageOut]


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
```

- [ ] **Step 8.4: Add validation helper for `ScopeSpec.folder_ids`**

Create `src/harbor_clerk/api/scope_validation.py`:

```python
"""Validation: ensure ScopeSpec.folder_ids references existing, available folders."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.schemas.scope import ScopeSpec
from harbor_clerk.models.watched import WatchedFolder


async def validate_scope_folders(scope: ScopeSpec | None, session: AsyncSession) -> None:
    """Raise 422 if any folder_id is unknown or unavailable.

    No-op when scope is None or scope.folder_ids is None/empty.
    """
    if scope is None or not scope.folder_ids:
        return

    folder_ids: list[uuid.UUID] = list(scope.folder_ids)
    rows = (
        await session.execute(
            select(WatchedFolder.folder_id, WatchedFolder.unavailable_reason).where(
                WatchedFolder.folder_id.in_(folder_ids)
            )
        )
    ).all()

    found_ids = {r[0] for r in rows}
    unknown = [str(fid) for fid in folder_ids if fid not in found_ids]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown folder_ids: {unknown}")

    unavailable = [str(r[0]) for r in rows if r[1] is not None]
    if unavailable:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot scope to unavailable folders: {unavailable}",
        )
```

- [ ] **Step 8.5: Wire scope into `create_conversation`**

Modify `src/harbor_clerk/api/routes/chat.py` around line 105:

```python
@router.post("/chat/conversations", response_model=ConversationSummary)
async def create_conversation(
    body: CreateConversationRequest,
    principal: Principal = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    from harbor_clerk.api.scope_validation import validate_scope_folders

    await validate_scope_folders(body.scope, session)

    scope_dict = body.scope.model_dump(exclude_none=True) if body.scope else {}
    conv = Conversation(
        user_id=principal.id,
        title=body.title,
        scope=scope_dict,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)

    return ConversationSummary(
        conversation_id=str(conv.conversation_id),
        title=conv.title,
        scope=conv.scope,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )
```

Also update the `list_conversations` projection (around line 84) and `get_conversation` projection (around line 123) to include `scope=c.scope` / `scope=conv.scope` in their respective response objects.

- [ ] **Step 8.6: Run the chat-route tests**

Run: `uv run pytest tests/test_chat_route_scope.py -v`
Expected: PASS — all 5 tests.

- [ ] **Step 8.7: Run the broader chat tests to catch regressions**

Run: `uv run pytest tests/ -k chat -v`
Expected: PASS — no regressions.

- [ ] **Step 8.8: Commit**

```bash
git add src/harbor_clerk/api/schemas/chat.py src/harbor_clerk/api/routes/chat.py src/harbor_clerk/api/scope_validation.py tests/test_chat_route_scope.py tests/conftest.py
git commit -m "feat(scope): chat route stores and returns conversation.scope"
```

---

## Task 9: Chat engine loads conversation scope and threads it to `execute_tool`

**Why:** A conversation's stored scope only matters if it's actually applied at retrieval time.

**Files:**
- Modify: `src/harbor_clerk/llm/chat.py` (the function that drives a chat turn — around line 492 where `execute_tool` is called)
- Test: `tests/test_chat_engine_scope_threading.py` (new)

- [ ] **Step 9.1: Write the failing test**

Create `tests/test_chat_engine_scope_threading.py`:

```python
"""Verify that the chat engine loads conversation.scope and passes a UserScope
into execute_tool on every tool call."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.api.scope import UserScope


@pytest.mark.asyncio
async def test_chat_engine_passes_user_scope_to_execute_tool(
    async_session, admin_user, two_folder_corpus, scoped_conversation
):
    """When a conversation has scope.folder_ids set, execute_tool gets a matching UserScope."""
    folder_a, _, _, _ = two_folder_corpus
    conv = scoped_conversation(folders=[folder_a.folder_id])

    with patch("harbor_clerk.llm.chat.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = '{"hits": []}'
        # Drive a synthetic chat turn that forces a tool call
        # (helper that posts a user message and lets the engine run a single search)
        await _drive_chat_turn(conv.conversation_id, admin_user.user_id, "find termination clauses")

    # First positional/kwarg call should include user_scope
    call = mock_exec.call_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs.get("user_scope") is not None
    assert isinstance(kwargs["user_scope"], UserScope)
    assert kwargs["user_scope"].folder_ids == [folder_a.folder_id]


@pytest.mark.asyncio
async def test_chat_engine_unscoped_passes_no_user_scope(async_session, admin_user, unscoped_conversation):
    """A conversation with scope=={} → execute_tool called with user_scope=None (or UserScope with empty folder_ids)."""
    conv = unscoped_conversation()
    with patch("harbor_clerk.llm.chat.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = '{"hits": []}'
        await _drive_chat_turn(conv.conversation_id, admin_user.user_id, "anything")

    kwargs = mock_exec.call_args.kwargs
    scope = kwargs.get("user_scope")
    assert scope is None or scope.is_unrestricted
```

(The `scoped_conversation`, `unscoped_conversation`, and `_drive_chat_turn` helpers are test-internal — define them in this file or extend conftest. Use whichever helper pattern existing chat tests use; check `tests/test_chat*` for the right idiom.)

- [ ] **Step 9.2: Run the test, verify it fails**

Run: `uv run pytest tests/test_chat_engine_scope_threading.py -v`
Expected: FAIL — `execute_tool` is called without `user_scope` kwarg today.

- [ ] **Step 9.3: Modify `chat.py` to load conversation scope and thread it**

In `src/harbor_clerk/llm/chat.py`, find the function that runs the chat turn (the one that loads `conv` and eventually calls `execute_tool` around line 492). Add scope-loading at the top of the function and pass it through:

```python
# Near the start of the chat turn, after loading the conversation:
from harbor_clerk.api.scope import UserScope

user_scope: UserScope | None = None
scope_dict = (conv.scope or {}) if conv is not None else {}
folder_ids_raw = scope_dict.get("folder_ids") or []
if folder_ids_raw:
    user_scope = UserScope(folder_ids=[uuid.UUID(fid) if isinstance(fid, str) else fid for fid in folder_ids_raw])

# Then, every execute_tool call inside the loop:
result_str = await execute_tool(fn_name, fn_args, user_id, user_scope=user_scope)
```

There are multiple `execute_tool` call sites in `chat.py` — update each one. Search the file for `execute_tool(` and add `user_scope=user_scope` to every call.

- [ ] **Step 9.4: Run the test, verify it passes**

Run: `uv run pytest tests/test_chat_engine_scope_threading.py -v`
Expected: PASS — both tests.

- [ ] **Step 9.5: Run the broader chat test suite**

Run: `uv run pytest tests/ -k chat -v`
Expected: PASS — no regression.

- [ ] **Step 9.6: Commit**

```bash
git add src/harbor_clerk/llm/chat.py tests/test_chat_engine_scope_threading.py
git commit -m "feat(scope): chat engine threads conversation scope into execute_tool"
```

---

## Task 10: Research route — accept and persist `scope` on research start

**Files:**
- Modify: `src/harbor_clerk/api/schemas/research.py` (`StartResearchRequest`, `ResearchSummary`, `ResearchDetail`)
- Modify: `src/harbor_clerk/api/routes/research.py` (the `POST /research` handler around line 209)
- Test: `tests/test_research_route_scope.py` (new)

- [ ] **Step 10.1: Write the failing test**

Create `tests/test_research_route_scope.py`:

```python
"""Tests for scope on POST /api/research and GET /api/research/{id}."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_start_research_with_no_scope_defaults_to_empty(async_client, admin_token):
    r = await async_client.post(
        "/api/research",
        json={"question": "What are the termination clauses?", "time_limit_minutes": 15, "depth": "light"},
        headers={"Authorization": f"Bearer {admin_token}"},
        params={"dry_run": "true"},  # if available; otherwise mock the engine
    )
    assert r.status_code in (200, 202)
    # research_id is in the response — fetch detail to inspect scope
    rid = r.json()["conversation_id"]
    d = await async_client.get(
        f"/api/research/{rid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert d.json()["scope"] == {}


@pytest.mark.asyncio
async def test_start_research_with_folder_scope_persists(async_client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    r = await async_client.post(
        "/api/research",
        json={
            "question": "Q",
            "time_limit_minutes": 15,
            "depth": "light",
            "scope": {"folder_ids": [str(folder_a.folder_id)]},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    rid = r.json()["conversation_id"]
    d = await async_client.get(
        f"/api/research/{rid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert d.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}


@pytest.mark.asyncio
async def test_start_research_rejects_unknown_folder_id(async_client, admin_token):
    r = await async_client.post(
        "/api/research",
        json={
            "question": "Q",
            "time_limit_minutes": 15,
            "depth": "light",
            "scope": {"folder_ids": [str(uuid.uuid4())]},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
```

- [ ] **Step 10.2: Run the test, verify it fails**

Run: `uv run pytest tests/test_research_route_scope.py -v`
Expected: FAIL — `scope` not in request schema and/or not in detail response.

- [ ] **Step 10.3: Extend research schemas**

Modify `src/harbor_clerk/api/schemas/research.py`:

```python
"""Pydantic schemas for research mode API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from harbor_clerk.api.schemas.scope import ScopeSpec


class StartResearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=10000)
    strategy: str | None = Field(default=None, pattern="^(search|sweep)$", description="Override default strategy")
    time_limit_minutes: int = Field(default=30, ge=15, le=180)
    depth: str = Field(default="standard", pattern="^(light|standard|thorough)$")
    scope: ScopeSpec | None = None
```

Add `scope: dict[str, Any] = {}` to `ResearchSummary` and `ResearchDetail`.

- [ ] **Step 10.4: Wire scope into `POST /research`**

Modify the handler around line 209 of `src/harbor_clerk/api/routes/research.py`:

```python
@router.post("/research")
async def start_research(
    body: StartResearchRequest,
    principal: Principal = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    from harbor_clerk.api.scope_validation import validate_scope_folders

    await validate_scope_folders(body.scope, session)
    scope_dict = body.scope.model_dump(exclude_none=True) if body.scope else {}

    # ... existing conversation + research_state creation ...
    # When creating the ResearchState row, include scope=scope_dict
```

(Apply the same pattern when creating the ResearchState — pass `scope=scope_dict` into the constructor.)

Also: update the `GET /research/{conv_id}` projection (line 97) and the list projection (line 63) to include `scope=state.scope` in their response objects.

- [ ] **Step 10.5: Run the test, verify it passes**

Run: `uv run pytest tests/test_research_route_scope.py -v`
Expected: PASS.

- [ ] **Step 10.6: Run the broader research test suite**

Run: `uv run pytest tests/ -k research -v`
Expected: PASS — no regressions.

- [ ] **Step 10.7: Commit**

```bash
git add src/harbor_clerk/api/schemas/research.py src/harbor_clerk/api/routes/research.py tests/test_research_route_scope.py
git commit -m "feat(scope): research route stores and returns research_state.scope"
```

---

## Task 11: Research engine loads `research_state.scope` and threads it through

**Why:** Same as Task 9 for chat. The engine has multiple `execute_tool()` call sites (lines ~659, ~680, ~760) and direct retrieval helpers. All need the user_scope plumbed through.

**Files:**
- Modify: `src/harbor_clerk/llm/research.py` (the research-run entry point around line 956, plus the helpers that call `execute_tool`)
- Test: `tests/test_research_engine_scope_threading.py` (new)

- [ ] **Step 11.1: Write the failing test**

Create `tests/test_research_engine_scope_threading.py`:

```python
"""Verify the research engine loads research_state.scope and passes UserScope into
every execute_tool call inside the run."""

from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.api.scope import UserScope


@pytest.mark.asyncio
async def test_research_engine_passes_user_scope(async_session, admin_user, two_folder_corpus, scoped_research_state):
    folder_a, _, _, _ = two_folder_corpus
    state = scoped_research_state(folders=[folder_a.folder_id])

    with patch("harbor_clerk.llm.research.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = '{"hits": []}'
        await _drive_one_research_round(state.conversation_id, admin_user.user_id)

    # Every execute_tool call should have user_scope.folder_ids == [folder_a.folder_id]
    for call in mock_exec.call_args_list:
        kwargs = call.kwargs
        assert "user_scope" in kwargs
        assert isinstance(kwargs["user_scope"], UserScope)
        assert kwargs["user_scope"].folder_ids == [folder_a.folder_id]
```

(`scoped_research_state` and `_drive_one_research_round` are test helpers — define in this file mirroring whatever pattern existing research tests use; check `tests/test_research*` for examples. If no existing pattern, use a minimal stub that creates the state row and calls the engine's per-round driver directly.)

- [ ] **Step 11.2: Run the test, verify it fails**

Run: `uv run pytest tests/test_research_engine_scope_threading.py -v`
Expected: FAIL — `user_scope` not in `execute_tool` kwargs.

- [ ] **Step 11.3: Load scope at the top of the research run + thread it**

Modify `src/harbor_clerk/llm/research.py`. At the start of the run (around line 956), load the scope:

```python
import uuid as _uuid
from harbor_clerk.api.scope import UserScope

async def run_research(conversation_id: uuid.UUID, user_id: uuid.UUID | None = None, ...):
    # ... existing setup ...
    state = await session.get(ResearchState, conversation_id)
    if state is None:
        ...

    # Load user scope from research_state.scope
    user_scope: UserScope | None = None
    scope_dict = state.scope or {}
    folder_ids_raw = scope_dict.get("folder_ids") or []
    if folder_ids_raw:
        user_scope = UserScope(
            folder_ids=[_uuid.UUID(fid) if isinstance(fid, str) else fid for fid in folder_ids_raw]
        )

    # ... pass user_scope to every helper that ultimately calls execute_tool
```

Then thread `user_scope` through the helper signatures that call `execute_tool`. Search `research.py` for `execute_tool(` and add `user_scope=user_scope` to every call. The helpers themselves may need to accept `user_scope: UserScope | None` as a kwarg if they're called from multiple places.

Also: any direct DB retrieval in research.py (look for `select(Document...)` or `select(Chunk...)` query construction) needs `apply_folder_scope(query, user_scope.folder_ids if user_scope else None)` applied.

- [ ] **Step 11.4: Run the test, verify it passes**

Run: `uv run pytest tests/test_research_engine_scope_threading.py -v`
Expected: PASS.

- [ ] **Step 11.5: Run the broader research test suite**

Run: `uv run pytest tests/ -k research -v`
Expected: PASS — no regressions.

- [ ] **Step 11.6: Commit**

```bash
git add src/harbor_clerk/llm/research.py tests/test_research_engine_scope_threading.py
git commit -m "feat(scope): research engine threads research_state.scope into execute_tool"
```

---

## Task 12: Search route — accept `scope` in request and apply it

**Files:**
- Modify: `src/harbor_clerk/api/schemas/search.py` (`SearchRequest`)
- Modify: `src/harbor_clerk/api/routes/search.py` (the `search()` handler around line 47)
- Test: `tests/test_search_route_scope.py` (new)

- [ ] **Step 12.1: Write the failing test**

Create `tests/test_search_route_scope.py`:

```python
"""Tests for scope on POST /api/search."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_search_with_no_scope_returns_all(async_client, admin_token, two_folder_corpus):
    r = await async_client.post(
        "/api/search",
        json={"query": "doc"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    # Either folder's docs may appear
    doc_ids = {h["doc_id"] for h in r.json()["hits"]}
    assert len(doc_ids) > 0


@pytest.mark.asyncio
async def test_search_with_folder_scope_restricts(async_client, admin_token, two_folder_corpus):
    folder_a, _, docs_in_a, _ = two_folder_corpus
    r = await async_client.post(
        "/api/search",
        json={"query": "doc", "scope": {"folder_ids": [str(folder_a.folder_id)]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    expected = {str(d.doc_id) for d in docs_in_a}
    returned = {h["doc_id"] for h in r.json()["hits"]}
    assert returned.issubset(expected)


@pytest.mark.asyncio
async def test_search_rejects_unknown_folder_id(async_client, admin_token):
    r = await async_client.post(
        "/api/search",
        json={"query": "x", "scope": {"folder_ids": [str(uuid.uuid4())]}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
```

- [ ] **Step 12.2: Run the test, verify it fails**

Run: `uv run pytest tests/test_search_route_scope.py -v`
Expected: FAIL — `scope` field is unknown to `SearchRequest`.

- [ ] **Step 12.3: Extend `SearchRequest`**

Modify `src/harbor_clerk/api/schemas/search.py`:

```python
from harbor_clerk.api.schemas.scope import ScopeSpec


class SearchRequest(BaseModel):
    query: str
    k: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    doc_id: str | None = None
    doc_ids: list[str] | None = Field(default=None, max_length=50)
    after: datetime | None = None
    before: datetime | None = None
    language: str | None = None
    mime_type: str | None = None
    faceted: bool = False
    scope: ScopeSpec | None = None    # new

    @model_validator(mode="after")
    def check_doc_id_mutual_exclusion(self):
        if self.doc_id is not None and self.doc_ids is not None:
            raise ValueError("Cannot specify both doc_id and doc_ids")
        return self
```

- [ ] **Step 12.4: Apply scope in the route handler**

Modify `src/harbor_clerk/api/routes/search.py` around line 47. Add scope validation + intersect-with-doc-ids logic, parallel to the existing API-key path:

```python
@router.post("/search", response_model=SearchResponse | FacetedSearchResponse)
async def search(
    body: SearchRequest,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    from harbor_clerk.api.scope import apply_folder_scope
    from harbor_clerk.api.scope_validation import validate_scope_folders

    await validate_scope_folders(body.scope, session)

    doc_id = uuid.UUID(body.doc_id) if body.doc_id else None
    doc_ids = [uuid.UUID(d) for d in body.doc_ids] if body.doc_ids else None

    # Per-API-key scope (today)
    if principal.type == "api_key" and principal.key_scope is not None and not principal.key_scope.is_unrestricted:
        visible_q = apply_key_scope(select(Document.doc_id), principal)
        visible_ids = {row[0] for row in (await session.execute(visible_q)).all()}
        # ... existing key-scope intersection logic, unchanged ...

    # Per-request user scope (new)
    if body.scope is not None and body.scope.folder_ids:
        scoped_q = apply_folder_scope(
            select(Document.doc_id).where(Document.status == "active"),
            list(body.scope.folder_ids),
        )
        scoped_visible = {row[0] for row in (await session.execute(scoped_q)).all()}
        if not scoped_visible:
            return SearchResponse(
                hits=[], total_candidates=0, has_more=False,
                possible_conflict=False, conflict_sources=[],
            )
        if doc_ids is not None:
            doc_ids = [d for d in doc_ids if d in scoped_visible]
            if not doc_ids:
                return SearchResponse(
                    hits=[], total_candidates=0, has_more=False,
                    possible_conflict=False, conflict_sources=[],
                )
        else:
            doc_ids = list(scoped_visible)
        if doc_id is not None and doc_id not in scoped_visible:
            return SearchResponse(
                hits=[], total_candidates=0, has_more=False,
                possible_conflict=False, conflict_sources=[],
            )

    # ... rest of the existing handler (hybrid_search call, response shaping) ...
```

- [ ] **Step 12.5: Run the test, verify it passes**

Run: `uv run pytest tests/test_search_route_scope.py -v`
Expected: PASS.

- [ ] **Step 12.6: Run the broader search tests**

Run: `uv run pytest tests/ -k search -v`
Expected: PASS — no regression.

- [ ] **Step 12.7: Commit**

```bash
git add src/harbor_clerk/api/schemas/search.py src/harbor_clerk/api/routes/search.py tests/test_search_route_scope.py
git commit -m "feat(scope): /api/search accepts and applies request-level scope"
```

---

## Task 13: Frontend — `useWatchedFolders` hook

**Why:** Three pages (Ask, Research, Search) need the same folder list. The hook centralizes the fetch + filters out unavailable folders.

**Files:**
- Create: `frontend/src/hooks/useWatchedFolders.ts`
- Test: `frontend/src/hooks/useWatchedFolders.test.tsx`

- [ ] **Step 13.1: Write the failing test**

Create `frontend/src/hooks/useWatchedFolders.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useWatchedFolders } from './useWatchedFolders';
import * as api from '../api/client';

describe('useWatchedFolders', () => {
  function wrapper({ children }: { children: React.ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }

  it('returns folders, filtering out unavailable ones', async () => {
    const mockGet = vi.spyOn(api, 'get').mockResolvedValue([
      { folder_id: 'a', label: 'A', path: '/a', unavailable_reason: null },
      { folder_id: 'b', label: 'B', path: '/b', unavailable_reason: 'unmounted' },
      { folder_id: 'c', label: 'C', path: '/c', unavailable_reason: null },
    ]);

    const { result } = renderHook(() => useWatchedFolders(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.folders.map((f) => f.folder_id)).toEqual(['a', 'c']);
    mockGet.mockRestore();
  });

  it('returns empty array when no folders', async () => {
    const mockGet = vi.spyOn(api, 'get').mockResolvedValue([]);
    const { result } = renderHook(() => useWatchedFolders(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.folders).toEqual([]);
    mockGet.mockRestore();
  });
});
```

- [ ] **Step 13.2: Run the test, verify it fails on missing module**

Run: `cd frontend && npm test -- useWatchedFolders`
Expected: FAIL — module not found.

- [ ] **Step 13.3: Implement the hook**

Create `frontend/src/hooks/useWatchedFolders.ts`:

```ts
import { useQuery } from '@tanstack/react-query';
import { get } from '../api/client';

export interface WatchedFolderInfo {
  folder_id: string;
  label: string;
  path: string;
  unavailable_reason: string | null;
}

export function useWatchedFolders() {
  const query = useQuery({
    queryKey: ['watch', 'folders'],
    queryFn: () => get<WatchedFolderInfo[]>('/api/watch/folders'),
    staleTime: 30_000,
  });

  const folders = (query.data ?? []).filter((f) => f.unavailable_reason === null);

  return {
    folders,
    isLoading: query.isLoading,
    isSuccess: query.isSuccess,
    isError: query.isError,
    refetch: query.refetch,
  };
}
```

- [ ] **Step 13.4: Run the test, verify it passes**

Run: `cd frontend && npm test -- useWatchedFolders`
Expected: PASS.

- [ ] **Step 13.5: Commit**

```bash
git add frontend/src/hooks/useWatchedFolders.ts frontend/src/hooks/useWatchedFolders.test.tsx
git commit -m "feat(scope): useWatchedFolders hook (filters out unavailable folders)"
```

---

## Task 14: Frontend — `<FolderPicker>` component

**Why:** The interactive multi-select used in the New Conversation modal, Research start form, and Search filter row.

**Files:**
- Create: `frontend/src/components/FolderPicker.tsx`
- Test: `frontend/src/components/FolderPicker.test.tsx`

- [ ] **Step 14.1: Write the failing test**

Create `frontend/src/components/FolderPicker.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FolderPicker } from './FolderPicker';

const folders = [
  { folder_id: 'a', label: 'Contracts', path: '/c', unavailable_reason: null },
  { folder_id: 'b', label: 'Legal', path: '/l', unavailable_reason: null },
];

describe('FolderPicker', () => {
  it('renders "Folders: All" when value is empty', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />);
    expect(screen.getByRole('button')).toHaveTextContent('Folders: All');
  });

  it('renders folder labels when one selected', () => {
    render(<FolderPicker value={['a']} onChange={() => {}} folders={folders} />);
    expect(screen.getByRole('button')).toHaveTextContent('Folders: Contracts');
  });

  it('renders count when multiple selected', () => {
    render(<FolderPicker value={['a', 'b']} onChange={() => {}} folders={folders} />);
    expect(screen.getByRole('button')).toHaveTextContent('Folders: Contracts, Legal (2)');
  });

  it('disables when there are no folders', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={[]} />);
    expect(screen.getByRole('button')).toBeDisabled();
    expect(screen.getByRole('button')).toHaveTextContent('No folders to scope to');
  });

  it('toggling a folder calls onChange', () => {
    const onChange = vi.fn();
    render(<FolderPicker value={[]} onChange={onChange} folders={folders} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByLabelText('Contracts'));
    expect(onChange).toHaveBeenCalledWith(['a']);
  });

  it('"Select all" selects every folder', () => {
    const onChange = vi.fn();
    render(<FolderPicker value={[]} onChange={onChange} folders={folders} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByText('Select all'));
    expect(onChange).toHaveBeenCalledWith(['a', 'b']);
  });

  it('"Clear" resets selection', () => {
    const onChange = vi.fn();
    render(<FolderPicker value={['a', 'b']} onChange={onChange} folders={folders} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.click(screen.getByText('Clear'));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('search input filters the list', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.change(screen.getByPlaceholderText('Search folders…'), { target: { value: 'leg' } });
    expect(screen.queryByText('Contracts')).not.toBeInTheDocument();
    expect(screen.getByText('Legal')).toBeInTheDocument();
  });
});
```

- [ ] **Step 14.2: Run the test, verify it fails**

Run: `cd frontend && npm test -- FolderPicker`
Expected: FAIL — module not found.

- [ ] **Step 14.3: Implement `<FolderPicker>`**

Create `frontend/src/components/FolderPicker.tsx`:

```tsx
import { useState, useMemo } from 'react';
import type { WatchedFolderInfo } from '../hooks/useWatchedFolders';

export interface FolderPickerProps {
  value: string[];
  onChange: (folder_ids: string[]) => void;
  folders: WatchedFolderInfo[];
  disabled?: boolean;
  size?: 'sm' | 'md';
}

function summarize(value: string[], folders: WatchedFolderInfo[]): string {
  if (value.length === 0) return 'Folders: All';
  const labels = value
    .map((id) => folders.find((f) => f.folder_id === id)?.label ?? '?')
    .slice(0, 3);
  const suffix = value.length > 1 ? ` (${value.length})` : '';
  return `Folders: ${labels.join(', ')}${suffix}`;
}

export function FolderPicker({ value, onChange, folders, disabled, size = 'md' }: FolderPickerProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');

  const visible = useMemo(
    () =>
      folders.filter((f) =>
        filter.trim() === '' ? true : f.label.toLowerCase().includes(filter.toLowerCase()),
      ),
    [folders, filter],
  );

  const noFolders = folders.length === 0;
  const buttonText = noFolders ? 'No folders to scope to' : summarize(value, folders);

  const toggle = (id: string) => {
    if (value.includes(id)) onChange(value.filter((v) => v !== id));
    else onChange([...value, id]);
  };

  return (
    <div className={`folder-picker folder-picker--${size}`}>
      <button
        type="button"
        onClick={() => !noFolders && setOpen((o) => !o)}
        disabled={disabled || noFolders}
        className="folder-picker__trigger"
      >
        {buttonText}
      </button>
      {open && !noFolders && (
        <div className="folder-picker__popover">
          <input
            type="text"
            placeholder="Search folders…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="folder-picker__search"
          />
          <div className="folder-picker__actions">
            <button type="button" onClick={() => onChange(folders.map((f) => f.folder_id))}>
              Select all
            </button>
            <button type="button" onClick={() => onChange([])}>
              Clear
            </button>
          </div>
          <ul className="folder-picker__list">
            {visible.map((f) => (
              <li key={f.folder_id}>
                <label>
                  <input
                    type="checkbox"
                    checked={value.includes(f.folder_id)}
                    onChange={() => toggle(f.folder_id)}
                    aria-label={f.label}
                  />
                  {f.label}
                </label>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
```

(Tailwind classes can be added per the existing component style. Test verifies behavior, not styling.)

- [ ] **Step 14.4: Run the test, verify it passes**

Run: `cd frontend && npm test -- FolderPicker`
Expected: PASS — all 8 tests.

- [ ] **Step 14.5: Commit**

```bash
git add frontend/src/components/FolderPicker.tsx frontend/src/components/FolderPicker.test.tsx
git commit -m "feat(scope): FolderPicker component (multi-select + search + select all/clear)"
```

---

## Task 15: Frontend — `<ScopeChip>` component (read-only display)

**Why:** Conversation header and Research detail render a chip showing the active scope but don't allow editing.

**Files:**
- Create: `frontend/src/components/ScopeChip.tsx`
- Test: `frontend/src/components/ScopeChip.test.tsx`

- [ ] **Step 15.1: Write the failing test**

Create `frontend/src/components/ScopeChip.test.tsx`:

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ScopeChip } from './ScopeChip';

const folders = [
  { folder_id: 'a', label: 'Contracts', path: '/c', unavailable_reason: null },
  { folder_id: 'b', label: 'Legal', path: '/l', unavailable_reason: null },
];

describe('ScopeChip', () => {
  it('renders "Folders: All" when scope is empty', () => {
    render(<ScopeChip scope={{}} folders={folders} />);
    expect(screen.getByText(/Folders: All/i)).toBeInTheDocument();
  });

  it('renders folder names when scope has folder_ids', () => {
    render(<ScopeChip scope={{ folder_ids: ['a'] }} folders={folders} />);
    expect(screen.getByText(/Contracts/i)).toBeInTheDocument();
  });

  it('shows count when multiple', () => {
    render(<ScopeChip scope={{ folder_ids: ['a', 'b'] }} folders={folders} />);
    expect(screen.getByText(/\(2\)/)).toBeInTheDocument();
  });

  it('handles unknown folder_ids gracefully', () => {
    render(<ScopeChip scope={{ folder_ids: ['unknown'] }} folders={folders} />);
    expect(screen.getByText(/Folders: \?/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 15.2: Run the test, verify it fails**

Run: `cd frontend && npm test -- ScopeChip`
Expected: FAIL — module not found.

- [ ] **Step 15.3: Implement `<ScopeChip>`**

Create `frontend/src/components/ScopeChip.tsx`:

```tsx
import type { WatchedFolderInfo } from '../hooks/useWatchedFolders';

export interface ScopeChipProps {
  scope: { folder_ids?: string[] } | null | undefined;
  folders: WatchedFolderInfo[];
  className?: string;
}

export function ScopeChip({ scope, folders, className }: ScopeChipProps) {
  const ids = scope?.folder_ids ?? [];
  if (ids.length === 0) {
    return <span className={className ?? 'scope-chip'}>Folders: All</span>;
  }
  const labels = ids
    .map((id) => folders.find((f) => f.folder_id === id)?.label ?? '?')
    .slice(0, 3);
  const suffix = ids.length > 1 ? ` (${ids.length})` : '';
  return <span className={className ?? 'scope-chip'}>{`Folders: ${labels.join(', ')}${suffix}`}</span>;
}
```

- [ ] **Step 15.4: Run the test, verify it passes**

Run: `cd frontend && npm test -- ScopeChip`
Expected: PASS — all 4 tests.

- [ ] **Step 15.5: Commit**

```bash
git add frontend/src/components/ScopeChip.tsx frontend/src/components/ScopeChip.test.tsx
git commit -m "feat(scope): ScopeChip read-only display component"
```

---

## Task 16: Ask — New Conversation modal + read-only header chip

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx` (or wherever the new-conversation modal lives)
- Modify: `frontend/src/contexts/ChatContext.tsx` (extend the create-conversation API call to pass `scope`)
- Modify: `frontend/src/api/client.ts` if the conversation type lives there

- [ ] **Step 16.1: Find the new-conversation creation site**

Run: `grep -n "createConversation\|POST.*conversations\|new.*conversation" frontend/src/contexts/ChatContext.tsx frontend/src/pages/ChatPage.tsx`

- [ ] **Step 16.2: Add a `scope_folder_ids` state to the new-conversation modal**

In the modal component (likely a piece of `ChatPage.tsx` or a child component), add:

```tsx
import { FolderPicker } from '../components/FolderPicker';
import { useWatchedFolders } from '../hooks/useWatchedFolders';

// inside the modal component:
const { folders } = useWatchedFolders();
const [scopeFolderIds, setScopeFolderIds] = useState<string[]>([]);

// in the modal JSX, below the model selector:
<FolderPicker
  value={scopeFolderIds}
  onChange={setScopeFolderIds}
  folders={folders}
/>
```

- [ ] **Step 16.3: Pass the scope to the create call**

When calling `createConversation`, include the scope:

```tsx
await createConversation({
  title,
  model: selectedModel,
  scope: scopeFolderIds.length > 0 ? { folder_ids: scopeFolderIds } : undefined,
});
```

And update the `createConversation` helper in `ChatContext.tsx` (or wherever it lives) to forward the new `scope` field to the POST body.

- [ ] **Step 16.4: Render the read-only chip in the conversation header**

In the chat conversation header (find the component that renders the title — search `grep -n "conv.title\|conversation.title" frontend/src/pages/ChatPage.tsx`):

```tsx
import { ScopeChip } from '../components/ScopeChip';

// next to the title:
<ScopeChip scope={conversation.scope} folders={folders} />
```

Make sure the conversation detail fetch (the call to `GET /api/chat/conversations/{id}`) is preserving the new `scope` field on the type. If the TypeScript Conversation type is defined locally, add `scope?: { folder_ids?: string[] }`.

- [ ] **Step 16.5: Verify by manual run**

Run: `cd frontend && npm run dev` (in one terminal) + `uv run harbor-clerk-api` (in another).

In the browser:
1. Open New Conversation modal → folder picker is visible.
2. Pick one folder, give it a title, submit.
3. Conversation opens — header chip shows "Folders: <name>".
4. Send a message that triggers a search. Verify the answer cites only docs from the chosen folder.

- [ ] **Step 16.6: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx frontend/src/contexts/ChatContext.tsx [other touched files]
git commit -m "feat(scope): Ask supports folder scope at conversation creation"
```

---

## Task 17: Research — start form picker + detail view chip

**Files:**
- Modify: `frontend/src/pages/ResearchPage.tsx`
- Modify: `frontend/src/contexts/ResearchContext.tsx` (the start-research call)

- [ ] **Step 17.1: Add the picker to the start form**

In `ResearchPage.tsx`, find the start-research form (search `grep -n "strategy\|depth\|time_limit" frontend/src/pages/ResearchPage.tsx`). Add state + picker alongside the existing controls:

```tsx
const { folders } = useWatchedFolders();
const [scopeFolderIds, setScopeFolderIds] = useState<string[]>([]);

// In the form:
<FolderPicker value={scopeFolderIds} onChange={setScopeFolderIds} folders={folders} />
```

- [ ] **Step 17.2: Pass scope when starting research**

When calling the start-research function:

```tsx
await startResearch({
  question,
  strategy,
  depth,
  time_limit_minutes: timeLimitMinutes,
  scope: scopeFolderIds.length > 0 ? { folder_ids: scopeFolderIds } : undefined,
});
```

And update the helper in `ResearchContext.tsx` to forward `scope`.

- [ ] **Step 17.3: Show the scope chip in research detail**

In the research detail view (the section that renders the run's metadata like strategy/depth/time_limit), add:

```tsx
<ScopeChip scope={research.scope} folders={folders} />
```

- [ ] **Step 17.4: Verify by manual run**

Open Research tab, fill out the start form with one folder selected, start a run. After it completes, verify the detail view shows the scope chip.

- [ ] **Step 17.5: Commit**

```bash
git add frontend/src/pages/ResearchPage.tsx frontend/src/contexts/ResearchContext.tsx
git commit -m "feat(scope): Research start form + detail view show folder scope"
```

---

## Task 18: Search — filter row picker

**Files:**
- Modify: `frontend/src/pages/SearchPage.tsx`

- [ ] **Step 18.1: Add the picker to the filter row**

Find the search filter UI (existing filters are language, MIME type, date). Add the picker:

```tsx
const { folders } = useWatchedFolders();
const [scopeFolderIds, setScopeFolderIds] = useState<string[]>([]);

// In the filter row:
<FolderPicker value={scopeFolderIds} onChange={setScopeFolderIds} folders={folders} size="sm" />
```

- [ ] **Step 18.2: Pass scope on each search**

Update the search call:

```tsx
const result = await post('/api/search', {
  query,
  k,
  // ... existing filters ...
  scope: scopeFolderIds.length > 0 ? { folder_ids: scopeFolderIds } : undefined,
});
```

- [ ] **Step 18.3: Verify by manual run**

In the Search tab, perform a search with all folders. Note the result count. Pick one folder via the chip, re-run. Result count should drop to the in-scope subset.

- [ ] **Step 18.4: Commit**

```bash
git add frontend/src/pages/SearchPage.tsx
git commit -m "feat(scope): Search filter row supports folder scope (per-session)"
```

---

## Task 19: End-to-end manual verification + cleanup

- [ ] **Step 19.1: Full Python test suite**

Run: `uv run pytest --ignore=tests/integration`
Expected: all tests pass.

- [ ] **Step 19.2: Full frontend test suite**

Run: `cd frontend && npm test`
Expected: all tests pass.

- [ ] **Step 19.3: Ruff lint + format check**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: both clean.

- [ ] **Step 19.4: Frontend lint + type check**

Run: `cd frontend && npm run lint && npm run type-check && npm run format:check`
Expected: all three clean.

- [ ] **Step 19.5: Manual end-to-end happy path**

Start the stack (`uv run harbor-clerk-api` + `cd frontend && npm run dev`):

1. **Ask** — start a new conversation scoped to Folder A. Ask a question that should pull from Folder A. Verify only A's docs are cited.
2. **Ask negative case** — same question but scope = empty. Verify other folders' docs can appear.
3. **Ask multi-folder** — start a conversation scoped to A + B. Verify both are reachable.
4. **Research** — start a run scoped to one folder. After it completes, verify the report only cites docs from that folder.
5. **Search** — search with no scope, note hit count. Apply a folder scope. Verify the count drops.
6. **API key parity** — create an API key scoped to the same folder as the Ask conversation; query MCP via the API key with the same question; verify the result set matches.

- [ ] **Step 19.6: Manual edge case — unavailable folder rejection**

Set one folder to unavailable (on Docker: unmount the bind; on macOS: temporarily remove). Attempt to start a new conversation scoped to it. Verify the API returns 422.

- [ ] **Step 19.7: Final commit (if anything was tweaked during verification)**

```bash
git status -s
# If clean, skip. Otherwise commit any small fixes.
```

- [ ] **Step 19.8: Push the branch**

```bash
git push -u origin feat/folder-scope-local-tools
```

- [ ] **Step 19.9: Dispatch a fresh-eyes review against the branch tip**

Per memory's "FRESH-EYES REVIEW BEFORE MERGING SUBSTANTIVE PRs" standing directive: this PR is multi-component (backend foundation + 3 wiring paths + frontend) — dispatch a `feature-dev:code-reviewer` agent with a minimal unconstrained prompt against the branch tip. Address findings ≥80 confidence before opening the PR.

- [ ] **Step 19.10: Open the PR**

```bash
gh pr create --title "feat: folder-scope filter for Ask/Research/Search" --body-file ...
```

Reference the spec, summarize the design highlights, list per-surface test instructions.

---

## Notes / pitfalls

- **MCP tool bodies don't need per-tool edits.** Task 6 extended `_visible_doc_ids` which is the central gate; the OR-with-`user_scope` is handled there, and all 19 tools that already call it get user-scope support transparently. The chunk-level enforcement (in `kb_read_passages` / `kb_expand_context`) also relies on `_visible_doc_ids` — same story.
- **`apply_key_scope` regression risk.** Task 3 refactors that helper to delegate to `apply_folder_scope`. The existing scoped-API-key tests in `tests/test_scoped_api_keys.py` must remain green throughout. They're explicitly re-run in Task 3 Step 3.5.
- **Server-side default `'{}'::jsonb`.** Tasks 1-2 set the column default in both Alembic and the ORM. SQLAlchemy's `default=dict` covers Python-side construction; `server_default="{}"` covers raw inserts. Both are needed.
- **`extra="ignore"` matters at the ScopeSpec level only.** When the field is added to chat/research/search request schemas, those schemas can keep their default `extra` policy — Pydantic will recurse into `ScopeSpec` with the schema's own `extra="ignore"`.
- **Conversation export.** This plan doesn't add scope to conversation export markdown; it's out of scope per the spec. If export gets reworked later, scope should land in the metadata header.
- **Forward-compat for collections.** Every place that touches scope today uses `folder_ids`. When Collections lands, the same files extend: `ScopeSpec` gains `collection_ids`, `apply_collection_scope` joins via a future `collection_members` table, `_visible_doc_ids` ORs the folder and collection results. No conversion of existing rows needed.

## Self-review checklist for the engineer

After completing all tasks, verify against the spec:

- [ ] Folder filter works on **Ask** at conversation creation. Picker visible in the modal.
- [ ] Folder filter works on **Research** at run start. Picker in the start form, chip in the detail view.
- [ ] Folder filter works on **Search** as a filter chip. Per-session only, no persistence.
- [ ] Empty selection = no restriction (matches `KeyScope` semantics).
- [ ] Unavailable folders rejected with 422.
- [ ] No `PATCH /api/chat/conversations/{id}` — scope is immutable for the container.
- [ ] API key scoping (existing) is unchanged in behavior. No regressions in `tests/test_scoped_api_keys.py`.
- [ ] The ScopeSpec wrapper accepts unknown future keys (`collection_ids`, etc.) without error.
- [ ] DB schema: `conversations.scope` and `research_state.scope` both `JSONB NOT NULL DEFAULT '{}'`.

## Spec coverage verification

Mapping each spec section to tasks:

| Spec section | Task(s) |
|---|---|
| Architecture summary | T3, T4, T6, T7 |
| Ask UX | T8, T9, T16 |
| Research UX | T10, T11, T17 |
| Search UX | T12, T18 |
| Empty-state semantics | T3 (helper), T4 (UserScope.is_unrestricted), T5 (ScopeSpec optional) |
| Data model — conversations.scope | T1, T2, T8 |
| Data model — research_state.scope | T1, T2, T10 |
| Backend — apply_folder_scope | T3 |
| Backend — UserScope on Principal | T4 |
| Backend — thread scope through tool dispatch | T7, T9, T11 |
| Backend — chunk-level enforcement | T6 (via `_visible_doc_ids`) |
| Backend — Search route applies scope | T12 |
| API shape — ScopeSpec wrapper | T5 |
| API shape — chat/research/search requests | T8, T10, T12 |
| API shape — validation (422 on unknown/unavailable folders) | T8 (validator), T10, T12 reuse |
| Frontend — `<FolderPicker>` | T13, T14 |
| Frontend — `<ScopeChip>` (read-only) | T15 |
| Frontend — Ask integration | T16 |
| Frontend — Research integration | T17 |
| Frontend — Search integration | T18 |
| Edge cases | T6 (visibility), T8 (422), T19 (manual) |
| Testing strategy | Per-task TDD steps + T19 end-to-end |

Every spec section has at least one task. No orphans.
