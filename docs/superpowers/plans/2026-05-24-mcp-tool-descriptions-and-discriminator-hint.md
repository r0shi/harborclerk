# MCP Tool Descriptions + Discriminator Hint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve recall/utility of HC's MCP tools for frontier cloud models by rewriting all 16 `kb_*` tool descriptions from API docs into behavior guides + adding a `discriminator_hint` field to `kb_search` responses that flags when top-K results share text but differ on a structured metadata field.

**Architecture:** Two complementary changes in `src/harbor_clerk/mcp_server.py`. New private module `src/harbor_clerk/mcp_discriminator.py` holds the discriminator algorithm (~80 lines, pure post-processing over `(hits, session)`). All 16 kb_* docstrings rewritten in-place using a consistent 4-part structure (what / when to use / output interpretation / decline conditions).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async (asyncpg), PostgreSQL 18 + JSONB, FastMCP (the `@mcp.tool()` decorator), pytest.

**Spec reference:** `docs/superpowers/specs/2026-05-24-mcp-tool-descriptions-and-discriminator-hint-design.md`

---

## File Structure

**New files:**
- `src/harbor_clerk/mcp_discriminator.py` — `_compute_discriminator_hint(hits, session) -> dict | None` + private helpers (`_find_differing_metadata_fields`, `_build_suggestion`)
- `tests/test_mcp_discriminator_hint.py` — unit tests for the algorithm (trigger conditions, helpers, edge cases)
- `tests/test_mcp_tool_descriptions.py` — grep-style assertions pinning the required surface of each rewritten description

**Modified files:**
- `src/harbor_clerk/mcp_server.py` — 16 docstring rewrites + one call site in `kb_search` that injects `discriminator_hint` into the response when non-`None`

**Untouched:**
- `src/harbor_clerk/search.py`, `search_types.py` — hybrid search algorithm + SearchHit shape stay as-is
- `Document.metadata` schema — PR-F's column unchanged
- All other kb_* implementations (only docstrings change)

---

## Task 1: `mcp_discriminator.py` module + unit tests

**Files:**
- Create: `src/harbor_clerk/mcp_discriminator.py`
- Create: `tests/test_mcp_discriminator_hint.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_discriminator_hint.py`:

```python
"""Unit tests for `_compute_discriminator_hint` and its helpers.

The discriminator surfaces a hint string to the model when top-K kb_search
hits span multiple docs whose relevance scores are close AND whose
structured metadata fields differ — pointing the model toward PR-F's
metadata_filter for disambiguation. The hint is absent (not null) when
any trigger condition fails.
"""

import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock

from harbor_clerk.mcp_discriminator import (
    _build_suggestion,
    _compute_discriminator_hint,
    _find_differing_metadata_fields,
)


@dataclass
class _FakeHit:
    """Minimal SearchHit stand-in — just the fields the discriminator reads."""

    doc_id: str
    doc_title: str
    score: float


def _fake_session_for(metadata_by_doc: dict[str, dict]):
    """Returns a MagicMock session whose .execute(...).all() yields rows
    with .doc_id and .doc_metadata for the keys in metadata_by_doc."""

    rows = []
    for did, meta in metadata_by_doc.items():
        row = MagicMock()
        row.doc_id = uuid.UUID(did)
        row.doc_metadata = meta
        rows.append(row)

    session = MagicMock()
    session.execute.return_value.all.return_value = rows
    return session


def test_compute_returns_none_when_fewer_than_two_hits():
    hits = [_FakeHit(doc_id=str(uuid.uuid4()), doc_title="only", score=0.9)]
    assert _compute_discriminator_hint(hits, _fake_session_for({})) is None


def test_compute_returns_none_when_all_hits_from_one_doc():
    did = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.9),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.85),
        _FakeHit(doc_id=did, doc_title="onedoc", score=0.8),
    ]
    assert _compute_discriminator_hint(hits, _fake_session_for({})) is None


def test_compute_returns_none_when_candidate_scores_too_far_apart():
    """ε = max(0.05, 0.1 * top_score). Top score 0.9 → ε = 0.09 → only
    docs with score >= 0.81 are candidates. doc-A is in; doc-B (0.7) is
    not — so only one candidate, no ambiguity, no hint."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.7),
    ]
    # Even though metadata differs, score gap makes B not a candidate
    session = _fake_session_for({a: {"sidecar": {"v": 1}}, b: {"sidecar": {"v": 2}}})
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_none_when_candidates_have_all_same_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    same = {"sidecar": {"vendor": "Acme", "term_months": 12}}
    session = _fake_session_for({a: same, b: same})
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_none_when_candidates_have_empty_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for({a: {}, b: {}})
    assert _compute_discriminator_hint(hits, session) is None


def test_compute_returns_hint_when_candidates_differ_on_metadata():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="0131_vendor_contract", score=0.9),
        _FakeHit(doc_id=b, doc_title="0149_vendor_contract", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "Pinnacle", "term_months": 24}},
            b: {"sidecar": {"vendor": "Pinnacle", "term_months": 12}},
        }
    )
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    assert set(hint["ambiguous_doc_ids"]) == {a, b}
    assert set(hint["ambiguous_doc_titles"]) == {"0131_vendor_contract", "0149_vendor_contract"}
    # Only term_months differs (vendor is same), so it's the only field surfaced
    assert "sidecar.term_months" in hint["differing_metadata"]
    assert "sidecar.vendor" not in hint["differing_metadata"]
    assert hint["differing_metadata"]["sidecar.term_months"] == {
        "0131_vendor_contract": 24,
        "0149_vendor_contract": 12,
    }
    assert "suggestion" in hint
    assert "metadata_filter" in hint["suggestion"]


def test_compute_orders_differing_fields_by_distinctness():
    """When multiple fields differ, the hint surfaces them ordered by the
    number of distinct values (most discriminating first). Capped at 3."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    c = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
        _FakeHit(doc_id=c, doc_title="C", score=0.87),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "X", "shared_field": "same", "type": "A"}},
            b: {"sidecar": {"vendor": "Y", "shared_field": "same", "type": "B"}},
            c: {"sidecar": {"vendor": "Z", "shared_field": "same", "type": "B"}},
        }
    )
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    # `vendor` has 3 distinct values (X, Y, Z); `type` has 2 (A, B); `shared_field` has 1 (excluded)
    paths = list(hint["differing_metadata"].keys())
    assert paths[0] == "sidecar.vendor"  # most discriminating first
    assert "sidecar.type" in paths
    assert "sidecar.shared_field" not in paths  # all same → not differing


def test_compute_skips_source_provenance_namespace():
    """The internal _source_provenance key shouldn't appear as a discriminator
    even though its timestamps will always differ across docs."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {
                "sidecar": {"vendor": "X"},
                "_source_provenance": {"sidecar": "2026-01-01T00:00:00+00:00"},
            },
            b: {
                "sidecar": {"vendor": "Y"},
                "_source_provenance": {"sidecar": "2026-02-02T00:00:00+00:00"},
            },
        }
    )
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert all(not p.startswith("_source_provenance") for p in paths)


def test_compute_skips_fields_missing_from_some_candidates():
    """A field that's present in some candidates but not others can't be
    used as a filter (it would exclude the missing ones). Skipped."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    hits = [
        _FakeHit(doc_id=a, doc_title="A", score=0.9),
        _FakeHit(doc_id=b, doc_title="B", score=0.88),
    ]
    session = _fake_session_for(
        {
            a: {"sidecar": {"vendor": "X", "only_in_a": "yes"}},
            b: {"sidecar": {"vendor": "Y"}},
        }
    )
    hint = _compute_discriminator_hint(hits, session)
    assert hint is not None
    paths = list(hint["differing_metadata"].keys())
    assert "sidecar.vendor" in paths
    assert "sidecar.only_in_a" not in paths


def test_find_differing_metadata_fields_returns_empty_for_identical_metadata():
    titles = {"a": "A", "b": "B"}
    metadata = {"a": {"sidecar": {"x": 1}}, "b": {"sidecar": {"x": 1}}}
    assert _find_differing_metadata_fields(["a", "b"], metadata, titles) == {}


def test_build_suggestion_mentions_top_field_value():
    """The suggestion string should reference at least one concrete
    metadata_filter call the model can use."""
    titles = {"a": "A", "b": "B"}
    top_fields = [("sidecar.term_months", {"A": 24, "B": 12})]
    s = _build_suggestion(top_fields, titles)
    assert "metadata_filter" in s
    assert "sidecar.term_months" in s
    # At least one of the concrete values appears
    assert "24" in s or "12" in s
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_discriminator_hint.py -v
```

