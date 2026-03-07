"""Corpus and per-document statistics endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, case, cast, extract, func, select, text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_read_access
from harbor_clerk.db import get_session
from harbor_clerk.models import (
    Chunk,
    Document,
    DocumentPage,
    DocumentVersion,
    Entity,
    IngestionJob,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stats"])

_DEFAULT_EXCLUDED_ENTITY_TYPES = {"CARDINAL", "ORDINAL", "QUANTITY"}


def _parse_exclude_types(exclude_types: str | None) -> set[str]:
    """Parse comma-separated exclude_types query param into a set."""
    if exclude_types is None:
        return _DEFAULT_EXCLUDED_ENTITY_TYPES
    if exclude_types == "":
        return set()
    return {t.strip() for t in exclude_types.split(",")}


def _latest_version_filter():
    """Return a join condition ensuring we only look at chunks/entities for the latest version."""
    return Document.latest_version_id == DocumentVersion.version_id


@router.get("/stats")
async def corpus_stats(
    exclude_types: str | None = Query(
        default=None,
        description="Comma-separated entity types to exclude (default: CARDINAL,ORDINAL,QUANTITY). Pass empty string to include all.",
    ),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return aggregate corpus-level statistics."""
    active = Document.status == "active"
    excluded = _parse_exclude_types(exclude_types)

    # Document count
    doc_count = (await session.execute(select(func.count()).select_from(Document).where(active))).scalar() or 0

    # Total chunks (scoped to latest versions of active docs)
    total_chunks = (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Chunk.doc_id == Document.doc_id)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.version_id)
            .where(active, _latest_version_filter())
        )
    ).scalar() or 0

    # Total pages
    total_pages = (
        await session.execute(
            select(func.count())
            .select_from(DocumentPage)
            .join(DocumentVersion, DocumentPage.version_id == DocumentVersion.version_id)
            .join(Document, DocumentVersion.doc_id == Document.doc_id)
            .where(active, _latest_version_filter())
        )
    ).scalar() or 0

    # Language distribution
    lang_rows = (
        await session.execute(
            select(Chunk.language, func.count())
            .join(Document, Chunk.doc_id == Document.doc_id)
            .join(DocumentVersion, Chunk.version_id == DocumentVersion.version_id)
            .where(active, _latest_version_filter())
            .group_by(Chunk.language)
        )
    ).all()
    languages = {row[0]: row[1] for row in lang_rows}

    # MIME type distribution
    mime_rows = (
        await session.execute(
            select(DocumentVersion.mime_type, func.count())
            .join(Document, DocumentVersion.doc_id == Document.doc_id)
            .where(active, _latest_version_filter())
            .group_by(DocumentVersion.mime_type)
        )
    ).all()
    mime_types = {(row[0] or "unknown"): row[1] for row in mime_rows}

    # OCR breakdown
    ocr_rows = (
        await session.execute(
            select(DocumentVersion.needs_ocr, func.count())
            .join(Document, DocumentVersion.doc_id == Document.doc_id)
            .where(active, _latest_version_filter())
            .group_by(DocumentVersion.needs_ocr)
        )
    ).all()
    ocr_breakdown = {"born_digital": 0, "ocr_used": 0, "unknown": 0}
    for needs_ocr, count in ocr_rows:
        if needs_ocr is True:
            ocr_breakdown["ocr_used"] += count
        elif needs_ocr is False:
            ocr_breakdown["born_digital"] += count
        else:
            ocr_breakdown["unknown"] += count

    # Size buckets
    buckets = [
        ("0-100KB", 0, 100 * 1024),
        ("100KB-1MB", 100 * 1024, 1024 * 1024),
        ("1-10MB", 1024 * 1024, 10 * 1024 * 1024),
        ("10-50MB", 10 * 1024 * 1024, 50 * 1024 * 1024),
        ("50-100MB", 50 * 1024 * 1024, 100 * 1024 * 1024),
        (">100MB", 100 * 1024 * 1024, None),
    ]
    size_cases = []
    for label, lo, hi in buckets:
        if hi is not None:
            size_cases.append(
                (
                    (DocumentVersion.size_bytes >= lo) & (DocumentVersion.size_bytes < hi),
                    label,
                )
            )
        else:
            size_cases.append(((DocumentVersion.size_bytes >= lo), label))

    size_rows = (
        await session.execute(
            select(
                case(*size_cases, else_="unknown").label("bucket"),
                func.count(),
            )
            .select_from(DocumentVersion)
            .join(Document, DocumentVersion.doc_id == Document.doc_id)
            .where(active, _latest_version_filter(), DocumentVersion.size_bytes.isnot(None))
            .group_by("bucket")
        )
    ).all()
    size_map = {row[0]: row[1] for row in size_rows}
    size_buckets = [{"label": label, "count": size_map.get(label, 0)} for label, _, _ in buckets]

    # Growth timeline (monthly)
    growth_rows = (
        await session.execute(
            select(
                func.to_char(func.date_trunc("month", Document.created_at), "YYYY-MM").label("month"),
                func.count(),
            )
            .where(active)
            .group_by("month")
            .order_by("month")
        )
    ).all()
    growth_timeline = [{"month": row[0], "count": row[1]} for row in growth_rows]

    # Pipeline timing from completed jobs
    timing_rows = (
        await session.execute(
            select(
                IngestionJob.stage,
                func.avg(extract("epoch", IngestionJob.finished_at) - extract("epoch", IngestionJob.started_at)).label(
                    "avg_secs"
                ),
                func.count(),
            )
            .where(
                IngestionJob.status == "done", IngestionJob.started_at.isnot(None), IngestionJob.finished_at.isnot(None)
            )
            .group_by(IngestionJob.stage)
        )
    ).all()
    pipeline_timing = {}
    for stage, avg_secs, count in timing_rows:
        stage_name = stage.value if hasattr(stage, "value") else str(stage)
        pipeline_timing[stage_name] = {"avg_secs": round(float(avg_secs), 2), "count": count}

    # Entity type counts
    entity_type_q = (
        select(Entity.entity_type, func.count())
        .join(Document, Entity.doc_id == Document.doc_id)
        .join(DocumentVersion, Entity.version_id == DocumentVersion.version_id)
        .where(active, _latest_version_filter())
    )
    if excluded:
        entity_type_q = entity_type_q.where(Entity.entity_type.notin_(excluded))
    entity_type_rows = (await session.execute(entity_type_q.group_by(Entity.entity_type))).all()
    entity_type_counts = {row[0]: row[1] for row in entity_type_rows}

    # Top 20 entities
    top_entity_q = (
        select(Entity.entity_text, Entity.entity_type, func.count().label("mentions"))
        .join(Document, Entity.doc_id == Document.doc_id)
        .join(DocumentVersion, Entity.version_id == DocumentVersion.version_id)
        .where(active, _latest_version_filter())
    )
    if excluded:
        top_entity_q = top_entity_q.where(Entity.entity_type.notin_(excluded))
    top_entity_rows = (
        await session.execute(
            top_entity_q.group_by(Entity.entity_text, Entity.entity_type).order_by(func.count().desc()).limit(20)
        )
    ).all()
    top_entities = [{"text": row[0], "type": row[1], "mentions": row[2]} for row in top_entity_rows]

    return {
        "document_count": doc_count,
        "total_chunks": total_chunks,
        "total_pages": total_pages,
        "languages": languages,
        "mime_types": mime_types,
        "ocr_breakdown": ocr_breakdown,
        "size_buckets": size_buckets,
        "growth_timeline": growth_timeline,
        "pipeline_timing": pipeline_timing,
        "entity_type_counts": entity_type_counts,
        "top_entities": top_entities,
    }


