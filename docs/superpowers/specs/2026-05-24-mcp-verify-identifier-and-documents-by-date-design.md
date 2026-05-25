# PR-E: `kb_verify_identifier` + `kb_documents_by_date` — Design

**Date:** 2026-05-24
**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** PR-F (`2026-05-24-document-metadata-extractors-design.md`), PR-D (`2026-05-24-mcp-tool-descriptions-and-discriminator-hint-design.md`); both have shipped.

## Goal

Add two structured-lookup MCP tools to address two reproducible failure modes from the PR-A/B/C answer-eval runs that PR-D + PR-F do not fully solve:

1. **Proactive identifier verification** — give models a sharp tool for "is this a real, unique identifier in the corpus?" so they can self-correct fabrications before quoting. PR-D's `discriminator_hint` solves the *reactive* form (kb_search returned ambiguous hits — here is what differs); `kb_verify_identifier` solves the *proactive* form (before I cite Doc X, does Doc X actually exist? alone? among N candidates?).

2. **Date-bounded lookups** — give models a way to ask "earliest doc matching X" or "latest doc before date Y" without relying on similarity ranking, which fails on boundary docs that aren't in the similarity top-K.

## Why this PR exists (cross-corpus failure signal)

Concrete eval-failure data from the answer-eval runs:

**Enron (`reports/enron-phase2a/detail.json`):** both `lookup`-type questions scored **0/2/0** (correctness 0, groundedness 2, completeness 0):

- `enron-lookup-earliest-california`: model identified July 30, 2000 as the earliest California-related email; ground truth is October 13, 1999. The model returned a similar-but-later email.
- `enron-lookup-skilling-last-pre-resign`: model returned "Memo from Jeff Skilling" (Teesside explosion); ground truth is "Please Plan to Attend." Wrong "last" email.

Both failures share a structural cause: there's no tool that returns docs **sorted by date** with an optional content filter. The model picked from the similarity-top-K, which doesn't intersect the actual boundary doc.

**Synthetic (`reports/synthetic-phase2b/detail.json`):** ~38% (10/26) of lookups scored ≤2 on correctness. Recurring rationale: "presenting multiple dates across various documents," "buries this correct answer among many other jurisdictions," "gives 'Office of the Managing Partner' as the sender rather than the specific individual." All are boundary-doc cases where multiple documents share an identifier fragment. PR-D's `discriminator_hint` addresses the *reactive* form; `kb_verify_identifier` adds a *proactive* lever (the model can verify before answering).

## Non-goals

- **No fuzzy match in `kb_verify_identifier`.** Exact-CONTAINS on title/filename + equals on metadata identifier fields. Similarity-style "did you mean…?" is `kb_search`'s job — keeping the affordance sharp matters more than convenience here.
- **No vector ranking in `kb_documents_by_date`.** The optional `query` parameter is FTS-only. Sort key is date, not similarity.
- **No new metadata extraction.** Operates on what PR-F already extracts (Tika, frontmatter, sidecar) + the existing `documents.created_at` ingest time.
- **No verify across full body text.** That's `kb_search` territory — would blur the affordance.
- **No natural-language date parsing** for `after`/`before`. ISO 8601 only — MCP clients pass machine dates.

## Architecture

Two new MCP tools, both implemented in a single new module with a shared date-discovery helper.

**New files:**
- `src/harbor_clerk/mcp_lookup_tools.py` — implementations of both tools (~300 LOC combined)
- `src/harbor_clerk/metadata_dates.py` — `effective_date()` helper (~40 LOC)
- `tests/test_mcp_lookup_tools.py` — algorithm + integration tests
- `tests/test_metadata_dates.py` — date-discovery unit tests

**Touched files:**
- `src/harbor_clerk/mcp_server.py` — two new `@mcp.tool` thin wrappers with PR-D-style 4-part docstrings (what / when / output / decline); registration in the tool list
- `tests/test_mcp_tool_descriptions.py` — extend the pin-test set for the two new tools