Expected: `ModuleNotFoundError: No module named 'harbor_clerk.mcp_discriminator'`.

- [ ] **Step 3: Create the `mcp_discriminator.py` module**

Create `src/harbor_clerk/mcp_discriminator.py`:

```python
# src/harbor_clerk/mcp_discriminator.py
"""Discriminator hint computation for kb_search responses.

When top-K hits span multiple docs whose scores are close AND whose
structured metadata fields differ, surface a hint that points the model
toward PR-F's metadata_filter for disambiguation. Pure post-processing
over the (hits, session) pair returned by hybrid_search.

The hint is OMITTED (not null) when any trigger condition fails — callers
should check `"discriminator_hint" in response` rather than truthiness.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from harbor_clerk.models import Document

# Internal namespace key written by the metadata extractor framework.
# Per-source timestamps always differ across docs, so excluding this
# namespace prevents it from drowning out real discriminating fields.
_PROVENANCE_KEY = "_source_provenance"

# Cap on how many differing fields appear in the hint output. Three is
# enough for a model to pick a useful filter without bloating the
# response. Ordered by distinctness (most discriminating first).
_MAX_FIELDS_IN_HINT = 3


def _compute_discriminator_hint(hits, session) -> dict | None:
    """Return a discriminator_hint dict if top hits are ambiguous on a
    structured metadata field, or None if no hint applies.

    `hits` is a list of SearchHit-shaped objects (must have `.doc_id`
    (str), `.doc_title` (str), `.score` (float)). `session` is a
    SQLAlchemy session used for one SELECT against `documents.metadata`.
    """
    if len(hits) < 2:
        return None

    # Group hits by doc_id; keep the best score per doc.
    by_doc: dict[str, float] = {}
    titles: dict[str, str] = {}
    for h in hits:
        if h.doc_id not in by_doc or h.score > by_doc[h.doc_id]:
            by_doc[h.doc_id] = h.score
            titles[h.doc_id] = h.doc_title or h.doc_id

    if len(by_doc) < 2:
        return None  # all hits from a single doc

    # Candidate set: docs whose best score is within ε of the overall top.
    top_score = max(by_doc.values())
    epsilon = max(0.05, 0.1 * top_score)
    candidates = [did for did, s in by_doc.items() if s >= top_score - epsilon]

    if len(candidates) < 2:
        return None

    # Fetch metadata for the candidates (one indexed lookup).
    rows = session.execute(
        select(Document.doc_id, Document.doc_metadata).where(
            Document.doc_id.in_([uuid.UUID(c) for c in candidates])
        )
    ).all()
    metadata_by_doc: dict[str, dict] = {str(row.doc_id): (row.doc_metadata or {}) for row in rows}

    # Find paths where the candidates have differing values.
    differing = _find_differing_metadata_fields(candidates, metadata_by_doc, titles)
    if not differing:
        return None

    # Order by number of distinct values (most discriminating first).
    top_fields = sorted(
        differing.items(),
        key=lambda kv: -len(set(kv[1].values())),
    )[:_MAX_FIELDS_IN_HINT]

    return {
        "ambiguous_doc_ids": candidates,
        "ambiguous_doc_titles": [titles[d] for d in candidates],
        "differing_metadata": dict(top_fields),
        "suggestion": _build_suggestion(top_fields, titles),
    }


def _find_differing_metadata_fields(
    candidates: list[str],
    metadata_by_doc: dict[str, dict],
    titles: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """For each `<namespace>.<key>` path present in ALL candidates' metadata,
    return a {title: value} mapping if values differ. Skips _source_provenance.
    Output: {"sidecar.vendor": {"A": "X", "B": "Y"}, ...}.
    """
    # Collect all paths that any candidate has, grouped by candidate
    paths_by_doc: dict[str, set[tuple[str, str]]] = {}
    for did in candidates:
        meta = metadata_by_doc.get(did, {})
        paths_by_doc[did] = set()
        for namespace, ns_dict in meta.items():
            if namespace == _PROVENANCE_KEY:
                continue
            if not isinstance(ns_dict, dict):
                continue
            for key in ns_dict:
                paths_by_doc[did].add((namespace, key))

    # Paths that exist in EVERY candidate
    if not paths_by_doc:
        return {}
    common_paths = set.intersection(*paths_by_doc.values())

    # For each common path, collect the per-doc values; keep only paths where
    # values differ (more than 1 distinct value).
    differing: dict[str, dict[str, Any]] = {}
    for namespace, key in common_paths:
        per_title: dict[str, Any] = {}
        for did in candidates:
            value = metadata_by_doc[did][namespace][key]
            per_title[titles[did]] = value
        if len(set(_make_hashable(v) for v in per_title.values())) > 1:
            differing[f"{namespace}.{key}"] = per_title

    return differing


def _make_hashable(v: Any) -> Any:
    """Convert v to a hashable form for set-based distinctness checks.
    Lists/dicts/etc are converted to tuples/frozensets; primitives pass through."""
    if isinstance(v, list):
        return tuple(_make_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _make_hashable(vv)) for k, vv in v.items()))
    return v


def _build_suggestion(top_fields: list[tuple[str, dict[str, Any]]], titles: dict[str, str]) -> str:
    """Construct a one-line human-readable suggestion for the model.

    Picks the most discriminating field (first in top_fields) and surfaces
    one concrete metadata_filter call. Falls back gracefully if top_fields
    is empty (shouldn't happen — the caller checks first).
    """
    if not top_fields:
        return "Top results are ambiguous but no discriminating metadata fields found."

    path, values_by_title = top_fields[0]
    # Pick the first concrete value to suggest.
    first_value = next(iter(values_by_title.values()))
    return (
        f"Top results are ambiguous. Use metadata_filter={{'{path}': {first_value!r}}} "
        f"to pin one doc. Available values: "
        + ", ".join(f"{t}={v!r}" for t, v in values_by_title.items())
        + "."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_mcp_discriminator_hint.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Lint + format**

```bash
uv run ruff check src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
uv run ruff format --check src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
git commit -m "feat(mcp): discriminator_hint algorithm + unit tests" \
  -m "Pure post-processing function _compute_discriminator_hint(hits, session) that returns a hint dict when top-K hits span multiple docs whose scores are close AND whose structured metadata fields differ. Returns None (hint omitted from response) when any trigger fails. ε = max(0.05, 0.1 * top_score). Skips _source_provenance namespace. Field absent when missing from any candidate. Top 3 differing fields ordered by distinctness." \
  -m "Integration into kb_search lands in Task 2." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Wire `discriminator_hint` into `kb_search` response

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — add one call site in `kb_search` body that injects `discriminator_hint`
- Append: `tests/test_mcp_discriminator_hint.py` — one integration test

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_mcp_discriminator_hint.py`:

```python
# ── Integration: kb_search includes discriminator_hint when applicable ──


