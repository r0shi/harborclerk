# Harbor Clerk Upgrade Runbook

This document covers operator-facing procedures for breaking-change upgrades.
For incremental version bumps, normal alembic migrations apply automatically
when the app starts; this runbook only covers explicit cutover events.

## Docker: MinIO to filesystem storage

Up to #888 the Compose stack stored originals (uploads, IMAP messages and
attachments) in a `minio` service running `minio/minio:latest`. That image is
no longer published, so the default stack now uses the filesystem backend on the
`originals` named volume (`STORAGE_BACKEND=filesystem`, `STORAGE_PATH=/data/originals`
in `app`, `watcher`, `worker-io` and `worker-cpu`), and `minio` sits behind an
opt-in profile whose image you must name in `MINIO_IMAGE`.

Watched-folder documents are unaffected: they are read in place and were never
in MinIO. A fresh install, or one that never used uploads or IMAP, needs nothing.

An install with objects in MinIO that starts the new stack without copying them
keeps searching (the text is in Postgres). At startup `app` checks whether the
oldest documents' stored originals are on the volume and, if any are missing,
logs an ERROR naming this section. Until the copy:

- downloading one of those documents (with `ALLOW_SOURCE_DOWNLOAD=true`) fails
  with a server error (500); it works again as soon as its object is copied;
- an extract that runs on one of them (only a reprocess re-extracts an existing
  document) fails, and the document stays failed after the copy until it is
  reprocessed: on the Documents page filter by status **Failed**, select, and
  **Reprocess**, or as an admin `POST /api/docs/reprocess-failed`.

If that already happened, follow the steps below from step 2 anyway. The copy
never replaces a file the new stack wrote. It can bring back objects that the
new stack deleted in the meantime, such as staging files of uploads that
finished or were cancelled. Nothing references those, so they only take space.

The filesystem backend keeps MinIO's bucket and key, at
`STORAGE_PATH/<bucket>/<key>`, so the database needs no change. MinIO keeps its
own on-disk format, so copy through its API rather than out of its volume. The
copy runs once, with `app` and `watcher` stopped, before the new stack starts:
they are the only services that write originals, so once they are stopped
nothing new reaches MinIO, and once the new stack runs nothing ever does.

1. Find the MinIO image you already have: `docker image ls | grep -i minio`.
   The commands below use `minio/minio:latest`; substitute yours.
2. Keep your `.env`'s `MINIO_*` values and pull the new code. Stop the two
   services that write originals (the web UI and mail sync are down until
   step 6; the old MinIO container keeps running):

   ```bash
   docker compose stop app watcher
   ```

3. Build the images that run Harbor Clerk's code. `docker compose run` builds
   only when no image exists, so without this it runs the old image, which has
   no copy command. The embedder and reranker images are large and unchanged by
   this upgrade, so leave them out:

   ```bash
   docker compose build app watcher worker-io worker-cpu worker-llm
   ```

4. Start MinIO alone from the profile and wait until it is healthy; it reuses
   the existing `minio_data` volume:

   ```bash
   MINIO_IMAGE=minio/minio:latest docker compose --profile minio up -d --wait minio
   ```

5. Copy every object into the `originals` volume:

   ```bash
   docker compose run --rm --no-deps app python -m harbor_clerk.maintenance.copy_minio_originals
   ```

   Run it once, and never two at the same time: concurrent runs report each
   other's copies as `differs`. MinIO is only read, and a file already in the
   volume is never replaced. Each object is written to a `.partial-*` temporary
   file and moved into place. If the run is interrupted, run it again: it first
   removes the `.partial-*` files the interrupted run left, and what was copied
   counts as already present. If MinIO cannot be listed (not running, not
   healthy yet, wrong `MINIO_*` values), it says so and exits 1. It ends with
   `copied N, already present N, differs N, folder markers skipped N, failed N`,
   and exits non-zero if anything failed:

   - **already present**: the volume has a file of the same size at that key.
   - **differs**: the volume has a file of a different size at that key, which
     was left alone and its key logged. On a first copy into an empty volume
     this is 0. Otherwise the volume's file was written by the new stack (a new
     version of an upload keeps its key) and is the one to keep.
   - **folder markers**: zero-byte keys ending in `/`, which are not originals.
   - **failed**: an object that could not be read, a key that would land
     outside the bucket directory, or a key that clashes with the volume's
     directory layout (`a` where `a/b` exists, or the reverse); the log names
     each one.

   `--bucket NAME` copies a bucket other than `MINIO_BUCKET` (default `originals`).
6. Start the new stack, leaving `STORAGE_BACKEND` unset. It uses the images
   built in step 3. With `ALLOW_SOURCE_DOWNLOAD=true`, download one uploaded or
   mailed document to confirm:

   ```bash
   docker compose up -d
   ```

7. Retire MinIO: `MINIO_IMAGE=minio/minio:latest docker compose --profile minio rm -sf minio`.
   Do not run the copy again from here on. It would not replace anything, but it
   would bring back objects the new stack has since deleted: finished uploads'
   staging files, purged documents, deleted mail. Keep the
   `<project>_minio_data` volume until you are satisfied, then remove it with
   `docker volume rm`. The `MINIO_*` lines in `.env` are then unused.

To keep running MinIO instead, set `STORAGE_BACKEND=minio` and `MINIO_IMAGE` in
`.env` and add `--profile minio` to every `docker compose` command (or set
`COMPOSE_PROFILES=minio`). Nothing waits for MinIO to become healthy, so `app`
may restart once while it starts. This path depends on an image that is no
longer maintained; copying is the supported one.

## Docker: Postgres won't start after re-pulling `pgvector/pgvector:pg18`

The pg18 image now defaults `PGDATA` to `/var/lib/postgresql/18/docker` and
refuses to start when a volume is mounted at `/var/lib/postgresql/data` with
`PGDATA` unset; its log says there "appears to be PostgreSQL data in
/var/lib/postgresql/data (unused mount/volume)" (#894). Any install that pulls
the current image hits it, fresh or existing. `docker-compose.yml` now sets
`PGDATA: /var/lib/postgresql/data` on `postgres`, the directory the `pg_data`
volume has always been mounted at, so pulling the new compose file is the whole
fix: the existing cluster is used as it is. Do not move the volume to
`/var/lib/postgresql` instead. Postgres would then initialize a new, empty
cluster under `18/docker` and the existing data would look lost.

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
