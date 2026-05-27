# Email header chunking + queryable email metadata

**Status:** spec
**Date:** 2026-05-27
**Predecessors:** PR #283 / 2026-05-04 email-ingestion design (set the original "headers stay structured, body becomes chunks" intent); PR #411 / kb_find_all + text_contains rollout (surfaced the recall ceiling that motivated this); 2026-05-05-prod sweep + 2026-05-27 cross-model eval (the empirical signal).

---

## Background

PR #411's cross-model eval (Sonnet, gpt-4o, Opus on 9 items) confirmed `kb_find_all` + `text_contains` works on body-content questions (`enron-find-ferc` recall jumped from 0.32 → 0.98 for gpt-4o + Opus; `synth-find-invoices-over-5000` 0.18 → 1.00 for gpt-4o). It also surfaced a wall: `enron-find-layforwarded2001` capped at 0.12 recall across all 3 models, and `enron-find-offbalancesheet` at 0.12. Direct DB inspection showed why:

- "Forwarded by Lay" → **0 chunks** in 10,576 Enron docs. The phrase only ever appears in email headers (`From:` / `X-From:` lines), which the current parser routes to dedicated `Document.email_*` columns. The chunker only sees `body_text`.
- "off-balance-sheet" → 2 docs (literal hyphenated) + 4 more (no hyphens), vs ground-truth 17. The 11 missing docs likely had the phrase in `Subject:` lines.

The 2026-05-04 email-ingestion design (line 33) explicitly chose this: *"No new MCP tools for MVP. Email docs flow through kb_search naturally; sender names appear as PERSON entities via spaCy, so kb_entity_search covers 'docs from Alice'."* The plan was that NER on `chunk_text` would catch sender names. **But NER never sees headers**, because headers don't enter `chunk_text`. The intended affordance is broken at the chunking layer.

Beyond NER: the existing MCP `metadata_filter` parameter queries the `doc_metadata` JSONB column. `Document.email_*` are dedicated typed columns, not JSONB — so `metadata_filter={"email.from_address": "alice@..."}` doesn't work today either. ([`tika.email_from`](src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py:44) *is* queryable via JSONB but the values are raw Tika strings — un-parsed, comma-joined, string-dated — making them awkward in practice.)

This spec closes both gaps: prepends a header block to email `body_text` (so NER, FTS, and `text_contains` see headers naturally), AND maps an `email.*` namespace in `metadata_filter` to the dedicated Document columns (typed, array-aware, exact + ILIKE operators).

## Goal

Two surgical changes:

1. **Header preamble in chunks.** Modify `parse_eml` to build a structured key:value header block (From, To, Cc, Subject, Date) and prepend it to `body_text`. The chunker then naturally places the preamble at the start of chunk 0. NER, FTS, and `text_contains` filter all see headers as searchable text. **Re-ingest of existing email docs required** (operator-triggered).
2. **`email.*` namespace in `metadata_filter`.** Extend `hybrid_search`'s filter translator to recognize keys prefixed `email.` and dispatch to Document column predicates (exact match, ILIKE substring with `_contains` suffix, or `= ANY` for array columns). Two new trgm GIN indexes on `email_subject` and `email_from_name` to keep ILIKE queries fast at corpus scale.

Both surface improvements propagate to `kb_search`, `kb_find_all`, and `kb_documents_by_date` automatically — they all delegate to `hybrid_search`. No MCP-tool-layer code change required, only docstring additions.

## Non-goals

- **Filename / source_path / watched-folder / `email_label_path` filtering** (the "shape 3" location-based filtering from the brainstorm). Deferred as its own future work.
- **TIKA_FIELD_ALIASES whitelist expansion** (EPUB ISBN, EXIF date-taken, Office custom properties). Independent cleanup; bundle separately if real demand surfaces.
- **Wikilink backlinks surfaced via MCP.** Already a deferred follow-up under PR #384.
- **Display-name parsing for To/Cc recipients.** The current parser only extracts addresses for To/Cc (only From gets name+address). Adding name parsing for the others would let the preamble render `To: Bob Smith` instead of `To: bob@x.com`. Cosmetic + slight NER win; defer.
- **Dedicated `kb_email_search` MCP tool.** Original 2026-05-04 spec parked it. The `email.*` namespace via `metadata_filter` covers the precise-filter case without growing tool surface.
- **`tika.email_*` deduplication / removal.** Tika's email parser already populates `tika.email_from` etc. (raw, comma-joined). The new normalized `email.*` namespace coexists. Not worth removing.

## §1 — Architecture overview

Two surgical changes to existing files; no new modules.