async def test_kb_search_includes_discriminator_hint_in_response(client, admin_user, db_session):
    """End-to-end: when kb_search returns hits whose top docs differ on
    structured metadata, the response includes a discriminator_hint."""
    import json
    from contextlib import contextmanager

    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.mcp_server import _mcp_principal, kb_search

    @contextmanager
    def _principal_in_context(user):
        token = _mcp_principal.set(user)
        try:
            yield
        finally:
            _mcp_principal.reset(token)

    async def _seed(*, title: str, metadata: dict):
        doc = Document(
            title=title,
            status="active",
            sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
            pipeline_status=PipelineStatus.ready,
            doc_metadata=metadata,
        )
        db_session.add(doc)
        await db_session.flush()
        chunk = Chunk(
            doc_id=doc.doc_id,
            chunk_num=0,
            chunk_text="jurisdiction governing law term",
            language="english",
        )
        db_session.add(chunk)
        await db_session.flush()
        return doc

    # Two contracts with the same vendor + same chunk text, differing on term_months
    a = await _seed(title="0131_vendor_contract", metadata={"sidecar": {"vendor": "Pinnacle", "term_months": 24}})
    b = await _seed(title="0149_vendor_contract", metadata={"sidecar": {"vendor": "Pinnacle", "term_months": 12}})

    with _principal_in_context(admin_user):
        raw = await kb_search(query="jurisdiction governing", k=10)

    parsed = json.loads(raw)
    # If the search returned 2+ hits across both docs (the chunk text is
    # identical so it should), the discriminator should fire on term_months.
    if len({hit["doc_id"] for hit in parsed.get("hits", [])}) >= 2:
        assert "discriminator_hint" in parsed
        hint = parsed["discriminator_hint"]
        assert "sidecar.term_months" in hint["differing_metadata"]
        assert "metadata_filter" in hint["suggestion"]
    else:
        # If retrieval only returned one of the docs, we can't assert the hint
        # fired — but we can assert the response has no broken discriminator_hint
        assert parsed.get("discriminator_hint") is None or isinstance(parsed["discriminator_hint"], dict)


async def test_kb_search_omits_discriminator_hint_when_not_applicable(client, admin_user, db_session):
    """When kb_search returns hits from only one doc, discriminator_hint
    is absent from the response (not present-as-None)."""
    import json
    from contextlib import contextmanager

    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.mcp_server import _mcp_principal, kb_search

    @contextmanager
    def _principal_in_context(user):
        token = _mcp_principal.set(user)
        try:
            yield
        finally:
            _mcp_principal.reset(token)

    doc = Document(
        title="solo",
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata={"sidecar": {"vendor": "Acme"}},
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="unique phrase xyz", language="english"))
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_search(query="unique phrase xyz", k=10)

    parsed = json.loads(raw)
    assert "discriminator_hint" not in parsed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_discriminator_hint.py::test_kb_search_includes_discriminator_hint_in_response tests/test_mcp_discriminator_hint.py::test_kb_search_omits_discriminator_hint_when_not_applicable -v
```

Expected: `AssertionError: assert 'discriminator_hint' in ...` (the kb_search response doesn't yet include the field).

- [ ] **Step 3: Wire `_compute_discriminator_hint` into `kb_search`**

Edit `src/harbor_clerk/mcp_server.py`. Add the import near the top with the other imports:

```python
from harbor_clerk.mcp_discriminator import _compute_discriminator_hint
```

Find the response-assembly block inside `kb_search` (around line 533-538, where `total_candidates` and `has_more` are added to the response dict). The response is built and returned via `json.dumps(...)`. Just before the final `return json.dumps(resp, ...)` call, inject the hint:

```python
# Compute discriminator_hint if applicable. Cheap post-processing:
# one indexed SELECT for top-K candidate docs' metadata. Skips when
# fewer than 2 hits.
hint = _compute_discriminator_hint(result.hits, session)
if hint is not None:
    resp["discriminator_hint"] = hint
```

The exact placement depends on where `resp` is built and where `session` is in scope. Find the line that assigns `resp["has_more"]` (around line 537) and add the hint computation immediately after it, but inside the same async session context. `session` is the AsyncSession variable already in use throughout the function.

**Important:** Because `_compute_discriminator_hint` uses `session.execute(...).all()` synchronously, you'll need to either:
- (Option A) Make `_compute_discriminator_hint` async and `await session.execute(...)`, OR
- (Option B) Keep it sync but use a sync session fetched via `get_sync_session()`

Looking at the existing kb_search code, `session` IS an `AsyncSession`. Refactor `_compute_discriminator_hint` to be async — change `def _compute_discriminator_hint(hits, session)` to `async def _compute_discriminator_hint(hits, session)` and `await` the session.execute call. Update the unit tests to use `await` and `pytest-asyncio` (which is already a test dep).

Specifically, in `mcp_discriminator.py`:

```python
async def _compute_discriminator_hint(hits, session) -> dict | None:
    # ... initial guards unchanged ...
    
    # Fetch metadata for the candidates (one indexed lookup).
    result = await session.execute(
        select(Document.doc_id, Document.doc_metadata).where(
            Document.doc_id.in_([uuid.UUID(c) for c in candidates])
        )
    )
    rows = result.all()
    metadata_by_doc: dict[str, dict] = {str(row.doc_id): (row.doc_metadata or {}) for row in rows}
    
    # ... rest unchanged ...
