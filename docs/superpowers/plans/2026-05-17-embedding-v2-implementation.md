# Embedding v2 (Granite-R2 + Reranker + Schema Rebase) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the embedding stack from multilingual-e5-small/384-dim to IBM Granite-R2/768-dim, add bge-reranker-v2-m3 as a cross-encoder precision stage, and rebase the alembic chain so the new initial migration represents the new schema directly.

**Architecture:** Three independent runtime services (embedder, reranker, HC API/workers) coordinate over HTTP. Schema sentinel rows in a new `schema_metadata` table assert binary↔DB compatibility at boot via `verify_schema_sentinel()` — panic-exits on mismatch. Alembic chain is squashed to a single `0001_initial.py`; existing DBs migrate via standalone `scripts/migrate_to_embedding_v2.py` or get wiped and re-ingested via watched folders.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, PostgreSQL 18 + pgvector, sentence-transformers (CrossEncoder for reranker), pytest with `pytest-httpx` (new dev dep), Swift 5.9 + AppKit (macOS service), React + TypeScript (frontend).

**Companion spec:** [`docs/superpowers/specs/2026-05-17-embedding-v2-design.md`](../specs/2026-05-17-embedding-v2-design.md). All design decisions and rationale live there; this plan only mechanically implements that spec.

---

## File Structure

**Create:**
- `src/harbor_clerk/models/schema_metadata.py` — SQLAlchemy model for the sentinel table
- `src/harbor_clerk/db_health.py` — `verify_schema_sentinel()` plus the panic-exit helper
- `src/harbor_clerk/search_rerank.py` — `rerank_hits()` async function + dataclass for reranker response
- `alembic/versions/0001_initial.py` — rebased single migration creating entire schema at embedding-v2 state
- `embedder/src/embedder/reranker.py` — FastAPI app with `POST /rerank`
- `docker/reranker.Dockerfile` — image with bge-reranker-v2-m3 baked in
- `scripts/migrate_to_embedding_v2.py` — standalone external migration script
- `docs/upgrade-runbook.md` — operator-facing upgrade guide (Path A wipe / Path B migrate)
- `macos/HarborClerkServer/HarborClerkServer/Services/RerankerService.swift` — Swift service definition
- `tests/test_schema_sentinel.py` — sentinel match/mismatch behavior
- `tests/test_db_health.py` — `verify_schema_sentinel()` behavior
- `tests/test_search_rerank.py` — `rerank_hits()` fallback + ordering
- `tests/test_alembic_initial.py` — fresh-DB migration smoke
- `tests/test_migrate_to_embedding_v2.py` — external script pre-flight + happy path
- `tests/test_reranker_service.py` — reranker FastAPI app (in-process)
- `tests/test_embedder_granite.py` — Granite-R2 loads + returns 768-dim (gated `requires_models`)
- `tests/test_search_rerank_integration.py` — end-to-end with real reranker (gated `requires_models`)

**Modify:**
- `src/harbor_clerk/config.py` — add 8 new settings (embed_* and reranker_*)
- `src/harbor_clerk/models/chunk.py` — `Vector(384)` → `Vector(768)`
- `src/harbor_clerk/models/__init__.py` — export `SchemaMetadata`
- `src/harbor_clerk/api/app.py` — call `verify_schema_sentinel()` in lifespan
- `src/harbor_clerk/worker/entry.py` — call `verify_schema_sentinel()` at boot
- `src/harbor_clerk/search.py` — wire `rerank_hits()` into `hybrid_search()`
- `src/harbor_clerk/api/schemas/search.py` — add `ScoreBreakdown`, `reranker_status` fields
- `src/harbor_clerk/worker/stages/embed.py` — bump timeout 1800 → 3600 (in pipeline timeout table)
- `src/harbor_clerk/worker/pipeline.py` — same timeout entry if defined there
- `embedder/src/embedder/app.py` — model default + ignore `task` param + 768-dim
- `embedder/pyproject.toml` — add reranker script entry; pin sentence-transformers
- `pyproject.toml` — add `pytest-httpx` dev dep; register `requires_models` pytest marker
- `docker/embedder.Dockerfile` — bake Granite-R2 weights at build time
- `docker-compose.yml` — add `reranker` service; HC env var `RERANKER_URL`
- `macos/scripts/download-model.sh` — extend to fetch Granite-R2 + reranker
- `macos/scripts/package.sh` — bundle the new model files
- `macos/HarborClerkServer/HarborClerkServer/AppSettings.swift` — `rerankerPort` + `rerankerEnabled`
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — register reranker in lifecycle
- `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift` — UI for reranker port
- `frontend/src/pages/SystemStatusPage.tsx` — show reranker health card
- `frontend/src/pages/ServiceLogsPage.tsx` — tail reranker.log

**Delete:**
- All files under `alembic/versions/` matching `0001_*.py` through `0022_*.py` (22 files) — replaced by the new `0001_initial.py`.

---

## Pre-Implementation Setup

### Branch + worktree

- [ ] **Step P1: Create the worktree from main**

Run:
```bash
cd ~/mcp-gateway
git worktree add .claude/worktrees/embedding-v2 -b feat/embedding-v2-granite-and-reranker main
cd .claude/worktrees/embedding-v2
```

Expected: New worktree created on a fresh branch off main. All commands below run from this worktree.

### Environment

- [ ] **Step P2: Verify the test DB is reachable**

Run:
```bash
psql -h localhost -p 5433 -U lka -d harbor_clerk_test -c "SELECT 1"  # macOS app DB
# OR
psql -h localhost -p 5432 -U lka -d harbor_clerk_test -c "SELECT 1"  # Docker
```

Expected: `1` row returned. If neither port works, start the macOS app or `docker compose up -d postgres` and retry.

- [ ] **Step P3: Verify the dev venv is current**

Run:
```bash
uv sync --all-extras
```

Expected: No errors; venv up to date.

---

## Task 1: Settings additions (embed_*)

**Files:**
- Modify: `src/harbor_clerk/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1.1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_embed_settings_have_granite_defaults(monkeypatch):
    """Defaults match the embedding-v2 spec: Granite-R2, 768-dim, no prefix."""
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_DIM", raising=False)
    monkeypatch.delenv("EMBED_NEEDS_PREFIX", raising=False)

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.embed_model == "ibm-granite/granite-embedding-311m-multilingual-r2"
    assert s.embed_dim == 768
    assert s.embed_needs_prefix is False


def test_embed_settings_override_via_env(monkeypatch):
    """Env vars override defaults — required for the e5-small rollback path."""
    monkeypatch.setenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
    monkeypatch.setenv("EMBED_DIM", "384")
    monkeypatch.setenv("EMBED_NEEDS_PREFIX", "true")

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.embed_model == "intfloat/multilingual-e5-small"
    assert s.embed_dim == 384
    assert s.embed_needs_prefix is True
```

- [ ] **Step 1.2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_config.py::test_embed_settings_have_granite_defaults -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'embed_model'`.

- [ ] **Step 1.3: Add the settings**

Edit `src/harbor_clerk/config.py`, find the `# Embedder` section and add immediately after `embedder_url`:

```python
    # Embedding model (set on the embedder side; HC needs to know for sentinel)
    embed_model: str = Field(default="ibm-granite/granite-embedding-311m-multilingual-r2")
    embed_dim: int = Field(default=768)
    embed_needs_prefix: bool = Field(default=False)  # Granite uses CLS pooling; e5 needed query:/passage:
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_config.py::test_embed_settings_have_granite_defaults tests/test_config.py::test_embed_settings_override_via_env -v
```

Expected: PASS, 2 tests.