**A. Header block prepended in the extract stage, reading from Document.email_* columns.**
- File: `src/harbor_clerk/worker/stages/extract.py`
- For email Documents (gated on `email_message_id IS NOT NULL AND email_parent_doc_id IS NULL` — excludes attachments), after Tika produces page text, build a structured key:value header block from the already-stored `Document.email_*` columns and prepend to the first DocumentPage's text.
- The chunker then naturally places the preamble at the start of chunk 0.
- The header preamble construction helper lives in `src/harbor_clerk/mail/parser.py` (called from extract.py via direct import).
- (Why we moved it from the parser: the parser approach was intuitive, but Tika is the source of truth for chunk text — the chunker reads `DocumentPage.page_text` populated by Tika; `parse_eml.body_text` is never persisted. Injecting in the parser would silently have no effect on chunks.)

**B. `email.*` keys recognized in `metadata_filter`.**
- File: `src/harbor_clerk/search.py` — the `metadata_filter` translator block inside `hybrid_search`.
- A small dispatch: if a filter key starts with `email.`, translate to a Document column predicate per §4's mapping table. Other keys continue to JSONB-translation as today.

Both changes propagate to `kb_search`, `kb_find_all`, and `kb_documents_by_date` automatically — they all delegate to `hybrid_search` / `find_all`. MCP tool docstrings get a paragraph each describing the new `email.*` filter keys.

## §2 — Preamble helper (parser.py) and extract-stage wiring

A new helper `_build_header_preamble(*, from_name, from_address, to_addresses, cc_addresses, subject, date_sent)` lives in `src/harbor_clerk/mail/parser.py`. It is called from the extract stage (`extract.py`) with values read directly from the already-stored `Document.email_*` columns, not from an `EmailParseResult`. The helper is pure — no DB access; extract.py imports it by name.

**Format — fixed key:value, in this exact order:**

```
From: Alice Anderson <alice@example.com>
To: Bob Smith, Carol Jones
Cc: Dan Roe
Subject: Q3 Vendor Agreement Review
Date: 2026-01-15

```
*(trailing blank line, then original body)*

**Per-field rules:**

| Field | Source | Format | Skip if |
|---|---|---|---|
| `From:` | `from_name` + `from_address` | `Name <address>` if both present; just address if `from_name` empty | both missing |
| `To:` | `to_addresses` | comma-joined addresses (display name parsing for To/Cc is out of scope — see Non-goals); if `>10` recipients → `$N recipients` (e.g., `45 recipients`) | empty list |
| `Cc:` | `cc_addresses` | same rules as To | empty list |
| `Subject:` | `subject` | as-is from parser; the existing `"(no subject)"` fallback persists | never (always include line if Subject column populated, which is always — fallback is `(no subject)`) |
| `Date:` | `date_sent` | ISO 8601 *date* `YYYY-MM-DD`, not full datetime | `None` |

**Edge cases:**

