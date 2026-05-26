# PR-H: MCP Security Cleanup — Design

**Date:** 2026-05-25
**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** PR-E (`2026-05-24-mcp-verify-identifier-and-documents-by-date-design.md`), PR-F (`2026-05-24-document-metadata-extractors-design.md`), PR-G (`2026-05-25-pr-g-harness-audit-and-cross-judge-design.md`); all shipped.

## Goal

Fix two security findings flagged by fresh-eyes reviewers during PR-E and PR-G review:

1. **ILIKE wildcard injection** in `_find_candidates` (`src/harbor_clerk/mcp_lookup_tools.py:94`): the MCP-tool-facing helper builds an ILIKE pattern from a user-supplied identifier without escaping `%`, `_`, or `\`. An identifier like `"50% off"` acts as a wildcard.
2. **Filesystem-tree leak** in `kb_get_document` (`src/harbor_clerk/mcp_server.py:1240`): the response includes the absolute `doc.source_path`. For watched-folder docs this leaks the host filesystem tree to any MCP client — including cloud LLMs that forward responses to inference endpoints.

While fixing #1, extract a shared `escape_ilike()` helper so all 4 ILIKE call sites in the codebase share the same escape implementation, instead of growing a 4th inline copy.

## Why this PR exists

Both findings are real but bounded:

- **ILIKE injection:** the new tool from PR-E is model-facing — the model rarely passes literal `%` or `_` — so real-world impact is low. But every other ILIKE site in the codebase escapes (`mcp_server.py:1795` + `:2014`, `api/routes/documents.py:84`), so this is inconsistency that will bite the next person who extends `_find_candidates`. Easy fix; even easier with a shared helper.
- **Filesystem leak:** higher-impact. A model with MCP-tool access (Claude.ai, ChatGPT connectors, the OpenClaw CLI harness) calls `kb_get_document` and gets back `"source_path": "/Users/alex/Documents/Tax/2024/Returns.pdf"`. The path travels to the cloud provider's inference endpoint and may be logged. The `canonical_filename` field covers the user-facing "which file is this?" question without leaking the tree.

Both fixes are surgical; the helper extraction is a small DRY refactor that keeps the security posture consistent across the codebase.

## Non-goals

- **No `_escape_ilike()` audit beyond the 4 known sites.** A `grep` for `ILIKE` confirms only these 4 places build pattern strings. Future-proof against new sites by the helper's existence + a comment in `sql_escape.py`.
- **No CLI / API key gating on `kb_get_document`.** The fix is the field removal, not a permission tweak. The tool stays usable by every MCP client; only the leaked field disappears.
- **No `os.path.basename(source_path)` substitute.** The `canonical_filename` field already serves the "leaf filename" use case. Adding a redundant field would be noise.
- **No backward-compat shim for the source_path field.** No tests assert it; no UI consumer reads it (the macOS app uses the `revealInFinder` Swift bridge, not the MCP response).

## Architecture

Three new/modified files in `src/harbor_clerk/` + corresponding tests.

**New files:**
- `src/harbor_clerk/sql_escape.py` (~15 LOC) — `escape_ilike(s: str) -> str` returns `s` with `%`, `_`, `\` escaped as `\%`, `\_`, `\\` so the result is safe to embed in an ILIKE pattern.
- `tests/test_sql_escape.py` (~25 LOC) — unit tests for the helper.

**Modified files:**
- `src/harbor_clerk/mcp_lookup_tools.py` — call `escape_ilike(normalized)` in `_find_candidates` before building the `pattern = f"%{escaped}%"` (line 94). Apply to title + canonical_filename ILIKEs; tika.title equality branch unchanged.
- `src/harbor_clerk/mcp_server.py` — replace 2 inline `re.sub(...)` calls (lines 1795, 2014) with `escape_ilike(...)` calls; delete the `"source_path": doc.source_path,` line from `kb_get_document` response (line 1240).
- `src/harbor_clerk/api/routes/documents.py` — replace 1 inline `re.sub(...)` call (line 84) with `escape_ilike(...)`.
- `src/harbor_clerk/cli/help/get-document.txt` — delete the `source_path` row from the documented response shape (line 39).
- `tests/test_mcp_lookup_tools.py` — 2 new regression tests for the escape behaviour.
- `tests/test_mcp_metadata_filter.py` — 1 new regression test that `source_path` is absent from `kb_get_document` responses.

**Why a new top-level module instead of `search.py`:** the helper is SQL-escape utility, not search logic. Keeping the boundary clean means `search.py` keeps its hybrid-FTS-and-vector focus. `sql_escape.py` is single-purpose and named for discoverability — when the next person needs to escape an ILIKE value, the obvious search ("escape" / "ilike") lands them there.

## `sql_escape.py`

```python
# src/harbor_clerk/sql_escape.py
"""SQL escape helpers.

Centralized so all sites that build pattern strings use the same
implementation. Adding a new ILIKE pattern site? Import escape_ilike here.
"""

