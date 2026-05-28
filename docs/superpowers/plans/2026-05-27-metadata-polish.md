# Metadata Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a shared `_apply_jsonb_metadata_filter()` helper (eliminating duplication between `hybrid_search` and `_query_documents_by_date`) AND add `dc:identifier → isbn` to TIKA_FIELD_ALIASES so EPUB ISBN becomes queryable via `metadata_filter`.

**Architecture:** Two surgical changes, both following patterns established by PR #414. The JSONB dedup mirrors the existing `_apply_email_metadata_filter()` extraction; the TIKA alias addition slots into the existing whitelist map.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, pytest.

**Spec:** `docs/superpowers/specs/2026-05-27-metadata-polish-design.md`

**File map (created or modified):**

- Modify: `src/harbor_clerk/search.py` — add `_apply_jsonb_metadata_filter()` helper, refactor `hybrid_search` call site
- Modify: `src/harbor_clerk/mcp_lookup_tools.py` — refactor `_query_documents_by_date` call site to use the new helper
- Modify: `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py` — add `dc:identifier → isbn` to `TIKA_FIELD_ALIASES`
- Modify: `tests/test_search_metadata_filter.py` — add unit test for `_apply_jsonb_metadata_filter()` in isolation
- Modify: `tests/ingest/test_tika_metadata_extractor.py` — add test for `dc:identifier → isbn` alias

**Conventions:**
- All work on branch `chore/metadata-polish` in the worktree at `.worktrees/metadata-polish/`.
- Tests run via `uv run pytest tests/<file>.py -v` from the worktree root.
- Each task ends with a commit; messages follow `refactor(<scope>): <what>` / `feat(<scope>): <what>` per repo convention.

---

## Task 1: Extract `_apply_jsonb_metadata_filter()` helper + refactor `hybrid_search`

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_search_metadata_filter.py`

- [ ] **Step 1: Write the failing unit test**

Append to `tests/test_search_metadata_filter.py`:

```python
def test_apply_jsonb_metadata_filter_string_value_appends_or_clause():
    """String value: appends an OR(containment, existence) predicate."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter({"sidecar.vendor": "Acme"}, doc_conditions)
    # One predicate appended
    assert len(doc_conditions) == 1
    # Rendered SQL contains both the @> containment and the ? existence
    compiled = str(doc_conditions[0].compile(compile_kwargs={"literal_binds": True}))
    assert "@>" in compiled
    assert " ? " in compiled or "?" in compiled  # JSONB existence operator


def test_apply_jsonb_metadata_filter_int_value_appends_containment_only():
    """Non-string scalar: containment only, no existence OR-branch."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter({"sidecar.term_months": 24}, doc_conditions)
    assert len(doc_conditions) == 1
    compiled = str(doc_conditions[0].compile(compile_kwargs={"literal_binds": True}))
    assert "@>" in compiled
    # No OR / existence — just plain containment
    assert " OR " not in compiled.upper()


