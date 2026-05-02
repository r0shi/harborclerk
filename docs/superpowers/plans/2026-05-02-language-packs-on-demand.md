# Language packs on demand — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a System Settings → Languages page where each non-English language is opt-in. Enabling pulls the right model artifacts for every tool (Tesseract OCR, spaCy NER) and wires the worker to use them dynamically.

**Architecture:** Static `(lang, tool) → ArtifactSpec` map drives a download manager, a System Settings UI page, and dynamic per-job language selection in the worker. English remains bundled. Embedder is already multilingual, no per-language artifact needed.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 async, Alembic, React 19, Tailwind 4, Tesseract via pytesseract, spaCy, multilingual-e5-small embedder.

**Spec:** `docs/superpowers/specs/2026-05-02-language-packs-on-demand-design.md`

---

## File Structure

**New files (Python):**
- `src/harbor_clerk/languages.py` — static `LANGUAGES: dict[str, LanguageSpec]` + `Tool` enum + `ArtifactSpec` dataclass
- `src/harbor_clerk/lang_packs/__init__.py` — package marker
- `src/harbor_clerk/lang_packs/manager.py` — download / verify / remove / status APIs
- `src/harbor_clerk/lang_packs/storage.py` — disk layout helpers (`lang_packs_dir()`, `artifact_path(lang, tool)`)
- `src/harbor_clerk/api/routes/languages.py` — `/admin/languages` REST endpoints
- `src/harbor_clerk/api/schemas/languages.py` — Pydantic request/response models

**Modified files (Python):**
- `src/harbor_clerk/worker/stages/ocr.py` — replace `TESSERACT_LANG = "eng+fra"` with dynamic per-job lang list; capture `ocr_languages_used`
- `src/harbor_clerk/worker/stages/entities.py` — load spaCy model per-language on demand
- `src/harbor_clerk/worker/ner.py` — extend `extract_entities_batch` to handle on-demand spaCy model loads with graceful "no NER for this language" fallback
- `src/harbor_clerk/api/app.py` — register the new languages router
- `src/harbor_clerk/models/document.py` — add `ocr_languages_used: list[str]` column
- `src/harbor_clerk/api/schemas/me.py` (or wherever preferences live) — add `enabled_languages: list[str]` to PreferencesUpdate
- `alembic/versions/0018_*.py` — new migration adding `documents.ocr_languages_used`

**New files (Frontend):**
- `frontend/src/pages/LanguagesPage.tsx` — System Settings → Languages page
- `frontend/src/api/languages.ts` — typed REST client for the new endpoints

**Modified files (Frontend):**
- `frontend/src/pages/SystemSettingsPage.tsx` — add "Languages" link
- `frontend/src/App.tsx` — add `/admin/languages` route
- `frontend/src/pages/DocumentDetailPage.tsx` — show `ocr_languages_used` and add per-doc "Reprocess with [enabled languages]" button
- `frontend/src/pages/HomePage.tsx` (or wherever the empty-state corpus banner lives) — add "Add languages" link

**New tests:**
- `tests/test_languages_static_map.py` — schema validation of the static map
- `tests/lang_packs/test_manager.py` — integration tests against `pytest-httpserver`
- `tests/api/test_languages_endpoints.py` — REST endpoint tests
- `tests/worker/test_ocr_dynamic_lang.py` — OCR runs with the right `-l` arg per preference
- `tests/worker/test_ner_dynamic_lang.py` — NER falls back gracefully when pack is missing

---

## Task 1: Static language capability map

**Files:**
- Create: `src/harbor_clerk/languages.py`
- Test: `tests/test_languages_static_map.py`

- [ ] **Step 1: Write the failing test for ArtifactSpec validation**

```python
# tests/test_languages_static_map.py
import pytest
from harbor_clerk.languages import LANGUAGES, Tool, ArtifactSpec


def test_languages_dict_has_english_with_no_artifacts():
    assert "en" in LANGUAGES
    assert LANGUAGES["en"].artifacts == {}, "English ships in the bundle, no per-language artifacts"


def test_every_language_spec_has_iso_code_matching_key():
    for code, spec in LANGUAGES.items():
        assert spec.code == code


def test_every_artifact_has_https_url_and_64_char_sha256():
    for code, spec in LANGUAGES.items():
        for tool, artifact in spec.artifacts.items():
            assert artifact.url.startswith("https://"), f"{code}/{tool} url not https"
            assert len(artifact.sha256) == 64, f"{code}/{tool} sha256 wrong length"
            assert artifact.size_bytes > 0


def test_french_has_both_ocr_and_ner_artifacts():
    fr = LANGUAGES["fr"]
    assert Tool.OCR in fr.artifacts
    assert Tool.NER in fr.artifacts
    assert "fra.traineddata" in fr.artifacts[Tool.OCR].install_subpath
    assert "fr_core_news_sm" in fr.artifacts[Tool.NER].install_subpath
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_languages_static_map.py -v`
Expected: FAIL with `ModuleNotFoundError` or `KeyError: 'en'`

