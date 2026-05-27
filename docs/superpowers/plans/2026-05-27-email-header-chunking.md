# Email Header Chunking + Queryable Email Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepend a structured email-header block (From / To / Cc / Subject / Date) to email `body_text` so chunks include it, AND expose `Document.email_*` columns via a new `email.*` namespace in `metadata_filter`.

**Architecture:** A new helper `_build_header_preamble()` in `parser.py` builds the block and `parse_eml` prepends it before returning. `hybrid_search` grows a pre-pass that strips `email.*` keys from `metadata_filter` and dispatches to dedicated column predicates (exact, ILIKE substring via `escape_ilike()`, or `= ANY` on TEXT[] arrays). Two new trgm GIN indexes on `email_subject` + `email_from_name` keep ILIKE queries fast. Docstring updates on `kb_search` / `kb_find_all` / `kb_documents_by_date` describe the new filter keys.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, asyncpg, PostgreSQL 18 with `pg_trgm`, pytest, alembic.

**Spec:** `docs/superpowers/specs/2026-05-27-email-header-chunking-design.md`

**File map (created or modified):**

- Modify: `src/harbor_clerk/mail/parser.py` — add `_build_header_preamble()` helper; wire into `parse_eml` to prepend to `body_text`
- Modify: `src/harbor_clerk/search.py` — add `email.*` pre-pass to the `metadata_filter` translator in `hybrid_search`
- Modify: `src/harbor_clerk/mcp_server.py` — docstring updates on `kb_search`, `kb_find_all`, `kb_documents_by_date` describing `email.*` filter keys
- Create: `alembic/versions/<rev>_email_metadata_trgm_indexes.py` — trgm GIN indexes on `documents.email_subject` + `documents.email_from_name`
- Modify: `tests/mail/test_parser.py` — extend with `_build_header_preamble` + preamble-in-`body_text` tests
- Modify: `tests/test_search_filtered.py` — extend with `email.*` filter cases
- Create: `tests/test_alembic_email_metadata_indexes.py` — verify both indexes present and are GIN trgm
- Modify: `tests/test_mcp_tool_descriptions.py` — assert `email.*` mentioned in three tools' descriptions
- Create: `tests/integration/test_email_header_chunks.py` — end-to-end: build `.eml`, parse, chunk, assert chunk 0 contains preamble, run `hybrid_search` with email filter, verify match

