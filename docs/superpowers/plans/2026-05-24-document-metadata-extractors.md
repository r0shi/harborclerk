# Document Metadata Extractors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture machine-readable metadata HC already has access to (Tika fields, YAML frontmatter, JSON sidecars) into a new `Document.doc_metadata` JSONB column, and expose it via a `metadata_filter` parameter on `kb_search` so models can disambiguate boundary-doc cases by fact rather than by similarity.

**Architecture:** New `src/harbor_clerk/ingest/metadata_extractors/` package with pluggable per-source extractors. Extract stage runs them and merges results under namespaced keys (`{tika: ..., frontmatter: ..., sidecar: ..., _source_provenance: ...}`). Search layer translates `metadata_filter={path: value}` to JSONB `@>` containment + `?` existence queries against a new GIN index. Frontmatter is stripped from body text before chunking.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async (asyncpg), Alembic, FastAPI, PostgreSQL 18 + JSONB GIN, Tika 3.3.0 via httpx, `python-frontmatter` 1.x (new runtime dep), pytest.

**Spec reference:** `docs/superpowers/specs/2026-05-24-document-metadata-extractors-design.md`

---

## File Structure

**New files (this PR):**
- `alembic/versions/0004_documents_metadata.py` — schema migration
- `src/harbor_clerk/ingest/__init__.py` — package marker
- `src/harbor_clerk/ingest/metadata_extractors/__init__.py` — registry + `run_all()` framework
- `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py` — Tika `/meta` extractor
- `src/harbor_clerk/ingest/metadata_extractors/frontmatter.py` — YAML frontmatter parser for markdown
- `src/harbor_clerk/ingest/metadata_extractors/sidecar.py` — `<stem>.json` loader
- `tests/ingest/__init__.py`
- `tests/ingest/test_metadata_extractor_framework.py` — `run_all()` orchestration
- `tests/ingest/test_tika_metadata_extractor.py`
- `tests/ingest/test_frontmatter_extractor.py`
- `tests/ingest/test_sidecar_extractor.py`
- `tests/ingest/test_search_metadata_filter.py` — end-to-end `metadata_filter` against a small in-memory corpus

**Modified files (this PR):**
- `src/harbor_clerk/models/document.py` — add `doc_metadata` column (SQLAlchemy attribute named `doc_metadata` to avoid shadowing `Base.metadata`)
- `src/harbor_clerk/worker/stages/extract.py` — call `run_all()` after body extraction, persist to `Document.doc_metadata`
- `src/harbor_clerk/worker/markdown_extract.py` — strip frontmatter from body text before chunking
- `src/harbor_clerk/search.py` — add `metadata_filter` param + JSONB query translation in `hybrid_search`
- `src/harbor_clerk/mcp_server.py` — add `metadata_filter` param to `kb_search` + add metadata field to `kb_get_document` response
- `pyproject.toml` — add `python-frontmatter>=1.1` runtime dep

**Untouched:** existing email-typed columns (`email_from_address`, etc.); existing kb_* tools other than `kb_search` and `kb_get_document` (those get description rewrites in PR-D).

---

## Task 1: Schema migration — add `doc_metadata` JSONB column

**Files:**
- Create: `alembic/versions/0004_documents_metadata.py`
- Modify: `src/harbor_clerk/models/document.py`

- [ ] **Step 1: Write the failing model test**

Create `tests/models/test_document_metadata_column.py`:

```python
"""Tests for the doc_metadata JSONB column on Document."""

from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus


async def test_document_doc_metadata_defaults_to_empty_dict(db_session):
    """A freshly-created Document has doc_metadata = {}."""
    doc = Document(
        title="t",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    assert doc.doc_metadata == {}


async def test_document_doc_metadata_persists_arbitrary_dict(db_session):
    """The column stores and round-trips an arbitrary nested dict."""
    payload = {
        "tika": {"author": "Jane", "page_count": 3},
        "frontmatter": {"tags": ["alpha", "beta"]},
        "_source_provenance": {"tika": "2026-05-24T00:00:00+00:00"},
    }
    doc = Document(
        title="t",
        status="active",
        sha256=b"\x01" * 32,
        pipeline_status=PipelineStatus.queued,
        doc_metadata=payload,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    assert doc.doc_metadata == payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_document_metadata_column.py -v`
Expected: `AttributeError: 'Document' object has no attribute 'doc_metadata'`.

- [ ] **Step 3: Add the column to the Document model**

Edit `src/harbor_clerk/models/document.py` — add after the existing email columns (around line 70, before the closing of the class). Add the `JSONB` import at the top of the imports:

```python
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
```

Add the column inside the `Document` class (preserve existing columns; insert near the end of the column declarations):

```python
    # Pluggable metadata extracted at ingest time. Namespaced by source:
    # {"tika": {...}, "frontmatter": {...}, "sidecar": {...},
    #  "_source_provenance": {"tika": "2026-...", ...}}
    # The Python attribute is `doc_metadata` (not `metadata`) to avoid
    # shadowing SQLAlchemy's `Base.metadata`. The PostgreSQL column is
    # named `metadata` for natural querying.
    doc_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default="{}",
    )
```

(If `JSONB` is already imported, skip the import line.)

- [ ] **Step 4: Create the alembic migration**

Create `alembic/versions/0004_documents_metadata.py`:

```python
"""documents.metadata JSONB column

Adds a JSONB metadata column populated at ingest by the new extractor
framework (Tika headers, YAML frontmatter, JSON sidecars). GIN index
supports the @> containment operator used by kb_search's metadata_filter
parameter.

The SQLAlchemy attribute on Document is `doc_metadata` to avoid shadowing
Base.metadata; the PostgreSQL column is `metadata`.

Revision ID: 0004_documents_metadata
Revises: 0003_wf_skip_tracking
Create Date: 2026-05-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_documents_metadata"
down_revision = "0003_wf_skip_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_documents_metadata_gin",
        "documents",
        ["metadata"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_documents_metadata_gin", table_name="documents")
    op.drop_column("documents", "metadata")
```

- [ ] **Step 5: Run the test to verify it passes**

The conftest applies alembic migrations against the test DB. Run:

