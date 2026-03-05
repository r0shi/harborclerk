# Harbor Clerk Architecture

## System Overview

```mermaid
graph TB
    subgraph Clients
        browser["Browser / WKWebView"]
        mcp_client["MCP Client<br/>(Claude, etc.)"]
    end

    subgraph Gateway
        caddy["Caddy<br/>HTTPS termination<br/>reverse proxy"]
    end

    subgraph Application
        api["FastAPI<br/>REST API + MCP + SPA"]
    end

    subgraph Workers
        wio["Worker (io queue)<br/>extract, chunk, entities,<br/>summarize, finalize"]
        wcpu["Worker (cpu queue)<br/>OCR, embed"]
    end

    subgraph Data
        pg[("PostgreSQL 18<br/>+ pgvector + pg_trgm")]
        store["Object Storage<br/>MinIO or Filesystem"]
    end

    subgraph Services
        tika["Apache Tika<br/>text extraction"]
        embedder["Embedder<br/>all-MiniLM-L6-v2<br/>384-dim"]
        llama["llama.cpp<br/>local LLM inference"]
    end

    browser -- "HTTPS" --> caddy
    mcp_client -- "HTTPS POST /mcp" --> caddy
    caddy --> api

    api -- "async queries" --> pg
    api -- "upload/download" --> store
    api -- "chat streaming" --> llama
    api -- "SSE /api/jobs/stream" --> browser

    wio -- "poll jobs<br/>LISTEN/NOTIFY" --> pg
    wcpu -- "poll jobs<br/>LISTEN/NOTIFY" --> pg
    wio -- "extract" --> tika
    wcpu -- "OCR" --> tika
    wcpu -- "embed" --> embedder
    wio -- "store originals" --> store

    llama -. "tool calls<br/>via API" .-> api
```

## Ingestion Pipeline

Seven idempotent stages, each guarded by row-level lock on `(version_id, stage)`:

```mermaid
graph LR
    upload(("Upload"))

    extract["1. extract<br/><i>io queue</i><br/>Tika / plain text"]
    ocr["2. ocr<br/><i>cpu queue</i><br/>pypdfium2 + Tesseract"]
    chunk["3. chunk<br/><i>io queue</i><br/>~1000 char, 150 overlap"]

    entities["4. entities<br/><i>io queue</i><br/>spaCy NER"]
    embed["5. embed<br/><i>cpu queue</i><br/>384-dim vectors"]
    summarize["6. summarize<br/><i>io queue</i><br/>LLM summary"]

    finalize["7. finalize<br/><i>io queue</i><br/>mark complete"]

    upload --> extract --> ocr --> chunk

    chunk --> entities & embed & summarize

    entities --> finalize
    embed --> finalize
    summarize --> finalize

    style ocr stroke-dasharray: 5 5
```

> OCR (dashed) is conditional: always for images; PDF only if extracted text is sparse; skipped for text-native formats.

## Retrieval Flow

```mermaid
graph LR
    query["Search Query"]
    fts["PostgreSQL FTS<br/>bilingual (en + fr)"]
    vec["pgvector<br/>cosine similarity"]
    merge["Merge & Dedupe<br/>version boost<br/>OCR confidence boost"]
    results["Top K Results<br/>with citations"]

    query --> fts & vec
    fts --> merge
    vec --> merge
    merge --> results
```

## Deployment Modes

### Docker Compose (Linux / DIY)

```mermaid
graph TB
    subgraph docker["Docker Compose"]
        gw["gateway<br/>Caddy"]
        app["app<br/>FastAPI"]
        wio["worker-io"]
        wcpu["worker-cpu"]
        emb["embedder"]
        pg[("postgres<br/>pgvector/pgvector:pg18")]
        minio["minio"]
        tika["tika"]
        llama["llama-server"]
    end

    gw --> app
    app --> pg & minio & llama
    wio --> pg & tika & minio
    wcpu --> pg & tika & emb
```

### macOS Native

```mermaid
graph TB
    subgraph menubar["Harbor Clerk Server<br/>(menubar agent)"]
        sm["ServiceManager"]
        pg["PostgreSQL 18<br/>(subprocess)"]
        tika["Tika<br/>(subprocess)"]
        emb["Embedder<br/>(subprocess)"]
        llama["llama.cpp<br/>(subprocess)"]
        api["harbor-clerk-api<br/>(subprocess)"]
        wio["worker io<br/>(subprocess)"]
        wcpu["worker cpu<br/>(subprocess)"]
    end

    client["Harbor Clerk<br/>(WKWebView app)"]

    sm --> pg & tika & emb & llama & api & wio & wcpu
    client -- "http://localhost:8000" --> api
```

## Data Model (key tables)

```mermaid
erDiagram
    users ||--o{ api_keys : creates
    users ||--o{ conversations : has
    conversations ||--o{ chat_messages : contains

    documents ||--o{ document_versions : has
    document_versions ||--o{ document_pages : has
    document_versions ||--o{ document_headings : has
    document_versions ||--o{ chunks : has
    document_versions ||--o{ entities : has
    document_versions ||--o{ ingestion_jobs : tracks

    upload_sessions ||--o{ uploads : groups
    uploads ||--o| document_versions : creates

    chunks {
        text content
        vector embedding_384dim
        tsvector fts_en
        tsvector fts_fr
    }

    documents {
        text title
        text filename
    }

    document_versions {
        enum status
        text summary
    }

    ingestion_jobs {
        enum stage
        enum status
        timestamp heartbeat_at
    }
```

## Auth Model

```mermaid
graph LR
    human["Human User"]
    apikey["API Key Client"]

    human -- "email + password" --> jwt["JWT<br/>access + refresh"]
    apikey -- "Authorization: Bearer" --> hash["key_hash lookup"]

    jwt --> api["API"]
    hash --> api

    api -- "role: admin" --> full["Full Access"]
    api -- "role: user" --> limited["Read + Upload"]
    api -- "api_key" --> readonly["Read-Only"]
```
