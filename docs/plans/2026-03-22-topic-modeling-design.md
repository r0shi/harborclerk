# Topic Modeling for Corpus Awareness

**Date:** 2026-03-22
**Status:** Approved

## Problem

The chat LLM doesn't know what topics the corpus covers until it searches. For broad queries ("compare all wine regions"), it may miss topics it doesn't think to search for. The existing `corpus_overview` tool lists documents but not topics — at 2,800 docs, it's too large to inject as context and doesn't provide thematic grouping.

## Design

### 1. BERTopic Clustering

A new module `src/harbor_clerk/topics.py` runs BERTopic over document centroids (average chunk embeddings, already computed for the stats clusters endpoint).

**Pipeline:** Pull centroids from DB → UMAP reduction (384-dim → ~15-dim) → HDBSCAN clustering → c-TF-IDF for topic keywords → store results.

**UMAP dependency:** Include `umap-learn` as a Python dependency. BERTopic's default pipeline is UMAP → HDBSCAN → c-TF-IDF. Skipping UMAP and passing 384-dim vectors directly to HDBSCAN performs poorly — distance metrics lose discriminative power above ~50 dimensions. The frontend's `umap-js` (used for the interactive 2D cluster map) serves a different purpose and isn't a substitute. The ~20MB cost is justified by quality.

**Dependencies:** `bertopic`, `hdbscan`, `umap-learn` added to main dependencies in `pyproject.toml`. All MIT/BSD licensed. Estimated ~70MB in venv, ~15MB in built app.

### 2. Storage

New table `corpus_topics`:
- `topic_id` (int, PK)
- `label` (text — e.g. "Wine Regions & Terroir")
- `keywords` (text[] — top 5 c-TF-IDF terms)
- `doc_count` (int)
- `representative_doc_ids` (uuid[] — 3 most central docs)
- `updated_at` (timestamptz)

New column on `documents`: `topic_id` (int, nullable FK → corpus_topics).

Staleness tracking: `corpus_topics_meta` single-row table with `last_computed_at` and `corpus_hash` (hash of doc count + latest version timestamp).

### 3. Refresh Triggers

- **Reaper loop** (every 5 min): check if `corpus_hash` changed since `last_computed_at`. If stale, recompute.
- **Batch upload completion**: after `_confirm_batch` finishes, enqueue a topic refresh.
- **Admin button**: "Recompute Topics" on Stats or System Maintenance page.
- **Minimum interval**: Skip if last run <15 minutes ago (prevents thrashing during bulk uploads).

### 4. Chat Integration

**System prompt hint** (~50 tokens): Injected dynamically at the start of each `chat_stream` call from the cached topics table. Format:

> "The corpus covers {n} topics: {comma-separated labels}. Use corpus_topics for details."

**New tool `corpus_topics`**: Returns full topic detail — label, keywords, doc count, representative doc titles. Available in both chat and research tool sets.

### 5. API

- `GET /api/stats/topics` — replace existing numpy k-means implementation with BERTopic results from cached table.
- `POST /api/system/recompute-topics` — admin-only, triggers immediate recompute.

### 6. Migration

- Add `corpus_topics` table.
- Add `corpus_topics_meta` table.
- Add `topic_id` column to `documents`.
