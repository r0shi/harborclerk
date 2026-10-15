"""The chunks.chunk_text trigram index is on the whitespace-folded text (0008, #819)."""

from sqlalchemy import text


async def test_chunks_chunk_text_ws_trgm_index_present(db_session):
    """ix_chunks_chunk_text_ws_trgm exists on the chunks table."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='chunks' "
            "AND indexname='ix_chunks_chunk_text_ws_trgm'"
        )
    )
    assert result.first() is not None, "ix_chunks_chunk_text_ws_trgm not found"


async def test_chunks_chunk_text_ws_trgm_index_is_gin_trgm(db_session):
    """Index uses gin + gin_trgm_ops on hc_fold_whitespace(chunk_text), so the folded ILIKE is index-eligible."""
    result = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_chunks_chunk_text_ws_trgm'")
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
    assert "hc_fold_whitespace(chunk_text)" in indexdef


async def test_unfolded_chunk_text_trgm_index_dropped(db_session):
    """0008 replaced ix_chunks_chunk_text_trgm: the unfolded predicate was its only reader."""
    result = await db_session.execute(text("SELECT 1 FROM pg_indexes WHERE indexname='ix_chunks_chunk_text_trgm'"))
    assert result.first() is None


async def test_chunks_chunk_text_ws_trgm_index_is_valid(db_session):
    """indisvalid: an interrupted CREATE INDEX CONCURRENTLY leaves an INVALID index the planner ignores."""
    result = await db_session.execute(
        text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'public.ix_chunks_chunk_text_ws_trgm'::regclass")
    )
    assert result.scalar() is True