```

And in `tests/test_mcp_discriminator_hint.py`, update the unit tests (the ones from Task 1) to be `async def` and `await` the call. The MagicMock-based `_fake_session_for` helper needs to return an `AsyncMock`-style session:

```python
def _fake_session_for(metadata_by_doc: dict[str, dict]):
    from unittest.mock import AsyncMock, MagicMock

    rows = []
    for did, meta in metadata_by_doc.items():
        row = MagicMock()
        row.doc_id = uuid.UUID(did)
        row.doc_metadata = meta
        rows.append(row)

    result_obj = MagicMock()
    result_obj.all.return_value = rows

    session = MagicMock()
    session.execute = AsyncMock(return_value=result_obj)
    return session
```

Each existing unit test that calls `_compute_discriminator_hint(...)` becomes `await _compute_discriminator_hint(...)` and the test function becomes `async def`.

In the integration test, the kb_search call is already async, so the call site change is just removing the `import` shuffle and pinning the await on _compute_discriminator_hint.

- [ ] **Step 4: Run all tests in the file**

```bash
uv run pytest tests/test_mcp_discriminator_hint.py -v
```

Expected: 13 passed (11 unit + 2 integration).

- [ ] **Step 5: Run the full test_mcp_tools suite to check for regressions**

```bash
uv run pytest tests/test_mcp_tools.py -v
```

Expected: still 65 pass. (kb_search's existing behavior is unchanged when no discriminator applies.)

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
uv run ruff format --check src/harbor_clerk/mcp_server.py src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
```

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/mcp_server.py src/harbor_clerk/mcp_discriminator.py tests/test_mcp_discriminator_hint.py
git commit -m "feat(mcp): wire discriminator_hint into kb_search response" \
  -m "_compute_discriminator_hint is now async (uses the async session already in scope in kb_search). Response includes discriminator_hint only when the algorithm returns non-None — absent otherwise (no 'discriminator_hint: null' pollution)." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Major-tier description rewrites (kb_search, kb_batch_search, kb_read_passages, kb_get_document)

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — 4 docstrings
- Create: `tests/test_mcp_tool_descriptions.py` — grep assertions

- [ ] **Step 1: Write the failing description-grep tests**

Create `tests/test_mcp_tool_descriptions.py`:

```python
"""Grep-style assertions that the rewritten kb_* tool descriptions hit
the required surface. These pin the descriptions against future drift —
if a future PR removes a key behavior cue, the regression is loud."""

from harbor_clerk.mcp_server import (
    kb_batch_search,
    kb_get_document,
    kb_read_passages,
    kb_search,
)


def _doc(fn):
    return (fn.__doc__ or "").lower()


# ── Major tier ────────────────────────────────────────────────────────


def test_kb_search_description_mentions_metadata_filter():
    assert "metadata_filter" in _doc(kb_search)


def test_kb_search_description_mentions_discriminator_hint():
    assert "discriminator_hint" in _doc(kb_search)


def test_kb_search_description_mentions_has_more_iteration():
    d = _doc(kb_search)
    assert "has_more" in d
    # Should mention what to do with it — paginate or refine
    assert "paginate" in d or "refine" in d or "iterate" in d


def test_kb_search_description_has_explicit_decline_guidance():
    """The negative-hedging fix lives in this description — must explicitly
    instruct the model to decline cleanly when no match exists."""
    d = _doc(kb_search)
    # Multiple phrasings acceptable — pin the intent, not the exact words
    assert any(
        cue in d
        for cue in (
            "not in the corpus",
            "doesn't exist",
            "say so",
            "decline",
            "no relevant",
        )
    )


def test_kb_batch_search_description_mentions_preference_over_sequential():
    """The under-tooling fix: gpt-4o doesn't reach for batch_search unless
    told to. Description must explicitly recommend it over multiple
    sequential kb_search calls."""
    d = _doc(kb_batch_search)
    assert "kb_search" in d
    # Some form of "prefer over sequential calls"
    assert any(
        cue in d
        for cue in ("prefer", "instead of", "rather than", "use this over")
    )


def test_kb_read_passages_description_recommends_verify_before_answer():
    """The verify-before-claim pattern from the negative-hedging items."""
    d = _doc(kb_read_passages)
    assert "verify" in d or "confirm" in d or "before answering" in d


def test_kb_get_document_description_mentions_metadata_field():
    """kb_get_document's response includes the metadata dict — description
    must surface that as the way to discover metadata_filter keys."""
    d = _doc(kb_get_document)
    assert "metadata" in d
    assert "metadata_filter" in d or "filter keys" in d
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 7 failed (all the assertions fail because the current descriptions don't mention the new surface).

- [ ] **Step 3: Rewrite `kb_search` docstring**

Edit `src/harbor_clerk/mcp_server.py`. Replace the existing `kb_search` docstring (lines 560-593) with:

```python
    """Search the knowledge base by topic, keyword, or question. Hybrid FTS + vector retrieval.

    Use this as the PRIMARY tool to find information in the corpus.

    What you get back:
      - `hits`: ranked chunks with score, doc_id, doc_title, pages, section heading
      - `total_candidates`: how many chunks matched before pagination
      - `has_more`: true if more results exist beyond your window — paginate via `offset`
        or refine your query if you don't yet have the answer
      - `discriminator_hint` (when present): top hits span multiple docs that differ on
        a structured metadata field. Use `metadata_filter` with the suggested key/value
        to pin the right doc. The hint includes `ambiguous_doc_titles`,
        `differing_metadata` (per-doc values), and a `suggestion` string.

    When to iterate:
      - One query returned ambiguous results across multiple docs → call `kb_batch_search`
        with varied query angles, OR use `metadata_filter` (from the discriminator_hint)
        to pin the right doc
      - `has_more` is true and you don't have the answer yet → paginate with `offset`
      - The top hit's chunk text doesn't fully answer the question → call
        `kb_read_passages` on the chunk_id to verify the surrounding text

    How to decline:
      - If retrieved chunks DON'T contain the answer the question asks for (e.g. the
        question mentions an invoice number / contract / person that doesn't appear in
        any retrieved doc), the information is NOT in the corpus — say so plainly. Do
        NOT report a "closest match" as a substitute. Adjacent or partial matches are
        not answers.

    Filters (all optional):
      doc_id: restrict to a single document (mutually exclusive with doc_ids)
      doc_ids: restrict to multiple documents (list of UUIDs, max 50)
      after: only documents updated at or after this ISO datetime
      before: only documents updated before this ISO datetime
      language: chunk language filter ("english" or "french")
      mime_type: document MIME type filter (e.g. "application/pdf")
      metadata_filter: dict of {"namespace.key": value} pairs to match against
        Document.metadata. Use to disambiguate when multiple candidate docs share
        text but differ on a structured field. Example:
        metadata_filter={"sidecar.vendor": "Acme", "sidecar.term_months": 24}.
        Inspect a document's available metadata via kb_get_document.

    detail levels control how much text is returned per hit:
      "full" (default): complete chunk text — best for reading a small
        number of high-confidence results carefully
      "brief": first ~200 characters per chunk (adjustable via brief_chars) —
        use when scanning 20-50 results to identify which are worth
        reading in full via kb_read_passages
      "compact": metadata only (chunk_id, doc_id, doc_title, score, pages,
        language — no text) — use when surveying a broad result set (50+) to
        understand score distribution and document coverage before narrowing down

    faceted: if true, groups hits by document with per-document top_score
      and hit_count — useful for understanding which documents are most
      relevant at a glance
    """