```bash
uv run pytest tests/models/test_document_metadata_column.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check src/harbor_clerk/models/document.py alembic/versions/0004_documents_metadata.py tests/models/test_document_metadata_column.py
uv run ruff format --check src/harbor_clerk/models/document.py alembic/versions/0004_documents_metadata.py tests/models/test_document_metadata_column.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0004_documents_metadata.py src/harbor_clerk/models/document.py tests/models/test_document_metadata_column.py
git commit -m "feat(schema): documents.metadata JSONB column + GIN index" \
  -m "First task in PR-F. Adds a namespaced metadata column populated at ingest by the new extractor framework (later tasks); GIN index supports the @> containment query used by kb_search's metadata_filter param. SQLAlchemy attribute is doc_metadata to avoid shadowing Base.metadata; PostgreSQL column is metadata for natural querying." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Extractor framework — Protocol + `run_all()` orchestrator

**Files:**
- Create: `src/harbor_clerk/ingest/__init__.py`
- Create: `src/harbor_clerk/ingest/metadata_extractors/__init__.py`
- Create: `tests/ingest/__init__.py`
- Create: `tests/ingest/test_metadata_extractor_framework.py`

- [ ] **Step 1: Write the failing framework test**

Create `tests/ingest/__init__.py` (empty file).

Create `tests/ingest/test_metadata_extractor_framework.py`:

```python
"""Tests for the metadata extractor framework: registration + run_all()."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from harbor_clerk.ingest.metadata_extractors import MetadataExtractor, _run_extractors


@dataclass
class _FakeDoc:
    """Stand-in for Document; only needs doc_id + title for the framework tests."""

    doc_id: uuid.UUID
    title: str = "fake"


class _AlphaExtractor:
    name = "alpha"

    def extract(self, *, doc, raw_bytes, source_path):
        return {"foo": "bar"}


class _BetaExtractor:
    name = "beta"

    def extract(self, *, doc, raw_bytes, source_path):
        return {"baz": "qux"}


class _SkippingExtractor:
    name = "skipper"

    def extract(self, *, doc, raw_bytes, source_path):
        return None  # signals "doesn't apply to this doc"


class _FailingExtractor:
    name = "broken"

    def extract(self, *, doc, raw_bytes, source_path):
        raise RuntimeError("intentionally broken")


def test_run_all_merges_namespaced_results():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _BetaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert out["alpha"] == {"foo": "bar"}
    assert out["beta"] == {"baz": "qux"}


def test_run_all_records_provenance_per_extractor():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    prov = out.get("_source_provenance", {})
    assert "alpha" in prov
    # ISO 8601 with offset
    parsed = datetime.fromisoformat(prov["alpha"])
    assert parsed.tzinfo is not None


def test_run_all_skips_extractors_that_return_none():
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _SkippingExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert "alpha" in out
    assert "skipper" not in out
    assert "skipper" not in out.get("_source_provenance", {})


def test_run_all_isolates_extractor_failures(caplog):
    """A failing extractor logs a warning but does not abort the others."""
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors(
        [_AlphaExtractor(), _FailingExtractor(), _BetaExtractor()],
        doc=doc,
        raw_bytes=b"",
        source_path=None,
    )
    assert "alpha" in out
    assert "beta" in out
    assert "broken" not in out
    # The warning landed
    assert any("broken" in rec.message for rec in caplog.records if rec.levelname == "WARNING")


def test_run_all_empty_when_no_extractors_match():
    """All extractors skip → empty dict, no provenance key."""
    doc = _FakeDoc(doc_id=uuid.uuid4())
    out = _run_extractors([_SkippingExtractor()], doc=doc, raw_bytes=b"", source_path=None)
    assert out == {}


def test_metadata_extractor_protocol_is_satisfied_by_duck_type():
    """MetadataExtractor is a @runtime_checkable Protocol — duck-typed mocks
    satisfy isinstance()."""
    assert isinstance(_AlphaExtractor(), MetadataExtractor)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/ingest/test_metadata_extractor_framework.py -v
```

Expected: `ModuleNotFoundError: No module named 'harbor_clerk.ingest'`.

- [ ] **Step 3: Create the ingest package**

Create `src/harbor_clerk/ingest/__init__.py` (empty file).

Create `src/harbor_clerk/ingest/metadata_extractors/__init__.py`:

```python
# src/harbor_clerk/ingest/metadata_extractors/__init__.py
"""Pluggable metadata extractors for HC's ingest pipeline.

Each extractor returns a dict of fields keyed under its `name` namespace
on the document's metadata JSONB column. The framework runs them in
EXTRACTORS order, merges results, and records per-source provenance
timestamps. A failing extractor logs a warning but does not abort the
others — ingestion stays resilient to one-off Tika hiccups or malformed
frontmatter.

Public surface:
  MetadataExtractor — @runtime_checkable Protocol
  EXTRACTORS        — production tuple, used by extract.py
  run_all(...)      — production entry point (uses EXTRACTORS)
  _run_extractors() — testable helper that takes an explicit extractor list
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class MetadataExtractor(Protocol):
    """A single extractor; one per metadata source.

    Implementations must declare `name` (the namespace key on the merged
    metadata dict) and `extract(*, doc, raw_bytes, source_path) -> dict | None`.
    Returning None signals "this extractor doesn't apply to this doc"
    (e.g. frontmatter extractor on a PDF) — the namespace is omitted
    entirely from the merged output.
    """

    name: str

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None: ...


def _run_extractors(
    extractors: list[MetadataExtractor],
    *,
    doc,
    raw_bytes: bytes,
    source_path: str | None,
) -> dict[str, Any]:
    """Run the given extractor list and merge results. See run_all() docstring."""
    out: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for ext in extractors:
        try:
            result = ext.extract(doc=doc, raw_bytes=raw_bytes, source_path=source_path)
        except Exception as exc:
            log.warning(
                "metadata extractor %s failed on doc %s: %s",
                ext.name,
                getattr(doc, "doc_id", "<unknown>"),
                exc,
            )
            continue
        if result:
            out[ext.name] = result
            provenance[ext.name] = datetime.now(UTC).isoformat()
    if out:
        out["_source_provenance"] = provenance
    return out


# Production extractor tuple — populated by individual extractors in
# later tasks. Keep at the bottom of the module so the imports below
# can reference symbols defined above.
EXTRACTORS: list[MetadataExtractor] = []


def run_all(*, doc, raw_bytes: bytes, source_path: str | None) -> dict[str, Any]:
    """Entry point used by the extract stage. Runs EXTRACTORS in order,
    merges results, returns a namespaced dict suitable for assigning to
    Document.doc_metadata."""
    return _run_extractors(EXTRACTORS, doc=doc, raw_bytes=raw_bytes, source_path=source_path)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/ingest/test_metadata_extractor_framework.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Lint + format**

```bash
uv run ruff check src/harbor_clerk/ingest/ tests/ingest/
uv run ruff format --check src/harbor_clerk/ingest/ tests/ingest/
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/ingest/ tests/ingest/__init__.py tests/ingest/test_metadata_extractor_framework.py
git commit -m "feat(ingest): metadata extractor framework — Protocol + run_all()" \
  -m "Pluggable per-source extractors keyed by namespace. Failing extractor logs + continues so one-off Tika hiccups or malformed frontmatter don't abort ingestion. EXTRACTORS list is empty for now; populated by Tika/frontmatter/sidecar extractors in the next three tasks." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Tika metadata extractor

**Files:**
- Create: `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py`
- Modify: `src/harbor_clerk/ingest/metadata_extractors/__init__.py` (add to EXTRACTORS)
- Create: `tests/ingest/test_tika_metadata_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/ingest/test_tika_metadata_extractor.py`:

```python
"""TikaMetadataExtractor — calls Tika's /meta endpoint, whitelists fields."""

import uuid
from dataclasses import dataclass

import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.ingest.metadata_extractors.tika_metadata import (
    TIKA_FIELD_ALIASES,
    TikaMetadataExtractor,
)


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str
    mime_type: str | None


def test_tika_extractor_returns_aliased_whitelisted_fields(httpserver: HTTPServer, monkeypatch):
    """Tika /meta returns a noisy dict; the extractor whitelists + aliases
    to readable filter keys and drops everything else."""
    tika_response = {
        # Whitelisted aliases
        "dc:creator": "Jane Doe",
        "dc:title": "Q3 Report",
        "dc:subject": "Quarterly Earnings",
        "xmpTPg:NPages": 12,
        "dcterms:created": "2024-09-15T10:30:00Z",
        # Noise that should be dropped
        "X-TIKA-Parsed-By": "org.apache.tika.parser.pdf.PDFParser",
        "X-TIKA-Content-Length": "8432",
        "pdf:PDFVersion": "1.7",
        "Custom-Random-Field": "ignored",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)

    # Point Tika settings at the test server
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="q3", mime_type="application/pdf"),
        raw_bytes=b"%PDF-1.7\n...",
        source_path=None,
    )

    assert out == {
        "author": "Jane Doe",
        "title": "Q3 Report",
        "subject": "Quarterly Earnings",
        "page_count": 12,
        "created_at": "2024-09-15T10:30:00Z",
    }


def test_tika_extractor_returns_none_when_tika_url_unset(monkeypatch):
    """If tika_url is empty, return None (no Tika to call)."""
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": ""})(),
    )
    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", mime_type="text/plain"),
        raw_bytes=b"hello",
        source_path=None,
    )
    assert out is None


def test_tika_extractor_handles_empty_response(httpserver: HTTPServer, monkeypatch):
    """Tika returning {} → return None (don't write an empty 'tika' namespace)."""
    httpserver.expect_request("/meta").respond_with_json({})
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )
    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", mime_type="application/pdf"),
        raw_bytes=b"...",
        source_path=None,
    )
    assert out is None


