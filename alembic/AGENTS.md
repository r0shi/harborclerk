# AGENTS.md — `alembic`

Async Alembic migrations against PostgreSQL 18 with `vector`, `pg_trgm`, and
`citext`. The current schema is whatever `versions/` produces; the table list in
`docs/architecture.md` is generated from SQLAlchemy metadata, not hand-written.

## Enum member names must be lowercase

Both `postgresql.ENUM` and `sa.Enum` send the Python member **`.name`**, not its
`.value`. So this works:

```python
class Role(enum.Enum):
    admin = "admin"      # correct — name matches the PG label
```

and this silently produces `ADMIN` in SQL against a type whose label is `admin`:

```python
class Role(enum.Enum):
    ADMIN = "admin"      # wrong
```

## Embedding dimension is pinned by a sentinel

`chunks.embedding` is `vector(768)` (Granite-R2). `schema_metadata` records the
embedding model and dimension, and startup **refuses to run against a
mismatch**. Changing the model is a migration plus a re-embed, not a config
edit — a dimension change invalidates every stored vector.

## No `tenant_id`

Single-tenant by design. No tenant table, no tenant column, not in a migration,
not "for later".

## Log-table conventions

Audit and request logs follow a shape established by `imap_command_log`, and new
ones should match so a single admin view can serve them:

- table named `<domain>_log`
- typed FK columns (`account_id`, `api_key_id`) rather than opaque strings
- `created_at` indexed, because the retention reaper scans it
- `details` as JSONB for variable payloads; scalar columns for fixed shapes
- an explicit per-table retention policy

## Generated columns

`chunks.fts_en` and `chunks.fts_fr` are **generated stored** tsvector columns
with GIN indexes — they are computed by PostgreSQL, so never write to them from
application code.

## Functions under an expression index are never redefined in place

`ix_chunks_chunk_text_ws_trgm` indexes `hc_fold_whitespace(chunk_text)`
(0008, #819). The index stores values computed with the function body of the
day it was built; `CREATE OR REPLACE` on that function leaves every stored
entry stale, and the planner keeps using the index, so `text_contains`
silently misses rows. A changed fold is a **new function name in a new
migration** that builds a new index on it and drops the old one. 0008 creates
the function only if absent and raises if an existing one's body differs.

## Concurrent index builds must survive an interrupted attempt

An interrupted `CREATE INDEX CONCURRENTLY` leaves an INVALID index and the
revision unstamped; the retry's `IF NOT EXISTS` then skips it. Every startup
path retries on its own (MigrationRunner, Docker restart), so a migration that
builds concurrently drops an invalid leftover first and checks `indisvalid`
before dropping anything the new index replaces (0008's
`_build_index_concurrently`).
