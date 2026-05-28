# Metadata polish: JSONB translator dedup + EPUB ISBN

**Status:** spec
**Date:** 2026-05-27
**Predecessors:** PR #414 (email header chunking — established the `_apply_email_metadata_filter` shared-helper pattern; surfaced the JSONB translator duplication as a follow-up).

---

## Background

PR #414's fresh-eyes review surfaced two metadata-layer follow-ups worth bundling:

1. **JSONB translator duplication.** PR #414 extracted `_apply_email_metadata_filter()` as a shared helper between `hybrid_search` and `_query_documents_by_date` — closing one duplication. But the *underlying* JSONB-translation block (the loop that turns `{"namespace.key": value}` into `doc_metadata @>` containment + optional JSONB `?` existence predicates) is still copy-pasted between `src/harbor_clerk/search.py:205-219` and `src/harbor_clerk/mcp_lookup_tools.py:396-411`. Both files even carry the comment *"If this diverges from search.py, both should be updated together"* — explicit acknowledgement of the hazard. This spec applies the same shared-helper pattern.

2. **EPUB ISBN missing from Tika whitelist.** `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py:27-52` maintains a whitelist mapping Tika field names to flat keys on `Document.doc_metadata.tika.*`. EPUB and PDF files carry a `dc:identifier` Dublin Core field (often an ISBN); Tika emits it in `/meta` responses; the alias map drops it. Adding one line makes ISBN queryable via `metadata_filter={"tika.isbn": "..."}`.

Both items are pure cleanup/additive — no behavior changes for existing callers. The grouping is sensible because both touch the metadata-filter surface and are small enough to ship together without scope bloat.

## Goal

Two surgical changes:

1. **Extract `_apply_jsonb_metadata_filter()` shared helper.** Mirror the `_apply_email_metadata_filter()` pattern (PR #414). Both `hybrid_search` and `_query_documents_by_date` call the helper; the 15-line loop body lives in one place.

2. **Add `dc:identifier` → `isbn` to TIKA_FIELD_ALIASES.** One-line addition. EPUB/PDF docs that have a `dc:identifier` get `tika.isbn` populated; the existing JSONB containment path makes it queryable via `metadata_filter`.

## Non-goals

- **`parse_eml.body_text` cleanup.** Captured as a PR #414 follow-up. Field is "dead in production" but actively used by ~10 parser-correctness tests. Real fix is either rewriting those tests (modest cleanup, limited gain) or a larger architectural shift to feed `parse_eml.body_text` into `DocumentPage` directly (skip Tika for emails — needs its own spec). Deferred.
- **Other TIKA_FIELD_ALIASES gaps.** EXIF date-taken, camera make/model, GPS coords for images; Office docx custom properties (`meta:revision`, etc.). Each ~1 line but speculative until a real use case shows up. ISBN alone has clear EPUB demand (the user's bundled-corpus question).
- **`dc:identifier` validation.** The Dublin Core spec defines `dc:identifier` as intentionally generic — it can be an ISBN, DOI, URN, or arbitrary string. We alias it to `isbn` because that's the most common use in HC's target corpora; mis-identified values still get stored under `tika.isbn` and just won't match an ISBN-shaped filter. No format validation in this PR.

## §1 — JSONB translator dedup

### New helper

In `src/harbor_clerk/search.py`, alongside the existing `_apply_email_metadata_filter()`, add:

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
    email.* keys first and returns the remaining dict; this one
    consumes that remaining dict.

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

Export from `__all__` alongside `_apply_email_metadata_filter`.

### Call site updates

**`src/harbor_clerk/search.py` (in `hybrid_search`)** — replace the existing inline loop at lines 205-219 with:

```python
    if metadata_filter:
        _apply_jsonb_metadata_filter(metadata_filter, doc_conditions)
```

**`src/harbor_clerk/mcp_lookup_tools.py` (in `_query_documents_by_date`)** — same pattern; replace the inline loop at lines 396-411 with the same one-line call. Import the helper alongside the existing `_apply_email_metadata_filter` import.

### Convention note

The two helpers follow different return conventions, intentionally:
- `_apply_email_metadata_filter(metadata_filter, doc_conditions) -> dict` — strips `email.*` keys, returns the remaining dict for downstream processing.
- `_apply_jsonb_metadata_filter(metadata_filter, doc_conditions) -> None` — consumes the remaining dict, no return value.

The asymmetry reflects the pipeline shape: email pre-pass → JSONB consumer. A future reader sees the convention and shouldn't be confused; documented in both docstrings.

## §2 — TIKA_FIELD_ALIASES: `dc:identifier` → `isbn`

In `src/harbor_clerk/ingest/metadata_extractors/tika_metadata.py`, add one entry to the `TIKA_FIELD_ALIASES` dict, grouped with the other Dublin Core fields:

```python
TIKA_FIELD_ALIASES: dict[str, str] = {
    # Dublin Core
    "dc:creator": "author",
    "dc:title": "title",
    "dc:subject": "subject",
    "dc:description": "description",
    "dc:language": "language",
    "dc:identifier": "isbn",  # NEW — EPUBs and some PDFs carry ISBN here
    "dcterms:created": "created_at",
    "dcterms:modified": "modified_at",
    "meta:keyword": "keywords",
    # ... rest unchanged
}
```

Position: between `dc:language` and `dcterms:created` — alphabetical-ish, stays within the Dublin Core block.

Once ingested, the field is queryable via:
```python
kb_search(query="...", metadata_filter={"tika.isbn": "978-0-..."})
kb_find_all(query="...", metadata_filter={"tika.isbn": "978-0-..."})
```

No new tools, no docstring changes — `metadata_filter` already documents the JSONB namespace pattern; `tika.isbn` slots in automatically.

## §3 — Testing

### Unit test for `_apply_jsonb_metadata_filter`

Add to `tests/test_search_metadata_filter.py` (the existing JSONB-translation tests live there). Verifies the helper:
- Builds containment predicates for scalar string values
- Adds the existence OR-branch for string values
- Skips the existence branch for non-string scalars (int, bool)
- Raises `ValueError` on missing/extra dots, empty segments
- Mutates `doc_conditions` in place (returns None)

Existing tests in `test_search_metadata_filter.py` and `test_mcp_lookup_tools.py` already cover the behavior through the public surface; this unit test just pins the helper's contract directly.

### Test for ISBN alias

Add to `tests/test_tika_metadata_extractor.py` (extend existing alias tests). Confirms a `dc:identifier` field in a mock Tika response gets stored as `tika.isbn` on the resulting metadata dict.

## §4 — Out of scope (deferred follow-ups)

- **`parse_eml.body_text` cleanup** — see Background; deferred until one of the two framings becomes worth it.
- **TIKA_FIELD_ALIASES — EXIF / Office custom properties** — each ~1 line; defer until a concrete use case (image-heavy corpus, Office workflow with custom metadata).
- **Shape-3 location filtering** (`canonical_filename`, `source_path`, `email_label_path`, `watched_folders.path`) — own spec; bigger scope.
- **Wikilink backlinks via MCP** — already in `pr_followups.md` under PR #384.
