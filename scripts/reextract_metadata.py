#!/usr/bin/env python3
"""One-shot script: re-extract metadata for all active documents in the
current HC instance. Idempotent — running twice produces the same metadata.

Use this after applying the documents.metadata migration to backfill
metadata for documents that were ingested before the extractor framework
existed.

Doesn't change body text or chunks — only writes to Document.doc_metadata.
For documents in source-path-backed watched folders, reads raw bytes from
disk. For legacy uploads with stored objects, fetches from storage.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from harbor_clerk.db import async_session_factory
from harbor_clerk.ingest.metadata_extractors import run_all as run_metadata_extractors
from harbor_clerk.models.document import Document
from harbor_clerk.storage import get_storage

log = logging.getLogger("reextract_metadata")


async def reextract_all(*, dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (processed, updated, skipped)."""
    storage = get_storage()

    processed = updated = skipped = 0
    async with async_session_factory() as session:
        result = await session.execute(select(Document).where(Document.status == "active"))
        docs = result.scalars().all()

        for doc in docs:
            processed += 1
            # Load raw bytes from source_path (watched folder) or storage (legacy upload)
            raw_bytes: bytes | None = None
            if doc.source_path and Path(doc.source_path).is_file():
                raw_bytes = Path(doc.source_path).read_bytes()
            elif doc.original_bucket and doc.original_object_key:
                try:
                    response = storage.get_object(doc.original_bucket, doc.original_object_key)
                    raw_bytes = response.read()
                except Exception as exc:
                    log.warning("storage fetch failed for doc %s: %s", doc.doc_id, exc)
                    skipped += 1
                    continue
            else:
                log.info("doc %s has no source_path or storage object; skipping", doc.doc_id)
                skipped += 1
                continue

            metadata = run_metadata_extractors(
                doc=doc,
                raw_bytes=raw_bytes,
                source_path=doc.source_path,
            )
            if metadata == doc.doc_metadata:
                continue  # idempotent no-op
            if dry_run:
                log.info("would update doc %s: %s", doc.doc_id, metadata)
                updated += 1
                continue
            doc.doc_metadata = metadata
            updated += 1

        if not dry_run:
            await session.commit()

    return processed, updated, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="don't commit changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    processed, updated, skipped = asyncio.run(reextract_all(dry_run=args.dry_run))
    log.info(
        "reextract_metadata complete: processed=%d updated=%d skipped=%d (dry_run=%s)",
        processed,
        updated,
        skipped,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
