"""API key management schemas."""

from datetime import datetime

from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    name: str
    raw_key: str  # shown once on creation
    mcp_path: str  # URL path for authless MCP clients: /t/<key>
    created_at: datetime


class ApiKeyInfo(BaseModel):
    key_id: str
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None
