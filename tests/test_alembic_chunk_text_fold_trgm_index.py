"""The chunks.chunk_text trigram index is on the folded text (0011, #828; 0008, #819, before it)."""

from sqlalchemy import text


async def test_chunks_chunk_text_fold_trgm_index_present(db_session):
    """ix_chunks_chunk_text_fold_trgm exists on the chunks table."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='chunks' "
            "AND indexname='ix_chunks_chunk_text_fold_trgm'"
        )
    )
    assert result.first() is not None, "ix_chunks_chunk_text_fold_trgm not found"


async def test_chunks_chunk_text_fold_trgm_index_is_gin_trgm(db_session):
    """Index uses gin + gin_trgm_ops on hc_fold_search_text(chunk_text), so the folded ILIKE is index-eligible."""
    result = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_chunks_chunk_text_fold_trgm'")
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
    assert "hc_fold_search_text(chunk_text)" in indexdef


async def test_earlier_chunk_text_trgm_indexes_dropped(db_session):
    """0008 replaced ix_chunks_chunk_text_trgm, and 0011 ix_chunks_chunk_text_ws_trgm: each had no other reader."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname IN ('ix_chunks_chunk_text_trgm', 'ix_chunks_chunk_text_ws_trgm')"
        )
    )
    assert result.all() == []


async def test_chunks_chunk_text_fold_trgm_index_is_valid(db_session):
    result = await db_session.execute(
        text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'public.ix_chunks_chunk_text_fold_trgm'::regclass")
    )
    assert result.scalar() is True
