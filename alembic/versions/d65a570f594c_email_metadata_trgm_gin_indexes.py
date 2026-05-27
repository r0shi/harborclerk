"""email metadata trgm gin indexes

Adds documents.email_subject (TEXT, nullable) and two GIN trigram indexes
on documents.email_subject and documents.email_from_name to support fast
ILIKE filtering used by the new email.subject_contains and
email.from_name_contains metadata_filter keys (see hybrid_search).
Without these, ILIKE '%v%' on these columns forces a seq-scan on the
documents table at corpus scale.

The pg_trgm extension is already enabled (see docker/postgres/init-
extensions.sql for Docker; same for macOS bundle).

CREATE INDEX CONCURRENTLY cannot run inside a transaction; wrap in
autocommit_block (pattern established by PR #411). The column ADD is
transactional (standard alembic DDL) and must run before the CONCURRENTLY
index creates, so it is placed in a separate block.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d65a570f594c"
down_revision = "b56bde44ec8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the email_subject column (transactional DDL — runs in alembic's
    # default transaction). ADD COLUMN IF NOT EXISTS makes the migration
    # replay-safe in environments where the column was added out-of-band.
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_subject TEXT")

    # Create trgm GIN indexes CONCURRENTLY (cannot run inside a transaction).
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_email_subject_trgm "
            "ON public.documents USING gin (email_subject public.gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_email_from_name_trgm "
            "ON public.documents USING gin (email_from_name public.gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_documents_email_subject_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.ix_documents_email_from_name_trgm")

    op.drop_column("documents", "email_subject")