def test_apply_jsonb_metadata_filter_multiple_keys_append_in_order():
    """Multiple keys: each becomes its own predicate appended in input order."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter(
        {"sidecar.vendor": "Acme", "frontmatter.author": "Alice"},
        doc_conditions,
    )
    assert len(doc_conditions) == 2


def test_apply_jsonb_metadata_filter_rejects_multi_dot_path():
    """Nested paths (two dots) raise ValueError."""
    import pytest
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    with pytest.raises(ValueError, match="exactly 'namespace.key'"):
        _apply_jsonb_metadata_filter(
            {"sidecar.contract.id": "x"}, [],
        )


def test_apply_jsonb_metadata_filter_rejects_empty_segment():
    """Empty namespace or key raises ValueError."""
    import pytest
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    with pytest.raises(ValueError, match="non-empty namespace and key"):
        _apply_jsonb_metadata_filter({".vendor": "Acme"}, [])

    with pytest.raises(ValueError, match="non-empty namespace and key"):
        _apply_jsonb_metadata_filter({"sidecar.": "Acme"}, [])


def test_apply_jsonb_metadata_filter_returns_none():
    """Helper mutates doc_conditions in place; returns None (companion to
    _apply_email_metadata_filter which returns the remaining dict)."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    result = _apply_jsonb_metadata_filter({"sidecar.vendor": "Acme"}, [])
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/test_search_metadata_filter.py -v -k "_apply_jsonb_metadata_filter"
```

Expected: 6 FAIL with `ImportError: cannot import name '_apply_jsonb_metadata_filter'`.

- [ ] **Step 3: Extract the helper**

In `src/harbor_clerk/search.py`, locate the existing `_apply_email_metadata_filter()` function. Add a new helper immediately after it (so the two related helpers sit together):

```python
def _apply_jsonb_metadata_filter(
    metadata_filter: dict[str, Any],
    doc_conditions: list,
) -> None:
    """Translate non-email.* metadata_filter keys into JSONB predicates.

    Each `<namespace>.<key>: value` pair becomes either:
      - JSONB @> containment: doc_metadata @> '{"ns": {"key": value}}'
        (matches scalar metadata)
      - OR JSONB ? existence: doc_metadata->'ns'->'key' ? 'value'
        (matches list-valued metadata containing the scalar)

    The OR lets a caller use a scalar filter value to match either a
    scalar metadata field (sidecar.vendor: "Acme") OR a list-valued one
    (frontmatter.tags: ["alpha", "beta"]) without knowing the shape.
    Only string filter values get the OR — JSONB `?` operator requires
    string keys, so non-string scalars (numbers, bools) use containment
    only.

    Mutates doc_conditions in place by appending predicates; returns
    None. Companion to _apply_email_metadata_filter — that one strips
    email.* keys and returns the remaining dict; this one consumes
    that remaining dict.

    Raises ValueError on malformed keys (must be 'namespace.key', one
    dot, two non-empty segments). Nested paths are not supported in v1.
    """
    for path, value in metadata_filter.items():
        if path.count(".") != 1:
            raise ValueError(
                f"metadata_filter keys must be exactly 'namespace.key' (one dot, two segments); "
                f"got {path!r}. Nested paths are not supported in v1."
            )
        ns, _, key = path.partition(".")
        if not ns or not key:
            raise ValueError(
                f"metadata_filter keys must have a non-empty namespace and key, got {path!r}"
            )
        containment = Document.doc_metadata.op("@>")(func.cast({ns: {key: value}}, JSONB))
        if isinstance(value, str):
            existence = Document.doc_metadata[ns][key].op("?")(value)
            doc_conditions.append(or_(containment, existence))
        else:
            doc_conditions.append(containment)
```

- [ ] **Step 4: Refactor `hybrid_search` call site to use the helper**

In `src/harbor_clerk/search.py`, find the second `if metadata_filter:` block (around line 205) — the one that does the JSONB translation (NOT the email.* pre-pass). Replace the entire `if metadata_filter:` block (including the `for path, value in metadata_filter.items():` loop and its body) with a one-line call:

```python
    if metadata_filter:
        _apply_jsonb_metadata_filter(metadata_filter, doc_conditions)
```

The surrounding context (the email.* pre-pass above, the `if doc_conditions:` block below) is unchanged.

- [ ] **Step 5: Update `__all__` if it exists**

If `src/harbor_clerk/search.py` exports symbols via `__all__`, add `"_apply_jsonb_metadata_filter"` alongside `"_apply_email_metadata_filter"`. (Check with `grep "__all__" src/harbor_clerk/search.py` — if no `__all__` is defined, skip this step.)

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/test_search_metadata_filter.py -v
```

Expected: all tests pass (the 6 new unit tests + all existing tests — hybrid_search regression coverage already exists).

