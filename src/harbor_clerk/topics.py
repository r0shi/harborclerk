import asyncio
import logging
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import Text, delete, func, select, update

from harbor_clerk.db import async_session_factory
from harbor_clerk.models.chunk import Chunk
from harbor_clerk.models.corpus_topic import CorpusTopic, CorpusTopicsMeta
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion

logger = logging.getLogger(__name__)

MIN_DOCS_FOR_TOPICS = 10
STALENESS_MINUTES = 15


def _compute_topics(
    titles: list[str],
    summaries: list[str],
    embeddings: np.ndarray,
) -> list[dict]:
    """Run BERTopic clustering on pre-computed document embeddings (sync, CPU-bound).

    Returns list of dicts: {topic_id, label, keywords, doc_ids, representative_doc_ids}.
    doc_ids is a list of integer indices into the input arrays.
    """
    from bertopic import BERTopic
    from hdbscan import HDBSCAN
    from umap import UMAP

    umap_model = UMAP(
        n_components=min(15, len(embeddings) - 2),
        n_neighbors=min(15, len(embeddings) - 1),
        min_dist=0.0,
        random_state=42,
    )
    hdbscan_model = HDBSCAN(min_cluster_size=5, min_samples=3)

    # Use summaries where available, fall back to titles
    docs = [s if s else t for s, t in zip(summaries, titles)]

    topic_model = BERTopic(
        umap_model=umap_model,
        hdbscan_model=hdbscan_model,
        verbose=False,
    )
    topics, _probs = topic_model.fit_transform(docs, embeddings=embeddings)

    topic_info = topic_model.get_topic_info()
    results = []

    for _, row in topic_info.iterrows():
        tid = row["Topic"]
        if tid == -1:
            continue  # skip outlier topic

        # Get top keywords via c-TF-IDF
        topic_words = topic_model.get_topic(tid)
        keywords = [w for w, _ in topic_words[:10]]
        label = " & ".join(keywords[:3])

        # Find document indices assigned to this topic
        doc_indices = [i for i, t in enumerate(topics) if t == tid]

        # Representative docs: up to 3
        representative_indices = doc_indices[:3]

        results.append(
            {
                "topic_id": tid,
                "label": label,
                "keywords": keywords,
                "doc_indices": doc_indices,
                "representative_indices": representative_indices,
            }
        )

    return results


async def recompute_topics() -> None:
    """Fetch document centroids, run BERTopic, store results."""
    async with async_session_factory() as session:
        # Fetch document centroids with titles and summaries
        rows = (
            await session.execute(
                select(
                    Chunk.doc_id,
                    Document.title,
                    DocumentVersion.summary,
                    func.avg(Chunk.embedding).cast(Text).label("centroid"),
                )
                .join(Document, Document.doc_id == Chunk.doc_id)
                .join(DocumentVersion, Document.latest_version_id == DocumentVersion.version_id)
                .where(Document.status == "active", Chunk.embedding.isnot(None))
                .group_by(Chunk.doc_id, Document.title, DocumentVersion.summary)
            )
        ).all()

        if len(rows) < MIN_DOCS_FOR_TOPICS:
            logger.info("Skipping topic computation: only %d documents (need %d)", len(rows), MIN_DOCS_FOR_TOPICS)
            return

        # Filter out rows with null centroids
        rows = [r for r in rows if r.centroid is not None]
        if len(rows) < MIN_DOCS_FOR_TOPICS:
            logger.info("Skipping topic computation: only %d documents with embeddings", len(rows))
            return

        doc_ids = [r.doc_id for r in rows]
        titles = [r.title for r in rows]
        summaries = [r.summary or "" for r in rows]
        centroids = np.array([[float(x) for x in r.centroid.strip("[]").split(",")] for r in rows])

        # Run BERTopic in thread executor (CPU-bound)
        loop = asyncio.get_running_loop()
        try:
            topic_results = await loop.run_in_executor(None, _compute_topics, titles, summaries, centroids)
        except Exception:
            logger.exception("BERTopic computation failed")
            return

        if not topic_results:
            logger.info("BERTopic produced no topics")
            return

        now = datetime.now(UTC)

        # Clear old topics and insert new ones
        await session.execute(delete(CorpusTopic))

        # Reset all document topic assignments
        await session.execute(update(Document).values(topic_id=None))

        for t in topic_results:
            rep_doc_ids = [doc_ids[i] for i in t["representative_indices"]]
            topic = CorpusTopic(
                topic_id=t["topic_id"],
                label=t["label"],
                keywords=t["keywords"],
                doc_count=len(t["doc_indices"]),
                representative_doc_ids=rep_doc_ids,
                updated_at=now,
            )
            session.add(topic)

            # Update documents with their topic assignment
            assigned_doc_ids = [doc_ids[i] for i in t["doc_indices"]]
            if assigned_doc_ids:
                await session.execute(
                    update(Document).where(Document.doc_id.in_(assigned_doc_ids)).values(topic_id=t["topic_id"])
                )

        # Upsert corpus_topics_meta
        meta = (await session.execute(select(CorpusTopicsMeta).where(CorpusTopicsMeta.id == 1))).scalar_one_or_none()
        doc_count = len(doc_ids)
        # Compute hash from doc count and latest version timestamp
        max_ts_row = (
            await session.execute(
                select(func.max(DocumentVersion.created_at).cast(Text)).where(
                    DocumentVersion.doc_id.in_(doc_ids),
                )
            )
        ).scalar_one_or_none()
        corpus_hash = f"{doc_count}:{max_ts_row or ''}"

        if meta:
            meta.last_computed_at = now
            meta.corpus_hash = corpus_hash
        else:
            session.add(CorpusTopicsMeta(id=1, last_computed_at=now, corpus_hash=corpus_hash))

        await session.commit()
        logger.info("Topic computation complete: %d topics from %d documents", len(topic_results), doc_count)


