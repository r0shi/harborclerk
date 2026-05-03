"""Tests for the dynamic schema-version detection at API startup."""

from harbor_clerk.api.app import _find_alembic_dir, _get_expected_schema_version


def test_find_alembic_dir_returns_existing_directory():
    """The walk-up should find the project's alembic dir."""
    result = _find_alembic_dir()
    assert result is not None
    assert result.is_dir()
    assert (result.parent / "alembic.ini").exists()


def test_get_expected_schema_version_returns_alembic_head():
    """The function should return the highest migration revision in the
    alembic versions directory.

    This test asserts the function returns *something* numeric and that it
    matches the highest-numbered revision file. We don't hardcode "0018"
    here because the whole point of this change is to stop hardcoding
    revision numbers — the test would defeat itself if it did.
    """
    result = _get_expected_schema_version()
    assert result is not None, "Expected a head revision; got None"

    # Cross-check against the highest-numbered migration file (filename
    # convention: NNNN_description.py).
    alembic_dir = _find_alembic_dir()
    assert alembic_dir is not None
    versions = sorted(p.name for p in (alembic_dir / "versions").glob("[0-9]*.py"))
    assert versions, "No numbered migration files found"
    highest_filename_revision = versions[-1].split("_", 1)[0]
    assert result == highest_filename_revision, (
        f"_get_expected_schema_version returned {result}, but the highest "
        f"migration file is {versions[-1]} (revision {highest_filename_revision})"
    )