- [ ] **Step 7: Run full search regression suite**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/test_search_filtered.py tests/test_search_metadata_filter.py tests/test_hybrid_search_with_rerank.py tests/test_find_all.py -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 8: Ruff clean**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run ruff check src/harbor_clerk/search.py tests/test_search_metadata_filter.py
```

Expected: clean.

- [ ] **Step 9: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
git add src/harbor_clerk/search.py tests/test_search_metadata_filter.py
git commit -m "refactor(search): extract _apply_jsonb_metadata_filter helper

Mirrors the _apply_email_metadata_filter() extraction from PR #414.
The JSONB-translation block (containment + optional existence OR for
string values) is now a single module-level function called from
hybrid_search. Identical block in _query_documents_by_date is
updated in the next commit.

Six unit tests pin the helper's contract: string vs int value
branching, multi-key ordering, raises on bad paths, returns None
(mutation-only convention, distinct from email helper which strips +
returns).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Refactor `_query_documents_by_date` call site

**Files:**
- Modify: `src/harbor_clerk/mcp_lookup_tools.py`

- [ ] **Step 1: Import the new helper**

In `src/harbor_clerk/mcp_lookup_tools.py`, find the existing import line (around line 27):

```python
from harbor_clerk.search import _apply_email_metadata_filter
```

Change to:

```python
from harbor_clerk.search import _apply_email_metadata_filter, _apply_jsonb_metadata_filter
```

- [ ] **Step 2: Replace the inline JSONB loop in `_query_documents_by_date`**

In the same file, find the second `if metadata_filter:` block (around line 396 — the one with the `for path, value in metadata_filter.items():` loop that builds JSONB predicates). Replace the entire block with:

```python
    if metadata_filter:
        _apply_jsonb_metadata_filter(metadata_filter, doc_conditions)
```

The email.* pre-pass above stays unchanged. The `for cond in doc_conditions: stmt = stmt.where(cond)` loop below stays unchanged.

- [ ] **Step 3: Run targeted tests**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/test_mcp_lookup_tools.py -v 2>&1 | tail -10
```

Expected: all pass. The existing tests cover the metadata_filter behavior end-to-end; the refactor preserves it.

- [ ] **Step 4: Run full regression**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/ -x --ignore=tests/integration 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 5: Ruff clean**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run ruff check src/harbor_clerk/mcp_lookup_tools.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
git add src/harbor_clerk/mcp_lookup_tools.py
git commit -m "refactor(mcp): use shared _apply_jsonb_metadata_filter in documents_by_date

Closes the duplicate JSONB-translation block between hybrid_search
and _query_documents_by_date. Both now route through the same helper
(extracted in the previous commit). The 'If this diverges from
search.py, both should be updated together' comment is no longer
needed — there's only one site to update.

No behavior change. Existing tests pass unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add `dc:identifier → isbn` to TIKA_FIELD_ALIASES

**Files:**
- Modify: `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py`
- Test: `tests/ingest/test_tika_metadata_extractor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/ingest/test_tika_metadata_extractor.py`:

```python
def test_tika_extractor_aliases_dc_identifier_to_isbn(httpserver: HTTPServer, monkeypatch):
    """EPUB and some PDFs carry ISBN as dc:identifier. The alias map
    routes it to a flat `tika.isbn` key for metadata_filter use."""
    tika_response = {
        "dc:identifier": "978-0-13-468599-1",
        "dc:title": "The Book",
        "Content-Type": "application/epub+zip",
    }
    httpserver.expect_request("/meta").respond_with_json(tika_response)

    monkeypatch.setattr(
        "harbor_clerk.ingest.metadata_extractors.tika_metadata.get_settings",
        lambda: type("S", (), {"tika_url": httpserver.url_for("").rstrip("/")})(),
    )

    extractor = TikaMetadataExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="book", mime_type="application/epub+zip"),
        raw_bytes=b"PK\x03\x04...",
        source_path=None,
    )

    assert out == {
        "isbn": "978-0-13-468599-1",
        "title": "The Book",
        "content_type": "application/epub+zip",
    }
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/ingest/test_tika_metadata_extractor.py::test_tika_extractor_aliases_dc_identifier_to_isbn -v
```

