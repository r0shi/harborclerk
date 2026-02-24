<p align="center">
  <img src="art/logo-large.png" alt="Harbor Clerk" width="280" />
</p>

# Harbor Clerk

**Keep your data. Ask it anything.**

Harbor Clerk is a safe harbor for your documents — and a capable clerk who knows where everything is. Drop in PDFs, scans, notes, or research files, and it turns them into a searchable, citation-backed knowledge base that runs entirely on your machine.

No SaaS account. No background sync. No shared tenancy. Your documents stay local.

Designed for small offices, independent operators, and privacy-focused individuals, Harbor Clerk runs comfortably on a Mac mini or similar hardware. It handles text extraction and OCR, builds hybrid full-text and vector search, and includes a built-in chat assistant powered by a local LLM — no cloud required. It also exposes a clean MCP endpoint so you can connect external models; they receive only cited snippets, never your full corpus.

This isn't a platform. It's a tool.
It keeps your documents where they belong — and makes them useful.

---

## Deployment Options

Harbor Clerk can run in two ways:

| | macOS Native | Docker Compose |
|---|---|---|
| **Best for** | Target audience — small offices with a Mac | DIY / Linux servers |
| **Services** | Managed by menubar app as subprocesses | Eight Docker containers |
| **Storage** | Local filesystem (`~/Library/Application Support/Harbor Clerk/`) | MinIO object storage + Docker volumes |
| **HTTPS** | Direct localhost access | Caddy reverse proxy with self-signed cert |

---

## macOS Native App

### Requirements

- Mac mini M2 or newer (M1 works, M2+ recommended)
- macOS 15.0 (Sequoia) or later
- 16 GB RAM minimum, 32 GB recommended for large document collections

### Getting Started

