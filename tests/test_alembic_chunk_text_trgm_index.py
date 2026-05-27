"""Verify the chunks.chunk_text trgm index exists after migrate-up."""

import pytest
from sqlalchemy import text


async def test_chunks_chunk_text_trgm_index_present(db_session):
    """ix_chunks_chunk_text_trgm exists on the chunks table."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='chunks' "
            "AND indexname='ix_chunks_chunk_text_trgm'"
        )
    )
    row = result.first()
    assert row is not None, "ix_chunks_chunk_text_trgm not found"


async def test_chunks_chunk_text_trgm_index_is_gin_trgm(db_session):
    """Index uses gin + gin_trgm_ops (so ILIKE %x% is index-eligible)."""
    result = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_chunks_chunk_text_trgm'")
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