Expected: FAIL — the `out` dict won't contain `"isbn"` because the alias isn't registered yet.

- [ ] **Step 3: Add the alias**

In `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py`, locate the `TIKA_FIELD_ALIASES` dict (around line 27). Insert the new entry inside the Dublin Core group, between `dc:language` and `dcterms:created`:

```python
TIKA_FIELD_ALIASES: dict[str, str] = {
    # Dublin Core
    "dc:creator": "author",
    "dc:title": "title",
    "dc:subject": "subject",
    "dc:description": "description",
    "dc:language": "language",
    # dc:identifier is intentionally generic in Dublin Core (can be ISBN,
    # DOI, URN, or arbitrary string). EPUBs and many PDFs carry ISBN here;
    # we alias to `isbn` because that's the most common HC-corpus use.
    # Non-ISBN values still round-trip to tika.isbn; consumers can filter
    # by shape as needed.
    "dc:identifier": "isbn",
    "dcterms:created": "created_at",
    "dcterms:modified": "modified_at",
    "meta:keyword": "keywords",
    # ... rest unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run pytest tests/ingest/test_tika_metadata_extractor.py -v
```

Expected: all tests pass (the new test + all existing tests, including `test_tika_field_aliases_has_no_duplicate_target_keys` which should still see `["page_count"]` as the only duplicate).

- [ ] **Step 5: Ruff clean**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run ruff check src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py tests/ingest/test_tika_metadata_extractor.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
git add src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py tests/ingest/test_tika_metadata_extractor.py
git commit -m "feat(tika): alias dc:identifier to isbn

EPUBs and many PDFs carry ISBN as dc:identifier in their Dublin Core
metadata. Tika's /meta endpoint emits it; the alias map was dropping
it. Adding one entry to TIKA_FIELD_ALIASES routes it to tika.isbn,
which is now queryable via metadata_filter={'tika.isbn': '...'}.

dc:identifier is intentionally generic in the DC spec (can also be
DOI, URN, etc.). Non-ISBN values still round-trip to tika.isbn; HC
doesn't validate the shape in v1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Final verification + push + PR

**Files:** None modified. Verification only.

- [ ] **Step 1: Full Python suite + lint**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
uv run ruff check . 2>&1 | tail -3
uv run ruff format --check . 2>&1 | tail -3
uv run pytest tests/ -x --ignore=tests/integration 2>&1 | tail -5
```

Expected: ruff clean; all non-integration tests pass.

- [ ] **Step 2: Dispatch fresh-eyes code review per the standing directive**

Per `MEMORY.md` standing directive, before opening a PR for any substantive change, dispatch a `feature-dev:code-reviewer` agent against the branch tip with a minimal prompt. For this PR, the prompt should be:

> Review the branch `chore/metadata-polish` against `main` in `/Users/alex/mcp-gateway/.worktrees/metadata-polish`. Two changes: shared `_apply_jsonb_metadata_filter` helper extracted from two duplicate call sites; new `dc:identifier → isbn` Tika alias. Spec: `docs/superpowers/specs/2026-05-27-metadata-polish-design.md`. Plan: `docs/superpowers/plans/2026-05-27-metadata-polish.md`. Flag any ≥80 confidence findings.

Address any findings before pushing.

- [ ] **Step 3: Push branch**

```bash
cd /Users/alex/mcp-gateway/.worktrees/metadata-polish
git push -u origin chore/metadata-polish
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "refactor(search): shared JSONB metadata_filter helper + EPUB ISBN alias" --body "$(cat <<'EOF'
## Summary

Two small metadata-layer cleanups bundled together:

1. **`_apply_jsonb_metadata_filter()` shared helper.** Closes the JSONB-translation duplication PR #414's fresh-eyes review flagged. The byte-identical 15-line block at `search.py:205-219` and `mcp_lookup_tools.py:396-411` (both files even carried a 'keep in sync' comment) is now one helper. Mirrors the `_apply_email_metadata_filter()` extraction PR #414 already established.