- [ ] **Step 3: Implement the static map**

```python
# src/harbor_clerk/languages.py
"""Static map of (language, tool) -> downloadable artifact spec.

Single source of truth for what we know how to install per-language.
The download manager (lang_packs/manager.py) consults this map; the
System Settings UI renders it; the worker reads the user preferences
and picks the right artifacts at job time.
"""
from dataclasses import dataclass, field
from enum import Enum


class Tool(Enum):
    OCR = "ocr"   # Tesseract traineddata
    NER = "ner"   # spaCy *_core_news_sm or *_core_web_sm

    # Extension point: add new tools here as Harbor Clerk grows


@dataclass(frozen=True)
class ArtifactSpec:
    url: str           # https URL to fetch from
    sha256: str        # hex digest, 64 chars
    size_bytes: int    # approximate, for UI download-size display
    install_subpath: str  # relative to lang_packs/<lang>/


@dataclass(frozen=True)
class LanguageSpec:
    code: str          # ISO 639-1, e.g. "fr"
    display_name: str  # e.g. "French"
    artifacts: dict[Tool, ArtifactSpec] = field(default_factory=dict)


LANGUAGES: dict[str, LanguageSpec] = {
    "en": LanguageSpec(
        code="en",
        display_name="English",
        artifacts={},  # bundled in the app — no per-language download
    ),
    "fr": LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url="https://github.com/tesseract-ocr/tessdata_fast/raw/main/fra.traineddata",
                sha256="<TODO: actual SHA from upstream>",
                size_bytes=25_000_000,
                install_subpath="tesseract/fra.traineddata",
            ),
            Tool.NER: ArtifactSpec(
                url="https://github.com/explosion/spacy-models/releases/download/fr_core_news_sm-3.8.0/fr_core_news_sm-3.8.0-py3-none-any.whl",
                sha256="<TODO: actual SHA from upstream>",
                size_bytes=50_000_000,
                install_subpath="ner/fr_core_news_sm-3.8.0-py3-none-any.whl",
            ),
        },
    ),
    # TODO: add ~25 more for the curated default list (de, es, it, pt, nl, pl,
    # ru, zh, ja, ko, ar, he, hi, vi, th, id, sv, da, no, fi, tr, el, cs, hu, uk)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_languages_static_map.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/languages.py tests/test_languages_static_map.py
git commit -m "feat(languages): static (lang, tool) -> ArtifactSpec map"
```

---

## Task 2: Disk layout helpers

**Files:**
- Create: `src/harbor_clerk/lang_packs/__init__.py`
- Create: `src/harbor_clerk/lang_packs/storage.py`
- Test: `tests/lang_packs/test_storage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/lang_packs/test_storage.py
import os
from harbor_clerk.lang_packs.storage import lang_packs_dir, artifact_path
from harbor_clerk.languages import LANGUAGES, Tool


def test_lang_packs_dir_uses_settings_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    assert lang_packs_dir() == tmp_path


def test_artifact_path_combines_lang_and_install_subpath(tmp_path, monkeypatch):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    fr_ocr_path = artifact_path("fr", Tool.OCR)
    assert fr_ocr_path == tmp_path / "fr" / "tesseract" / "fra.traineddata"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/lang_packs/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement storage helpers**

```python
# src/harbor_clerk/lang_packs/__init__.py
"""On-demand language pack management.

See docs/superpowers/specs/2026-05-02-language-packs-on-demand-design.md
"""

# src/harbor_clerk/lang_packs/storage.py
"""Disk layout for downloaded language artifacts.

    <lang_packs_dir>/<lang>/<install_subpath>

Configurable via LANG_PACKS_DIR env var (defaults to platform-appropriate
application support directory). Same convention as models_dir for LLMs.
"""
import os
from pathlib import Path

from harbor_clerk.languages import LANGUAGES, Tool