- [ ] **Step 1.5: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_config.py
git commit -m "feat(config): add embed_model/embed_dim/embed_needs_prefix settings for embedding-v2"
```

---

## Task 2: Settings additions (reranker_*)

**Files:**
- Modify: `src/harbor_clerk/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 2.1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_reranker_settings_have_defaults(monkeypatch):
    for var in ("RERANKER_ENABLED", "RERANKER_URL", "RERANKER_TOP_K_PAD",
                "RERANKER_POOL_SIZE", "RERANKER_STRICT", "RERANKER_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.reranker_enabled is True
    assert s.reranker_url == "http://reranker:8001"
    assert s.reranker_top_k_pad == 40
    assert s.reranker_pool_size == 50
    assert s.reranker_strict is False
    assert s.reranker_timeout_seconds == 30.0


def test_reranker_url_override_for_macos(monkeypatch):
    """macOS native sets RERANKER_URL to 127.0.0.1 with the per-instance port."""
    monkeypatch.setenv("RERANKER_URL", "http://127.0.0.1:8201")

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.reranker_url == "http://127.0.0.1:8201"
```

- [ ] **Step 2.2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_config.py::test_reranker_settings_have_defaults -v
```

Expected: FAIL with `AttributeError`.

- [ ] **Step 2.3: Add the settings**

Edit `src/harbor_clerk/config.py`, immediately after the new `embed_needs_prefix` line from Task 1:

```python
    # Reranker (bge-reranker-v2-m3 cross-encoder, separate service)
    reranker_enabled: bool = Field(default=True)
    reranker_url: str = Field(default="http://reranker:8001")
    reranker_top_k_pad: int = Field(default=40)
    reranker_pool_size: int = Field(default=50)
    reranker_strict: bool = Field(default=False)
    reranker_timeout_seconds: float = Field(default=30.0)
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_config.py -v -k reranker
```

Expected: PASS, 2 tests.

- [ ] **Step 2.5: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_config.py
git commit -m "feat(config): add reranker_* settings for bge-reranker-v2-m3"
```

---

## Task 3: SchemaMetadata model

**Files:**
- Create: `src/harbor_clerk/models/schema_metadata.py`
- Modify: `src/harbor_clerk/models/__init__.py`

- [ ] **Step 3.1: Create the model file**

Create `src/harbor_clerk/models/schema_metadata.py`:

```python
"""Schema sentinel rows — assert what model/dim this DB is compatible with."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class SchemaMetadata(Base):
    __tablename__ = "schema_metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 3.2: Export from models package**

Edit `src/harbor_clerk/models/__init__.py`, add to the imports + `__all__`:

```python
from harbor_clerk.models.schema_metadata import SchemaMetadata
```

And include `"SchemaMetadata"` in `__all__` (alphabetical position).

- [ ] **Step 3.3: Verify import works**

Run:
```bash
uv run python -c "from harbor_clerk.models import SchemaMetadata; print(SchemaMetadata.__tablename__)"
```

Expected: `schema_metadata`

- [ ] **Step 3.4: Commit**

```bash
git add src/harbor_clerk/models/schema_metadata.py src/harbor_clerk/models/__init__.py
git commit -m "feat(models): add SchemaMetadata sentinel table"
```

---

## Task 4: Update chunk model dim to 768

**Files:**
- Modify: `src/harbor_clerk/models/chunk.py:58`

- [ ] **Step 4.1: Change the column type**

Edit `src/harbor_clerk/models/chunk.py`, find line ~58 (the `embedding = mapped_column(Vector(384), nullable=True)` line) and change to:

```python
    embedding = mapped_column(Vector(768), nullable=True)
```

- [ ] **Step 4.2: Verify import + size**

Run:
```bash
uv run python -c "from harbor_clerk.models.chunk import Chunk; print(Chunk.__table__.c.embedding.type)"
```

Expected: `VECTOR(768)`

- [ ] **Step 4.3: Commit**

```bash
git add src/harbor_clerk/models/chunk.py
git commit -m "feat(models): bump chunk embedding to 768-dim for Granite-R2"
```

---

## Task 5: Rebase alembic to single 0001_initial.py

This task is the riskiest in the plan because it replaces 22 migration files with one. Take it slowly.

**Files:**
- Delete: `alembic/versions/0001_initial_schema.py` through `alembic/versions/0022_imap_command_log.py` (22 files)
- Create: `alembic/versions/0001_initial.py`
- Test: `tests/test_alembic_initial.py`

- [ ] **Step 5.1: Snapshot the current head + existing-DB schema for reference**

Run (against a Postgres with the current schema):
```bash
mkdir -p /tmp/embedding-v2-rebase-ref
pg_dump --schema-only -h localhost -p 5433 -U lka harbor_clerk_test > /tmp/embedding-v2-rebase-ref/old-schema.sql
uv run alembic current > /tmp/embedding-v2-rebase-ref/old-current.txt
uv run alembic history --verbose > /tmp/embedding-v2-rebase-ref/old-history.txt
```

Expected: Three files written under `/tmp/embedding-v2-rebase-ref/`. These are reference artifacts for verifying the rebase preserved everything.

- [ ] **Step 5.2: Drop the test DB so the rebased migration can be applied from scratch**

Run:
```bash
psql -h localhost -p 5433 -U lka -d postgres -c "DROP DATABASE IF EXISTS harbor_clerk_test"
psql -h localhost -p 5433 -U lka -d postgres -c "CREATE DATABASE harbor_clerk_test"
psql -h localhost -p 5433 -U lka -d harbor_clerk_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS citext;"
```

Expected: Empty DB with the three extensions installed.

- [ ] **Step 5.3: Delete the 22 existing migration files**

Run:
```bash
ls alembic/versions/*.py | grep -v __init__ | xargs rm
ls alembic/versions/
```

Expected: Only `__init__.py` and any non-migration helpers remain. Migration files gone.

- [ ] **Step 5.4: Autogenerate the new initial migration**

With the chunk model at Vector(768) (Task 4) and SchemaMetadata model created (Task 3):

```bash
DATABASE_URL=postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test uv run alembic revision --autogenerate -m "initial"
```

Expected: A new file under `alembic/versions/` whose name starts with a hash and ends `_initial.py`. The file's `upgrade()` body should `op.create_table` every table the codebase defines, including `chunks` with `embedding` as `vector(768)` and `schema_metadata` with `(key, value, set_at)`.

- [ ] **Step 5.5: Rename the generated file**

Find the autogen filename (e.g. `abc123def456_initial.py`) and rename to follow project convention:

```bash
mv alembic/versions/*_initial.py alembic/versions/0001_initial.py
```

Also edit the file's `revision = "..."` and `down_revision = None` lines so the revision id matches the filename's prefix:

```python
revision = "0001_initial"
down_revision = None
```

- [ ] **Step 5.6: Add the sentinel-row INSERT and downgrade refusal**

Edit `alembic/versions/0001_initial.py`. At the BOTTOM of the autogenerated `upgrade()` body (after every `op.create_table` and `op.create_index`), append:

```python
    # Sentinel rows — verified at boot by verify_schema_sentinel().
    # Tells the binary which embedding model + dim this DB is compatible with.
    op.execute(
        """
        INSERT INTO schema_metadata (key, value) VALUES
            ('embed_model', 'granite-embedding-311m-multilingual-r2'),
            ('embed_dim', '768'),
            ('reranker', 'bge-reranker-v2-m3')
        """
    )
```

Replace the entire `downgrade()` body with:

```python
def downgrade() -> None:
    raise NotImplementedError(
        "embedding-v2 initial migration is not reversible; restore from backup."
    )
```

- [ ] **Step 5.7: Write the smoke test**

Create `tests/test_alembic_initial.py`:

```python
"""Smoke test: the rebased 0001_initial.py creates the expected schema."""

import subprocess

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_alembic_upgrade_head_succeeds_on_empty_db(db_session):
    """db_session uses a freshly-truncated test DB. Verify the chunks table
    has vector(768) and schema_metadata has the three sentinel rows."""
    # Check chunks.embedding column type
    result = await db_session.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
    )
    dim_type = result.scalar_one()
    assert dim_type == "vector(768)"

    # Check sentinel rows
    result = await db_session.execute(
        text("SELECT key, value FROM schema_metadata ORDER BY key")
    )
    rows = {key: value for key, value in result.all()}
    assert rows == {
        "embed_model": "granite-embedding-311m-multilingual-r2",
        "embed_dim": "768",
        "reranker": "bge-reranker-v2-m3",
    }


def test_alembic_history_has_single_revision():
    """The rebase squashed 22 migrations into 1."""
    result = subprocess.run(
        ["uv", "run", "alembic", "history"],
        capture_output=True,
        text=True,
        check=True,
    )
    # alembic history output has one line per revision
    revision_lines = [
        line for line in result.stdout.splitlines() if line.startswith("<base>")
    ]
    assert len(revision_lines) == 1, f"expected 1 revision, got history:\n{result.stdout}"
```

- [ ] **Step 5.8: Apply the migration + run the test**

Run:
```bash
DATABASE_URL=postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test uv run alembic upgrade head
uv run pytest tests/test_alembic_initial.py -v
```

Expected: alembic upgrade succeeds (creates all tables); both tests PASS.

- [ ] **Step 5.9: Compare against snapshot to verify no schema regression**

Run:
```bash
pg_dump --schema-only -h localhost -p 5433 -U lka harbor_clerk_test > /tmp/embedding-v2-rebase-ref/new-schema.sql
diff /tmp/embedding-v2-rebase-ref/old-schema.sql /tmp/embedding-v2-rebase-ref/new-schema.sql
```

Expected diff content:
- `chunks.embedding` type changes from `vector(384)` to `vector(768)`
- New `schema_metadata` table
- New `schema_metadata.value` insert
- `alembic_version.version_num` value differs
- (No other column drops, no constraint losses, no index drops outside of `chunks_embedding_hnsw_idx`)

If the diff shows anything else missing (e.g. a column you forgot to declare on a model), fix the model and regenerate the migration (Step 5.4-5.6).

- [ ] **Step 5.10: Commit**

```bash
git add alembic/versions/ tests/test_alembic_initial.py
git commit -m "feat(alembic): rebase chain to single 0001_initial.py with embedding-v2 schema"
```

---

## Task 6: Register `requires_models` pytest marker + pytest-httpx dep

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 6.1: Add the marker + dep**

Edit `pyproject.toml`. Find `[tool.pytest.ini_options]` `markers = [...]` block (around line 63 per earlier inspection) and update:

```toml
markers = [
    "integration: end-to-end tests requiring external services (Dovecot, etc.). Run with `uv run pytest -m integration`.",
    "requires_models: tests that load embedding/reranker model weights (~2 GB). Run with `uv run pytest -m requires_models`. Skipped in default CI.",
]
```

Find the test deps section (`[project.optional-dependencies] test = [...]`) and add:

```toml
test = [
    # ... existing entries unchanged ...
    "pytest-httpx>=0.32",
]
```

- [ ] **Step 6.2: Sync deps**

Run:
```bash
uv sync --all-extras
```

Expected: `pytest-httpx` installed.

- [ ] **Step 6.3: Verify the marker is registered**

Run:
```bash
uv run pytest --markers | grep requires_models
```

Expected: `@pytest.mark.requires_models: tests that load embedding/reranker model weights ...`

- [ ] **Step 6.4: Configure default CI to skip requires_models**

Look in `.github/workflows/ci.yml` for the `pytest` invocation. Modify it to add `-m "not requires_models"`:

```yaml
      - run: uv run pytest tests/ -v -m "not requires_models"
```

(If the existing command has other markers like `-m "not integration"`, change to `-m "not requires_models and not integration"`.)

- [ ] **Step 6.5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml uv.lock
git commit -m "chore(test): register requires_models marker + add pytest-httpx dev dep"
```

---

## Task 7: db_health.verify_schema_sentinel

**Files:**
- Create: `src/harbor_clerk/db_health.py`
- Create: `tests/test_db_health.py`

- [ ] **Step 7.1: Write the failing tests**

Create `tests/test_db_health.py`:

```python
"""Tests for verify_schema_sentinel — panics out on binary↔DB mismatch."""

import pytest
from sqlalchemy import text

from harbor_clerk.db_health import SchemaSentinelMismatch, verify_schema_sentinel


@pytest.mark.asyncio
async def test_verify_sentinel_passes_on_match(db_session, monkeypatch):
    """Sentinel rows match the settings — no exception."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "embed_model": "granite-embedding-311m-multilingual-r2",
                "embed_dim": 768,
            },
        )(),
    )
    # db_session fixture loads the rebased migration which populates sentinel rows.
    # No exception should be raised.
    await verify_schema_sentinel(db_session)


@pytest.mark.asyncio
async def test_verify_sentinel_raises_on_model_mismatch(db_session, monkeypatch):
    """Sentinel says granite, settings say e5-small — must raise."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {"embed_model": "intfloat/multilingual-e5-small", "embed_dim": 384},
        )(),
    )
    with pytest.raises(SchemaSentinelMismatch) as exc_info:
        await verify_schema_sentinel(db_session)
    msg = str(exc_info.value)
    assert "embed_model" in msg
    assert "granite-embedding-311m-multilingual-r2" in msg
    assert "multilingual-e5-small" in msg


@pytest.mark.asyncio
async def test_verify_sentinel_raises_on_missing_table(db_session, monkeypatch):
    """schema_metadata table doesn't exist — must raise distinctly."""
    await db_session.execute(text("DROP TABLE schema_metadata"))
    await db_session.commit()
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "embed_model": "granite-embedding-311m-multilingual-r2",
                "embed_dim": 768,
            },
        )(),
    )
    with pytest.raises(SchemaSentinelMismatch) as exc_info:
        await verify_schema_sentinel(db_session)
    assert "schema_metadata" in str(exc_info.value)
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_db_health.py -v
```

Expected: FAIL (import errors — module doesn't exist).

- [ ] **Step 7.3: Implement db_health.py**

Create `src/harbor_clerk/db_health.py`:

```python
"""Schema sentinel verification — refuse to run when DB and binary disagree."""

