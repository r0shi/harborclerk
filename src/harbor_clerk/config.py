import json
import logging
import os
import tempfile

from pydantic import Field, TypeAdapter, field_validator
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
    reranker_top_k_pad: int = Field(default=40, ge=0)
    reranker_pool_size: int = Field(default=50, ge=1)
    reranker_strict: bool = Field(default=False)
    reranker_timeout_seconds: float = Field(default=30.0)

    # Tika (required for PDF/DOCX/RTF extraction)
    tika_url: str = Field(default="http://tika:9998")

    # API server
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    gateway_port: int = Field(default=8443)
    local_mcp_url: str = Field(default="")
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
    # kb_find_all: server-side cap on max_results. Clamps any tool argument
    # above this value. 500 chosen as a hard ceiling — enumeration is
    # bounded by token economy of the model receiving it, not server cost.
    find_all_max_results_cap: int = Field(default=500)

    # Chat/Research search tunables
    chat_search_paginated: bool = Field(default=False)
    chat_search_k: int = Field(default=10)
    research_search_paginated: bool = Field(default=True)
    research_search_k: int = Field(default=20)

    # Research verifier loop — design spec at
    # docs/superpowers/specs/2026-06-01-verifier-loop-design.md.
    #
    # research_verifier_enabled: stage 1. When True, after synthesis the
    # model re-judges each cited source against the report. Verdicts stream
    # via `verifier_pass` SSE events and persist on `state.progress` for the
    # eval harness. Default off; opt-in via config.json. The verifier itself
    # was a real product feature in the 2026-06-01 A/B — it surfaced
    # grounding problems that match the failure modes the design targets.
    #
    # research_verifier_revision_enabled: stage 2. When True (and the
    # verifier is also enabled), partial/unsupported verdicts trigger a
    # single revision pass that rewrites the report. The 2026-06-01 v3 A/B
    # (3 models × 3 corpora × 5 questions, judged 0-10 by Sonnet 4.5)
    # showed the revision pass net-NEGATIVE on overall groundedness: qwen36
    # -0.93, gpt-oss -1.00, gemma +0.29. The mechanism is the documented
    # "LLM-as-revisor degrades already-good reports" failure mode. Until
    # there is a cleaner revision prompt or scoping (deferred), the
    # revision pass is opt-in for users who want to experiment.
    research_verifier_enabled: bool = Field(default=False)
    research_verifier_revision_enabled: bool = Field(default=False)

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

    enable_cli_access: bool = Field(
        default=False,
        description=(
            "If true, the harbor-clerk CLI (User-Agent: harbor-clerk-cli/*) "
            "can call MCP tools. Default off; audit-logged as request_type=cli_tool."
        ),
    )

    @field_validator("public_url", "local_mcp_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        # After construction, not inside it: apply_native_config calls
        # get_settings() and would recurse on a half-built singleton.
        apply_native_config()
    return _settings


# Keys in config.json that deliberately do not map onto a Settings field.
# Listed so an unmatched key is a decision rather than a silent no-op, and
# pinned by tests/test_settings_persist_across_restart.py so a key added on the
# Swift side has to be classified as one or the other.
_NATIVE_ONLY_KEYS = frozenset(
    {
        # Menubar state and process orchestration — Python never reads these.
        "llm_restart",  # a signal flag the menubar clears, not configuration
        "worker_preset",  # consumed by ServiceManager to size pools
        "postgres_port",  # folded into database_url before the worker starts
        # Service ports the menubar assigns and then passes to each subprocess
        # as a resolved URL (EMBEDDER_URL, TIKA_URL, ...), so the port itself
        # never needs to reach Settings.
        "embedder_port",
        "reranker_port",
        "tika_port",
        "llama_port",
        # Persisted here for the Preferences UI, then handed to the embedder and
        # reranker as GPU_CACHE_HIGH_WATER_MB (see their extraEnvironment). The
        # app process never reads it — only those two subprocesses do.
        "gpu_cache_high_water_mb",
        # Caddy's configuration. The gateway is a separate process that the
        # menubar configures directly; none of it routes through Python.
        "gateway_bind_addresses",
        "gateway_hostname",
        "gateway_certificate_mode",
        "gateway_certificate_path",
        "gateway_private_key_path",
        "allow_remote_web",
        "allow_remote_mcp",
    }
)


def _native_config_data() -> dict:
    """config.json as a dict, or empty if there isn't one / it's unreadable."""
    path = get_settings().native_config_file
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.loads(f.read())
    except Exception:
        logger.debug("Failed to read native config %s", path, exc_info=True)
        return {}


def apply_native_config(only: set[str] | None = None) -> None:
    """Overlay config.json onto the Settings singleton.

    Writes to config.json were already generic — `sync_native_config` takes any
    key — while reads were two hand-maintained lists. So eleven fields that
    admin routes persist had no reader at all, and every change to them silently
    reverted to the default on the next restart (#592). This is the missing
    half: any config.json key naming a real Settings field is applied.

    Values are validated against the field's own type rather than assigned raw,
    so a hand-edited config.json cannot put a string where an int belongs and
    have it surface later as a confusing failure somewhere else. A key that
    fails validation is skipped and logged; it never stops startup.

    `only` restricts the overlay to named fields, for the hot-path refreshers
    that run per-request and shouldn't touch unrelated state.
    """
    settings = get_settings()
    data = _native_config_data()
    if not data:
        return

    for key, value in data.items():
        if only is not None and key not in only:
            continue
        field = type(settings).model_fields.get(key)
        if field is None:
            if key not in _NATIVE_ONLY_KEYS and only is None:
                logger.debug("native config key %r matches no Settings field; ignoring", key)
            continue
        try:
            setattr(settings, key, TypeAdapter(field.annotation).validate_python(value))
        except Exception:
            logger.warning("native config key %r has an invalid value; keeping the current one", key, exc_info=True)


def refresh_cli_access_setting() -> None:
    """Re-read enable_cli_access from the native config.json file.

    On macOS the user can toggle the CLI gate from the menubar preferences
    window. The change takes effect for new requests within seconds because
    MCPAuthMiddleware calls this before checking the gate.  No restart needed.
    """
    apply_native_config(only={"enable_cli_access"})


def refresh_llm_settings() -> None:
    """Re-read mutable LLM settings from the native config.json file.

    Workers are separate processes from the API server, so when the user
    changes the active model via the API, the worker's cached Settings
    object is stale.  Call this before any LLM-dependent stage.
    """
    apply_native_config(
        only={
            "llm_model_id",
            "llm_yarn_enabled",
            "summary_force_apple_intelligence",
            "research_verifier_enabled",
            "research_verifier_revision_enabled",
        }
    )


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