def lang_packs_dir() -> Path:
    """Root directory for downloaded language artifacts."""
    if env_dir := os.environ.get("LANG_PACKS_DIR"):
        return Path(env_dir)
    # Default to ~/Library/Application Support/Harbor Clerk/lang-packs on macOS,
    # ~/.local/share/harbor-clerk/lang-packs on Linux.
    if os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "lang-packs"
    return Path.home() / ".local" / "share" / "harbor-clerk" / "lang-packs"


def artifact_path(lang_code: str, tool: Tool) -> Path:
    """Where this artifact lives on disk after download."""
    spec = LANGUAGES[lang_code]
    artifact = spec.artifacts[tool]
    return lang_packs_dir() / lang_code / artifact.install_subpath
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/lang_packs/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/lang_packs/ tests/lang_packs/test_storage.py
git commit -m "feat(lang_packs): disk layout helpers (lang_packs_dir, artifact_path)"
```

---

## Task 3: Download manager

**Files:**
- Create: `src/harbor_clerk/lang_packs/manager.py`
- Test: `tests/lang_packs/test_manager.py`

- [ ] **Step 1: Add pytest-httpserver to test deps**

Modify `pyproject.toml` `[project.optional-dependencies] test`:

```toml
test = [
    # ... existing deps ...
    "pytest-httpserver>=1.1",
]
```

Run: `uv sync --extra test`

- [ ] **Step 2: Write the failing tests**

```python
# tests/lang_packs/test_manager.py
import hashlib
import pytest
from pytest_httpserver import HTTPServer

from harbor_clerk.lang_packs.manager import (
    DownloadStatus,
    download_artifact,
    verify_artifact,
    remove_artifact,
)
from harbor_clerk.languages import LANGUAGES, Tool, LanguageSpec, ArtifactSpec


@pytest.fixture
def fr_with_local_url(monkeypatch, httpserver: HTTPServer, tmp_path):
    """Patch LANGUAGES['fr'] to point at a local httpserver fixture."""
    payload = b"FAKE-TRAINEDDATA-CONTENT"
    sha = hashlib.sha256(payload).hexdigest()
    httpserver.expect_request("/fra.traineddata").respond_with_data(payload)
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    fake_fr = LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url=httpserver.url_for("/fra.traineddata"),
                sha256=sha,
                size_bytes=len(payload),
                install_subpath="tesseract/fra.traineddata",
            ),
        },
    )
    monkeypatch.setitem(LANGUAGES, "fr", fake_fr)
    return payload, sha


def test_download_artifact_writes_file_and_verifies(fr_with_local_url, tmp_path):
    payload, sha = fr_with_local_url
    result = download_artifact("fr", Tool.OCR)
    assert result.status == "installed"
    assert (tmp_path / "fr" / "tesseract" / "fra.traineddata").read_bytes() == payload
    assert verify_artifact("fr", Tool.OCR) is True


def test_download_failure_on_sha_mismatch(monkeypatch, httpserver, tmp_path):
    httpserver.expect_request("/fra.traineddata").respond_with_data(b"WRONG-CONTENT")
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    fake_fr = LanguageSpec(
        code="fr",
        display_name="French",
        artifacts={
            Tool.OCR: ArtifactSpec(
                url=httpserver.url_for("/fra.traineddata"),
                sha256="0" * 64,  # never matches
                size_bytes=10,
                install_subpath="tesseract/fra.traineddata",
            ),
        },
    )
    monkeypatch.setitem(LANGUAGES, "fr", fake_fr)
    result = download_artifact("fr", Tool.OCR)
    assert result.status == "failed"
    assert "sha256" in result.error.lower()
    # Failed downloads must NOT leave a partial file on disk
    assert not (tmp_path / "fr" / "tesseract" / "fra.traineddata").exists()


def test_remove_artifact_deletes_file(fr_with_local_url, tmp_path):
    download_artifact("fr", Tool.OCR)
    assert (tmp_path / "fr" / "tesseract" / "fra.traineddata").exists()
    remove_artifact("fr", Tool.OCR)
    assert not (tmp_path / "fr" / "tesseract" / "fra.traineddata").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/lang_packs/test_manager.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement the download manager**

