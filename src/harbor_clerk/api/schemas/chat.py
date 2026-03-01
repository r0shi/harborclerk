"""Pydantic schemas for chat and model management endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# --- Conversations ---


class CreateConversationRequest(BaseModel):
    title: str = "New conversation"


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    tool_calls: Any | None = None
    tool_call_id: str | None = None
    rag_context: Any | None = None
    tokens_used: int | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageOut]


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


# --- Models ---


class ModelOut(BaseModel):
    id: str
    name: str
    size_bytes: int
    context_window: int
    supports_tools: bool
    downloaded: bool
    active: bool
    downloading: bool = False
