"""Chat and model management API routes."""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
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
from harbor_clerk.config import get_settings, sync_native_config
from harbor_clerk.db import get_session
from harbor_clerk.llm.chat import chat_stream
from harbor_clerk.llm.download import (
    delete_model,
    download_model,
    get_download_status,
    get_model_path,
    is_downloading,
    list_downloaded,
)
from harbor_clerk.llm.models import get_model, list_models
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _summarize_tool_result(content: str) -> str:
    """Create a short human-readable summary of a tool result."""
    from harbor_clerk.llm.chat import _summarize_result

    return _summarize_result(content)


def _enrich_tool_calls(tool_calls: list[dict], results: dict[str, str]) -> list[dict]:
    """Add result summaries to tool calls in the frontend-friendly format.

    Converts from OpenAI format ({id, type, function: {name, arguments}})
    to the display format ({name, arguments, result}) that ToolCallCard expects.
    """
    enriched = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", tc.get("name", ""))
        # Parse arguments from JSON string (OpenAI format) or dict (already parsed)
        raw_args = func.get("arguments", tc.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        else:
            args = raw_args
        tc_id = tc.get("id", "")
        enriched.append(
            {
                "name": name,
                "arguments": args,
                "result": results.get(tc_id),
            }
        )
    return enriched


# --- Conversations ---


@router.get("/chat/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Conversation).where(Conversation.user_id == principal.id).order_by(Conversation.updated_at.desc())
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
        select(ChatMessage).where(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.created_at)
    )
    all_msgs = msgs_result.scalars().all()

    # Build a lookup of tool_call_id → result summary for enrichment.
    # This uses the already-fetched message set (no extra query), and the
    # idx_messages_conv(conversation_id, created_at) index covers the query.
    tool_results_by_id: dict[str, str] = {}
    for m in all_msgs:
        if m.role == "tool" and m.tool_call_id and m.content:
            tool_results_by_id[m.tool_call_id] = _summarize_tool_result(m.content)

    messages = []
    for m in all_msgs:
        tc = m.tool_calls
        # Enrich tool_calls with result summaries so the frontend can
        # display them in disclosure triangles after page reload.
        if tc and isinstance(tc, list):
            tc = _enrich_tool_calls(tc, tool_results_by_id)
        messages.append(
            ChatMessageOut(
                message_id=str(m.message_id),
                role=m.role,
                content=m.content,
                tool_calls=tc,
                tool_call_id=m.tool_call_id,
                rag_context=m.rag_context,
                tokens_used=m.tokens_used,
                model_id=m.model_id,
                context_pct=m.context_pct,
                created_at=m.created_at,
            )
        )

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


@router.get("/chat/conversations/{conv_id}/export")
async def export_conversation(
    conv_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Export conversation as a Markdown transcript."""
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.user_id != principal.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    msgs_result = await session.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.created_at)
    )
    msgs = msgs_result.scalars().all()

    lines: list[str] = [f"# {conv.title}\n"]
    lines.append(f"*Exported {conv.created_at.strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append("---\n")

    for m in msgs:
        if m.role == "tool":
            continue
        if m.role == "user":
            lines.append(f"**You:**\n\n{m.content}\n")
        elif m.role == "assistant":
            model_label = ""
            if m.model_id:
                model_info = get_model(m.model_id)
                model_label = f" *({model_info.name if model_info else m.model_id})*"
            # Strip <think> blocks for export
            content = m.content
            if content.startswith("<think>") and "</think>" in content:
                content = content[content.index("</think>") + len("</think>") :].strip()
            lines.append(f"**Assistant{model_label}:**\n\n{content}\n")

    transcript = "\n".join(lines)
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in conv.title).strip()[:60] or "conversation"
    filename = f"{safe_title}.md"

    from urllib.parse import quote

    encoded_filename = quote(filename)
    return Response(
        content=transcript,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


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
            detail="No LLM model configured. Select and activate a model in Settings.",
        )

    # Update conversation timestamp
    conv.updated_at = func.now()
    await session.commit()

    return StreamingResponse(
        chat_stream(conv_id, body.content, user_id=principal.id),
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
            downloading=is_downloading(m.id),
            yarn_available=m.yarn is not None,
            yarn_extended_context=m.yarn.extended_context if m.yarn else None,
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
    def _download_with_logging():
        try:
            download_model(model_id)
        except Exception:
            logger.exception("Background model download failed for %s", model_id)

    asyncio.get_running_loop().run_in_executor(None, _download_with_logging)

    return {"status": "downloading"}


@router.put("/chat/models/{model_id}/activate", status_code=200)
async def activate_model(
    model_id: str,
    principal: Principal = Depends(require_admin),
):
    if get_model_path(model_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not downloaded")
    settings = get_settings()
    settings.llm_model_id = model_id
    sync_native_config("llm_model_id", model_id)
    return {"status": "activated"}


@router.put("/chat/models/deactivate", status_code=200)
async def deactivate_model(
    principal: Principal = Depends(require_admin),
):
    settings = get_settings()
    settings.llm_model_id = ""
    sync_native_config("llm_model_id", "")
    return {"status": "deactivated"}


@router.put("/chat/models/yarn", status_code=200)
async def toggle_yarn(
    principal: Principal = Depends(require_admin),
    enabled: bool = True,
):
    settings = get_settings()
    settings.llm_yarn_enabled = enabled
    sync_native_config("llm_yarn_enabled", enabled)
    return {"status": "enabled" if enabled else "disabled", "yarn_enabled": enabled}


@router.get("/chat/models/yarn", status_code=200)
async def get_yarn_status(
    principal: Principal = Depends(require_user),
):
    settings = get_settings()
    return {"yarn_enabled": settings.llm_yarn_enabled}


@router.delete("/chat/models/{model_id}", status_code=204)
async def remove_model(
    model_id: str,
    principal: Principal = Depends(require_admin),
):
    if not delete_model(model_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model not found or not downloaded",
        )

    # If this was the active model, clear it
    settings = get_settings()
    if settings.llm_model_id == model_id:
        settings.llm_model_id = ""
        sync_native_config("llm_model_id", "")


@router.get("/chat/models/download-progress")
async def download_progress_stream(
    principal: Principal = Depends(require_user),
):
    """SSE stream for model download progress via in-memory polling."""
    import json

    async def event_generator():
        prev_active: set[str] = set()
        errored: set[str] = set()
        try:
            while True:
                entries = get_download_status()
                current_active: set[str] = set()
                for entry in entries:
                    yield f"data: {json.dumps(entry)}\n\n"
                    if entry["status"] == "downloading":
                        current_active.add(entry["model_id"])
                    elif entry["status"] == "error":
                        errored.add(entry["model_id"])
                # Detect completions: was downloading, now gone (and not errored)
                for model_id in prev_active - current_active - errored:
                    yield f"data: {json.dumps({'model_id': model_id, 'status': 'complete', 'progress': 100})}\n\n"
                prev_active = current_active
                errored.clear()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
