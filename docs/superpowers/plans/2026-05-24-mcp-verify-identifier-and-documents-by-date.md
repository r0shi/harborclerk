# PR-E: `kb_verify_identifier` + `kb_documents_by_date` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two structured-lookup MCP tools — `kb_verify_identifier` (proactive fabrication-prevention) and `kb_documents_by_date` (date-bounded lookups) — plus matching `harbor-clerk` CLI subcommands.

**Architecture:** Both tools live in a single new module `src/harbor_clerk/mcp_lookup_tools.py` and share a date-discovery helper `src/harbor_clerk/metadata_dates.py`. `verify_identifier` reuses PR-D's `_find_differing_metadata_fields()` for the discriminating-fields projection. `documents_by_date` builds a single SQL query with a `COALESCE` over JSONB extractors for the effective-date sort key. Each MCP tool gets a peer CLI subcommand matching PR #397's 1:1 contract.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async + asyncpg, PostgreSQL 18 + pgvector, FastMCP, pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-05-24-mcp-verify-identifier-and-documents-by-date-design.md`

---

## File Structure

**New files:**
- `src/harbor_clerk/metadata_dates.py` — `effective_date(doc) -> tuple[datetime | None, str]` helper.
- `src/harbor_clerk/mcp_lookup_tools.py` — `verify_identifier()` + `documents_by_date()` async functions.
- `src/harbor_clerk/cli/commands/verify_identifier.py` — `harbor-clerk verify-identifier` subcommand.
- `src/harbor_clerk/cli/commands/documents_by_date.py` — `harbor-clerk documents-by-date` subcommand.
- `src/harbor_clerk/cli/help/verify-identifier.txt` — long-form help text.
- `src/harbor_clerk/cli/help/documents-by-date.txt` — long-form help text.
- `tests/test_metadata_dates.py` — date-discovery unit tests.
- `tests/test_mcp_lookup_tools.py` — algorithm + integration tests for both tools.
- `tests/cli/test_commands_verify_identifier.py` — CLI command tests.
- `tests/cli/test_commands_documents_by_date.py` — CLI command tests.

**Modified files:**
- `src/harbor_clerk/mcp_server.py` — register `kb_verify_identifier` + `kb_documents_by_date` MCP tools.
- `src/harbor_clerk/cli/commands/__init__.py` — add `"verify-identifier"` + `"documents-by-date"` to `_COMMAND_NAMES`.
- `src/harbor_clerk/cli/output.py` — register text renderers for the two new commands.
- `tests/test_mcp_tool_descriptions.py` — pin the new tool descriptions (4-part docstring + key affordance words).
- `tests/cli/test_e2e.py` — smoke tests that the new help text loads via `--help`.

---

## Task 1: `metadata_dates.py` — `effective_date()` helper

**Files:**
- Create: `src/harbor_clerk/metadata_dates.py`
- Test:   `tests/test_metadata_dates.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metadata_dates.py
"""Unit tests for src/harbor_clerk/metadata_dates.py::effective_date()."""

from datetime import datetime, timezone

from harbor_clerk.metadata_dates import effective_date


class _Doc:
    """Stand-in for the Document model — only fields effective_date reads.

    Using a tiny dataclass-like helper instead of importing Document keeps
    this file a pure unit test (no DB, no SQLAlchemy fixtures).
    """

    def __init__(self, doc_metadata=None, created_at=None):
        self.doc_metadata = doc_metadata
        self.created_at = created_at


def test_uses_tika_when_present():
    doc = _Doc(doc_metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}})
    dt, label = effective_date(doc)
    assert dt == datetime(1999, 10, 13, 8, 34, tzinfo=timezone.utc)
    assert label == "tika.created_at"


def test_prefers_tika_over_frontmatter_and_sidecar():
    doc = _Doc(
        doc_metadata={
            "tika": {"created_at": "2020-01-01T00:00:00Z"},
            "frontmatter": {"date": "2010-01-01"},
            "sidecar": {"date": "2015-01-01"},
        }
    )
    dt, label = effective_date(doc)
    assert dt.year == 2020
    assert label == "tika.created_at"


def test_falls_through_to_frontmatter():
    doc = _Doc(doc_metadata={"frontmatter": {"date": "2025-04-19"}})
    dt, label = effective_date(doc)
    assert dt == datetime(2025, 4, 19, tzinfo=timezone.utc)
    assert label == "frontmatter.date"


def test_falls_through_to_sidecar():
    doc = _Doc(doc_metadata={"sidecar": {"date": "2024-08-01T12:00:00+00:00"}})
    dt, label = effective_date(doc)
    assert dt == datetime(2024, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert label == "sidecar.date"


def test_falls_through_to_ingest():
    ingest = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    doc = _Doc(doc_metadata={}, created_at=ingest)
    dt, label = effective_date(doc)
    assert dt == ingest
    assert label == "ingest"


def test_iso_without_timezone_assumed_utc():
    doc = _Doc(doc_metadata={"tika": {"created_at": "2024-01-15T10:00:00"}})
    dt, _ = effective_date(doc)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)


def test_date_only_string_parses():
    doc = _Doc(doc_metadata={"frontmatter": {"date": "2024-01-15"}})
    dt, _ = effective_date(doc)
    assert dt == datetime(2024, 1, 15, tzinfo=timezone.utc)


def test_naive_datetime_object_assumed_utc():
    doc = _Doc(doc_metadata={"sidecar": {"date": datetime(2024, 2, 2)}})
    dt, _ = effective_date(doc)
    assert dt == datetime(2024, 2, 2, tzinfo=timezone.utc)


