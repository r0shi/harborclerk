# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Harbor Clerk** — a single-tenant, web-first document dropbox for non-technical offices running on Mac mini/Studio. Local extraction, OCR (English/French), hybrid retrieval. Cloud LLMs query via MCP over HTTPS with read-only API keys, receiving only cited snippets (no full corpus upload).

The full specification lives in `spec.txt`.

## Architecture

### Docker Compose (DIY/Linux)

| Container | Role |
|---|---|
| **gateway** (Caddy) | HTTPS termination (self-signed CA), reverse proxy |
| **app** (FastAPI) | REST API + MCP endpoint + serves React SPA |
| **worker-io** | PostgreSQL-polling worker for `io` queue (extract/chunk/finalize) |
| **worker-cpu** | PostgreSQL-polling worker for `cpu` queue (ocr/embed) |
| **embedder** | multilingual-e5-small model server (384-dim, MIT) |
| **postgres** | PostgreSQL + pgvector + pg_trgm |
| **minio** | Object storage for originals |
| **tika** | Apache Tika server (text extraction for PDF, Office, eBook, HTML, email formats) |

### macOS Native Apps

Two native macOS apps under `macos/`:

- **Harbor Clerk Server** — menubar agent app managing all backend services as subprocesses (PostgreSQL, Tika, Embedder, API, Workers)
- **Harbor Clerk** — WKWebView app wrapping the React SPA from localhost

Build scripts in `macos/scripts/`, orchestrated by `macos/Makefile`.

### Storage Backend

Configurable via `STORAGE_BACKEND` env var:
- `minio` (default) — MinIO/S3-compatible object storage (Docker Compose)
- `filesystem` — local filesystem under `STORAGE_PATH` (native macOS app)

### Text Extraction

All non-TXT/MD/CSV/image formats (PDF, DOCX, DOC, ODT, XLSX, PPTX, RTF, EPUB, HTML, EML, etc.) are extracted via Apache Tika. TXT/MD/CSV use direct UTF-8 decode. Tika is required.

**No multi-tenancy.** Single-tenant appliance — no `tenant_id` anywhere in schema or API.

**UI:** Vite + React + TypeScript SPA, built to static files and served by FastAPI at `/`.

## Python Package

Package name: `harbor_clerk` (under `src/harbor_clerk/`).

Entry points:
- `harbor-clerk-api` — FastAPI server
- `harbor-clerk-worker` — PostgreSQL-polling background worker
- `harbor-clerk-seed` — Database seeder

## Ingestion Pipeline

Seven idempotent stages, each guarded by row-level lock on `(version_id, stage)` in `ingestion_jobs`:

1. **extract** (io, 600s) — Apache Tika for PDF/Office/eBook/HTML/email, plain text fallback for TXT/MD/CSV
2. **ocr** (cpu, 7200s) — conditional: always for images (JPEG/PNG/TIFF); PDF if `extracted_chars < 500` or `alpha_ratio < 0.2`; never for text-native formats. Uses pypdfium2 + Tesseract (eng+fra). Skipped if not needed.
3. **chunk** (io, 1200s) — ~1000 char target, 150 char overlap, preserves page ranges + char offsets. Detects language per chunk
4. **entities** (io, 900s) — spaCy NER (en_core_web_sm / fr_core_news_sm). Skipped if spaCy unavailable.
5. **embed** (cpu, 1800s) — calls embedder container over HTTP, 384-dim vectors stored in pgvector
6. **summarize** (io, 900s) — generates document summary
7. **finalize** (io, 600s) — completes ingestion

**Job timeouts:** `signal.alarm()` per stage with error handling updating `ingestion_jobs` to error. Workers send heartbeats every 30s. Reaper detects orphans via stale heartbeat (>90s) or 2x timeout fallback.

**Worker wakeup:** Workers use PostgreSQL `LISTEN` on per-queue channels (`job_enqueued_io`, `job_enqueued_cpu`) for instant wakeup instead of polling.

## Upload Flow

