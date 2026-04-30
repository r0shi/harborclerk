"""Docker auto-discovery: every top-level subdir of WATCH_ROOT becomes a watched folder."""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.watched import WatchedFolder

logger = logging.getLogger(__name__)


def scan_watch_root(session: Session, watch_root: str) -> None:
    """Reconcile watched_folders rows against the contents of WATCH_ROOT.

    - New subdir → insert row with auto_discovered=True.
    - Existing auto-discovered row whose path is gone → enabled=False, unavailable_reason='unmounted'.
    - Existing auto-discovered row whose path reappeared → clear unavailable_reason, re-enable.
    - Manually-added (auto_discovered=False) rows are ignored entirely — they are owned by the API.
    Caller is responsible for commit.
    """
    if not watch_root:
        return

    root = Path(watch_root)
    if not root.is_dir():
        logger.debug("watcher: WATCH_ROOT %s does not exist; skipping scan", watch_root)
        return

    on_disk = {str(p) for p in root.iterdir() if p.is_dir()}

    auto_rows = session.query(WatchedFolder).filter_by(auto_discovered=True).all()
    db_paths = {row.path for row in auto_rows}

    # New paths → insert
    for path in on_disk - db_paths:
        session.add(
            WatchedFolder(
                path=path,
                bookmark_data=None,
                auto_discovered=True,
                display_name=Path(path).name,
            )
        )

    # Reconcile existing rows
    for row in auto_rows:
        if row.path in on_disk:
            if row.unavailable_reason == "unmounted":
                row.unavailable_reason = None
                row.enabled = True
        else:
            if row.unavailable_reason != "unmounted":
                row.unavailable_reason = "unmounted"
                row.enabled = False