```

- [ ] **Step 4: Rewrite `kb_batch_search` docstring**

Edit `src/harbor_clerk/mcp_server.py`. Find the `kb_batch_search` docstring (around line 759, starting with `"""Run multiple search queries`) and replace with:

```python
    """Run multiple search queries in one call (max 5), grouped per query.

    PREFER THIS OVER multiple sequential kb_search calls when you need to:
      - Triangulate a single answer from multiple angles ("does this contract
        mention X, Y, or Z?")
      - Compare relevance of related concepts in one round-trip
      - Disambiguate ambiguous results from a single kb_search by probing
        with varied query phrasings

    When to use it:
      - One kb_search returned ambiguous results from multiple docs → run 2-3
        varied queries here to see which doc consistently ranks first across
        angles (docs appearing in multiple batch queries are strongly
        corroborated as the right match)
      - You need to check several related facts in one go without serial
        round-trips

    What you get back:
      Per-query result dicts (hits, total_candidates, has_more) plus the same
      `discriminator_hint` field on each query's response when applicable.
      Treat each query's response the same way you'd treat a single kb_search
      response — pagination, iteration, and decline rules are identical.

    How to decline:
      Same as kb_search — if NONE of your queries returned a doc matching the
      question's identifier (invoice number / contract / person / etc.), the
      information is NOT in the corpus. Say so plainly rather than reporting
      adjacent matches as substitutes.

    All filters (doc_id, doc_ids, after, before, language, mime_type,
    metadata_filter) are shared across queries — see kb_search for documentation.
    """
```

- [ ] **Step 5: Rewrite `kb_read_passages` docstring**

Find `kb_read_passages` (around line 889) and replace its docstring with:

```python
    """Read specific passages by chunk_id. Use to verify content before answering.

    Use this after kb_search to:
      - Verify the top hit actually contains the answer the question asks for
        (the chunk text in kb_search results is sometimes truncated or
        out-of-context — read the full passage before committing)
      - Read a few high-confidence hits in full when the chunk text in
        kb_search wasn't enough context to answer
      - Confirm that a specific named entity / number / clause is present
        in the cited chunk before claiming it (the verify-before-answer
        pattern — protects against hallucinating from adjacent text)

    Output: list of passages with full chunk_text, doc_id, doc_title, pages,
    and section heading. Set include_context=True to also get the chunks
    immediately before and after each requested chunk (useful when the
    target chunk references "as discussed above" or similar).

    Take this seriously: before reporting a specific number, date, name, or
    clause as an answer, READ THE CHUNK that supposedly contains it. If the
    chunk doesn't actually contain it, the kb_search hit was a near-miss
    rather than a real match — search with different queries or decline.
    """
```

- [ ] **Step 6: Rewrite `kb_get_document` docstring**

Find `kb_get_document` (around line 1045) and replace its docstring. Look at the current docstring to see what it shows; rewrite as:

```python
    """Get a document's metadata + summary by doc_id. Use to inspect structure before deeper queries.

    What you get back:
      - Title, mime_type, summary (LLM-generated 1-paragraph overview), section
        headings outline, ingestion status, chunk count
      - `metadata`: the document's structured metadata extracted at ingest
        (sidecar facts, Tika fields, frontmatter, etc.). The keys here are
        EXACTLY the filter keys you can pass to kb_search via metadata_filter
        — e.g. `metadata.sidecar.vendor` becomes
        `metadata_filter={"sidecar.vendor": "..."}`

    When to use it:
      - You want to inspect what filter keys exist on a doc before crafting
        a metadata_filter for kb_search
      - You need the summary + structure of a doc to decide whether it's
        worth reading in full via kb_read_document
      - You got a doc_id from kb_search or kb_find_related and want quick
        context before reading chunks

    Output shape: a single dict with the doc's metadata + headings; does NOT
    include chunk text (use kb_read_document or kb_read_passages for that).
    """
```

- [ ] **Step 7: Run the description tests to verify they pass**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 7 passed.

- [ ] **Step 8: Run the full test_mcp_tools suite to check for regressions**

```bash
uv run pytest tests/test_mcp_tools.py -q
```

Expected: 65 pass (description changes don't affect behavior, only the docstring text — the tests are mostly behavioral).

- [ ] **Step 9: Lint + format**

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
uv run ruff format --check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
```

- [ ] **Step 10: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): major-tier tool description rewrites (4 tools, behavior guides)" \
  -m "Rewrites kb_search, kb_batch_search, kb_read_passages, kb_get_document docstrings into the 4-part behavior-guide structure (what / when / output interpretation / decline conditions). Closes the loop on the cross-corpus cross-provider negative-hedging finding by adding explicit decline guidance + verify-before-answer + metadata_filter discovery via kb_get_document.metadata field." -m "7 grep assertions pin the required surface (metadata_filter mention, discriminator_hint mention, decline cues, has_more iteration cue, batch-search-over-sequential preference, etc) against future drift." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Moderate-tier description rewrites (kb_expand_context, kb_find_related, kb_document_outline, kb_corpus_overview)

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — 4 docstrings
- Modify: `tests/test_mcp_tool_descriptions.py` — append grep assertions for moderate tier

- [ ] **Step 1: Append moderate-tier grep tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
# ── Moderate tier ─────────────────────────────────────────────────────


from harbor_clerk.mcp_server import (
    kb_corpus_overview,
    kb_document_outline,
    kb_expand_context,
    kb_find_related,
)


def test_kb_expand_context_description_recommends_pairing_with_read_passages():
    d = _doc(kb_expand_context)
    assert "kb_read_passages" in d


def test_kb_find_related_description_explains_when_it_beats_kb_search():
    d = _doc(kb_find_related)
    # Should explain it's a complement: you have one good hit and want to
    # expand the relevance set
    assert "expand" in d or "complement" in d or "related" in d


def test_kb_document_outline_description_recommends_pairing_with_read_passages():
    d = _doc(kb_document_outline)
    assert "kb_read_passages" in d


def test_kb_corpus_overview_description_recommends_first_use():
    """kb_corpus_overview should be flagged as the right FIRST call when
    the model doesn't know the corpus shape."""
    d = _doc(kb_corpus_overview)
    assert "first" in d or "start" in d
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 4 new failures (in addition to the 7 already passing from Task 3).

- [ ] **Step 3: Rewrite `kb_expand_context` docstring**

Find `kb_expand_context` (around line 978) and replace its docstring with:

```python
    """Read N chunks immediately before/after a given chunk_id.

    Use after kb_search or kb_read_passages when the chunk you got back
    doesn't show enough surrounding context to fully understand it —
    e.g. when the chunk text references "as described above" or "see
    section 4" or the answer spans a chunk boundary.

    Pair with kb_read_passages: kb_read_passages reads SPECIFIC chunks you
    already know about; kb_expand_context fetches the surrounding CONTEXT
    (n chunks before + n after the target).

    n controls the window size (default 2 chunks each direction = 5 total).
    Returns the chunks in order with the target chunk marked is_target=True.
    """
```

- [ ] **Step 4: Rewrite `kb_find_related` docstring**

Find `kb_find_related` (around line 1417) and replace its docstring with:

```python
    """Find documents related to a given doc_id by semantic overlap.

    Use to EXPAND a relevance set: you have one good hit from kb_search and
    want to find docs that cover the same topic / cite each other / share
    entities. Complement to kb_search — kb_search starts from a QUERY,
    kb_find_related starts from a KNOWN DOC.

    When to use:
      - Found one relevant doc; need to see what else in the corpus is
        similar (e.g. "this is the Q3 board minutes — what other meeting
        minutes discuss the same topics?")
      - Need to triangulate a fact: read related docs to confirm a claim
      - Mapping a corpus around a known anchor doc

    Returns top-K related docs with their titles + relevance scores. To
    actually read the related docs' content, follow up with kb_get_document
    or kb_search.
    """
```

- [ ] **Step 5: Rewrite `kb_document_outline` docstring**

Find `kb_document_outline` (around line 1364) and replace its docstring with:

```python
    """Get a document's section structure (table of contents).

    Use to navigate inside a document by section heading. Pair with
    kb_read_passages to read specific sections you've identified.

    When to use:
      - You have a doc_id and want to know WHERE in the doc to look
        (e.g. "the contract has a Termination section — let me read just
        that")
      - You're answering a question about doc structure ("how many
        sections does this report have?")

    Returns the heading hierarchy with chunk_ids per heading, so you can
    follow up with kb_read_passages([chunk_id, ...]) to read a specific
    section without dumping the full document.
    """
```

- [ ] **Step 6: Rewrite `kb_corpus_overview` docstring**

Find `kb_corpus_overview` (around line 1142) and replace its docstring with:

```python
    """Survey the corpus: doc types, date ranges, sample titles.

    Use FIRST when you don't know the corpus shape — what kinds of documents
    exist, what time periods are covered, what topics dominate. This is the
    right starting tool when the user's question is broad ("what's in this
    corpus?") or when you're not sure what to search for.

    What you get back:
      - Document count by type (invoice, contract, policy, etc.)
      - Date range of documents in the corpus
      - Sample titles (first `limit` docs by recency)
      - Top entities / topics if available

    When to use:
      - First call in a new conversation when you don't know the corpus
      - User asks "what kinds of docs do you have?" / "what topics?"
      - You want to scope a search ("are there any 2024 contracts?") before
        running kb_search

    Does NOT return chunk text — for actual content, follow up with
    kb_search or kb_list_recent.
    """
```

- [ ] **Step 7: Run tests + lint + commit**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 11 passed (7 major + 4 moderate).

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
uv run ruff format --check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
```

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): moderate-tier tool description rewrites (4 tools)" \
  -m "kb_expand_context / kb_find_related / kb_document_outline / kb_corpus_overview now lead with the use-case + pair-with guidance. kb_corpus_overview is explicitly flagged as the right FIRST call in a new conversation." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Light-tier description rewrites (kb_entity_search, kb_entity_overview, kb_entity_cooccurrence, kb_list_recent, kb_read_document)

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — 5 docstrings
- Modify: `tests/test_mcp_tool_descriptions.py` — append grep assertions for light tier

- [ ] **Step 1: Append light-tier grep tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
# ── Light tier ────────────────────────────────────────────────────────


from harbor_clerk.mcp_server import (
    kb_entity_cooccurrence,
    kb_entity_overview,
    kb_entity_search,
    kb_list_recent,
    kb_read_document,
)


def test_kb_entity_search_description_explains_when_it_beats_freetext():
    d = _doc(kb_entity_search)
    # Should clarify it's for entity-specific queries vs free-text
    assert "entity" in d
    assert "person" in d or "organization" in d or "named" in d


def test_kb_entity_overview_description_mentions_per_doc_option():
    d = _doc(kb_entity_overview)
    # Should mention you can scope to a single doc
    assert "doc_id" in d


def test_kb_entity_cooccurrence_description_explains_use_case():
    d = _doc(kb_entity_cooccurrence)
    # Should clarify it surfaces entities appearing together
    assert "together" in d or "co-occur" in d or "with" in d


def test_kb_list_recent_description_recommends_for_temporal_queries():
    d = _doc(kb_list_recent)
    assert "recent" in d or "newest" in d or "latest" in d


def test_kb_read_document_description_clarifies_vs_kb_get_document():
    """kb_read_document returns full text; kb_get_document returns metadata.
    Description should make the distinction explicit."""
    d = _doc(kb_read_document)
    assert "kb_get_document" in d or "full text" in d or "full document" in d
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 5 new failures.

- [ ] **Step 3: Rewrite light-tier docstrings**

Edit `src/harbor_clerk/mcp_server.py`. Replace each of these 5 docstrings:

**`kb_entity_search`** (around line 1564):
```python
    """Find documents that mention a specific named entity (person, organization, place).

    Use when:
      - The question is about a SPECIFIC named entity (e.g. "What did Alice
        Johnson say in board meetings?", "Which contracts mention Acme Corp?")
      - You want to disambiguate between similarly-named entities
      - kb_search by free-text returned too many false matches because the
        entity name is also a common word

    Returns docs containing the entity, with mention count + sample chunks.
    Pair with kb_read_passages to read the specific mentions in context.

    For broader entity surveys (which entities appear most, which are linked),
    use kb_entity_overview instead.
    """
```

**`kb_entity_overview`** (around line 1679):
```python
    """Survey entities in the corpus (or scoped to a single doc).

    Use when:
      - The user asks "who/what is mentioned in this corpus?"
      - You want to find the most-discussed entities by category
        (people, organizations, places, dates)
      - You want a quick entity inventory for a specific document
        (pass doc_id to scope)

    Returns top entities by mention count, grouped by type. Pair with
    kb_entity_search to find docs mentioning a specific entity you spot
    in the overview, or kb_entity_cooccurrence to see which entities
    appear together.
    """
```

**`kb_entity_cooccurrence`** (around line 1771):
```python
    """Find which entities appear together in the same documents or chunks.

    Use when:
      - You want to understand relationships between entities
        (e.g. "which executives are mentioned alongside Project X?",
        "who works with whom?")
      - The question is about WHO/WHAT shares context with a known entity
      - You're mapping a network of related people/orgs across the corpus

    Returns pairs of co-occurring entities with their joint frequency.
    Pair with kb_entity_search on a specific entity from the result to
    see the actual documents where the co-occurrence happens.
    """
```

**`kb_list_recent`** (around line 1091):
```python
    """List the most recently-added documents in the corpus.

    Use for temporal queries — "what was added last week", "show me the
    newest contracts", "any recent meeting minutes about X". Quick way to
    see what's NEW without running a content search.

    Returns up to `limit` docs (default 20) sorted by created_at descending.
    Use kb_search with `after` / `before` filters if you need a specific
    date range rather than just "most recent".
    """
```

**`kb_read_document`** (around line 1900):
```python
    """Read the full text of a document by doc_id.

    Use when you need the COMPLETE document content — not just metadata or
    a few chunks. Different from kb_get_document, which returns metadata +
    summary + structure but no chunk text.

    CAUTION: full documents can be very large. Prefer kb_search +
    kb_read_passages for targeted reading of specific sections. Use this
    tool only when:
      - The document is short enough to read whole (check kb_get_document
        first to see size)
      - The question requires synthesizing across the entire document
        rather than locating a specific fact
      - kb_search couldn't pin a specific chunk and you need to scan more
        broadly

    Returns the full text in chunk order, with optional pagination.
    """
```

- [ ] **Step 4: Run tests + lint + commit**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 16 passed (7 + 4 + 5).

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
uv run ruff format --check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
```

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): light-tier tool description rewrites (5 tools)" \
  -m "Entity-search trio + kb_list_recent + kb_read_document: clarity polish + cross-reference to peer tools. kb_read_document now explicitly contrasted with kb_get_document so models pick the right one for the question." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Admin-tier description rewrites (kb_ingest_status, kb_reprocess, kb_system_health)

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` — 3 docstrings
- Modify: `tests/test_mcp_tool_descriptions.py` — append grep assertions for admin tier

- [ ] **Step 1: Append admin-tier grep tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
# ── Admin tier ────────────────────────────────────────────────────────


from harbor_clerk.mcp_server import (
    kb_ingest_status,
    kb_reprocess,
    kb_system_health,
)


def test_kb_ingest_status_description_clarifies_admin_use():
    d = _doc(kb_ingest_status)
    # Should clarify it's for inspecting ingestion progress (operator-facing)
    assert "ingest" in d or "pipeline" in d


def test_kb_reprocess_description_warns_admin_only():
    d = _doc(kb_reprocess)
    # Should clarify it's destructive (re-runs the pipeline)
    assert "admin" in d or "re-run" in d or "re-runs" in d or "reprocess" in d


def test_kb_system_health_description_clarifies_diagnostic_purpose():
    d = _doc(kb_system_health)
    assert "health" in d or "diagnostic" in d or "status" in d
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 3 new failures.

- [ ] **Step 3: Rewrite admin-tier docstrings**

Edit `src/harbor_clerk/mcp_server.py`. Replace each of these 3 docstrings:

**`kb_ingest_status`** (around line 1282):
```python
    """Inspect a document's ingestion pipeline status (operator-facing).

    Use when troubleshooting why a doc isn't appearing in searches or
    when investigating ingestion failures. Returns the stage-by-stage
    status (extract, ocr, chunk, embed, entities, summarize, finalize)
    plus any error messages.

    Rarely needed during normal query flow — models should reach for this
    only when the user is asking about ingestion state, not content.
    """
