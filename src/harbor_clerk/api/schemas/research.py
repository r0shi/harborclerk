"""Pydantic schemas for research mode API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from harbor_clerk.api.schemas.scope import ScopeSpec


class StartResearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=10000)
    strategy: str | None = Field(default=None, pattern="^(search|sweep)$", description="Override default strategy")
    time_limit_minutes: int = Field(default=30, ge=15, le=180)
    depth: str = Field(default="standard", pattern="^(light|standard|thorough)$")
    scope: ScopeSpec | None = None


class ResearchProgress(BaseModel):
    """Progress snapshot from research_state."""

    conversation_id: str
    question: str
    strategy: str
    status: str
    current_round: int
    max_rounds: int
    time_limit_minutes: int | None = None
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
    time_limit_minutes: int | None = None
    depth: str | None = None
    scope: dict[str, Any] = {}
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
    time_limit_minutes: int | None = None
    depth: str | None = None
    scope: dict[str, Any] = {}
    progress: dict | None = None
    notes: str | None = None
    report: str | None = None
    model_id: str | None = None
    messages: list[dict]
    # Deduped citation records — the documents whose passages informed the
    # synthesized report, persisted on research_state.citations by the
    # research engine.
    citations: list[dict] = []
    # Free-text reason set when the research moved to ``interrupted`` or
    # ``failed``. Examples: ``"Synthesis failed: LLM error (502)"``,
    # ``"Research task stalled — no progress for 5+ minutes"`` (reaper),
    # ``"Stream disconnected before completion"``. Used by the test harness
    # to attribute interrupts to a specific cause (synthesis failure vs.
    # reaper vs. client disconnect) rather than collapsing them all into
    # the generic ``"research interrupted by Harbor Clerk"`` label.
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ResearchActiveCheck(BaseModel):
    active: bool
    research_id: str | None = None
