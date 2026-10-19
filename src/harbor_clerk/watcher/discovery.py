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
        real = os.path.realpath(path)
        if real in known:
            continue
        # Two entries in one scan can resolve to the same directory (`b -> a`).
        known.add(real)
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


def _watch_root_check(watch_root: str, mountinfo_path: str) -> tuple[str, str] | None:
    """(kind, message) for a WATCH_ROOT that cannot work, or None. kind: invalid, missing, unmounted."""
    if not watch_root:
        return None
    root = Path(watch_root)
    if not root.is_absolute():
        return "invalid", f"WATCH_ROOT={watch_root} is not an absolute path."
    if not root.is_dir():
        return "missing", (
            f"WATCH_ROOT={watch_root} does not exist or is not a directory. It must be the watch "
            "mount (/data/watch in docker-compose.yml) or a directory under it that exists on the host."
        )
    points = _mount_points(mountinfo_path)
    if points is None:
        return None
    real = Path(os.path.realpath(root))
    if not any(str(p) in points for p in (real, *real.parents) if str(p) != "/"):
        return "unmounted", (
            f"WATCH_ROOT={watch_root} is not on a mounted volume. It must be the watch mount "
            "(/data/watch in docker-compose.yml) or a directory under it."
        )
    return None


def watch_root_problem(watch_root: str, mountinfo_path: str = _MOUNTINFO) -> str | None:
    """Why WATCH_ROOT cannot work, or None.

    Under Compose, ./data/watch is mounted at /data/watch in every service that
    reads watched files, and WATCH_ROOT must be that directory or one under it.
    The check is generic rather than tied to /data/watch: the root must exist,
    and (where /proc/self/mountinfo can be read) it or an ancestor other than /
    must be a mount point. A root on the container's own filesystem is invisible
    to the workers and lost on recreate. Empty WATCH_ROOT (macOS native) is fine.
    """
    check = _watch_root_check(watch_root, mountinfo_path)
    return check[1] if check else None


# What each problem means for the service that found it.
_CONSEQUENCES = {
    ("watcher", "invalid"): "No watched folders will be discovered until this is fixed.",
    ("watcher", "missing"): "No watched folders will be discovered until this is fixed.",
    ("watcher", "unmounted"): (
        "Folders there are discovered, but their documents will fail to extract: the workers "
        "and the app cannot see files that exist only in the watcher's container."
    ),
    ("api", "invalid"): "Watched documents cannot be downloaded, and the Folders page will list none from it.",
    ("api", "missing"): "Watched documents cannot be downloaded, and the Folders page will list none from it.",
    ("api", "unmounted"): (
        "Watched documents cannot be downloaded from this container, and their folders will show "
        "extraction failures: no other service can see files outside the watch mount."
    ),
}


def log_watch_root_problem(watch_root: str, service: str, mountinfo_path: str = _MOUNTINFO) -> str | None:
    """Log a WATCH_ROOT problem at ERROR for a service's startup, with what it means there; returns it."""
    check = _watch_root_check(watch_root, mountinfo_path)
    if not check:
        return None
    kind, problem = check
    consequence = _CONSEQUENCES.get((service, kind), "Watched folders will not work until this is fixed.")
    logger.error("%s: %s %s", service, problem, consequence)
    return problem