```

**`kb_reprocess`** (around line 1329):
```python
    """Re-run the ingestion pipeline for a specific document. ADMIN-ONLY.

    Use when a previously-ingested doc has issues (bad summary, missing
    entities, OCR errors) and the operator wants to reprocess from
    scratch without re-uploading. Re-runs the full extract → ocr → chunk →
    embed → entities → summarize → finalize chain.

    Rarely useful during normal query flow. Requires admin permissions.
    """
```

**`kb_system_health`** (around line 2036):
```python
    """Check HC's system health (PostgreSQL, storage, Tika, reranker). DIAGNOSTIC.

    Use when investigating system issues — "why are searches slow", "is the
    embedder up", "is storage reachable". Returns per-component status.

    Rarely useful during normal query flow — models should reach for this
    only when the user is troubleshooting infrastructure.
    """
```

- [ ] **Step 4: Run tests + lint + commit**

```bash
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: 19 passed (7 + 4 + 5 + 3 — all 16 tools covered).

```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
uv run ruff format --check src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
```

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): admin-tier tool description rewrites (3 tools)" \
  -m "kb_ingest_status / kb_reprocess / kb_system_health get clarity polish + the explicit 'rarely useful during normal query flow' nudge so models don't reach for them in content-driven conversations." -m "Completes all 16 kb_* tool description rewrites for PR-D." \
  -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full-suite regression sanity check