from __future__ import annotations

import re

# Characters with special meaning in PostgreSQL ILIKE patterns:
#   %  - matches any sequence of zero or more characters
#   _  - matches any single character
#   \  - escape character itself
# The default escape character is backslash; we escape each with a backslash
# so the resulting string is safe to embed inside `%...%` (or any pattern).
_ILIKE_METACHAR_RE = re.compile(r"([%_\\])")


def escape_ilike(value: str) -> str:
    """Return `value` with ILIKE metacharacters (%, _, \\) escaped.

    Use whenever building an ILIKE pattern from user-supplied input:

        escaped = escape_ilike(query)
        stmt = stmt.where(Document.title.op("ILIKE")(f"%{escaped}%"))

    Idempotent on inputs without metacharacters (returns the input unchanged).
    """
    return _ILIKE_METACHAR_RE.sub(r"\\\1", value)
```

## ILIKE escape fix

In `mcp_lookup_tools.py::_find_candidates`, change:

```python
    normalized = _normalize(identifier)
    if not normalized:
        return []

    # SQL-side pass: title / canonical_filename ILIKE, plus tika.title equality.
    pattern = f"%{normalized}%"
```

to:

```python
    normalized = _normalize(identifier)
    if not normalized:
        return []

    # SQL-side pass: title / canonical_filename ILIKE, plus tika.title equality.
    # Escape ILIKE metacharacters so an identifier like "50% off" or "___"
    # doesn't act as a wildcard. (tika.title branch uses equality — no wildcards
    # — so it doesn't need the escape.)
    pattern = f"%{escape_ilike(normalized)}%"
```

Add `from harbor_clerk.sql_escape import escape_ilike` to the imports.

**Behavior change:** identifiers containing `%`, `_`, or `\` now match literally instead of as wildcards. Examples:
- `"50% off"` — was: matches "50anything off"; now: matches "50% off" literally.
- `"___"` (three underscores) — was: matches any 3-character substring; now: matches "___" literally.
- `"\path"` — was: SQL error (lone backslash in some PG configurations); now: matches "\path" literally.

## Migrate the 3 existing call sites

Replace each `re.sub(r"([%_\\])", r"\\\1", X)` call:

- `src/harbor_clerk/mcp_server.py:1795`:
  ```python
  -    escaped_query = re.sub(r"([%_\\])", r"\\\1", query)
  +    escaped_query = escape_ilike(query)
  ```
- `src/harbor_clerk/mcp_server.py:2014`:
  ```python
  -    escaped_text = re.sub(r"([%_\\])", r"\\\1", entity_text)
  +    escaped_text = escape_ilike(entity_text)
  ```
- `src/harbor_clerk/api/routes/documents.py:84`:
  ```python
  -        escaped = re.sub(r"([%_\\])", r"\\\1", q)
  +        escaped = escape_ilike(q)
  ```

Each site gains the `from harbor_clerk.sql_escape import escape_ilike` import (or appends to an existing harbor_clerk import block). `import re` lines stay if other regex use remains in the file; remove if `re` becomes unused.

## `source_path` removal from `kb_get_document`

In `src/harbor_clerk/mcp_server.py`, delete one line:

```python
            "extracted_chars": doc.extracted_chars,
-           "source_path": doc.source_path,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
```

In `src/harbor_clerk/cli/help/get-document.txt`, delete one row:

```
    "extracted_chars": 1234,
-   "source_path":     "/data/watch/...",    // absolute path of the watched file
    "updated_at":      "ISO 8601 timestamp"
```

No other touches needed — no frontend reads it, no test asserts it.

## Testing

### `tests/test_sql_escape.py` (~6 tests)

```python
"""Unit tests for src/harbor_clerk/sql_escape.py."""

from harbor_clerk.sql_escape import escape_ilike


def test_escape_ilike_passes_through_safe_input():
    assert escape_ilike("hello world") == "hello world"


def test_escape_ilike_escapes_percent():
    assert escape_ilike("50% off") == "50\\% off"


def test_escape_ilike_escapes_underscore():
    assert escape_ilike("contract_2024") == "contract\\_2024"


def test_escape_ilike_escapes_backslash():
    assert escape_ilike(r"path\to\file") == r"path\\to\\file"


