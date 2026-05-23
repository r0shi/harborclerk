# Phase 4: Wikilink Graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Obsidian-style `[[wikilinks]]` from Markdown docs into a new `document_links` table, resolve them at finalize-time against the corpus, and blend the resulting link graph into `kb_find_related` so explicitly-linked notes surface as related (alongside embedding-similar ones).

**Architecture:** A new `DocumentLink` model + alembic migration `0002` adds the graph table. `extract_markdown` (extended) parses `[[…]]` patterns from the body BEFORE normalization rewrites them, returning structured `wikilinks` on its result. The extract stage writes one `DocumentLink` row per link with `target_doc_id=NULL, resolved=False`. The finalize stage runs the resolver (line-anchored name match against `canonical_filename` stem or `documents.title`) for both this doc's outgoing links AND any dangling links pointing AT this doc (re-resolution as the corpus grows). `kb_find_related` prepends explicitly-linked docs (similarity 1.0) to the embedding-derived results, then dedups.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (existing model conventions), alembic (existing migration pattern), pytest. No new third-party dependencies.

**Working directory:** the `feat/markdown-text-handling` worktree. All paths below are relative to its root. Phase 3 must be committed before starting.

**Spec:** `docs/superpowers/specs/2026-05-22-markdown-text-handling-design.md` (Phase 4).

**Future work (out of scope):** surface backlinks via MCP (a `kb_backlinks` tool or backlink data in `kb_get_document`) and a backlinks panel in the document-detail UI. The spec's Phase 4 only goes as deep as Graph + Retrieval; MCP/UI surfacing is deferred.

---

## File Structure

- **Create** `src/harbor_clerk/models/document_link.py` — the `DocumentLink` SQLAlchemy model.
- **Modify** `src/harbor_clerk/models/__init__.py` — export `DocumentLink`.
- **Create** `alembic/versions/0002_document_links.py` — the migration that creates the table + indexes.
- **Modify** `src/harbor_clerk/worker/markdown_extract.py` — add `parse_wikilinks(text)` helper; extend `MarkdownExtractResult` with a `wikilinks` field; have `extract_markdown` populate it BEFORE normalization runs.
- **Modify** `src/harbor_clerk/worker/stages/extract.py` — when `markdown_result` is non-None, delete existing `DocumentLink` rows for the doc and insert new ones from `markdown_result.wikilinks` (unresolved).
- **Modify** `src/harbor_clerk/worker/stages/finalize.py` — add `_resolve_link()` pure helper; in `run_finalize` build a name→doc_ids candidate map from active docs, resolve this doc's outgoing links, then resolve any dangling links pointing AT this doc.
- **Modify** `src/harbor_clerk/mcp_server.py` — extend `kb_find_related` to prepend explicitly-linked docs (similarity=1.0) ahead of embedding results, with dedup and the `k` cap respected.
- **Modify** `tests/test_markdown_extract.py` — unit tests for `parse_wikilinks` + the orchestrator's wikilinks field.
- **Modify** `tests/test_pipeline.py` — DB-backed integration test for extract → finalize → resolved links + `kb_find_related` blending.

Build order: Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9. The migration (Task 1) must run before any DB-backed test in later tasks.

---

### Task 1: `DocumentLink` model + alembic migration `0002`

**Files:**
- Create: `src/harbor_clerk/models/document_link.py`
- Modify: `src/harbor_clerk/models/__init__.py`
- Create: `alembic/versions/0002_document_links.py`
- Test: `tests/test_document_link_model.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_document_link_model.py`:

```python
"""Tests for the DocumentLink model and the 0002 migration."""

import uuid

from sqlalchemy import select

from harbor_clerk.models import Document, DocumentLink
from harbor_clerk.models.enums import PipelineStatus


def test_document_link_model_importable():
    """DocumentLink must be importable from harbor_clerk.models."""
    assert DocumentLink.__tablename__ == "document_links"


def test_document_link_required_fields(sync_session):
    """A minimal DocumentLink can be inserted with just src_doc_id + link_text + target_title."""
    doc = Document(
        title="src note",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add(doc)
    sync_session.flush()

    link = DocumentLink(
        src_doc_id=doc.doc_id,
        link_text="Target Note",
        target_title="target note",
    )
    sync_session.add(link)
    sync_session.commit()

    row = sync_session.execute(
        select(DocumentLink).where(DocumentLink.src_doc_id == doc.doc_id)
    ).scalar_one()
    assert row.link_text == "Target Note"
    assert row.target_title == "target note"
    assert row.target_doc_id is None
    assert row.anchor is None
    assert row.alias is None
    assert row.resolved is False
    assert row.created_at is not None
    assert isinstance(row.link_id, uuid.UUID)


def test_document_link_optional_fields(sync_session):
    """anchor, alias, target_doc_id, resolved can all be populated."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    tgt = Document(
        title="tgt",
        status="active",
        sha256=b"\x02" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add_all([src, tgt])
    sync_session.flush()

    link = DocumentLink(
        src_doc_id=src.doc_id,
        target_doc_id=tgt.doc_id,
        link_text="Target Note#Section|Alias",
        target_title="target note",
        anchor="Section",
        alias="Alias",
        resolved=True,
    )
    sync_session.add(link)
    sync_session.commit()

    row = sync_session.execute(select(DocumentLink).where(DocumentLink.link_id == link.link_id)).scalar_one()
    assert row.target_doc_id == tgt.doc_id
    assert row.anchor == "Section"
    assert row.alias == "Alias"
    assert row.resolved is True


def test_document_link_src_cascade_deletes_links(sync_session):
    """Deleting the source document cascades to its outgoing links."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x03" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add(src)
    sync_session.flush()
    sync_session.add(DocumentLink(src_doc_id=src.doc_id, link_text="x", target_title="x"))
    sync_session.commit()

    sync_session.delete(src)
    sync_session.commit()

    rows = sync_session.execute(select(DocumentLink)).scalars().all()
    assert rows == []


def test_document_link_target_set_null_on_target_delete(sync_session):
    """Deleting the target document sets target_doc_id NULL on incoming links
    (NOT CASCADE — links remain so the graph remembers the broken reference)."""
    src = Document(
        title="src",
        status="active",
        sha256=b"\x04" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    tgt = Document(
        title="tgt",
        status="active",
        sha256=b"\x05" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    sync_session.add_all([src, tgt])
    sync_session.flush()
    sync_session.add(
        DocumentLink(
            src_doc_id=src.doc_id,
            target_doc_id=tgt.doc_id,
            link_text="tgt",
            target_title="tgt",
            resolved=True,
        )
    )
    sync_session.commit()

    sync_session.delete(tgt)
    sync_session.commit()

    row = sync_session.execute(select(DocumentLink)).scalar_one()
    assert row.target_doc_id is None
    # `resolved` stays True historically — the resolver doesn't undo itself.
    # Re-resolution happens only when a new doc finalizes that matches.
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_document_link_model.py -v`
Expected: `ImportError: cannot import name 'DocumentLink'` (or table doesn't exist).

- [ ] **Step 3: Create the model**

Create `src/harbor_clerk/models/document_link.py`:

```python
"""DocumentLink — a parsed wikilink from one Document to another (or to a
target that doesn't exist yet).

Populated by the extract stage from Markdown ``[[…]]`` patterns. Resolved
by the finalize stage against the active corpus.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, uuid_pk


class DocumentLink(Base):
    __tablename__ = "document_links"

    link_id: Mapped[uuid_pk]

    # Source — the document whose body contained the [[…]]. Outgoing links
    # for src_doc_id are deleted when the source is deleted (CASCADE).
    src_doc_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Target — the document the link resolved to, or NULL if still unresolved.
    # SET NULL on target delete (not CASCADE) so the graph remembers the
    # broken reference; a future doc with the same name can re-resolve it.
    target_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The raw inner text of the [[…]] match, exactly as it appeared.
    link_text: Mapped[str] = mapped_column(Text, nullable=False)

    # The parsed target name (before `#` and `|`), lowercased + stripped.
    # Indexed for re-resolution lookups when a new doc finalizes.
    target_title: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Optional heading anchor (`#Section` portion).
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional display alias (`|Alias` portion).
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True once finalize has identified a unique target. Stays True after
    # the resolver runs even if the target is later deleted (target_doc_id
    # would become NULL via the SET NULL FK).
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
```

- [ ] **Step 4: Export from `models/__init__.py`**

Open `src/harbor_clerk/models/__init__.py`. Add an import line for `DocumentLink` (alongside the existing `Document`, `DocumentHeading`, `DocumentPage`, etc.) and add it to the `__all__` list if one is present. The exact form depends on the existing file structure — read it first and follow the established convention. The minimum is making `from harbor_clerk.models import DocumentLink` work.

- [ ] **Step 5: Create the migration**

Create `alembic/versions/0002_document_links.py`:

```python
"""document_links

Adds the document_links table for Phase 4 of the markdown-handling feature.
Each row is a parsed wikilink ([[Note Name]]) from one document to another
(or to a target that doesn't exist yet — target_doc_id is nullable).

Revision ID: 0002_document_links
Revises: 0001_initial
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_document_links"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_links",
        sa.Column("link_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("src_doc_id", sa.UUID(), nullable=False),
        sa.Column("target_doc_id", sa.UUID(), nullable=True),
        sa.Column("link_text", sa.Text(), nullable=False),
        sa.Column("target_title", sa.Text(), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["src_doc_id"], ["documents.doc_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_doc_id"], ["documents.doc_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("link_id"),
    )
    op.create_index("ix_document_links_src_doc_id", "document_links", ["src_doc_id"])
    op.create_index("ix_document_links_target_doc_id", "document_links", ["target_doc_id"])
    op.create_index("ix_document_links_target_title", "document_links", ["target_title"])


def downgrade() -> None:
    op.drop_index("ix_document_links_target_title", table_name="document_links")
    op.drop_index("ix_document_links_target_doc_id", table_name="document_links")
    op.drop_index("ix_document_links_src_doc_id", table_name="document_links")
    op.drop_table("document_links")
```

- [ ] **Step 6: Run migration + tests**

The test fixture (`conftest.py`) runs `alembic upgrade head` against the test DB before tests, so simply running the test suite applies the new migration. If you want to verify manually first:

Run: `uv run alembic upgrade head` (in the worktree root) — confirm it applies cleanly.

Then run: `uv run pytest tests/test_document_link_model.py -v`
Expected: PASS (5 tests).

Run `uv run ruff check src/harbor_clerk/models/document_link.py src/harbor_clerk/models/__init__.py alembic/versions/0002_document_links.py tests/test_document_link_model.py` — confirm clean.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/models/document_link.py src/harbor_clerk/models/__init__.py alembic/versions/0002_document_links.py tests/test_document_link_model.py
git commit -m "feat(model): DocumentLink + 0002 migration for wikilink graph"
```

---

### Task 2: `parse_wikilinks` helper

A pure helper in `markdown_extract.py` that scans Markdown body text for `[[…]]` patterns and returns each as a structured dict. Supports plain `[[Note]]`, anchor `[[Note#Section]]`, alias `[[Note|Alias]]`, and combined `[[Note#Section|Alias]]`.

**Files:**
- Modify: `src/harbor_clerk/worker/markdown_extract.py`
- Modify: `tests/test_markdown_extract.py`

- [ ] **Step 1: Write the failing tests**

Extend the import in `tests/test_markdown_extract.py` to include `parse_wikilinks`, then append:

```python
from harbor_clerk.worker.markdown_extract import parse_wikilinks


# --- parse_wikilinks ---


def test_parse_wikilinks_empty():
    assert parse_wikilinks("") == []
    assert parse_wikilinks("No wikilinks here.") == []


def test_parse_wikilinks_plain():
    out = parse_wikilinks("See [[Note Name]] for details.")
    assert len(out) == 1
    assert out[0]["link_text"] == "Note Name"
    assert out[0]["target_title"] == "note name"  # lowercased, stripped
    assert out[0]["anchor"] is None
    assert out[0]["alias"] is None


def test_parse_wikilinks_with_anchor():
    out = parse_wikilinks("See [[Note#Section]].")
    assert len(out) == 1
    assert out[0]["target_title"] == "note"
    assert out[0]["anchor"] == "Section"
    assert out[0]["alias"] is None


def test_parse_wikilinks_with_alias():
    out = parse_wikilinks("See [[Note|the note]].")
    assert len(out) == 1
    assert out[0]["target_title"] == "note"
    assert out[0]["alias"] == "the note"
    assert out[0]["anchor"] is None


def test_parse_wikilinks_anchor_and_alias():
    out = parse_wikilinks("See [[Note#Section|Alias]].")
    assert len(out) == 1
    assert out[0]["target_title"] == "note"
    assert out[0]["anchor"] == "Section"
    assert out[0]["alias"] == "Alias"


def test_parse_wikilinks_multiple():
    text = "See [[A]] and [[B|alt]] and [[C#anchor]]."
    out = parse_wikilinks(text)
    assert len(out) == 3
    assert [w["target_title"] for w in out] == ["a", "b", "c"]


def test_parse_wikilinks_target_title_trims_whitespace():
    out = parse_wikilinks("[[  Note Name  ]]")
    assert out[0]["target_title"] == "note name"
    assert out[0]["link_text"] == "  Note Name  "  # link_text preserves the raw inner text


def test_parse_wikilinks_skips_brokens():
    """Unbalanced brackets or empty targets are not matched."""
    assert parse_wikilinks("[[]]") == []
    assert parse_wikilinks("[[ | only alias ]]") == []
    assert parse_wikilinks("text [[broken") == []
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_markdown_extract.py -k parse_wikilinks -v`
Expected: `ImportError: cannot import name 'parse_wikilinks'`.

- [ ] **Step 3: Implement `parse_wikilinks`**

Add to `src/harbor_clerk/worker/markdown_extract.py` (next to `normalize_markdown`):

```python
# Wikilink capture pattern: matches [[Target]], [[Target#Anchor]],
# [[Target|Alias]], and [[Target#Anchor|Alias]].
# Group 1 = target (before # or |), Group 2 = anchor (optional),
# Group 3 = alias (optional).
_WIKILINK_CAPTURE_RE = re.compile(
    r"\[\["
    r"([^\[\]|#]+?)"            # group 1: target
    r"(?:#([^\[\]|]+))?"        # group 2: anchor (optional)
    r"(?:\|([^\[\]]+))?"        # group 3: alias (optional)
    r"\]\]"
)


def parse_wikilinks(text: str) -> list[dict]:
    """Extract all ``[[…]]`` wikilinks from ``text``.

    Returns a list of dicts with these keys (one per match):

    - ``link_text``: the raw inner text exactly as it appeared (preserves
      whitespace, original casing, anchor + alias separators).
    - ``target_title``: the parsed target name (text before ``#`` or ``|``),
      lowercased and stripped, for case-insensitive matching at resolve time.
    - ``anchor``: the ``#Anchor`` portion as it appeared, or ``None``.
    - ``alias``: the ``|Alias`` portion as it appeared, or ``None``.

    Wikilinks with empty or whitespace-only targets are skipped.
    """
    if not text:
        return []

    results: list[dict] = []
    for match in _WIKILINK_CAPTURE_RE.finditer(text):
        target_raw = match.group(1)
        target_title = target_raw.strip().lower()
        if not target_title:
            continue
        # Reconstruct link_text from the raw groups (matches what was between [[ and ]])
        anchor = match.group(2)
        alias = match.group(3)
        link_text = target_raw
        if anchor is not None:
            link_text += "#" + anchor
        if alias is not None:
            link_text += "|" + alias
        results.append(
            {
                "link_text": link_text,
                "target_title": target_title,
                "anchor": anchor,
                "alias": alias,
            }
        )
    return results
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_markdown_extract.py -k parse_wikilinks -v`
Expected: PASS (8 tests).

Run `uv run ruff check src/harbor_clerk/worker/markdown_extract.py tests/test_markdown_extract.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/markdown_extract.py tests/test_markdown_extract.py
git commit -m "feat(markdown): parse_wikilinks helper for [[…]] capture"
```

---

### Task 3: `extract_markdown` returns wikilinks

Extend `MarkdownExtractResult` with a `wikilinks` field, and have `extract_markdown` call `parse_wikilinks` on the BODY (before normalization rewrites the `[[…]]` patterns).

**Files:**
- Modify: `src/harbor_clerk/worker/markdown_extract.py`
- Modify: `tests/test_markdown_extract.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_markdown_extract.py`:

```python
def test_extract_markdown_captures_wikilinks():
    """The orchestrator returns parsed wikilinks from the body."""
    data = b"---\ntitle: X\n---\nSee [[Note A]] and [[Note B|alt]].\n"
    result = extract_markdown(data)
    assert len(result.wikilinks) == 2
    assert {w["target_title"] for w in result.wikilinks} == {"note a", "note b"}


def test_extract_markdown_wikilinks_empty_when_none():
    data = b"# Heading\n\nNo links here.\n"
    result = extract_markdown(data)
    assert result.wikilinks == []


def test_extract_markdown_wikilinks_preserved_through_normalization():
    """Wikilinks are captured BEFORE normalization unwraps them, so the
    parsed list still has them even though the page text doesn't show [[…]]."""
    data = b"See [[Target Note|alias text]] in the body.\n"
    result = extract_markdown(data)
    page_text = result.pages[0][1]
    # The normalized body has the alias text, not the [[…]] markup.
    assert "alias text" in page_text
    assert "[[" not in page_text
    # The wikilink is still captured.
    assert len(result.wikilinks) == 1
    assert result.wikilinks[0]["target_title"] == "target note"
    assert result.wikilinks[0]["alias"] == "alias text"
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_markdown_extract.py -k extract_markdown_captures or extract_markdown_wikilinks -v`
Expected: FAIL — `MarkdownExtractResult` has no `wikilinks` attribute, or it does but is empty.

- [ ] **Step 3: Extend `MarkdownExtractResult` and `extract_markdown`**

In `src/harbor_clerk/worker/markdown_extract.py`:

(a) Extend the `MarkdownExtractResult` dataclass — add a `wikilinks: list[dict]` field:

Replace the existing dataclass:

```python
@dataclass
class MarkdownExtractResult:
    """Output of :func:`extract_markdown` — what ``run_extract`` consumes.

    - ``title``: the frontmatter ``title`` field, if present and a non-empty
      string. The caller updates ``documents.title`` from this.
    - ``pages``: ``[(page_num, text)]`` in the same shape as ``_extract_txt`` /
      ``_extract_via_tika`` return.
    - ``headings``: a list of dicts ``{"level", "title", "position", "page_num"}``
      in the same shape as the Tika heading flow's output.
    """

    title: str | None
    pages: list[tuple[int, str]]
    headings: list[dict]
```

with:

```python
@dataclass
class MarkdownExtractResult:
    """Output of :func:`extract_markdown` — what ``run_extract`` consumes.

    - ``title``: the frontmatter ``title`` field, if present and a non-empty
      string. The caller updates ``documents.title`` from this.
    - ``pages``: ``[(page_num, text)]`` in the same shape as ``_extract_txt`` /
      ``_extract_via_tika`` return.
    - ``headings``: a list of dicts ``{"level", "title", "position", "page_num"}``
      in the same shape as the Tika heading flow's output.
    - ``wikilinks``: a list of dicts ``{"link_text", "target_title", "anchor",
      "alias"}`` for each ``[[…]]`` found in the BODY (before normalization).
    """

    title: str | None
    pages: list[tuple[int, str]]
    headings: list[dict]
    wikilinks: list[dict]
```

(b) In `extract_markdown`, after computing `body` (the frontmatter-stripped text) and BEFORE the normalization runs, call `parse_wikilinks(body)`. Add this line right after the existing `raw_headings, fence_ranges = parse_markdown_structure(body)` line:

```python
    wikilinks = parse_wikilinks(body)
```

(c) Change the final return statement from:

```python
    return MarkdownExtractResult(title=title, pages=pages, headings=headings)
```

to:

```python
    return MarkdownExtractResult(title=title, pages=pages, headings=headings, wikilinks=wikilinks)
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_markdown_extract.py -v`
Expected: PASS — all prior tests still pass + 3 new = previous count + 3.

Run `uv run ruff check src/harbor_clerk/worker/markdown_extract.py tests/test_markdown_extract.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/markdown_extract.py tests/test_markdown_extract.py
git commit -m "feat(markdown): extract_markdown returns parsed wikilinks"
```

---

### Task 4: Extract stage writes `document_links` rows

When `markdown_result` is non-None, the extract stage deletes any existing `DocumentLink` rows for this doc (idempotency) and writes one row per captured wikilink with `target_doc_id=None, resolved=False`.

**Files:**
- Modify: `src/harbor_clerk/worker/stages/extract.py`

No new unit test in this task — the integration is verified by the end-to-end test in Task 8 plus the orchestrator's wikilinks-field test from Task 3.

- [ ] **Step 1: Update the import**

In `src/harbor_clerk/worker/stages/extract.py`, the existing models import is:

```python
from harbor_clerk.models import Document, DocumentHeading, DocumentPage
```

Extend it to include `DocumentLink`:

```python
from harbor_clerk.models import Document, DocumentHeading, DocumentLink, DocumentPage
```

- [ ] **Step 2: Add the link-write block in `run_extract`**

Inside `run_extract`, after the existing heading-writing block (the unified one that writes `DocumentHeading` rows from `markdown_result.headings` or Tika), and BEFORE the `# Determine if OCR is needed` block, add:

```python
        # Wikilinks: only Markdown extraction produces these. Delete existing
        # rows for the doc (idempotent on reprocess), then write one
        # unresolved row per captured link. Resolution runs in finalize.
        if markdown_result is not None:
            existing_links = (
                session.execute(select(DocumentLink).where(DocumentLink.src_doc_id == doc_id))
                .scalars()
                .all()
            )
            for link in existing_links:
                session.delete(link)
            session.flush()
            for w in markdown_result.wikilinks:
                session.add(
                    DocumentLink(
                        src_doc_id=doc_id,
                        link_text=w["link_text"],
                        target_title=w["target_title"],
                        anchor=w["anchor"],
                        alias=w["alias"],
                    )
                )
            if markdown_result.wikilinks:
                logger.info(
                    "Captured %d wikilinks for doc %s",
                    len(markdown_result.wikilinks),
                    doc_id,
                )
```

- [ ] **Step 3: Lint and run tests**

Run: `uv run ruff check src/harbor_clerk/worker/stages/extract.py` — clean.

Run: `uv run pytest tests/test_markdown_extract.py tests/test_pipeline.py tests/test_watch_pipeline.py -q`
Expected: PASS (no regression; the new behavior is exercised by the Phase 2 markdown integration tests which already use markdown docs — they should now ALSO be writing zero or more DocumentLink rows without error).

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/worker/stages/extract.py
git commit -m "feat(extract): write document_links rows for captured wikilinks"
```

---

### Task 5: `_resolve_link` pure helper

A pure resolution function in `worker/stages/finalize.py` that matches a `target_title` against a precomputed candidate map. Returns the unique resolved `doc_id` or `None` (no match OR ambiguous).

**Files:**
- Modify: `src/harbor_clerk/worker/stages/finalize.py`
- Create: `tests/test_finalize_resolve.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_finalize_resolve.py`:

```python
"""Tests for the wikilink resolver helper in finalize.py."""

import uuid

from harbor_clerk.worker.stages.finalize import _resolve_link


def test_resolve_link_empty_inputs():
    assert _resolve_link("", {}) is None
    assert _resolve_link("   ", {}) is None
    assert _resolve_link("anything", {}) is None


def test_resolve_link_unique_match():
    did = uuid.uuid4()
    candidates = {"my note": [did]}
    assert _resolve_link("My Note", candidates) == did


def test_resolve_link_case_insensitive():
    did = uuid.uuid4()
    candidates = {"my note": [did]}
    assert _resolve_link("MY NOTE", candidates) == did
    assert _resolve_link("  my note  ", candidates) == did


def test_resolve_link_no_match():
    candidates = {"other note": [uuid.uuid4()]}
    assert _resolve_link("My Note", candidates) is None


def test_resolve_link_ambiguous_returns_none():
    """Two docs with the same name → ambiguous → unresolved."""
    candidates = {"shared name": [uuid.uuid4(), uuid.uuid4()]}
    assert _resolve_link("Shared Name", candidates) is None
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_finalize_resolve.py -v`
Expected: `ImportError: cannot import name '_resolve_link'`.

- [ ] **Step 3: Implement `_resolve_link` in `finalize.py`**

Add to `src/harbor_clerk/worker/stages/finalize.py` (before `run_finalize`):

```python
def _resolve_link(target_title: str, candidates_by_name: dict[str, list[uuid.UUID]]) -> uuid.UUID | None:
    """Match a parsed wikilink target against the active corpus.

    ``target_title`` is the parsed name from a ``[[…]]`` capture (already
    lowercased + stripped by ``parse_wikilinks``, but we re-normalize
    defensively).

    ``candidates_by_name`` maps lowercase-stripped names to a list of doc_ids
    that match that name. A name is either a document's ``canonical_filename``
    stem (lowercased) or its ``title`` (lowercased). The caller builds this
    map from the active corpus.

    Returns the unique matching ``doc_id``, or ``None`` if no match or the
    name is ambiguous (two or more docs share it).
    """
    if not target_title:
        return None
    key = target_title.strip().lower()
    if not key:
        return None
    matches = candidates_by_name.get(key, [])
    if len(matches) == 1:
        return matches[0]
    return None
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/test_finalize_resolve.py -v`
Expected: PASS (5 tests).

Run `uv run ruff check src/harbor_clerk/worker/stages/finalize.py tests/test_finalize_resolve.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/worker/stages/finalize.py tests/test_finalize_resolve.py
git commit -m "feat(finalize): _resolve_link helper for wikilink target matching"
```

---

### Task 6: Resolution runs in `run_finalize`

Extend `run_finalize` to build a candidate map from active docs, resolve this doc's outgoing unresolved links, AND re-resolve dangling links pointing AT this doc (so as a vault is ingested incrementally, links to a not-yet-existed target get resolved when the target arrives).

**Files:**
- Modify: `src/harbor_clerk/worker/stages/finalize.py`

- [ ] **Step 1: Update imports**

Change the existing models import in `finalize.py`:

```python
from harbor_clerk.models import Chunk, Document, DocumentPage, Upload
```

to:

```python
from harbor_clerk.models import Chunk, Document, DocumentLink, DocumentPage, Upload
```

- [ ] **Step 2: Add the resolution block in `run_finalize`**

Inside the `try` block in `run_finalize`, AFTER `doc.pipeline_status = PipelineStatus.ready` and `doc.updated_at = datetime.now(UTC)`, but BEFORE the existing `# Mark related uploads as done` block, add the resolution logic:

```python
        # --- Wikilink resolution ---
        # Build a name→doc_ids map across all active docs (this doc included,
        # because incoming links from this doc to itself are valid).
        all_active = session.execute(
            select(Document.doc_id, Document.canonical_filename, Document.title).where(
                Document.status == "active"
            )
        ).all()
        candidates_by_name: dict[str, list[uuid.UUID]] = {}
        for did, fname, title in all_active:
            if fname:
                stem = fname.rsplit(".", 1)[0].strip().lower()
                if stem:
                    candidates_by_name.setdefault(stem, []).append(did)
            if title:
                t = title.strip().lower()
                if t:
                    candidates_by_name.setdefault(t, []).append(did)

        # Resolve this doc's outgoing unresolved links.
        outgoing = (
            session.execute(
                select(DocumentLink).where(
                    DocumentLink.src_doc_id == doc_id, DocumentLink.resolved.is_(False)
                )
            )
            .scalars()
            .all()
        )
        for link in outgoing:
            resolved_id = _resolve_link(link.target_title, candidates_by_name)
            if resolved_id is not None:
                link.target_doc_id = resolved_id
                link.resolved = True

        # Re-resolve dangling links pointing AT this doc. Compute the names
        # this doc matches; find unresolved links whose target_title matches
        # one of them; resolve if the name is unique (i.e. only this doc has it).
        my_names: set[str] = set()
        if doc.canonical_filename:
            stem = doc.canonical_filename.rsplit(".", 1)[0].strip().lower()
            if stem:
                my_names.add(stem)
        if doc.title:
            t = doc.title.strip().lower()
            if t:
                my_names.add(t)

        if my_names:
            dangling = (
                session.execute(
                    select(DocumentLink).where(
                        DocumentLink.resolved.is_(False),
                        DocumentLink.target_title.in_(my_names),
                        DocumentLink.src_doc_id != doc_id,  # outgoing already handled above
                    )
                )
                .scalars()
                .all()
            )
            for link in dangling:
                resolved_id = _resolve_link(link.target_title, candidates_by_name)
                if resolved_id is not None:
                    link.target_doc_id = resolved_id
                    link.resolved = True

        if outgoing or (my_names and dangling):
            logger.info(
                "Resolved %d outgoing + %d dangling wikilinks for doc %s",
                sum(1 for link in outgoing if link.resolved),
                sum(1 for link in (dangling if my_names else []) if link.resolved),
                doc_id,
            )
```

- [ ] **Step 3: Lint and run the resolver tests + pipeline regression**

Run: `uv run ruff check src/harbor_clerk/worker/stages/finalize.py`
Expected: clean.

Run: `uv run pytest tests/test_finalize_resolve.py tests/test_pipeline.py tests/test_watch_pipeline.py -q`
Expected: PASS (no regression).

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/worker/stages/finalize.py
git commit -m "feat(finalize): resolve outgoing + dangling wikilinks at finalize-time"
```

---

### Task 7: `kb_find_related` blends linked docs

Extend `kb_find_related` in `src/harbor_clerk/mcp_server.py` to prepend explicitly-linked documents (those that this doc links to AND those that link to this doc, both via `DocumentLink.resolved=True`) ahead of the embedding-derived results. Linked docs get `similarity=1.0`. Deduplicate. Respect the `k` cap.

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`

- [ ] **Step 1: Update the imports in `mcp_server.py`**

The existing imports of models in `mcp_server.py` include `Document`, `Chunk`, etc. Extend to also import `DocumentLink`. Find the existing model import section and add `DocumentLink`:

```python
from harbor_clerk.models.document_link import DocumentLink
```

(Or, if other models in that file are imported via `from harbor_clerk.models import ...`, add `DocumentLink` to that import. Read the file to see the existing pattern and match it.)

Also add `or_` to the SQLAlchemy import if it's not already there:

```python
from sqlalchemy import or_, select, func  # (extend the existing line)
```

- [ ] **Step 2: Replace the result-composition section of `kb_find_related`**

In the existing `kb_find_related` function, after `if not nearest: return ...` and before the line `# Fetch document metadata for results`, AND the subsequent `related_ids = [...]` / `distances = {...}` / `docs_result = ...` / `related_docs = {...}` / `items = []` / final `for rid in related_ids:` loop — replace the current "compose the result list from embedding-nearest only" section with a version that prepends linked docs.

Find the existing block (currently at lines ~1479–1509):

```python
        if not nearest:
            return json.dumps({"doc_id": doc_id, "related": []})

        # Fetch document metadata for results
        related_ids = [row[0] for row in nearest]
        distances = {row[0]: float(row[1]) for row in nearest}

        docs_result = await session.execute(select(Document).where(Document.doc_id.in_(related_ids)))
        related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in related_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": rdoc.summary,
                "similarity": round(1.0 - distances[rid], 4),
                "doc_type": rdoc.doc_type,
                "mime_type": rdoc.mime_type,
                "canonical_filename": rdoc.canonical_filename,
            }
        )

    return json.dumps(
        {"doc_id": doc_id, "related": items},
        indent=2,
    )
```

Replace with:

```python
        # Explicit wikilink graph: docs this doc links to OR docs that link
        # to this doc, both via resolved DocumentLink rows. Linked docs are
        # prepended to the result with similarity=1.0 (max signal).
        link_rows = (
            await session.execute(
                select(DocumentLink.src_doc_id, DocumentLink.target_doc_id).where(
                    DocumentLink.resolved.is_(True),
                    or_(
                        DocumentLink.src_doc_id == target_id,
                        DocumentLink.target_doc_id == target_id,
                    ),
                )
            )
        ).all()
        linked_ids: list[uuid.UUID] = []
        seen_linked: set[uuid.UUID] = set()
        for src, tgt in link_rows:
            other = tgt if src == target_id else src
            if other is None or other == target_id or other in seen_linked:
                continue
            if visible_ids is not None and other not in visible_ids:
                continue
            seen_linked.add(other)
            linked_ids.append(other)

        # Embedding-nearest results (deduplicated against linked).
        nearest_ids = [row[0] for row in nearest if row[0] not in seen_linked]
        distances = {row[0]: float(row[1]) for row in nearest}

        # Merge — linked first, then embedding-nearest — capped at k.
        merged_ids: list[uuid.UUID] = (linked_ids + nearest_ids)[:k]
        if not merged_ids:
            return json.dumps({"doc_id": doc_id, "related": []})

        docs_result = await session.execute(
            select(Document).where(Document.doc_id.in_(merged_ids), Document.status == "active")
        )
        related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in merged_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        if rid in seen_linked:
            similarity = 1.0
            source = "linked"
        else:
            similarity = round(1.0 - distances[rid], 4)
            source = "embedding"
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": rdoc.summary,
                "similarity": similarity,
                "source": source,
                "doc_type": rdoc.doc_type,
                "mime_type": rdoc.mime_type,
                "canonical_filename": rdoc.canonical_filename,
            }
        )

    return json.dumps(
        {"doc_id": doc_id, "related": items},
        indent=2,
    )
```

The new `source` field (`"linked"` vs `"embedding"`) is added so callers can tell which signal produced each result; existing callers that don't read `source` are unaffected.

- [ ] **Step 3: Lint and run the MCP tests + pipeline regression**

Run: `uv run ruff check src/harbor_clerk/mcp_server.py`
Expected: clean.

Run: `uv run pytest tests/test_mcp_tools.py tests/test_pipeline.py tests/test_watch_pipeline.py -q`
Expected: PASS (no regression in existing `kb_find_related` tests; the new behavior is additive — existing tests don't exercise linked docs, so the new code path doesn't fire).

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/mcp_server.py
git commit -m "feat(mcp): blend wikilink graph into kb_find_related results"
```

---

### Task 8: End-to-end integration test

Verify the full chain: two Markdown docs that link to each other, ingested through extract → finalize, produce resolved `DocumentLink` rows in both directions, and `kb_find_related` returns each as a related doc via the link graph.

**Files:**
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Append the integration test**

Append to `tests/test_pipeline.py`:

```python
def test_wikilink_graph_resolves_and_kb_find_related_returns_linked(sync_session, tmp_path):
    """Integration: doc A and doc B link to each other via [[…]]. After
    extract + finalize, the document_links rows resolve in both directions.
    kb_find_related on either doc returns the other with source='linked'."""
    import asyncio
    import hashlib
    import json

    from sqlalchemy import select

    from harbor_clerk.mcp_server import kb_find_related
    from harbor_clerk.models.document import Document
    from harbor_clerk.models.document_link import DocumentLink
    from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
    from harbor_clerk.models.ingestion_job import IngestionJob
    from harbor_clerk.worker.stages.extract import run_extract
    from harbor_clerk.worker.stages.finalize import run_finalize

    def _ingest(md_text: str, name: str) -> uuid.UUID:
        md_path = tmp_path / name
        md_path.write_text(md_text)
        doc = Document(
            title=md_path.stem,
            canonical_filename=md_path.name,
            status="active",
            sha256=hashlib.sha256(md_path.read_bytes()).digest(),
            source_path=str(md_path),
            pipeline_status=PipelineStatus.queued,
        )
        sync_session.add(doc)
        sync_session.flush()
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
        sync_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.queued))
        sync_session.commit()
        return doc.doc_id

    # Doc A links to "Note B"; Doc B links to "Note A".
    a_id = _ingest("# Note A\n\nSee [[Note B]] for details.\n", "Note A.md")
    b_id = _ingest("# Note B\n\nBack to [[Note A]].\n", "Note B.md")

    # Extract both.
    run_extract(a_id)
    run_extract(b_id)
    # Finalize in order — A first, then B. After B finalizes the dangling
    # link from A to B should be resolved by the dangling-resolve pass.
    run_finalize(a_id)
    run_finalize(b_id)

    sync_session.expire_all()
    links = sync_session.execute(select(DocumentLink).order_by(DocumentLink.src_doc_id)).scalars().all()
    assert len(links) == 2, f"expected 2 links total, got {len(links)}: {[(l.src_doc_id, l.target_title, l.resolved) for l in links]}"
    # Both must be resolved after finalize finishes for both docs.
    assert all(l.resolved for l in links), f"links unresolved: {[(l.target_title, l.resolved) for l in links]}"
    # A→B and B→A both present.
    pairs = {(l.src_doc_id, l.target_doc_id) for l in links}
    assert (a_id, b_id) in pairs
    assert (b_id, a_id) in pairs

    # kb_find_related on A returns B as a 'linked' result.
    result_str = asyncio.run(kb_find_related.fn(doc_id=str(a_id), k=5))
    result = json.loads(result_str)
    related = result.get("related", [])
    by_id = {r["doc_id"]: r for r in related}
    assert str(b_id) in by_id, f"B not in kb_find_related(A): {related!r}"
    assert by_id[str(b_id)]["source"] == "linked"
    assert by_id[str(b_id)]["similarity"] == 1.0
