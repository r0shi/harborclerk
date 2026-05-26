# PR-H: MCP Security Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two MCP-side security findings (ILIKE wildcard injection in `_find_candidates`, absolute `source_path` leak in `kb_get_document`) and extract a shared `escape_ilike()` helper consolidating the 4 ILIKE-pattern call sites in the codebase.

**Architecture:** Single helper module `src/harbor_clerk/sql_escape.py` exposes `escape_ilike(s: str) -> str`. Three existing call sites (`mcp_server.py:1795`, `mcp_server.py:2014`, `api/routes/documents.py:84`) get migrated to use it. One new call site (`mcp_lookup_tools.py:_find_candidates`) gets the escape applied for the first time. Separately, `kb_get_document` drops the `source_path` field from its response, with the CLI help text updated to match. No backward-compat shim.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-05-25-pr-h-mcp-security-cleanup-design.md`

---

## File Structure

**New files:**
- `src/harbor_clerk/sql_escape.py` — `escape_ilike(value: str) -> str` helper (~15 LOC).
- `tests/test_sql_escape.py` — 6 unit tests for the helper.

**Modified files:**
- `src/harbor_clerk/mcp_lookup_tools.py` — apply `escape_ilike()` to the ILIKE pattern in `_find_candidates`.
- `src/harbor_clerk/mcp_server.py` — migrate 2 inline `re.sub(...)` calls to `escape_ilike(...)`; drop unused `import re`; delete the `source_path` line from `kb_get_document`.
- `src/harbor_clerk/api/routes/documents.py` — migrate 1 inline `re.sub(...)` call to `escape_ilike(...)`; drop unused `import re`.
- `src/harbor_clerk/cli/help/get-document.txt` — delete the `source_path` row from the documented response shape.
- `tests/test_mcp_lookup_tools.py` — 2 new regression tests for `%` / `_` wildcard escape.
- `tests/test_mcp_metadata_filter.py` — 1 new regression test that `source_path` is absent.

---

## Task 1: `sql_escape.py` helper + unit tests

**Files:**
- Create: `src/harbor_clerk/sql_escape.py`
- Create: `tests/test_sql_escape.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sql_escape.py
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

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sql_escape.py -v`
Expected: 6 FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.sql_escape'`

- [ ] **Step 3: Implement `escape_ilike`**

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sql_escape.py -v`
Expected: 6 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check src/harbor_clerk/sql_escape.py tests/test_sql_escape.py`
Expected: "All checks passed!"

Run: `uv run ruff format src/harbor_clerk/sql_escape.py tests/test_sql_escape.py`

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/sql_escape.py tests/test_sql_escape.py
git commit -m "feat(sql-escape): centralized escape_ilike() helper"
```

---

## Task 2: Migrate 3 existing ILIKE call sites

**Goal:** Replace inline `re.sub(r"([%_\\])", r"\\\1", ...)` at 3 call sites with `escape_ilike(...)`. Drop unused `import re` from `mcp_server.py` and `api/routes/documents.py` (each had `re` imported solely for the ILIKE escape).

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py:6` (drop `import re`), `:1795` (migrate), `:2014` (migrate)
- Modify: `src/harbor_clerk/api/routes/documents.py:5` (drop `import re`), `:84` (migrate)

- [ ] **Step 1: Quick pre-check for unrelated `re.` uses**

Run from worktree root:
```bash
grep -cE "\bre\." src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/documents.py
```

