# CLAUDE.md — `alembic`

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
