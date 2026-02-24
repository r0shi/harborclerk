"""Chat and model management API routes."""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin, require_user
from harbor_clerk.api.schemas.chat import (
    ChatMessageOut,
    ConversationDetail,
    ConversationSummary,
    CreateConversationRequest,
    ModelOut,
    SendMessageRequest,
)
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.llm.chat import chat_stream
from harbor_clerk.llm.download import (
    delete_model,
    download_model,
    get_model_path,
    is_downloading,
    list_downloaded,
)
from harbor_clerk.llm.models import list_models
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


# --- Conversations ---


@router.get("/chat/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == principal.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        ConversationSummary(
            conversation_id=str(c.conversation_id),
            title=c.title,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in result.scalars().all()
    ]


@router.post("/chat/conversations", response_model=ConversationSummary)
async def create_conversation(
    body: CreateConversationRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    conv = Conversation(user_id=principal.id, title=body.title)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return ConversationSummary(
        conversation_id=str(conv.conversation_id),
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.get("/chat/conversations/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    msgs_result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at)
    )
    messages = [
        ChatMessageOut(
            message_id=str(m.message_id),
            role=m.role,
            content=m.content,
            tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id,
            tokens_used=m.tokens_used,
            created_at=m.created_at,
        )
        for m in msgs_result.scalars().all()
    ]

    return ConversationDetail(
        conversation_id=str(conv.conversation_id),
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=messages,
    )


@router.delete("/chat/conversations/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    await session.delete(conv)
    await session.commit()


@router.post("/chat/conversations/{conv_id}/messages")
async def send_message(
    conv_id: uuid.UUID,
    body: SendMessageRequest,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    settings = get_settings()
    if not settings.llm_model_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM model configured",
        )

    # Update conversation timestamp
    conv.updated_at = func.now()

    return StreamingResponse(
        chat_stream(conv_id, body.content, session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- Models ---


@router.get("/chat/models", response_model=list[ModelOut])
async def list_available_models(
    principal: Principal = Depends(require_user),
):
    settings = get_settings()
    downloaded = set(list_downloaded())
    return [
        ModelOut(
            id=m.id,
            name=m.name,
            size_bytes=m.size_bytes,
            context_window=m.context_window,
            supports_tools=m.supports_tools,
            downloaded=m.id in downloaded,
            active=m.id == settings.llm_model_id,
        )
        for m in list_models()
    ]


@router.post("/chat/models/{model_id}/download", status_code=202)
async def start_model_download(
    model_id: str,
    principal: Principal = Depends(require_admin),
):
    from harbor_clerk.llm.models import get_model

    info = get_model(model_id)
    if info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown model")

    if get_model_path(model_id) is not None:
        return {"status": "already_downloaded"}

    if is_downloading(model_id):
        return {"status": "already_downloading"}

    # Run download in background thread to avoid blocking
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, download_model, model_id)

    return {"status": "downloading"}


@router.delete("/chat/models/{model_id}", status_code=204)
async def remove_model(
    model_id: str,
    principal: Principal = Depends(require_admin),
):
    if not delete_model(model_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found or not downloaded")

    # If this was the active model, clear it
    settings = get_settings()
    if settings.llm_model_id == model_id:
        settings.llm_model_id = ""


@router.get("/chat/models/download-progress")
async def download_progress_stream(
    principal: Principal = Depends(require_user),
):
    """SSE stream for model download progress events via PostgreSQL LISTEN/NOTIFY."""
    import asyncpg

    from harbor_clerk.llm.download import DOWNLOAD_CHANNEL

    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def event_generator():
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _on_notify(conn, pid, channel, payload):
            queue.put_nowait(payload)

        conn = await asyncpg.connect(dsn)
        try:
            await conn.add_listener(DOWNLOAD_CHANNEL, _on_notify)
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await conn.remove_listener(DOWNLOAD_CHANNEL, _on_notify)
            await conn.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
