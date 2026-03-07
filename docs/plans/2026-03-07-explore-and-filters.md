# Explore View & Document Filters Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the existing document corpus browsable and explorable through filters, entity-based navigation, topic clusters, timeline, and related documents — surfacing signals already extracted during ingestion.

**Architecture:** Add a `doc_type` classification column populated during the summarize stage. Enhance the Documents page with multi-facet filtering (entity, file type, language, doc type) and sort controls. Create a new Explore page with entity-based virtual folders, auto-named topic clusters, and a timeline view. Add a related documents sidebar to the document detail page.

**Tech Stack:** Python/FastAPI backend, PostgreSQL (pgvector, pg_trgm), Alembic async migrations, React/TypeScript/Tailwind frontend, existing LLM summarize infrastructure.

---

## Task 1: Alembic Migration — Add `doc_type` Column

**Files:**
- Create: `alembic/versions/0002_add_doc_type.py`
- Modify: `src/harbor_clerk/models/document_version.py`

**Step 1: Create the migration**

```bash
cd /Users/alex/mcp-gateway
uv run alembic revision -m "add doc_type to document_versions" --rev-id 0002
```

**Step 2: Write the migration**

Edit `alembic/versions/0002_add_doc_type.py`:

```python
"""add doc_type to document_versions

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("doc_type", sa.Text(), nullable=True))
    op.create_index("ix_versions_doc_type", "document_versions", ["doc_type"])


def downgrade() -> None:
    op.drop_index("ix_versions_doc_type", table_name="document_versions")
    op.drop_column("document_versions", "doc_type")
```

**Step 3: Add the column to the SQLAlchemy model**

In `src/harbor_clerk/models/document_version.py`, add after the `summary_model` column:

```python
doc_type: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**Step 4: Add `doc_type` to the DocumentSummary schema**

In `src/harbor_clerk/api/schemas/documents.py`, add `doc_type: str | None = None` to `DocumentSummary`.

**Step 5: Run the migration against a test DB to verify**

```bash
uv run alembic upgrade head
```

**Step 6: Commit**

```bash
git add alembic/versions/0002_add_doc_type.py src/harbor_clerk/models/document_version.py src/harbor_clerk/api/schemas/documents.py
git commit -m "feat: add doc_type column to document_versions"
```

---

## Task 2: Classify Document Type During Summarize Stage

**Files:**
- Modify: `src/harbor_clerk/llm/summarize.py`
- Modify: `src/harbor_clerk/worker/stages/summarize.py`
- Test: `tests/test_summarize.py`

**Context:** The summarize stage already sends document text to the LLM. We add a lightweight follow-up call (or combine into the existing prompt) to classify the document type as a short phrase.

**Step 1: Write the failing test**

In `tests/test_summarize.py`, add:

```python
class TestClassifyDocType:
    def test_returns_short_phrase(self):
        """classify_doc_type should return a short classification phrase."""
        from harbor_clerk.llm.summarize import classify_doc_type
        # Will test with mock LLM in integration; for now verify function exists
        assert callable(classify_doc_type)

    def test_extractive_fallback_uses_mime(self):
        """When no LLM is available, fall back to MIME-based classification."""
        from harbor_clerk.llm.summarize import _mime_to_doc_type
        assert _mime_to_doc_type("application/pdf") == "PDF Document"
        assert _mime_to_doc_type("text/plain") == "Text File"
        assert _mime_to_doc_type("text/csv") == "Spreadsheet"
        assert _mime_to_doc_type("image/jpeg") == "Image"
        assert _mime_to_doc_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "Word Document"
        assert _mime_to_doc_type("application/epub+zip") == "E-Book"
        assert _mime_to_doc_type("message/rfc822") == "Email"
        assert _mime_to_doc_type("application/x-unknown") == "Document"
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_summarize.py::TestClassifyDocType -v
```

**Step 3: Implement `classify_doc_type` and `_mime_to_doc_type`**

In `src/harbor_clerk/llm/summarize.py`, add:

```python
_MIME_TYPE_MAP = {
    "application/pdf": "PDF Document",
    "text/plain": "Text File",
    "text/markdown": "Text File",
    "text/csv": "Spreadsheet",
    "image/jpeg": "Image",
    "image/png": "Image",
    "image/tiff": "Image",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word Document",
    "application/msword": "Word Document",
    "application/vnd.oasis.opendocument.text": "Word Document",
    "application/rtf": "Word Document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Presentation",
    "application/epub+zip": "E-Book",
    "text/html": "Web Page",
    "message/rfc822": "Email",
}