def test_tika_extractor_collects_email_headers(httpserver: HTTPServer, monkeypatch):
    """For .eml files, Tika emits Message-From/To/Cc/Subject headers."""
    tika_response = {
        "Message-From": "alice@example.com",
        "Message-To": "bob@example.com,carol@example.com",
        "Message-Cc": "dave@example.com",
        "Message-Subject": "Re: Project Update",
        "dcterms:created": "2024-09-15T10:30:00Z",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)
    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="re-project-update", mime_type="message/rfc822"),
        raw_bytes=b"From: alice@example.com\n...",
        source_path=None,
    )

    assert out["email_from"] == "alice@example.com"
    assert out["email_to"] == "bob@example.com,carol@example.com"
    assert out["email_cc"] == "dave@example.com"
    assert out["email_subject"] == "Re: Project Update"


def test_tika_field_aliases_has_no_duplicate_target_keys():
    """The alias map can map two Tika keys to the same target (e.g. both
    xmpTPg:NPages and Page-Count → page_count); for each target key, the
    LAST writer wins. Document the conflict set for future readers."""
    targets = list(TIKA_FIELD_ALIASES.values())
    # We allow duplicates (Tika emits aliases for the same concept), but the
    # test pins the current count so a future refactor surfaces the change.
    unique_targets = set(targets)
    duplicates = [t for t in unique_targets if targets.count(t) > 1]
    # Document expected duplicates explicitly:
    assert sorted(duplicates) == sorted(["page_count"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/ingest/test_tika_metadata_extractor.py -v
```

Expected: `ModuleNotFoundError: No module named 'harbor_clerk.ingest.metadata_extractors.tika_metadata'`.

- [ ] **Step 3: Implement the Tika extractor**

Create `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py`:

```python
# src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py
"""TikaMetadataExtractor — captures Tika's metadata dict.

Tika's /meta endpoint returns a JSON dict with potentially 50-200 fields,
most of which are framework noise (X-TIKA-Parsed-By, X-TIKA-Content-Length,
parser-specific keys). The whitelist + alias map normalizes the wildly
varying Tika field names to readable filter keys; unknown fields are dropped.
No raw passthrough — keeps the metadata blob bounded and the filter surface
clean.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from harbor_clerk.config import get_settings

log = logging.getLogger(__name__)

# Tika field name → readable filter key. When two Tika fields map to the
# same target, the LAST one wins (intentional — Tika sometimes emits the
# same concept under multiple keys, and the most reliable one is listed
# last). Test pins the duplicate set so a future refactor surfaces changes.
TIKA_FIELD_ALIASES: dict[str, str] = {
    # Dublin Core
    "dc:creator": "author",
    "dc:title": "title",
    "dc:subject": "subject",
    "dc:description": "description",
    "dc:language": "language",
    "dcterms:created": "created_at",
    "dcterms:modified": "modified_at",
    "meta:keyword": "keywords",
    # Pagination / structure (last writer wins → Page-Count beats xmpTPg:NPages)
    "xmpTPg:NPages": "page_count",
    "Page-Count": "page_count",
    # MIME / encoding
    "Content-Type": "content_type",
    "Content-Encoding": "encoding",
    # Email headers (Tika's email parser; takes precedence over dcterms:created
    # when both appear — order matters)
    "Message-From": "email_from",
    "Message-To": "email_to",
    "Message-Cc": "email_cc",
    "Message-Subject": "email_subject",
}


class TikaMetadataExtractor:
    """Calls Tika's /meta endpoint, whitelists fields, drops noise."""

    name = "tika"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        settings = get_settings()
        if not settings.tika_url:
            return None
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.put(
                    f"{settings.tika_url}/meta",
                    content=raw_bytes,
                    headers={"Accept": "application/json"},
                )
                if resp.status_code != 200:
                    log.warning(
                        "tika /meta returned HTTP %s for doc %s",
                        resp.status_code,
                        getattr(doc, "doc_id", "<unknown>"),
                    )
                    return None
                raw: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("tika /meta failed for doc %s: %s", getattr(doc, "doc_id", "<unknown>"), exc)
            return None

        # Whitelist + alias
        out: dict[str, Any] = {}
        for tika_key, target_key in TIKA_FIELD_ALIASES.items():
            if tika_key in raw:
                out[target_key] = raw[tika_key]

        return out or None
```

- [ ] **Step 4: Register the extractor**

Edit `src/harbor_clerk/ingest/metadata_extractors/__init__.py` — find the `EXTRACTORS` list (currently empty) and add the import + entry:

```python
# At the bottom of the file (after the existing class + function defs):
from harbor_clerk.ingest.metadata_extractors.tika_metadata import TikaMetadataExtractor  # noqa: E402

EXTRACTORS: list[MetadataExtractor] = [TikaMetadataExtractor()]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/ingest/ -v
```

Expected: all 6 framework tests + 5 Tika tests pass (11 total).

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check src/harbor_clerk/ingest/ tests/ingest/
uv run ruff format --check src/harbor_clerk/ingest/ tests/ingest/
```

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py src/harbor_clerk/ingest/metadata_extractors/__init__.py tests/ingest/test_tika_metadata_extractor.py
git commit -m "feat(ingest): TikaMetadataExtractor — captures /meta, whitelists noise" \
  -m "Calls Tika's /meta endpoint per file, whitelists ~15 useful fields (author, title, subject, dates, page_count, content_type, email headers for .eml), drops the 50-200 noise fields Tika emits (X-TIKA-Parsed-By, parser-specific keys, etc). Aliases Tika's wildly-varying field names (dc:creator, dcterms:created, etc) to readable filter keys." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Frontmatter extractor

**Files:**
- Modify: `pyproject.toml` (add `python-frontmatter>=1.1` runtime dep)
- Create: `src/harbor_clerk/ingest/metadata_extractors/frontmatter.py`
- Modify: `src/harbor_clerk/ingest/metadata_extractors/__init__.py` (register)
- Create: `tests/ingest/test_frontmatter_extractor.py`

- [ ] **Step 1: Add the runtime dependency**

Edit `pyproject.toml`. Find the `dependencies = [...]` list and add `"python-frontmatter>=1.1",` alphabetically:

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    # ... existing entries ...
    "python-frontmatter>=1.1",
    # ... continue with the rest ...
]
```

Then sync:

```bash
uv sync --extra test
```

Expected: `python-frontmatter` installed successfully.

- [ ] **Step 2: Write the failing test**

Create `tests/ingest/test_frontmatter_extractor.py`:

```python
"""FrontmatterExtractor — parses YAML frontmatter from markdown."""

import uuid
from dataclasses import dataclass

from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str
    canonical_filename: str | None


def test_frontmatter_extractor_parses_yaml_block():
    body = b"""---
title: Meeting Notes
date: 2024-09-15
tags: [team, planning]
status: draft
---

# Meeting Notes

We discussed roadmap items.
"""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="meeting", canonical_filename="notes.md"),
        raw_bytes=body,
        source_path=None,
    )
    assert out == {
        "title": "Meeting Notes",
        "date": "2024-09-15",  # YAML date → ISO string at the boundary
        "tags": ["team", "planning"],
        "status": "draft",
    }


def test_frontmatter_extractor_returns_none_for_non_markdown():
    """A .pdf or .docx file is never parsed for frontmatter."""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="r", canonical_filename="report.pdf"),
        raw_bytes=b"%PDF-1.7\n---\ntitle: nope\n---\n...",
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_returns_none_for_markdown_without_frontmatter():
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="r", canonical_filename="note.md"),
        raw_bytes=b"# Just a heading\n\nNo frontmatter here.\n",
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_handles_malformed_yaml_without_raising():
    """A markdown file with broken YAML in the frontmatter block → returns
    None + logs a warning. Does NOT propagate the parser exception (the
    framework would catch it anyway but the extractor itself is defensive)."""
    body = b"""---
title: Meeting
date: 2024-09-15
unclosed_list: [a, b
---

content
"""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="m", canonical_filename="m.md"),
        raw_bytes=body,
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_recognises_markdown_extension_variants():
    """Both .md and .markdown trigger parsing."""
    body = b"---\nfoo: bar\n---\n\ncontent\n"
    extractor = FrontmatterExtractor()
    for ext in (".md", ".markdown"):
        out = extractor.extract(
            doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", canonical_filename=f"file{ext}"),
            raw_bytes=body,
            source_path=None,
        )
        assert out == {"foo": "bar"}, f"failed for extension {ext}"


