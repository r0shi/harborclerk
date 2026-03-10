"""Research mode API routes — start, resume, list, detail, delete."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_user
from harbor_clerk.api.schemas.research import (
    ResearchActiveCheck,
    ResearchDetail,
    ResearchSummary,
    StartResearchRequest,
)
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.llm.model_settings import get_model_setting
from harbor_clerk.llm.models import DEFAULT_RESEARCH_MAX_ROUNDS, default_research_strategy
from harbor_clerk.llm.research import research_stream
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation
from harbor_clerk.models.research_state import ResearchState

logger = logging.getLogger(__name__)
router = APIRouter(tags=["research"])


def _require_human(principal: Principal) -> None:
    """Raise 403 if the caller is not a human user (i.e. API key)."""
    if principal.type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Research mode requires a human user session",
        )


# ---------------------------------------------------------------------------
# GET /research/active — is a research task currently running?
# ---------------------------------------------------------------------------


@router.get("/research/active", response_model=ResearchActiveCheck)
async def check_active(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)
    result = await session.execute(select(ResearchState).where(ResearchState.status == "running"))
    running = result.scalar_one_or_none()
    if running:
        return ResearchActiveCheck(active=True, research_id=str(running.conversation_id))
    return ResearchActiveCheck(active=False)


# ---------------------------------------------------------------------------
# GET /research — list research tasks for current user
# ---------------------------------------------------------------------------


@router.get("/research", response_model=list[ResearchSummary])
async def list_research(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)
    result = await session.execute(
        select(Conversation, ResearchState)
        .join(ResearchState, ResearchState.conversation_id == Conversation.conversation_id)
        .where(Conversation.mode == "research", Conversation.user_id == principal.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        ResearchSummary(
            conversation_id=str(conv.conversation_id),
            title=conv.title,
            strategy=rs.strategy,
            status=rs.status,
            current_round=rs.current_round,
            max_rounds=rs.max_rounds,
            created_at=conv.created_at,
            completed_at=rs.completed_at,
        )
        for conv, rs in result.all()
    ]


# ---------------------------------------------------------------------------
# GET /research/{conv_id} — full detail with messages
# ---------------------------------------------------------------------------


@router.get("/research/{conv_id}", response_model=ResearchDetail)
async def get_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)

    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research task not found")

    state = await session.get(ResearchState, conv_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research state not found")

    # Load messages
    msgs_result = await session.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.created_at)
    )
    all_msgs = msgs_result.scalars().all()

    # Find user question (first user message)
    question = ""
    for m in all_msgs:
        if m.role == "user":
            question = m.content or ""
            break

    # Find report (last assistant message if completed)
    report: str | None = None
    if state.status == "completed":
        for m in reversed(all_msgs):
            if m.role == "assistant" and m.content:
                report = m.content
                break

    # Build tool result lookup for enrichment
    from harbor_clerk.api.routes.chat import _enrich_tool_calls, _summarize_tool_result

    tool_results_by_id: dict[str, str] = {}
    for m in all_msgs:
        if m.role == "tool" and m.tool_call_id and m.content:
            tool_results_by_id[m.tool_call_id] = _summarize_tool_result(m.content)

    # Build message dicts
    messages: list[dict] = []
    for m in all_msgs:
        msg_dict: dict = {
            "message_id": str(m.message_id),
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        tc = m.tool_calls
        if tc and isinstance(tc, list):
            tc = _enrich_tool_calls(tc, tool_results_by_id)
        if tc:
            msg_dict["tool_calls"] = tc
        if m.tool_call_id:
            msg_dict["tool_call_id"] = m.tool_call_id
        if m.model_id:
            msg_dict["model_id"] = m.model_id
        messages.append(msg_dict)

    return ResearchDetail(
        conversation_id=str(conv.conversation_id),
        title=conv.title,
        question=question,
        strategy=state.strategy,
        status=state.status,
        current_round=state.current_round,
        max_rounds=state.max_rounds,
        progress=state.progress,
        report=report,
        model_id=settings.llm_model_id if (settings := get_settings()).llm_model_id else None,
        messages=messages,
        created_at=conv.created_at,
        completed_at=state.completed_at,
    )


# ---------------------------------------------------------------------------
# POST /research — start a new research task
# ---------------------------------------------------------------------------


@router.post("/research")
async def start_research(
    body: StartResearchRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)

    # Check no active research
    running_result = await session.execute(select(ResearchState).where(ResearchState.status == "running"))
    if running_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A research task is already running")

    settings = get_settings()
    if not settings.llm_model_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM model configured. Select and activate a model in Settings.",
        )

    # Determine strategy
    strategy = body.strategy or default_research_strategy(settings.llm_model_id)

    # Get max_rounds from per-model settings or global default
    max_rounds_val = await get_model_setting(session, settings.llm_model_id, "research_max_rounds")
    max_rounds = int(max_rounds_val) if max_rounds_val is not None else DEFAULT_RESEARCH_MAX_ROUNDS

    # Create conversation
    conv = Conversation(user_id=principal.id, title="New conversation", mode="research")
    session.add(conv)
    await session.flush()

    # Create user message
    user_msg = ChatMessage(
        conversation_id=conv.conversation_id,
        role="user",
        content=body.question,
    )
    session.add(user_msg)

    # Create research state
    state = ResearchState(
        conversation_id=conv.conversation_id,
        strategy=strategy,
        status="running",
        current_round=0,
        max_rounds=max_rounds,
    )
    session.add(state)
    await session.commit()

    return StreamingResponse(
        research_stream(conv.conversation_id, user_id=principal.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Research-Id": str(conv.conversation_id),
        },
    )


# ---------------------------------------------------------------------------
# POST /research/{conv_id}/resume — resume an interrupted task
# ---------------------------------------------------------------------------


@router.post("/research/{conv_id}/resume")
async def resume_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)

    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research task not found")

    state = await session.get(ResearchState, conv_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research state not found")

    if state.status != "interrupted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume research with status '{state.status}' — only 'interrupted' tasks can be resumed",
        )

    # Check no other research is running
    running_result = await session.execute(
        select(ResearchState).where(
            ResearchState.status == "running",
            ResearchState.conversation_id != conv_id,
        )
    )
    if running_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another research task is already running")

    return StreamingResponse(
        research_stream(conv.conversation_id, user_id=principal.id, resume=True),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# DELETE /research/{conv_id} — delete a research task
# ---------------------------------------------------------------------------


@router.delete("/research/{conv_id}", status_code=200)
async def delete_research(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    _require_human(principal)

    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id or conv.mode != "research":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research task not found")

    await session.delete(conv)
    await session.commit()
    return {"ok": True}