2. **`dc:identifier → isbn` Tika alias.** EPUB and many PDF files carry ISBN as the Dublin Core `dc:identifier` field. Tika's `/meta` endpoint emits it; the whitelist alias map was dropping it. One-line addition makes `metadata_filter={'tika.isbn': '978-...'}` work.

## What changed

| Change | File | Lines |
|---|---|---|
| New `_apply_jsonb_metadata_filter()` helper | `src/harbor_clerk/search.py` | ~30 added |
| `hybrid_search` uses the helper | `src/harbor_clerk/search.py` | -15 + 2 |
| `_query_documents_by_date` uses the helper | `src/harbor_clerk/mcp_lookup_tools.py` | -15 + 2 (+1 import line) |
| `dc:identifier → isbn` alias | `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py` | +1 + comment |
| Unit tests for `_apply_jsonb_metadata_filter()` | `tests/test_search_metadata_filter.py` | +6 tests |
| Test for ISBN alias | `tests/ingest/test_tika_metadata_extractor.py` | +1 test |

## Test plan

- [x] \`uv run pytest tests/ -x --ignore=tests/integration\` — all pass
- [x] \`uv run ruff check . && uv run ruff format --check .\` — clean
- [x] Fresh-eyes code-reviewer dispatched, ≥80 confidence findings addressed

## Out of scope (captured in pr_followups.md)

- \`parse_eml.body_text\` cleanup (field is dead in production but used by tests)
- TIKA_FIELD_ALIASES: EXIF date-taken / GPS / camera, Office docx custom props
- Shape-3 location filtering (filename / source_path / watched-folder / email_label_path)
- **CRITICAL follow-up:** watched-folder \`.eml\` files don't get \`email_*\` columns populated — only IMAP-ingested emails do. PR #414's preamble injection is dormant on filesystem-ingested email corpora. Tracked separately for its own spec.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Watch CI**

```bash
gh pr checks <PR_NUMBER> --watch
```

Expected: all checks green. If container-scan flakes (`No space left on device` is a known runner-side issue per PR #414's history), retry with `gh run rerun <run-id> --failed`.

- [ ] **Step 6: Merge on green**

```bash
gh pr merge <PR_NUMBER> --squash --delete-branch
```

- [ ] **Step 7: Update local main + clean up worktree**

```bash
cd /Users/alex/mcp-gateway
git checkout main 2>&1 | tail -2
git pull origin main 2>&1 | tail -3
git worktree remove .worktrees/metadata-polish --force 2>&1 | tail -3
git worktree list
```

Note: another session may be on a different branch in the main checkout — verify with `git branch --show-current` before any `git checkout` operation. If main isn't accessible because of a worktree conflict, just leave it; the merged PR is what matters.

---

## Self-Review

**Spec coverage:**
- §1 `_apply_jsonb_metadata_filter()` extraction → Tasks 1 + 2 ✓
- §2 `dc:identifier → isbn` alias → Task 3 ✓
- §3 Testing (unit test for helper + test for ISBN alias) → Tasks 1 + 3 ✓
- §4 Out of scope — not in plan (correct) ✓

**Placeholder scan:** No "TBD", "TODO", or vague steps. Every code block is complete; every command has expected output noted; the only intentional placeholder is `<PR_NUMBER>` in Tasks 4 Steps 5-6, which the engineer fills in from the URL `gh pr create` prints.

**Type consistency:**
- Helper signature in Task 1 (`_apply_jsonb_metadata_filter(metadata_filter: dict, doc_conditions: list) -> None`) matches the call sites in Tasks 1 + 2 (`_apply_jsonb_metadata_filter(metadata_filter, doc_conditions)`).
- Returns `None` (mutates in place) — confirmed in Task 1's unit test and consistent with how Tasks 1 + 2 use the return value (i.e., they don't).
- Companion helper `_apply_email_metadata_filter` returns the remaining dict — the asymmetry is documented in the docstring and called out in the spec.