1. Compute SHA256 of uploaded file
2. If SHA256 matches existing version → duplicate
3. Otherwise ask user: "New document" or "New version of existing?" (future: auto-detect)

## Retrieval

Hybrid search: Postgres FTS (bilingual, queries both `fts_en` and `fts_fr` columns) + pgvector cosine → merge/dedupe with boosts for latest version and higher OCR confidence → top K (default 10). All results include citations. Returns `possible_conflict=true` when top results have similar scores across different versions/documents.

## Auth Model

- **Human users**: email + password → JWT access token + refresh cookie. Roles: `admin` / `user`
- **API keys**: admin-created, read-only, stored as `key_hash`. Header: `Authorization: Bearer <api_key>`

## Key API Surface

- REST: `/api/auth/login`, `/api/uploads`, `/api/docs`, `/api/search`, `/api/passages/read`, `/api/system/health`
- MCP: `POST /mcp` — 16 tools: `kb_search`, `kb_batch_search`, `kb_read_passages`, `kb_expand_context`, `kb_get_document`, `kb_list_recent`, `kb_corpus_overview`, `kb_document_outline`, `kb_find_related`, `kb_entity_search`, `kb_entity_overview`, `kb_entity_cooccurrence`, `kb_read_document`, `kb_ingest_status`, `kb_reprocess`, `kb_system_health`
- SSE: `GET /api/jobs/stream` — streams job progress events (server→client only)

## Database

PostgreSQL 18 with extensions: `vector`, `pg_trgm`, `citext`. No tenant table or tenant_id columns.

Key tables: `users`, `api_keys`, `documents`, `document_versions`, `document_pages`, `document_headings`, `chunks`, `entities`, `ingestion_jobs`, `uploads`, `upload_sessions`, `audit_log`, `conversations`, `chat_messages`.

`chunks` has dual FTS columns (`fts_en` TSVECTOR, `fts_fr` TSVECTOR) both as generated stored columns with GIN indexes, plus `embedding vector(384)` with HNSW index. Full DDL in `spec.txt` section I.

Storage bucket: `originals`, key pattern: `originals/versions/<version_id>/<original_filename>`.

## Linting & Formatting

**Python:** Ruff (config in `pyproject.toml`). Rules: E/F/W/I/UP/B/SIM. Line length 120.
- `uv run ruff check .` — lint
- `uv run ruff format .` — format
- Ruff is in `[project.optional-dependencies] test`

**Frontend:** ESLint 10 (flat config) + Prettier. Config in `frontend/eslint.config.js` and `frontend/.prettierrc`. `frontend/.npmrc` has `legacy-peer-deps=true` for peer dep compatibility.
- `npm run lint` / `npm run format:check` / `npm run type-check`

## CI

GitHub Actions workflows run on PRs to `main`. Branch protection requires all 5 checks to pass.

**`.github/workflows/ci.yml`:**
- **python** job: ruff check, ruff format --check, pytest (with pgvector service container)
- **frontend** job: eslint, prettier --check, tsc --noEmit

**`.github/workflows/codeql.yml`:**
- **codeql** job: Python SAST via CodeQL (`security-and-quality` queries). Also runs weekly (Monday 6am UTC)

**`.github/workflows/security.yml`:**
- **dependency-audit** job: `pip-audit` (HIGH severity, via `pypa/gh-action-pip-audit`) + `npm audit` (high/critical)
- **container-scan** job: builds app + embedder Docker images, scans with Trivy (HIGH/CRITICAL, `--ignore-unfixed`)

**Dependabot** (`.github/dependabot.yml`): weekly pip + npm dependency update PRs. Security updates (auto-PRs for CVEs) also enabled.

**Secret scanning** + **push protection** enabled at repo level.

## Worker Presets (C = logical cores)

- **Quiet**: io=1, cpu=1
- **Balanced**: io=max(2, C//4), cpu=2
- **Fast**: io=max(2, C//2), cpu=3
- Hard caps: io_workers ≤ 8, cpu_workers ≤ 4
