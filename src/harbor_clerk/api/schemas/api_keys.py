"""API key management schemas."""

from datetime import datetime

from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    name: str
    raw_key: str  # shown once on creation
    created_at: datetime


class ApiKeyInfo(BaseModel):
    key_id: str
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None