```python
# src/harbor_clerk/lang_packs/manager.py
"""Download / verify / remove language pack artifacts.

Synchronous for the initial cut. Background-job version (with progress
streaming over SSE) is a future enhancement; the API endpoint can wrap
download_artifact in run_in_executor for now.
"""
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from harbor_clerk.languages import LANGUAGES, Tool
from harbor_clerk.lang_packs.storage import artifact_path

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    status: str  # "installed" | "failed" | "already_installed"
    error: str | None = None
    bytes_downloaded: int = 0


def download_artifact(lang_code: str, tool: Tool) -> DownloadResult:
    """Fetch + verify + install. Returns a result object."""
    spec = LANGUAGES[lang_code].artifacts[tool]
    target = artifact_path(lang_code, tool)

    if target.exists() and verify_artifact(lang_code, tool):
        return DownloadResult(status="already_installed")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")

    try:
        with httpx.stream("GET", spec.url, timeout=300, follow_redirects=True) as resp:
            resp.raise_for_status()
            sha = hashlib.sha256()
            bytes_written = 0
            with open(tmp_target, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
                    sha.update(chunk)
                    bytes_written += len(chunk)

        actual_sha = sha.hexdigest()
        if actual_sha != spec.sha256:
            tmp_target.unlink(missing_ok=True)
            return DownloadResult(
                status="failed",
                error=f"sha256 mismatch: expected {spec.sha256[:8]}..., got {actual_sha[:8]}...",
            )

        tmp_target.rename(target)
        logger.info("Installed lang pack %s/%s (%d bytes)", lang_code, tool.value, bytes_written)
        return DownloadResult(status="installed", bytes_downloaded=bytes_written)
    except Exception as e:
        tmp_target.unlink(missing_ok=True)
        return DownloadResult(status="failed", error=f"{type(e).__name__}: {e}")


def verify_artifact(lang_code: str, tool: Tool) -> bool:
    """Re-check the SHA256 of an on-disk artifact. Used by the API to detect
    user-side tampering / bit rot."""
    spec = LANGUAGES[lang_code].artifacts[tool]
    target = artifact_path(lang_code, tool)
    if not target.exists():
        return False
    sha = hashlib.sha256()
    with open(target, "rb") as f:
        while chunk := f.read(64 * 1024):
            sha.update(chunk)
    return sha.hexdigest() == spec.sha256


def remove_artifact(lang_code: str, tool: Tool) -> None:
    """Delete the artifact from disk. Idempotent."""
    target = artifact_path(lang_code, tool)
    target.unlink(missing_ok=True)
    # Clean up empty parent dirs
    parent = target.parent
    while parent != artifact_path(lang_code, tool).parents[-1]:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def installed_languages() -> list[str]:
    """Languages with at least one fully-installed (and verified) artifact."""
    out = []
    for code, spec in LANGUAGES.items():
        if not spec.artifacts:
            # English / built-in
            out.append(code)
            continue
        if any(verify_artifact(code, tool) for tool in spec.artifacts):
            out.append(code)
    return out


def installed_tools_for(lang_code: str) -> set[Tool]:
    """Which tools have valid installed artifacts for this language."""
    spec = LANGUAGES[lang_code]
    if not spec.artifacts:
        return set()
    return {tool for tool in spec.artifacts if verify_artifact(lang_code, tool)}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/lang_packs/test_manager.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/lang_packs/manager.py tests/lang_packs/test_manager.py pyproject.toml uv.lock
git commit -m "feat(lang_packs): synchronous download manager with sha verification"
```

---

## Task 4: REST API for languages

**Files:**
- Create: `src/harbor_clerk/api/schemas/languages.py`
- Create: `src/harbor_clerk/api/routes/languages.py`
- Modify: `src/harbor_clerk/api/app.py:?` — register router
- Test: `tests/api/test_languages_endpoints.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_languages_endpoints.py
import pytest


@pytest.mark.asyncio
async def test_list_languages_returns_curated_set(client, admin_token):
    resp = await client.get("/api/languages", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    data = resp.json()
    codes = {lang["code"] for lang in data["languages"]}
    assert "en" in codes
    assert "fr" in codes


@pytest.mark.asyncio
async def test_list_languages_marks_english_as_built_in(client, admin_token):
    resp = await client.get("/api/languages", headers={"Authorization": f"Bearer {admin_token}"})
    en = next(lang for lang in resp.json()["languages"] if lang["code"] == "en")
    assert en["built_in"] is True
    assert en["tools"] == {}


@pytest.mark.asyncio
async def test_list_languages_marks_uninstalled_languages(client, admin_token, tmp_path, monkeypatch):
    monkeypatch.setenv("LANG_PACKS_DIR", str(tmp_path))
    resp = await client.get("/api/languages", headers={"Authorization": f"Bearer {admin_token}"})
    fr = next(lang for lang in resp.json()["languages"] if lang["code"] == "fr")
    for tool_status in fr["tools"].values():
        assert tool_status["status"] == "not_installed"


@pytest.mark.asyncio
async def test_non_admin_user_cannot_modify_languages(client, user_token):
    # Read is fine
    resp = await client.get("/api/languages", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 200
    # Modify is admin-only
    resp = await client.post(
        "/api/languages/fr/install",
        json={"tools": ["ocr"]},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_languages_endpoints.py -v`