**No new files or code — verification only.**

- [ ] **Step 1: Full pytest suite**

```bash
uv run pytest tests/ -q 2>&1 | tail -10
```

Expected: existing 931+ pass + 13 new discriminator tests + 19 new description tests = ~963 pass, no regressions.

- [ ] **Step 2: Ruff check + format check**

```bash
uv run ruff check src/harbor_clerk/ tests/
uv run ruff format --check src/harbor_clerk/ tests/
```

Expected: clean.

- [ ] **Step 3: Frontend type-check** (no frontend changes, but verify nothing leaked):

```bash
cd frontend && npm run lint && npx tsc --noEmit
```

Expected: clean (only pre-existing warnings).

- [ ] **Step 4: No commit needed — this is a verification gate.**

If any check fails, return to the relevant task to fix before live validation.

---

## Task 8: Live validation against synthetic corpus on both providers

**Prerequisites:**
- HC must be rebuilt + restarted with this branch's code so the new descriptions and `discriminator_hint` are live in the running MCP server
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HC_API_KEY` (Synthetic-scoped) all set
- PR-B's frozen `synthetic.yaml` available

- [ ] **Step 1: Verify HC is reachable + on the new build**

```bash
curl -s http://localhost:8100/api/system/health | head -3
```

Expected: 200 with the build hash matching the HEAD of this branch. If hash is stale, the user needs to rebuild + restart.

- [ ] **Step 2: Re-run synthetic eval with Sonnet (refresh captures so the new descriptions are exercised)**

```bash
RUN_ID="pr-d-sonnet-$(date +%Y%m%d-%H%M%S)"
OPENAI_API_KEY="$OPENAI_API_KEY" \
HC_API_KEY="$SYNTHETIC_HC_API_KEY" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id "$RUN_ID" \
  --mode answer-eval \
  --corpora synthetic \
  --models claude-sonnet-4-6 \
  --label pr-d-synthetic-sonnet \
  --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" \
  --api-base http://localhost:8100 \
  --refresh \
  2>&1 | tee /tmp/pr-d-sonnet-run.log | tail -30
