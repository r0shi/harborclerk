"""Schema test: WatchedFolder gains skipped_count + skipped_extensions."""

import pytest
from sqlalchemy import select

from harbor_clerk.models.watched import WatchedFolder


@pytest.fixture
def sync_session(_engine):
    """Per-test sync session with table cleanup (mirrors db_session's async cleanup)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from harbor_clerk.models import Base

    sync_url = str(_engine.url).replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url, echo=False)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        # Cleanup watched tables in FK order (watched_files → watched_folders).
        with engine.begin() as conn:
            for table_name in ("watched_files", "watched_folders"):
                table = Base.metadata.tables.get(table_name)
                if table is not None:
                    conn.execute(table.delete())
        engine.dispose()


def test_watched_folder_skip_columns_defaults(sync_session):
    """A folder inserted without the new fields gets skipped_count=0 and
    skipped_extensions=[] from the column defaults."""
    folder = WatchedFolder(path="/tmp/test-skip-defaults")
    sync_session.add(folder)
    sync_session.commit()

    row = sync_session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder.folder_id)).scalar_one()
    assert row.skipped_count == 0
    assert row.skipped_extensions == []


def test_watched_folder_skip_columns_round_trip(sync_session):
    """The fields can be written and read back as int + list[str]."""
    folder = WatchedFolder(
        path="/tmp/test-skip-roundtrip",
        skipped_count=7,
        skipped_extensions=[".canvas", ".excalidraw", ".xyz"],
    )
    sync_session.add(folder)
    sync_session.commit()

    row = sync_session.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder.folder_id)).scalar_one()
    assert row.skipped_count == 7
    # PG ARRAY(Text) returns a list, not a set — order is preserved as inserted.
    assert row.skipped_extensions == [".canvas", ".excalidraw", ".xyz"]
