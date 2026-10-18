"""Docker auto-discovery: every top-level subdir of WATCH_ROOT becomes a watched folder."""

import logging
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.watched import WatchedFolder

logger = logging.getLogger(__name__)


def scan_watch_root(session: Session, watch_root: str) -> None:
    """Reconcile watched_folders rows against the contents of WATCH_ROOT.

    - New subdir → insert row with auto_discovered=True, unless any row already
      has that path: a second row would mean two observers and two documents
      per file.
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

    all_rows = session.query(WatchedFolder).all()
    auto_rows = [row for row in all_rows if row.auto_discovered]
    known = {os.path.realpath(row.path) for row in all_rows}

    # New paths → insert, unless some row (auto-discovered or not) already has the path
    for path in sorted(on_disk):
        if os.path.realpath(path) in known:
            continue
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


_MOUNTINFO = "/proc/self/mountinfo"
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _mount_points(mountinfo_path: str) -> set[str] | None:
    """Mount points from a mountinfo file (field 5, octal-escaped), or None when it can't be read."""
    try:
        lines = Path(mountinfo_path).read_text().splitlines()
    except OSError:
        return None
    points = set()
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            points.add(_OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def watch_root_problem(watch_root: str, mountinfo_path: str = _MOUNTINFO) -> str | None:
    """Why WATCH_ROOT cannot work, or None.

    Under Compose, ./data/watch is mounted at /data/watch in every service that
    reads watched files, and WATCH_ROOT must be that directory or one under it.
    The check is generic rather than tied to /data/watch: the root must exist,
    and (where /proc/self/mountinfo can be read) it or an ancestor other than /
    must be a mount point. A root on the container's own filesystem is invisible
    to the workers and lost on recreate. Empty WATCH_ROOT (macOS native) is fine.
    """
    if not watch_root:
        return None
    root = Path(watch_root)
    if not root.is_absolute():
        return f"WATCH_ROOT={watch_root} is not an absolute path"
    if not root.is_dir():
        return (
            f"WATCH_ROOT={watch_root} does not exist or is not a directory. It must be the watch "
            "mount (/data/watch in docker-compose.yml) or a directory under it that exists on the host."
        )
    points = _mount_points(mountinfo_path)
    if points is None:
        return None
    real = Path(os.path.realpath(root))
    if not any(str(p) in points for p in (real, *real.parents) if str(p) != "/"):
        return (
            f"WATCH_ROOT={watch_root} is not on a mounted volume. It must be the watch mount "
            "(/data/watch in docker-compose.yml) or a directory under it; files anywhere else are "
            "not visible to the workers that read them."
        )
    return None


def log_watch_root_problem(watch_root: str, service: str) -> str | None:
    """Log watch_root_problem() at ERROR for a service's startup; returns the problem."""
    problem = watch_root_problem(watch_root)
    if problem:
        logger.error("%s: %s Watched folders will not be discovered until this is fixed.", service, problem)
    return problem