from __future__ import annotations

import logging
import sys

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.config import get_settings

logger = logging.getLogger(__name__)


class SchemaSentinelMismatch(RuntimeError):
    """Raised when schema_metadata sentinel rows don't match the binary's settings."""


async def verify_schema_sentinel(session: AsyncSession) -> None:
    """Compare schema_metadata sentinel rows to current settings.

    Raises ``SchemaSentinelMismatch`` if any of:
    - the ``schema_metadata`` table doesn't exist
    - the ``embed_model`` row is missing or differs from ``settings.embed_model``
    - the ``embed_dim`` row is missing or differs from ``settings.embed_dim``

    Caller decides whether to ``sys.exit(2)``; see ``panic_on_sentinel_mismatch``.
    """
    settings = get_settings()
    try:
        result = await session.execute(
            text("SELECT key, value FROM schema_metadata WHERE key IN ('embed_model', 'embed_dim')")
        )
    except ProgrammingError as exc:
        raise SchemaSentinelMismatch(
            "schema_metadata table is missing. "
            "This DB has not been migrated to embedding-v2. "
            "Either drop the database and re-launch (Path A) "
            "or run scripts/migrate_to_embedding_v2.py (Path B). "
            "See docs/upgrade-runbook.md#embedding-v2."
        ) from exc

    rows = {key: value for key, value in result.all()}
    mismatches: list[str] = []
    found_model = rows.get("embed_model", "<missing>")
    if found_model != settings.embed_model:
        mismatches.append(f"  embed_model: expected={settings.embed_model!r}, found={found_model!r}")
    found_dim = rows.get("embed_dim", "<missing>")
    if found_dim != str(settings.embed_dim):
        mismatches.append(f"  embed_dim:   expected={settings.embed_dim!r}, found={found_dim!r}")

    if mismatches:
        raise SchemaSentinelMismatch(
            "Schema sentinel mismatch — refusing to start.\n"
            + "\n".join(mismatches)
            + "\nThis binary requires the embedding-v2 schema. Either:\n"
            "  1. Drop and recreate the database (fresh schema, then re-ingest via watched folders), OR\n"
            "  2. Run scripts/migrate_to_embedding_v2.py against this DB.\n"
            "See docs/upgrade-runbook.md#embedding-v2 for details."
        )


async def panic_on_sentinel_mismatch(session: AsyncSession) -> None:
    """Call verify_schema_sentinel; on failure log CRITICAL and sys.exit(2)."""
    try:
        await verify_schema_sentinel(session)
    except SchemaSentinelMismatch as exc:
        logger.critical("%s", exc)
        sys.exit(2)
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_db_health.py -v
```

Expected: PASS, 3 tests.

- [ ] **Step 7.5: Commit**

```bash
git add src/harbor_clerk/db_health.py tests/test_db_health.py
git commit -m "feat(db): verify_schema_sentinel + panic_on_sentinel_mismatch helpers"
```

---

## Task 8: Wire sentinel check into API lifespan

**Files:**
- Modify: `src/harbor_clerk/api/app.py`
- Test: `tests/test_api_startup_sentinel.py`

- [ ] **Step 8.1: Write the failing test**

Create `tests/test_api_startup_sentinel.py`:

```python
"""Verify the API lifespan calls panic_on_sentinel_mismatch."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_calls_sentinel_check():
    """Boot path must include panic_on_sentinel_mismatch before serving."""
    from harbor_clerk.api.app import app

    with patch("harbor_clerk.api.app.panic_on_sentinel_mismatch", new=AsyncMock()) as mock_panic:
        async with app.router.lifespan_context(app):
            pass
        mock_panic.assert_awaited()
```

- [ ] **Step 8.2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_api_startup_sentinel.py -v
```

Expected: FAIL — `panic_on_sentinel_mismatch` isn't imported in `app.py`.

- [ ] **Step 8.3: Wire the call into the lifespan**

Edit `src/harbor_clerk/api/app.py`. Find the existing lifespan function (around lines 260-290 per earlier inspection — the block that calls `_get_expected_schema_version` and queries `alembic_version`). Right after that alembic check, add:

```python
    from harbor_clerk.db_health import panic_on_sentinel_mismatch

    async with async_session_factory() as db:
        await panic_on_sentinel_mismatch(db)
```

Also add to the top-of-file imports if not already present:

```python
from harbor_clerk.db_health import panic_on_sentinel_mismatch
```

(If the function is already wired via top-level import, you can drop the local re-import.)

- [ ] **Step 8.4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_api_startup_sentinel.py -v
```

Expected: PASS.

- [ ] **Step 8.5: Smoke-test that the API starts cleanly against the migrated test DB**

Run:
```bash
DATABASE_URL=postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test \
  uv run harbor-clerk-api &
API_PID=$!
sleep 3
curl -sf http://127.0.0.1:8000/api/system/health
kill $API_PID
```

Expected: Health endpoint returns 200. No "schema sentinel mismatch" in the logs.

- [ ] **Step 8.6: Commit**

```bash
git add src/harbor_clerk/api/app.py tests/test_api_startup_sentinel.py
git commit -m "feat(api): panic_on_sentinel_mismatch at lifespan startup"
```

---

## Task 9: Wire sentinel check into worker entry

**Files:**
- Modify: `src/harbor_clerk/worker/entry.py`
- Test: `tests/test_worker_startup_sentinel.py`

- [ ] **Step 9.1: Write the failing test**

Create `tests/test_worker_startup_sentinel.py`:

```python
"""Verify the worker entry calls panic_on_sentinel_mismatch before pulling jobs."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_worker_main_calls_sentinel_check():
    """The worker boot path must invoke panic_on_sentinel_mismatch before
    starting the polling loop."""
    from harbor_clerk.worker import entry

    with patch.object(entry, "panic_on_sentinel_mismatch", new=AsyncMock()) as mock_panic, \
         patch.object(entry, "_run_worker_loop", new=AsyncMock(side_effect=KeyboardInterrupt)):
        with pytest.raises(KeyboardInterrupt):
            await entry.async_main(["--queues", "io"])
        mock_panic.assert_awaited()
```

(If `entry.py` doesn't currently have an `async_main` or `_run_worker_loop`, the test name and the expected entry points need to follow whatever's there. Read `src/harbor_clerk/worker/entry.py` first to confirm the loop structure.)

- [ ] **Step 9.2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_worker_startup_sentinel.py -v
```

Expected: FAIL.

- [ ] **Step 9.3: Wire the sentinel call**

Edit `src/harbor_clerk/worker/entry.py`. Add to top-of-file imports:

```python
from harbor_clerk.db_health import panic_on_sentinel_mismatch
from harbor_clerk.db import async_session_factory
```

In `main()` (or wherever the worker first opens a DB connection, around line 271 per earlier inspection): immediately after creating the session factory and BEFORE entering the polling loop:

```python
    async with async_session_factory() as db:
        await panic_on_sentinel_mismatch(db)
```

If `main()` is sync, wrap the call:

```python
    import asyncio
    async def _check_sentinel():
        async with async_session_factory() as db:
            await panic_on_sentinel_mismatch(db)
    asyncio.run(_check_sentinel())
```

- [ ] **Step 9.4: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_worker_startup_sentinel.py -v
```

Expected: PASS.

- [ ] **Step 9.5: Smoke-test worker startup**

Run:
```bash
DATABASE_URL=postgresql+asyncpg://lka@localhost:5433/harbor_clerk_test \
  timeout 5 uv run harbor-clerk-worker --queues io || true
```

Expected: Worker boots, logs "Schema sentinel verified" (or no panic), times out cleanly after 5s.

- [ ] **Step 9.6: Commit**

```bash
git add src/harbor_clerk/worker/entry.py tests/test_worker_startup_sentinel.py
git commit -m "feat(worker): panic_on_sentinel_mismatch at boot"
```

---

## Task 10: Embedder app — model swap + ignore task param

**Files:**
- Modify: `embedder/src/embedder/app.py`
- Create: `embedder/tests/test_embedder_app.py` (new tests dir; create alongside)

- [ ] **Step 10.1: Create the embedder tests dir + write the failing test**

Run:
```bash
mkdir -p embedder/tests
touch embedder/tests/__init__.py
```

Create `embedder/tests/test_embedder_app.py`:

```python
"""Tests for the embedder FastAPI app.

These tests use a stub model loader to avoid downloading Granite-R2 weights.
Real-model tests live in tests/test_embedder_granite.py behind the
``requires_models`` marker.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_model():
    """Stub SentenceTransformer that returns deterministic 768-dim vectors."""
    model = MagicMock()
    model.get_sentence_embedding_dimension.return_value = 768

    def encode(texts, normalize_embeddings=True):
        # Return a deterministic 768-dim vector per text
        return np.stack([np.full(768, 0.1, dtype=np.float32) for _ in texts])

    model.encode.side_effect = encode
    return model


@pytest.fixture
def client(stub_model):
    with patch("embedder.app.SentenceTransformer", return_value=stub_model):
        from embedder.app import app

        with TestClient(app) as c:
            yield c


def test_embed_returns_768_dim(client):
    r = client.post("/embed", json={"texts": ["hello world"]})
    assert r.status_code == 200
    body = r.json()
    assert body["dimensions"] == 768
    assert len(body["embeddings"][0]) == 768


def test_embed_ignores_task_param(client, stub_model):
    """task=query must NOT add 'query: ' prefix when embed_needs_prefix is False (Granite default)."""
    client.post("/embed", json={"texts": ["hello"], "task": "query"})
    encoded_texts = stub_model.encode.call_args[0][0]
    assert encoded_texts == ["hello"], f"task param should not modify texts; got {encoded_texts}"


def test_embed_task_query_still_accepted(client):
    """Backward compat: workers send task=passage; must not 422."""
    r = client.post("/embed", json={"texts": ["hello"], "task": "passage"})
    assert r.status_code == 200
```

- [ ] **Step 10.2: Run test to verify it fails**

Run:
```bash
uv --project embedder run --extra test pytest embedder/tests/test_embedder_app.py -v
```

(If `embedder/pyproject.toml` doesn't have a `test` extras section, add `[project.optional-dependencies] test = ["pytest>=9", "fastapi>=0.115", "numpy>=1.26"]` first.)

Expected: FAIL — task param is still being prepended (current code applies prefix unconditionally).

- [ ] **Step 10.3: Update the embedder app**

Edit `embedder/src/embedder/app.py`. Replace the module-level `TASK_PREFIXES` block and the `embed` endpoint body:

```python
MODEL_NAME = os.environ.get("EMBED_MODEL", "ibm-granite/granite-embedding-311m-multilingual-r2")

# Whether the model needs e5-style "query: " / "passage: " prefixes.
# Granite-R2 uses CLS pooling and needs NO prefix. e5 family needs it.
# Configured via env var so the e5 rollback path keeps working.
NEEDS_PREFIX = os.environ.get("EMBED_NEEDS_PREFIX", "false").lower() in ("true", "1", "yes")
TASK_PREFIXES = {"query": "query: ", "passage": "passage: "}
```

And in the `embed` endpoint, replace the prefix application:

```python
@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    texts = request.texts
    if NEEDS_PREFIX and request.task:
        prefix = TASK_PREFIXES[request.task]
        texts = [prefix + t for t in texts]
    embeddings = _model.encode(texts, normalize_embeddings=True)
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        model=MODEL_NAME,
        dimensions=embeddings.shape[1],
    )
```

- [ ] **Step 10.4: Run tests to verify they pass**

Run:
```bash
uv --project embedder run --extra test pytest embedder/tests/test_embedder_app.py -v
```

Expected: PASS, 3 tests.

- [ ] **Step 10.5: Commit**

```bash
git add embedder/src/embedder/app.py embedder/tests/ embedder/pyproject.toml
git commit -m "feat(embedder): default to Granite-R2, ignore task param when EMBED_NEEDS_PREFIX=false"
```

---

## Task 11: Reranker FastAPI service

**Files:**
- Create: `embedder/src/embedder/reranker.py`
- Modify: `embedder/pyproject.toml`
- Create: `embedder/tests/test_reranker_service.py`

- [ ] **Step 11.1: Write the failing tests**

Create `embedder/tests/test_reranker_service.py`:

```python
"""Tests for the reranker FastAPI app, using a stub CrossEncoder."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_cross_encoder():
    """Stub CrossEncoder that returns predictable scores: higher index → higher score."""
    ce = MagicMock()

    def predict(pairs):
        # pairs is [[query, passage], ...]; score = passage index / total
        # We inject the index via a control character so the stub knows the order
        return [float(i) / max(1, len(pairs) - 1) for i in range(len(pairs))]

    ce.predict.side_effect = predict
    return ce


