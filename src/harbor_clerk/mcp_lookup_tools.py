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

from sqlalchemy import case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import TIMESTAMP
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

    # Python-side pass for identifier-like metadata keys. Fetch all active
    # docs whose metadata is non-empty (cheap with the existing GIN index)
    # and walk their JSON structure.
    stmt_meta = select(Document).where(Document.status == "active").where(Document.doc_metadata != {})
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


def _date_components():
    """Return (label, expr) tuples per priority slot.

    Expressions are built fresh per call — SQLAlchemy column elements are
    immutable but we avoid any cross-call binding state by constructing here.
    The priority order is: tika.created_at → frontmatter.date → sidecar.date → ingest.
    """
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
        # For JSONB-cast slots we check the raw JSONB path (before cast) because
        # the cast itself may produce NULL on malformed text and we can't compare
        # a cast expression to NULL with isnot() in a CASE WHEN in a meaningful way.
        # Checking the raw astext isnot(None) is equivalent to the JSONB key existing.
        tika_raw = Document.doc_metadata["tika"]["created_at"].astext
        fm_raw = Document.doc_metadata["frontmatter"]["date"].astext
        sc_raw = Document.doc_metadata["sidecar"]["date"].astext

        expr = func.coalesce(
            cast(tika_raw, TIMESTAMP(timezone=True)),
            cast(fm_raw, TIMESTAMP(timezone=True)),
            cast(sc_raw, TIMESTAMP(timezone=True)),
            Document.created_at,
        )
        source_label_expr = case(
            (tika_raw.isnot(None), literal("tika.created_at")),
            (fm_raw.isnot(None), literal("frontmatter.date")),
            (sc_raw.isnot(None), literal("sidecar.date")),
            else_=literal("ingest"),
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

    # Optional metadata_filter — same validation as hybrid_search in search.py.
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
            if isinstance(value, str):
                stmt = stmt.where(Document.doc_metadata[ns][key].astext == value)
            else:
                from sqlalchemy.dialects.postgresql import JSONB

                stmt = stmt.where(Document.doc_metadata[ns][key].cast(JSONB) == cast(literal(str(value)), JSONB))

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
