"""The Alembic head this build expects, read from its migrations directory.

Shared by the API's startup check (`api/app.py`, `api/routes/system.py`) and the
watcher's startup wait (#904), which must not import the API application.
The migrations directory ships beside the code in every layout: the repo root
in development, `/app` in the Docker image (`--no-editable`, so the package is
under `/app/.venv/.../site-packages`), and `Contents/Resources` in the macOS
bundle (`macos/scripts/package.sh` copies `alembic/` and `alembic.ini` there,
beside `venv/`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def find_alembic_dir() -> Path | None:
    """Walk parents of this file looking for an `alembic/` directory next to
    an `alembic.ini`. Works in dev (src layout: repo root has both) and in
    the bundled macOS app (bundle resource dir has both). Returns None if
    no candidate is found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic"
        if candidate.is_dir() and (parent / "alembic.ini").exists():
            return candidate
    return None


def _script_directory():
    """Alembic's ScriptDirectory for this build's migrations, or None if alembic or the directory is missing."""
    try:
        from alembic.script import ScriptDirectory
    except ImportError:
        return None

    alembic_dir = find_alembic_dir()
    if alembic_dir is None:
        return None

    try:
        return ScriptDirectory(str(alembic_dir))
    except Exception:
        logger.exception("Found alembic dir at %s but could not read it", alembic_dir)
        return None


def expected_schema_version() -> str | None:
    """Discover the head migration revision via alembic's ScriptDirectory.

    Returns the revision id string (e.g. "0018") or None if alembic isn't
    importable or the migrations directory can't be located.

    This replaces the previous pattern of hardcoding `_EXPECTED_SCHEMA_VERSION`
    inside the lifespan function — that constant had to be bumped on every
    new migration, which was easy to forget and produced misleading
    "schema mismatch" log lines after every release. Reading the head
    dynamically keeps the check in sync automatically.
    """
    script = _script_directory()
    if script is None:
        return None
    try:
        return script.get_current_head()
    except Exception:
        logger.exception("Could not read the alembic head")
        return None


@dataclass(frozen=True)
class SchemaHead:
    """This build's head revision, and every revision on the way to it (the head included)."""

    revision: str
    ancestors: frozenset[str]


def schema_head() -> SchemaHead | None:
    """The head and its ancestors, or None when they cannot be read (see `expected_schema_version`).

    The watcher's alone: the walk over the whole history is never on the API's path, so its failure
    cannot hide the head from the API's schema check or `system.py`'s report.
    """
    script = _script_directory()
    if script is None:
        return None
    try:
        head = script.get_current_head()
        if head is None:
            return None
        ancestors = frozenset(rev.revision for rev in script.iterate_revisions(head, "base"))
    except Exception:
        logger.exception("Could not read the alembic head")
        return None
    return SchemaHead(revision=head, ancestors=ancestors)
