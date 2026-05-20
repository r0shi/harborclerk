"""Tests for the dynamic schema-version detection at API startup."""

from harbor_clerk.api.app import _find_alembic_dir, _get_expected_schema_version


def test_find_alembic_dir_returns_existing_directory():
    """The walk-up should find the project's alembic dir."""
    result = _find_alembic_dir()
    assert result is not None
    assert result.is_dir()
    assert (result.parent / "alembic.ini").exists()


def test_get_expected_schema_version_returns_alembic_head():
    """The function should return the Alembic head revision ID.

    After the embedding-v2 rebase the chain is a single migration with
    revision id "0001_initial". We don't hardcode that literal here so this
    test stays valid across future rebases — we just assert the function
    returns a non-empty string and that at least one numbered migration file
    exists.
    """
    result = _get_expected_schema_version()
    assert result is not None, "Expected a head revision; got None"
    assert isinstance(result, str) and result, "Expected a non-empty revision string"

    # Sanity: the alembic versions directory must have at least one numbered file.
    alembic_dir = _find_alembic_dir()
    assert alembic_dir is not None
    versions = sorted(p.name for p in (alembic_dir / "versions").glob("[0-9]*.py"))
    assert versions, "No numbered migration files found"
