"""Verify the documents.email_subject + email_from_name trgm indexes exist."""

from sqlalchemy import text


async def test_documents_email_subject_trgm_index_present(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='documents' "
            "AND indexname='ix_documents_email_subject_trgm'"
        )
    )
    assert result.first() is not None, "ix_documents_email_subject_trgm not found"


async def test_documents_email_subject_trgm_index_is_gin_trgm(db_session):
    result = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_documents_email_subject_trgm'")
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef


async def test_documents_email_from_name_trgm_index_present(db_session):
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='documents' "
            "AND indexname='ix_documents_email_from_name_trgm'"
        )
    )
    assert result.first() is not None, "ix_documents_email_from_name_trgm not found"


async def test_documents_email_from_name_trgm_index_is_gin_trgm(db_session):
    result = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_documents_email_from_name_trgm'")
    )
    indexdef = (result.scalar() or "").lower()
    assert "using gin" in indexdef
    assert "gin_trgm_ops" in indexdef
