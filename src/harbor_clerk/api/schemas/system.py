from pydantic import BaseModel, Field


class RetrievalSettingsResponse(BaseModel):
    max_history_messages: int
    mcp_max_k: int
    mcp_brief_chars: int
    chat_search_paginated: bool
    chat_search_k: int
    research_search_paginated: bool
    research_search_k: int


class DeleteAllRequest(BaseModel):
    confirmation: str = Field(description="Must be exactly 'DELETE EVERYTHING'")


class RetrievalSettingsUpdate(BaseModel):
    max_history_messages: int | None = Field(default=None, ge=10, le=100)
    mcp_max_k: int | None = Field(default=None, ge=10, le=1000)
    mcp_brief_chars: int | None = Field(default=None, ge=50, le=1000)
    chat_search_paginated: bool | None = None
    chat_search_k: int | None = Field(default=None, ge=5, le=50)
    research_search_paginated: bool | None = None
    research_search_k: int | None = Field(default=None, ge=10, le=100)
