"""Tests for the ScopeSpec Pydantic schema."""

import uuid

import pytest
from pydantic import ValidationError

from harbor_clerk.api.schemas.scope import ScopeSpec


def test_empty_scope_spec_round_trips():
    """ScopeSpec() = empty = no restriction. Round-trips to/from {}."""
    spec = ScopeSpec()
    assert spec.folder_ids is None
    assert spec.model_dump(exclude_none=True) == {}


def test_scope_spec_with_folder_ids():
    folder_id = uuid.uuid4()
    spec = ScopeSpec(folder_ids=[folder_id])
    assert spec.folder_ids == [folder_id]


def test_scope_spec_accepts_empty_folder_ids_list():
    """An explicit empty list is valid and equivalent to None per the spec."""
    spec = ScopeSpec(folder_ids=[])
    assert spec.folder_ids == []


def test_scope_spec_ignores_unknown_keys():
    """Future axes (collection_ids, doc_ids, etc.) sent by newer clients to an older
    server must parse without error. extra='ignore' guarantees forward-compat."""
    spec = ScopeSpec.model_validate(
        {"folder_ids": [str(uuid.uuid4())], "collection_ids": ["future-key"], "extra_weirdness": 42}
    )
    assert spec.folder_ids is not None
    # The unknown keys are silently dropped.
    assert "collection_ids" not in spec.model_dump()


def test_scope_spec_rejects_non_uuid_folder_ids():
    """Bad UUIDs should fail validation cleanly with 422."""
    with pytest.raises(ValidationError):
        ScopeSpec(folder_ids=["not-a-uuid"])