Expected: `mcp_server.py:2` and `documents.py:1`. (Both counts equal the inline escape calls we're replacing — after migration `re` is unused in both files.)

- [ ] **Step 2: Migrate `mcp_server.py:1795`**

Find the existing code:

```python
    # Escape ILIKE metacharacters in query
    escaped_query = re.sub(r"([%_\\])", r"\\\1", query)
```

Replace with:

```python
    # Escape ILIKE metacharacters in query (helper at src/harbor_clerk/sql_escape.py)
    escaped_query = escape_ilike(query)
```

- [ ] **Step 3: Migrate `mcp_server.py:2014`**

Find:

```python
    escaped_text = re.sub(r"([%_\\])", r"\\\1", entity_text)
```

Replace with:

```python
    escaped_text = escape_ilike(entity_text)
```

- [ ] **Step 4: Replace `import re` in `mcp_server.py` with the new import**

Find at the top of `src/harbor_clerk/mcp_server.py` (around line 6):

```python
import re
```

Remove it. Then add to the harbor_clerk imports block (alphabetized; insert wherever it fits the existing convention):

```python
from harbor_clerk.sql_escape import escape_ilike
```

If the file already groups `from harbor_clerk.X import Y` imports together, slot the new line in alphabetical order; otherwise append after the last `from harbor_clerk.` import.

- [ ] **Step 5: Migrate `api/routes/documents.py:84`**

Find:

```python
    if q:
        escaped = re.sub(r"([%_\\])", r"\\\1", q)
        pattern = f"%{escaped}%"
```

Replace with:

```python
    if q:
        escaped = escape_ilike(q)
        pattern = f"%{escaped}%"
```

- [ ] **Step 6: Replace `import re` in `api/routes/documents.py` with the new import**

Find at the top of `src/harbor_clerk/api/routes/documents.py` (line 5):

```python
import re
```

Remove it. Add to the harbor_clerk imports block:

```python
from harbor_clerk.sql_escape import escape_ilike
```

- [ ] **Step 7: Verify no stray `re.` usage remains in the modified files**

Run:
```bash
grep -nE "\bre\." src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/documents.py
```

Expected: no output (every `re.` use was the ILIKE escape we just replaced).

- [ ] **Step 8: Run the existing test suites for the migrated paths**

Run:
```bash
uv run pytest tests/test_api_documents.py tests/test_mcp_tools.py -q
```

Expected: All existing tests pass — migration is behavior-preserving. If any test fails with an `ImportError` / `NameError`, you missed a `re.` reference; go back to Step 7.

- [ ] **Step 9: Ruff + format**

Run:
```bash
uv run ruff check src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/documents.py
```
Expected: "All checks passed!" — if it complains about unused `re`, you missed Step 4 or 6.

Run:
```bash
uv run ruff format src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/documents.py
```

- [ ] **Step 10: Commit**

```bash
git add src/harbor_clerk/mcp_server.py src/harbor_clerk/api/routes/documents.py
git commit -m "refactor(sql-escape): migrate 3 existing ILIKE escape sites to escape_ilike()"
```

---

## Task 3: Apply escape to `_find_candidates` + regression tests

**Goal:** Fix the PR-E ILIKE wildcard injection in `_find_candidates`. The `tika.title` equality branch is unchanged (no wildcards). Two regression tests prove `%` and `_` are no longer wildcards.

**Files:**
- Modify: `src/harbor_clerk/mcp_lookup_tools.py:94` (apply escape)
- Modify: `tests/test_mcp_lookup_tools.py` (append 2 regression tests)

- [ ] **Step 1: Write the failing regression tests** (append to existing file)

```python
# Append to tests/test_mcp_lookup_tools.py


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

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k "escapes"`
Expected: 2 FAIL — without the escape, `distractor` is also returned, breaking the `assert distractor.doc_id not in doc_ids`.

- [ ] **Step 3: Apply the escape in `_find_candidates`**

Open `src/harbor_clerk/mcp_lookup_tools.py`. Add to the harbor_clerk imports block (after the existing `from harbor_clerk.models import Document` line, or wherever the convention fits):

```python
from harbor_clerk.sql_escape import escape_ilike
```

Find the existing code (around line 87–94):

```python
    normalized = _normalize(identifier)
    if not normalized:
        return []

    # SQL-side pass: title / canonical_filename ILIKE, plus tika.title equality.
    # The metadata-key match needs Python-side traversal because the leaf
    # paths are variable.
    pattern = f"%{normalized}%"
```

Replace with:

```python
    normalized = _normalize(identifier)
    if not normalized:
        return []

    # SQL-side pass: title / canonical_filename ILIKE, plus tika.title equality.
    # The metadata-key match needs Python-side traversal because the leaf
    # paths are variable. Escape ILIKE metacharacters so an identifier like
    # "50% off" or "___" doesn't act as a wildcard. (tika.title branch uses
    # equality without wildcards — no escape needed there.)
    pattern = f"%{escape_ilike(normalized)}%"
```

Note: the `tika.title` branch a few lines below (`Document.doc_metadata["tika"]["title"].astext.op("ILIKE")(normalized)`) stays unchanged — it's case-insensitive equality without wildcards, so escaping there would break the intended match.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_lookup_tools.py -v -k "escapes"`
Expected: 2 PASS — the 2 new regression tests now pass.

Run the full file to confirm no other regressions:
```bash
uv run pytest tests/test_mcp_lookup_tools.py -q
```
Expected: All tests pass (existing PR-E + PR-G tests unaffected).

- [ ] **Step 5: Ruff + format**

Run:
```bash
uv run ruff check src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
uv run ruff format src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
```

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mcp_lookup_tools.py tests/test_mcp_lookup_tools.py
git commit -m "fix(mcp-lookup): escape ILIKE metacharacters in _find_candidates pattern"
```

---

## Task 4: Remove `source_path` from `kb_get_document` + regression test

**Goal:** Stop leaking the absolute host-filesystem path through `kb_get_document`. Drop the field from the response dict, drop the matching row from the CLI help text, add a regression test that the field is absent.

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py:1240` (delete one line)
- Modify: `src/harbor_clerk/cli/help/get-document.txt:39` (delete one line)
- Modify: `tests/test_mcp_metadata_filter.py` (append 1 regression test)

- [ ] **Step 1: Write the failing regression test** (append to existing file)

```python
# Append to tests/test_mcp_metadata_filter.py

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
    # canonical_filename / title still convey the leaf-filename
    assert "title" in parsed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_metadata_filter.py::test_kb_get_document_does_not_leak_source_path -v`
Expected: FAIL — `source_path` IS in `parsed` (the bug being fixed).

- [ ] **Step 3: Delete `source_path` from `kb_get_document` response**

Open `src/harbor_clerk/mcp_server.py`. Find the response dict around line 1230–1246:

```python
    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "status": doc.status,
            "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
            "summary": doc.summary,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "extracted_chars": doc.extracted_chars,
            "source_path": doc.source_path,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            "metadata": doc.doc_metadata,
            "jobs": jobs,
        },
        indent=2,
    )
```

Delete the one line:

```python
            "source_path": doc.source_path,
```

The resulting block should be:

```python
    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "status": doc.status,
            "pipeline_status": doc.pipeline_status.value if doc.pipeline_status else None,
            "summary": doc.summary,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "extracted_chars": doc.extracted_chars,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            "metadata": doc.doc_metadata,
            "jobs": jobs,
        },
        indent=2,
    )
```

- [ ] **Step 4: Delete `source_path` row from the CLI help text**

Open `src/harbor_clerk/cli/help/get-document.txt`. Find around line 39:

```
    "source_path":     "/data/watch/...",    // absolute path of the watched file
```

Delete that one line. (Surrounding lines documenting other fields stay intact.)

- [ ] **Step 5: Run regression test to verify it passes**

Run: `uv run pytest tests/test_mcp_metadata_filter.py::test_kb_get_document_does_not_leak_source_path -v`
Expected: PASS.

Run the existing kb_get_document tests to confirm nothing else broke:
```bash
uv run pytest tests/test_mcp_metadata_filter.py tests/test_mcp_tools.py -v -k "get_document or kb_get_document"
```
Expected: All pass.

- [ ] **Step 6: Ruff + format**

Run:
```bash
uv run ruff check src/harbor_clerk/mcp_server.py tests/test_mcp_metadata_filter.py
uv run ruff format src/harbor_clerk/mcp_server.py tests/test_mcp_metadata_filter.py
```

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/mcp_server.py src/harbor_clerk/cli/help/get-document.txt tests/test_mcp_metadata_filter.py
git commit -m "fix(mcp): remove absolute source_path from kb_get_document response"
```

---

## Task 5: Full-suite regression + ruff/format + fresh-eyes review + open PR

**Files:** No code changes initially; review may surface fixes.

- [ ] **Step 1: Confirm Postgres is up on the test port**

Run:
```bash
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep 5433
```

Expected: at least one `LISTEN` line on port 5433. If empty, restart the bundled PG (the worktree's conftest auto-detects 5433 first):

```bash
PGCTL=/Users/alex/mcp-gateway/macos/build/postgres/bin/pg_ctl
DATA="/Users/alex/Library/Application Support/Harbor Clerk/postgres-data"
LOGFILE="/Users/alex/Library/Application Support/Harbor Clerk/logs/postgres.log"
rm -f "$DATA/postmaster.pid"
"$PGCTL" -D "$DATA" -l "$LOGFILE" -o "-p 5433 -k /tmp" start
```

- [ ] **Step 2: Full HC test suite**

Run:
```bash
uv run pytest tests/ --ignore=tests/test_macos_smoke.py -q
```
Expected: ≥1146 passed, 9 skipped (1137 baseline + 6 from Task 1 + 2 from Task 3 + 1 from Task 4 = 9 new). 0 failures.

- [ ] **Step 3: Ruff check + format check across the repo**

Run:
```bash
uv run ruff check . 2>&1 | tail -3
uv run ruff format --check . 2>&1 | tail -3
```
Expected: "All checks passed!" and "X files already formatted".

- [ ] **Step 4: Push the branch**

```bash
git push -u origin fix/pr-h-mcp-security
```

- [ ] **Step 5: Dispatch the fresh-eyes reviewer**

Use the `Agent` tool with:
- `subagent_type`: `feature-dev:code-reviewer`
- Minimal prompt: branch name + PR-H summary (ILIKE escape consolidation + source_path leak removal); link the spec doc; tell the reviewer to report ≥80-confidence findings only. No focus areas, no carve-outs.

Address any ≥80-confidence findings inline; re-run tests + ruff after each fix; commit each fix with `fix(pr-h):` prefix.

- [ ] **Step 6: Open the PR**

```bash
gh pr create --title "fix(mcp): security cleanup — ILIKE escape consolidation + source_path leak (PR-H)" \
  --body-file /tmp/pr-h-body.md
```

PR body should include:
- **Summary** (the two fixes + the consolidation refactor)
- **Why** (fresh-eyes findings from PR-E + PR-F + PR-G reviews — cite each)
- **What's in it** (file list grouped by purpose)
- **Test plan** (suite count delta, ruff result, fresh-eyes-review summary)
- **Backward-compat note** (`source_path` field removal — no tests, no UI consumers; documented as MCP-debug-only field)
- **Spec + plan links**

- [ ] **Step 7: Enable auto-merge**

```bash
gh pr merge <PR-number> --auto --squash
```

CI takes ~5–8 minutes. The harness won't notify on merge; the user sees it land.

---

## Self-Review Notes

Spec coverage:

- **§ Goal + Why** — covered by Tasks 1–4 (helper, migration, ILIKE fix, source_path removal). ✓
- **§ Non-goals** — encoded as omissions, not tasks (no audit beyond the 4 sites; no gating; no basename substitute; no backward-compat shim). ✓
- **§ Architecture** — Tasks 1–4 correspond to the new/modified files in the spec's File Structure table. ✓
- **§ `sql_escape.py`** — Task 1 implementation + tests match the spec verbatim. ✓
- **§ ILIKE escape fix** — Task 3 (in `_find_candidates`) with the spec's exact `tika.title` non-modification note carried through. ✓
- **§ Migrate the 3 existing call sites** — Task 2 covers all three; the `import re` removal is explicit in the spec ("remove if `re` becomes unused"). ✓
- **§ `source_path` removal from `kb_get_document`** — Task 4. ✓
- **§ Testing** — `tests/test_sql_escape.py` (Task 1), 2 ILIKE regression tests (Task 3), 1 source_path regression test (Task 4). ✓
- **§ Decisions** — all 5 decisions carried through (helper extraction in scope; helper module name + location; `source_path` omit-not-basename; no CLI/API gating; no backward-compat shim). ✓
- **§ Out of scope** — items left absent from tasks (the CLI `--include-source-path` flag and the broader MCP-tool audit). ✓
- **§ Open questions/risks** — the spec already documents these inline; no new tasks needed. ✓

**Type / signature consistency:** `escape_ilike(value: str) -> str` is defined in Task 1 and called in Tasks 2 + 3. All call sites pass a `str` (queries, entity_text, q, normalized). No drift.

**Placeholder scan:** no "TBD" / "TODO" / "fill in" / "similar to Task N" in the plan. Every step that touches code shows the code or the exact edit.

**Bite-size check:** every step is 1–5 minutes (write a function, run a command, edit one line block, run a test). No mega-steps that bundle multiple decisions.
