# AGENTS.md — `src/harbor_clerk`

Backend invariants. Everything here is verified against source; stage
assignments, timeouts, and queue membership are published as a generated table
in `docs/architecture.md#ingestion-pipeline-stages`.

## Ingestion pipeline

- **Every stage is idempotent** and guarded by a row-level lock on
  `(doc_id, stage)` via `SELECT ... FOR UPDATE SKIP LOCKED`.
- **Always call `mark_stage_done` on every exit path, including early returns.**
  An early `return` on a pipeline-seq race once skipped it, leaving the job row
  `running` until the reaper fired 20 minutes later — and re-enqueue is
  suppressed while a row is `running`, so the document simply stalled.
- **Sequential prefix:** `extract` → `ocr` → `chunk`. Then `entities` and
  `embed` fan out and **gate `finalize`**.
- **`summarize` is a background stage.** It runs on the `llm` queue and does
  **not** gate `finalize`; a document is searchable before its summary exists.
  See the rationale comment in `worker/pipeline.py`.
- **Three queues: `io`, `cpu`, `llm`.** A new stage must be assigned to one, and
  every queue must have a subscriber — a queue nobody listens to silently
  swallows work (this shipped once; `tests/test_docker_compose_queues.py` now
  guards it).
- **OCR is conditional** and runs in-process (pypdfium2 + Tesseract). It never
  calls Tika.

## Extraction

Three paths, not two. Plain-text formats decode directly via
`file_types.PLAIN_TEXT_EXTENSIONS`; markdown has its own branch preserving
heading structure (`worker/markdown_extract.py`); everything else goes through
Tika. Don't invent a fourth.

## Storage

All storage access goes through the `StorageBackend` ABC — never touch MinIO or
the filesystem directly. `STORAGE_BACKEND` selects the implementation.

Watched-folder documents are read in place from `source_path` and are never
written to. IMAP messages and attachments are the one case where content is
fetched and stored, because there is no file on disk to reference.

## Retrieval

- Full-text search is **bilingual**: query both `fts_en` and `fts_fr`.
- The reranker is on by default (`reranker_enabled`) and **degrades gracefully**
  — if it is unreachable, the merged FTS+vector score is the final ordering.
  Never let a reranker failure fail a search.
- Every result carries citations.

## Data model

- The `documents` model is **flat**. There is no version table; reprocessing
  updates the row in place. Same SHA on a known path is a no-op; a changed SHA
  reprocesses.
- `chunks.embedding` is `vector(768)` (Granite-R2). `schema_metadata` records
  the embedding model and dimension, and startup refuses a mismatch — changing
  the model requires a migration, not just a config edit.
- No `tenant_id`. Anywhere.

## Async gotchas

- `enqueue_stage()` uses a **sync** session. Calling it in a loop from an async
  endpoint blocks the event loop — wrap it in `run_in_executor`.
- SQLAlchemy enum members must be **lowercase** to match Postgres values: both
  `postgresql.ENUM` and `sa.Enum` send `.name`, not `.value`.

## Legacy upload storage

The legacy upload path stores originals in the `originals` bucket under
`originals/<doc_id>/<original_filename>`. Watched-folder and IMAP documents do
**not** use it — watched files are read in place from `source_path`, and mail
content is stored via the normal document path.
