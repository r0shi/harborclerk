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
application code. Since 0009 (#825) they are computed over
`hc_fold_whitespace(chunk_text)`: under LC_CTYPE C (the Mac app) the parser
reads an NBSP as a letter. Build every tsquery against them through
`search.websearch_tsquery`, which folds the query the same way. The model's
`Computed(...)` text is the migration's, verbatim (a test compares them).

Changing a stored generated expression (`ALTER COLUMN ... SET EXPRESSION`)
rewrites `chunks` under ACCESS EXCLUSIVE and rebuilds **every** index on it,
the HNSW index included (40,000 chunks: about 26 s, 16 s of it HNSW). A rewrite
also rebuilds, and so validates, an INVALID index left by an interrupted
concurrent build. Killing the alembic client does not stop its backend: the lock
lasts for the whole rewrite unless the backend is cancelled. env.py names its
session from `HC_MIGRATION_APPLICATION_NAME` (`harbor_clerk.migration_session`),
and anything that runs alembic with a deadline cancels by that name, as the
`/system/run-migrations` route does.

## Migration runs are serial; wait for the lock by polling

env.py holds a session-level advisory lock (`MIGRATION_LOCK_KEY` in
`harbor_clerk.migration_session`) for the whole run, so a second runner waits
for the one in progress and then finds head, instead of rewriting `chunks`
again. Every online alembic command goes through it: `alembic current` and
`alembic stamp` now wait behind a running migration. The Run Migrations route
refuses with 409 while the lock is held rather than queueing.

**Never wait with `pg_advisory_lock`.** A backend blocked in it holds a
snapshot; `CREATE INDEX CONCURRENTLY` (0008) waits for every older snapshot in
the database while the waiter waits for the lock the build's session holds: a
deadlock, and the detector killed one of the two runs (the builder, mid-build,
leaves an INVALID index). env.py polls `pg_try_advisory_lock` once a second and
ends the transaction after every try. A transaction left open across tries is
worse: the deadlock becomes an undetected hang, because the build waits for
that transaction to end.

## Functions under an expression index are never redefined in place

`ix_chunks_chunk_text_ws_trgm` indexes `hc_fold_whitespace(chunk_text)`
(0008, #819), and `fts_en` / `fts_fr` store values computed with it (0009,
#825). Both hold values computed with the function body of the day they were
written; `CREATE OR REPLACE` on that function leaves every stored entry stale,
and the planner keeps using the index, so `text_contains` and keyword search
silently miss rows. A changed fold is a **new function name in a new
migration** that builds a new index on it, resets the column expressions, and
drops the old one. 0008 creates the function only if absent and raises if an
existing one's body differs; 0009 runs the same check. `DROP FUNCTION ...
CASCADE` on it drops both FTS columns.

## Concurrent index builds must survive an interrupted attempt

An interrupted `CREATE INDEX CONCURRENTLY` leaves an INVALID index and the
revision unstamped; the retry's `IF NOT EXISTS` then skips it. Every startup
path retries on its own (MigrationRunner, Docker restart), so a migration that
builds concurrently drops an invalid leftover first and checks `indisvalid`
before dropping anything the new index replaces (0008's
`_build_index_concurrently`).