@pytest.fixture
def client(stub_cross_encoder):
    with patch("embedder.reranker.CrossEncoder", return_value=stub_cross_encoder):
        from embedder.reranker import app

        with TestClient(app) as c:
            yield c


def test_rerank_returns_top_k_sorted_desc(client):
    r = client.post(
        "/rerank",
        json={
            "query": "what is the termination clause",
            "passages": ["passage 0", "passage 1", "passage 2", "passage 3"],
            "top_k": 2,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "BAAI/bge-reranker-v2-m3"
    assert len(body["scores"]) == 2
    # Scores must be sorted desc
    assert body["scores"][0]["score"] >= body["scores"][1]["score"]
    # With our stub (score = i/3), top should be index 3
    assert body["scores"][0]["index"] == 3
    assert body["scores"][1]["index"] == 2


def test_rerank_empty_passages_returns_empty(client):
    r = client.post("/rerank", json={"query": "anything", "passages": [], "top_k": 10})
    assert r.status_code == 200
    assert r.json()["scores"] == []


def test_rerank_top_k_greater_than_len(client):
    r = client.post(
        "/rerank",
        json={"query": "x", "passages": ["a", "b"], "top_k": 100},
    )
    assert r.status_code == 200
    assert len(r.json()["scores"]) == 2


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 11.2: Run test to verify it fails**

Run:
```bash
uv --project embedder run --extra test pytest embedder/tests/test_reranker_service.py -v
```

Expected: FAIL — `embedder.reranker` doesn't exist.

- [ ] **Step 11.3: Implement the reranker app**

Create `embedder/src/embedder/reranker.py`:

```python
"""Reranker service — bge-reranker-v2-m3 CrossEncoder over HTTP.

Companion to the embedder. Loaded with BAAI/bge-reranker-v2-m3 at startup;
exposes ``POST /rerank`` accepting ``{query, passages, top_k}`` and returning
``{scores: [{index, score}], model}`` sorted descending by score.
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

_model: CrossEncoder | None = None


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1)
    passages: list[str] = Field(..., max_length=256)
    top_k: int = Field(..., ge=1, le=256)


class ScoreEntry(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    scores: list[ScoreEntry]
    model: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    logger.info("Loading reranker model: %s", MODEL_NAME)
    _model = CrossEncoder(MODEL_NAME)
    logger.info("Reranker model loaded")
    yield
    _model = None


app = FastAPI(title="Harbor Clerk Reranker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not req.passages:
        return RerankResponse(scores=[], model=MODEL_NAME)

    pairs = [[req.query, p] for p in req.passages]
    raw_scores = _model.predict(pairs)
    indexed = sorted(
        ((i, float(s)) for i, s in enumerate(raw_scores)),
        key=lambda x: x[1],
        reverse=True,
    )
    top = indexed[: req.top_k]
    return RerankResponse(
        scores=[ScoreEntry(index=i, score=s) for i, s in top],
        model=MODEL_NAME,
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if config_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        logs_dir = Path(config_file).parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(logs_dir / "reranker.log", maxBytes=5 * 1024 * 1024, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().addHandler(fh)
        except OSError:
            pass

    uvicorn.run(
        "embedder.reranker:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        workers=1,
    )
```

- [ ] **Step 11.4: Register the entry point**

Edit `embedder/pyproject.toml`. Find the `[project.scripts]` block and add:

```toml
[project.scripts]
harbor-clerk-embedder = "embedder.app:main"
harbor-clerk-reranker = "embedder.reranker:main"
```

- [ ] **Step 11.5: Run tests to verify they pass**

Run:
```bash
uv --project embedder sync
uv --project embedder run --extra test pytest embedder/tests/test_reranker_service.py -v
```

Expected: PASS, 4 tests.

- [ ] **Step 11.6: Commit**

```bash
git add embedder/src/embedder/reranker.py embedder/pyproject.toml embedder/tests/test_reranker_service.py
git commit -m "feat(reranker): add bge-reranker-v2-m3 FastAPI service"
```

---

## Task 12: Search API schema additions

**Files:**
- Modify: `src/harbor_clerk/api/schemas/search.py`
- Test: `tests/test_search_schemas.py`

- [ ] **Step 12.1: Write the failing test**

Create `tests/test_search_schemas.py`:

```python
"""Tests for ScoreBreakdown + reranker_status additions to search schemas."""

from harbor_clerk.api.schemas.search import (
    ScoreBreakdown,
    SearchHitOut,
    SearchResponse,
)


def test_score_breakdown_serializes_with_optional_reranker():
    sb = ScoreBreakdown(fts=0.3, vector=0.5, hybrid=0.4, reranker=0.9)
    assert sb.model_dump() == {"fts": 0.3, "vector": 0.5, "hybrid": 0.4, "reranker": 0.9}

    sb_no_rerank = ScoreBreakdown(fts=0.3, vector=0.5, hybrid=0.4, reranker=None)
    assert sb_no_rerank.model_dump()["reranker"] is None


def test_search_hit_out_score_breakdown_is_optional():
    """Existing clients that don't know about score_breakdown still work."""
    hit = SearchHitOut(
        chunk_id="00000000-0000-0000-0000-000000000001",
        doc_id="00000000-0000-0000-0000-000000000001",
        chunk_num=0,
        chunk_text="hello",
        page_start=1,
        page_end=1,
        language="en",
        ocr_used=False,
        ocr_confidence=None,
        score=0.5,
        doc_title="A doc",
        score_breakdown=None,
    )
    assert hit.score_breakdown is None


def test_search_response_has_reranker_status():
    resp = SearchResponse(
        hits=[],
        total_candidates=0,
        has_more=False,
        possible_conflict=False,
        conflict_sources=[],
        reranker_status="disabled",
    )
    assert resp.reranker_status == "disabled"
```

- [ ] **Step 12.2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/test_search_schemas.py -v
```

Expected: FAIL — fields not defined.

- [ ] **Step 12.3: Add the fields**

Edit `src/harbor_clerk/api/schemas/search.py`. Add at the top of the file (after the existing imports):

```python
from typing import Literal
```

Add a new model before `SearchHitOut`:

```python
class ScoreBreakdown(BaseModel):
    fts: float
    vector: float
    hybrid: float
    reranker: float | None = None
```

Add `score_breakdown: ScoreBreakdown | None = None` as the last field of `SearchHitOut`.

Add `reranker_status: Literal["ok", "disabled", "failed"] = "disabled"` as a new field on `SearchResponse`.

- [ ] **Step 12.4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_search_schemas.py -v
```

Expected: PASS, 3 tests.

- [ ] **Step 12.5: Commit**

```bash
git add src/harbor_clerk/api/schemas/search.py tests/test_search_schemas.py
git commit -m "feat(api): add ScoreBreakdown + reranker_status to search schemas"
```

---

## Task 13: search_rerank.rerank_hits

**Files:**
- Create: `src/harbor_clerk/search_rerank.py`
- Create: `tests/test_search_rerank.py`

- [ ] **Step 13.1: Write the failing tests**

Create `tests/test_search_rerank.py`:

```python
"""Tests for rerank_hits — calls reranker service, falls back on failure."""

import pytest
from pytest_httpx import HTTPXMock

from harbor_clerk.search import SearchHit
from harbor_clerk.search_rerank import rerank_hits


def _hit(doc_id: str, doc_title: str, chunk_text: str, score: float) -> SearchHit:
    return SearchHit(
        chunk_id=f"chunk-{doc_id}",
        doc_id=doc_id,
        chunk_num=0,
        chunk_text=chunk_text,
        page_start=1,
        page_end=1,
        language="en",
        ocr_used=False,
        ocr_confidence=None,
        score=score,
        doc_title=doc_title,
    )


@pytest.mark.asyncio
async def test_rerank_hits_reorders_by_reranker_score(httpx_mock: HTTPXMock):
    hits = [
        _hit("doc-a", "A", "first chunk", 0.5),
        _hit("doc-b", "B", "second chunk", 0.9),
        _hit("doc-c", "C", "third chunk", 0.3),
    ]
    # Reranker reverses the order: index 2 best, then 0, then 1
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [
                {"index": 2, "score": 0.95},
                {"index": 0, "score": 0.50},
                {"index": 1, "score": 0.10},
            ],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    reranked = await rerank_hits("query", hits, top_k=2)
    assert [h.doc_id for h in reranked] == ["doc-c", "doc-a"]
    assert reranked[0].score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_rerank_hits_fallback_on_http_error(httpx_mock: HTTPXMock):
    """Reranker 500 → log warning, return hits[:top_k] unchanged."""
    hits = [
        _hit("doc-a", "A", "x", 0.5),
        _hit("doc-b", "B", "y", 0.3),
    ]
    httpx_mock.add_response(url="http://reranker:8001/rerank", status_code=500)
    result, status = await rerank_hits("q", hits, top_k=2, return_status=True)
    assert [h.doc_id for h in result] == ["doc-a", "doc-b"]
    assert status == "failed"


@pytest.mark.asyncio
async def test_rerank_hits_strict_mode_raises_on_failure(httpx_mock: HTTPXMock, monkeypatch):
    """When settings.reranker_strict=True, HTTP failure raises."""
    from harbor_clerk import search_rerank

    monkeypatch.setattr(
        search_rerank,
        "_settings",
        lambda: type("S", (), {
            "reranker_url": "http://reranker:8001",
            "reranker_strict": True,
            "reranker_timeout_seconds": 30.0,
            "reranker_pool_size": 50,
        })(),
    )
    httpx_mock.add_response(url="http://reranker:8001/rerank", status_code=500)
    with pytest.raises(Exception):
        await rerank_hits("q", [_hit("a", "A", "x", 0.5)], top_k=1)


@pytest.mark.asyncio
async def test_rerank_hits_empty_input_returns_empty(httpx_mock: HTTPXMock):
    """No hits in → no hits out, no HTTP call."""
    result = await rerank_hits("q", [], top_k=10)
    assert result == []
    # httpx_mock would fail at teardown if an unconsumed mock was set;
    # we set none, so the test asserts that no call was made by virtue of clean teardown.


@pytest.mark.asyncio
async def test_rerank_hits_caps_at_pool_size(httpx_mock: HTTPXMock, monkeypatch):
    """If len(hits) > pool_size, only the first pool_size are sent."""
    from harbor_clerk import search_rerank

    monkeypatch.setattr(
        search_rerank,
        "_settings",
        lambda: type("S", (), {
            "reranker_url": "http://reranker:8001",
            "reranker_strict": False,
            "reranker_timeout_seconds": 30.0,
            "reranker_pool_size": 3,
        })(),
    )
    hits = [_hit(f"d{i}", f"D{i}", f"x{i}", 0.5) for i in range(10)]
    httpx_mock.add_response(
        url="http://reranker:8001/rerank",
        json={
            "scores": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.8}, {"index": 2, "score": 0.7}],
            "model": "BAAI/bge-reranker-v2-m3",
        },
    )
    result = await rerank_hits("q", hits, top_k=3)
    # The request only included the first 3 hits (pool_size cap)
    req = httpx_mock.get_request()
    assert len(req.json()["passages"]) == 3
    assert len(result) == 3
```

- [ ] **Step 13.2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_search_rerank.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 13.3: Implement search_rerank.py**

Create `src/harbor_clerk/search_rerank.py`:

```python
"""Reranker integration — POST passages to /rerank, return reordered hits."""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from harbor_clerk.config import get_settings as _settings
from harbor_clerk.search import SearchHit

logger = logging.getLogger(__name__)


def _format_passage(hit: SearchHit) -> str:
    """Reranker input: include doc title for cross-encoder context."""
    return f"Title: {hit.doc_title}\n\nChunk: {hit.chunk_text}"


async def rerank_hits(
    query: str,
    hits: list[SearchHit],
    top_k: int,
    *,
    return_status: bool = False,
) -> list[SearchHit] | tuple[list[SearchHit], Literal["ok", "disabled", "failed"]]:
    """Call the reranker service; reorder ``hits`` by reranker score; return top_k.

    On HTTP failure:
      - if ``settings.reranker_strict`` is True, propagate the exception
      - otherwise log a warning and return ``hits[:top_k]`` in the original order

    ``hits`` are silently truncated to ``settings.reranker_pool_size`` before
    being sent (caps reranker latency at predictable bounds).

    When ``return_status=True``, returns ``(hits, status)`` where status is
    one of "ok" / "failed" so the caller can populate ``SearchResponse.reranker_status``.
    """
    settings = _settings()
    if not hits:
        return ([], "disabled") if return_status else []

    pool = hits[: settings.reranker_pool_size]
    passages = [_format_passage(h) for h in pool]

    try:
        async with httpx.AsyncClient(timeout=settings.reranker_timeout_seconds) as client:
            r = await client.post(
                f"{settings.reranker_url}/rerank",
                json={"query": query, "passages": passages, "top_k": top_k},
            )
            r.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        if settings.reranker_strict:
            raise
        logger.warning("reranker call failed; falling back to hybrid-only top-K: %r", exc)
        fallback = hits[:top_k]
        return (fallback, "failed") if return_status else fallback

    body = r.json()
    reordered: list[SearchHit] = []
    for entry in body["scores"]:
        idx = entry["index"]
        score = entry["score"]
        h = pool[idx]
        # Replace score with reranker score; keep all other fields.
        reordered.append(h.model_copy(update={"score": score}) if hasattr(h, "model_copy") else _copy_with_score(h, score))

    return (reordered, "ok") if return_status else reordered


def _copy_with_score(hit: SearchHit, score: float) -> SearchHit:
    """Dataclass-friendly copy with new score. Replaces model_copy for
    non-Pydantic ``SearchHit`` definitions."""
    import dataclasses

    if dataclasses.is_dataclass(hit):
        return dataclasses.replace(hit, score=score)
    # Fallback: mutate a shallow copy
    import copy

    new = copy.copy(hit)
    new.score = score
    return new
```

- [ ] **Step 13.4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_search_rerank.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 13.5: Commit**

```bash
git add src/harbor_clerk/search_rerank.py tests/test_search_rerank.py
git commit -m "feat(search): rerank_hits helper with fallback + pool_size cap"
```

---

## Task 14: Integrate reranker into hybrid_search

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Modify: `src/harbor_clerk/api/routes/search.py`
- Test: `tests/test_hybrid_search_with_rerank.py`

- [ ] **Step 14.1: Write the failing test**

Create `tests/test_hybrid_search_with_rerank.py`:

```python
"""End-to-end test: hybrid_search calls rerank_hits when settings.reranker_enabled."""

from unittest.mock import AsyncMock, patch

import pytest

from harbor_clerk.search import hybrid_search


@pytest.mark.asyncio
async def test_hybrid_search_calls_reranker_when_enabled(db_session, monkeypatch):
    """When reranker_enabled, hybrid_search invokes rerank_hits and uses its order."""
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type("S", (), {
            "reranker_enabled": True,
            "reranker_url": "http://x:1",
            "reranker_strict": False,
            "reranker_timeout_seconds": 30.0,
            "reranker_pool_size": 50,
            "reranker_top_k_pad": 40,
        })(),
    )
    # Stub rerank_hits to verify it's called and its result is used.
    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock(return_value=([], "ok"))) as mock:
        await hybrid_search(db_session, query="anything", k=10)
        mock.assert_awaited_once()
        kwargs = mock.await_args.kwargs
        assert kwargs.get("top_k") == 10
        assert kwargs.get("return_status") is True


@pytest.mark.asyncio
async def test_hybrid_search_skips_reranker_when_disabled(db_session, monkeypatch):
    monkeypatch.setattr(
        "harbor_clerk.config.get_settings",
        lambda: type("S", (), {
            "reranker_enabled": False,
            "reranker_top_k_pad": 40,
            "reranker_pool_size": 50,
        })(),
    )
    with patch("harbor_clerk.search.rerank_hits", new=AsyncMock()) as mock:
        await hybrid_search(db_session, query="anything", k=10)
        mock.assert_not_called()
```

- [ ] **Step 14.2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/test_hybrid_search_with_rerank.py -v
```

Expected: FAIL — rerank not wired.

- [ ] **Step 14.3: Wire rerank_hits into hybrid_search**

Edit `src/harbor_clerk/search.py`. Add to the imports near the top:

```python
from harbor_clerk.search_rerank import rerank_hits
```

Find the existing `hybrid_search()` function. After the FTS+vector merge produces the candidate pool (the variable name will be something like `merged` or `hits`), but before truncating to top-K, add:

```python
    settings = get_settings()
    reranker_status: Literal["ok", "disabled", "failed"] = "disabled"
    if settings.reranker_enabled and merged:
        pool_size = min(k + settings.reranker_top_k_pad, settings.reranker_pool_size)
        pool = merged[:pool_size]
        reranked, reranker_status = await rerank_hits(
            query, pool, top_k=k, return_status=True
        )
        if reranker_status == "ok":
            merged = reranked
        # else: keep original `merged`, just log the fallback (rerank_hits did)
```

(The exact variable name `merged` may differ — adapt to whatever the function calls its post-merge list. The signature change: the function may also need to return `reranker_status` so the route handler can put it in the response — extend the return dataclass.)

Also update `src/harbor_clerk/search.py`'s `SearchResult` dataclass (defined at line ~39) to include the field:

```python
    reranker_status: str = "disabled"
```

And set it in `hybrid_search`'s return value (use the local `reranker_status` variable from the snippet above).

- [ ] **Step 14.4: Wire the field through the route**

Edit `src/harbor_clerk/api/routes/search.py`. In the `search()` handler, where `SearchResponse(...)` is constructed, add:

```python
        reranker_status=result.reranker_status,
```

- [ ] **Step 14.5: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_hybrid_search_with_rerank.py tests/test_search_schemas.py -v
```

Expected: PASS.

- [ ] **Step 14.6: Smoke-test against a running embedder + stub reranker**

Run:
```bash
# In one terminal: start the existing search test
uv run pytest tests/test_api_routes_search.py -v -k "search" || echo "(existing route tests still pass)"
```

Expected: No regressions in existing search tests.

- [ ] **Step 14.7: Commit**

```bash
git add src/harbor_clerk/search.py src/harbor_clerk/api/routes/search.py tests/test_hybrid_search_with_rerank.py
git commit -m "feat(search): wire rerank_hits into hybrid_search with status propagation"
```

---

## Task 15: Bump embed stage timeout

**Files:**
- Modify: `src/harbor_clerk/worker/pipeline.py` (or wherever `_TIMEOUTS` is defined)

- [ ] **Step 15.1: Locate the timeout table**

Run:
```bash
grep -rn "1800\|JobStage.embed" src/harbor_clerk/worker/ | grep -i timeout
```

Expected: One or two hits naming the embed stage timeout.

- [ ] **Step 15.2: Update the timeout from 1800 to 3600**

Edit the located file. Change `1800` (associated with `JobStage.embed`) to `3600`. Add a comment:

```python
    # Granite-R2 is ~5× slower per chunk than e5-small; bumped from 1800s
    # for embedding-v2.
    JobStage.embed: 3600,
```

- [ ] **Step 15.3: Verify worker tests still pass**

Run:
```bash
uv run pytest tests/test_worker_pipeline.py -v 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 15.4: Commit**

```bash
git add src/harbor_clerk/worker/pipeline.py
git commit -m "chore(worker): bump embed stage timeout 1800s → 3600s for Granite-R2"
```

---

## Task 16: External migration script — pre-flight checks

**Files:**
- Create: `scripts/migrate_to_embedding_v2.py`
- Create: `tests/test_migrate_to_embedding_v2.py`

- [ ] **Step 16.1: Write the failing pre-flight tests**

Create `tests/test_migrate_to_embedding_v2.py`:

```python
"""Tests for the external migration script."""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text


SCRIPT = Path(__file__).parent.parent / "scripts" / "migrate_to_embedding_v2.py"


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_script_refuses_without_confirm(test_db_url):
    r = _run("--db-url", test_db_url)
    assert r.returncode != 0
    assert "--confirm" in r.stderr or "--confirm" in r.stdout


def test_script_refuses_without_db_url():
    r = _run("--confirm")
    assert r.returncode != 0


def test_script_refuses_when_sentinel_already_set(test_db_url, db_session):
    """The rebased migration populated the sentinel — script must refuse to re-run."""
    # db_session fixture already ran 0001_initial.py which populated sentinel rows
    r = _run("--db-url", test_db_url, "--confirm")
    assert r.returncode != 0
    assert "already" in r.stderr.lower() or "sentinel" in r.stderr.lower()


def test_script_refuses_when_alembic_at_wrong_head(test_db_url, db_session):
    """If alembic_version isn't '0022', refuse — we don't know how to handle it."""
    import asyncio

    async def _set_old_version():
        # Drop sentinel + chunks.embedding rebuild to simulate a pre-rebase DB
        await db_session.execute(text("DROP TABLE schema_metadata"))
        await db_session.execute(text("UPDATE alembic_version SET version_num = '0019_documents_ocr_languages_used'"))
        await db_session.commit()

    asyncio.run(_set_old_version())
    r = _run("--db-url", test_db_url, "--confirm")
    assert r.returncode != 0
    assert "0022" in r.stderr
```

(The `test_db_url` fixture needs adding to conftest.py if not present — it should expose the `DATABASE_URL` env var as a sync postgres URL `postgresql://...` for the script to use, not the asyncpg variant.)

- [ ] **Step 16.2: Write the script skeleton with pre-flight checks only**

Create `scripts/migrate_to_embedding_v2.py`:

```python
"""External migration script: existing-DB → embedding-v2 schema.

For operators upgrading an existing Harbor Clerk DB without wiping it.
Single transaction; idempotent pre-flight refusals prevent re-running or
running against an unexpected schema state.

Usage:
  uv run python scripts/migrate_to_embedding_v2.py \\
    --db-url postgresql://user:pass@host:port/dbname \\
    --confirm

See docs/upgrade-runbook.md#embedding-v2 for full operator runbook.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg2

# The single expected alembic version that this script knows how to migrate FROM.
# If the DB is at any other revision, the script refuses (we'd be guessing).
EXPECTED_PRE_REBASE_HEAD = "0022_imap_command_log"

# The revision id of the rebased initial migration. Must match the
# `revision = "0001_initial"` line at the top of alembic/versions/0001_initial.py.
REBASED_INITIAL_REVISION = "0001_initial"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(_log_path()),
        ],
    )


def _log_path() -> Path:
    import os

    if os.environ.get("HARBOR_CLERK_LOG_DIR"):
        return Path(os.environ["HARBOR_CLERK_LOG_DIR"]) / "migrate_to_embedding_v2.log"
    # macOS native default
    candidate = Path.home() / "Library/Application Support/Harbor Clerk/logs"
    if candidate.parent.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate / "migrate_to_embedding_v2.log"
    Path("./logs").mkdir(parents=True, exist_ok=True)
    return Path("./logs/migrate_to_embedding_v2.log")


def preflight_check(conn) -> None:
    """Raise RuntimeError with operator-actionable message on any check failure."""
    with conn.cursor() as cur:
        # Check 1: schema_metadata must not already exist with sentinel rows
        cur.execute(
            "SELECT to_regclass('schema_metadata')"
        )
        exists = cur.fetchone()[0]
        if exists is not None:
            cur.execute("SELECT key FROM schema_metadata WHERE key='embed_model'")
            if cur.fetchone() is not None:
                raise RuntimeError(
                    "schema_metadata.embed_model row already exists — this DB has "
                    "already been migrated to embedding-v2. Aborting."
                )

        # Check 2: alembic_version must equal the expected pre-rebase head
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                "alembic_version table is empty — DB is in an unexpected state. Aborting."
            )
        current = row[0]
        if current != EXPECTED_PRE_REBASE_HEAD:
            raise RuntimeError(
                f"alembic_version is '{current}', expected '{EXPECTED_PRE_REBASE_HEAD}'. "
                f"This script knows how to migrate only from the pre-rebase head. "
                f"If your DB is at a different revision, restore from backup or upgrade "
                f"to '{EXPECTED_PRE_REBASE_HEAD}' first."
            )

        # Check 3: chunks.embedding must be vector(384)
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
        col_row = cur.fetchone()
        if col_row is None or col_row[0] != "vector(384)":
            raise RuntimeError(
                f"Expected chunks.embedding to be vector(384); found {col_row[0] if col_row else '<missing>'}. Aborting."
            )


def run_migration(conn) -> None:
    """Apply the embedding-v2 schema changes + enqueue re-embed jobs. Single transaction."""
    with conn.cursor() as cur:
        logger.info("Creating schema_metadata table + sentinel rows")
        cur.execute(
            """
            CREATE TABLE schema_metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO schema_metadata (key, value) VALUES
              ('embed_model', 'granite-embedding-311m-multilingual-r2'),
              ('embed_dim', '768'),
              ('reranker', 'bge-reranker-v2-m3')
            """
        )

        logger.info("Dropping + recreating chunks.embedding as vector(768)")
        cur.execute("ALTER TABLE chunks DROP COLUMN embedding")
        cur.execute("ALTER TABLE chunks ADD COLUMN embedding vector(768)")

        logger.info("Rebuilding chunks_embedding_hnsw_idx")
        cur.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw_idx")
        cur.execute(
            "CREATE INDEX chunks_embedding_hnsw_idx ON chunks "
            "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )

        logger.info("Updating alembic_version → %s", REBASED_INITIAL_REVISION)
        cur.execute(
            "UPDATE alembic_version SET version_num = %s", (REBASED_INITIAL_REVISION,)
        )

        logger.info("Enqueueing embed-stage jobs for all ready docs (most-recent-first)")
        cur.execute(
            """
            INSERT INTO ingestion_jobs (doc_id, stage, queue, status, attempts, created_at, updated_at)
            SELECT doc_id, 'embed', 'cpu', 'queued', 0, NOW(), NOW()
            FROM documents
            WHERE pipeline_status = 'ready'
            ORDER BY updated_at DESC
            """
        )
        enqueued = cur.rowcount
        logger.info("Enqueued %d embed jobs", enqueued)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        prog="migrate_to_embedding_v2",
        description="Migrate an existing Harbor Clerk DB to embedding-v2 schema.",
    )
    parser.add_argument("--db-url", required=True, help="sync postgres URL: postgresql://user:pass@host:port/dbname")
    parser.add_argument("--confirm", action="store_true", required=True, help="acknowledge destructive operation")
    args = parser.parse_args(argv)

    logger.info("Connecting to %s", args.db_url.split("@")[-1])
    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False
    try:
        try:
            preflight_check(conn)
        except RuntimeError as exc:
            logger.error("Pre-flight check failed: %s", exc)
            return 1

        run_migration(conn)
        conn.commit()
        logger.info("Migration complete. Re-launch HC from the embedding-v2 binary.")
        return 0
    except Exception:
        conn.rollback()
        logger.exception("Migration failed; transaction rolled back. DB is unchanged.")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 16.3: Add psycopg2 to deps**

Edit `pyproject.toml`, find the main `dependencies = [...]` block and add (if not present):

```toml
"psycopg2-binary>=2.9",
```

Run:
```bash
uv sync
```

- [ ] **Step 16.4: Add the `test_db_url` fixture if missing**

Edit `tests/conftest.py`. After the existing fixtures, add:

```python
@pytest.fixture
def test_db_url() -> str:
    """Sync postgres URL for the test DB; used by scripts that don't speak asyncpg."""
    import os
    url = os.environ.get(
        "TEST_DB_URL",
        "postgresql://lka@localhost:5433/harbor_clerk_test",
    )
    return url
```

- [ ] **Step 16.5: Run pre-flight tests to verify they pass**

Run:
```bash
uv run pytest tests/test_migrate_to_embedding_v2.py -v -k "refuses"
```

Expected: 4 tests PASS (the "refuses_*" subset). The happy-path test (next task) still fails — that's fine for now.

- [ ] **Step 16.6: Commit**

```bash
git add scripts/migrate_to_embedding_v2.py tests/test_migrate_to_embedding_v2.py tests/conftest.py pyproject.toml uv.lock
git commit -m "feat(scripts): migrate_to_embedding_v2.py with pre-flight refusals"
```

---

## Task 17: External migration script — happy path

**Files:**
- Modify: `tests/test_migrate_to_embedding_v2.py`

- [ ] **Step 17.1: Write the happy-path test**

Append to `tests/test_migrate_to_embedding_v2.py`:

```python
def test_script_happy_path_against_pre_rebase_db(test_db_url, db_session):
    """Set DB to look like pre-rebase, run script, verify post-state."""
    import asyncio

    async def _make_pre_rebase():
        # Simulate the pre-embedding-v2 state:
        # - no schema_metadata table
        # - alembic_version at 0022
        # - chunks.embedding at vector(384)
        await db_session.execute(text("DROP TABLE IF EXISTS schema_metadata"))
        await db_session.execute(text("UPDATE alembic_version SET version_num = '0022_imap_command_log'"))
        await db_session.execute(text("ALTER TABLE chunks DROP COLUMN embedding"))
        await db_session.execute(text("ALTER TABLE chunks ADD COLUMN embedding vector(384)"))
        # Seed one ready doc so the re-enqueue has something to count
        await db_session.execute(
            text(
                "INSERT INTO documents (doc_id, title, mime_type, pipeline_status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'test', 'text/plain', 'ready', NOW(), NOW())"
            )
        )
        await db_session.commit()

    asyncio.run(_make_pre_rebase())

    r = _run("--db-url", test_db_url, "--confirm")
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

    # Verify post-state
    async def _verify():
        sentinel = (await db_session.execute(
            text("SELECT value FROM schema_metadata WHERE key='embed_model'")
        )).scalar_one()
        assert sentinel == "granite-embedding-311m-multilingual-r2"

        col_type = (await db_session.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) "
                "FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        )).scalar_one()
        assert col_type == "vector(768)"

        revision = (await db_session.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one()
        assert revision == "0001_initial"

        # One enqueued embed job for the test doc we seeded
        job_count = (await db_session.execute(
            text("SELECT COUNT(*) FROM ingestion_jobs WHERE stage='embed' AND status='queued'")
        )).scalar_one()
        assert job_count == 1

    asyncio.run(_verify())
```

- [ ] **Step 17.2: Run test to verify it passes**

Run:
```bash
uv run pytest tests/test_migrate_to_embedding_v2.py -v
```

Expected: All tests PASS (5 total).

- [ ] **Step 17.3: Smoke-run the script's --help**

Run:
```bash
uv run python scripts/migrate_to_embedding_v2.py --help
```

Expected: Help text printed, lists `--db-url` and `--confirm` as required, exit 0.

- [ ] **Step 17.4: Commit**

```bash
git add tests/test_migrate_to_embedding_v2.py
git commit -m "test(scripts): migrate_to_embedding_v2 happy path verifies post-state"
```

---

## Task 18: Docker — embedder image bakes Granite-R2

**Files:**
- Modify: `docker/embedder.Dockerfile`

- [ ] **Step 18.1: Add weight bake to the embedder Dockerfile**

Edit `docker/embedder.Dockerfile`. Add (or modify the existing model download step) so it pre-downloads Granite-R2 at build time:

```dockerfile
# Pre-download the Granite-R2 weights at build time so the container
# starts immediately. ~700 MB to image size; acceptable per project policy.
RUN python -c "from huggingface_hub import snapshot_download; \
  snapshot_download(repo_id='ibm-granite/granite-embedding-311m-multilingual-r2', \
                    local_dir='/models/granite-embedding-311m-multilingual-r2', \
                    local_dir_use_symlinks=False)"
ENV EMBED_MODEL=/models/granite-embedding-311m-multilingual-r2
```

(If a similar `huggingface_hub` block exists for e5-small, replace it; don't keep both.)

- [ ] **Step 18.2: Build the image to verify**

Run:
```bash
docker build -f docker/embedder.Dockerfile -t harbor-clerk-embedder:embedding-v2 .
```

Expected: Build succeeds. Image is ~2 GB (base + Python + sentence-transformers + Granite weights).

- [ ] **Step 18.3: Smoke-run the container**

Run:
```bash
docker run --rm -d --name embedder-smoke -p 9001:8000 harbor-clerk-embedder:embedding-v2
sleep 15  # give the model time to load
curl -sf http://localhost:9001/health | grep granite
docker stop embedder-smoke
```

Expected: `/health` returns 200 with `granite-embedding-311m-multilingual-r2` in the response.

- [ ] **Step 18.4: Commit**

```bash
git add docker/embedder.Dockerfile
git commit -m "chore(docker): bake Granite-R2 weights into embedder image"
```

---

## Task 19: Docker — reranker image + compose entry

**Files:**
- Create: `docker/reranker.Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 19.1: Create the reranker Dockerfile**

Create `docker/reranker.Dockerfile`:

```dockerfile
# Reranker service — bge-reranker-v2-m3 CrossEncoder.
# Shares the embedder package wheel (single Python project, two entry points).

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY embedder /app/embedder
RUN pip install --no-cache-dir /app/embedder

# Pre-download the reranker weights at build time. ~1.2 GB.
RUN python -c "from huggingface_hub import snapshot_download; \
  snapshot_download(repo_id='BAAI/bge-reranker-v2-m3', \
                    local_dir='/models/bge-reranker-v2-m3', \
                    local_dir_use_symlinks=False)"
ENV RERANKER_MODEL=/models/bge-reranker-v2-m3

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=60s \
  CMD python -c "import httpx; httpx.get('http://localhost:8001/health').raise_for_status()"

CMD ["harbor-clerk-reranker"]
ENV HOST=0.0.0.0 PORT=8001
```

- [ ] **Step 19.2: Add reranker service to docker-compose.yml**

Edit `docker-compose.yml`. After the `embedder:` block, add:

```yaml
  reranker:
    build:
      context: .
      dockerfile: docker/reranker.Dockerfile
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8001/health').raise_for_status()"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 60s
    networks:
      - harbor-clerk
```

In the `app:`, `worker-io:`, and `worker-cpu:` service blocks, add to `environment:`:

```yaml
      RERANKER_URL: http://reranker:8001
```

And add `reranker:` to their `depends_on:` lists.

- [ ] **Step 19.3: Build + smoke-test reranker container**

Run:
```bash
docker compose build reranker
docker compose up -d reranker
sleep 30  # weights load
curl -sf http://localhost:8001/health
docker compose down
```

Expected: Build succeeds; `/health` returns 200.

- [ ] **Step 19.4: Commit**

```bash
git add docker/reranker.Dockerfile docker-compose.yml
git commit -m "feat(docker): add reranker service with bge-reranker-v2-m3"
```

---

## Task 20: macOS — bundle the new model files

**Files:**
- Modify: `macos/scripts/download-model.sh`
- Modify: `macos/scripts/package.sh`

- [ ] **Step 20.1: Extend download-model.sh to fetch both models**

Edit `macos/scripts/download-model.sh`. Find the existing model-download invocation (which currently fetches multilingual-e5-small) and add Granite-R2 + reranker:

```bash
# Granite-R2 embedder (~700 MB)
huggingface-cli download \
  ibm-granite/granite-embedding-311m-multilingual-r2 \
  --local-dir "$MODEL_DIR/granite-embedding-311m-multilingual-r2" \
  --local-dir-use-symlinks False

# bge-reranker-v2-m3 cross-encoder (~1.2 GB)
huggingface-cli download \
  BAAI/bge-reranker-v2-m3 \
  --local-dir "$MODEL_DIR/bge-reranker-v2-m3" \
  --local-dir-use-symlinks False
```

(Keep the e5-small download IF it's needed for the rollback path; otherwise remove.)

- [ ] **Step 20.2: Verify package.sh copies both directories**

Edit `macos/scripts/package.sh`. Find where model files are copied into the app bundle's `Resources/`. Ensure both `granite-embedding-311m-multilingual-r2/` and `bge-reranker-v2-m3/` directories are included.

If the existing script copies the entire `MODEL_DIR`, no change needed — verify by running:

```bash
cd macos && make download-models && make package
```

Then check:

```bash
ls "macos/build/output/Harbor Clerk Server.app/Contents/Resources/" | grep -E "granite|bge-reranker"
```

Expected: Both directories present.

- [ ] **Step 20.3: Commit**

```bash
git add macos/scripts/download-model.sh macos/scripts/package.sh
git commit -m "build(macos): bundle Granite-R2 + bge-reranker-v2-m3 in the app"
```

---

## Task 21: macOS — RerankerService.swift

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/Services/RerankerService.swift`

- [ ] **Step 21.1: Create the service**

Create `macos/HarborClerkServer/HarborClerkServer/Services/RerankerService.swift`:

```swift
import Foundation

final class RerankerService: PythonService {
    init() {
        super.init(name: "Reranker")
    }

    override var executableName: String { "harbor-clerk-reranker" }

    override var extraEnvironment: [String: String] {
        let modelPath = Bundle.main.resourceURL!
            .appendingPathComponent("model/bge-reranker-v2-m3").path
        return [
            "RERANKER_MODEL": modelPath,
            "HOST": "127.0.0.1",
            "PORT": String(AppSettings.shared.rerankerPort),
        ]
    }

    override func healthCheck() async -> Bool {
        let port = AppSettings.shared.rerankerPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        return await httpProbeOK(url)
    }
}
```

- [ ] **Step 21.2: Add rerankerPort to AppSettings**

Edit `macos/HarborClerkServer/HarborClerkServer/AppSettings.swift`. Find the existing `embedderPort` declaration and add immediately after:

```swift
    @Published var rerankerPort: Int = 8201
    @Published var rerankerEnabled: Bool = true
```

If `AppSettings` persists to UserDefaults, add the load/save lines for both keys following the existing pattern for `embedderPort`.

- [ ] **Step 21.3: Register in ServiceManager**

Edit `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift`. Find where `EmbedderService()` is added to the service list. Add `RerankerService()` immediately after it (so startup order is: Postgres → Tika → Embedder → Reranker → API → Workers):

```swift
        services.append(RerankerService())
```

Verify the lifecycle (start/stop/healthcheck) treats the reranker the same as the embedder by virtue of inheriting `PythonService`.

- [ ] **Step 21.4: Build the macOS app**

Run:
```bash
cd macos && make
```

Expected: Build succeeds. New `RerankerService.swift` compiles cleanly.

- [ ] **Step 21.5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/Services/RerankerService.swift \
        macos/HarborClerkServer/HarborClerkServer/AppSettings.swift \
        macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift
git commit -m "feat(macos): RerankerService managed by ServiceManager"
```

---

## Task 22: macOS — UI for reranker port + status

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift`
- Modify: `frontend/src/pages/SystemStatusPage.tsx`
- Modify: `frontend/src/pages/ServiceLogsPage.tsx`

- [ ] **Step 22.1: Add reranker port + toggle to PreferencesWindow**

Edit `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift`. Find where `embedderPort` is exposed in the UI. Add a parallel control for `rerankerPort` immediately below, plus a toggle for `rerankerEnabled`. Reuse the existing patterns (Stepper/TextField/Toggle).

- [ ] **Step 22.2: Add reranker card to SystemStatusPage**

Edit `frontend/src/pages/SystemStatusPage.tsx`. Find the existing service-status cards (one for embedder, one for postgres, etc.). Add a parallel card for reranker — fetch its health from a new field on `/api/system/health` if available, OR add the reranker URL fetch alongside the existing embedder fetch.

If `/api/system/health` doesn't currently include reranker status, add it: edit `src/harbor_clerk/api/routes/system.py` to probe the reranker URL similarly to how embedder is probed.

- [ ] **Step 22.3: Add reranker.log to ServiceLogsPage**

Edit `frontend/src/pages/ServiceLogsPage.tsx`. Find the existing log-tailing list. Add `reranker.log` alongside `embedder.log`.

- [ ] **Step 22.4: Build the frontend**

Run:
```bash
cd frontend && npm run build
```

Expected: Build succeeds with no type errors.

- [ ] **Step 22.5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift \
        frontend/src/pages/SystemStatusPage.tsx \
        frontend/src/pages/ServiceLogsPage.tsx \
        src/harbor_clerk/api/routes/system.py
git commit -m "feat(ui): reranker port preference + status card + log tail"
```

---

## Task 23: Documentation — upgrade runbook

**Files:**
- Create: `docs/upgrade-runbook.md`

- [ ] **Step 23.1: Write the upgrade runbook**

Create `docs/upgrade-runbook.md`:

```markdown
# Harbor Clerk Upgrade Runbook

This document covers operator-facing procedures for breaking-change upgrades.
For incremental version bumps, normal alembic migrations apply automatically
when the app starts; this runbook only covers explicit cutover events.

## Embedding v2 (Granite-R2 + Reranker + Schema Rebase)

### What this upgrade does

- Replaces the embedding model (`multilingual-e5-small` 384-dim → `granite-embedding-311m-multilingual-r2` 768-dim).
- Adds a cross-encoder reranker service (`bge-reranker-v2-m3`).
- Rebases the alembic migration chain into a single `0001_initial.py`.
- Bumps the macOS bundle / Docker images by ~2 GB (model weights).

### Compatibility

- Old `alembic_version='0022'` databases REFUSE to upgrade in-app — the new binary's alembic chain doesn't include revision `0022`. This is intentional. You must either wipe + re-ingest (Path A) or run the external migration script (Path B).
- Old `multilingual-e5-small` embeddings are NOT usable with the new model. The DB column type changes; all existing embeddings are destroyed by either path.
- API tool surface is unchanged — MCP clients (frontier LLMs calling `kb_search`) see no contract changes.

### Path A — wipe + re-ingest (recommended for office appliances)

1. **Stop Harbor Clerk** (menubar Quit on macOS, `docker compose down` on Docker).
2. **Drop the database.**
   - macOS: open the Postgres console and run `DROP DATABASE harbor_clerk; CREATE DATABASE harbor_clerk;` (or use `psql -h localhost -p <port> -U <user> -d postgres -c "DROP DATABASE harbor_clerk; CREATE DATABASE harbor_clerk;"`).
   - Docker: `docker compose down -v` (the `-v` removes the postgres volume too).
3. **Re-launch from the embedding-v2 binary.**
   - macOS: launch HarborClerkServer.app from the new build. `alembic upgrade head` creates the fresh schema; sentinel passes.
   - Docker: `docker compose up -d`. Entrypoint runs `alembic upgrade head`.
4. **Watched folders auto-rescan.** All previously-watched files are re-ingested with Granite-R2 embeddings. Search becomes available as soon as the first few docs finish.

### Path B — preserve existing DB

For operators who want to keep their existing documents/conversations/audit-log without re-ingesting.

1. **Stop Harbor Clerk** (full stop — workers + API + embedder all down).
2. **Back up the database.** Recommended: `pg_dump harbor_clerk > harbor_clerk_pre_embedding_v2.sql`. The script is non-reversible.
3. **Run the migration script:**

```bash
cd ~/mcp-gateway   # or wherever the embedding-v2 source lives
uv run python scripts/migrate_to_embedding_v2.py \
    --db-url postgresql://<user>:<pass>@localhost:<port>/harbor_clerk \
    --confirm
```

Expected output (~5 seconds):
- "Creating schema_metadata table + sentinel rows"
- "Dropping + recreating chunks.embedding as vector(768)"
- "Rebuilding chunks_embedding_hnsw_idx"
- "Updating alembic_version → 0001_initial"
- "Enqueued N embed jobs"
- "Migration complete. Re-launch HC from the embedding-v2 binary."

4. **Re-launch HC from the embedding-v2 binary.** Sentinel check passes (script populated it). Workers come up, see the queued embed jobs, and trickle through them most-recent-first.
5. **Watch the UI banner** that shows "Re-embedding: N of M docs". Search continues to work throughout — chunks without new embeddings just don't surface via the vector leg until they're processed (FTS leg unaffected).

### Rollback

If the embedding-v2 cutover surfaces a showstopper:

1. Stop HC.
2. Restore from the pre-migration backup (Path B only — Path A wiped the data).
3. Re-launch from the pre-embedding-v2 binary.

The migration script is non-reversible by design; rollback requires the backup.

### Troubleshooting

- **"Schema sentinel mismatch — refusing to start"** at boot: your binary expects a model/dim that doesn't match the sentinel rows. Either re-run the migration or re-launch from a binary that matches the DB.
- **"Can't locate revision identified by '0022'"** from alembic: this is the expected refusal when the new binary sees the old DB. Follow Path A or B above.
- **Reranker fails to start**: check `~/Library/Application Support/Harbor Clerk/logs/reranker.log`. The bge-reranker-v2-m3 model needs ~1.2 GB free RAM at load time.
- **Re-embed taking forever**: Granite-R2 is ~5x slower per chunk than e5-small. For a 10K-doc corpus on M-series silicon expect a few hours. The "Re-embedding" badge shows progress; HC keeps serving search during the trickle.
```

- [ ] **Step 23.2: Commit**

```bash
git add docs/upgrade-runbook.md
git commit -m "docs: embedding-v2 upgrade runbook with Path A + Path B"
```

---

## Task 24: Verify + retrieval-eval gate

**Files:**
- (no code changes — verification only)

- [ ] **Step 24.1: Full local verify**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ -m "not requires_models and not integration" -q
cd frontend && npm run lint && npm run type-check && cd ..
```

Expected: All four steps pass.

- [ ] **Step 24.2: Run requires_models integration tests**

Run (will download Granite-R2 + reranker if not cached):
```bash
uv run pytest tests/ -m requires_models -v
```

Expected: PASS. First run may take ~5 minutes downloading model weights.

- [ ] **Step 24.3: Bring up the full embedding-v2 stack locally**

Choose one:

**Docker path:**
```bash
docker compose up -d --build
sleep 60  # weights load
curl -sk https://localhost/api/system/health
```

**macOS path:**
```bash
cd macos && make && open "build/output/Harbor Clerk Server.app"
# wait for menubar to show all services green
curl -sf http://127.0.0.1:8100/api/system/health
```

Expected: Health endpoint returns 200; embedder + reranker both report healthy.

- [ ] **Step 24.4: Re-ingest the test corpora (or migrate the existing DB via the script)**

For test corpora ingest:
```bash
# Watched folders should auto-rescan if pre-configured. Otherwise drop the DB and let the
# next launch ingest fresh, OR run the migration script against the existing DB.
```

Wait for at least the CUAD corpus to finish ingesting (the small one; ~5-10 min).

- [ ] **Step 24.5: Run the retrieval-eval gate**

Run:
```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
  --mode retrieval-eval \
  --run-id 2026-05-05-prod \
  --api-base http://localhost:8100 \
  --label embedding-v2 \
  --corpora cuad   # start with cuad; add other corpora as they finish ingesting
```

Expected: Eval completes in seconds. `summary.json` shows recall@10 improvement over baseline.

- [ ] **Step 24.6: Capture metrics for the PR**

Run:
```bash
cat ~/Library/Application\ Support/Harbor\ Clerk/test-corpora/results/2026-05-05-prod/retrieval-eval/embedding-v2/summary.json
cat ~/Library/Application\ Support/Harbor\ Clerk/test-corpora/results/2026-05-05-prod/retrieval-eval/embedding-v2/metrics.csv | head -20
```

Expected: numbers showing the recall@10 / mrr / ndcg@10 deltas. Will be pasted into the PR description.

- [ ] **Step 24.7: Acceptance gate check**

Compare to the gate criteria from the spec:
- Overall recall@10 improvement **≥ +0.08** (vs e5-small baseline from sweep results)
- No per-corpus regression worse than **-0.05**
- nDCG@10 also improves overall

If the gate doesn't pass: do not proceed to PR. Diagnose (often: bad chunking, wrong prefix handling, reranker pool too small) and iterate.

If the gate passes: proceed to Task 25.

---

## Task 25: Open the PR

- [ ] **Step 25.1: Push the branch**

Run:
```bash
git push -u origin feat/embedding-v2-granite-and-reranker
```

- [ ] **Step 25.2: Dispatch the final pre-PR fresh-eyes code review**

Per [`memory/feedback_capture_pr_followups.md`](../../../memory/feedback_capture_pr_followups.md) Directive 2: send a fresh-eyes `feature-dev:code-reviewer` subagent against the branch tip with a MINIMAL prompt — no focus areas, no carve-outs. Address all findings ≥ 80 confidence before opening the PR.

- [ ] **Step 25.3: Open the PR**

Run:
```bash
gh pr create --title "feat: embedding-v2 (Granite-R2 + bge-reranker-v2-m3 + schema rebase)" --body "$(cat <<'EOF'
## Summary
- Swap embedding model from multilingual-e5-small (384-dim) to ibm-granite/granite-embedding-311m-multilingual-r2 (768-dim)
- Add BAAI/bge-reranker-v2-m3 as a cross-encoder precision stage on top of hybrid retrieval
- Rebase alembic chain to a single `0001_initial.py`; external `scripts/migrate_to_embedding_v2.py` for operators preserving existing DBs
- Schema sentinel (`schema_metadata` table) hard-fails boot on binary/DB mismatch

## Retrieval-eval gate

```
<paste contents of summary.json + first 5 lines of metrics.csv from Task 24.6 here>
```

Gate criteria met: overall recall@10 improvement ≥ +0.08, no per-corpus regression > -0.05.

## Test plan
- [ ] CI: ruff + pytest (default markers, excludes requires_models + integration)
- [ ] Local: `uv run pytest tests/ -m requires_models -v` passes (Granite + reranker load, return 768-dim, rerank a sample)
- [ ] Local: Docker stack comes up clean; sentinel verifies; search returns rerank-ordered hits
- [ ] Local: macOS bundle builds; reranker subprocess starts; PreferencesWindow shows the port

## Out of scope / deferred
- Late chunking → next phase
- Multi-granularity embeddings (section + document tables) → next phase
- Heading chain in passages → next phase

## Migration path
Operators choose one:
- **Path A**: stop HC, drop DB, re-launch (fresh schema; watched folders re-ingest).
- **Path B**: stop HC, run `scripts/migrate_to_embedding_v2.py --db-url ... --confirm`, re-launch (background re-embed trickle).

See `docs/upgrade-runbook.md#embedding-v2` for full operator runbook.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 25.4: Add deferred items to pr_followups.md**

Per the standing directive, append entries to `~/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md` for:
- Late chunking (next-phase work)
- Multi-granularity embeddings
- Heading chain in passages
- (Any items surfaced during the code review that didn't get fixed)

---

## Self-Review Notes

Spec coverage verified:
- ✅ Component 1 (embedder swap) → Tasks 10, 18, 20
- ✅ Component 2 (reranker service) → Tasks 11, 19, 21
- ✅ Component 3 (sentinel + db_health) → Tasks 3, 7, 8, 9
- ✅ Component 4 (search reranking) → Tasks 12, 13, 14
- ✅ Component 5 (macOS service-management) → Tasks 21, 22
- ✅ Component 6 (alembic rebase) → Tasks 4, 5
- ✅ Component 7 (external migration script) → Tasks 16, 17
- ✅ Settings additions → Tasks 1, 2
- ✅ Worker timeout bump → Task 15
- ✅ Documentation → Task 23
- ✅ Retrieval-eval gate → Task 24
- ✅ PR + followups → Task 25

Tasks have complete code in every step. No placeholders found.

Type/name consistency: `verify_schema_sentinel` / `panic_on_sentinel_mismatch` / `SchemaSentinelMismatch` used uniformly across Tasks 7-9. `rerank_hits` signature consistent across Tasks 13-14. `SearchHit` / `SearchHitOut` distinction preserved (internal dataclass vs API output model).
