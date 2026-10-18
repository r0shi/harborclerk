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

## `created_at` is not an insertion order

`server_default=now()` is the **transaction's** start, so every row one
transaction writes ties on it, and the ids are random UUIDs. Ordering by
`created_at` alone returns ties in heap order. `chat_stream` writes a whole turn
in one transaction, which sent tool results before their calls (#809):
`chat_messages.seq` (0010) is an identity, and every reader orders by
`(created_at, seq)`; `tests/test_chat_message_order.py` guards both. A new
table whose readers need the order rows were written in needs the same key.

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
application code. Since 0011 (#828; 0009, #825, before it) they are computed
over `hc_fold_search_text(chunk_text)`: under LC_CTYPE C (the Mac app) the
parser reads every non-ASCII character, an NBSP or a curly apostrophe, as a
letter. Build every tsquery against them through `search.websearch_tsquery`,
which folds the query the same way (`text_contains.fold_search_text`, held to
the SQL function by `tests/test_search_fold.py`). The model's `Computed(...)`
text is the migration's, verbatim (a test compares them).

Changing a stored generated expression (`ALTER COLUMN ... SET EXPRESSION`)
rewrites `chunks` under ACCESS EXCLUSIVE and rebuilds **every** index on it,
the HNSW index included (40,000 chunks: 0009 about 26 s, 16 s of it HNSW, when
measured for #827; 0011 and 0009 both 39-40 s on one machine on one day). The
time of an expression in a generated column is paid once per column per row:
0011's fold costs 1.9 s per 40,000 chunks against 0.6 s for 0008's. A rewrite
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

`ix_chunks_chunk_text_fold_trgm` indexes `hc_fold_search_text(chunk_text)`,
and `fts_en` / `fts_fr` store values computed with it (0011, #828). Both hold
values computed with the function body of the day they were written; `CREATE
OR REPLACE` on that function leaves every stored entry stale, and the planner
keeps using the index, so `text_contains` and keyword search silently miss
rows. A changed fold is a **new function name in a new migration** that builds
a new index on it, resets the column expressions, and drops the old ones, as
0011 did to 0008's `hc_fold_whitespace`. Each fold migration creates its
function only if absent and raises if an existing one's definition differs.
`DROP FUNCTION ... CASCADE` on it drops both FTS columns.

When the change rewrites `chunks` anyway, build the new index in the same
transaction, not concurrently (0011): ACCESS EXCLUSIVE is already held, a
concurrent build would be rebuilt by the rewrite, and an interrupted run rolls
back whole with no INVALID leftover. Drop the old index before the rewrite so
it is not rebuilt only to be dropped. Expression-index statistics come only
from an `ANALYZE` with no column list.

## Concurrent index builds must survive an interrupted attempt

An interrupted `CREATE INDEX CONCURRENTLY` leaves an INVALID index and the
revision unstamped; the retry's `IF NOT EXISTS` then skips it. Every startup
path retries on its own (MigrationRunner, Docker restart), so a migration that
builds concurrently drops an invalid leftover first and checks `indisvalid`
before dropping anything the new index replaces (0008's
`_build_index_concurrently`).
