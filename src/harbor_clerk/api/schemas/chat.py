"""Pydantic schemas for chat and model management endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from harbor_clerk.api.schemas.scope import ScopeSpec

# --- Conversations ---


class CreateConversationRequest(BaseModel):
    title: str = "New conversation"
    scope: ScopeSpec | None = None


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    scope: dict[str, Any] = {}


class ChatMessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    tool_calls: Any | None = None
    tool_call_id: str | None = None
    rag_context: Any | None = None
    tokens_used: int | None = None
    model_id: str | None = None
    context_pct: int | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    scope: dict[str, Any] = {}
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
    supports_research: bool = True
    downloaded: bool
    active: bool
    downloading: bool = False
    yarn_available: bool = False
    yarn_extended_context: int | None = None
    # Memory budget (#556): what llama-server needs at the model's context, the
    # smallest Mac that runs it with the rest of the app resident, and what
    # this machine can do: the largest context that fits here (0 = the weights
    # alone do not fit) and this machine's physical memory.
    memory_bytes: int = 0
    min_ram_gb: int = 0
    max_context_here: int = 0
    fits_here: bool = True
    system_ram_gb: float = 0.0
