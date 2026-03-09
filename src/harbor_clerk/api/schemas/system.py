from pydantic import BaseModel, Field


class RetrievalSettingsResponse(BaseModel):
    max_tool_rounds: int
    max_history_messages: int
    mcp_max_k: int
    mcp_brief_chars: int


class DeleteAllRequest(BaseModel):
    confirmation: str = Field(description="Must be exactly 'DELETE EVERYTHING'")


class RetrievalSettingsUpdate(BaseModel):
    max_tool_rounds: int = Field(ge=1, le=10)
    max_history_messages: int = Field(ge=10, le=100)
    mcp_max_k: int = Field(ge=10, le=1000)
    mcp_brief_chars: int = Field(ge=50, le=1000)
