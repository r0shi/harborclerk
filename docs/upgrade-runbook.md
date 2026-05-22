# Harbor Clerk Upgrade Runbook

This document covers operator-facing procedures for breaking-change upgrades.
For incremental version bumps, normal alembic migrations apply automatically
when the app starts; this runbook only covers explicit cutover events.

## Embedding v2

### What this upgrade does

- Replaces the embedding model (`multilingual-e5-small` 384-dim → `granite-embedding-311m-multilingual-r2` 768-dim).
- Adds a cross-encoder reranker service (`bge-reranker-v2-m3`).
- Rebases the alembic migration chain into a single `0001_initial.py`.
- Bumps the macOS bundle / Docker images by ~2 GB (model weights).

### Compatibility

- Old `alembic_version='0023'` databases REFUSE to upgrade in-app — the new binary's alembic chain doesn't include revision `0023`. This is intentional. You must either wipe + re-ingest (Path A) or run the external migration script (Path B).
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
- **"Can't locate revision identified by '0023'"** from alembic: this is the expected refusal when the new binary sees the old DB. Follow Path A or B above.
- **Reranker fails to start**: check `~/Library/Application Support/Harbor Clerk/logs/reranker.log`. The bge-reranker-v2-m3 model needs ~1.2 GB free RAM at load time.
- **Re-embed taking forever**: Granite-R2 is ~5x slower per chunk than e5-small. For a 10K-doc corpus on M-series silicon expect a few hours. The "Re-embedding" badge shows progress; HC keeps serving search during the trickle.
