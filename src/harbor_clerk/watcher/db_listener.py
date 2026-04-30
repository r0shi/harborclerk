"""LISTEN watched_folders_changed — yields parsed payloads as NOTIFYs arrive."""

import json
import logging
import select as select_mod
import threading
from collections.abc import Iterator

import psycopg2
import psycopg2.extensions

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import _make_sync_url
from harbor_clerk.watcher.notify import CHANNEL

logger = logging.getLogger(__name__)


def _open_listen_connection() -> psycopg2.extensions.connection:
    """Open a standalone psycopg2 connection in autocommit mode for LISTEN."""
    settings = get_settings()
    dsn = _make_sync_url(settings.database_url).replace("postgresql+psycopg2://", "postgresql://")
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def listen_for_folder_changes(
    stop_event: threading.Event,
    poll_interval: float = 1.0,
) -> Iterator[dict]:
    """Yield {folder_id, action} dicts as NOTIFYs arrive on watched_folders_changed.

    Uses a standalone psycopg2 connection (not a SQLAlchemy session) to avoid
    holding a pool slot for the lifetime of the listener.
    """
    conn = _open_listen_connection()
    cursor = conn.cursor()
    cursor.execute(f"LISTEN {CHANNEL};")

    try:
        while not stop_event.is_set():
            if select_mod.select([conn], [], [], poll_interval) == ([], [], []):
                continue
            conn.poll()
            while conn.notifies:
                notif = conn.notifies.pop(0)
                try:
                    yield json.loads(notif.payload)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("watcher: malformed NOTIFY payload: %r", notif.payload)
    finally:
        try:
            cursor.execute(f"UNLISTEN {CHANNEL};")
        except Exception:
            pass
        cursor.close()
        conn.close()
