"""Tests for the new scope JSONB column on Conversation and ResearchState."""

import uuid

from sqlalchemy import select

from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState


async def test_conversation_scope_defaults_to_empty_dict(db_session, admin_user):
    """A new conversation has scope == {} when not explicitly set."""
    conv = Conversation(user_id=admin_user.user_id, title="Test")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    assert conv.scope == {}


async def test_conversation_scope_round_trips_folder_ids(db_session, admin_user):
    """Setting scope persists the JSONB shape correctly."""
    folder_id = str(uuid.uuid4())
    conv = Conversation(
        user_id=admin_user.user_id,
        title="Scoped",
        scope={"folder_ids": [folder_id]},
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)

    assert conv.scope == {"folder_ids": [folder_id]}

    # Round-trip via fresh query
    fetched = (
        await db_session.execute(select(Conversation).where(Conversation.conversation_id == conv.conversation_id))
    ).scalar_one()
    assert fetched.scope == {"folder_ids": [folder_id]}


async def test_research_state_scope_defaults_to_empty_dict(db_session, admin_user):
    """A new research_state row has scope == {} when not explicitly set."""
    conv = Conversation(user_id=admin_user.user_id, title="Research conv", mode="research")
    db_session.add(conv)
    await db_session.flush()
    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy="search",
        status="queued",
        max_rounds=5,
    )
    db_session.add(state)
    await db_session.commit()
    await db_session.refresh(state)

    assert state.scope == {}
