"""Tests for UserScope and Principal.user_scope."""

import uuid

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import UserScope


def test_user_scope_defaults():
    """Empty UserScope means no restriction (matches KeyScope semantics)."""
    scope = UserScope()
    assert scope.folder_ids is None
    assert scope.is_unrestricted is True


def test_user_scope_is_unrestricted_with_empty_list():
    """folder_ids=[] also unrestricted (matches KeyScope)."""
    scope = UserScope(folder_ids=[])
    assert scope.is_unrestricted is True


def test_user_scope_with_folders_is_restricted():
    folder_id = uuid.uuid4()
    scope = UserScope(folder_ids=[folder_id])
    assert scope.is_unrestricted is False
    assert scope.folder_ids == [folder_id]


def test_principal_user_scope_defaults_to_none():
    """API-key principals carry key_scope; user principals carry user_scope. Both default None."""
    p = Principal(type="user", id=uuid.uuid4(), role="user")
    assert p.user_scope is None
    assert p.key_scope is None


def test_principal_can_carry_user_scope():
    folder_id = uuid.uuid4()
    p = Principal(
        type="user",
        id=uuid.uuid4(),
        role="user",
        user_scope=UserScope(folder_ids=[folder_id]),
    )
    assert p.user_scope is not None
    assert p.user_scope.folder_ids == [folder_id]