@router.get("/stats/clusters")
async def document_clusters(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return document centroid embeddings for client-side UMAP clustering."""
    rows = (
        await session.execute(
            text("""
                SELECT
                    c.doc_id,
                    d.title,
                    dv.mime_type,
                    avg(c.embedding)::text AS centroid
                FROM chunks c
                JOIN documents d ON c.doc_id = d.doc_id
                JOIN document_versions dv ON c.version_id = dv.version_id
                WHERE d.status = 'active'
                  AND d.latest_version_id = dv.version_id
                  AND c.embedding IS NOT NULL
                GROUP BY c.doc_id, d.title, dv.mime_type
            """)
        )
    ).all()

    documents = []
    for row in rows:
        # pgvector avg() returns a string like '[0.1,0.2,...]'
        centroid_str = row[3]
        if centroid_str:
            centroid = [float(x) for x in centroid_str.strip("[]").split(",")]
        else:
            continue
        documents.append(
            {
                "doc_id": str(row[0]),
                "title": row[1],
                "mime_type": row[2] or "unknown",
                "centroid": centroid,
            }
        )

    return {"documents": documents}


@router.get("/stats/entity-network")
async def entity_network(
    limit: int = Query(default=50, ge=10, le=100, description="Number of top entities"),
    exclude_types: str | None = Query(
        default=None,
        description="Comma-separated entity types to exclude (default: CARDINAL,ORDINAL,QUANTITY). Pass empty string to include all.",
    ),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return force-graph-ready entity co-occurrence network."""
    excluded = _parse_exclude_types(exclude_types)

    # Step 1: Get top N entities by mention count (scoped to active docs)
    top_q = (
        select(
            Entity.entity_text,
            Entity.entity_type,
            func.count().label("mentions"),
        )
        .join(Document, Entity.doc_id == Document.doc_id)
        .join(DocumentVersion, Entity.version_id == DocumentVersion.version_id)
        .where(Document.status == "active", _latest_version_filter())
    )
    if excluded:
        top_q = top_q.where(Entity.entity_type.notin_(excluded))
    top_rows = (
        await session.execute(
            top_q.group_by(Entity.entity_text, Entity.entity_type).order_by(func.count().desc()).limit(limit)
        )
    ).all()

    if not top_rows:
        return {"nodes": [], "edges": []}

    nodes = []
    entity_keys = set()
    for text_val, type_val, mentions in top_rows:
        key = f"{type_val}:{text_val}"
        entity_keys.add(key)
        nodes.append({"id": key, "text": text_val, "type": type_val, "mentions": mentions})

    # Step 2: Find co-occurrences (entities in the same chunk)
    top_texts = [row[0] for row in top_rows]

    e1 = Entity.__table__.alias("e1")
    e2 = Entity.__table__.alias("e2")

    cooccurrence_rows = (
        await session.execute(
            select(
                e1.c.entity_type,
                e1.c.entity_text,
                e2.c.entity_type,
                e2.c.entity_text,
                func.count().label("weight"),
            )
            .select_from(e1)
            .join(e2, e1.c.chunk_id == e2.c.chunk_id)
            .join(Document, e1.c.doc_id == Document.doc_id)
            .join(DocumentVersion, e1.c.version_id == DocumentVersion.version_id)
            .where(
                Document.status == "active",
                Document.latest_version_id == DocumentVersion.version_id,
                e1.c.entity_text.in_(top_texts),
                e2.c.entity_text.in_(top_texts),
                e1.c.entity_id < e2.c.entity_id,  # avoid duplicates
            )
            .group_by(e1.c.entity_type, e1.c.entity_text, e2.c.entity_type, e2.c.entity_text)
        )
    ).all()

    edges = []
    for t1, txt1, t2, txt2, weight in cooccurrence_rows:
        source = f"{t1}:{txt1}"
        target = f"{t2}:{txt2}"
        if source in entity_keys and target in entity_keys:
            edges.append({"source": source, "target": target, "weight": weight})

    return {"nodes": nodes, "edges": edges}


def _kmeans(data: np.ndarray, k: int, max_iter: int = 50) -> list[int]:
    """Simple k-means clustering using numpy. Returns list of cluster labels."""
    import numpy as np

    n = len(data)
    rng = np.random.default_rng(42)
    indices = [rng.integers(n)]
    for _ in range(1, k):
        dists = np.min([np.sum((data - data[i]) ** 2, axis=1) for i in indices], axis=0)
        total = dists.sum()
        if total == 0:
            probs = np.ones(n) / n
        else:
            probs = dists / total
        indices.append(rng.choice(n, p=probs))
    centroids = data[indices].copy()

    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        dists = np.stack([np.sum((data - c) ** 2, axis=1) for c in centroids], axis=1)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = data[mask].mean(axis=0)

    return labels.tolist()


@router.get("/stats/topics")
async def topic_clusters(
    k: int = Query(default=0, ge=0, le=30, description="Number of clusters (0=auto)"),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Cluster documents by embedding similarity and auto-name each cluster."""
    from collections import Counter

    import numpy as np

    # Fetch centroids (same query as /stats/clusters but with doc_type)
    rows = (
        await session.execute(
            select(
                Chunk.doc_id,
                Document.title,
                DocumentVersion.mime_type,
                DocumentVersion.doc_type,
                func.avg(Chunk.embedding).cast(Text).label("centroid"),
            )
            .join(Document, Document.doc_id == Chunk.doc_id)
            .join(DocumentVersion, Document.latest_version_id == DocumentVersion.version_id)
            .where(Document.status == "active", Chunk.embedding.isnot(None))
            .group_by(Chunk.doc_id, Document.title, DocumentVersion.mime_type, DocumentVersion.doc_type)
        )
    ).all()

    if len(rows) < 3:
        return {"clusters": [], "doc_count": len(rows)}

    rows = [r for r in rows if r[4] is not None]
    if len(rows) < 3:
        return {"clusters": [], "doc_count": len(rows)}

    doc_ids = [str(r[0]) for r in rows]
    titles = [r[1] for r in rows]
    doc_types = [r[3] for r in rows]
    centroids = np.array([[float(x) for x in r[4].strip("[]").split(",")] for r in rows])

    # Auto-select k if not specified: sqrt(n) clamped to 3-15
    n = len(rows)
    if k == 0:
        k = max(3, min(15, int(n**0.5)))
    k = min(k, n)

    # Simple k-means (numpy only, no sklearn dependency)
    labels = _kmeans(centroids, k, max_iter=50)

    # Build clusters
    clusters: dict[int, dict] = {}
    for i, label in enumerate(labels):
        if label not in clusters:
            clusters[label] = {"doc_ids": [], "titles": [], "doc_types": []}
        clusters[label]["doc_ids"].append(doc_ids[i])
        clusters[label]["titles"].append(titles[i])
        clusters[label]["doc_types"].append(doc_types[i])

    # Name each cluster from most common doc_type, or most common title words
    result = []
    for label, info in sorted(clusters.items()):
        type_counts = Counter(t for t in info["doc_types"] if t)
        if type_counts:
            name = type_counts.most_common(1)[0][0]
        else:
            words = []
            for t in info["titles"]:
                words.extend(w for w in t.lower().split() if len(w) > 3)
            word_counts = Counter(words)
            name = word_counts.most_common(1)[0][0].title() if word_counts else f"Group {label + 1}"

        result.append(
            {
                "cluster_id": label,
                "name": name,
                "doc_count": len(info["doc_ids"]),
                "doc_ids": info["doc_ids"],
                "sample_titles": info["titles"][:5],
            }
        )

    result.sort(key=lambda c: c["doc_count"], reverse=True)
    return {"clusters": result, "doc_count": n}


@router.get("/stats/timeline")
async def document_timeline(
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Document count by month for timeline visualization."""
    rows = (
        await session.execute(
            select(
                func.to_char(func.date_trunc("month", Document.created_at), "YYYY-MM").label("month"),
                func.count().label("count"),
            )
            .where(Document.status == "active")
            .group_by(func.date_trunc("month", Document.created_at))
            .order_by(func.date_trunc("month", Document.created_at))
        )
    ).all()
    return [{"month": r[0], "count": r[1]} for r in rows]


@router.get("/docs/{doc_id}/stats")
async def document_stats(
    doc_id: uuid.UUID,
    exclude_types: str | None = Query(
        default=None,
        description="Comma-separated entity types to exclude (default: CARDINAL,ORDINAL,QUANTITY). Pass empty string to include all.",
    ),
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Return per-document statistics."""
    excluded = _parse_exclude_types(exclude_types)
    # Verify doc exists and is active
    doc = (
        await session.execute(select(Document).where(Document.doc_id == doc_id, Document.status == "active"))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    version_id = doc.latest_version_id
    if version_id is None:
        return {
            "chunk_count": 0,
            "page_count": 0,
            "languages": {},
            "entity_types": {},
            "top_entities": [],
            "ocr_confidence": None,
        }

    # Chunk count
    chunk_count = (
        await session.execute(select(func.count()).select_from(Chunk).where(Chunk.version_id == version_id))
    ).scalar() or 0

    # Page count
    page_count = (
        await session.execute(
            select(func.count()).select_from(DocumentPage).where(DocumentPage.version_id == version_id)
        )
    ).scalar() or 0

    # Language distribution
    lang_rows = (
        await session.execute(
            select(Chunk.language, func.count()).where(Chunk.version_id == version_id).group_by(Chunk.language)
        )
    ).all()
    languages = {row[0]: row[1] for row in lang_rows}

    # Entity type counts
    etype_q = select(Entity.entity_type, func.count()).where(Entity.version_id == version_id)
    if excluded:
        etype_q = etype_q.where(Entity.entity_type.notin_(excluded))
    etype_rows = (await session.execute(etype_q.group_by(Entity.entity_type))).all()
    entity_types = {row[0]: row[1] for row in etype_rows}

    # Top 10 entities
    top_q = select(Entity.entity_text, Entity.entity_type, func.count().label("mentions")).where(
        Entity.version_id == version_id
    )
    if excluded:
        top_q = top_q.where(Entity.entity_type.notin_(excluded))
    top_rows = (
        await session.execute(
            top_q.group_by(Entity.entity_text, Entity.entity_type).order_by(func.count().desc()).limit(10)
        )
    ).all()
    top_entities = [{"text": row[0], "type": row[1], "mentions": row[2]} for row in top_rows]

    # OCR confidence
    ocr_row = (
        await session.execute(
            select(
                func.avg(cast(DocumentPage.ocr_confidence, DOUBLE_PRECISION)),
                func.min(cast(DocumentPage.ocr_confidence, DOUBLE_PRECISION)),
                func.max(cast(DocumentPage.ocr_confidence, DOUBLE_PRECISION)),
            ).where(DocumentPage.version_id == version_id, DocumentPage.ocr_used.is_(True))
        )
    ).one()
    ocr_confidence = None
    if ocr_row[0] is not None:
        ocr_confidence = {
            "avg": round(float(ocr_row[0]), 3),
            "min": round(float(ocr_row[1]), 3),
            "max": round(float(ocr_row[2]), 3),
        }

    return {
        "chunk_count": chunk_count,
        "page_count": page_count,
        "languages": languages,
        "entity_types": entity_types,
        "top_entities": top_entities,
        "ocr_confidence": ocr_confidence,
    }