```

Notes:
- `kb_find_related.fn(...)` is the underlying async function the MCP decorator wraps; `kb_find_related` itself is a `Tool` object. The `.fn` attribute calls the function directly without going through the MCP protocol — appropriate for unit-level use.
- The test sets up two `IngestionJob` rows per doc (extract + finalize) because `mark_stage_running` in each stage requires a queued row.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_pipeline.py::test_wikilink_graph_resolves_and_kb_find_related_returns_linked -v`
Expected: PASS.

If `kb_find_related.fn` is not the correct attribute name (the MCP decorator's internals vary), read `kb_find_related` to find how the underlying coroutine is exposed and adjust the call. Acceptable alternatives: `kb_find_related.func`, `kb_find_related.callable`, or wrap with `asyncio.run` against the registered tool. If after a brief investigation the right invocation isn't clear, STOP and report NEEDS_CONTEXT — do not invent a calling convention.

Run `uv run ruff check tests/test_pipeline.py` — clean.

- [ ] **Step 3: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test(pipeline): integration test for wikilink graph + kb_find_related blending"
```

---

### Task 9: Phase 4 verification

Confirm the whole phase is clean before completing it.

- [ ] **Step 1: Lint and format**

Run: `uv run ruff check src/ tests/`
Expected: no errors.

Run: `uv run ruff format --check src/ tests/`
Expected: clean.

- [ ] **Step 2: Run Phase 4 + prior-phase test sets**

Run:
```bash
uv run pytest tests/test_document_link_model.py tests/test_finalize_resolve.py tests/test_markdown_extract.py tests/test_pipeline.py tests/test_watch_pipeline.py tests/test_chunking.py tests/test_extract_helpers.py tests/test_file_types.py -v
```
Expected: all PASS.

- [ ] **Step 3: Run the full suite (regression check)**

Run: `uv run pytest tests/ -q -m "not integration and not requires_models"`
Expected: PASS, count ≥ Phase 3 baseline (856) plus Phase 4's new tests.

- [ ] **Step 4: Confirm migration applies cleanly to a fresh DB**

Run: `uv run alembic upgrade head` (in worktree root) — confirm it reports `0001_initial` → `0002_document_links` applied.

Run: `uv run alembic current` — confirm head is `0002_document_links`.

- [ ] **Step 5: Quick sanity check on the data shape**

Optional but reassuring: open `psql` against the test DB (port 5433 or 5432, db `harbor_clerk_test`, user `lka`, password `lka_dev_password`), run `\d document_links` and confirm the columns + indexes match the migration.

---

## Self-Review

Checked against the spec's Phase 4:

- **New `document_links` table:** Task 1 — model + migration with all columns from the spec (link_id, src_doc_id, target_doc_id, link_text, target_title, anchor, alias, resolved, created_at), CASCADE on src, SET NULL on target, indexed on src/target/target_title.
- **Capture at extract time:** Task 2 (`parse_wikilinks`) + Task 3 (`extract_markdown` returns wikilinks) + Task 4 (extract stage writes rows).
- **Resolution at finalize, name match (filename stem primary, title secondary):** Task 5 (`_resolve_link` pure helper) + Task 6 (`run_finalize` builds candidate map covering BOTH filename stems and titles, runs resolve for outgoing + dangling).
- **Re-resolution of dangling links:** Task 6 explicitly handles this — when a doc finalizes whose name matches a previously-dangling link's target_title, the link is resolved.
- **Ambiguity → unresolved:** `_resolve_link` returns `None` when `candidates_by_name[key]` has length ≠ 1.
- **Retrieval integration (`kb_find_related` blend):** Task 7 — linked docs prepended with similarity=1.0; new `source` field discriminates between "linked" and "embedding"; existing dedup + k cap preserved.
- **Integration test end-to-end:** Task 8 — two interlinking docs, full extract+finalize, asserts both links resolved AND `kb_find_related` returns the other doc with `source="linked"`.

No placeholders. Type names consistent throughout (`DocumentLink`, `parse_wikilinks`, `_resolve_link`, `wikilinks` field, `link_text`/`target_title`/`anchor`/`alias` keys).

**Out of scope (deferred to future work):** surfacing backlinks via MCP (`kb_backlinks` tool / extending `kb_get_document`) and a backlinks panel in the document-detail UI. These are recorded in the spec's Deferred / Future Work section.