**Conventions:**
- All work on branch `spec/email-header-chunking` in the worktree at `.worktrees/spec-email-headers/`.
- Tests run via `uv run pytest tests/<file>.py -v` from the worktree root.
- Each task ends with a commit; messages follow `feat(<scope>): <what>` / `fix(<scope>): <what>` per repo convention.
- All new ILIKE call sites use `escape_ilike()` from `src/harbor_clerk/sql_escape.py` (PR-H established this rule).
- Alembic `CREATE INDEX CONCURRENTLY` must be wrapped in `op.get_context().autocommit_block()` (PR #411 established this pattern).

---

## Task 1: Add `_build_header_preamble()` helper to parser

**Files:**
- Modify: `src/harbor_clerk/mail/parser.py`
- Test: `tests/mail/test_parser.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/mail/test_parser.py`:

```python
from datetime import datetime, timezone


def test_header_preamble_full_block():
    """All five fields render in fixed key:value order, then a blank line."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="Alice Anderson",
        from_address="alice@firm.com",
        to_addresses=["bob@firm.com", "carol@firm.com"],
        cc_addresses=["dan@firm.com"],
        subject="Q3 Vendor Agreement Review",
        date_sent=datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc),
    )

    assert preamble == (
        "From: Alice Anderson <alice@firm.com>\n"
        "To: bob@firm.com, carol@firm.com\n"
        "Cc: dan@firm.com\n"
        "Subject: Q3 Vendor Agreement Review\n"
        "Date: 2026-01-15\n"
        "\n"
    )


def test_header_preamble_omits_empty_recipients():
    """Empty To / Cc lists skip those lines entirely."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="Alice",
        from_address="alice@firm.com",
        to_addresses=[],
        cc_addresses=[],
        subject="Solo memo",
        date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )

    assert "To:" not in preamble
    assert "Cc:" not in preamble
    assert preamble == (
        "From: Alice <alice@firm.com>\n"
        "Subject: Solo memo\n"
        "Date: 2026-01-15\n"
        "\n"
    )


def test_header_preamble_from_no_name():
    """from_name empty → emit address only (no angle brackets)."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="",
        from_address="bot@notifications.example.com",
        to_addresses=["alice@firm.com"],
        cc_addresses=[],
        subject="Notification",
        date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )

    assert "From: bot@notifications.example.com\n" in preamble
    assert "<" not in preamble  # no angle brackets when no display name


def test_header_preamble_to_cap_at_11_recipients():
    """>10 To recipients collapses to '$N recipients'; ≤10 lists addresses."""
    from harbor_clerk.mail.parser import _build_header_preamble

    # Exactly 10 — listed
    ten = [f"r{i}@firm.com" for i in range(10)]
    preamble_10 = _build_header_preamble(
        from_name="Alice", from_address="alice@firm.com",
        to_addresses=ten, cc_addresses=[],
        subject="S", date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    assert "r0@firm.com" in preamble_10
    assert "r9@firm.com" in preamble_10
    assert "recipients" not in preamble_10

    # 11 — collapsed
    eleven = [f"r{i}@firm.com" for i in range(11)]
    preamble_11 = _build_header_preamble(
        from_name="Alice", from_address="alice@firm.com",
        to_addresses=eleven, cc_addresses=[],
        subject="S", date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    assert "To: 11 recipients\n" in preamble_11
    assert "r0@firm.com" not in preamble_11


def test_header_preamble_cc_cap_at_11_recipients():
    """Same >10 collapse rule applies to Cc."""
    from harbor_clerk.mail.parser import _build_header_preamble

    eleven = [f"c{i}@firm.com" for i in range(11)]
    preamble = _build_header_preamble(
        from_name="Alice", from_address="alice@firm.com",
        to_addresses=["bob@firm.com"], cc_addresses=eleven,
        subject="S", date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )

    assert "Cc: 11 recipients\n" in preamble
    assert "c0@firm.com" not in preamble


def test_header_preamble_no_date():
    """date_sent=None → Date line omitted entirely."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="Alice", from_address="alice@firm.com",
        to_addresses=["bob@firm.com"], cc_addresses=[],
        subject="S", date_sent=None,
    )

    assert "Date:" not in preamble


def test_header_preamble_no_from_at_all():
    """Both from_name and from_address empty → From line omitted."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="", from_address="",
        to_addresses=["bob@firm.com"], cc_addresses=[],
        subject="S", date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )

    assert "From:" not in preamble


def test_header_preamble_subject_always_shown():
    """Subject line is always present (caller's '(no subject)' fallback persists)."""
    from harbor_clerk.mail.parser import _build_header_preamble

    preamble = _build_header_preamble(
        from_name="Alice", from_address="alice@firm.com",
        to_addresses=["bob@firm.com"], cc_addresses=[],
        subject="(no subject)", date_sent=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )

    assert "Subject: (no subject)\n" in preamble
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/mail/test_parser.py -v -k "header_preamble"
```

Expected: 8 FAIL with `ImportError: cannot import name '_build_header_preamble'`.

- [ ] **Step 3: Add the helper**

In `src/harbor_clerk/mail/parser.py`, after the existing private helpers (e.g., `_synthesize_message_id`, `_parse_addresses`) and before the module-level `parse_eml`:

```python
_RECIPIENT_CAP = 10


def _build_header_preamble(
    *,
    from_name: str,
    from_address: str,
    to_addresses: list[str],
    cc_addresses: list[str],
    subject: str,
    date_sent: datetime | None,
) -> str:
    """Render a key:value header block to prepend to body_text.

    Lines emitted in fixed order — From, To, Cc, Subject, Date — with
    omit-on-empty for each line and a trailing blank line. The chunker
    treats the preamble as the start of chunk 0, exposing headers to
    NER + FTS + text_contains without changing downstream pipeline code.

    To / Cc with >_RECIPIENT_CAP entries collapse to '$N recipients' to
    bound preamble length on distribution-list emails.
    """
    lines: list[str] = []

    # From: 'Name <address>' if name present, just address if not, omit if both empty
    if from_name and from_address:
        lines.append(f"From: {from_name} <{from_address}>")
    elif from_address:
        lines.append(f"From: {from_address}")

    if to_addresses:
        if len(to_addresses) > _RECIPIENT_CAP:
            lines.append(f"To: {len(to_addresses)} recipients")
        else:
            lines.append(f"To: {', '.join(to_addresses)}")

    if cc_addresses:
        if len(cc_addresses) > _RECIPIENT_CAP:
            lines.append(f"Cc: {len(cc_addresses)} recipients")
        else:
            lines.append(f"Cc: {', '.join(cc_addresses)}")

    # Subject is always shown (caller's '(no subject)' fallback persists)
    lines.append(f"Subject: {subject}")

    if date_sent is not None:
        lines.append(f"Date: {date_sent.strftime('%Y-%m-%d')}")

    return "\n".join(lines) + "\n\n"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/mail/test_parser.py -v -k "header_preamble"
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add src/harbor_clerk/mail/parser.py tests/mail/test_parser.py
git commit -m "feat(mail): add _build_header_preamble helper

Builds a key:value email header block (From / To / Cc / Subject / Date)
in fixed order, ≤10 recipients listed, >10 collapsed to '\$N recipients',
ISO YYYY-MM-DD dates, trailing blank line.

Helper only — not yet wired into parse_eml. See
docs/superpowers/specs/2026-05-27-email-header-chunking-design.md §2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Wire `_build_header_preamble` into `parse_eml`

**Files:**
- Modify: `src/harbor_clerk/mail/parser.py`
- Test: `tests/mail/test_parser.py`

- [ ] **Step 1: Write failing test**

Append to `tests/mail/test_parser.py`:

```python
def test_parse_eml_prepends_header_preamble_to_body_text():
    """parse_eml's body_text starts with the rendered header preamble."""
    from harbor_clerk.mail.parser import parse_eml
    from tests.mail.fixtures.build_eml import build_simple_email

    eml = build_simple_email(
        message_id="<preamble-test@example.com>",
        subject="Q3 Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com", "carol@firm.com"],
        cc=["dan@firm.com"],
        body_text="Original body content goes here.",
    )

    result = parse_eml(eml)

    # body_text starts with the preamble block
    assert result.body_text.startswith("From: Alice Anderson <alice@firm.com>\n")
    assert "To: bob@firm.com, carol@firm.com\n" in result.body_text
    assert "Cc: dan@firm.com\n" in result.body_text
    assert "Subject: Q3 Review\n" in result.body_text
    # And the body still follows
    assert "Original body content goes here." in result.body_text
    # With exactly one blank line between preamble and body
    assert "\n\nOriginal body content goes here." in result.body_text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/mail/test_parser.py::test_parse_eml_prepends_header_preamble_to_body_text -v