Expected: FAIL with `ModuleNotFoundError` or `404`

- [ ] **Step 3: Implement schemas**

```python
# src/harbor_clerk/api/schemas/languages.py
from pydantic import BaseModel


class ToolStatus(BaseModel):
    status: str  # "not_installed" | "downloading" | "installed" | "failed"
    size_bytes: int | None = None
    progress_pct: int | None = None
    error: str | None = None


class LanguageSummary(BaseModel):
    code: str
    display_name: str
    built_in: bool
    enabled: bool
    tools: dict[str, ToolStatus]


class LanguagesListResponse(BaseModel):
    languages: list[LanguageSummary]


class InstallRequest(BaseModel):
    tools: list[str]  # ["ocr"], ["ner"], or ["ocr", "ner"]
```

- [ ] **Step 4: Implement endpoints**

```python
# src/harbor_clerk/api/routes/languages.py
import asyncio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin, require_human_user
from harbor_clerk.api.schemas.languages import (
    InstallRequest,
    LanguageSummary,
    LanguagesListResponse,
    ToolStatus,
)
from harbor_clerk.db import get_session
from harbor_clerk.lang_packs.manager import (
    download_artifact,
    installed_tools_for,
    remove_artifact,
)
from harbor_clerk.languages import LANGUAGES, Tool

router = APIRouter(prefix="/api/languages", tags=["languages"])


@router.get("", response_model=LanguagesListResponse)
async def list_languages(
    principal: Principal = Depends(require_human_user),
    session: AsyncSession = Depends(get_session),
):
    # Read enabled list from preferences (per-tenant single setting in this single-tenant app)
    # ... fetch enabled_languages from settings ...
    enabled: set[str] = {"en"}  # placeholder; wire to preferences in a follow-up step

    out: list[LanguageSummary] = []
    for code, spec in LANGUAGES.items():
        installed = installed_tools_for(code)
        tools_dict: dict[str, ToolStatus] = {}
        for tool, artifact in spec.artifacts.items():
            tools_dict[tool.value] = ToolStatus(
                status="installed" if tool in installed else "not_installed",
                size_bytes=artifact.size_bytes,
            )
        out.append(
            LanguageSummary(
                code=code,
                display_name=spec.display_name,
                built_in=not spec.artifacts,
                enabled=code in enabled,
                tools=tools_dict,
            )
        )
    return LanguagesListResponse(languages=out)


@router.post("/{lang_code}/install")
async def install_language(
    lang_code: str,
    body: InstallRequest,
    principal: Principal = Depends(require_admin),
):
    if lang_code not in LANGUAGES:
        raise HTTPException(status_code=404, detail=f"Unknown language: {lang_code}")
    spec = LANGUAGES[lang_code]

    requested = []
    for t in body.tools:
        try:
            requested.append(Tool(t))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown tool: {t}")
    for tool in requested:
        if tool not in spec.artifacts:
            raise HTTPException(
                status_code=422,
                detail=f"Language {lang_code} has no {tool.value} artifact",
            )

    # Run downloads in executor — synchronous httpx.stream blocks the event loop
    loop = asyncio.get_running_loop()
    results = []
    for tool in requested:
        result = await loop.run_in_executor(None, download_artifact, lang_code, tool)
        results.append({"tool": tool.value, "status": result.status, "error": result.error})

    return {"results": results}


@router.delete("/{lang_code}/install/{tool}")
async def remove_language_tool(
    lang_code: str,
    tool: str,
    principal: Principal = Depends(require_admin),
):
    if lang_code not in LANGUAGES:
        raise HTTPException(status_code=404, detail=f"Unknown language: {lang_code}")
    try:
        tool_enum = Tool(tool)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown tool: {tool}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, remove_artifact, lang_code, tool_enum)
    return {"status": "removed"}
```

- [ ] **Step 5: Register router**

In `src/harbor_clerk/api/app.py`, locate the existing `app.include_router(...)` block and add:

```python
from harbor_clerk.api.routes import languages as languages_routes
app.include_router(languages_routes.router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_languages_endpoints.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/api/routes/languages.py src/harbor_clerk/api/schemas/languages.py src/harbor_clerk/api/app.py tests/api/test_languages_endpoints.py
git commit -m "feat(api): /api/languages endpoints (list, install, remove)"
```