def test_unparseable_string_falls_through_to_next_source():
    doc = _Doc(
        doc_metadata={"tika": {"created_at": "not-a-date"}},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    dt, label = effective_date(doc)
    assert label == "ingest"
    assert dt.year == 2026


def test_all_sources_missing_returns_none_and_none_label():
    doc = _Doc(doc_metadata={}, created_at=None)
    dt, label = effective_date(doc)
    assert dt is None
    assert label == "none"


def test_none_doc_metadata_falls_through_to_ingest():
    """doc_metadata=None should not raise — treat as empty dict."""
    ingest = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = _Doc(doc_metadata=None, created_at=ingest)
    dt, label = effective_date(doc)
    assert label == "ingest"
    assert dt == ingest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metadata_dates.py -v`
Expected: 11 FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.metadata_dates'`

- [ ] **Step 3: Implement `effective_date`**

```python
# src/harbor_clerk/metadata_dates.py
"""Effective-date discovery for documents.

Walks a priority chain to return the canonical date for a document:
  1. metadata.tika.created_at  (Tika-extracted email-send / file-creation date)
  2. metadata.frontmatter.date (markdown frontmatter)
  3. metadata.sidecar.date     (JSON sidecar)
  4. documents.created_at      (ingest time — last resort)

Used by kb_documents_by_date for sorting + result annotation. Standalone
module so future features (document list views, exports, filters) can
share one place for "the canonical date for this doc."
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


# Priority chain: (label, getter). First non-null parseable value wins.
_PRIORITY: list[tuple[str, Callable[[Any], Any]]] = [
    ("tika.created_at", lambda d: (d.doc_metadata or {}).get("tika", {}).get("created_at")),
    ("frontmatter.date", lambda d: (d.doc_metadata or {}).get("frontmatter", {}).get("date")),
    ("sidecar.date", lambda d: (d.doc_metadata or {}).get("sidecar", {}).get("date")),
]


def effective_date(doc) -> tuple[datetime | None, str]:
    """Return (effective_date, source_label) for a document.

    Walks the priority chain and returns the first non-null value that
    parses as a datetime. Falls back to `documents.created_at` (ingest
    time) if no metadata source provides a valid date. Returns
    `(None, "none")` only if every source is missing AND ingest is also
    null (unreachable in practice — documents always have created_at).

    source_label ∈ {"tika.created_at", "frontmatter.date", "sidecar.date",
                    "ingest", "none"}.
    """
    for label, getter in _PRIORITY:
        raw = getter(doc)
        parsed = _parse(raw)
        if parsed is not None:
            return parsed, label
    if getattr(doc, "created_at", None) is not None:
        return _ensure_utc(doc.created_at), "ingest"
    return None, "none"


def _parse(raw: Any) -> datetime | None:
    """Parse a value into a UTC-aware datetime. Returns None on failure."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalise "Z" suffix to "+00:00" since fromisoformat() only
    # accepts the latter on Python 3.10. Both forms are valid ISO 8601.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _ensure_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _ensure_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass aware datetimes through."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metadata_dates.py -v`
Expected: 11 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/metadata_dates.py tests/test_metadata_dates.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/metadata_dates.py tests/test_metadata_dates.py`
Expected: "X files left unchanged" or "X file reformatted"

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/metadata_dates.py tests/test_metadata_dates.py
git commit -m "feat(metadata): effective_date() helper with priority chain"
```

---

## Task 2: `mcp_lookup_tools.py` — candidate matching for `verify_identifier`

**Goal:** Add the matching layer that finds all documents whose title, filename, or identifier-like metadata field matches a normalized identifier string. Returns a deduplicated list of `Document` rows. The full `verify_identifier()` public function follows in Task 3.

**Files:**
- Create: `src/harbor_clerk/mcp_lookup_tools.py` (matching layer only)
- Test:   `tests/test_mcp_lookup_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_lookup_tools.py
"""Algorithm + integration tests for src/harbor_clerk/mcp_lookup_tools.py.

Task 2 (this file) covers candidate matching for verify_identifier:
title + canonical_filename ILIKE; metadata.tika.title equals; identifier-like
metadata key equals across sidecar/frontmatter.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mcp_lookup_tools import _find_candidates
from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_doc(
    db_session: AsyncSession,
    *,
    title: str,
    canonical_filename: str | None = None,
    metadata: dict | None = None,
) -> Document:
    doc = Document(
        title=title,
        canonical_filename=canonical_filename,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata or {},
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# Matching tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_matches_returns_empty_list(db_session):
    await _seed_doc(db_session, title="Unrelated Doc")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "nothing-matches")
    assert candidates == []


@pytest.mark.asyncio
async def test_matches_by_title_contains(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await _seed_doc(db_session, title="Unrelated")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_canonical_filename_contains(db_session):
    target = await _seed_doc(
        db_session, title="Doc One", canonical_filename="0131_vendor_contract.pdf"
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "vendor_contract")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_are_case_insensitive(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "PINNACLE")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_whitespace_normalized_in_input(db_session):
    target = await _seed_doc(db_session, title="Pinnacle Vendor Contract")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "  pinnacle   vendor  ")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_tika_title_equals(db_session):
    target = await _seed_doc(
        db_session,
        title="raw-filename",
        metadata={"tika": {"title": "Pinnacle Vendor Contract"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle Vendor Contract")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_sidecar_identifier_key_equals(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"sidecar": {"contract_id": "K-2025-031", "vendor": "Acme"}},
    )
    # Negative: another doc has the same value in a non-id key — must NOT match.
    await _seed_doc(
        db_session,
        title="Other doc",
        metadata={"sidecar": {"vendor": "K-2025-031"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2025-031")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_nested_identifier_key(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"sidecar": {"contract": {"id": "K-2025-031"}}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2025-031")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_matches_by_list_valued_identifier_key(db_session):
    target = await _seed_doc(
        db_session,
        title="Some doc",
        metadata={"frontmatter": {"order_id": ["K-1", "K-2"]}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "K-2")
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_deduplicates_when_multiple_fields_match(db_session):
    target = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract",
        canonical_filename="pinnacle-vendor-contract.pdf",
        metadata={"tika": {"title": "Pinnacle Vendor Contract"}},
    )
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle Vendor Contract")
    # All three columns match this doc; result must contain it once.
    assert [c.doc_id for c in candidates] == [target.doc_id]


@pytest.mark.asyncio
async def test_excludes_inactive_documents(db_session):
    doc = await _seed_doc(db_session, title="Pinnacle")
    doc.status = "deleted"
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert candidates == []


@pytest.mark.asyncio
async def test_caps_at_100_candidates(db_session):
    """100 docs with a shared substring — result is capped at 100."""
    for i in range(105):
        await _seed_doc(db_session, title=f"Pinnacle {i:03d}")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "Pinnacle")
    assert len(candidates) == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v`
Expected: 12 FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.mcp_lookup_tools'`

- [ ] **Step 3: Implement the matching layer**

```python
# src/harbor_clerk/mcp_lookup_tools.py
"""kb_verify_identifier + kb_documents_by_date — structured-lookup tools.

Both tools perform non-similarity lookups against the documents table and
the metadata JSONB column added in PR-F. verify_identifier returns
not_found / unique / ambiguous; documents_by_date returns docs sorted by
their effective date (per metadata_dates.effective_date priority chain).

This module exposes two public async functions used by mcp_server.py:
  - verify_identifier(session, identifier) -> dict   (added in Task 3)
  - documents_by_date(session, ...) -> dict          (added in Task 5)

Task 2 establishes the candidate-matching layer for verify_identifier.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import Document


# Cap on candidates returned for verify_identifier. Bounds payload size
# and discriminating-fields compute cost on degenerate inputs.
_VERIFY_CANDIDATE_CAP = 100

# Leaf-key names that count as identifiers when matching metadata.sidecar.**
# or metadata.frontmatter.**. Anything else is treated as a disambiguator
# (vendor, date, etc.), surfaced via discriminating_fields rather than
# matched against the input string.
_IDENTIFIER_KEY_RE = re.compile(
    r"^(id|contract_id|policy_id|case_id|order_id|invoice_id|message_id|.*_id)$"
)

# Metadata namespaces searched for identifier-like keys. metadata.tika.title
# is handled separately because the match target is the title VALUE, not
# any key under tika.
_ID_KEY_NAMESPACES = ("sidecar", "frontmatter")


def _normalize(s: str) -> str:
    """Lower-case, strip, and collapse internal whitespace."""
    return " ".join(s.lower().split())


def _iter_id_like_leaves(metadata: dict) -> list[Any]:
    """Walk metadata.sidecar.** and metadata.frontmatter.**, yielding the
    value of every leaf whose KEY matches _IDENTIFIER_KEY_RE.

    Lists at leaves contribute each element. Returns a flat list of raw
    values (caller does normalisation + comparison).
    """
    out: list[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, dict):
                    _walk(value)
                elif _IDENTIFIER_KEY_RE.match(str(key)):
                    if isinstance(value, list):
                        out.extend(value)
                    else:
                        out.append(value)

    for ns in _ID_KEY_NAMESPACES:
        ns_dict = metadata.get(ns)
        if isinstance(ns_dict, dict):
            _walk(ns_dict)

    return out


async def _find_candidates(session: AsyncSession, identifier: str) -> list[Document]:
    """Return the set of active Documents matching `identifier` by any of:
      - title CONTAINS identifier (case-insensitive, whitespace-normalised)
      - canonical_filename CONTAINS identifier (same normalisation)
      - metadata.tika.title EQUALS identifier (case-insensitive)
      - any identifier-like-key leaf value in metadata.sidecar.** or
        metadata.frontmatter.** EQUALS identifier (case-insensitive)

    Deduplicated by doc_id. Capped at _VERIFY_CANDIDATE_CAP results
    (the public verify_identifier in Task 3 sets `overflow: true` when
    the cap is reached).
    """
    normalized = _normalize(identifier)
    if not normalized:
        return []

    # SQL-side pass: title / canonical_filename ILIKE, plus tika.title equality.
    # The metadata-key match needs Python-side traversal because the leaf
    # paths are variable.
    pattern = f"%{normalized}%"
    stmt = (
        select(Document)
        .where(Document.status == "active")
        .where(
            or_(
                Document.title.op("ILIKE")(pattern),
                Document.canonical_filename.op("ILIKE")(pattern),
                # JSONB ->> returns text; lower() then equals the normalized input.
                Document.doc_metadata["tika"]["title"].astext.op("ILIKE")(normalized),
            )
        )
    )
    sql_hits = (await session.execute(stmt)).scalars().all()
    sql_hit_ids = {d.doc_id for d in sql_hits}

    # Python-side pass for identifier-like metadata keys. Fetch all active
    # docs whose metadata is non-empty (cheap with the existing GIN index)
    # and walk their JSON structure.
    stmt_meta = (
        select(Document)
        .where(Document.status == "active")
        .where(Document.doc_metadata != {})
    )
    meta_hits = (await session.execute(stmt_meta)).scalars().all()

    extra: list[Document] = []
    for doc in meta_hits:
        if doc.doc_id in sql_hit_ids:
            continue
        for raw in _iter_id_like_leaves(doc.doc_metadata or {}):
            if isinstance(raw, str) and _normalize(raw) == normalized:
                extra.append(doc)
                break

    combined = list(sql_hits) + extra
    # Deduplicate preserving order; cap.
    seen: set = set()
    result: list[Document] = []
    for d in combined:
        if d.doc_id in seen:
            continue
        seen.add(d.doc_id)
        result.append(d)
        if len(result) >= _VERIFY_CANDIDATE_CAP:
            break
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v`
Expected: 12 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
git commit -m "feat(mcp-lookup): _find_candidates matching layer for verify_identifier"
```

---

## Task 3: `verify_identifier()` public response shape

**Goal:** Add the public `verify_identifier(session, identifier) -> dict` function on top of Task 2's `_find_candidates`. Branches on candidate count (not_found / unique / ambiguous), projects per-candidate `discriminating_fields` via reuse of PR-D's `_find_differing_metadata_fields`, builds the suggestion, and surfaces `overflow: true` when the cap was reached.

**Files:**
- Modify: `src/harbor_clerk/mcp_lookup_tools.py`
- Modify: `tests/test_mcp_lookup_tools.py`

- [ ] **Step 1: Write the failing tests** (append to existing file)

```python
# Append to tests/test_mcp_lookup_tools.py

from harbor_clerk.mcp_lookup_tools import verify_identifier


# ---------------------------------------------------------------------------
# verify_identifier response-shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_returns_status_not_found(db_session):
    await _seed_doc(db_session, title="Unrelated")
    await db_session.flush()

    result = await verify_identifier(db_session, "nothing-matches")
    assert result == {"status": "not_found", "identifier": "nothing-matches"}


@pytest.mark.asyncio
async def test_unique_match_returns_status_unique(db_session):
    target = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract",
        canonical_filename="0131_vendor_contract.pdf",
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Vendor Contract")
    assert result["status"] == "unique"
    assert result["match"]["doc_id"] == str(target.doc_id)
    assert result["match"]["title"] == "Pinnacle Vendor Contract"
    assert result["match"]["canonical_filename"] == "0131_vendor_contract.pdf"
    assert result["match"]["discriminating_fields"] == {}


@pytest.mark.asyncio
async def test_ambiguous_match_with_discriminating_fields(db_session):
    a = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract A",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions", "term_months": 24}},
    )
    b = await _seed_doc(
        db_session,
        title="Pinnacle Vendor Contract B",
        metadata={"sidecar": {"vendor": "Pinnacle Industries", "term_months": 36}},
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Vendor Contract")
    assert result["status"] == "ambiguous"
    assert result["count"] == 2
    ids = {c["doc_id"] for c in result["candidates"]}
    assert ids == {str(a.doc_id), str(b.doc_id)}

    # Per-candidate discriminating_fields should carry that candidate's own values.
    for c in result["candidates"]:
        assert "sidecar.vendor" in c["discriminating_fields"]
        assert "sidecar.term_months" in c["discriminating_fields"]
    suggestion = result["suggestion"]
    assert "sidecar.vendor" in suggestion or "sidecar.term_months" in suggestion


@pytest.mark.asyncio
async def test_ambiguous_with_no_differing_fields_uses_fallback_suggestion(db_session):
    await _seed_doc(
        db_session,
        title="Pinnacle Contract Alpha",
        metadata={"sidecar": {"vendor": "Same Vendor"}},
    )
    await _seed_doc(
        db_session,
        title="Pinnacle Contract Beta",
        metadata={"sidecar": {"vendor": "Same Vendor"}},
    )
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle Contract")
    assert result["status"] == "ambiguous"
    for c in result["candidates"]:
        assert c["discriminating_fields"] == {}
    assert "identical" in result["suggestion"].lower()


@pytest.mark.asyncio
async def test_empty_identifier_returns_error(db_session):
    result = await verify_identifier(db_session, "")
    assert "error" in result
    assert "non-empty" in result["error"]


@pytest.mark.asyncio
async def test_whitespace_only_identifier_returns_error(db_session):
    result = await verify_identifier(db_session, "   ")
    assert "error" in result


@pytest.mark.asyncio
async def test_overflow_flag_set_when_cap_exceeded(db_session):
    for i in range(105):
        await _seed_doc(db_session, title=f"Pinnacle {i:03d}")
    await db_session.flush()

    result = await verify_identifier(db_session, "Pinnacle")
    assert result["status"] == "ambiguous"
    assert result["count"] == 100
    assert result["overflow"] is True
    assert "more than 100" in result["suggestion"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k verify_identifier`
Expected: 7 FAIL — `ImportError: cannot import name 'verify_identifier' from 'harbor_clerk.mcp_lookup_tools'`

- [ ] **Step 3: Implement `verify_identifier` + helpers** (append to module)

```python
# Append to src/harbor_clerk/mcp_lookup_tools.py

# Reuse PR-D's algorithm for "which metadata paths differ across candidates".
# Imported despite the leading underscore — Python doesn't enforce it; the
# function is the canonical implementation of "what fields distinguish
# these docs" and duplicating it would risk drift.
from harbor_clerk.mcp_discriminator import _find_differing_metadata_fields


async def verify_identifier(session: AsyncSession, identifier: str) -> dict:
    """Verify that an identifier resolves to a unique document.

    Returns one of three response shapes:
      - {"status": "not_found", "identifier": <input>}
      - {"status": "unique", "match": {doc_id, title, canonical_filename,
                                       discriminating_fields: {}}}
      - {"status": "ambiguous", "count": N, "candidates": [...],
         "suggestion": "...", "overflow"?: true}

    Empty / whitespace-only identifier returns {"error": "..."}.
    """
    if not identifier or not identifier.strip():
        return {"error": "identifier must be a non-empty string"}

    candidates = await _find_candidates(session, identifier)

    if not candidates:
        return {"status": "not_found", "identifier": identifier}

    if len(candidates) == 1:
        d = candidates[0]
        return {
            "status": "unique",
            "match": {
                "doc_id": str(d.doc_id),
                "title": d.title,
                "canonical_filename": d.canonical_filename,
                "discriminating_fields": {},
            },
        }

    # Ambiguous — compute which metadata paths differ across candidates.
    titles = {str(d.doc_id): (d.title or str(d.doc_id)) for d in candidates}
    metadata_by_doc = {str(d.doc_id): (d.doc_metadata or {}) for d in candidates}
    candidate_ids = [str(d.doc_id) for d in candidates]
    differing = _find_differing_metadata_fields(candidate_ids, metadata_by_doc, titles)

    cand_payload: list[dict] = []
    for d in candidates:
        did = str(d.doc_id)
        # Project the per-candidate value at each differing path.
        per_cand: dict = {}
        for path in differing:
            ns, key = path.split(".", 1)
            value = metadata_by_doc[did].get(ns, {}).get(key)
            if value is not None:
                per_cand[path] = value
        cand_payload.append(
            {
                "doc_id": did,
                "title": d.title,
                "canonical_filename": d.canonical_filename,
                "discriminating_fields": per_cand,
            }
        )

    overflow = len(candidates) >= _VERIFY_CANDIDATE_CAP

    if overflow:
        suggestion = (
            "More than 100 candidates matched — refine the identifier with a more "
            "specific substring, or use kb_search(metadata_filter=...) to narrow."
        )
    elif differing:
        field_list = ", ".join(differing.keys())
        suggestion = (
            f"{len(candidates)} candidates differ on {field_list} — "
            f"pick the one matching your intent."
        )
    else:
        suggestion = (
            "Multiple candidates have identical discriminating metadata — "
            "try kb_get_document on each to inspect body."
        )

    payload: dict = {
        "status": "ambiguous",
        "count": len(candidates),
        "candidates": cand_payload,
        "suggestion": suggestion,
    }
    if overflow:
        payload["overflow"] = True
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k verify_identifier`
Expected: 7 PASS (and Task 2's 12 still PASS — run the whole file)

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v`
Expected: 19 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
git commit -m "feat(mcp-lookup): verify_identifier response shape + discriminating_fields"
```

---

## Task 4: `documents_by_date` SQL query layer

**Goal:** Add a private `_query_documents_by_date()` helper that builds the SQL query for the date-bounded lookup. Handles the date `COALESCE` across (tika.created_at, frontmatter.date, sidecar.date, ingest), optional FTS query, optional metadata_filter, optional after/before bounds, direction, and limit. Returns rows of `(Document, effective_date, date_source)`. Public `documents_by_date()` follows in Task 5.

**Files:**
- Modify: `src/harbor_clerk/mcp_lookup_tools.py`
- Modify: `tests/test_mcp_lookup_tools.py`

- [ ] **Step 1: Write the failing tests** (append to existing file)

```python
# Append to tests/test_mcp_lookup_tools.py

from datetime import datetime, timezone

from harbor_clerk.mcp_lookup_tools import _query_documents_by_date
from harbor_clerk.models import Chunk


async def _seed_doc_with_chunk(
    db_session, *, title: str, metadata: dict | None = None, text: str = "body text"
) -> Document:
    """Like _seed_doc but also adds a chunk so FTS queries can match."""
    doc = await _seed_doc(db_session, title=title, metadata=metadata or {})
    db_session.add(
        Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="english")
    )
    await db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# _query_documents_by_date tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earliest_orders_by_tika_date_ascending(db_session):
    older = await _seed_doc_with_chunk(
        db_session,
        title="Older email",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
    )
    newer = await _seed_doc_with_chunk(
        db_session,
        title="Newer email",
        metadata={"tika": {"created_at": "2001-08-14T08:34:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=10)
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    # Older first when direction=earliest. Other test fixtures may add docs
    # too (autouse cleanup), so only assert the relative order of these two.
    assert doc_ids.index(str(older.doc_id)) < doc_ids.index(str(newer.doc_id))


@pytest.mark.asyncio
async def test_latest_orders_by_date_descending(db_session):
    older = await _seed_doc_with_chunk(
        db_session,
        title="Older",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    newer = await _seed_doc_with_chunk(
        db_session,
        title="Newer",
        metadata={"tika": {"created_at": "2024-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="latest", limit=10)
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    assert doc_ids.index(str(newer.doc_id)) < doc_ids.index(str(older.doc_id))


@pytest.mark.asyncio
async def test_query_filters_to_fts_matching_docs(db_session):
    cali = await _seed_doc_with_chunk(
        db_session,
        title="California email",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
        text="discussion about California regulators",
    )
    await _seed_doc_with_chunk(
        db_session,
        title="Unrelated",
        metadata={"tika": {"created_at": "1999-10-14T00:00:00Z"}},
        text="discussion about something else entirely",
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session, direction="earliest", query="California", limit=10
    )
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(cali.doc_id) in doc_ids
    assert len(doc_ids) == 1


@pytest.mark.asyncio
async def test_metadata_filter_applied(db_session):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Pinnacle contract",
        metadata={
            "tika": {"created_at": "2024-01-01T00:00:00Z"},
            "sidecar": {"vendor": "Pinnacle Tech Solutions"},
        },
    )
    await _seed_doc_with_chunk(
        db_session,
        title="Other contract",
        metadata={
            "tika": {"created_at": "2024-01-01T00:00:00Z"},
            "sidecar": {"vendor": "Other Vendor"},
        },
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions"},
        limit=10,
    )
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    assert doc_ids == [str(target.doc_id)]


@pytest.mark.asyncio
async def test_after_filter_bounds_results(db_session):
    early = await _seed_doc_with_chunk(
        db_session,
        title="Early",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    late = await _seed_doc_with_chunk(
        db_session,
        title="Late",
        metadata={"tika": {"created_at": "2025-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session, direction="earliest", after="2024-01-01", limit=10
    )
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(late.doc_id) in doc_ids
    assert str(early.doc_id) not in doc_ids


@pytest.mark.asyncio
async def test_before_filter_bounds_results(db_session):
    early = await _seed_doc_with_chunk(
        db_session,
        title="Early",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    late = await _seed_doc_with_chunk(
        db_session,
        title="Late",
        metadata={"tika": {"created_at": "2025-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session, direction="latest", before="2024-01-01", limit=10
    )
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(early.doc_id) in doc_ids
    assert str(late.doc_id) not in doc_ids


@pytest.mark.asyncio
async def test_date_source_label_reflects_priority_chain(db_session):
    tika_doc = await _seed_doc_with_chunk(
        db_session,
        title="Tika source",
        metadata={"tika": {"created_at": "2020-01-01T00:00:00Z"}},
    )
    fm_doc = await _seed_doc_with_chunk(
        db_session,
        title="FM source",
        metadata={"frontmatter": {"date": "2020-02-01"}},
    )
    sc_doc = await _seed_doc_with_chunk(
        db_session,
        title="Sidecar source",
        metadata={"sidecar": {"date": "2020-03-01"}},
    )
    ingest_only = await _seed_doc_with_chunk(db_session, title="Ingest only")
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=20)
    source_by_id = {str(d.doc_id): src for d, _, src in rows}
    assert source_by_id[str(tika_doc.doc_id)] == "tika.created_at"
    assert source_by_id[str(fm_doc.doc_id)] == "frontmatter.date"
    assert source_by_id[str(sc_doc.doc_id)] == "sidecar.date"
    assert source_by_id[str(ingest_only.doc_id)] == "ingest"


@pytest.mark.asyncio
async def test_explicit_date_field_skips_fallback(db_session):
    """date_field='sidecar.date' means docs without sidecar.date are sorted by NULL
    (PG sorts NULLs LAST asc / FIRST desc), not by Tika even if Tika exists."""
    sc_only = await _seed_doc_with_chunk(
        db_session,
        title="Sidecar only",
        metadata={"sidecar": {"date": "2020-01-01"}},
    )
    tika_only = await _seed_doc_with_chunk(
        db_session,
        title="Tika only",
        metadata={"tika": {"created_at": "2010-01-01T00:00:00Z"}},
    )
    await db_session.flush()

    rows = await _query_documents_by_date(
        db_session, direction="earliest", date_field="sidecar.date", limit=10
    )
    # sc_only is the first non-NULL date when sorting by sidecar.date asc;
    # tika_only is NULL on that field.
    doc_ids = [str(d.doc_id) for d, _, _ in rows]
    sc_idx = doc_ids.index(str(sc_only.doc_id))
    tika_idx = doc_ids.index(str(tika_only.doc_id))
    assert sc_idx < tika_idx
    # date_source label should reflect the explicit choice for the matching doc.
    source_by_id = {str(d.doc_id): src for d, _, src in rows}
    assert source_by_id[str(sc_only.doc_id)] == "sidecar.date"


@pytest.mark.asyncio
async def test_limit_respected(db_session):
    for i in range(5):
        await _seed_doc_with_chunk(
            db_session,
            title=f"Doc {i}",
            metadata={"tika": {"created_at": f"2024-01-{i + 1:02d}T00:00:00Z"}},
        )
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_excludes_inactive_documents(db_session):
    doc = await _seed_doc_with_chunk(
        db_session,
        title="Will be deleted",
        metadata={"tika": {"created_at": "2024-01-01T00:00:00Z"}},
    )
    doc.status = "deleted"
    await db_session.flush()

    rows = await _query_documents_by_date(db_session, direction="earliest", limit=10)
    doc_ids = {str(d.doc_id) for d, _, _ in rows}
    assert str(doc.doc_id) not in doc_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k "by_date"`
Expected: 10 FAIL — `ImportError: cannot import name '_query_documents_by_date'`

- [ ] **Step 3: Implement `_query_documents_by_date`** (append to module)

```python
# Append to src/harbor_clerk/mcp_lookup_tools.py

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import case, cast, func, literal, text
from sqlalchemy import Text as SAText
from sqlalchemy.dialects.postgresql import TIMESTAMP

from harbor_clerk.models import Chunk
# Existing search.py validator — raises ValueError on bad keys; identical
# normalisation the metadata_filter on kb_search uses.
from harbor_clerk.search import _validate_metadata_filter


# Map external date_field names to (JSONB-or-column expression, source label).
# Used both for the COALESCE chain (default) and the single-field override.
def _date_components():
    """Return (expr, label) per priority slot — recomputed per query because
    SQLAlchemy expressions are immutable per construction."""
    tika_expr = cast(Document.doc_metadata["tika"]["created_at"].astext, TIMESTAMP(timezone=True))
    fm_expr = cast(Document.doc_metadata["frontmatter"]["date"].astext, TIMESTAMP(timezone=True))
    sc_expr = cast(Document.doc_metadata["sidecar"]["date"].astext, TIMESTAMP(timezone=True))
    ingest_expr = Document.created_at
    return [
        ("tika.created_at", tika_expr),
        ("frontmatter.date", fm_expr),
        ("sidecar.date", sc_expr),
        ("ingest", ingest_expr),
    ]


_ALLOWED_DATE_FIELDS = {"tika.created_at", "frontmatter.date", "sidecar.date", "ingest"}


def _parse_iso_date(value: str) -> datetime:
    """Parse an ISO 8601 date or datetime string into a UTC-aware datetime."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _query_documents_by_date(
    session: AsyncSession,
    *,
    direction: Literal["earliest", "latest"] = "earliest",
    query: str | None = None,
    metadata_filter: dict | None = None,
    after: str | None = None,
    before: str | None = None,
    date_field: str | None = None,
    limit: int = 10,
) -> list[tuple[Document, datetime, str]]:
    """Run the SQL query for documents_by_date and return rows.

    Each row is (Document, effective_date, date_source_label). Caller is
    responsible for shaping the response.

    Raises ValueError on invalid `date_field`, unparseable `after`/`before`,
    or invalid `metadata_filter` (via reuse of search.py validator).
    """
    components = _date_components()

    # Build the effective-date expression + source-label CASE.
    if date_field is not None:
        if date_field not in _ALLOWED_DATE_FIELDS:
            raise ValueError(
                f"date_field must be one of: {sorted(_ALLOWED_DATE_FIELDS)}; got {date_field!r}"
            )
        expr = next(e for label, e in components if label == date_field)
        source_label_expr = literal(date_field)
    else:
        # COALESCE in priority order; CASE picks the first non-null source.
        expr = func.coalesce(*[e for _, e in components])
        source_label_expr = case(
            *[(components[i][1].isnot(None), literal(components[i][0])) for i in range(len(components))],
            else_=literal("none"),
        )

    stmt = (
        select(Document, expr.label("effective_date"), source_label_expr.label("date_source"))
        .where(Document.status == "active")
    )

    # Optional FTS filter via chunks join.
    if query:
        fts_subq = (
            select(Chunk.doc_id.distinct())
            .where(
                Chunk.fts_en.op("@@")(func.websearch_to_tsquery("english", query))
                | Chunk.fts_fr.op("@@")(func.websearch_to_tsquery("french", query))
            )
            .scalar_subquery()
        )
        stmt = stmt.where(Document.doc_id.in_(fts_subq))

    # Optional metadata_filter (reuse PR-F's validator + simple per-path eq).
    if metadata_filter:
        _validate_metadata_filter(metadata_filter)
        for path, value in metadata_filter.items():
            ns, key = path.split(".", 1)
            if isinstance(value, str):
                # JSONB ->> returns text; scalar string equality covers the
                # synthetic-corpus disambiguation case (vendor names, etc.).
                stmt = stmt.where(Document.doc_metadata[ns][key].astext == value)
            else:
                # Numeric / bool / list values — JSONB equality on the path.
                stmt = stmt.where(
                    Document.doc_metadata[ns][key]
                    == cast(literal(value), Document.doc_metadata.type)
                )

    # Optional after / before bounds on the effective date.
    if after:
        stmt = stmt.where(expr >= _parse_iso_date(after))
    if before:
        stmt = stmt.where(expr <= _parse_iso_date(before))

    # Sort.
    if direction == "earliest":
        stmt = stmt.order_by(expr.asc().nulls_last())
    elif direction == "latest":
        stmt = stmt.order_by(expr.desc().nulls_last())
    else:
        raise ValueError(f"direction must be 'earliest' or 'latest'; got {direction!r}")

    stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return [(row.Document, row.effective_date, row.date_source) for row in result.all()]
```

> **Note for the engineer:** the `metadata_filter` block above intentionally uses the simpler scalar-equality path that matches every test in this plan. The full JSONB `@>` containment with list-valued fallback already exists in `src/harbor_clerk/search.py` (used by `kb_search`). Task 6 wires the MCP tool through; if integration tests reveal a gap, lift the helper from search.py at that point. YAGNI for now.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k "by_date"`
Expected: 10 PASS

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v`
Expected: 29 PASS (12 from Task 2 + 7 from Task 3 + 10 from Task 4)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
git commit -m "feat(mcp-lookup): _query_documents_by_date SQL with COALESCE date expr"
```

---

## Task 5: `documents_by_date()` public response shape

**Goal:** Add the public `documents_by_date(session, ...) -> dict` that wraps Task 4's rows in the response shape: `{direction, count, results}` with per-result `{doc_id, title, canonical_filename, date, date_source}`. Handles input validation by catching `ValueError` from the helper and returning an error response.

**Files:**
- Modify: `src/harbor_clerk/mcp_lookup_tools.py`
- Modify: `tests/test_mcp_lookup_tools.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
# Append to tests/test_mcp_lookup_tools.py

from harbor_clerk.mcp_lookup_tools import documents_by_date


# ---------------------------------------------------------------------------
# documents_by_date response-shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_documents_by_date_response_shape(db_session):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Pinnacle Doc",
        metadata={"tika": {"created_at": "2024-06-15T12:00:00Z"}},
    )
    await db_session.flush()

    result = await documents_by_date(db_session, direction="earliest", limit=10)
    assert result["direction"] == "earliest"
    assert isinstance(result["count"], int)
    assert any(r["doc_id"] == str(target.doc_id) for r in result["results"])
    row = next(r for r in result["results"] if r["doc_id"] == str(target.doc_id))
    assert row["title"] == "Pinnacle Doc"
    assert row["date"] == "2024-06-15T12:00:00+00:00"
    assert row["date_source"] == "tika.created_at"


@pytest.mark.asyncio
async def test_documents_by_date_count_matches_results_length(db_session):
    for i in range(3):
        await _seed_doc_with_chunk(
            db_session,
            title=f"Doc {i}",
            metadata={"tika": {"created_at": f"2024-01-{i + 1:02d}T00:00:00Z"}},
        )
    await db_session.flush()

    result = await documents_by_date(db_session, direction="earliest", limit=2)
    assert result["count"] == 2
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_documents_by_date_empty_results(db_session):
    """No active docs at all — return {count: 0, results: []}, never an error."""
    result = await documents_by_date(db_session, direction="earliest", limit=10)
    assert result["direction"] == "earliest"
    # We can't assert count == 0 because other tests may seed docs in the
    # same DB session; instead assert the shape is non-error.
    assert "error" not in result
    assert "results" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_direction_returns_error(db_session):
    result = await documents_by_date(db_session, direction="sideways", limit=10)
    assert "error" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_date_field_returns_error(db_session):
    result = await documents_by_date(
        db_session, direction="earliest", date_field="bogus.field", limit=10
    )
    assert "error" in result
    assert "tika.created_at" in result["error"]  # accepted values listed


@pytest.mark.asyncio
async def test_documents_by_date_invalid_after_returns_error(db_session):
    result = await documents_by_date(
        db_session, direction="earliest", after="not-a-date", limit=10
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_documents_by_date_invalid_metadata_filter_returns_error(db_session):
    result = await documents_by_date(
        db_session,
        direction="earliest",
        metadata_filter={"too.many.dots": "x"},
        limit=10,
    )
    assert "error" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k "documents_by_date_"`
Expected: 7 FAIL — `ImportError: cannot import name 'documents_by_date'`

- [ ] **Step 3: Implement `documents_by_date`** (append to module)

```python
# Append to src/harbor_clerk/mcp_lookup_tools.py

async def documents_by_date(
    session: AsyncSession,
    *,
    direction: str = "earliest",
    query: str | None = None,
    metadata_filter: dict | None = None,
    after: str | None = None,
    before: str | None = None,
    date_field: str | None = None,
    limit: int = 10,
) -> dict:
    """Return documents sorted by their effective date.

    Response shape:
      {"direction": <input>, "count": N, "results": [
         {"doc_id": "...", "title": "...", "canonical_filename": "...",
          "date": "ISO-8601", "date_source": "tika.created_at" | ...},
         ...
      ]}

    Returns {"error": "..."} on invalid direction, date_field, after/before
    string, or metadata_filter.
    """
    try:
        rows = await _query_documents_by_date(
            session,
            direction=direction,
            query=query,
            metadata_filter=metadata_filter,
            after=after,
            before=before,
            date_field=date_field,
            limit=limit,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    results: list[dict] = []
    for doc, eff_date, src in rows:
        results.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "canonical_filename": doc.canonical_filename,
                "date": eff_date.isoformat() if eff_date else None,
                "date_source": src,
            }
        )

    return {"direction": direction, "count": len(results), "results": results}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v`
Expected: 36 PASS (29 from Tasks 2–4 + 7 from this task)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`

Run: `uv run ruff format src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
git commit -m "feat(mcp-lookup): documents_by_date public response shape"
```

---

## Task 6: MCP wiring — register `kb_verify_identifier` + `kb_documents_by_date`

**Goal:** Register the two backend functions as MCP tools in `mcp_server.py` with PR-D-style 4-part docstrings (what / when / output / decline) and add pin tests in `test_mcp_tool_descriptions.py`. Each tool opens a session via the existing `async_session_factory` pattern and serializes the result to JSON.

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Modify: `tests/test_mcp_tool_descriptions.py`

- [ ] **Step 1: Write the failing description pin tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
# Append to tests/test_mcp_tool_descriptions.py

from harbor_clerk.mcp_server import kb_documents_by_date, kb_verify_identifier


def test_kb_verify_identifier_docstring_has_four_parts():
    doc = kb_verify_identifier.__doc__ or ""
    # Same 4-part convention as PR-D: section headers with trailing colon.
    assert "What you get back" in doc or "What it returns" in doc
    assert "When to" in doc
    assert "How to decline" in doc or "Decline" in doc


def test_kb_verify_identifier_mentions_key_affordances():
    doc = (kb_verify_identifier.__doc__ or "").lower()
    assert "verify" in doc
    assert "before" in doc  # "use before quoting" / "before you cite"
    assert "ambiguous" in doc or "ambiguity" in doc
    assert "not_found" in doc


def test_kb_documents_by_date_docstring_has_four_parts():
    doc = kb_documents_by_date.__doc__ or ""
    assert "What you get back" in doc or "What it returns" in doc
    assert "When to" in doc
    assert "How to decline" in doc or "Decline" in doc


def test_kb_documents_by_date_mentions_key_affordances():
    doc = (kb_documents_by_date.__doc__ or "").lower()
    assert "earliest" in doc
    assert "latest" in doc
    assert "date_source" in doc
```

Also append to `tests/test_mcp_lookup_tools.py` an integration test that exercises the MCP layer end-to-end (matches PR-F's `test_mcp_metadata_filter.py` pattern):

```python
# Append to tests/test_mcp_lookup_tools.py

import json
from contextlib import asynccontextmanager, contextmanager

from harbor_clerk.api.deps import Principal
from harbor_clerk.mcp_server import (
    _mcp_principal,
    kb_documents_by_date,
    kb_verify_identifier,
)


@contextmanager
def _principal_in_context(user):
    token = _mcp_principal.set(Principal(type="user", id=user.user_id, role="admin"))
    try:
        yield
    finally:
        _mcp_principal.reset(token)


@pytest.fixture
async def mock_session_factory(db_session, _engine, monkeypatch):
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.mark.asyncio
async def test_kb_verify_identifier_unique_end_to_end(
    client, admin_user, db_session, mock_session_factory
):
    target = await _seed_doc(db_session, title="Singular Pinnacle Doc")
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_verify_identifier(identifier="Singular Pinnacle Doc")
    parsed = json.loads(raw)
    assert parsed["status"] == "unique"
    assert parsed["match"]["doc_id"] == str(target.doc_id)


@pytest.mark.asyncio
async def test_kb_documents_by_date_end_to_end(
    client, admin_user, db_session, mock_session_factory
):
    target = await _seed_doc_with_chunk(
        db_session,
        title="Earliest doc",
        metadata={"tika": {"created_at": "1999-10-13T08:34:00Z"}},
        text="discussion of California regulators",
    )
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_documents_by_date(direction="earliest", query="California", limit=5)
    parsed = json.loads(raw)
    assert parsed["direction"] == "earliest"
    assert any(r["doc_id"] == str(target.doc_id) for r in parsed["results"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_tool_descriptions.py tests/test_mcp_lookup_tools.py -v -k "kb_verify or kb_documents"`
Expected: All FAIL — `ImportError: cannot import name 'kb_verify_identifier' from 'harbor_clerk.mcp_server'`

- [ ] **Step 3: Add the two MCP tools to `mcp_server.py`**

Append (just before the existing `kb_system_health` definition; placement is cosmetic but groups them with other admin-style tools):

```python
# In src/harbor_clerk/mcp_server.py — add two new @mcp.tool functions.

import json as _json

from harbor_clerk.mcp_lookup_tools import (
    documents_by_date as _documents_by_date_impl,
)
from harbor_clerk.mcp_lookup_tools import (
    verify_identifier as _verify_identifier_impl,
)


@mcp.tool
async def kb_verify_identifier(identifier: str) -> str:
    """Verify a document identifier resolves to exactly one document.

    Use this BEFORE quoting a specific document — checks the identifier
    against title, filename, and identifier-like metadata fields. Sharp
    affordance for fabrication-prevention.

    When to call:
      - Before claiming "the X contract says…" — verify X is a real, unique
        identifier first
      - When kb_search returns hits whose titles all share a substring you
        used as an identifier — verify whether you're looking at one doc or
        N variants
      - When the user asks about a specific named document

    What you get back:
      - status="not_found": no document matches — say so plainly; do NOT
        fall back to "closest match"
      - status="unique": one match — its doc_id, title, canonical_filename
      - status="ambiguous": multiple matches — each candidate carries
        discriminating_fields (the metadata that distinguishes them) and
        a suggestion for how to pick one

    When the response is ambiguous, the discriminating_fields per candidate
    tell you what differs — pick the one matching the user's intent, or ask
    a clarifying question. With overflow=true (more than 100 candidates),
    refine the identifier with a more specific substring.

    How to decline:
      - status="not_found" means the identifier is NOT in the corpus. Do
        NOT search by similarity as a substitute — tell the user the
        identifier doesn't exist.
      - status="ambiguous" with no discriminating_fields means the
        candidates are indistinguishable by metadata; inspect with
        kb_get_document if you must.
    """
    async with async_session_factory() as session:
        result = await _verify_identifier_impl(session, identifier)
    return _json.dumps(result, default=str)


@mcp.tool
async def kb_documents_by_date(
    direction: str = "earliest",
    query: str | None = None,
    metadata_filter: dict | None = None,
    after: str | None = None,
    before: str | None = None,
    date_field: str | None = None,
    limit: int = 10,
) -> str:
    """Return documents sorted by their effective date.

    Use this when the user asks for the earliest / latest / first / last
    document matching some criteria — similarity-ranked search (kb_search)
    will not surface boundary docs reliably.

    When to call:
      - "What's the earliest email about X" / "Find the oldest invoice"
      - "What's the latest version of Y" / "Most recent contract with Z"
      - "Show me everything before/after a specific date"
      - When you need chronological ordering, not similarity ordering

    What you get back:
      - direction: echoes the input ("earliest" or "latest")
      - count: number of results returned
      - results[]: each carries doc_id, title, canonical_filename, date
        (ISO 8601 UTC), and date_source — which field the date came from:
          "tika.created_at"  → Tika-extracted email-send / file date
          "frontmatter.date" → markdown frontmatter
          "sidecar.date"     → JSON sidecar
          "ingest"           → fallback to documents.created_at when no
                               metadata date is available

    Parameters:
      direction (default "earliest"): "earliest" | "latest"
      query: optional FTS-only filter (no vector). Returns docs whose chunks
        match the query, sorted by date.
      metadata_filter: dict of {"namespace.key": value} pairs — same shape
        as kb_search's metadata_filter, applied as JSONB containment.
      after, before: ISO 8601 date or datetime strings; bound the
        effective-date range.
      date_field: explicit override for the effective-date source. Accepted
        values: "tika.created_at", "frontmatter.date", "sidecar.date",
        "ingest". When set, no fallback is consulted (docs missing that
        field sort to the end as NULLs).
      limit (default 10): max results.

    How to decline:
      - If results is empty, no docs match the date bounds / query / filter
        — say so plainly. Do NOT broaden the bounds and re-run unless the
        user asks.
      - If the user asks for "the earliest" and the top result's
        date_source is "ingest", note that no metadata date was available
        — the result is sorted by ingest time, not by document content date.
    """
    async with async_session_factory() as session:
        result = await _documents_by_date_impl(
            session,
            direction=direction,
            query=query,
            metadata_filter=metadata_filter,
            after=after,
            before=before,
            date_field=date_field,
            limit=limit,
        )
    return _json.dumps(result, default=str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_tool_descriptions.py tests/test_mcp_lookup_tools.py -v`
Expected: All new tests PASS

Run: `uv run pytest tests/ -v -x --ignore=tests/test_macos_smoke.py`
Expected: Full suite PASS (no regressions)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py tests/test_mcp_lookup_tools.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py tests/test_mcp_lookup_tools.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py tests/test_mcp_lookup_tools.py
git commit -m "feat(mcp): register kb_verify_identifier + kb_documents_by_date"
```

---

## Task 7: CLI `harbor-clerk verify-identifier`

**Goal:** Add the `verify-identifier` CLI subcommand following PR #397's pattern: one file per command in `cli/commands/`, one help text in `cli/help/`, one test module in `tests/cli/`, and a text renderer registered in `cli/output.py`.

**Files:**
- Create: `src/harbor_clerk/cli/commands/verify_identifier.py`
- Create: `src/harbor_clerk/cli/help/verify-identifier.txt`
- Modify: `src/harbor_clerk/cli/output.py` (register text renderer)
- Create: `tests/cli/test_commands_verify_identifier.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_commands_verify_identifier.py
"""CLI tests for harbor-clerk verify-identifier.

Mocks McpHttpClient + resolve_config the same way test_commands_search.py does.
"""

import json
from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    with (
        patch("harbor_clerk.cli.commands.verify_identifier.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.verify_identifier.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_verify_identifier_requires_positional(capsys):
    """Calling without an identifier should fail argparse."""
    # argparse exits via parser.error → exit code 1 in this CLI.
    rc, _client = _run(
        ["verify-identifier"], {"status": "not_found", "identifier": ""}
    )
    assert rc == 1


def test_verify_identifier_calls_tool_with_identifier(capsys):
    rc, client = _run(
        ["verify-identifier", "Pinnacle Vendor Contract", "--json"],
        {"status": "not_found", "identifier": "Pinnacle Vendor Contract"},
    )
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_verify_identifier", {"identifier": "Pinnacle Vendor Contract"}
    )


def test_verify_identifier_json_mode_passes_payload_through(capsys):
    payload = {
        "status": "unique",
        "match": {
            "doc_id": "abc-123",
            "title": "Pinnacle",
            "canonical_filename": "pin.pdf",
            "discriminating_fields": {},
        },
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--json"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == payload


def test_verify_identifier_text_mode_renders_unique(capsys):
    payload = {
        "status": "unique",
        "match": {
            "doc_id": "abc-123",
            "title": "Pinnacle Vendor Contract",
            "canonical_filename": "pin.pdf",
            "discriminating_fields": {},
        },
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "unique" in out.lower()
    assert "Pinnacle Vendor Contract" in out
    assert "abc-123" in out


def test_verify_identifier_text_mode_renders_ambiguous(capsys):
    payload = {
        "status": "ambiguous",
        "count": 2,
        "candidates": [
            {
                "doc_id": "id-1",
                "title": "Pinnacle A",
                "canonical_filename": "a.pdf",
                "discriminating_fields": {"sidecar.vendor": "Tech Solutions"},
            },
            {
                "doc_id": "id-2",
                "title": "Pinnacle B",
                "canonical_filename": "b.pdf",
                "discriminating_fields": {"sidecar.vendor": "Industries"},
            },
        ],
        "suggestion": "2 candidates differ on sidecar.vendor.",
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ambiguous" in out.lower()
    assert "Pinnacle A" in out
    assert "Pinnacle B" in out
    assert "Tech Solutions" in out
    assert "Industries" in out


def test_verify_identifier_text_mode_renders_not_found(capsys):
    payload = {"status": "not_found", "identifier": "nope"}
    rc, _client = _run(["verify-identifier", "nope", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "not_found" in out.lower() or "not found" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_commands_verify_identifier.py -v`
Expected: All FAIL — command not registered.

> Note: argparse will reject the unknown `verify-identifier` subcommand at the parser level. Tests will fail because either (a) the subparser doesn't exist (registration not yet done in Task 9), or (b) the module doesn't exist. Both are real failures driving Task 7 + Task 9 together.

For Task 7's tests to actually run, we need the module to exist AND the command to be registered. We'll register it in Task 9, but the module must exist now. Patching `verify_identifier.McpHttpClient` requires the module to be importable.

- [ ] **Step 3: Implement the CLI command module**

```python
# src/harbor_clerk/cli/commands/verify_identifier.py
"""harbor-clerk verify-identifier — verify an identifier resolves to one doc."""

from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("verify-identifier") or "Verify a document identifier."
    p = subparsers.add_parser(
        "verify-identifier",
        help="Verify an identifier resolves to exactly one document",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument(
        "identifier",
        help="The identifier string to check against title, filename, and identifier-like metadata fields",
    )
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments = {"identifier": args.identifier}
    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_verify_identifier", arguments)
    render(payload, mode=mode, command="verify-identifier")
    return 0
```

- [ ] **Step 4: Add the text renderer to `output.py`**

Append to `src/harbor_clerk/cli/output.py`:

```python
# Append to src/harbor_clerk/cli/output.py

@register_text_renderer("verify-identifier")
def _render_verify_identifier(payload, stream):
    if not isinstance(payload, dict):
        stream.write(repr(payload) + "\n")
        return

    status = payload.get("status")
    if "error" in payload:
        stream.write(f"error: {payload['error']}\n")
        return

    if status == "not_found":
        stream.write(f"not_found: {payload.get('identifier', '')}\n")
        return

    if status == "unique":
        m = payload.get("match", {})
        stream.write(f"unique: {m.get('title', '')}  [{m.get('doc_id', '')}]\n")
        if m.get("canonical_filename"):
            stream.write(f"  filename: {m['canonical_filename']}\n")
        return

    if status == "ambiguous":
        count = payload.get("count", 0)
        overflow = " (overflow)" if payload.get("overflow") else ""
        stream.write(f"ambiguous: {count} candidates{overflow}\n")
        for c in payload.get("candidates", []):
            stream.write(f"  - {c.get('title', '')}  [{c.get('doc_id', '')}]\n")
            for path, value in (c.get("discriminating_fields") or {}).items():
                stream.write(f"      {path}={value!r}\n")
        suggestion = payload.get("suggestion")
        if suggestion:
            stream.write(f"\n{suggestion}\n")
        return

    # Unknown status — fall back to JSON-ish
    import json as _json
    stream.write(_json.dumps(payload, indent=2, default=str) + "\n")
```

- [ ] **Step 5: Create the help text file**

```text
# src/harbor_clerk/cli/help/verify-identifier.txt
harbor-clerk verify-identifier — verify an identifier resolves to one document

DESCRIPTION
  Checks whether a given identifier string maps to exactly one document in
  the corpus. Use BEFORE quoting a specific named document — sharp
  affordance for fabrication-prevention.

  Matches against:
    - document title (case-insensitive CONTAINS)
    - canonical filename (case-insensitive CONTAINS)
    - metadata.tika.title (case-insensitive EQUALS)
    - identifier-like metadata keys (id, contract_id, policy_id, case_id,
      order_id, invoice_id, message_id, *_id) under metadata.sidecar.** or
      metadata.frontmatter.** (case-insensitive EQUALS)

  Status outcomes:
    not_found  → no document matches; do NOT fall back to similarity
    unique     → exactly one match
    ambiguous  → multiple matches; each candidate carries
                 discriminating_fields showing what distinguishes it

USAGE
  harbor-clerk verify-identifier <identifier> [options]

OPTIONS
  (Inherits global flags: --url, --api-key, --insecure, --json, --format.)

RETURNS (JSON)
  // not_found
  { "status": "not_found", "identifier": "<input>" }

  // unique
  { "status": "unique",
    "match": { "doc_id": "...", "title": "...",
               "canonical_filename": "...",
               "discriminating_fields": {} } }

  // ambiguous (count > 1)
  { "status": "ambiguous",
    "count": 3,
    "candidates": [
      { "doc_id": "...", "title": "...",
        "canonical_filename": "...",
        "discriminating_fields": { "sidecar.vendor": "Acme",
                                   "sidecar.term_months": 24 } }
    ],
    "suggestion": "3 candidates differ on sidecar.vendor — pick one." }

  // ambiguous with overflow (> 100 matches)
  { "status": "ambiguous", "count": 100, "overflow": true,
    "candidates": [ /* 100 entries */ ],
    "suggestion": "More than 100 candidates matched — refine ..." }

EXAMPLES
  # Sharp existence check before quoting
  harbor-clerk verify-identifier "Pinnacle Vendor Contract"

  # JSON output for scripting
  harbor-clerk verify-identifier "K-2025-031" --json

  # Disambiguate before answering
  harbor-clerk verify-identifier "Senior Project Coordinator onboarding letter"
```

- [ ] **Step 6: Register the command** (also covered in Task 9, but the test depends on it)

Add to `src/harbor_clerk/cli/commands/__init__.py` `_COMMAND_NAMES`:

```python
_COMMAND_NAMES = [
    "search",
    "batch-search",
    "read-passages",
    "expand-context",
    "read-document",
    "get-document",
    "list-recent",
    "corpus-overview",
    "document-outline",
    "find-related",
    "entity-search",
    "entity-overview",
    "entity-cooccurrence",
    "ingest-status",
    "reprocess",
    "system-health",
    "verify-identifier",          # <-- add
    # documents-by-date added in Task 8
]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_commands_verify_identifier.py -v`
Expected: 6 PASS

- [ ] **Step 8: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/cli/commands/verify_identifier.py src/harbor_clerk/cli/output.py src/harbor_clerk/cli/commands/__init__.py tests/cli/test_commands_verify_identifier.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/cli/commands/verify_identifier.py src/harbor_clerk/cli/output.py src/harbor_clerk/cli/commands/__init__.py tests/cli/test_commands_verify_identifier.py`

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/cli/commands/verify_identifier.py \
        src/harbor_clerk/cli/commands/__init__.py \
        src/harbor_clerk/cli/help/verify-identifier.txt \
        src/harbor_clerk/cli/output.py \
        tests/cli/test_commands_verify_identifier.py
git commit -m "feat(cli): harbor-clerk verify-identifier subcommand"
```

---

## Task 8: CLI `harbor-clerk documents-by-date`

**Goal:** Same pattern as Task 7 for `documents-by-date`. Adds the command module with rich argparse options (direction, query, metadata-filter as JSON, after, before, date-field, limit), help text, text renderer, and tests.

**Files:**
- Create: `src/harbor_clerk/cli/commands/documents_by_date.py`
- Create: `src/harbor_clerk/cli/help/documents-by-date.txt`
- Modify: `src/harbor_clerk/cli/output.py` (register renderer)
- Modify: `src/harbor_clerk/cli/commands/__init__.py` (register command)
- Create: `tests/cli/test_commands_documents_by_date.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_commands_documents_by_date.py
"""CLI tests for harbor-clerk documents-by-date."""

import json
from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    with (
        patch("harbor_clerk.cli.commands.documents_by_date.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.documents_by_date.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


_EMPTY = {"direction": "earliest", "count": 0, "results": []}


def test_default_direction_is_earliest(capsys):
    rc, client = _run(["documents-by-date", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["direction"] == "earliest"


def test_direction_latest_flag(capsys):
    rc, client = _run(["documents-by-date", "--direction", "latest", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["direction"] == "latest"


def test_query_flag_forwards(capsys):
    rc, client = _run(
        ["documents-by-date", "--query", "California", "--json"], _EMPTY
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["query"] == "California"


def test_after_and_before_flags_forward(capsys):
    rc, client = _run(
        [
            "documents-by-date",
            "--after",
            "2024-01-01",
            "--before",
            "2024-12-31",
            "--json",
        ],
        _EMPTY,
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["after"] == "2024-01-01"
    assert args["before"] == "2024-12-31"


def test_date_field_flag_forwards(capsys):
    rc, client = _run(
        ["documents-by-date", "--date-field", "sidecar.date", "--json"], _EMPTY
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["date_field"] == "sidecar.date"


def test_metadata_filter_parses_json(capsys):
    rc, client = _run(
        [
            "documents-by-date",
            "--metadata-filter",
            '{"sidecar.vendor": "Pinnacle Tech Solutions"}',
            "--json",
        ],
        _EMPTY,
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["metadata_filter"] == {"sidecar.vendor": "Pinnacle Tech Solutions"}


def test_metadata_filter_invalid_json_returns_usage_error(capsys):
    rc, _client = _run(
        ["documents-by-date", "--metadata-filter", "not-json", "--json"], _EMPTY
    )
    assert rc == 1


def test_limit_flag_forwards(capsys):
    rc, client = _run(["documents-by-date", "--limit", "25", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["limit"] == 25


def test_optional_flags_absent_when_not_provided(capsys):
    rc, client = _run(["documents-by-date", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    # Only direction + limit should be present in the minimal call.
    assert "query" not in args
    assert "after" not in args
    assert "before" not in args
    assert "date_field" not in args
    assert "metadata_filter" not in args


def test_text_mode_renders_results(capsys):
    payload = {
        "direction": "earliest",
        "count": 2,
        "results": [
            {
                "doc_id": "id-1",
                "title": "Earliest doc",
                "canonical_filename": "a.eml",
                "date": "1999-10-13T08:34:00+00:00",
                "date_source": "tika.created_at",
            },
            {
                "doc_id": "id-2",
                "title": "Second doc",
                "canonical_filename": "b.eml",
                "date": "1999-10-14T00:00:00+00:00",
                "date_source": "tika.created_at",
            },
        ],
    }
    rc, _client = _run(
        ["documents-by-date", "--format", "text"], payload
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Earliest doc" in out
    assert "1999-10-13" in out
    assert "tika.created_at" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_commands_documents_by_date.py -v`
Expected: All FAIL — module doesn't exist + command not registered.

- [ ] **Step 3: Implement the CLI command module**

```python
# src/harbor_clerk/cli/commands/documents_by_date.py
"""harbor-clerk documents-by-date — date-sorted document lookup."""

from __future__ import annotations

import argparse
import json as _json
import sys

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.commands import _common_parser
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.help import load
from harbor_clerk.cli.output import render, resolve_mode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = load("documents-by-date") or "Documents sorted by effective date."
    p = subparsers.add_parser(
        "documents-by-date",
        help="Documents sorted by their effective date (earliest / latest)",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_parser()],
    )
    p.add_argument(
        "--direction",
        choices=["earliest", "latest"],
        default="earliest",
        help="Sort direction (default: earliest)",
    )
    p.add_argument(
        "--query",
        help="Optional FTS filter (no vector ranking). Narrows the set, then sorts by date.",
    )
    p.add_argument(
        "--metadata-filter",
        help='JSON string of {"namespace.key": value} pairs (e.g. \'{"sidecar.vendor": "Pinnacle"}\')',
    )
    p.add_argument("--after", help="YYYY-MM-DD or ISO 8601 datetime; lower bound on effective date")
    p.add_argument("--before", help="YYYY-MM-DD or ISO 8601 datetime; upper bound on effective date")
    p.add_argument(
        "--date-field",
        choices=["tika.created_at", "frontmatter.date", "sidecar.date", "ingest"],
        help="Override auto-discovery; pin sort to a single source field",
    )
    p.add_argument("-l", "--limit", type=int, default=10, help="Max results (default: 10)")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "direction": args.direction,
        "limit": args.limit,
    }
    if args.query:
        arguments["query"] = args.query
    if args.after:
        arguments["after"] = args.after
    if args.before:
        arguments["before"] = args.before
    if args.date_field:
        arguments["date_field"] = args.date_field
    if args.metadata_filter:
        try:
            arguments["metadata_filter"] = _json.loads(args.metadata_filter)
        except _json.JSONDecodeError as exc:
            sys.stderr.write(
                f"harbor-clerk: --metadata-filter must be valid JSON: {exc}\n"
            )
            return 1

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_documents_by_date", arguments)
    render(payload, mode=mode, command="documents-by-date")
    return 0
```

- [ ] **Step 4: Add the text renderer to `output.py`**

Append to `src/harbor_clerk/cli/output.py`:

```python
@register_text_renderer("documents-by-date")
def _render_documents_by_date(payload, stream):
    if not isinstance(payload, dict):
        stream.write(repr(payload) + "\n")
        return

    if "error" in payload:
        stream.write(f"error: {payload['error']}\n")
        return

    direction = payload.get("direction", "")
    count = payload.get("count", 0)
    stream.write(f"{count} results ({direction})\n")
    for r in payload.get("results", []):
        date = r.get("date") or ""
        date_short = date[:10] if isinstance(date, str) and len(date) >= 10 else date
        src = r.get("date_source") or ""
        title = r.get("title") or ""
        doc_id = r.get("doc_id") or ""
        stream.write(f"  {date_short} [{src}]  {title}  ({doc_id})\n")
```

- [ ] **Step 5: Create the help text file**

```text
# src/harbor_clerk/cli/help/documents-by-date.txt
harbor-clerk documents-by-date — date-sorted document lookup

DESCRIPTION
  Returns documents sorted by their effective date (earliest or latest),
  with optional FTS query, metadata filter, and date bounds. Use this when
  the user asks for the earliest / latest / oldest / newest document —
  similarity-ranked search (search) will not surface boundary docs
  reliably.

  The effective date is discovered per document via this priority chain:
    1. metadata.tika.created_at  (Tika-extracted email-send / file date)
    2. metadata.frontmatter.date (markdown frontmatter)
    3. metadata.sidecar.date     (JSON sidecar)
    4. documents.created_at      (ingest time — last resort)

  Use --date-field to pin the sort to a single source (docs missing that
  field sort to the end as NULLs).

USAGE
  harbor-clerk documents-by-date [options]

OPTIONS
      --direction earliest|latest    Sort direction (default: earliest)
      --query TEXT                   Optional FTS filter (no vector ranking)
      --metadata-filter JSON         JSONB containment filter, e.g.
                                       '{"sidecar.vendor": "Pinnacle"}'
      --after YYYY-MM-DD             Lower bound on effective date
      --before YYYY-MM-DD            Upper bound on effective date
      --date-field FIELD             Pin sort to one source field. Choices:
                                       tika.created_at, frontmatter.date,
                                       sidecar.date, ingest
  -l, --limit INT                    Max results (default: 10)
  (Plus global flags: --url, --api-key, --insecure, --json, --format)

RETURNS (JSON)
  { "direction": "earliest",
    "count": 5,
    "results": [
      { "doc_id": "...",
        "title": "...",
        "canonical_filename": "...",
        "date": "1999-10-13T08:34:00+00:00",
        "date_source": "tika.created_at" } ] }

EXAMPLES
  # Earliest email about California regulators
  harbor-clerk documents-by-date --direction earliest --query "California"

  # Latest contract from a specific vendor
  harbor-clerk documents-by-date --direction latest \\
    --metadata-filter '{"sidecar.vendor": "Pinnacle Tech Solutions"}'

  # All docs ingested before a date, sorted oldest-first
  harbor-clerk documents-by-date --direction earliest \\
    --before 2024-01-01 --date-field ingest

  # Skilling's last pre-resignation email
  harbor-clerk documents-by-date --direction latest \\
    --query "from:skilling" --before 2001-08-14
```

- [ ] **Step 6: Register the command**

Edit `src/harbor_clerk/cli/commands/__init__.py` — add `"documents-by-date"` to `_COMMAND_NAMES` (alphabetically logical placement, but order is functional only for `--help`'s listing):

```python
_COMMAND_NAMES = [
    "search",
    "batch-search",
    "read-passages",
    "expand-context",
    "read-document",
    "get-document",
    "list-recent",
    "corpus-overview",
    "document-outline",
    "find-related",
    "entity-search",
    "entity-overview",
    "entity-cooccurrence",
    "ingest-status",
    "reprocess",
    "system-health",
    "verify-identifier",
    "documents-by-date",          # <-- add
]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_commands_documents_by_date.py -v`
Expected: 10 PASS

- [ ] **Step 8: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/cli/commands/documents_by_date.py src/harbor_clerk/cli/output.py src/harbor_clerk/cli/commands/__init__.py tests/cli/test_commands_documents_by_date.py`

Run: `uv run ruff format src/harbor_clerk/cli/commands/documents_by_date.py src/harbor_clerk/cli/output.py src/harbor_clerk/cli/commands/__init__.py tests/cli/test_commands_documents_by_date.py`

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/cli/commands/documents_by_date.py \
        src/harbor_clerk/cli/commands/__init__.py \
        src/harbor_clerk/cli/help/documents-by-date.txt \
        src/harbor_clerk/cli/output.py \
        tests/cli/test_commands_documents_by_date.py
git commit -m "feat(cli): harbor-clerk documents-by-date subcommand"
```

---

## Task 9: e2e smoke tests for the new CLI commands

**Goal:** Extend `tests/cli/test_e2e.py` to confirm both new commands register correctly and their `--help` text loads from the `.txt` files.

**Files:**
- Modify: `tests/cli/test_e2e.py`

- [ ] **Step 1: Read the existing test_e2e.py shape**

```bash
head -30 tests/cli/test_e2e.py
```

This shows the existing pattern (likely each command has a `test_<command>_help_loads` test using capsys to capture `--help` output).

- [ ] **Step 2: Add the failing tests**

Append to `tests/cli/test_e2e.py`:

```python
# Append to tests/cli/test_e2e.py

import pytest

from harbor_clerk.cli import main as cli_main


def test_verify_identifier_help_loads(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main.main(["verify-identifier", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "verify-identifier" in out
    assert "Verify a document identifier" in out or "verify an identifier" in out.lower()


def test_documents_by_date_help_loads(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main.main(["documents-by-date", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "documents-by-date" in out
    assert "earliest" in out and "latest" in out


def test_top_level_help_lists_new_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "verify-identifier" in out
    assert "documents-by-date" in out
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_e2e.py -v -k "verify_identifier or documents_by_date or top_level"`
Expected: 3 PASS (commands were already registered in Tasks 7 and 8)

- [ ] **Step 4: Ruff + format**

Run: `uv run ruff check tests/cli/test_e2e.py`

Run: `uv run ruff format tests/cli/test_e2e.py`

- [ ] **Step 5: Commit**

```bash
git add tests/cli/test_e2e.py
git commit -m "test(cli): e2e --help smoke tests for new commands"
```

---

## Task 10: Full-suite regression + ruff/format sanity

**Goal:** Confirm nothing else regressed. Run the full pytest suite, then `ruff check` + `ruff format --check` on the entire repo.

**Files:** No code changes — verification only.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest tests/ --ignore=tests/test_macos_smoke.py -v`
Expected: All tests PASS (count should be 1000+ given the additions in Tasks 1–9).

If any test fails:
- If the failure is in our new code, fix it inline and re-run.
- If the failure is in unrelated code (a flaky network test, a pre-existing skip flag), document it in the commit message but do not fix in this PR.

- [ ] **Step 2: Ruff check on the whole repo**

Run: `uv run ruff check .`
Expected: "All checks passed!"

- [ ] **Step 3: Ruff format check on the whole repo**

Run: `uv run ruff format --check .`
Expected: All files formatted correctly.

If any file is mis-formatted, run `uv run ruff format .` and commit.

- [ ] **Step 4: Commit any sanity fixes**

```bash
# Only if there were fixes from steps 1–3:
git add -p   # selectively stage
git commit -m "fix(pr-e): sanity-pass adjustments"
```

If no fixes needed, skip this step.

---

## Task 11: Fresh-eyes review + open PR

**Goal:** Dispatch the standing-directive fresh-eyes reviewer with a minimal prompt. Address ≥80-confidence findings inline, then open the PR.

**Files:** No code changes initially; review may surface fixes.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/mcp-verify-identifier-and-documents-by-date
```

- [ ] **Step 2: Dispatch the fresh-eyes reviewer**

Use the `Agent` tool with:
- `subagent_type`: `feature-dev:code-reviewer`
- Minimal prompt: state the branch + the PR-E goal (kb_verify_identifier + kb_documents_by_date + CLI parity); link the spec doc; tell the reviewer to report ≥80-confidence findings only. No focus areas, no carve-outs.

- [ ] **Step 3: Address findings**

For each finding the reviewer flags at ≥80 confidence:
- Fix inline.
- Re-run the relevant tests.
- Commit each fix as its own commit with a `fix(pr-e):` prefix.

- [ ] **Step 4: Open the PR**

```bash
gh pr create --title "feat(mcp): kb_verify_identifier + kb_documents_by_date + CLI parity (PR-E)" \
  --body-file /tmp/pr-e-body.md
```

PR body should include:
- **Summary** (2–3 bullets describing the two new tools + CLI parity)
- **Why** (cross-corpus eval failure data: Enron lookup 0/2/0 + synthetic boundary-doc cases)
- **What's in it** (file list grouped by tool / CLI / tests)
- **Test plan** (commands run, suite size, fresh-eyes review summary)
- **Spec + plan links** (this plan + the spec doc)

- [ ] **Step 5: Update the `pr_followups.md` if the PR description has an Out-of-scope section**

The spec's "Out of scope / follow-ups" section ports straight into the PR body's deferred-work section. Per the standing directive, every PR with a deferred-work section also gets an entry in `~/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md`.

- [ ] **Step 6: Enable auto-merge**

```bash
gh pr merge <PR-number> --auto --squash
```

CI takes ~5–8 minutes. The harness will not notify on merge; the user will see it land.

---

## Self-Review Notes

After writing the full plan, run through the spec section-by-section:

- **§ Goal + Why** — covered by Tasks 2–6 (verify_identifier addresses fabrication / boundary-doc; documents_by_date addresses Enron earliest/latest failures). ✓
- **§ Non-goals** — not encoded as tasks (negative space); the implementation in Tasks 2–5 doesn't touch them. ✓
- **§ Architecture** — Tasks 1 (metadata_dates), 2–5 (mcp_lookup_tools), 6 (MCP wiring), 7–8 (CLI commands), 9 (CLI smoke). All files in the "File Structure" section have a task. ✓
- **§ kb_verify_identifier — Matching rules** — Task 2. ✓
- **§ kb_verify_identifier — Response shape** — Task 3. ✓
- **§ kb_verify_identifier — Discriminating-fields algorithm** — Task 3 (reuses PR-D helper via import). ✓
- **§ kb_verify_identifier — Suggestion text** — Task 3 (suggestion-builder logic is in `verify_identifier` itself, distinct from PR-D's `_build_suggestion`). ✓
- **§ kb_verify_identifier — Cap & overflow** — Tasks 2 (cap in `_find_candidates`) + 3 (overflow flag + suggestion). ✓
- **§ kb_documents_by_date — Signature, date discovery, query semantics, date bounds, response shape** — Tasks 4 + 5. ✓
- **§ metadata_dates.py** — Task 1. ✓
- **§ CLI parity** — Tasks 7 + 8 + 9. ✓
- **§ Error handling** — embedded across Tasks 3 (verify_identifier), 5 (documents_by_date), 7 (CLI metadata-filter JSON error), 8 (CLI metadata-filter JSON error). ✓
- **§ Testing** — Tasks 1, 2, 3, 4, 5 (unit + integration), 6 (description pins + integration), 7, 8, 9 (CLI). ✓
- **§ Out of scope** — items are deferred by absence, not by task. ✓

**Type-consistency spot check:**
- `verify_identifier(session, identifier)` — same signature across Task 3 implementation, Task 6 MCP wrapper, Task 7 CLI call (which sends `{"identifier": ...}` to `kb_verify_identifier`). ✓
- `documents_by_date(session, *, direction, query, metadata_filter, after, before, date_field, limit)` — same across Task 5 implementation, Task 6 MCP wrapper, Task 8 CLI builder. ✓
- `_find_differing_metadata_fields(candidate_ids, metadata_by_doc, titles)` — reused per the existing signature in `mcp_discriminator.py`. ✓
- `effective_date(doc) -> tuple[datetime | None, str]` — referenced in Task 1's tests + used implicitly by Task 4's SQL (which mirrors the same priority chain at the SQL level rather than calling the Python helper per row). ✓

**Placeholder scan:** no "TBD" / "TODO" / "fill in" / "similar to" tokens in the plan. Every step that touches code shows the code.
