"""API key schemas."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

# The api_keys columns are 32-bit integers: a larger value would pass validation and then
# fail the INSERT/UPDATE with a 500.
INT32_MAX = 2**31 - 1

# A key's snippet cap: None (no cap) or N >= 0, the first N characters of the passage and
# page text the key receives on any surface, none at 0 (harbor_clerk/passage_cap.py, #823).
SnippetCap = Annotated[int | None, Field(ge=0, le=INT32_MAX)]

# A per-key rate limit: None (the system default), 0 (unlimited) or N >= 1 requests.
RateLimit = Annotated[int | None, Field(ge=0, le=INT32_MAX)]

# A key's name, stripped; a blank name is refused on create and PATCH alike.
KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateApiKeyRequest(BaseModel):
    name: KeyName
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] = "full"
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: SnippetCap = None
    rate_limit_rpm: RateLimit = None
    rate_limit_rph: RateLimit = None


class PatchApiKeyRequest(BaseModel):
    name: KeyName | None = None
    expires_at: datetime | None = None
    permission_tier: Literal["search", "read", "full"] | None = None
    tool_overrides: dict[str, bool] | None = None
    scope_topic_ids: list[int] | None = None
    scope_folder_ids: list[str] | None = None
    max_snippet_chars: SnippetCap = None
    rate_limit_rpm: RateLimit = None
    rate_limit_rph: RateLimit = None


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
    rate_limit_rpm: int | None
    rate_limit_rph: int | None
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


# --- Usage / Audit dashboard schemas ---


class ToolCount(BaseModel):
    endpoint: str
    count: int


class UsageSummaryResponse(BaseModel):
    requests: dict[str, int]
    errors: dict[str, int]
    denials: dict[str, int]
    rate_limited: dict[str, int]
    last_used_at: datetime | None
    top_tools: dict[str, list[ToolCount]]


class TimelineDay(BaseModel):
    date: str
    ok: int
    error: int
    denied: int
    rate_limited: int


class RequestLogEntry(BaseModel):
    request_id: str
    request_type: str
    endpoint: str
    parameters: dict | None
    status: str
    status_detail: str | None
    result_summary: dict | None
    duration_ms: int
    created_at: datetime


class RequestLogPage(BaseModel):
    items: list[RequestLogEntry]
    total: int
    page: int
    page_size: int


class PurgeResponse(BaseModel):
    deleted: int