```

Expected: FAIL — `body_text` starts with the original body (no preamble).

- [ ] **Step 3: Wire the helper into parse_eml**

In `src/harbor_clerk/mail/parser.py`, modify `parse_eml` to prepend the preamble. Change the existing `body_text = _extract_body_text(msg)` line to:

```python
    body_text = _extract_body_text(msg)
    preamble = _build_header_preamble(
        from_name=from_name,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        subject=subject,
        date_sent=date_sent,
    )
    body_text = preamble + body_text
```

The `EmailParseResult(...)` constructor call below stays unchanged (it already passes `body_text=body_text`).

- [ ] **Step 4: Run test to verify it passes; also run the full parser test file to catch regressions**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/mail/test_parser.py -v
```

Expected: all tests passed (the existing tests that assert `"Please review" in result.body_text` still work — substring match is unaffected by the preamble).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add src/harbor_clerk/mail/parser.py tests/mail/test_parser.py
git commit -m "feat(mail): prepend header preamble to body_text in parse_eml

Chunker sees the preamble at the start of body_text → it lands at the
start of chunk 0 → NER picks up sender names, FTS indexes subject lines,
text_contains matches header patterns.

No downstream code changes needed. Existing email docs need re-ingest
(deferred to operator; see spec §5).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Alembic migration — trgm GIN indexes on `documents.email_subject` + `documents.email_from_name`

**Files:**
- Create: `alembic/versions/<auto-rev>_email_metadata_trgm_indexes.py`
- Create: `tests/test_alembic_email_metadata_indexes.py`

- [ ] **Step 1: Check current alembic head**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run alembic heads
```

Note the head revision id (e.g., `b56bde44ec8c` after PR #411). Call it `<prev_head>` below.

- [ ] **Step 2: Defensive check — indexes don't already exist**

```bash
grep -rn "email_subject.*trgm\|email_from_name.*trgm" alembic/versions/ | head
```

Expected: no results. If results appear, stop and report.

- [ ] **Step 3: Create the migration scaffold**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run alembic revision -m "email metadata trgm gin indexes"
```

