# Topic Modeling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add BERTopic-based topic modeling that gives the chat LLM awareness of what the corpus covers, via a system prompt hint and a `corpus_topics` tool.

**Architecture:** BERTopic runs as a background job over document centroid embeddings (already in DB). Results are cached in a `corpus_topics` table. A compact topic summary is injected into the chat system prompt. A new `corpus_topics` tool provides full detail on demand.

**Tech Stack:** BERTopic, HDBSCAN, umap-learn, SQLAlchemy, FastAPI, React/TypeScript

---

### Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add BERTopic and its key deps**

In `pyproject.toml`, add to the `dependencies` list:

```
"bertopic>=0.16",
"hdbscan>=0.8.33",
"umap-learn>=0.5",
```

**Step 2: Lock and verify**

```bash
cd /Users/alex/mcp-gateway && uv lock && uv sync
uv run python -c "from bertopic import BERTopic; print('OK')"
```

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add bertopic, hdbscan, umap-learn dependencies"
```

---

### Task 2: DB migration — topic tables and document.topic_id

**Files:**
- Create: `alembic/versions/0009_topic_modeling.py`
- Modify: `src/harbor_clerk/models/document.py` — add `topic_id` column
- Create: `src/harbor_clerk/models/corpus_topic.py` — new ORM model

**Step 1: Create ORM model for corpus_topics**

Create `src/harbor_clerk/models/corpus_topic.py`:

```python
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class CorpusTopic(Base):
    __tablename__ = "corpus_topics"

    topic_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    representative_doc_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorpusTopicsMeta(Base):
    __tablename__ = "corpus_topics_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default="1")
    last_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    corpus_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**Step 2: Add topic_id to Document model**

In `src/harbor_clerk/models/document.py`, add:

```python
topic_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("corpus_topics.topic_id", ondelete="SET NULL"), nullable=True)
```

**Step 3: Register models in `__init__.py`**

In `src/harbor_clerk/models/__init__.py`, add:

```python
from harbor_clerk.models.corpus_topic import CorpusTopic, CorpusTopicsMeta
```

**Step 4: Create migration**

Create `alembic/versions/0009_topic_modeling.py`:

```python
"""Add topic modeling tables and document.topic_id."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corpus_topics",
        sa.Column("topic_id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("keywords", ARRAY(sa.Text()), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("representative_doc_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "corpus_topics_meta",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corpus_hash", sa.Text(), nullable=True),
    )
    # Idempotent: check before adding column
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='documents' AND column_name='topic_id'"
    ))
    if not result.fetchone():
        op.add_column("documents", sa.Column(
            "topic_id", sa.Integer(),
            sa.ForeignKey("corpus_topics.topic_id", ondelete="SET NULL"),
            nullable=True,
        ))


def downgrade() -> None:
    op.drop_column("documents", "topic_id")
    op.drop_table("corpus_topics_meta")
    op.drop_table("corpus_topics")
```

**Step 5: Commit**

```bash
git add src/harbor_clerk/models/corpus_topic.py src/harbor_clerk/models/document.py \
    src/harbor_clerk/models/__init__.py alembic/versions/0009_topic_modeling.py
git commit -m "feat: add corpus_topics tables and document.topic_id"
```

---

### Task 3: Topic computation module

**Files:**
- Create: `src/harbor_clerk/topics.py`

**Step 1: Implement topic computation**

Create `src/harbor_clerk/topics.py`. This module:
1. Fetches document centroids from the DB (same query as `/stats/clusters`)
2. Runs BERTopic with UMAP + HDBSCAN
3. Stores results in `corpus_topics` table
4. Updates `documents.topic_id` for each document
5. Updates `corpus_topics_meta` with timestamp and hash

Key points:
- Use `BERTopic(umap_model=UMAP(...), hdbscan_model=HDBSCAN(...), calculate_probabilities=False)` — skip sentence-transformers (we already have embeddings)
- Call `topic_model.fit(docs, embeddings)` where `docs` are document titles/summaries (for c-TF-IDF labeling) and `embeddings` are the centroids
- Extract topic labels from `topic_model.get_topic_info()`
- The function should be `async def recompute_topics()` and use `run_in_executor` for the CPU-bound BERTopic fitting
- Compute `corpus_hash` as `f"{doc_count}:{latest_version_timestamp}"` for staleness detection
- Minimum doc count: skip if fewer than 10 documents (BERTopic needs a reasonable corpus)