async def get_topics_for_tool() -> str:
    """Return topic data formatted for tool call response."""
    import json

    async with async_session_factory() as session:
        rows = (await session.execute(select(CorpusTopic).order_by(CorpusTopic.doc_count.desc()))).scalars().all()

    if not rows:
        return json.dumps(
            {"message": "No topics computed yet. The corpus may be too small or topics haven't been generated."}
        )

    topics = [
        {
            "topic": r.label,
            "keywords": r.keywords,
            "doc_count": r.doc_count,
        }
        for r in rows
    ]
    return json.dumps({"topics": topics, "total_topics": len(topics)}, indent=2)


async def get_topic_summary() -> str | None:
    """Return a compact one-liner summarising corpus topics, or None if not computed."""
    async with async_session_factory() as session:
        meta = (await session.execute(select(CorpusTopicsMeta).where(CorpusTopicsMeta.id == 1))).scalar_one_or_none()
        if not meta or not meta.last_computed_at:
            return None

        topics = (await session.execute(select(CorpusTopic).order_by(CorpusTopic.doc_count.desc()))).scalars().all()

        if not topics:
            return None

        labels = [t.label for t in topics]
        return f"The corpus covers {len(labels)} topics: {', '.join(labels)}"


async def check_and_recompute_topics(session) -> None:
    """Check if topics are stale and recompute if needed.

    Uses the provided session for reading staleness info, but recompute_topics()
    creates its own session for the heavy work.
    """
    try:
        # Count active documents with embeddings
        doc_count_result = await session.execute(
            select(func.count(func.distinct(Chunk.doc_id)))
            .join(Document, Document.doc_id == Chunk.doc_id)
            .where(Document.status == "active", Chunk.embedding.isnot(None))
        )
        doc_count = doc_count_result.scalar_one()

        if doc_count < MIN_DOCS_FOR_TOPICS:
            return

        # Get latest version timestamp as staleness indicator
        latest_ts = await session.execute(
            select(func.max(DocumentVersion.created_at).cast(Text))
            .join(Document, Document.latest_version_id == DocumentVersion.version_id)
            .where(Document.status == "active")
        )
        latest_str = latest_ts.scalar_one_or_none() or ""
        current_hash = f"{doc_count}:{latest_str}"

        # Check stored meta
        meta = (await session.execute(select(CorpusTopicsMeta).where(CorpusTopicsMeta.id == 1))).scalar_one_or_none()

        if meta and meta.corpus_hash == current_hash:
            return  # corpus unchanged

        now = datetime.now(UTC)
        if meta and meta.last_computed_at and (now - meta.last_computed_at) < timedelta(minutes=STALENESS_MINUTES):
            return  # computed recently, wait

        logger.info(
            "Corpus topics stale (hash %s != %s), triggering recompute",
            meta.corpus_hash if meta else None,
            current_hash,
        )
        await recompute_topics()
    except Exception:
        logger.exception("Topic staleness check failed")
