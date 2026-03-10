"""Pydantic schemas for research mode API."""

from datetime import datetime

from pydantic import BaseModel, Field


class StartResearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=10000)
    strategy: str | None = Field(default=None, pattern="^(search|sweep)$", description="Override default strategy")


class ResearchProgress(BaseModel):
    """Progress snapshot from research_state."""

    conversation_id: str
    question: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    progress: dict | None = None
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class ResearchSummary(BaseModel):
    """List item for research history."""

    conversation_id: str
    title: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    created_at: datetime
    completed_at: datetime | None = None


class ResearchDetail(BaseModel):
    """Full research task with messages."""

    conversation_id: str
    title: str
    question: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    progress: dict | None = None
    report: str | None = None
    model_id: str | None = None
    messages: list[dict]
    created_at: datetime
    completed_at: datetime | None = None


class ResearchActiveCheck(BaseModel):
    active: bool
    research_id: str | None = None