**Step 2: Add helper to get cached topic summary string**

Add `async def get_topic_summary() -> str | None` that reads the `corpus_topics` table and returns a formatted one-liner like:
`"The corpus covers 15 topics: Wine Regions & Terroir, French Pastry Technique, Japanese Culinary Tools, ..."`

Returns `None` if no topics have been computed yet.

**Step 3: Commit**

```bash
git add src/harbor_clerk/topics.py
git commit -m "feat: BERTopic topic computation and caching"
```

---

### Task 4: Background refresh triggers

**Files:**
- Modify: `src/harbor_clerk/api/app.py` — add topic refresh check to reaper loop

**Step 1: Add staleness check to reaper loop**

In `_session_reaper_loop()` in `app.py`, after the existing reaper logic, add a topic refresh check:

```python
# Refresh topics if corpus changed
from harbor_clerk.topics import check_and_recompute_topics
await check_and_recompute_topics(db)
```

The `check_and_recompute_topics` function (in topics.py) should:
1. Compute current `corpus_hash`
2. Read `corpus_topics_meta.corpus_hash`
3. If different AND `last_computed_at` is >15 min ago (or None), run `recompute_topics`

**Step 2: Commit**

```bash
git add src/harbor_clerk/api/app.py src/harbor_clerk/topics.py
git commit -m "feat: topic refresh in reaper loop with staleness check"
```

---

### Task 5: API endpoints

**Files:**
- Modify: `src/harbor_clerk/api/routes/stats.py` — replace k-means `/stats/topics` with cached BERTopic results
- Modify: `src/harbor_clerk/api/routes/system.py` — add `POST /system/recompute-topics`

**Step 1: Replace `/stats/topics`**

Replace the `topic_clusters` endpoint (and remove `_kmeans`) with a simple query of the `corpus_topics` table. Return the same shape: `{"clusters": [...], "doc_count": N}` but with BERTopic-quality labels and keywords.

**Step 2: Add admin recompute endpoint**

Add `POST /api/system/recompute-topics` (admin-only) that calls `recompute_topics()` and returns the result count.

**Step 3: Commit**

```bash
git add src/harbor_clerk/api/routes/stats.py src/harbor_clerk/api/routes/system.py
git commit -m "feat: replace k-means topics with cached BERTopic, add recompute endpoint"
```

---

### Task 6: Chat tool and system prompt injection

**Files:**
- Modify: `src/harbor_clerk/llm/tools.py` — add `corpus_topics` tool definition and mapper
- Modify: `src/harbor_clerk/llm/chat.py` — inject topic summary into system prompt

**Step 1: Add `corpus_topics` tool**

In `tools.py`, add to `_BASE_CHAT_TOOLS`:

```python
{
    "type": "function",
    "function": {
        "name": "corpus_topics",
        "description": "List the main topics in the knowledge base with keywords and document counts. Use this to understand what the corpus covers before searching.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},
```

Add a mapper and dispatch entry that calls a new MCP function or directly queries the `corpus_topics` table.

**Step 2: Inject topic summary into chat system prompt**

In `chat_stream()`, after building the system prompt messages list, prepend the topic summary:

```python
from harbor_clerk.topics import get_topic_summary
topic_hint = await get_topic_summary()
if topic_hint:
    system_content = SYSTEM_PROMPT + f"\n\n## Corpus topics\n{topic_hint}"
else:
    system_content = SYSTEM_PROMPT
messages: list[dict] = [{"role": "system", "content": system_content}]
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/llm/tools.py src/harbor_clerk/llm/chat.py
git commit -m "feat: corpus_topics tool and system prompt topic hint"
```

---

### Task 7: Verify, lint, and build

**Step 1: Run Python checks**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check . && uv run ruff format --check .
```

**Step 2: Run frontend checks**

```bash
cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check && npm run format:check
```

**Step 3: Build macOS apps**

```bash
cd /Users/alex/mcp-gateway/macos && make apps
```

---

### Task 8: Create PR, merge, clean up

```bash
git push -u origin feat/topic-modeling
gh pr create --title "feat: BERTopic topic modeling for corpus awareness"
```

Watch CI, merge when green, clean up branch.