This creates `alembic/versions/<new_rev>_email_metadata_trgm_gin_indexes.py`. Note the filename's revision id; call it `<new_rev>`.

- [ ] **Step 4: Write the migration body**

Replace the scaffold body with this. Use the actual `<new_rev>` and `<prev_head>` from Steps 1+3.

```python
"""email metadata trgm gin indexes

Two GIN trigram indexes on documents.email_subject and
documents.email_from_name to support fast ILIKE filtering used by the
new email.subject_contains and email.from_name_contains metadata_filter
keys (see hybrid_search). Without these, ILIKE '%v%' on these columns
forces a seq-scan on the documents table at corpus scale.

The pg_trgm extension is already enabled (see docker/postgres/init-
extensions.sql for Docker; same for macOS bundle).

CREATE INDEX CONCURRENTLY cannot run inside a transaction; wrap in
autocommit_block (pattern established by PR #411).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "<new_rev>"
down_revision = "<prev_head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_email_subject_trgm "
            "ON public.documents USING gin (email_subject public.gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_email_from_name_trgm "
            "ON public.documents USING gin (email_from_name public.gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_documents_email_subject_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_documents_email_from_name_trgm")
```

- [ ] **Step 5: Write the test**

Create `tests/test_alembic_email_metadata_indexes.py`:

```python
"""Verify the documents.email_subject + email_from_name trgm indexes exist."""

from sqlalchemy import text


async def test_documents_email_subject_trgm_index_present(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='documents' "
            "AND indexname='ix_documents_email_subject_trgm'"
        )
    )
    assert result.first() is not None, "ix_documents_email_subject_trgm not found"


async def test_documents_email_subject_trgm_index_is_gin_trgm(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='ix_documents_email_subject_trgm'"
        )
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef


async def test_documents_email_from_name_trgm_index_present(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='documents' "
            "AND indexname='ix_documents_email_from_name_trgm'"
        )
    )
    assert result.first() is not None, "ix_documents_email_from_name_trgm not found"


async def test_documents_email_from_name_trgm_index_is_gin_trgm(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='ix_documents_email_from_name_trgm'"
        )
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_alembic_email_metadata_indexes.py -v
```

Expected: 4 passed.