**Why a single module for both tools:** they share semantics ("structured non-similarity lookup over the metadata column"), they share the same surface area in `mcp_server.py`, and individually each is small (~150 LOC). Mirrors the pattern of `mcp_discriminator.py` keeping algorithm + helpers together.

**Why a separate `metadata_dates.py`:** the effective-date concept is genuinely reusable — future features (document list views, exports, additional filters) can call `effective_date(doc)` rather than re-deriving the priority chain.

## `kb_verify_identifier`

### Signature

```python
async def kb_verify_identifier(identifier: str) -> str  # JSON-encoded
```

### Matching rules

Case-insensitive, whitespace-normalized (collapse internal whitespace; strip leading/trailing).

- **CONTAINS match on:**
  - `documents.title`
  - `documents.canonical_filename`
- **EQUALS match (case-insensitive, whitespace-normalized) on:**
  - `metadata.tika.title` (Tika-extracted title from DOCX/PDF/EPUB properties — sometimes differs from HC's `documents.title`)
  - any leaf string value reachable at `metadata.sidecar.**` or `metadata.frontmatter.**` (recursive walk into nested objects), whose **leaf key name** matches the identifier-like-key regex:

    ```
    ^(id|contract_id|policy_id|case_id|order_id|invoice_id|message_id|.*_id)$
    ```

  The recursive walk lets a nested structure like `{"contract": {"id": "K-2025-031"}}` participate — the leaf-key check is on `id`, not `contract.id`. Lists at leaves participate too: a value list `["K-2025-031", "K-2025-032"]` matches if the input equals any element.

  Rationale: identifier fields users actually put in sidecars/frontmatter follow this naming convention. Names like `vendor` or `date` are not identifiers in this sense — they're disambiguators, surfaced via `discriminating_fields` in the response, not by matching against the input string.

**Normalization applies to both sides** of every match: the input identifier and the candidate field value are both lower-cased and whitespace-collapsed before comparison.

### Response shape

```jsonc
// 0 matches
{
  "status": "not_found",
  "identifier": "<input string>"
}

// exactly 1 match
{
  "status": "unique",
  "match": {
    "doc_id": "<uuid>",
    "title": "<doc title>",
    "canonical_filename": "<filename>",
    "discriminating_fields": {}  // always empty when unique
  }
}

// N > 1 matches
{
  "status": "ambiguous",
  "count": 3,
  "candidates": [
    {
      "doc_id": "<uuid>",
      "title": "Pinnacle Vendor Contract",
      "canonical_filename": "0131_vendor_contract.pdf",
      "discriminating_fields": {
        "sidecar.vendor": "Pinnacle Tech Solutions, LLC",
        "sidecar.term_months": 24
      }
    }
    // ...
  ],
  "suggestion": "3 candidates differ on sidecar.vendor and sidecar.term_months — pick the one matching your intent."
}
```

### Discriminating-fields algorithm

Reuse `_find_differing_metadata_fields()` from PR-D's `src/harbor_clerk/mcp_discriminator.py` to determine **which** metadata paths differ across the candidate set. That function returns `{"namespace.key": {title: value, ...}, ...}` — paths whose values vary across candidates, with the full per-candidate values.

For each candidate's `discriminating_fields` in the response, **project** the per-candidate value at each differing path. (The helper returns the cross-candidate view; verify_identifier presents the per-candidate view.) Both views derive from the same underlying paths — single source of truth for "what metadata distinguishes these docs."

Skip the `_source_provenance` namespace (same as discriminator_hint).

### Suggestion text

The `suggestion` field is verify-specific (different from PR-D's `discriminator_hint` suggestion, which is geared toward "issue a follow-up `kb_search` with `metadata_filter=...`"). For verify, the model already has the candidates in hand; the suggestion guides selection:

```
"3 candidates differ on sidecar.vendor and sidecar.term_months — pick the one matching your intent."
```

When no fields differ across candidates (rare; would mean the candidates are duplicates the discriminator can't distinguish), suggestion reads:

```
"Multiple candidates have identical discriminating metadata — try kb_get_document on each to inspect body."
```

### Cap & overflow

Worst case all docs contain the identifier substring. Cap the candidates list at **100**. When the underlying match set exceeds the cap, include `"overflow": true` in the response alongside `count` (which reflects the cap, not the true total) and adjust the suggestion to "Filter by … or refine the identifier — more than 100 candidates matched."

```jsonc
// >100 matches
{
  "status": "ambiguous",
  "count": 100,
  "overflow": true,
  "candidates": [ /* 100 entries */ ],
  "suggestion": "..."
}
```

### Performance

- Index: existing GIN on `metadata` from PR-F handles the JSONB equality lookups efficiently.
- ILIKE on title/filename is small-N (corpora are O(10^4) docs; the LIKE scan is bounded).
- The 100-candidate cap bounds payload size and discriminating-fields compute cost.

## `kb_documents_by_date`

### Signature

```python
async def kb_documents_by_date(
    direction: Literal["earliest", "latest"] = "earliest",
    query: str | None = None,
    metadata_filter: dict | None = None,
    after: str | None = None,
    before: str | None = None,
    date_field: str | None = None,
    limit: int = 10,
) -> str  # JSON-encoded
```

### Date discovery (default behavior)

For each candidate document, the effective date is the **first non-null value** in this priority chain:

1. `metadata.tika.created_at` — Tika-extracted creation/send date
2. `metadata.frontmatter.date` — markdown frontmatter
3. `metadata.sidecar.date` — JSON sidecar
4. `documents.created_at` — ingest time (last resort)

When the caller passes an explicit `date_field`, only that field is consulted (no fallback). The accepted values are exactly:

- `"tika.created_at"`
- `"frontmatter.date"`
- `"sidecar.date"`
- `"ingest"`

(`date_field` is a shorthand — callers do not pass the `metadata.` prefix.) Unknown `date_field` returns an error response listing the accepted values.

### Query semantics

If `query` is provided: FTS-only filter via `chunks.fts_en + chunks.fts_fr` (no vector — sort key is time, not similarity). Implementation: derive the candidate document set as `SELECT DISTINCT doc_id FROM chunks WHERE fts_en @@ websearch_to_tsquery('english', :q) OR fts_fr @@ websearch_to_tsquery('french', :q)`, then join to `documents` and apply the date sort + filters. A chunk match anywhere in a document admits the document to the candidate set.

If `query` is absent: returns all active docs sorted by date (subject to `metadata_filter` / `after` / `before`).

### Date bounds

`after` and `before` apply to the **effective date** (the one chosen by the discovery chain). ISO 8601 only — no natural-language parsing.

### Response shape

```jsonc
{
  "direction": "earliest",
  "count": 5,
  "results": [
    {
      "doc_id": "<uuid>",
      "title": "<doc title>",
      "canonical_filename": "<filename>",
      "date": "1999-10-13T00:00:00+00:00",
      "date_source": "metadata.tika.created_at"
    }
    // ...
  ]
}
```

### Sort & SQL

The effective-date sort is a SQL CASE expression (or `COALESCE` over JSONB extracts and `documents.created_at`) inside the `ORDER BY`. After-filter and before-filter apply to the same expression.

When `date_field` is explicit, the expression collapses to a single JSONB extract or column reference.

## `metadata_dates.py`

```python
def effective_date(doc: Document) -> tuple[datetime | None, str]:
    """
    Returns (date, source_label) for a document.

    Priority order:
      metadata.tika.created_at
      → metadata.frontmatter.date
      → metadata.sidecar.date
      → documents.created_at

    source_label ∈ {"tika.created_at", "frontmatter.date",
                    "sidecar.date", "ingest", "none"}.
    "none" returned only if every source is missing AND ingest is null
    (unreachable in practice — docs always have created_at).
    """
```

**Parsing:** handles ISO 8601 strings with timezone, ISO without timezone (assume UTC), date-only strings, and `datetime` objects (sidecar JSON may have been coerced). Returns `None` for unparseable values rather than raising — keeps callers simple.

## Error handling

- **Invalid identifier** (empty / whitespace-only): tool returns JSON `{"error": "identifier must be a non-empty string"}`. No exception.
- **Invalid `direction`**: rejected by Pydantic `Literal` validation at the MCP layer. No manual check.
- **Invalid date string** (`after`, `before`): `{"error": "could not parse date: '<value>'"}`.
- **Invalid `metadata_filter`** (multi-dot path, empty segment): reuse PR-F's `ValueError`; MCP layer catches and returns error JSON. Same surface as `kb_search`.
- **Invalid `date_field`** (unknown name): `{"error": "date_field must be one of: tika.created_at, frontmatter.date, sidecar.date, ingest"}`.
- **Empty results**: never an error. `kb_verify_identifier` returns `{"status": "not_found"}`; `kb_documents_by_date` returns `{"count": 0, "results": []}`.
- **DB timeout / connection error**: propagate to the existing MCP error handler. Matches the pattern of every other kb_* tool.

## Testing

### `tests/test_metadata_dates.py` (~6 tests)

- Each priority source individually (Tika / frontmatter / sidecar / ingest)
- Priority order: Tika wins when multiple sources present; frontmatter wins when no Tika; etc.
- ISO 8601 with timezone, ISO without timezone, date-only
- Unparseable string → `None`
- All sources missing except ingest → falls through to ingest with `source_label="ingest"`

### `tests/test_mcp_lookup_tools.py` (~15–18 tests)

**`kb_verify_identifier`:**
- `status="not_found"` for an identifier no doc matches
- `status="unique"` by title match
- `status="unique"` by `canonical_filename` match
- `status="unique"` by `metadata.sidecar.contract_id` exact match
- `status="ambiguous"` with `candidates` list and `suggestion`
- Case-insensitive: `"PINNACLE"` matches `"Pinnacle Vendor Contract"`
- Whitespace-normalized: `"  pinnacle  "` matches the same
- Identifier-like-key regex matches `case_id`, `order_id`; does NOT match `vendor`, `term_months`
- `discriminating_fields` is empty when status is unique; populated when ambiguous and candidates differ on at least one field
- Empty / whitespace-only identifier → error response

**`kb_documents_by_date`:**
- `direction="earliest"` returns oldest first
- `direction="latest"` returns newest first
- `query` + `direction` returns only docs matching the FTS query, sorted by date
- `metadata_filter` + `direction` filters by JSONB containment, sorted
- `after` / `before` bound the result set
- Explicit `date_field="ingest"` overrides the discovery chain
- Doc with no Tika / frontmatter / sidecar date → uses ingest time, `date_source="ingest"`
- `limit` is respected
- Empty results return `{"count": 0, "results": []}`
- Invalid `date_field` → error response

### Tool-description tests (extend `tests/test_mcp_tool_descriptions.py`)

- 4-part docstring (what / when / output / decline) per PR-D convention
- Key affordance words present: `"verify"`, `"before quoting"`, `"earliest"`, `"latest"`, `"date_source"`
- New tools listed in the public-tool inventory (the test that pins the list of exposed tools)

### Integration test

Seed test DB with 3 fixture docs spanning corpus shapes:

- **Enron-style**: `metadata.tika.created_at = 1999-10-13`, body mentions "California"
- **Synthetic-style**: `metadata.sidecar.date = 2025-04-19`, body mentions onboarding
- **Plain**: only `documents.created_at`, no other dates

End-to-end:
- `kb_documents_by_date(direction="earliest", query="California")` returns the Enron-style doc first with `date_source="tika.created_at"` — same scenario as the failed Enron eval.
- `kb_verify_identifier("Pinnacle vendor contract")` against a multi-Pinnacle fixture returns `status="ambiguous"` with discriminating_fields populated.

## Out of scope / follow-ups

- **Numeric/range comparators on metadata** (`>=`, `<`, `between`, `in`) — still deferred per PR-F's spec; PR-E's `after`/`before` for dates is the most-common-case partial answer. Generalised range/comparator operators is a separate metadata-filter-v2 PR.
- **Folder-path-as-metadata for date discovery** (e.g. `2024-10/foo.eml` filename → date) — separate config-story PR per PR-F's spec.
- **`kb_count_matches`** — pure count tool ("how many docs match X?") — punted; revisit if eval shows the gap.
- **`verify_identifier` across body text** — explicit non-goal. That's `kb_search`'s job; would blur the affordance.
- **Localized / natural-language date parsing** (`"January 15, 2024"` / `"15/01/2024"` / `"yesterday"`) — ISO only for v1. MCP clients are machines.
- **Sidecar identifier-key heuristic refinement** — the regex `^(id|contract_id|policy_id|case_id|order_id|invoice_id|message_id|.*_id)$` is a guess at common naming. Real-world corpora may use `bates`, `docket`, `claim_number`, `policy_no`. Revisit if eval misses surface; consider a config-driven list.
- **`verify_identifier` `fields=` parameter** — to let the model choose which fields to search. Punted; v1 hardcodes the field set. Add if eval shows model wants the flexibility.
- **PR-G (harness audit + cross-judge sensitivity)** — pure harness work, can land in parallel or after PR-E.

## Decisions (closed during brainstorming)

- **Scope: both tools** (`kb_verify_identifier` + `kb_documents_by_date`). PR-D's `discriminator_hint` covers the reactive disambiguation path; PR-E adds the proactive verify path and the date-sorted path that similarity ranking can't reach.
- **`kb_verify_identifier` is exact-CONTAINS on title + filename, equals on identifier-like metadata keys.** No similarity fallback — keeps the affordance sharp.
- **`kb_documents_by_date` uses a smart date-field auto-discovery chain** with optional explicit `date_field` override. Defaults work for the eval-failure shapes (Enron emails use Tika; synthetic uses sidecar; plain docs fall back to ingest).
- **No-date docs fall through to ingest time** rather than being excluded. Predictable; debuggable; `date_source` annotation tells the model what happened.
- **Each `verify_identifier` candidate carries `doc_id` + `title` + `canonical_filename` + `discriminating_fields`** (computed via PR-D's algorithm reused). Models can disambiguate without follow-up `kb_get_document` calls.
- **Single implementation module + separate date helper.** `mcp_lookup_tools.py` for both tools (they're semantically related and individually small); `metadata_dates.py` for the shared helper that future code can call.
- **Both tools follow PR-D's 4-part docstring convention** (what / when / output / decline). Pin-tested in `test_mcp_tool_descriptions.py`.

## Open questions / risks

- **Identifier-key regex precision.** The current pattern catches common naming but will miss legal corpora (`bates`, `docket`) and insurance corpora (`policy_no`, `claim_number`). Initial release ships the conservative pattern; widen based on observed gaps. Worst case: a sidecar field models can't reach with `verify_identifier` is still reachable with `kb_search(metadata_filter=...)` from PR-F.
- **Tika date field name variations.** Tika's `Message-Date` (emails) is aliased to `email_date` and stored at `metadata.tika.created_at` in HC's extractor (per PR-F). Other Tika date fields exist (`Last-Modified`, `dcterms:modified`); not surfaced yet. If the eval shows `modified_at` as a useful sort key, add it to the chain (priority 1.5, between Tika created and frontmatter).
- **Performance ceiling on `verify_identifier` ILIKE.** On corpora >100k docs, the title+filename ILIKE scan dominates. Acceptable for v1 (no observed corpus near that size in HC's target audience); revisit if it becomes a bottleneck. Pre-built trigram index on `documents.title` would mitigate.
- **SQL portability of the COALESCE-over-JSONB ORDER BY.** Targeting PostgreSQL only (HC's only backend) — no cross-DB concern. PG can sort by an expression in `ORDER BY` even when not in `SELECT`.