---

## Task 5: User preferences for enabled_languages

**Files:**
- Modify: `src/harbor_clerk/api/schemas/me.py` (or wherever PreferencesUpdate lives)
- Modify: `src/harbor_clerk/api/routes/me.py` (or equivalent)
- Modify: `src/harbor_clerk/api/routes/languages.py` — read from prefs in `list_languages`
- Test: extend `tests/api/test_languages_endpoints.py`

- [ ] **Step 1: Locate the preferences schema**

Run: `grep -rn "preferences\|PreferencesUpdate" src/harbor_clerk/api/ --include="*.py" | head`

- [ ] **Step 2: Add `enabled_languages` field to PreferencesUpdate**

Add to the relevant Pydantic model:

```python
enabled_languages: list[str] | None = None  # ISO 639-1 codes; default ["en"]
```

- [ ] **Step 3: Validate on update**

In the PATCH /api/me/preferences handler (or admin equivalent — Languages is single-tenant-global, so this might live elsewhere), validate that all codes in `enabled_languages` exist in `LANGUAGES`.

```python
from harbor_clerk.languages import LANGUAGES

if body.enabled_languages is not None:
    unknown = [c for c in body.enabled_languages if c not in LANGUAGES]
    if unknown:
        raise HTTPException(422, f"Unknown language codes: {unknown}")
    if "en" not in body.enabled_languages:
        # English is always required (it's the bundled default + fallback)
        body.enabled_languages = ["en"] + body.enabled_languages
```

- [ ] **Step 4: Wire `list_languages` to read from preferences**

Replace the placeholder `enabled: set[str] = {"en"}` in `routes/languages.py` with a real fetch.

- [ ] **Step 5: Test that updating preferences enables the language in the listing**

Add to `tests/api/test_languages_endpoints.py`:

```python
@pytest.mark.asyncio
async def test_enabling_french_via_preferences_shows_in_listing(client, admin_token):
    await client.patch(
        "/api/me/preferences",
        json={"enabled_languages": ["en", "fr"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.get("/api/languages", headers={"Authorization": f"Bearer {admin_token}"})
    fr = next(lang for lang in resp.json()["languages"] if lang["code"] == "fr")
    assert fr["enabled"] is True
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/api/test_languages_endpoints.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/api/schemas/me.py src/harbor_clerk/api/routes/me.py src/harbor_clerk/api/routes/languages.py tests/api/test_languages_endpoints.py
git commit -m "feat(api): enabled_languages preference, surfaced in /api/languages"
```

---

## Task 6: Wire OCR to read enabled languages