```

`--refresh` is important: it forces re-capture (otherwise the eval would reuse PR-C's captures, which were generated against PR-D-less descriptions).

Expected: 29 items captured + judged. Compare overall correctness / groundedness / completeness to PR-B's first-run results (correctness 3.33 / 3.57 / 2.73).

- [ ] **Step 3: Re-run with gpt-4o**

```bash
RUN_ID="pr-d-gpt4o-$(date +%Y%m%d-%H%M%S)"
OPENAI_API_KEY="$OPENAI_API_KEY" \
HC_API_KEY="$SYNTHETIC_HC_API_KEY" \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id "$RUN_ID" \
  --mode answer-eval \
  --corpora synthetic \
  --models gpt-4o \
  --label pr-d-synthetic-gpt4o \
  --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" \
  --api-base http://localhost:8100 \
  --refresh \
  2>&1 | tee /tmp/pr-d-gpt4o-run.log | tail -30
```

Expected: 29/29 → compare to PR-C's first-run gpt-4o results (correctness 2.83 / 3.79 / 2.83).

- [ ] **Step 4: Save the summaries for PR comment**

```bash
cat "$HOME/Library/Application Support/Harbor Clerk/test-corpora/answer-eval/reports/pr-d-synthetic-sonnet/summary.json"
cat "$HOME/Library/Application Support/Harbor Clerk/test-corpora/answer-eval/reports/pr-d-synthetic-gpt4o/summary.json"
```

Both should show n=29 with the rolled-up metrics.

- [ ] **Step 5: Inspect captures for discriminator_hint usage**

Pick a few items where you'd expect the hint to fire (the boundary-doc items from PR-B/C: `synth-onboarding-mgr-0061`, `synth-vcontract-law-0131`, `synth-mkt-budget-0221`, `synth-policy-effective-0192`). Read the captured tool_transcript:

```bash
python3 -c "
import json
for item_id in ['synth-vcontract-law-0131_vendor_contract', 'synth-mkt-budget-0221_marketing_brief', 'synth-policy-effective-0192_policy_doc']:
    for model in ['claude-sonnet-4-6', 'gpt-4o']:
        path = '$HOME/Library/Application Support/Harbor Clerk/test-corpora/answer-eval/captures/synthetic/' + model + '/' + item_id + '.json'
        try:
            c = json.load(open(path))
            transcript = c.get('tool_transcript', [])
            saw_hint = any('discriminator_hint' in t.get('result_summary', '') for t in transcript)
            used_filter = any('metadata_filter' in str(t.get('args', {})) for t in transcript)
            print(f'{model} {item_id}: hint_seen={saw_hint} filter_used={used_filter}')
        except FileNotFoundError:
            pass
"
```

Note any items where the model SAW the hint but DIDN'T use metadata_filter — those are the description-tuning targets for v2.

- [ ] **Step 6: Record findings for PR description**

Write to `/tmp/pr-d-validation-notes.md`:
- Per-provider score deltas (PR-D Sonnet vs PR-B Sonnet; PR-D gpt-4o vs PR-C gpt-4o)
- Which items moved (item-level diff on the boundary-doc cases + the negative item)
- Hint-fire rate and hint-acted-on rate (from Step 5's grep)
- Any surprises in the captures (e.g. a model that didn't reach for kb_batch_search even with the new description)

These go into the PR body.

---

## Task 9: Self-review, fresh-eyes review, open PR

- [ ] **Step 1: Diff summary against `main`**

```bash
git fetch origin main --quiet
git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD
```

Expected: ~7-8 commits, touching `mcp_server.py`, `mcp_discriminator.py`, the two new test files.

- [ ] **Step 2: Final lint + format pass**

```bash
uv run ruff check src/harbor_clerk/ tests/
uv run ruff format --check src/harbor_clerk/ tests/
```

- [ ] **Step 3: Dispatch fresh-eyes code reviewer** (per MEMORY.md standing directive — minimal prompt, no carve-outs)

Use the Agent tool with `subagent_type: feature-dev:code-reviewer` and a minimal prompt: "Review the diff `git diff origin/main..HEAD` on branch `feat/mcp-tool-descriptions-discriminator-hint`. Spec is at `docs/superpowers/specs/2026-05-24-mcp-tool-descriptions-and-discriminator-hint-design.md`. Identify bugs, security issues, design problems with ≥80 confidence."

Address ≥80-confidence findings inline.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/mcp-tool-descriptions-discriminator-hint
```

- [ ] **Step 5: Open the PR with the validation results**

Use the validation notes from `/tmp/pr-d-validation-notes.md` to write the PR body. Use `gh pr create --base main --head feat/mcp-tool-descriptions-discriminator-hint --title "feat(mcp): tool description rewrites + kb_search discriminator_hint" --body-file /tmp/pr-d-body.md`.

PR body should cover:
- The two changes (16-tool description rewrite + discriminator_hint) and why bundling
- The 4-tier rewrite structure
- The discriminator_hint trigger conditions + output shape (with an example)
- Validation results: PR-D vs PR-B/C deltas; hint-fire rate; hint-acted-on rate
- Known limitations / queued follow-ups (PR-E, PR-G, prompt-tuning)
- Pointer to the spec + plan docs

- [ ] **Step 6: Mark PR-D done.**

---

## Self-Review Notes (for the agentic worker)

**1. Spec coverage:** Every section of `2026-05-24-mcp-tool-descriptions-and-discriminator-hint-design.md` has a corresponding task:
- discriminator_hint algorithm → Task 1
- discriminator_hint integration into kb_search → Task 2
- Major-tier description rewrites → Task 3
- Moderate-tier → Task 4
- Light-tier → Task 5
- Admin-tier → Task 6
- Regression + ruff → Task 7
- Live validation → Task 8
- Fresh-eyes review + PR → Task 9

The spec's 4 trigger conditions + ε threshold are encoded in Task 1's algorithm. The 4-part description structure + the per-tier guidance are encoded in Tasks 3-6's docstring text.

**2. Placeholder scan:** No `TBD` / "TODO" / "implement later" in any code step. The validation notes file in Task 8 step 6 is filled in from real run output, not placeholder text.

**3. Type consistency:**
- `_compute_discriminator_hint(hits, session) -> dict | None` is async after Task 2's refactor (Task 1 starts sync, Task 2 promotes — the unit tests get updated in Task 2 step 3).
- `discriminator_hint` field shape (`ambiguous_doc_ids`, `ambiguous_doc_titles`, `differing_metadata`, `suggestion`) is consistent across Task 1's algorithm, Task 1's tests, Task 2's integration test, and Task 8's transcript inspection.
- Tool name references (`kb_search`, `kb_batch_search`, etc.) are consistent across the description rewrites and the grep tests.
- The `_make_hashable` helper handles the dict / list / primitive cases — needed because some metadata values (e.g. `tags: ["a", "b"]`) are unhashable lists, and `set()`-based distinctness needs hashable items.