def test_frontmatter_extractor_falls_back_to_source_path_when_no_filename():
    """If canonical_filename is None, use source_path to detect the extension."""
    body = b"---\nfoo: bar\n---\n\ncontent\n"
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", canonical_filename=None),
        raw_bytes=body,
        source_path="/tmp/note.md",
    )
    assert out == {"foo": "bar"}
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/ingest/test_frontmatter_extractor.py -v
```

Expected: ImportError on `harbor_clerk.ingest.metadata_extractors.frontmatter`.

- [ ] **Step 4: Implement the extractor**

Create `src/harbor_clerk/ingest/metadata_extractors/frontmatter.py`:

```python
# src/harbor_clerk/ingest/metadata_extractors/frontmatter.py
"""FrontmatterExtractor — parses YAML frontmatter from markdown files.

Obsidian, MkDocs, Jekyll, Hugo all use the `---`-delimited YAML header
pattern at the top of markdown files. The extractor parses that header
and returns it as the metadata dict. For non-markdown files (PDFs, .eml,
.docx, etc.) it returns None.

Frontmatter values are normalized to JSON-serializable scalars: YAML
dates → ISO strings at the boundary (the doc_metadata column is JSONB,
which doesn't have a native date type — keeping dates as strings makes
filter values comparable without further parsing).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import frontmatter

log = logging.getLogger(__name__)

_MARKDOWN_EXTENSIONS = (".md", ".markdown")


class FrontmatterExtractor:
    """YAML frontmatter parser for markdown files."""

    name = "frontmatter"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        # Identify markdown via canonical_filename first, fall back to source_path
        filename = getattr(doc, "canonical_filename", None) or source_path or ""
        if not any(filename.lower().endswith(ext) for ext in _MARKDOWN_EXTENSIONS):
            return None

        try:
            parsed = frontmatter.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            log.warning(
                "frontmatter parse failed for doc %s: %s",
                getattr(doc, "doc_id", "<unknown>"),
                exc,
            )
            return None

        if not parsed.metadata:
            return None

        return _jsonify(parsed.metadata)


def _jsonify(obj: Any) -> Any:
    """Recursively convert YAML-parsed values to JSON-serializable equivalents.
    Mainly: date/datetime → ISO string."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj
```

- [ ] **Step 5: Register the extractor**

Edit `src/harbor_clerk/ingest/metadata_extractors/__init__.py` — extend the EXTRACTORS list:

```python
from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor  # noqa: E402
from harbor_clerk.ingest.metadata_extractors.tika_metadata import TikaMetadataExtractor  # noqa: E402

EXTRACTORS: list[MetadataExtractor] = [
    TikaMetadataExtractor(),
    FrontmatterExtractor(),
]
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/ingest/ -v
```

Expected: all framework + Tika + frontmatter tests pass (17 total).

- [ ] **Step 7: Lint + format**

```bash
uv run ruff check src/harbor_clerk/ingest/ tests/ingest/ pyproject.toml
uv run ruff format --check src/harbor_clerk/ingest/ tests/ingest/
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/harbor_clerk/ingest/metadata_extractors/frontmatter.py src/harbor_clerk/ingest/metadata_extractors/__init__.py tests/ingest/test_frontmatter_extractor.py
git commit -m "feat(ingest): FrontmatterExtractor — YAML frontmatter from markdown" \
  -m "Parses --- delimited YAML header from .md/.markdown files via python-frontmatter (1.1+). Returns None for non-markdown or markdown without frontmatter. Malformed YAML logs a warning and returns None (defensive — framework would catch the exception anyway). YAML dates normalised to ISO strings since JSONB lacks a native date type and filter values need to be comparable as-is." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Sidecar extractor

**Files:**
- Create: `src/harbor_clerk/ingest/metadata_extractors/sidecar.py`
- Modify: `src/harbor_clerk/ingest/metadata_extractors/__init__.py` (register)
- Create: `tests/ingest/test_sidecar_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/ingest/test_sidecar_extractor.py`:

```python
"""SidecarExtractor — loads <stem>.json next to source_path."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from harbor_clerk.ingest.metadata_extractors.sidecar import SidecarExtractor


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str


def test_sidecar_extractor_loads_json_next_to_source_path(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("invoice body text")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme", "total_usd": 1234.56}))

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"invoice body text",
        source_path=str(doc_file),
    )
    assert out == {"vendor": "Acme", "total_usd": 1234.56}


def test_sidecar_extractor_returns_none_when_no_sidecar_exists(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_returns_none_when_source_path_unset():
    """Legacy uploaded docs without a watched-folder source_path → no sidecar."""
    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=None,
    )
    assert out is None


def test_sidecar_extractor_logs_and_returns_none_on_malformed_json(tmp_path: Path):
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text("{ not valid json")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_returns_none_for_empty_sidecar(tmp_path: Path):
    """An empty JSON object {} → return None so the namespace isn't written."""
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text("{}")

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None


def test_sidecar_extractor_rejects_non_object_top_level(tmp_path: Path):
    """A top-level list or string in the sidecar is not a valid metadata
    dict — log warning, return None."""
    doc_file = tmp_path / "0001_invoice.txt"
    doc_file.write_text("body")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text('["not", "an", "object"]')

    extractor = SidecarExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="i"),
        raw_bytes=b"body",
        source_path=str(doc_file),
    )
    assert out is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/ingest/test_sidecar_extractor.py -v
```

Expected: ImportError on the sidecar module.

- [ ] **Step 3: Implement**

Create `src/harbor_clerk/ingest/metadata_extractors/sidecar.py`:

```python
# src/harbor_clerk/ingest/metadata_extractors/sidecar.py
"""SidecarExtractor — loads <stem>.json next to source_path.

For docs ingested via watched folders (synthetic test corpus, real-world
power users with curated metadata files), look for a JSON file with the
same stem as the source file and load it as the metadata dict.

Example: a watched folder containing
  invoices/2024-Q3/INV-001.pdf
  invoices/2024-Q3/INV-001.json
would surface the INV-001.json contents under the 'sidecar' namespace on
the Document for INV-001.pdf.

Returns None for docs without source_path (legacy uploads), without a
matching sidecar file, with malformed JSON, with an empty object, or
with a non-object top-level JSON value (list/string/etc).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SidecarExtractor:
    """Loads <stem>.json next to the source file."""

    name = "sidecar"

    def extract(self, *, doc, raw_bytes: bytes, source_path: str | None) -> dict | None:
        if not source_path:
            return None
        src = Path(source_path)
        sidecar = src.with_suffix(".json")
        if not sidecar.is_file():
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "sidecar load failed for doc %s (%s): %s",
                getattr(doc, "doc_id", "<unknown>"),
                sidecar,
                exc,
            )
            return None

        if not isinstance(payload, dict):
            log.warning(
                "sidecar for doc %s is not a JSON object (got %s); ignoring",
                getattr(doc, "doc_id", "<unknown>"),
                type(payload).__name__,
            )
            return None

        return payload or None
```

- [ ] **Step 4: Register**

Edit `src/harbor_clerk/ingest/metadata_extractors/__init__.py`:

```python
from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor  # noqa: E402
from harbor_clerk.ingest.metadata_extractors.sidecar import SidecarExtractor  # noqa: E402
from harbor_clerk.ingest.metadata_extractors.tika_metadata import TikaMetadataExtractor  # noqa: E402

EXTRACTORS: list[MetadataExtractor] = [
    TikaMetadataExtractor(),
    FrontmatterExtractor(),
    SidecarExtractor(),
]
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/ingest/ -v
```