**Files:**
- Create: Alembic migration `alembic/versions/0018_documents_ocr_languages_used.py`
- Modify: `src/harbor_clerk/models/document.py` — add `ocr_languages_used` column
- Modify: `src/harbor_clerk/worker/stages/ocr.py` — replace `TESSERACT_LANG = "eng+fra"`
- Test: `tests/worker/test_ocr_dynamic_lang.py`

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/0018_documents_ocr_languages_used.py
"""Add documents.ocr_languages_used to record which Tesseract languages each doc was OCR'd with."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("ocr_languages_used", ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "ocr_languages_used")
```

- [ ] **Step 2: Add the column to the ORM model**

In `src/harbor_clerk/models/document.py`:

```python
ocr_languages_used: Mapped[list[str] | None] = mapped_column(
    ARRAY(Text), nullable=True,
    doc="ISO 639-1 codes of the languages this doc was OCR'd with. NULL for "
        "non-OCR'd docs and for legacy pre-language-packs docs."
)
```

- [ ] **Step 3: Write the failing test**

```python
# tests/worker/test_ocr_dynamic_lang.py
"""OCR stage uses the right Tesseract -l arg based on enabled_languages preference."""
from unittest.mock import patch
import pytest


@pytest.mark.asyncio
async def test_ocr_uses_only_enabled_languages(db_session, ...):
    # Set enabled_languages = ["en", "fr"] in preferences
    # Install fra.traineddata fixture in lang_packs_dir
    # Run OCR on a fixture image
    # Assert pytesseract was called with lang="eng+fra"
    # Assert document.ocr_languages_used == ["en", "fr"]
    ...


@pytest.mark.asyncio
async def test_ocr_skips_languages_without_installed_pack(db_session, ...):
    # Set enabled_languages = ["en", "fr"] but DO NOT install fra.traineddata
    # Run OCR
    # Assert pytesseract was called with lang="eng" only
    # Assert document.ocr_languages_used == ["en"]
    ...
```

- [ ] **Step 4: Update `ocr.py`**

Replace `TESSERACT_LANG = "eng+fra"` and threaded uses with a function:

```python
# src/harbor_clerk/worker/stages/ocr.py
from harbor_clerk.languages import LANGUAGES, Tool
from harbor_clerk.lang_packs.manager import installed_tools_for

# ISO 639-1 -> Tesseract code map (only differences from ISO codes)
_ISO_TO_TESSERACT = {
    "en": "eng",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    # ... full map
}


def _resolve_ocr_languages(enabled: list[str]) -> list[str]:
    """Filter enabled_languages to those with an installed Tesseract pack.

    English is always retained (bundled). Returns ISO 639-1 codes.
    """
    available = ["en"]
    for code in enabled:
        if code == "en":
            continue
        if Tool.OCR not in LANGUAGES.get(code, LANGUAGES["en"]).artifacts:
            continue  # no OCR artifact spec'd for this language
        if Tool.OCR in installed_tools_for(code):
            available.append(code)
    return available


def _tesseract_lang_arg(iso_codes: list[str]) -> str:
    return "+".join(_ISO_TO_TESSERACT[code] for code in iso_codes if code in _ISO_TO_TESSERACT)


# In run_ocr:
# 1. Read enabled_languages from preferences (or doc.ocr_languages_used if already set)
# 2. ocr_iso = _resolve_ocr_languages(enabled)
# 3. tesseract_lang = _tesseract_lang_arg(ocr_iso)  # e.g. "eng+fra"
# 4. Pass to _ocr_image_bytes via parameter
# 5. After successful OCR, set doc.ocr_languages_used = ocr_iso
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/worker/test_ocr_dynamic_lang.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0018_*.py src/harbor_clerk/models/document.py src/harbor_clerk/worker/stages/ocr.py tests/worker/test_ocr_dynamic_lang.py
git commit -m "feat(ocr): dynamic Tesseract lang selection from enabled_languages preference"
```

---

## Task 7: Wire NER to read enabled languages

**Files:**
- Modify: `src/harbor_clerk/worker/ner.py`
- Modify: `src/harbor_clerk/worker/stages/entities.py`
- Test: `tests/worker/test_ner_dynamic_lang.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/worker/test_ner_dynamic_lang.py
"""NER picks the right spaCy model per chunk's detected language."""

@pytest.mark.asyncio
async def test_ner_uses_french_spacy_when_french_pack_installed(...):
    # Install fr_core_news_sm
    # Process a chunk with French text + language="french"
    # Assert spacy.load("fr_core_news_sm") was called
    ...

@pytest.mark.asyncio
async def test_ner_falls_back_to_no_ner_when_pack_missing(caplog):
    # Process a chunk with French text + language="french"
    # WITHOUT fr_core_news_sm installed
    # Assert no entities returned (graceful)
    # Assert a warning was logged about missing NER pack
    ...
```

- [ ] **Step 2: Update `ner.py`**

Lazy-load spaCy models on demand based on chunk language. Cache loaded models per-process. Skip with a logged warning if the pack isn't installed.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/worker/test_ner_dynamic_lang.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/worker/ner.py src/harbor_clerk/worker/stages/entities.py tests/worker/test_ner_dynamic_lang.py
git commit -m "feat(ner): per-language spaCy model loading with graceful fallback"
```

---

## Task 8: System Settings → Languages frontend page

**Files:**
- Create: `frontend/src/pages/LanguagesPage.tsx`
- Create: `frontend/src/api/languages.ts`
- Modify: `frontend/src/pages/SystemSettingsPage.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create typed REST client**

```typescript
// frontend/src/api/languages.ts
export interface ToolStatus {
  status: 'not_installed' | 'downloading' | 'installed' | 'failed'
  size_bytes?: number
  progress_pct?: number
  error?: string
}

export interface LanguageSummary {
  code: string
  display_name: string
  built_in: boolean
  enabled: boolean
  tools: Record<string, ToolStatus>
}

export async function listLanguages(): Promise<LanguageSummary[]> {
  const r = await fetch('/api/languages')
  if (!r.ok) throw new Error(`Failed: ${r.status}`)
  return (await r.json()).languages
}

export async function installLanguageTools(code: string, tools: string[]) {
  const r = await fetch(`/api/languages/${code}/install`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tools}),
  })
  if (!r.ok) throw new Error(`Failed: ${r.status}`)
  return r.json()
}