def _mime_to_doc_type(mime_type: str) -> str:
    """Fallback doc type classification from MIME type."""
    if mime_type in _MIME_TYPE_MAP:
        return _MIME_TYPE_MAP[mime_type]
    if mime_type.startswith("image/"):
        return "Image"
    if mime_type.startswith("text/"):
        return "Text File"
    return "Document"


async def classify_doc_type(chunks: list[str], mime_type: str = "") -> str:
    """Classify the document type as a short phrase (2-4 words).

    Uses the first ~2000 chars of the document to ask the LLM for a classification.
    Falls back to MIME-based classification if no LLM is available.
    """
    settings = get_settings()
    if not settings.llm_model_id:
        return _mime_to_doc_type(mime_type)

    # Use first ~2000 chars for classification (enough to determine type)
    sample = "\n\n".join(chunks)[:2000]

    prompt = (
        "What type of document is this? Respond with ONLY a short phrase (2-4 words) "
        "like: Legal Contract, Tax Return, Meeting Notes, Research Paper, Invoice, "
        "Recipe, Resume, Technical Manual, News Article, Personal Letter, etc. "
        "Do not explain, just the type.\n\n"
        f"Document excerpt:\n{sample}"
    )

    try:
        result = await _call_llm(prompt, max_tokens=20)
        # Clean up: strip quotes, periods, whitespace, limit length
        doc_type = result.strip().strip('"\'.')
        if len(doc_type) > 50:
            doc_type = doc_type[:50]
        return doc_type or _mime_to_doc_type(mime_type)
    except Exception:
        logger.debug("LLM doc_type classification failed, using MIME fallback", exc_info=True)
        return _mime_to_doc_type(mime_type)
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_summarize.py::TestClassifyDocType -v
```

**Step 5: Wire into the summarize worker stage**

In `src/harbor_clerk/worker/stages/summarize.py`, after the `generate_summary()` call, add:

```python
# Classify document type
doc_type = await classify_doc_type(chunk_texts, mime_type=version.mime_type or "")
version.doc_type = doc_type
```

Import `classify_doc_type` from `harbor_clerk.llm.summarize`.

**Step 6: Update the Documents API to return doc_type**

In `src/harbor_clerk/api/routes/documents.py`, in the `list_documents` function where `DocumentSummary` is constructed, add `doc_type=latest.doc_type` (alongside `summary`, `summary_model`).

**Step 7: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/llm/summarize.py src/harbor_clerk/worker/stages/summarize.py src/harbor_clerk/api/routes/documents.py
uv run ruff format src/harbor_clerk/llm/summarize.py src/harbor_clerk/worker/stages/summarize.py src/harbor_clerk/api/routes/documents.py
git add -A
git commit -m "feat: classify doc_type during summarize stage"
```

---

## Task 3: Entity Autocomplete API Endpoint

**Files:**
- Modify: `src/harbor_clerk/api/routes/documents.py`
- Modify: `src/harbor_clerk/api/schemas/documents.py`
- Test: `tests/test_api_docs.py` (or create `tests/test_entity_autocomplete.py`)

**Context:** The entities table already has a trigram GIN index on `entity_text`. We need a fast autocomplete endpoint that searches across all entities corpus-wide.

**Step 1: Write the failing test**

Create `tests/test_entity_autocomplete.py`:

```python
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_entity_autocomplete_returns_matches(async_client: AsyncClient, auth_headers: dict):
    """Entity autocomplete should return matching entities with counts."""
    resp = await async_client.get(
        "/api/docs/entities/autocomplete",
        params={"q": "test", "limit": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # Each item should have entity_text, entity_type, doc_count
    if data:
        assert "entity_text" in data[0]
        assert "entity_type" in data[0]
        assert "doc_count" in data[0]


@pytest.mark.asyncio
async def test_entity_autocomplete_requires_min_chars(async_client: AsyncClient, auth_headers: dict):
    """Should return empty for queries shorter than 2 chars."""
    resp = await async_client.get(
        "/api/docs/entities/autocomplete",
        params={"q": "a"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []
```

**Step 2: Implement the endpoint**

In `src/harbor_clerk/api/routes/documents.py`, add:

```python
@router.get("/docs/entities/autocomplete")
async def entity_autocomplete(
    q: str = Query(min_length=1, max_length=100),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Fast entity autocomplete with trigram matching across all active documents."""
    if len(q) < 2:
        return []

    filters = [
        Document.status == "active",
        Document.latest_version_id == Entity.version_id,
        Entity.entity_text.ilike(f"%{q}%"),
    ]
    if entity_type:
        filters.append(Entity.entity_type == entity_type)

    result = await session.execute(
        select(
            Entity.entity_text,
            Entity.entity_type,
            func.count(func.distinct(Entity.doc_id)).label("doc_count"),
        )
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(*filters)
        .group_by(Entity.entity_text, Entity.entity_type)
        .order_by(func.count(func.distinct(Entity.doc_id)).desc())
        .limit(limit)
    )
    rows = result.all()
    return [
        {"entity_text": r.entity_text, "entity_type": r.entity_type, "doc_count": r.doc_count}
        for r in rows
    ]
```

**Step 3: Run test**

```bash
uv run pytest tests/test_entity_autocomplete.py -v
```

