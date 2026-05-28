"""Tests for _visible_doc_ids honoring user_scope alongside key_scope."""

import pytest

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import UserScope
from harbor_clerk.mcp_server import _visible_doc_ids


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_no_scope_returns_none(db_session, admin_user, two_folder_corpus):
    """User principal with no user_scope → None (unrestricted)."""
    p = Principal(type="user", id=admin_user.user_id, role="user")
    visible = await _visible_doc_ids(db_session, p)
    assert visible is None


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_with_empty_scope_returns_none(db_session, admin_user, two_folder_corpus):
    """user_scope with folder_ids=[] is unrestricted."""
    p = Principal(
        type="user",
        id=admin_user.user_id,
        role="user",
        user_scope=UserScope(folder_ids=[]),
    )
    visible = await _visible_doc_ids(db_session, p)
    assert visible is None


@pytest.mark.asyncio
async def test_visible_doc_ids_user_principal_with_folder_scope_restricts(db_session, admin_user, two_folder_corpus):
    """user_scope with folder_ids returns only docs in those folders."""
    folder_a, _, docs_in_a, _ = two_folder_corpus
    p = Principal(
        type="user",
        id=admin_user.user_id,
        role="user",
        user_scope=UserScope(folder_ids=[folder_a.folder_id]),
    )
    visible = await _visible_doc_ids(db_session, p)
    expected = {d.doc_id for d in docs_in_a}
    assert visible == expected