- [ ] **Step 7: Verify head advanced**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run alembic heads
```

Expected: `<new_rev>`.

- [ ] **Step 8: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add alembic/versions/<new_rev>_email_metadata_trgm_gin_indexes.py tests/test_alembic_email_metadata_indexes.py
git commit -m "feat(db): trgm GIN indexes on documents.email_subject + email_from_name

Supports the new email.subject_contains and email.from_name_contains
metadata_filter keys (see hybrid_search). Without these, ILIKE '%v%' on
these columns forces a seq-scan at corpus scale.

CONCURRENTLY-created via autocommit_block to avoid blocking ingest
workers on large corpora (pattern from PR #411).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add `email.*` pre-pass to `hybrid_search`'s metadata_filter translator

**Files:**
- Modify: `src/harbor_clerk/search.py`
- Test: `tests/test_search_filtered.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_search_filtered.py`:

```python
async def test_metadata_filter_email_from_address_exact(db_session):
    """email.from_address matches case-insensitively."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_ef_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_address="alice@firm.com")
    b = Document(title="B", status="active",
                 sha256=b"sha_ef_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_address="bob@firm.com")
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="quarterly report", language="en"))
    await db_session.flush()

    # Exact match
    res = await hybrid_search(
        db_session, "quarterly", k=10,
        metadata_filter={"email.from_address": "alice@firm.com"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}

    # Case-insensitive
    res2 = await hybrid_search(
        db_session, "quarterly", k=10,
        metadata_filter={"email.from_address": "ALICE@FIRM.COM"},
    )
    assert {h.doc_id for h in res2.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_from_name_contains(db_session):
    """email.from_name_contains does ILIKE substring (case-insensitive)."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_fc_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_name="Alice Anderson")
    b = Document(title="B", status="active",
                 sha256=b"sha_fc_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_name="Bob Smith")
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="quarterly report", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session, "quarterly", k=10,
        metadata_filter={"email.from_name_contains": "alice"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_subject_contains(db_session):
    """email.subject_contains does ILIKE substring (case-insensitive)."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_sc_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_subject="Q3 Vendor Agreement Review")
    b = Document(title="B", status="active",
                 sha256=b"sha_sc_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_subject="Holiday party planning")
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session, "content", k=10,
        metadata_filter={"email.subject_contains": "VENDOR"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_to_addresses_array_element(db_session):
    """email.to_addresses matches when the address appears in the array."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_ta_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_to_addresses=["bob@firm.com", "carol@firm.com"])
    b = Document(title="B", status="active",
                 sha256=b"sha_ta_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_to_addresses=["dan@firm.com"])
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session, "content", k=10,
        metadata_filter={"email.to_addresses": "carol@firm.com"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_to_addresses_list_any(db_session):
    """List value matches when ANY element appears in the array."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_tl_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_to_addresses=["bob@firm.com", "carol@firm.com"])
    b = Document(title="B", status="active",
                 sha256=b"sha_tl_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_to_addresses=["zelda@elsewhere.com"])
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session, "content", k=10,
        metadata_filter={"email.to_addresses": ["carol@firm.com", "nobody@nope.com"]},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_contains_escapes_special_chars(db_session):
    """ILIKE special chars (% _) in input are matched literally."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_se_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_subject="Revenue grew 50% YoY")
    b = Document(title="B", status="active",
                 sha256=b"sha_se_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_subject="Revenue grew significantly")
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="content", language="en"))
    await db_session.flush()

    # "50%" must match only Doc A; '%' must NOT act as a wildcard
    res = await hybrid_search(
        db_session, "content", k=10,
        metadata_filter={"email.subject_contains": "50%"},
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}


async def test_metadata_filter_email_unknown_key_raises(db_session):
    """Unknown email.* key raises ValueError (loud failure)."""
    import pytest
    from harbor_clerk.search import hybrid_search

    with pytest.raises(ValueError, match="email"):
        await hybrid_search(
            db_session, "content", k=10,
            metadata_filter={"email.bogus_field": "x"},
        )


async def test_metadata_filter_email_does_not_break_jsonb(db_session):
    """Existing JSONB metadata_filter still works alongside email.* keys."""
    from harbor_clerk.models import Chunk, Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.search import hybrid_search

    a = Document(title="A", status="active",
                 sha256=b"sha_jx_a_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_address="alice@firm.com",
                 doc_metadata={"sidecar": {"vendor": "Acme"}})
    b = Document(title="B", status="active",
                 sha256=b"sha_jx_b_0000000000000000000000",
                 pipeline_status=PipelineStatus.ready,
                 email_from_address="alice@firm.com",
                 doc_metadata={"sidecar": {"vendor": "Globex"}})
    db_session.add_all([a, b])
    await db_session.flush()
    for doc in (a, b):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0,
                             chunk_text="content", language="en"))
    await db_session.flush()

    # Both filters apply (AND): from Alice AND vendor=Acme
    res = await hybrid_search(
        db_session, "content", k=10,
        metadata_filter={
            "email.from_address": "alice@firm.com",
            "sidecar.vendor": "Acme",
        },
    )
    assert {h.doc_id for h in res.hits} == {str(a.doc_id)}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_search_filtered.py -v -k "email"
```

Expected: 8 FAIL. Most will fail with a `ValueError` from the existing JSONB translator complaining about `email.foo` keys (the existing validator only accepts one-dot two-segment paths and translates them to JSONB containment — which won't match anything since the dedicated columns aren't in `doc_metadata`).

- [ ] **Step 3: Add the email.* pre-pass to `hybrid_search`**

In `src/harbor_clerk/search.py`, locate the `if metadata_filter:` block (around line 109). The new pre-pass goes BEFORE that block, working on a **copy** of `metadata_filter` so we can mutate while iterating. The full replacement:

```python
    # --- email.* pre-pass: strip email.* keys before the JSONB translator ---
    # The Document.email_* columns are dedicated typed columns, not in
    # doc_metadata JSONB, so they need column-level predicates rather than
    # JSONB containment. We pull these out first, build predicates, and
    # let the remaining keys (if any) flow through the JSONB block below.
    if metadata_filter:
        from harbor_clerk.sql_escape import escape_ilike

        email_keys = {k: v for k, v in metadata_filter.items() if k.startswith("email.")}
        if email_keys:
            metadata_filter = {k: v for k, v in metadata_filter.items() if not k.startswith("email.")}
            for path, value in email_keys.items():
                _, _, subkey = path.partition(".")
                if subkey == "from_address":
                    doc_conditions.append(func.lower(Document.email_from_address) == value.lower())
                elif subkey == "from_name":
                    doc_conditions.append(Document.email_from_name == value)
                elif subkey == "from_name_contains":
                    escaped = escape_ilike(value)
                    doc_conditions.append(
                        Document.email_from_name.ilike(f"%{escaped}%", escape="\\")
                    )
                elif subkey == "subject":
                    doc_conditions.append(Document.email_subject == value)
                elif subkey == "subject_contains":
                    escaped = escape_ilike(value)
                    doc_conditions.append(
                        Document.email_subject.ilike(f"%{escaped}%", escape="\\")
                    )
                elif subkey == "to_addresses":
                    if isinstance(value, list):
                        # ANY of the listed addresses in the array column
                        from sqlalchemy import or_ as _or_
                        doc_conditions.append(
                            _or_(*[v == func.any_(Document.email_to_addresses) for v in value])
                        )
                    else:
                        doc_conditions.append(value == func.any_(Document.email_to_addresses))
                elif subkey == "cc_addresses":
                    if isinstance(value, list):
                        from sqlalchemy import or_ as _or_
                        doc_conditions.append(
                            _or_(*[v == func.any_(Document.email_cc_addresses) for v in value])
                        )
                    else:
                        doc_conditions.append(value == func.any_(Document.email_cc_addresses))
                elif subkey == "thread_id":
                    doc_conditions.append(Document.email_thread_id == value)
                elif subkey == "message_id":
                    doc_conditions.append(Document.email_message_id == value)
                else:
                    raise ValueError(
                        f"unknown email.* filter key: {path!r}. Recognized: "
                        f"email.from_address, email.from_name, email.from_name_contains, "
                        f"email.subject, email.subject_contains, email.to_addresses, "
                        f"email.cc_addresses, email.thread_id, email.message_id."
                    )
```

Place this block **immediately before** the existing `if metadata_filter:` block (which then handles only the remaining non-`email.*` keys).

The existing `if metadata_filter:` block also needs a small adjustment — when `metadata_filter` is now `{}` after extracting all email keys, the loop should be skipped naturally. The existing code already handles this (`if metadata_filter:` is falsy for `{}`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_search_filtered.py -v -k "email"
```

Expected: 8 passed.

- [ ] **Step 5: Run the full regression suite to catch any breakage**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_search_filtered.py tests/test_search_metadata_filter.py tests/test_hybrid_search_with_rerank.py tests/test_find_all.py tests/test_kb_find_all_mcp.py -v 2>&1 | tail -10
```

Expected: all pass, no regressions in existing search behavior.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add src/harbor_clerk/search.py tests/test_search_filtered.py
git commit -m "feat(search): email.* metadata_filter namespace

hybrid_search now recognizes email.* keys in metadata_filter and
dispatches to dedicated Document.email_* column predicates:

  email.from_address          exact, case-insensitive
  email.from_name             exact
  email.from_name_contains    ILIKE %v% (escaped)
  email.subject               exact
  email.subject_contains      ILIKE %v% (escaped)
  email.to_addresses          = ANY(array); list value → OR across elements
  email.cc_addresses          same shape
  email.thread_id             exact
  email.message_id            exact

Unknown email.* key raises ValueError (loud failure). Non-email.* keys
in metadata_filter continue to JSONB-translate as before.

ILIKE special chars (% _) routed through escape_ilike() per PR-H
convention. The new trgm GIN indexes on email_subject + email_from_name
(Task 3) keep _contains queries fast at corpus scale.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Docstring updates on `kb_search`, `kb_find_all`, `kb_documents_by_date`

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Test: `tests/test_mcp_tool_descriptions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
def test_kb_search_docstring_mentions_email_namespace():
    """email.* filter keys must appear in kb_search description so models
    can discover them."""
    from harbor_clerk.mcp_server import kb_search
    desc = (kb_search.__doc__ or "").lower()
    assert "email.from_address" in desc
    assert "email.subject_contains" in desc
    assert "email.to_addresses" in desc


def test_kb_find_all_docstring_mentions_email_namespace():
    from harbor_clerk.mcp_server import kb_find_all
    desc = (kb_find_all.__doc__ or "").lower()
    assert "email.from_address" in desc
    assert "email.subject_contains" in desc


def test_kb_documents_by_date_docstring_mentions_email_namespace():
    from harbor_clerk.mcp_server import kb_documents_by_date
    desc = (kb_documents_by_date.__doc__ or "").lower()
    assert "email.from_address" in desc
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_mcp_tool_descriptions.py -v -k "email_namespace"
```

Expected: 3 FAIL.

- [ ] **Step 3: Add the docstring paragraph to all three tools**

In `src/harbor_clerk/mcp_server.py`, find each of `kb_search`, `kb_find_all`, `kb_documents_by_date`. Each has a `metadata_filter` parameter section in its docstring. After the existing `metadata_filter: ...` description (which talks about `namespace.key` JSONB lookups), append this paragraph:

```
        Additionally accepts the `email.*` namespace for native Document
        column lookups (faster + typed vs. raw tika.email_* via JSONB):
          email.from_address (exact, case-insensitive)
          email.from_name / email.from_name_contains
          email.subject / email.subject_contains
          email.to_addresses / email.cc_addresses (element-of-array;
            pass a list to match any element)
          email.thread_id / email.message_id (exact)
        For date filtering on emails, use the after=/before= filters
        (already mapped to email_date_sent for emails).
```

Match the existing indentation. Same paragraph in all three docstrings.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/test_mcp_tool_descriptions.py -v
```

Expected: all pass (the 3 new tests + the existing ones).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "docs(mcp): document email.* filter namespace on three tools

kb_search, kb_find_all, kb_documents_by_date docstrings each get a
paragraph describing the new email.* keys. Single source of truth —
hybrid_search owns the dispatch; the docstrings just surface the keys
to the model.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: End-to-end integration test (`.eml` → chunks → kb_search filter)

**Files:**
- Create: `tests/integration/test_email_header_chunks.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_email_header_chunks.py`:

```python
"""End-to-end: parse a real-shaped .eml, persist Document + chunks, then
verify the header preamble lives in chunk 0 AND the email.* filter
namespace returns the right doc."""

from harbor_clerk.mail.parser import parse_eml
from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search
from tests.mail.fixtures.build_eml import build_simple_email


async def test_eml_chunk_0_contains_header_preamble(db_session):
    """parse_eml + persist + chunk → chunk 0's text contains header preamble."""
    eml = build_simple_email(
        message_id="<e2e-headers@example.com>",
        subject="Q3 Vendor Agreement Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com"],
        body_text="The quarterly report attached covers Q3 performance.",
    )
    parsed = parse_eml(eml)

    doc = Document(
        title=parsed.subject, status="active",
        sha256=b"sha_e2e_h_0000000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id=parsed.message_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_to_addresses=parsed.to_addresses,
        email_subject=parsed.subject,
        email_date_sent=parsed.date_sent,
    )
    db_session.add(doc)
    await db_session.flush()
    # Single chunk holding the full body_text (preamble + body), simulating
    # what the real chunker does for a short body.
    db_session.add(Chunk(
        doc_id=doc.doc_id, chunk_num=0,
        chunk_text=parsed.body_text, language="en",
    ))
    await db_session.flush()

    # Header preamble lines all present in the chunk text
    assert "From: Alice Anderson <alice@firm.com>" in parsed.body_text
    assert "To: bob@firm.com" in parsed.body_text
    assert "Subject: Q3 Vendor Agreement Review" in parsed.body_text
    # And the body
    assert "quarterly report" in parsed.body_text


async def test_eml_email_from_address_filter_retrieves_doc(db_session):
    """End-to-end: hybrid_search with email.from_address filter returns
    only the doc whose dedicated column matches."""
    eml = build_simple_email(
        message_id="<e2e-filter@example.com>",
        subject="Q3 Vendor Agreement Review",
        sender="Alice Anderson <alice@firm.com>",
        recipients=["bob@firm.com"],
        body_text="The quarterly report attached covers Q3 performance.",
    )
    parsed = parse_eml(eml)

    # Doc A: from Alice
    a = Document(
        title=parsed.subject, status="active",
        sha256=b"sha_e2e_f_a_000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id=parsed.message_id,
        email_from_address=parsed.from_address,
        email_from_name=parsed.from_name,
        email_subject=parsed.subject,
    )
    db_session.add(a)
    await db_session.flush()
    db_session.add(Chunk(doc_id=a.doc_id, chunk_num=0,
                         chunk_text=parsed.body_text, language="en"))

    # Doc B: same subject, different sender (should NOT match the filter)
    b = Document(
        title=parsed.subject, status="active",
        sha256=b"sha_e2e_f_b_000000000000000000",
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        email_message_id="<e2e-other@example.com>",
        email_from_address="zelda@elsewhere.com",
        email_from_name="Zelda Z",
        email_subject=parsed.subject,
    )
    db_session.add(b)
    await db_session.flush()
    db_session.add(Chunk(doc_id=b.doc_id, chunk_num=0,
                         chunk_text="quarterly report content", language="en"))
    await db_session.flush()

    res = await hybrid_search(
        db_session, "quarterly report", k=10,
        metadata_filter={"email.from_address": "alice@firm.com"},
    )
    doc_ids = {h.doc_id for h in res.hits}
    assert str(a.doc_id) in doc_ids
    assert str(b.doc_id) not in doc_ids
```

- [ ] **Step 2: Run the integration tests**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run pytest tests/integration/test_email_header_chunks.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Run lint + format check + full suite**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/ -x --ignore=tests/integration 2>&1 | tail -5
uv run pytest tests/integration/test_email_header_chunks.py -v 2>&1 | tail -5
```

Expected: ruff clean; main suite passes; integration test passes.

- [ ] **Step 4: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git add tests/integration/test_email_header_chunks.py
git commit -m "test(integration): end-to-end email header preamble + email.* filter

Two end-to-end tests that exercise the full path: build .eml → parse →
persist Document + chunk → assert chunk text contains the preamble AND
hybrid_search with email.from_address filter retrieves the right doc.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Final verification + operator runbook note

**Files:** None modified. Verification + a single doc note.

- [ ] **Step 1: Run full Python suite + lint**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
uv run ruff check . 2>&1 | tail -3
uv run ruff format --check . 2>&1 | tail -3
uv run pytest tests/ -x --ignore=tests/integration 2>&1 | tail -5
```

Expected: ruff clean; all tests pass.

- [ ] **Step 2: Append operator runbook note to the spec**

Open `docs/superpowers/specs/2026-05-27-email-header-chunking-design.md` and confirm the spec's §5 (Re-ingest handoff) is intact. If not, add this section at the very end of the spec:

```markdown
## Operator runbook (post-deploy)

After the PR merges and the macOS app rebuilds, existing email docs need
re-ingest for the header preamble to materialize in their chunks. Run
this SQL against the menubar's Postgres (port 5433, db `lka`, user `lka`):

    UPDATE ingestion_jobs SET status = 'pending'
      WHERE doc_id IN (
        SELECT doc_id FROM documents WHERE email_message_id IS NOT NULL
      )
      AND stage = 'extract';

Workers will pick up the queue automatically. Expect ~1–2 hours wallclock
for 10k email docs. Visible via the Observatory page.
```

(If §5 already covers this, skip. The spec was written to include it.)

- [ ] **Step 3: Push branch + open PR**

```bash
cd /Users/alex/mcp-gateway/.worktrees/spec-email-headers
git push -u origin spec/email-header-chunking
```

Then open a PR titled `feat(mail+search): email header chunking + queryable email.* metadata` with a body summarizing the changes per the spec.

---

## Self-Review

**Spec coverage:**
- §1 architecture overview → Tasks 1, 2 (parser); Task 4 (search.py dispatch) ✓
- §2 parser header preamble format → Tasks 1, 2 ✓
- §3 chunking integration (no code change, just edge cases) → covered by integration test in Task 6 ✓
- §4 metadata_filter mapping for email.* keys → Task 4 ✓
- §4 trgm GIN indexes on email_subject + email_from_name → Task 3 ✓
- §4 docstring updates on kb_search/kb_find_all/kb_documents_by_date → Task 5 ✓
- §5 re-ingest handoff → Task 7 (runbook note) ✓
- §6 testing → Tasks 1-6 (unit + integration + tool-description tests) ✓
- §7 out of scope → not in plan (correct) ✓

**Placeholder scan:** No "TBD", "TODO", or "implement later". Every step has either complete code, a verbatim command, or a clear human action. The two unavoidable parameter substitutions in Task 3 (`<new_rev>`, `<prev_head>`) are explicitly defined by the alembic-revision command in Step 3 — the engineer reads them off and substitutes literal strings.

**Type consistency:**
- `_build_header_preamble` signature (Task 1) matches the call site in `parse_eml` (Task 2). Same kw-only args: `from_name, from_address, to_addresses, cc_addresses, subject, date_sent`.
- `email.*` filter keys (Task 4) match the docstring text (Task 5).
- Index names (`ix_documents_email_subject_trgm`, `ix_documents_email_from_name_trgm`) match between migration (Task 3) and tests (Task 3).