1. **Download** Harbor Clerk from the [releases page](https://github.com/r0shi/harborclerk/releases)
2. **Launch** "Harbor Clerk Server" — a menubar icon appears and services start automatically
3. **Open** "Harbor Clerk" (or click "Open Harbor Clerk" in the menubar) to access the web UI
4. **Create** your admin account on the setup page

### What Gets Installed

All data lives in `~/Library/Application Support/Harbor Clerk/`:

| Directory | Contents |
|---|---|
| `postgres-data/` | PostgreSQL database |
| `originals/` | Uploaded document files |
| `models/` | Downloaded LLM models for local chat |
| `logs/` | Service logs |
| `config.json` | Settings (ports, worker preset, etc.) |

### Preferences

Open Preferences (Cmd+,) from the menubar to configure:

- **Network Access** — allow remote browser or MCP connections (off by default — local only)
- **Performance** — worker preset (Quiet / Balanced / Fast)
- **Advanced** — ports, log level

---

## Docker Compose

### Prerequisites

- Docker Desktop (macOS/Windows) or Docker Engine + Compose (Linux)
- At least 4 GB RAM allocated to Docker (8 GB recommended)

### Quick Start

```bash
git clone https://github.com/r0shi/harborclerk.git
cd harborclerk
cp .env.example .env
```

Edit `.env` and change `SECRET_KEY` to a random string:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Start the stack:

```bash
docker compose up --build
```

Open **https://localhost/** and accept the self-signed certificate. Create your admin account on the setup page.

### Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-in-production` | JWT signing key — **change this** |
| `DATABASE_URL` | `postgresql+asyncpg://lka:...` | PostgreSQL connection string |
| `MINIO_ENDPOINT` | `minio:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin123` | MinIO secret key |
| `LOG_LEVEL` | `INFO` | Logging level |

For production, change the PostgreSQL and MinIO credentials as well.

### Managing the Stack

```bash
docker compose up --build         # build and start (foreground)
docker compose up --build -d      # build and start (background)
docker compose up --build -d app  # rebuild just the app
docker compose down               # stop (keeps data)
docker compose down -v            # stop and delete all data
docker compose logs -f app        # tail app logs
```

### Services

| Service | Role |
|---|---|
| **gateway** | Caddy reverse proxy with automatic HTTPS (self-signed) |
| **app** | FastAPI REST API + MCP endpoint + serves React SPA |
| **worker-io** | Background worker for text extraction and chunking |
| **worker-cpu** | Background worker for OCR and embedding |
| **embedder** | Embedding model server (nomic-embed-text-v2-moe, 768-dim) |
| **postgres** | PostgreSQL with pgvector and pg_trgm extensions |
| **minio** | Object storage for original files |
| **tika** | Apache Tika for PDF, DOCX, and RTF text extraction |

---

## Architecture

### Ingestion Pipeline

Upload a file and it goes through five idempotent stages:

1. **Extract** — pull text from PDF, DOCX, RTF, and other formats via Apache Tika
2. **OCR** — conditional: always for images, for PDFs with little extractable text. Uses Tesseract (English + French)
3. **Chunk** — split into ~1000 character segments with 150 char overlap, preserving page references
4. **Embed** — generate 768-dim vectors via the embedder service
5. **Finalize** — mark ingestion complete

Progress is streamed to the UI via server-sent events. Processing can be cancelled from the admin UI.

### Hybrid Search

Results combine PostgreSQL full-text search (bilingual English/French) and pgvector cosine similarity, merged and ranked with boosts for latest document versions and higher OCR confidence. All results include source citations with page numbers.

### Local Chat

A built-in chat assistant runs a local LLM (via llama-server) with access to the knowledge base through tool calls. Models can be downloaded and managed from the admin UI. No data leaves the machine.

### Auth

- **Human users**: email + password, JWT access tokens + refresh cookies. Roles: `admin` / `user`.
- **API keys**: admin-created, read-only, for MCP clients. Stored as SHA-256 hashes.

---

## API Reference

### REST

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/login` | POST | Login (email + password) |
| `/api/auth/refresh` | POST | Refresh access token |
| `/api/system/setup-status` | GET | Check if first-time setup is needed |
| `/api/setup` | POST | Create initial admin account |
| `/api/uploads` | POST | Upload documents |
| `/api/uploads/confirm` | POST | Confirm upload action |
| `/api/docs` | GET | List documents |
| `/api/docs/{id}` | GET | Document detail with versions |
| `/api/docs/{id}/content` | GET | Read document text (with page ranges) |
| `/api/docs/{id}` | DELETE | Soft-delete a document |
| `/api/docs/{id}/reprocess` | POST | Re-run ingestion |
| `/api/docs/{id}/cancel` | POST | Cancel in-progress ingestion |
| `/api/search` | POST | Hybrid search |
| `/api/passages/read` | POST | Read passages by chunk IDs |
| `/api/chat/conversations` | GET/POST | List or create chat conversations |
| `/api/chat/conversations/{id}/messages` | POST | Send a message (streamed response) |
| `/api/chat/models` | GET | List available LLM models |
| `/api/chat/models/{id}/download` | POST | Download a model |
| `/api/system/health` | GET | Health check |
| `/api/jobs/stream` | GET | SSE stream of job progress |

### MCP

`POST /mcp` — Streamable HTTP transport. Authenticate with `Authorization: Bearer <api_key>`.

| Tool | Description |
|---|---|
| `kb_search` | Hybrid search with citations |
| `kb_read_passages` | Read specific passages by chunk ID |
| `kb_get_document` | Document metadata and versions |
| `kb_list_recent` | Recently added documents |
| `kb_ingest_status` | Check ingestion progress |
| `kb_reprocess` | Re-run ingestion on a document |
| `kb_system_health` | System health check |

---

## Building from Source

### macOS Native Apps

```bash
cd macos
make all
```

This builds both apps into `macos/build/output/`. Requires Xcode command-line tools, Python 3.12+, and Homebrew (for Tesseract).

### Frontend

```bash
cd frontend
npm install
npm run dev     # dev server with HMR
npm run build   # production build → dist/
```

### Python Backend

The project uses [uv](https://docs.astral.sh/uv/) for Python package management:

```bash
uv sync
uv run harbor-clerk-api      # API server
uv run harbor-clerk-worker   # background worker
```

---

## License

MIT — see [LICENSE](LICENSE) for details. Third-party dependencies are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