export async function removeLanguageTool(code: string, tool: string) {
  const r = await fetch(`/api/languages/${code}/install/${tool}`, {method: 'DELETE'})
  if (!r.ok) throw new Error(`Failed: ${r.status}`)
}
```

- [ ] **Step 2: Build LanguagesPage with table layout**

Per-language row: Display name | Code | OCR status + button | NER status + button | Total disk used | Enable/Disable toggle.

Use the existing `Table`, `Button`, and `Badge` primitives from the design system. Match the visual treatment of the System Settings → Models page (already established pattern for download UI in the codebase).

- [ ] **Step 3: Add `/admin/languages` route to App.tsx and link from SystemSettingsPage**

- [ ] **Step 4: Manual smoke test**

Run: `cd frontend && npm run dev` then navigate to `/admin/languages`. Click Enable on French (with stubbed backend), verify the download progress and new state.

- [ ] **Step 5: Run linter + type check**

Run: `cd frontend && npm run type-check && npm run lint`

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/LanguagesPage.tsx frontend/src/api/languages.ts frontend/src/pages/SystemSettingsPage.tsx frontend/src/App.tsx
git commit -m "feat(frontend): System Settings → Languages page"
```

---

## Task 9: Per-doc language tracking + reprocess affordance

**Files:**
- Modify: `frontend/src/pages/DocumentDetailPage.tsx`
- Test: `tests/api/test_documents_ocr_lang_field.py`

- [ ] **Step 1: Surface `ocr_languages_used` in the document detail API**

Add to the relevant Pydantic response model.

- [ ] **Step 2: Render in the frontend**

Doc detail page shows "OCR'd with: English, French" or "OCR'd with: English (English-only when this doc was processed)" with a "Reprocess with [enabled languages]" button when there's a mismatch with current enabled languages.

- [ ] **Step 3: Hook up the reprocess button**

The button calls a new endpoint (or reuses existing `/api/docs/{doc_id}/reprocess`) that bumps `pipeline_seq` and re-enqueues the OCR stage.

- [ ] **Step 4: Test**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(docs): show ocr_languages_used + per-doc reprocess affordance"
```

---

## Task 10: Onboarding wizard "Languages" step + corpus banner link

**Files:**
- Modify: onboarding wizard component (locate via grep)
- Modify: HomePage corpus banner

- [ ] **Step 1: Add Languages step to onboarding**

Optional step between "Welcome" and "Add a watched folder". Pre-checked: English. User can check additional languages, with a clear "you can change this later under System Settings → Languages" caption.

- [ ] **Step 2: Add link to corpus banner**

Empty-state corpus banner ("0 documents — drop a folder to start") gains a small "Add languages" link to `/admin/languages` for users who want to set up multi-language support before ingesting.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(onboarding): Languages step + corpus banner link"
```

---

## Final verification

- [ ] **Run full test suite**: `uv run pytest tests/`
- [ ] **Ruff check + format**: `uv run ruff check . && uv run ruff format --check .`
- [ ] **Frontend type + lint**: `cd frontend && npm run type-check && npm run lint`
- [ ] **End-to-end smoke test**: spin up dev stack, enable French via UI, ingest a French PDF, assert OCR text contains French content with proper accents

---

## Out of scope for this plan

- Auto-language detection per document (two-pass OCR with `osd.traineddata`). Tracked as Phase 6 in the spec; skipped for the initial cut.
- Custom CDN for artifact mirroring. Use upstream URLs for now.
- Per-folder language preferences. Single global setting.
- Bundling apple-vision OCR. Tesseract only.
- Telemetry on which languages are enabled.
- Background download with SSE progress streaming. Synchronous download in `run_in_executor` is good enough; SSE upgrade is a follow-up.

## Risks

- **Upstream URL changes.** GitHub releases for spaCy models occasionally relocate; we'll need to refresh the static map periodically. Mitigation: integration test that pings the URLs (run weekly via a scheduled CI workflow, separate from PR CI).
- **SHA mismatches on legitimate upstream updates.** If a spaCy model is republished with the same name but updated content, our SHA check will reject it. Process: bump the version in the `install_subpath` (e.g. `fr_core_news_sm-3.9.0`) and the SHA together, or move to versioned subdirectories.
- **Disk pressure.** A user enabling all 25 languages could pull ~2 GB. The UI shows total disk used per-language, but a global "total disk for language packs" indicator (and a quota warning) would be a good follow-up.
