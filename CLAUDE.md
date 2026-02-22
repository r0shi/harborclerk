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
| **worker-io** | RQ worker for `io` queue (extract/chunk/finalize) |
| **worker-cpu** | RQ worker for `cpu` queue (ocr/embed) |
| **embedder** | sentence-transformers model server (all-MiniLM-L6-v2, 384-dim) |
| **postgres** | PostgreSQL + pgvector + pg_trgm |
| **redis** | RQ job queue backend |
| **minio** | Object storage for originals |
| **tika** | Apache Tika server (RTF and fallback extraction) |

### macOS Native Apps

Two native macOS apps under `macos/`:

- **Harbor Clerk Server** — menubar agent app managing all backend services as subprocesses (PostgreSQL, Redis, Embedder, API, Workers)
- **Harbor Clerk** — WKWebView app wrapping the React SPA from localhost

Build scripts in `macos/scripts/`, orchestrated by `macos/Makefile`.

### Storage Backend

Configurable via `STORAGE_BACKEND` env var:
- `minio` (default) — MinIO/S3-compatible object storage (Docker Compose)
- `filesystem` — local filesystem under `STORAGE_PATH` (native macOS app)

### RTF Extraction

Configurable: uses Apache Tika when `TIKA_URL` is set, falls back to `striprtf` library when empty.

**No multi-tenancy.** Single-tenant appliance — no `tenant_id` anywhere in schema or API.

**UI:** Vite + React + TypeScript SPA, built to static files and served by FastAPI at `/`.

## Python Package

Package name: `harbor_clerk` (under `src/harbor_clerk/`).

Entry points:
- `harbor-clerk-api` — FastAPI server
- `harbor-clerk-worker` — RQ worker
- `harbor-clerk-seed` — Database seeder

## Ingestion Pipeline

Five idempotent stages, each guarded by row-level lock on `(version_id, stage)` in `ingestion_jobs`:

1. **extract** (io) — PyMuPDF for PDF, python-docx for DOCX, striprtf/Tika for RTF, plain text fallback
2. **ocr** (cpu) — conditional: always for JPEG; PDF if `extracted_chars < 500` or `alpha_ratio < 0.2`; never for DOCX/RTF/TXT. Uses pdftoppm + Tesseract (eng+fra)
3. **chunk** (io) — ~1000 char target, 150 char overlap, preserves page ranges + char offsets. Detects language per chunk
4. **embed** (cpu) — calls embedder container over HTTP, 384-dim vectors stored in pgvector
5. **finalize** (io) — completes ingestion

**Job timeouts:** RQ native `job_timeout` per stage with `on_failure` callback updating `ingestion_jobs` to error. Reaper (every 5 min) only handles orphans — jobs marked running in DB but absent from RQ registry.

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
- MCP: `POST /mcp` — tools: `kb_search`, `kb_read_passages`, `kb_get_document`, `kb_list_recent`, `kb_ingest_status`, `kb_reprocess`, `kb_system_health`
- SSE: `GET /api/jobs/stream` — streams job progress events (server→client only)

## Database

PostgreSQL with extensions: `pgcrypto`, `vector`, `pg_trgm`. No tenant table or tenant_id columns.

Key tables: `users`, `api_keys`, `documents`, `document_versions`, `document_pages`, `chunks`, `ingestion_jobs`, `uploads`, `audit_log`.

`chunks` has dual FTS columns (`fts_en` TSVECTOR, `fts_fr` TSVECTOR) both as generated stored columns with GIN indexes, plus `embedding vector(384)` with HNSW index. Full DDL in `spec.txt` section I.

Storage bucket: `originals`, key pattern: `originals/versions/<version_id>/<original_filename>`.

## Worker Presets (C = logical cores)

- **Quiet**: io=1, cpu=1
- **Balanced**: io=max(2, C//4), cpu=1
- **Fast**: io=max(2, C//2), cpu=min(2, max(1, C//4))
- Hard caps: io_workers ≤ 8, cpu_workers ≤ 2
