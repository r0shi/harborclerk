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
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import case, cast, func, literal, null, or_, select
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mcp_discriminator import _find_differing_metadata_fields
from harbor_clerk.models import Chunk, Document

_VERIFY_CANDIDATE_CAP = 100

# Leaf-key names that count as identifiers when matching metadata.sidecar.**
# or metadata.frontmatter.**. Anything else is treated as a disambiguator
# (vendor, date, etc.), surfaced via discriminating_fields rather than
# matched against the input string.
_IDENTIFIER_KEY_RE = re.compile(r"^(id|contract_id|policy_id|case_id|order_id|invoice_id|message_id|.*_id)$")

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

    # Python-side pass for identifier-like metadata keys. Fetch active
    # docs whose metadata is non-empty and walk their JSON structure.
    # Capped at _VERIFY_CANDIDATE_CAP so the Python-side loop is always
    # bounded: a corpus with 10k+ metadata-bearing docs never loads them
    # all into memory on a single call.
    stmt_meta = (
        select(Document)
        .where(Document.status == "active")
        .where(Document.doc_metadata != {})
        .limit(_VERIFY_CANDIDATE_CAP)
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


def _has_nested_metadata(metadata_by_doc: dict[str, dict]) -> bool:
    """Return True if any candidate document has a dict-valued leaf inside a
    known metadata namespace (e.g. ``sidecar.contract`` is itself a dict).

    This signals that the discriminating information may be nested deeper than
    the one-level projection used by ``_find_differing_metadata_fields``, so
    the ambiguous-match suggestion should guide the caller to inspect the raw
    document rather than implying the candidates are identical.
    """
    for meta in metadata_by_doc.values():
        for ns in _ID_KEY_NAMESPACES:
            ns_dict = meta.get(ns)
            if isinstance(ns_dict, dict):
                for value in ns_dict.values():
                    if isinstance(value, dict):
                        return True
    return False


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
        suggestion = f"{len(candidates)} candidates differ on {field_list} — pick the one matching your intent."
    elif _has_nested_metadata(metadata_by_doc):
        suggestion = (
            "Multiple candidates matched, but their distinguishing metadata is nested — "
            "inspect with kb_get_document on each."
        )
    else:
        suggestion = (
            "Multiple candidates have identical discriminating metadata — try kb_get_document on each to inspect body."
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


# ---------------------------------------------------------------------------
# _query_documents_by_date — SQL query layer for kb_documents_by_date
# ---------------------------------------------------------------------------

_ALLOWED_DATE_FIELDS = frozenset({"tika.created_at", "frontmatter.date", "sidecar.date", "ingest"})

# ISO 8601 prefix: requires at minimum YYYY-MM-DD.  Matches date-only strings
# ("2024-01-15") as well as full datetimes ("2024-01-15T08:00:00Z").  Non-ISO
# strings emitted by Tika (e.g. "N/A", "", localized formats) won't match and
# produce NULL instead of raising invalid_datetime_format.
_ISO_DATE_PREFIX_RE = r"^\d{4}-\d{2}-\d{2}"


def _safe_timestamp_cast(text_expr: Any) -> Any:
    """Return a SQLAlchemy expression that casts *text_expr* to TIMESTAMPTZ
    when it starts with an ISO 8601 date prefix, or NULL otherwise.

    This prevents a single document with a malformed Tika date (e.g. "N/A")
    from raising ``invalid_datetime_format`` and aborting the entire query.
    """
    return case(
        (text_expr.op("~")(_ISO_DATE_PREFIX_RE), cast(text_expr, TIMESTAMP(timezone=True))),
        else_=null(),
    )


def _date_components() -> list[tuple[str, Any]]:
    """Return (label, expr) tuples per priority slot.

    Expressions are built fresh per call — SQLAlchemy column elements are
    immutable but we avoid any cross-call binding state by constructing here.
    The priority order is: tika.created_at → frontmatter.date → sidecar.date → ingest.

    JSONB text slots use _safe_timestamp_cast so a malformed date value
    (e.g. Tika emitting "N/A") returns NULL and falls through to the next
    priority slot instead of raising an invalid_datetime_format error.
    """
    tika_expr = _safe_timestamp_cast(Document.doc_metadata["tika"]["created_at"].astext)
    fm_expr = _safe_timestamp_cast(Document.doc_metadata["frontmatter"]["date"].astext)
    sc_expr = _safe_timestamp_cast(Document.doc_metadata["sidecar"]["date"].astext)
    ingest_expr = Document.created_at
    return [
        ("tika.created_at", tika_expr),
        ("frontmatter.date", fm_expr),
        ("sidecar.date", sc_expr),
        ("ingest", ingest_expr),
    ]


def _parse_iso_date(value: str) -> datetime:
    """Parse an ISO 8601 date or datetime string into a UTC-aware datetime."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
    or invalid `metadata_filter` keys.
    """
    if direction not in ("earliest", "latest"):
        raise ValueError(f"direction must be 'earliest' or 'latest'; got {direction!r}")

    components = _date_components()

    # Build the effective-date expression + source-label CASE.
    if date_field is not None:
        if date_field not in _ALLOWED_DATE_FIELDS:
            raise ValueError(f"date_field must be one of: {sorted(_ALLOWED_DATE_FIELDS)}; got {date_field!r}")
        expr = next(e for label, e in components if label == date_field)
        source_label_expr = literal(date_field)
    else:
        # COALESCE in priority order; CASE picks the first non-null source label.
        # Both use the same guarded slot expressions from _date_components() so
        # they always agree on which slot "won" — if a JSONB cast returned NULL
        # (malformed date), the CASE won't label it as that slot either.
        slot_exprs = [e for _, e in components]  # already guarded by _safe_timestamp_cast
        slot_labels = [label for label, _ in components]

        expr = func.coalesce(*slot_exprs)

        # Build CASE using isnot(None) on the SAME guarded expressions that
        # COALESCE uses, so label and date always refer to the same source.
        # The final slot ("ingest") is Document.created_at which is never NULL.
        source_label_expr = case(
            *[(slot_exprs[i].isnot(None), literal(slot_labels[i])) for i in range(len(components) - 1)],
            else_=literal(slot_labels[-1]),
        )

    stmt = select(Document, expr.label("effective_date"), source_label_expr.label("date_source")).where(
        Document.status == "active"
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

    # Optional metadata_filter — same validation and matching logic as
    # hybrid_search() in search.py.  Each "<namespace>.<key>": value pair
    # becomes:
    #   - JSONB @> containment: doc_metadata @> '{"ns": {"key": value}}'
    #     (matches scalar metadata)
    #   - OR JSONB ? existence: doc_metadata->'ns'->'key' ? 'value'
    #     (matches list-valued metadata containing the scalar)
    # The OR lets a caller use a scalar filter value to match either a
    # scalar metadata field OR a list-valued one without knowing the shape,
    # matching the behavior of kb_search.  If this diverges from search.py,
    # both should be updated together.
    if metadata_filter:
        for path, value in metadata_filter.items():
            if path.count(".") != 1:
                raise ValueError(
                    f"metadata_filter keys must be exactly 'namespace.key' (one dot, two segments); "
                    f"got {path!r}. Nested paths are not supported in v1."
                )
            ns, _, key = path.partition(".")
            if not ns or not key:
                raise ValueError(f"metadata_filter keys must have a non-empty namespace and key, got {path!r}")
            containment = Document.doc_metadata.op("@>")(func.cast({ns: {key: value}}, JSONB))
            if isinstance(value, str):
                existence = Document.doc_metadata[ns][key].op("?")(value)
                stmt = stmt.where(or_(containment, existence))
            else:
                stmt = stmt.where(containment)

    # Optional after / before bounds on the effective date.
    if after:
        stmt = stmt.where(expr >= _parse_iso_date(after))
    if before:
        stmt = stmt.where(expr <= _parse_iso_date(before))

    # Sort: NULLs last for both ascending and descending.
    if direction == "earliest":
        stmt = stmt.order_by(expr.asc().nullslast())
    else:
        stmt = stmt.order_by(expr.desc().nullslast())

    stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return [(row.Document, row.effective_date, row.date_source) for row in result.all()]


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