def test_escape_ilike_handles_multiple_metacharacters():
    assert escape_ilike("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_escape_ilike_empty_string():
    assert escape_ilike("") == ""
```

### `tests/test_mcp_lookup_tools.py` (2 new regression tests, appended)

```python
@pytest.mark.asyncio
async def test_find_candidates_escapes_percent_wildcard(db_session):
    """An identifier with literal % must not act as a wildcard."""
    target = await _seed_doc(db_session, title="50% off coupon")
    distractor = await _seed_doc(db_session, title="50 percent reduction")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "50% off")
    doc_ids = {c.doc_id for c in candidates}
    assert target.doc_id in doc_ids
    assert distractor.doc_id not in doc_ids
    # Without the escape, the pattern would be "%50% off%" → matches any title
    # containing "50<anything> off".


@pytest.mark.asyncio
async def test_find_candidates_escapes_underscore_wildcard(db_session):
    """An identifier with literal _ must not act as SQL's any-char wildcard."""
    target = await _seed_doc(db_session, title="contract_2024")
    distractor = await _seed_doc(db_session, title="contracta2024")
    await db_session.flush()

    candidates = await _find_candidates(db_session, "contract_2024")
    doc_ids = {c.doc_id for c in candidates}
    assert target.doc_id in doc_ids
    assert distractor.doc_id not in doc_ids
    # Without the escape, "_" matches any single character, so distractor
    # "contracta2024" would have matched.
```

### `tests/test_mcp_metadata_filter.py` (1 new regression test, appended)

```python
async def test_kb_get_document_does_not_leak_source_path(
    client, admin_user, db_session, mock_session_factory
):
    """source_path is host-filesystem-private; never include in MCP responses."""
    target = await _seed_doc(db_session, title="some-doc", metadata={})
    target.source_path = "/Users/alex/private/host-only/path.pdf"
    await db_session.flush()

    with _principal_in_context(admin_user):
        raw = await kb_get_document(doc_id=str(target.doc_id))

    parsed = json.loads(raw)
    assert "source_path" not in parsed
    # canonical_filename should still convey the leaf filename
    assert "title" in parsed
```

### Regression suite

After all changes, `uv run pytest tests/ --ignore=tests/test_macos_smoke.py` should pass with the same baseline (≥1137) plus 3 new tests; the migration of the 3 existing escape call sites should be behavior-preserving (existing tests for those code paths still pass).

## Decisions (closed during brainstorming)

- **`source_path` fix: omit from response entirely** rather than `os.path.basename(...)` or `allow_source_download`-gated. Simplest; smallest surface; no operator-facing knob added; `canonical_filename` already covers the leaf-filename use case.
- **Pull the shared `escape_ilike()` helper into PR-H scope** rather than landing the new fix as a 4th inline copy. 4 call sites is enough to justify the refactor; the helper module is ~15 LOC + 6 unit tests; the migration of the 3 existing sites is mechanical.
- **Helper lives at `src/harbor_clerk/sql_escape.py`** (top-level, narrow name) rather than under `search.py` or a new `db/` sub-package. Discoverable by future maintainers ("escape", "ilike") without creating a new package.
- **No CLI / API key gating on `kb_get_document`.** The fix is the field removal; the tool stays usable.
- **No backward-compat shim for `source_path`.** No tests, no frontend consumers; removal is safe.

## Out of scope / follow-ups

- **`source_path` exposure via the `harbor-clerk get-document` CLI's `--include-source-path` flag** — could add a CLI-only opt-in for operators running on the same host. Defer: the existing `revealInFinder` Swift bridge covers the macOS case; Docker users have host-level filesystem access independent of this field.
- **Audit other MCP tool responses for similar filesystem-leak patterns.** Spot-checked during brainstorm; nothing else surfaces `source_path` or similar. If a future tool needs to surface host-path data, design it through the `allow_source_download` gate pattern from `/api/docs/{id}/download`.

## Open questions / risks

- **`%`/`_` in real-world identifiers is rare.** The two regression tests are the canary; production exposure is low. The fix is still worth shipping for consistency with the rest of the codebase.
- **Removing `source_path` from `kb_get_document` is a JSON response-shape change.** No tests/UI assert it, but a hypothetical third-party MCP client that scraped it would see `KeyError`. Acceptable trade-off — the field documented as "absolute path of the watched file" is not safe to depend on for any user-facing logic anyway.
- **The `escape_ilike()` helper is trivial — refactor cost is the 4 call-site touches + the test file.** Bounded; doesn't justify deferring.