Expected: all framework + Tika + frontmatter + sidecar tests pass (23 total).

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check src/harbor_clerk/ingest/ tests/ingest/
uv run ruff format --check src/harbor_clerk/ingest/ tests/ingest/
```

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/ingest/metadata_extractors/sidecar.py src/harbor_clerk/ingest/metadata_extractors/__init__.py tests/ingest/test_sidecar_extractor.py
git commit -m "feat(ingest): SidecarExtractor — loads <stem>.json next to source_path" \
  -m "For docs ingested via watched folders that happen to have a JSON sidecar with the same stem (synthetic test corpus + real-world power users with curated metadata files). Returns None for docs without source_path, without a matching sidecar, with malformed JSON, with an empty object, or with a non-object top-level JSON value." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Wire extractors into the extract stage

**Files:**
- Modify: `src/harbor_clerk/worker/stages/extract.py` — call `run_all()` and persist
- Modify: `src/harbor_clerk/worker/markdown_extract.py` — strip frontmatter from body before chunking
- Create: `tests/worker/test_extract_metadata_persistence.py`

- [ ] **Step 1: Write the failing test for the extract stage**

Create `tests/worker/test_extract_metadata_persistence.py`:

```python
"""End-to-end: the extract stage runs the extractor framework and persists
results to Document.doc_metadata. Uses a tmp sidecar to avoid hitting Tika
in the unit-test path."""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus


async def test_extract_stage_writes_sidecar_metadata_to_doc(db_session, tmp_path: Path):
    # Set up doc + source file + sidecar
    source = tmp_path / "0001_invoice.txt"
    source.write_text("Invoice body text")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme", "total_usd": 100}))

    doc = Document(
        title="0001_invoice",
        canonical_filename="0001_invoice.txt",
        status="active",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.queued,
        mime_type="text/plain",
        source_path=str(source),
    )
    db_session.add(doc)
    await db_session.commit()
    doc_id = doc.doc_id

    # Run extract — mock Tika so we don't need a live Tika in tests
    from harbor_clerk.worker.stages import extract

    with patch.object(extract, "_extract_via_tika", return_value=[(1, "Invoice body text")]):
        with patch(
            "harbor_clerk.ingest.metadata_extractors.tika_metadata.httpx.Client"
        ) as mock_tika_meta:
            # Make Tika /meta return nothing — only sidecar will write metadata
            mock_tika_meta.return_value.__enter__.return_value.put.return_value.status_code = 200
            mock_tika_meta.return_value.__enter__.return_value.put.return_value.json.return_value = {}
            extract.run_extract(doc_id)

    # Verify doc.doc_metadata has the sidecar namespace
    await db_session.expire_all()
    doc = (await db_session.execute(select(Document).where(Document.doc_id == doc_id))).scalar_one()
    assert "sidecar" in doc.doc_metadata
    assert doc.doc_metadata["sidecar"] == {"vendor": "Acme", "total_usd": 100}
    assert "_source_provenance" in doc.doc_metadata
    assert "sidecar" in doc.doc_metadata["_source_provenance"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/worker/test_extract_metadata_persistence.py -v
```

Expected: assertion failure — doc.doc_metadata is still `{}` because the extract stage doesn't call run_all() yet.

- [ ] **Step 3: Wire `run_all()` into the extract stage**

Edit `src/harbor_clerk/worker/stages/extract.py`. Near the top, add the import:

```python
from harbor_clerk.ingest.metadata_extractors import run_all as run_metadata_extractors
```

Then find the `run_extract` function. After the body text + page extraction completes and BEFORE the session.commit() that persists Document changes, insert the metadata call. The exact location depends on the current file structure — locate the section that updates `doc.extracted_chars`, `doc.has_text_layer`, etc., and add the metadata block adjacent to those updates:

```python
# Extract metadata (Tika headers, frontmatter, sidecar). Runs after body
# extraction so the Tika /meta call can be skipped cheaply if the body
# extraction already failed (data is the raw_bytes available here).
try:
    metadata = run_metadata_extractors(doc=doc, raw_bytes=data, source_path=doc.source_path)
    if metadata:
        doc.doc_metadata = metadata
except Exception as exc:
    # Extractor framework swallows individual extractor failures already;
    # this catch is for catastrophic framework-level failures (import
    # errors, etc). Log and continue — metadata is additive.
    logger.warning("metadata extraction framework failed for doc %s: %s", doc_id, exc)
```

(Use the existing `data` variable that holds the raw bytes loaded for body extraction. Naming may vary slightly — search for the variable used by `_extract_via_tika`.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/worker/test_extract_metadata_persistence.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Write the failing test for frontmatter stripping**

Create `tests/worker/test_markdown_frontmatter_stripping.py`:

```python
"""Markdown extract path strips YAML frontmatter from body text before
chunking, so the chunks/embedding/FTS don't include raw YAML noise."""

from harbor_clerk.worker.markdown_extract import strip_frontmatter


def test_strip_frontmatter_removes_yaml_block():
    body = """---
title: Notes
tags: [a, b]
---

# Heading

Body content.
"""
    assert (
        strip_frontmatter(body)
        == """# Heading

Body content.
"""
    )


def test_strip_frontmatter_passes_through_unchanged_without_frontmatter():
    body = "# Just a heading\n\nNo frontmatter.\n"
    assert strip_frontmatter(body) == body


def test_strip_frontmatter_passes_through_unchanged_when_yaml_is_malformed():
    """Malformed YAML → don't risk losing body content; pass through unchanged.
    Metadata extraction logs the parse failure separately."""
    body = """---
title: Meeting
unclosed_list: [a, b
---

content
"""
    assert strip_frontmatter(body) == body
```

- [ ] **Step 6: Run test to verify it fails**

```bash
uv run pytest tests/worker/test_markdown_frontmatter_stripping.py -v
```

Expected: ImportError on `strip_frontmatter`.

- [ ] **Step 7: Implement `strip_frontmatter`**

Edit `src/harbor_clerk/worker/markdown_extract.py`. Add at module scope:

```python
import frontmatter as _frontmatter


def strip_frontmatter(text: str) -> str:
    """Remove the leading YAML frontmatter block from markdown text.

    If the text doesn't start with `---\\n` or the frontmatter is malformed,
    return the text unchanged. Metadata extraction (FrontmatterExtractor in
    src/harbor_clerk/ingest/metadata_extractors/frontmatter.py) logs the
    parse failure separately, so this is a pure "drop the YAML if it parses
    cleanly, else pass through" operation.
    """
    try:
        parsed = _frontmatter.loads(text)
    except Exception:
        return text
    if not parsed.metadata:
        # No frontmatter detected → return as-is
        return text
    # frontmatter.loads.content strips the YAML header; restore a trailing
    # newline if the original had one (defensive — most markdown files do)
    content = parsed.content
    if text.endswith("\n") and not content.endswith("\n"):
        content += "\n"
    return content
```

Then find where the markdown body text is chunked in the same file (search for "chunk" or the function that processes markdown after extraction) and call `strip_frontmatter()` on the body before chunking. The exact integration point depends on the file's structure; add the call where the body string is finalized for the chunker.

- [ ] **Step 8: Run tests**

```bash
uv run pytest tests/worker/test_markdown_frontmatter_stripping.py tests/worker/test_extract_metadata_persistence.py -v
```

Expected: 4 passed.

- [ ] **Step 9: Lint + format**

```bash
uv run ruff check src/harbor_clerk/worker/stages/extract.py src/harbor_clerk/worker/markdown_extract.py tests/worker/
uv run ruff format --check src/harbor_clerk/worker/stages/extract.py src/harbor_clerk/worker/markdown_extract.py tests/worker/
```

- [ ] **Step 10: Commit**

```bash
git add src/harbor_clerk/worker/stages/extract.py src/harbor_clerk/worker/markdown_extract.py tests/worker/test_extract_metadata_persistence.py tests/worker/test_markdown_frontmatter_stripping.py
git commit -m "feat(worker): extract stage runs metadata extractors + strips frontmatter" \
  -m "Extract stage now calls run_metadata_extractors() after body extraction; results land on Document.doc_metadata. Markdown body text gets its YAML frontmatter stripped before chunking so retrieval doesn't include raw 'tags: [foo, bar]' noise — the same fields are still accessible via the frontmatter metadata namespace and via metadata_filter." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `kb_search` `metadata_filter` parameter — query layer

**Files:**
- Modify: `src/harbor_clerk/search.py` — extend `hybrid_search` with `metadata_filter`
- Create: `tests/test_search_metadata_filter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_metadata_filter.py`:

```python
"""kb_search metadata_filter — JSONB @> containment + ? existence fallback."""

import uuid

from sqlalchemy import text

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search


async def _seed_doc(db_session, *, title: str, metadata: dict) -> Document:
    """Helper: insert a Document with one trivial Chunk so FTS can match."""
    doc = Document(
        title=title,
        status="active",
        sha256=uuid.uuid4().bytes,  # 16 bytes — pad to 32
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata,
    )
    # sha256 must be exactly 32 bytes
    doc.sha256 = (uuid.uuid4().bytes + uuid.uuid4().bytes)[:32]
    db_session.add(doc)
    await db_session.flush()
    chunk = Chunk(
        doc_id=doc.doc_id,
        chunk_idx=0,
        text="governing law jurisdiction terms",
        language="english",
    )
    db_session.add(chunk)
    # Manually populate fts_en since the generated column needs the row
    # to exist; flushing covers this.
    await db_session.flush()
    return doc


async def test_metadata_filter_pins_doc_by_scalar_field(db_session):
    """A scalar filter on a scalar metadata value matches via @> containment."""
    pinnacle = await _seed_doc(
        db_session,
        title="vendor-pinnacle-A",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    other = await _seed_doc(
        db_session,
        title="vendor-other",
        metadata={"sidecar": {"vendor": "Other Vendor, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="governing law",
        k=10,
        metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions, LLC"},
    )
    cited_doc_ids = {hit.doc_id for hit in result.hits}
    assert pinnacle.doc_id in cited_doc_ids
    assert other.doc_id not in cited_doc_ids


async def test_metadata_filter_pins_doc_by_list_value_via_existence(db_session):
    """A scalar filter on a list-valued metadata field matches if the list
    contains the scalar (JSONB ? operator)."""
    tagged = await _seed_doc(
        db_session,
        title="tagged-alpha",
        metadata={"frontmatter": {"tags": ["alpha", "beta"]}},
    )
    untagged = await _seed_doc(
        db_session,
        title="untagged",
        metadata={"frontmatter": {"tags": ["gamma"]}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="governing law",
        k=10,
        metadata_filter={"frontmatter.tags": "alpha"},
    )
    cited = {hit.doc_id for hit in result.hits}
    assert tagged.doc_id in cited
    assert untagged.doc_id not in cited


async def test_metadata_filter_combines_multiple_keys_with_and(db_session):
    """Two filter keys → AND. Both must match for a doc to be returned."""
    a = await _seed_doc(
        db_session,
        title="vendor-pinnacle-24mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    b = await _seed_doc(
        db_session,
        title="vendor-pinnacle-12mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="governing law",
        k=10,
        metadata_filter={
            "sidecar.vendor": "Pinnacle Tech Solutions, LLC",
            "sidecar.term_months": 24,
        },
    )
    cited = {hit.doc_id for hit in result.hits}
    assert a.doc_id in cited
    assert b.doc_id not in cited


async def test_metadata_filter_empty_match_returns_no_hits(db_session):
    """A filter that matches no docs returns an empty result set (not an error)."""
    await _seed_doc(
        db_session,
        title="vendor-pinnacle",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC"}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="governing law",
        k=10,
        metadata_filter={"sidecar.vendor": "Nonexistent Vendor Co"},
    )
    assert result.hits == []


async def test_metadata_filter_absent_falls_back_to_existing_behavior(db_session):
    """When metadata_filter is None, behavior is unchanged."""
    doc = await _seed_doc(db_session, title="any", metadata={})
    await db_session.commit()
    result = await hybrid_search(db_session, query="governing law", k=10, metadata_filter=None)
    assert any(hit.doc_id == doc.doc_id for hit in result.hits)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_search_metadata_filter.py -v
```

Expected: `TypeError: hybrid_search() got an unexpected keyword argument 'metadata_filter'`.

- [ ] **Step 3: Extend `hybrid_search`**

Edit `src/harbor_clerk/search.py`. Update the `hybrid_search` signature and the doc-filter assembly block. Find the existing signature and add `metadata_filter`:

```python
async def hybrid_search(
    session: AsyncSession,
    query: str,
    k: int = 10,
    doc_id: uuid.UUID | None = None,
    offset: int = 0,
    *,
    doc_ids: list[uuid.UUID] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> SearchResult:
```

(Add `from typing import Any` at the top if not present.)

In the body, after the existing `doc_conditions` block, add metadata filter translation:

```python
    # Metadata filter translation. Each "<namespace>.<key>": value pair
    # becomes either:
    #   - JSONB @> containment: doc_metadata @> '{"ns": {"key": value}}'
    #   - JSONB ? existence (for list-valued targets): doc_metadata->'ns'->'key' ? 'value'
    # We try containment first; if no docs match, retry with the existence
    # operator since the metadata value might be list-valued. This handles
    # both `{tags: "alpha"}` and `{tags: ["alpha", "beta"]}` filter shapes
    # without the caller needing to know which the corpus uses.
    if metadata_filter:
        for path, value in metadata_filter.items():
            ns, _, key = path.partition(".")
            if not ns or not key:
                raise ValueError(
                    f"metadata_filter keys must be 'namespace.key', got {path!r}"
                )
            # Containment OR existence:
            #   doc_metadata @> '{"ns": {"key": value}}' (scalar metadata)
            #   OR doc_metadata->'ns'->'key' ? :value::text (list metadata; needs string key)
            containment = Document.doc_metadata.op("@>")(
                func.cast({ns: {key: value}}, JSONB)
            )
            if isinstance(value, str):
                existence = Document.doc_metadata[ns][key].op("?")(value)
                doc_conditions.append(or_(containment, existence))
            else:
                doc_conditions.append(containment)
```

Add the imports at the top of `search.py`:

```python
from sqlalchemy import or_, func
from sqlalchemy.dialects.postgresql import JSONB
```

(Some may already be imported; just ensure all three are available.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_search_metadata_filter.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Lint + format**

```bash
uv run ruff check src/harbor_clerk/search.py tests/test_search_metadata_filter.py
uv run ruff format --check src/harbor_clerk/search.py tests/test_search_metadata_filter.py
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/search.py tests/test_search_metadata_filter.py
git commit -m "feat(search): kb_search metadata_filter via JSONB @> + ? fallback" \
  -m "hybrid_search gains metadata_filter dict[str, Any] | None. Each 'namespace.key': value pair translates to a JSONB containment (@>) OR existence (?) clause on Document.doc_metadata, AND-ed with other filter keys. The OR-with-existence handles list-valued metadata fields (e.g. tags: ['alpha', 'beta']) being filtered by a scalar value without the caller needing to know the metadata shape." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `kb_search` + `kb_get_document` MCP wiring

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — add `metadata_filter` to `kb_search`; add `metadata` field to `kb_get_document` response
- Create: `tests/test_mcp_metadata_filter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_metadata_filter.py`:

```python
"""MCP wiring for kb_search metadata_filter and kb_get_document metadata field."""

import json
import uuid

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus


async def _seed(db_session, *, title: str, metadata: dict) -> Document:
    doc = Document(
        title=title,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_idx=0, text="hello world", language="english"))
    await db_session.flush()
    return doc


async def test_kb_search_passes_metadata_filter_through(client, admin_user, admin_token, db_session):
    """The MCP kb_search tool forwards metadata_filter to hybrid_search."""
    target = await _seed(db_session, title="target", metadata={"sidecar": {"vendor": "Acme"}})
    other = await _seed(db_session, title="other", metadata={"sidecar": {"vendor": "Beta"}})
    await db_session.commit()

    # The MCP server is invoked over its tool interface, but we can call
    # the function directly with the test's principal context. The
    # production path uses HTTP/MCP; for unit-test purposes we exercise
    # the function with the admin's session context.
    from harbor_clerk.mcp_server import kb_search

    # ...In the test, the function pulls principal from contextvar; the
    # admin fixture sets it up. Detailed setup follows the same pattern
    # as the existing test_mcp_*.py files.
    pass  # See implementation note below — this test requires the existing MCP-test fixtures
```

(Note: MCP tool tests in this codebase are nuanced because the principal is read from a contextvar. The implementation of this test should follow the existing pattern from any `tests/test_mcp_*.py` file. If no such file exists yet, the test can be simpler — directly call `hybrid_search` with the filter and verify the output, which is what Task 7's tests already do. In that case, this task's test focuses on Step 2: kb_get_document returning metadata.)

Replace the `pass` body with a simpler test that focuses on what's new:

```python
async def test_kb_get_document_includes_doc_metadata(client, admin_user, admin_token, db_session):
    """kb_get_document response includes the metadata dict so models can
    inspect available filter keys."""
    target = await _seed(
        db_session,
        title="invoice-acme",
        metadata={"sidecar": {"vendor": "Acme", "total_usd": 100}},
    )
    await db_session.commit()

    # Invoke the MCP tool function directly
    from harbor_clerk.mcp_server import kb_get_document, set_principal_for_test

    set_principal_for_test(admin_user)  # see implementation note
    try:
        raw = await kb_get_document(doc_id=str(target.doc_id))
    finally:
        set_principal_for_test(None)

    parsed = json.loads(raw)
    assert parsed.get("metadata") == {"sidecar": {"vendor": "Acme", "total_usd": 100}}
```

If `set_principal_for_test` doesn't exist, add a tiny test helper to `mcp_server.py` (just a thin wrapper around the contextvar set/reset), or use whatever fixture pattern existing MCP tests use. (Implementation discretion: if no MCP-tool tests exist, this test can be deferred to integration testing — note in the commit message.)

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_mcp_metadata_filter.py -v
```

Expected: failure (kb_get_document doesn't return metadata; kb_search doesn't accept metadata_filter).

- [ ] **Step 3: Add `metadata_filter` to `kb_search`**

Edit `src/harbor_clerk/mcp_server.py`. Find the `kb_search` function signature (around line 546). Add `metadata_filter` to the params:

```python
async def kb_search(
    query: str,
    k: int = 10,
    offset: int = 0,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict | None = None,
    faceted: bool = False,
) -> str:
```

Extend the docstring's "Filters" section:

```python
    Filters (all optional):
      doc_id: restrict to a single document (mutually exclusive with doc_ids)
      doc_ids: restrict to multiple documents (list of UUIDs, max 50)
      after: only documents updated at or after this ISO datetime
      before: only documents updated before this ISO datetime
      language: chunk language filter ("english" or "french")
      mime_type: document MIME type filter (e.g. "application/pdf")
      metadata_filter: dict of {"namespace.key": value} pairs to match against
        Document.metadata. Use to disambiguate when multiple candidates share
        text but differ on a structured field. Example:
        metadata_filter={"sidecar.vendor": "Acme", "sidecar.term_months": 24}.
        Inspect a document's available metadata via kb_get_document.
```

Find the call to `hybrid_search` further down in the function body. Add `metadata_filter=metadata_filter` to its keyword arguments.

- [ ] **Step 4: Add `metadata` to `kb_get_document` response**

Find `kb_get_document` in `mcp_server.py` (search for `def kb_get_document` or `@mcp.tool` near "Full document"). In the response assembly, add the metadata field:

```python
    # In the dict that builds the response:
    "metadata": doc.doc_metadata,
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_mcp_metadata_filter.py -v
```

Expected: 1 passed (or both if the kb_search MCP test was kept).

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_metadata_filter.py
uv run ruff format --check src/harbor_clerk/mcp_server.py tests/test_mcp_metadata_filter.py
```

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_metadata_filter.py
git commit -m "feat(mcp): kb_search.metadata_filter + kb_get_document.metadata" \
  -m "kb_search now accepts metadata_filter (forwarded to hybrid_search). kb_get_document response includes the document's metadata dict so models can inspect available filter keys before crafting a filter. Tool description for the new param is a placeholder; PR-D's full description rewrite pass will polish it." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Re-ingest script for existing dev/test corpora

**Files:**
- Create: `scripts/reextract_metadata.py`

- [ ] **Step 1: Write the script**

Create `scripts/reextract_metadata.py`:

```python
#!/usr/bin/env python3
"""One-shot script: re-extract metadata for all active documents in the
current HC instance. Idempotent — running twice produces the same metadata.

Use this after applying the documents.metadata migration to backfill
metadata for documents that were ingested before the extractor framework
existed.

Doesn't change body text or chunks — only writes to Document.doc_metadata.
For documents in source-path-backed watched folders, reads raw bytes from
disk. For legacy uploads with stored objects, fetches from storage.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from harbor_clerk.db import get_session_factory
from harbor_clerk.ingest.metadata_extractors import run_all as run_metadata_extractors
from harbor_clerk.models import Document
from harbor_clerk.storage import get_storage

log = logging.getLogger("reextract_metadata")


async def reextract_all(*, dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (processed, updated, skipped)."""
    session_factory = get_session_factory()
    storage = get_storage()

    processed = updated = skipped = 0
    async with session_factory() as session:
        result = await session.execute(select(Document).where(Document.status == "active"))
        docs = result.scalars().all()

        for doc in docs:
            processed += 1
            # Load raw bytes from source_path (watched folder) or storage (legacy upload)
            raw_bytes: bytes | None = None
            if doc.source_path and Path(doc.source_path).is_file():
                raw_bytes = Path(doc.source_path).read_bytes()
            elif doc.original_bucket and doc.original_object_key:
                try:
                    raw_bytes = storage.get_object(doc.original_bucket, doc.original_object_key)
                except Exception as exc:
                    log.warning("storage fetch failed for doc %s: %s", doc.doc_id, exc)
                    skipped += 1
                    continue
            else:
                log.info("doc %s has no source_path or storage object; skipping", doc.doc_id)
                skipped += 1
                continue

            metadata = run_metadata_extractors(
                doc=doc,
                raw_bytes=raw_bytes,
                source_path=doc.source_path,
            )
            if metadata == doc.doc_metadata:
                continue  # idempotent no-op
            if dry_run:
                log.info("would update doc %s: %s", doc.doc_id, metadata)
                updated += 1
                continue
            doc.doc_metadata = metadata
            updated += 1

        if not dry_run:
            await session.commit()

    return processed, updated, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="don't commit changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    processed, updated, skipped = asyncio.run(reextract_all(dry_run=args.dry_run))
    log.info(
        "reextract_metadata complete: processed=%d updated=%d skipped=%d (dry_run=%s)",
        processed,
        updated,
        skipped,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check it imports cleanly**

```bash
uv run python -c "from scripts.reextract_metadata import reextract_all; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Lint + format**

```bash
uv run ruff check scripts/reextract_metadata.py
uv run ruff format --check scripts/reextract_metadata.py
```

- [ ] **Step 4: Commit**

```bash
git add scripts/reextract_metadata.py
git commit -m "chore(scripts): reextract_metadata.py — one-shot metadata backfill" \
  -m "For existing dev/test corpora (and any production user who wants to populate metadata on docs ingested before PR-F). Idempotent. Reads raw bytes from source_path (watched folders) or storage (legacy uploads). --dry-run flag for safe inspection." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Full-suite regression sanity check

**No new files or code — verification only.**

- [ ] **Step 1: Run the full pytest suite**

```bash
uv run pytest tests/ -q 2>&1 | tail -10
```

Expected: existing tests pass, no regressions from the model/search/extract changes. The pre-existing spaCy `test_entity_overlap_english` may still fail (unrelated; missing model in test venv). Roughly 30+ new tests for PR-F land on top of the existing suite.

- [ ] **Step 2: Lint + format on every changed file**

```bash
uv run ruff check src/harbor_clerk/ tests/ scripts/
uv run ruff format --check src/harbor_clerk/ tests/ scripts/
```

Expected: clean across the touched tree.

- [ ] **Step 3: Frontend type-check** (no frontend changes in PR-F, but verifying nothing leaked):

```bash
cd frontend && npm run lint && npx tsc --noEmit
```

Expected: clean (or only pre-existing warnings).

- [ ] **Step 4: No commit needed** — verification gate.

If any check fails, return to the relevant earlier task and fix; don't proceed to Task 11 (live validation) on a yellow signal.

---

## Task 11: Live validation against the synthetic corpus

**Prerequisites:**
- HC must be running (`http://localhost:8100`).
- PR-RSS's "Reprocess (no summaries)" button merged on main (lets us re-ingest fast without burning LLM spend on summaries that aren't changing).
- Synthetic corpus already ingested in HC (per CLAUDE.md, lives under `~/Library/Application Support/Harbor Clerk/test-corpora/synthetic/ingest`).

- [ ] **Step 1: Trigger the metadata extraction pass via Reprocess (no summaries)**

Open System Maintenance in HC's UI, click "Reprocess (no summaries)", confirm. Wait for the pipeline to drain (watch Observatory queue widget go to 0).

Alternative (CLI): hit the PR-RSS endpoint directly:

```bash
curl -X POST -H "Authorization: Bearer $HC_API_KEY" \
  http://localhost:8100/api/system/reprocess-all-skip-summarize
```

- [ ] **Step 2: Spot-check the synthetic corpus's metadata**

Pick a few synthetic docs (one each of invoice, vendor_contract, policy_doc, employee_handbook) and verify their metadata via `kb_get_document`. Expected:

- **Invoice docs** (`0001_invoice.json` etc.) → `sidecar` namespace with `vendor`, `invoice_number`, `total_usd`, `date`.
- **Vendor contract docs** → `sidecar` with `vendor`, `term_months`, `monthly_fee_usd`, `governing_law`.
- **Policy docs** → `sidecar` with `policy_name`, `version`, `effective_date`, `owner`.

```bash
# Pull a doc's metadata via MCP
hc_api_key=$HC_API_KEY  # Synthetic-scoped key
curl -s -H "Authorization: Bearer $hc_api_key" \
  "http://localhost:8100/api/docs?q=0131_vendor_contract" | jq '.[0].metadata'
```

Expected: `{"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24, ...}, "_source_provenance": {"sidecar": "2026-..."}}`.

If the metadata is empty for synthetic docs, the sidecar extractor isn't finding the `.json` sidecars — debug the source_path being passed.

- [ ] **Step 3: Spot-check non-synthetic metadata sources**

If the user's HC has Obsidian-vault markdown files in a watched folder, pick one with frontmatter and verify the `frontmatter` namespace populates. If they have PDFs, verify `tika` namespace shows `author`, `title`, `page_count` where Tika supplies them. Document what's populated (and what isn't) for the PR description.

- [ ] **Step 4: Validate `metadata_filter` end-to-end**

Run a hand-crafted MCP search that exercises the disambiguation case:

```bash
curl -X POST http://localhost:8100/mcp/mcp \
  -H "Authorization: Bearer $HC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "kb_search",
      "arguments": {
        "query": "governing law",
        "metadata_filter": {"sidecar.vendor": "Pinnacle Tech Solutions, LLC", "sidecar.term_months": 24},
        "k": 3
      }
    }
  }' | jq '.result.content[0].text' | jq '.hits[] | {doc_title, score}'
```

Expected: returns chunks from the specific Pinnacle 24-month contract (synthetic doc `0131_vendor_contract`), not from `0149` or other Pinnacle docs with different terms.

- [ ] **Step 5: Record findings for PR description**

Note in `/tmp/pr-f-validation-notes.md`:
- Which extractors fired on which doc types
- Field surface across mime types (Tika is uneven — DOCX rich, PDF sparse, .eml structured)
- Any surprise/dropped fields
- The exact `metadata_filter` query that pinned the Pinnacle 24-month contract

These notes go into the PR body.

---

## Task 12: Self-review, fresh-eyes review, open PR

- [ ] **Step 1: Diff summary against `main`**

```bash
git fetch origin main --quiet
git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD
```

Expected: ~10 commits, the file structure listed at the top of this plan.

- [ ] **Step 2: Final lint + format pass**

```bash
uv run ruff check src/harbor_clerk/ tests/ scripts/
uv run ruff format --check src/harbor_clerk/ tests/ scripts/
```

- [ ] **Step 3: Dispatch fresh-eyes code reviewer** (per MEMORY.md standing directive — minimal prompt, no carve-outs)

Use the Agent tool with `subagent_type: feature-dev:code-reviewer` and a minimal prompt: "Review the diff `git diff origin/main..HEAD` on branch `feat/document-metadata-extractors`. Spec is at `docs/superpowers/specs/2026-05-24-document-metadata-extractors-design.md`. Identify bugs, security issues, design problems with ≥80 confidence."

Address ≥80-confidence findings inline.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/document-metadata-extractors
```

- [ ] **Step 5: Open the PR with metrics + validation notes from Task 11**

Use the validation notes from `/tmp/pr-f-validation-notes.md` to write the PR body. Use `gh pr create --base main --head feat/document-metadata-extractors --title "feat(ingest): document metadata extractors + kb_search metadata_filter" --body-file /tmp/pr-f-body.md`.

PR body should cover:
- What the extractors capture (Tika, frontmatter, sidecar)
- `metadata_filter` shape + the smart `@>`/`?` fallback
- Validation results from Task 11 (Pinnacle 24-month pinned, etc.)
- Known limitations (only doc-types we tested for; future PRs will add folder-path metadata, LLM-extraction, frontend display, operators)
- Pointers to upstream follow-ups: PR-D (tool description rewrites + kb_search response enhancements), PR-E (kb_verify_identifier + kb_documents_by_date), PR-G (harness audit + cross-judge sensitivity)

---

## Self-Review Notes (for the agentic worker)

**1. Spec coverage:** Every section of `2026-05-24-document-metadata-extractors-design.md` has a corresponding task:
- Schema (Document.metadata JSONB + GIN) → Task 1
- Extractor framework (Protocol + run_all) → Task 2
- TikaMetadataExtractor → Task 3
- FrontmatterExtractor → Task 4
- SidecarExtractor → Task 5
- Extract stage wiring + frontmatter strip → Task 6
- kb_search metadata_filter query layer → Task 7
- kb_search + kb_get_document MCP wiring → Task 8
- Re-ingest script → Task 9
- Regression + ruff → Task 10
- Live validation → Task 11
- Fresh-eyes review + PR → Task 12

The 4 locked-in decisions from spec review (frontmatter stripped, kb_get_document returns metadata, exact-match-only operators, smart @>/? fallback) all have implementing tasks.

**2. Placeholder scan:** No `TBD` / "TODO" / "implement later" in any code step. Task 8's note about MCP-tool test fixtures explicitly says to follow existing patterns and fall back to direct-function testing if MCP fixtures don't exist — that's an honest "implementation discretion" point, not a placeholder.

**3. Type consistency:**
- `Document.doc_metadata` (Python attr) ↔ `metadata` (PostgreSQL column) — consistent across Task 1 (definition), Task 6 (extract stage assignment), Task 7 (query layer), Task 8 (MCP response), Task 9 (re-ingest script).
- `MetadataExtractor` Protocol's `extract(*, doc, raw_bytes, source_path) -> dict | None` signature is consistent in Task 2 (definition) and Tasks 3–5 (all three implementations).
- `metadata_filter` parameter shape `dict[str, Any] | None` (or `dict | None` in MCP-layer) is consistent across Task 7 (`hybrid_search` signature) and Task 8 (MCP `kb_search` signature).
- Extractor `name` field values (`"tika"`, `"frontmatter"`, `"sidecar"`) are consistent across the extractor implementations and the test assertions on metadata namespaces.