**Step 4: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/routes/documents.py
git add -A
git commit -m "feat: entity autocomplete endpoint with trigram search"
```

---

## Task 4: Enhanced Document Listing — Filters & Sort

**Files:**
- Modify: `src/harbor_clerk/api/routes/documents.py` (the `list_documents` endpoint)
- Modify: `src/harbor_clerk/api/schemas/documents.py`

**Context:** Currently `GET /api/docs` supports only `q` (title filter), `limit`, and `offset`. We need to add: `entity` (filter by entity text), `entity_type`, `mime_type`, `language`, `doc_type`, `sort` (field), `sort_dir` (asc/desc).

**Step 1: Add new query parameters to list_documents**

In `src/harbor_clerk/api/routes/documents.py`, modify `list_documents`:

```python
@router.get("/docs", response_model=PaginatedDocuments)
async def list_documents(
    limit: int = Query(default=50, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
    entity: str | None = Query(default=None, description="Filter by entity text (ILIKE)"),
    entity_type: str | None = Query(default=None, description="Filter by entity type (PERSON, ORG, GPE, etc.)"),
    mime_type: str | None = Query(default=None, description="Filter by MIME type"),
    language: str | None = Query(default=None, description="Filter by dominant language (en, fr, etc.)"),
    doc_type: str | None = Query(default=None, description="Filter by document type classification"),
    sort: str = Query(default="updated", regex="^(updated|created|title)$"),
    sort_dir: str = Query(default="desc", regex="^(asc|desc)$"),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
```

**Step 2: Build dynamic query with filters**

Replace the existing query building with:

```python
    base = select(Document).where(Document.status == "active")

    if q:
        pattern = f"%{q}%"
        base = base.where(
            or_(Document.title.ilike(pattern), Document.canonical_filename.ilike(pattern))
        )

    # Join to latest version for version-based filters
    if any([mime_type, doc_type, language]):
        base = base.join(
            DocumentVersion,
            Document.latest_version_id == DocumentVersion.version_id,
        )
        if mime_type:
            base = base.where(DocumentVersion.mime_type == mime_type)
        if doc_type:
            base = base.where(DocumentVersion.doc_type == doc_type)
        if language:
            # Dominant language: most chunks in that language
            lang_subq = (
                select(Chunk.doc_id)
                .where(Chunk.language == language)
                .group_by(Chunk.doc_id)
                .subquery()
            )
            base = base.where(Document.doc_id.in_(select(lang_subq.c.doc_id)))

    if entity:
        entity_subq = select(Entity.doc_id).where(
            Entity.entity_text.ilike(f"%{entity}%")
        )
        if entity_type:
            entity_subq = entity_subq.where(Entity.entity_type == entity_type)
        entity_subq = entity_subq.group_by(Entity.doc_id).subquery()
        base = base.where(Document.doc_id.in_(select(entity_subq.c.doc_id)))

    # Sorting
    sort_column = {
        "updated": Document.updated_at,
        "created": Document.created_at,
        "title": Document.title,
    }[sort]
    order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    base = base.order_by(order)
```

**Step 3: Add filter metadata endpoint for available values**

Add a new endpoint to provide filter options:

```python
@router.get("/docs/filters")
async def document_filters(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return available filter values for the documents list."""
    # MIME types
    mime_rows = (await session.execute(
        select(DocumentVersion.mime_type, func.count())
        .join(Document, Document.latest_version_id == DocumentVersion.version_id)
        .where(Document.status == "active", DocumentVersion.mime_type.isnot(None))
        .group_by(DocumentVersion.mime_type)
        .order_by(func.count().desc())
    )).all()

    # Doc types
    doc_type_rows = (await session.execute(
        select(DocumentVersion.doc_type, func.count())
        .join(Document, Document.latest_version_id == DocumentVersion.version_id)
        .where(Document.status == "active", DocumentVersion.doc_type.isnot(None))
        .group_by(DocumentVersion.doc_type)
        .order_by(func.count().desc())
    )).all()

    # Languages (dominant per document approximated by most common chunk language)
    lang_rows = (await session.execute(
        select(Chunk.language, func.count(func.distinct(Chunk.doc_id)))
        .join(Document, Document.doc_id == Chunk.doc_id)
        .where(Document.status == "active", Chunk.language.isnot(None))
        .group_by(Chunk.language)
        .order_by(func.count(func.distinct(Chunk.doc_id)).desc())
    )).all()

    # Entity types
    ent_type_rows = (await session.execute(
        select(Entity.entity_type, func.count(func.distinct(Entity.doc_id)))
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(Document.status == "active")
        .group_by(Entity.entity_type)
        .order_by(func.count(func.distinct(Entity.doc_id)).desc())
    )).all()

    return {
        "mime_types": [{"value": r[0], "count": r[1]} for r in mime_rows],
        "doc_types": [{"value": r[0], "count": r[1]} for r in doc_type_rows],
        "languages": [{"value": r[0], "count": r[1]} for r in lang_rows],
        "entity_types": [{"value": r[0], "count": r[1]} for r in ent_type_rows],
    }
```

**Step 4: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/routes/documents.py
uv run ruff format src/harbor_clerk/api/routes/documents.py
git add -A
git commit -m "feat: document list filters (entity, mime, language, doc_type, sort)"
```

---

## Task 5: Frontend — Documents Page Filters & Sort

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`
- Modify: `frontend/src/api.ts` (if needed for new query params)

**Context:** Add a filter bar above the documents table with: entity autocomplete input, file type dropdown, language dropdown, doc type dropdown, sort controls. Load filter options from `GET /api/docs/filters` on mount.

**Step 1: Add filter state and load filter options**

At the top of `DocumentsPage`, add state for each filter:

```typescript
const [filters, setFilters] = useState<{
  mime_types: { value: string; count: number }[]
  doc_types: { value: string; count: number }[]
  languages: { value: string; count: number }[]
  entity_types: { value: string; count: number }[]
}>({ mime_types: [], doc_types: [], languages: [], entity_types: [] })

const [mimeFilter, setMimeFilter] = useState('')
const [langFilter, setLangFilter] = useState('')
const [docTypeFilter, setDocTypeFilter] = useState('')
const [entityFilter, setEntityFilter] = useState('')
const [entityTypeFilter, setEntityTypeFilter] = useState('')
const [sortField, setSortField] = useState<'updated' | 'created' | 'title'>('updated')
const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

// Entity autocomplete
const [entityQuery, setEntityQuery] = useState('')
const [entitySuggestions, setEntitySuggestions] = useState<{ entity_text: string; entity_type: string; doc_count: number }[]>([])
```

**Step 2: Fetch filter options on mount**

```typescript
useEffect(() => {
  get<typeof filters>('/api/docs/filters').then(setFilters).catch(() => {})
}, [])
```

**Step 3: Wire filters into the document fetch**

Update the `loadDocuments` function to pass filter params:

```typescript
const params = new URLSearchParams()
params.set('limit', String(pageSize))
params.set('offset', String(page * pageSize))
params.set('sort', sortField)
params.set('sort_dir', sortDir)
if (searchQuery) params.set('q', searchQuery)
if (mimeFilter) params.set('mime_type', mimeFilter)
if (langFilter) params.set('language', langFilter)
if (docTypeFilter) params.set('doc_type', docTypeFilter)
if (entityFilter) params.set('entity', entityFilter)
if (entityTypeFilter) params.set('entity_type', entityTypeFilter)

const data = await get<PaginatedDocs>(`/api/docs?${params}`)
```

**Step 4: Add entity autocomplete input**

```typescript
// Debounced entity search
useEffect(() => {
  if (entityQuery.length < 2) { setEntitySuggestions([]); return }
  const timer = setTimeout(async () => {
    const params = new URLSearchParams({ q: entityQuery, limit: '10' })
    if (entityTypeFilter) params.set('entity_type', entityTypeFilter)
    const results = await get<typeof entitySuggestions>(`/api/docs/entities/autocomplete?${params}`)
    setEntitySuggestions(results)
  }, 200)
  return () => clearTimeout(timer)
}, [entityQuery, entityTypeFilter])
```

**Step 5: Build the filter bar UI**

Add a collapsible filter bar between the search input and the table. Include:
- Entity search input with dropdown autocomplete suggestions
- File type `<select>` populated from `filters.mime_types`
- Language `<select>` populated from `filters.languages`
- Doc type `<select>` populated from `filters.doc_types`
- Sort toggle buttons (Updated / Created / Name) with asc/desc arrow
- "Clear filters" button when any filter is active

Use the existing Tailwind design tokens. Keep the filter bar compact (single row, small dropdowns).

**Step 6: Add doc_type column to the table**

Add a "Type" column after the title column showing `doc.doc_type` as a small badge.

**Step 7: Lint and commit**

```bash
cd frontend && npm run lint && npm run type-check && npm run format:check
git add -A
git commit -m "feat: document filters (entity, type, language) and sort controls"
```

---

## Task 6: Frontend — Related Documents Sidebar

**Files:**
- Modify: `frontend/src/pages/DocumentDetailPage.tsx`

**Context:** The API endpoint `GET /api/docs/{id}/related` already exists and returns related documents with similarity scores. We just need to call it and render a sidebar panel.

**Step 1: Add state and fetch related documents**

```typescript
const [related, setRelated] = useState<{ doc_id: string; title: string; summary: string | null; similarity: number }[]>([])

useEffect(() => {
  if (!id) return
  get<{ related: typeof related }>(`/api/docs/${id}/related?k=5`)
    .then(data => setRelated(data.related))
    .catch(() => {})
}, [id])
```

**Step 2: Render the related documents panel**

Add a "Related Documents" disclosure section (consistent with existing disclosure sections on the page) after the document stats section:

```tsx
{related.length > 0 && (
  <details className="group rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac" open>
    <summary className="cursor-pointer px-5 py-3 text-sm font-semibold">
      Related Documents ({related.length})
    </summary>
    <div className="border-t border-gray-200 dark:border-gray-700 px-5 py-3 space-y-2">
      {related.map(r => (
        <Link
          key={r.doc_id}
          to={`/docs/${r.doc_id}`}
          className="block rounded-lg px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
        >
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
              {r.title}
            </span>
            <span className="ml-2 text-xs text-gray-400 tabular-nums">
              {Math.round(r.similarity * 100)}%
            </span>
          </div>
          {r.summary && (
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">
              {r.summary}
            </p>
          )}
        </Link>
      ))}
    </div>
  </details>
)}
```

**Step 3: Lint and commit**

```bash
cd frontend && npm run lint && npm run type-check
git add -A
git commit -m "feat: related documents sidebar on document detail page"
```

---

## Task 7: API — Topic Clusters with Auto-Generated Names

**Files:**
- Modify: `src/harbor_clerk/api/routes/stats.py`
- Modify: `src/harbor_clerk/api/schemas/stats.py` (if separate)

**Context:** The existing `/api/stats/clusters` endpoint returns per-document centroids. We add a new endpoint that clusters these centroids server-side and names each cluster using the most common entities/doc_types in its members.

**Step 1: Implement the topic clusters endpoint**

In `src/harbor_clerk/api/routes/stats.py`, add:

```python
@router.get("/stats/topics")
async def topic_clusters(
    k: int = Query(default=0, ge=0, le=30, description="Number of clusters (0=auto)"),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Cluster documents by embedding similarity and auto-name each cluster."""
    import numpy as np

    # Fetch centroids (same query as /stats/clusters)
    rows = (await session.execute(
        select(
            Chunk.doc_id,
            Document.title,
            DocumentVersion.mime_type,
            DocumentVersion.doc_type,
            func.avg(Chunk.embedding).cast(Text).label("centroid"),
        )
        .join(Document, Document.doc_id == Chunk.doc_id)
        .join(DocumentVersion, Document.latest_version_id == DocumentVersion.version_id)
        .where(Document.status == "active", Chunk.embedding.isnot(None))
        .group_by(Chunk.doc_id, Document.title, DocumentVersion.mime_type, DocumentVersion.doc_type)
    )).all()

    if len(rows) < 3:
        return {"clusters": [], "doc_count": len(rows)}

    doc_ids = [str(r[0]) for r in rows]
    titles = [r[1] for r in rows]
    mime_types = [r[2] for r in rows]
    doc_types = [r[3] for r in rows]
    centroids = np.array([[float(x) for x in r[4].strip("[]").split(",")] for r in rows])

    # Auto-select k if not specified: sqrt(n) clamped to 3-15
    n = len(rows)
    if k == 0:
        k = max(3, min(15, int(n ** 0.5)))
    k = min(k, n)

    # Simple k-means (numpy only, no sklearn dependency)
    labels = _kmeans(centroids, k, max_iter=50)

    # Build clusters
    from collections import Counter
    clusters = {}
    for i, label in enumerate(labels):
        if label not in clusters:
            clusters[label] = {"doc_ids": [], "titles": [], "doc_types": [], "mime_types": []}
        clusters[label]["doc_ids"].append(doc_ids[i])
        clusters[label]["titles"].append(titles[i])
        clusters[label]["doc_types"].append(doc_types[i])
        clusters[label]["mime_types"].append(mime_types[i])

    # Name each cluster from most common doc_type, or most common title words
    result = []
    for label, info in sorted(clusters.items()):
        # Try doc_type first
        type_counts = Counter(t for t in info["doc_types"] if t)
        if type_counts:
            name = type_counts.most_common(1)[0][0]
        else:
            # Fall back to most common significant words in titles
            words = []
            for t in info["titles"]:
                words.extend(w for w in t.lower().split() if len(w) > 3)
            word_counts = Counter(words)
            name = word_counts.most_common(1)[0][0].title() if word_counts else f"Group {label + 1}"

        result.append({
            "cluster_id": label,
            "name": name,
            "doc_count": len(info["doc_ids"]),
            "doc_ids": info["doc_ids"],
            "sample_titles": info["titles"][:5],
        })

    # Sort by size descending
    result.sort(key=lambda c: c["doc_count"], reverse=True)
    return {"clusters": result, "doc_count": n}


def _kmeans(data: "np.ndarray", k: int, max_iter: int = 50) -> list[int]:
    """Simple k-means clustering using numpy. Returns list of cluster labels."""
    import numpy as np

    n = len(data)
    # Initialize with k-means++ style: pick first centroid randomly, rest by distance
    rng = np.random.default_rng(42)
    indices = [rng.integers(n)]
    for _ in range(1, k):
        dists = np.min([np.sum((data - data[i]) ** 2, axis=1) for i in indices], axis=0)
        probs = dists / dists.sum()
        indices.append(rng.choice(n, p=probs))
    centroids = data[indices].copy()

    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        # Assign
        dists = np.stack([np.sum((data - c) ** 2, axis=1) for c in centroids], axis=1)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        # Update centroids
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = data[mask].mean(axis=0)

    return labels.tolist()
```

**Step 2: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/routes/stats.py
uv run ruff format src/harbor_clerk/api/routes/stats.py
git add -A
git commit -m "feat: topic clusters endpoint with auto-naming"
```

---

## Task 8: Frontend — Explore Page

**Files:**
- Create: `frontend/src/pages/ExplorePage.tsx`
- Modify: `frontend/src/App.tsx` (add route)
- Modify: `frontend/src/components/Layout.tsx` (add tab)

**Context:** The Explore page has three sections:
1. **Entity Folders** — browse by People, Places, Organizations (from entity index)
2. **Topic Clusters** — auto-named groups from embeddings
3. **Timeline** — document density over time, clickable to filter

**Step 1: Create the Explore page component**

Create `frontend/src/pages/ExplorePage.tsx`:

The page fetches three data sources:
- `GET /api/docs/filters` — for entity type counts
- `GET /api/docs/entities/autocomplete?entity_type=PERSON&q=&limit=50` — for each entity type (top entities)
- `GET /api/stats/topics` — topic clusters

**Entity Folders Section:**
- Three expandable cards: People (PERSON), Places (GPE/LOC), Organizations (ORG)
- Each shows top entities with doc counts, clickable to navigate to `/docs?entity=<name>&entity_type=<type>`
- "Show all" link at bottom of each card

**Topic Clusters Section:**
- Grid of cards, one per cluster
- Each card shows: cluster name, doc count, 3-5 sample titles
- Click navigates to `/docs?doc_type=<cluster_name>` or a cluster-specific filter

**Timeline Section:**
- Horizontal bar chart or simple histogram showing document count by month
- Use `GET /api/stats` (existing endpoint) which returns date-range info, or add a simple aggregation endpoint
- Each bar is clickable to filter docs by date range

**Step 2: Add a timeline data endpoint (if needed)**

In `src/harbor_clerk/api/routes/stats.py`, add:

```python
@router.get("/stats/timeline")
async def document_timeline(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Document count by month for timeline visualization."""
    rows = (await session.execute(
        select(
            func.date_trunc("month", Document.created_at).label("month"),
            func.count().label("count"),
        )
        .where(Document.status == "active")
        .group_by(func.date_trunc("month", Document.created_at))
        .order_by(func.date_trunc("month", Document.created_at))
    )).all()
    return [{"month": r[0].isoformat(), "count": r[1]} for r in rows]
```

**Step 3: Add a top-entities endpoint for each type**

In `src/harbor_clerk/api/routes/documents.py`, add (or reuse autocomplete with empty q):

```python
@router.get("/docs/entities/top")
async def top_entities(
    entity_type: str = Query(...),
    limit: int = Query(default=30, ge=1, le=100),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Top entities of a given type across active documents."""
    result = await session.execute(
        select(
            Entity.entity_text,
            func.count(func.distinct(Entity.doc_id)).label("doc_count"),
        )
        .join(Document, Document.doc_id == Entity.doc_id)
        .where(Document.status == "active", Entity.entity_type == entity_type)
        .group_by(Entity.entity_text)
        .order_by(func.count(func.distinct(Entity.doc_id)).desc())
        .limit(limit)
    )
    return [{"entity_text": r[0], "doc_count": r[1]} for r in result.all()]
```

**Step 4: Build the Explore page UI**

Key design decisions:
- Use the project's existing Tailwind design tokens and card styles
- Entity folders use `<details>` disclosure elements (consistent with rest of app)
- Topic clusters in a responsive grid
- Timeline as a simple horizontal bar chart (pure CSS/SVG, no charting library needed — or use recharts which is already installed)
- All clickable elements navigate to the Documents page with appropriate filters pre-applied

**Step 5: Add route and tab**

In `frontend/src/App.tsx`, add inside the Layout routes:
```tsx
<Route path="/explore" element={<ExplorePage />} />
```

In `frontend/src/components/Layout.tsx`, add the tab between Documents and Raw Search:
```tsx
<TabLink to="/explore">Explore</TabLink>
```

**Step 6: Lint, type-check, format**

```bash
cd frontend && npm run lint && npm run type-check && npm run format:check
```

If format issues: `npx prettier --write src/pages/ExplorePage.tsx`

**Step 7: Commit**

```bash
git add -A
git commit -m "feat: Explore page with entity folders, topic clusters, and timeline"
```

---

## Task 9: Final Integration & Polish

**Step 1: Run full backend test suite**

```bash
uv run pytest tests/ -v
```

**Step 2: Run full frontend checks**

```bash
cd frontend && npm run lint && npm run type-check && npm run format:check
```

**Step 3: Build macOS apps to verify compilation**

```bash
cd macos && make apps
```

**Step 4: Manual smoke test checklist**

- [ ] Documents page: filter by entity (with autocomplete), file type, language, doc type
- [ ] Documents page: sort by name ascending, date descending
- [ ] Documents page: clear all filters
- [ ] Document detail: related documents section shows with similarity scores
- [ ] Explore page: entity folders expand and show top entities with counts
- [ ] Explore page: clicking an entity navigates to filtered documents
- [ ] Explore page: topic clusters display with names and sample titles
- [ ] Explore page: timeline shows document density by month
- [ ] Explore page: clicking a timeline bar filters documents
- [ ] New document upload: summarize stage populates doc_type

**Step 5: Final commit and PR**

```bash
git add -A
git commit -m "chore: integration polish and manual test fixes"
gh pr create --title "feat: Explore view, document filters, and related documents" --body "..."
```

---

## Future Work (Deferred)

### Auto-Generated Topic Hierarchy (6.B)

**Concept:** Take topic clusters from embeddings, ask the LLM to organize cluster names into a 2-3 level hierarchy (e.g., Legal/Contracts, Finance/Tax Returns). Store as JSON tree in a new table. Run as batch job on demand or on schedule.

**Challenges:**
- Batch job UX for non-technical users — needs careful design (auto-run? button? schedule?)
- Hierarchy stability — clusters shift as corpus grows, hierarchy needs graceful updates
- LLM dependency — requires active model for naming

**When to build:** After user feedback on the flat topic clusters in Explore. If users find the flat list insufficient for large corpora (100+ documents), the hierarchy becomes valuable.

---

## File Summary

| File | Changes |
|---|---|
| `alembic/versions/0002_add_doc_type.py` | New migration: add `doc_type` column |
| `src/harbor_clerk/models/document_version.py` | Add `doc_type` mapped column |
| `src/harbor_clerk/api/schemas/documents.py` | Add `doc_type` to `DocumentSummary` |
| `src/harbor_clerk/llm/summarize.py` | Add `classify_doc_type()`, `_mime_to_doc_type()` |
| `src/harbor_clerk/worker/stages/summarize.py` | Call `classify_doc_type()` after summary |
| `src/harbor_clerk/api/routes/documents.py` | Enhanced list (filters/sort), entity autocomplete, top entities |
| `src/harbor_clerk/api/routes/stats.py` | Topic clusters endpoint, timeline endpoint, `_kmeans()` |
| `frontend/src/pages/DocumentsPage.tsx` | Filter bar, sort controls, doc_type column |
| `frontend/src/pages/DocumentDetailPage.tsx` | Related documents disclosure section |
| `frontend/src/pages/ExplorePage.tsx` | New: entity folders, topic clusters, timeline |
| `frontend/src/App.tsx` | Add `/explore` route |
| `frontend/src/components/Layout.tsx` | Add Explore tab |
| `tests/test_summarize.py` | Tests for `classify_doc_type`, `_mime_to_doc_type` |
| `tests/test_entity_autocomplete.py` | Tests for autocomplete endpoint |
