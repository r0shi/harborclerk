"""Pydantic models for the /api/languages endpoints."""

from pydantic import BaseModel, Field


class ToolStatus(BaseModel):
    """Per-tool installation status for one language."""

    status: str = Field(description="not_installed | installed | failed")
    size_bytes: int | None = Field(default=None, description="Approximate download size from the static map")


class LanguageSummary(BaseModel):
    """One row in the /api/languages response — what the UI renders per language."""

    code: str = Field(description="ISO 639-1 code (e.g. 'fr')")
    display_name: str
    built_in: bool = Field(description="True for languages bundled with the app (English)")
    enabled: bool = Field(description="True if this language is in the user's enabled_languages preference")
    tools: dict[str, ToolStatus] = Field(
        description="Per-tool status keyed by Tool.value (e.g. 'ocr', 'ner')",
    )


class LanguagesListResponse(BaseModel):
    languages: list[LanguageSummary]


class InstallRequest(BaseModel):
    """Body for POST /api/languages/{code}/install."""

    tools: list[str] = Field(description="Tool.value names to install (e.g. ['ocr', 'ner'])")


class InstallToolResult(BaseModel):
    tool: str
    status: str = Field(description="installed | already_installed | failed")
    error: str | None = None


class InstallResponse(BaseModel):
    results: list[InstallToolResult]


class RemoveResponse(BaseModel):
    status: str = Field(description="removed")
