"""API key schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] = "full"
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None


class PatchApiKeyRequest(BaseModel):
    name: str | None = None
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] | None = None
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: int | None = None


class ApiKeyOut(BaseModel):
    key_id: str
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    permission_tier: str
    tool_overrides: dict[str, bool]
    scope_topic_ids: list[int] | None
    scope_folder_ids: list[str] | None
    max_snippet_chars: int | None
    scope_summary: str


class ScopePreviewRequest(BaseModel):
    permission_tier: Literal["search", "read", "full"] = "full"
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None


class ScopePreviewResponse(BaseModel):
    accessible_documents: int
    total_documents: int


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    name: str
    raw_key: str  # shown once on creation
    mcp_path: str  # URL path for authless MCP clients: /t/<key>
    created_at: datetime