- **No name parsed for an address:** `from_name == ""` → emit `From: alice@example.com`. NER won't catch a name that isn't there; FTS still indexes the local-part.
- **All headers somehow absent:** preamble is the empty string; nothing prepended; body_text passes through unchanged. (This can't happen for real emails — every `.eml` has at least a Subject fallback — but the code is robust to it.)
- **Subject always shown** (even `"(no subject)"`) — keeps the format predictable for grep + `text_contains` patterns.
- **Body separator:** preamble ends with `\n\n`. Exactly one blank line between Date and body. Strong chunk-boundary signal.

**Length budget:** typical preamble ~120–180 chars. Worst case (10-recipient To + 10-recipient Cc + long subject): ~400 chars. Safely under the 1000-char chunk target.

## §3 — Chunking integration

**No chunker code changes.** The chunker reads `DocumentPage.page_text` (Tika's output, stored in the DB) and produces ~1000-char chunks with 150-char overlap. The preamble is prepended to the first page's text by the extract stage *before* the chunk stage runs, so chunk 0 naturally inherits the preamble without any chunker modification.

**Source of the preamble:** extract.py post-Tika, reading `Document.email_*` columns (populated earlier by `mail/ingest.py`). Attachment Documents (`email_parent_doc_id IS NOT NULL`) are excluded by the gate — they never receive the preamble.

**Edge cases handled by the existing chunker:**

- **Body shorter than one chunk** → chunk 0 = preamble + entire body. Single-chunk doc.
- **Empty-body email** → chunk 0 contains preamble only (~150 chars). The headers make the doc *more* discoverable than today via subject/from-name FTS.
- **Chunk overlap windows.** The 150-char overlap means chunk 1's start may include the tail of chunk 0's body — *not* the preamble (assuming preamble + body > ~1150 chars, which is typical). Subject lines etc. appear in chunk 0 only.
- **Preamble accidentally splitting across chunks.** Edge case: a ~900-char email where preamble + first body sentence lands at the chunk boundary. The preamble ends with `\n\n` (a strong sentence boundary for the chunker). Risk is negligible.
- **Attachment Documents** → extract stage gate excludes them; they receive no preamble. Only email body Documents (top-level emails) get the injection.

**Header block placed at chunk 0 only** (i.e., the start of the first page's text). Not repeated per chunk. Subject lines / from-names are 1-shot context for that doc — repeating them per chunk would bloat embeddings without adding discrimination signal.

## §4 — `metadata_filter` mapping for `email.*` keys

The `hybrid_search` filter translator gets a small pre-pass: any key starting with `email.` is stripped of prefix and dispatched per the table below. Remaining keys hit JSONB-translation as today.

**Recognized keys, mapping, and operator:**

| Filter key | Document column | Operator | Notes |
|---|---|---|---|
| `email.from_address` | `email_from_address` | exact (`= LOWER(...)` on both sides) | case-insensitive |
| `email.from_name` | `email_from_name` | exact (`=`) | rarely useful (display names vary); included for completeness |
| `email.from_name_contains` | `email_from_name` | `ILIKE '%v%'` (escaped) | the typical "from Alice" pattern |
| `email.subject` | `email_subject` | exact (`=`) | rarely useful (subjects almost never match verbatim) |
| `email.subject_contains` | `email_subject` | `ILIKE '%v%'` (escaped) | the typical "subject about X" pattern |
| `email.to_addresses` | `email_to_addresses` (TEXT[]) | `value = ANY(column)` | element match; list value → OR across elements |
| `email.cc_addresses` | `email_cc_addresses` (TEXT[]) | `value = ANY(column)` | same |
| `email.thread_id` | `email_thread_id` | exact (`=`) | by design |
| `email.message_id` | `email_message_id` | exact (`=`) | by design |

**Not exposed in this namespace:** `email.date_sent`. Use the existing `after`/`before` filter on `kb_search`/`kb_find_all`/`kb_documents_by_date` — they map to `Document.created_at`, which equals `email_date_sent` for emails (per `mail/ingest.py:99`).

**ILIKE escaping:** the `_contains` operators route through the shared `escape_ilike()` helper at `src/harbor_clerk/sql_escape.py` (the same helper PR-H established for all ILIKE call sites). `%` and `_` in user input are escaped to literal characters via `escape="\\"`.

**Validation:**
- Unknown `email.*` key (e.g., `email.foo`) → raise `ValueError` at translation time. Loud failure; the model gets feedback rather than silent empty results.
- `email.from_address` with a value that doesn't look like an email address → no validation; pass through as-is.
- Array-column key with a list value (`{"email.to_addresses": ["a@x", "b@y"]}`) → "any of these addresses" semantics (`= ANY` with OR across elements; SQL `value = ANY(column) OR value2 = ANY(column)`).

**New trgm GIN indexes** (new alembic migration):

```sql
CREATE INDEX CONCURRENTLY ix_documents_email_subject_trgm
  ON public.documents USING gin (email_subject public.gin_trgm_ops);
CREATE INDEX CONCURRENTLY ix_documents_email_from_name_trgm
  ON public.documents USING gin (email_from_name public.gin_trgm_ops);
```

Without these, `_contains` queries force seq-scans on the documents table. CONCURRENTLY-created via `autocommit_block()` per the pattern PR #411 established.

**Docstring updates** for `kb_search`, `kb_find_all`, `kb_documents_by_date`:

```
metadata_filter additionally accepts the `email.*` namespace for native
Document column lookups (faster + typed vs. raw tika.email_* via JSONB):
  email.from_address (exact)
  email.from_name_contains (substring)
  email.subject_contains (substring)
  email.to_addresses / email.cc_addresses (element-of-array)
  email.thread_id / email.message_id (exact)
  email.date_sent — use after=/before= filters instead (already mapped
    to email_date_sent for emails).
```

## §5 — Re-ingest handoff (operator-triggered)

After the code lands and the macOS app rebuilds, all existing email docs need their extract stage re-run so the new chunks (with header preamble) materialize.

**Criterion:** any Document with `email_message_id IS NOT NULL AND email_parent_doc_id IS NULL`. Attachment Documents also carry `email_message_id` (linking them to their parent email), but the extract-stage gate requires `email_parent_doc_id IS NULL` to exclude attachments from preamble injection — only email body Documents get the preamble.

**Operator action:** the spec doesn't pick a mechanism; HC's existing bulk-reprocess machinery (Documents page bulk action, `kb_reprocess` MCP tool in a loop, or a direct SQL update on `ingestion_jobs.status`) all work. Cleanest one-line trigger via direct SQL:

```sql
UPDATE ingestion_jobs SET status = 'queued'
  WHERE doc_id IN (
    SELECT doc_id FROM documents WHERE email_message_id IS NOT NULL AND email_parent_doc_id IS NULL
  )
  AND stage = 'extract';
```

Note: this raw SQL doesn't bump `documents.pipeline_seq`, which the worker race-checks during extract/chunk. For more robust re-processing on large corpora, prefer the `kb_reprocess` MCP tool (per-doc) or the Documents page bulk reprocess UI — both update `pipeline_seq` correctly. The raw SQL is fine on a quiescent system but may surprise during heavy concurrent ingest.

**Expected runtime:** ~1–2 hours wallclock for 10k emails on the menubar Mac. Full pipeline per doc: extract → chunk → entities → embed (skip summarize: body content unchanged; skip finalize: no-op). Visible via the Observatory page.

**Spec doc surfaces this as an operational note**, called out at the end of the implementation-plan deliverable so the operator (the project author for now) knows the change requires re-ingest to take effect on existing docs.

## §6 — Testing

**Unit tests:**

- `tests/mail/test_parser_header_preamble.py` (new):
  - Header block format — verify each field's exact line per §2
  - To/Cc collapse to `$N recipients` at >10
  - Missing `from_name` renders address only
  - `Subject:` always shown (uses `(no subject)` fallback when absent)
  - `Date:` is ISO `YYYY-MM-DD`, not full datetime
  - Body separator is exactly one blank line
  - Preamble + original body returned correctly
- `tests/test_search_filtered.py` (extend):
  - Each `email.*` filter key produces the expected match set (one test per key)
  - `email.from_address` is case-insensitive exact match
  - `email.from_name_contains` does ILIKE substring (case-insensitive)
  - `email.subject_contains` does ILIKE substring (case-insensitive)
  - `email.to_addresses` matches when the address appears in the array
  - List value on array key matches when ANY element appears
  - ILIKE special chars in input are escaped (`%`, `_`, `\`)
  - Unknown `email.foo` key raises `ValueError`
- `tests/test_alembic_email_metadata_indexes.py` (new):
  - Two trgm GIN indexes present on `documents.email_subject` and `documents.email_from_name`
  - Both use `gin_trgm_ops` (so ILIKE `%v%` is index-eligible)

**Integration tests:**

- `tests/integration/test_email_header_chunks.py` (new):
  - Parse a fixture `.eml` → assert returned `body_text` contains the preamble at the start, followed by the original body
  - Build chunks via the chunk stage → assert chunk 0's `chunk_text` contains `From: ...` / `Subject: ...` lines
  - Build a `with_recipients_long` fixture (≥11 To addresses) → assert preamble shows `$N recipients` collapse

**MCP-layer tests** (`tests/test_mcp_tool_descriptions.py`):
- Update expected docstring substrings to include the new `email.*` filter keys
- One test per kb_search / kb_find_all / kb_documents_by_date confirming `email.from_address` mentioned in description

## §7 — Out of scope / future work

- **Filename / `source_path` / watched-folder / `email_label_path` filtering** ("shape 3"). Deferred to its own follow-up.
- **TIKA_FIELD_ALIASES whitelist expansion** (EPUB ISBN, EXIF date-taken, Office custom properties).
- **Wikilink backlinks via MCP** (already deferred under PR #384).
- **Display-name parsing for To/Cc recipients** — parser today only extracts addresses for To/Cc; only From has name+address. Future enrichment.
- **Dedicated `kb_email_search` tool** — original spec's parked item; `email.*` namespace via `metadata_filter` covers most precise-filter cases.
- **`tika.email_*` namespace deduplication** — coexists with the new `email.*` namespace; not worth removing.

## §8 — Open questions

None blocking the implementation plan. Resolved inline during brainstorm:
- **Headers in chunks vs metadata-only:** revisited the 2026-05-04 design intent (NER on chunk_text for "from X"); confirmed implementation gap (headers never enter chunks → NER never sees senders); both routes (chunked preamble + metadata mapping) ship.
- **To/Cc collapse threshold:** 10 (from initial 5 proposal, expanded per user feedback).
- **Date as date-only vs full datetime:** date-only (`YYYY-MM-DD`).
- **trgm GIN indexes on email columns:** yes, both `email_subject` and `email_from_name`.
- **Re-ingest:** operator-triggered; no in-PR script.
