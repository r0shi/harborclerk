"""REST endpoints for managing language packs.

Read-only ``GET /api/languages`` is available to any authenticated user
(the Languages page lives under System Settings but the read view is
useful from elsewhere too — e.g. a future "doc was OCR'd as English but
French is enabled, want to reprocess?" affordance). Mutations (install,
remove) are admin-only.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin, require_human_user
from harbor_clerk.api.schemas.languages import (
    InstallRequest,
    InstallResponse,
    InstallToolResult,
    LanguagesListResponse,
    LanguageSummary,
    RemoveResponse,
    ToolStatus,
)
from harbor_clerk.db import get_session
from harbor_clerk.lang_packs.manager import (
    download_artifact,
    installed_tools_for,
    remove_artifact,
    remove_language,
)
from harbor_clerk.languages import LANGUAGES, Tool
from harbor_clerk.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/languages", tags=["languages"])


async def _get_enabled_languages(principal: Principal, session: AsyncSession) -> set[str]:
    """Read the operator's enabled_languages preference from their user record.

    Single-tenant appliance, so any human user's preference is effectively
    the global setting for that user. API-key callers don't have a
    preferences blob; they see English-only enabled. Falls back to
    {'en'} if the preference isn't set yet (fresh install).
    """
    if principal.type != "user":
        return {"en"}
    user = await session.get(User, principal.id)
    if user is None:
        return {"en"}
    prefs = user.preferences or {}
    enabled = prefs.get("enabled_languages")
    if isinstance(enabled, list) and enabled:
        return {str(c) for c in enabled if isinstance(c, str)}
    return {"en"}


def _summarize_language(code: str, enabled: set[str]) -> LanguageSummary:
    spec = LANGUAGES[code]
    installed = installed_tools_for(code)
    tools_dict: dict[str, ToolStatus] = {}
    for tool, artifact in spec.artifacts.items():
        tools_dict[tool.value] = ToolStatus(
            status="installed" if tool in installed else "not_installed",
            size_bytes=artifact.size_bytes,
        )
    return LanguageSummary(
        code=code,
        display_name=spec.display_name,
        built_in=not spec.artifacts,
        enabled=code in enabled,
        tools=tools_dict,
    )


@router.get("", response_model=LanguagesListResponse)
async def list_languages(
    principal: Principal = Depends(require_human_user),
    session: AsyncSession = Depends(get_session),
) -> LanguagesListResponse:
    """Return the curated language list with per-tool install state and the
    operator's current enabled-set."""
    enabled = await _get_enabled_languages(principal, session)
    return LanguagesListResponse(
        languages=[_summarize_language(code, enabled) for code in LANGUAGES],
    )


@router.post("/{lang_code}/install", response_model=InstallResponse)
async def install_language_tools(
    lang_code: str,
    body: InstallRequest,
    principal: Principal = Depends(require_admin),
) -> InstallResponse:
    """Download and install one or more tool artifacts for a language.

    Synchronous from the API's perspective — the call returns when every
    requested artifact has been fetched and verified, or failed. Per-call
    runtime is bounded by the artifact size; French OCR (~1 MB) and NER
    (~16 MB) finish in seconds on residential broadband. If we ever need
    to install much larger artifacts, switch to a background-task pattern
    with SSE progress.

    Each artifact is installed independently; one failure doesn't roll
    back the others.
    """
    if lang_code not in LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown language: {lang_code!r}",
        )
    spec = LANGUAGES[lang_code]

    requested: list[Tool] = []
    for t in body.tools:
        try:
            requested.append(Tool(t))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown tool: {t!r}",
            )
    for tool in requested:
        if tool not in spec.artifacts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Language {lang_code!r} has no {tool.value} artifact",
            )

    # download_artifact uses synchronous httpx.stream; run in a worker
    # thread so the event loop stays responsive for other requests.
    loop = asyncio.get_running_loop()
    results: list[InstallToolResult] = []
    for tool in requested:
        result = await loop.run_in_executor(None, download_artifact, lang_code, tool)
        results.append(
            InstallToolResult(
                tool=tool.value,
                status=result.status,
                error=result.error,
            )
        )
        if result.status == "failed":
            logger.warning(
                "Language pack install failed: %s/%s — %s",
                lang_code,
                tool.value,
                result.error,
            )
    return InstallResponse(results=results)


@router.delete("/{lang_code}/install/{tool_name}", response_model=RemoveResponse)
async def remove_language_tool(
    lang_code: str,
    tool_name: str,
    principal: Principal = Depends(require_admin),
) -> RemoveResponse:
    """Remove a single tool artifact for a language. Idempotent."""
    if lang_code not in LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown language: {lang_code!r}",
        )
    try:
        tool = Tool(tool_name)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown tool: {tool_name!r}",
        )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, remove_artifact, lang_code, tool)
    return RemoveResponse(status="removed")


@router.delete("/{lang_code}", response_model=RemoveResponse)
async def remove_language_completely(
    lang_code: str,
    principal: Principal = Depends(require_admin),
) -> RemoveResponse:
    """Remove every installed artifact for a language. Idempotent.

    Convenience for the "Disable French entirely" UI affordance — saves
    the client from looping over Tool variants and gracefully handles
    partial installs.
    """
    if lang_code not in LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown language: {lang_code!r}",
        )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, remove_language, lang_code)
    return RemoveResponse(status="removed")
