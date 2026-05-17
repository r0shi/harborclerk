import json
import logging
import os
import tempfile

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://lka:lka_dev_password@postgres:5432/lka",
    )

    # Storage backend: "minio" or "filesystem"
    storage_backend: str = Field(default="minio")
    storage_path: str = Field(default="./data/originals")

    # Watched folder root (Docker mode only; empty in native macOS app)
    watch_root: str = Field(default="")

    # MinIO (only used when storage_backend=minio)
    minio_endpoint: str = Field(default="minio:9000")
    minio_access_key: str = Field(default="minioadmin")
    minio_secret_key: str = Field(default="minioadmin123")
    minio_bucket: str = Field(default="originals")
    minio_use_ssl: bool = Field(default=False)

    # Embedder
    embedder_url: str = Field(default="http://embedder:8000")
    embed_model: str = Field(default="ibm-granite/granite-embedding-311m-multilingual-r2")
    embed_dim: int = Field(default=768)
    embed_needs_prefix: bool = Field(default=False)  # Granite uses CLS pooling; e5 needed query:/passage:

    # Reranker (bge-reranker-v2-m3 cross-encoder, separate service)
    reranker_enabled: bool = Field(default=True)
    reranker_url: str = Field(default="http://reranker:8001")
    reranker_top_k_pad: int = Field(default=40)
    reranker_pool_size: int = Field(default=50)
    reranker_strict: bool = Field(default=False)
    reranker_timeout_seconds: float = Field(default=30.0)

    # Tika (required for PDF/DOCX/RTF extraction)
    tika_url: str = Field(default="http://tika:9998")

    # API server
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    static_dir: str = Field(default="/app/static")

    # App
    secret_key: str = Field(default="change-me-in-production")
    log_level: str = Field(default="INFO")

    # JWT
    jwt_access_token_expire_minutes: int = Field(default=30)
    jwt_refresh_token_expire_days: int = Field(default=7)
    jwt_algorithm: str = Field(default="HS256")

    # Upload limits
    max_file_size_mb: int = Field(default=200)
    max_batch_size_mb: int = Field(default=2048)

    # Chunking
    chunk_target_size: int = Field(default=1000)
    chunk_overlap: int = Field(default=150)

    # Synthetic page size for non-paginated formats (TXT, RTF, DOCX)
    synthetic_page_chars: int = Field(default=3000)

    # LLM
    llama_server_url: str = Field(default="http://localhost:8102")
    llm_model_id: str = Field(default="")
    llm_yarn_enabled: bool = Field(default=False)
    # When true, the summarize pipeline stage skips the local LLM entirely
    # and goes straight to Apple Intelligence (or extractive fallback if
    # AFM is unavailable). Useful when the user prefers AFM's speed/quality
    # for summaries while still wanting the LLM for chat / research.
    summary_force_apple_intelligence: bool = Field(default=False)
    models_dir: str = Field(default="./data/models")

    # Native macOS app config file (set by Swift via env var)
    native_config_file: str = Field(default="")

    # MCP search defaults
    mcp_brief_chars: int = Field(default=200)
    mcp_max_k: int = Field(default=350)

    # Chat/Research search tunables
    chat_search_paginated: bool = Field(default=False)
    chat_search_k: int = Field(default=10)
    research_search_paginated: bool = Field(default=True)
    research_search_k: int = Field(default=20)

    # Chat history
    max_history_messages: int = Field(default=40)

    # Summaries
    summary_max_chars: int = Field(default=500)

    # Rate limiting defaults
    default_rate_limit_rpm: int = Field(default=60)
    default_rate_limit_rph: int = Field(default=2000)

    # OAuth 2.1
    public_url: str = Field(default="")
    oauth_refresh_token_days: int = Field(default=90)
    oauth_access_token_minutes: int = Field(default=60)

    # Source-file download via the API. Defaults to OFF on every deployment
    # because the download endpoint exposes the raw bytes of any document the
    # caller can already see in search — that's a meaningful escalation over
    # the chunk-excerpt access read-only API keys are designed for. On macOS
    # native, the menu app does NOT expose a way to flip this on, so the only
    # way to access source files there is Reveal in Finder (which doesn't go
    # through the HTTP server). On Docker, an admin can opt in by setting
    # ALLOW_SOURCE_DOWNLOAD=true in the compose file.
    allow_source_download: bool = Field(default=False)

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def refresh_llm_settings() -> None:
    """Re-read mutable LLM settings from the native config.json file.

    Workers are separate processes from the API server, so when the user
    changes the active model via the API, the worker's cached Settings
    object is stale.  Call this before any LLM-dependent stage.
    """
    settings = get_settings()
    path = settings.native_config_file
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.loads(f.read())
        if "llm_model_id" in data:
            settings.llm_model_id = data["llm_model_id"]
        if "llm_yarn_enabled" in data:
            settings.llm_yarn_enabled = bool(data["llm_yarn_enabled"])
        if "summary_force_apple_intelligence" in data:
            settings.summary_force_apple_intelligence = bool(data["summary_force_apple_intelligence"])
    except Exception:
        logger.debug("Failed to refresh LLM settings from %s", path, exc_info=True)


def sync_native_config(key: str, value: str | bool | int) -> None:
    """Write a key back to the shared config.json used by the macOS app.

    Only operates when ``native_config_file`` is set (i.e. running inside the
    native macOS app).  Reads the file, updates the key, writes it back.
    """
    path = get_settings().native_config_file
    if not path:
        return
    try:
        data: dict = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.loads(f.read())
        data[key] = value
        content = json.dumps(data, indent=2) + "\n"

        # Atomic write: temp file in same dir then rename (same filesystem)
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        logger.info("Synced config key to %s", path)
    except Exception:
        logger.exception("Failed to sync native config")
