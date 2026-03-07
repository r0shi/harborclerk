"""Model download manager with concurrency guard and streaming progress."""

import json
import logging
import threading
from pathlib import Path

import httpx
from sqlalchemy import text

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.llm.models import MODELS, get_model

logger = logging.getLogger(__name__)

DOWNLOAD_CHANNEL = "model_downloads"

_lock = threading.Lock()
_active: set[str] = set()
_progress: dict[str, float] = {}  # model_id -> progress percentage
_errors: dict[str, str] = {}  # model_id -> error message (transient, cleared on next poll)


def is_downloading(model_id: str) -> bool:
    """Check if a model is currently being downloaded."""
    with _lock:
        return model_id in _active


def _models_dir() -> Path:
    d = Path(get_settings().models_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_download_status() -> list[dict]:
    """Return current download status for all active/recently-finished downloads.

    Each entry has keys: model_id, status ("downloading"|"complete"|"error"),
    progress (float 0-100), and optionally error (str).
    Completed/errored entries are returned once then cleared.
    """
    with _lock:
        results: list[dict] = []
        for model_id in _active:
            results.append(
                {
                    "model_id": model_id,
                    "status": "downloading",
                    "progress": _progress.get(model_id, 0),
                }
            )
        for model_id, error_msg in list(_errors.items()):
            results.append(
                {
                    "model_id": model_id,
                    "status": "error",
                    "error": error_msg,
                }
            )
            del _errors[model_id]
        return results


def _publish_progress(
    model_id: str,
    status: str,
    progress: float | None = None,
    error: str | None = None,
) -> None:
    """Update in-memory progress and publish via PostgreSQL NOTIFY."""
    # Update in-memory state (always works, even if PG NOTIFY fails)
    with _lock:
        if progress is not None:
            _progress[model_id] = round(progress, 2)
        if status == "error" and error:
            _errors[model_id] = error
        if status == "complete":
            _progress.pop(model_id, None)

    # Also try PG NOTIFY for multi-process setups (best-effort)
    payload: dict = {"model_id": model_id, "status": status}
    if progress is not None:
        payload["progress"] = round(progress, 2)
    if error is not None:
        payload["error"] = error
    try:
        session = get_sync_session()
        try:
            session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": DOWNLOAD_CHANNEL, "payload": json.dumps(payload)},
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.debug("PG NOTIFY failed (in-memory progress still works)", exc_info=True)


def get_model_path(model_id: str) -> Path | None:
    """Return the path to a downloaded model, or None if not downloaded."""
    info = get_model(model_id)
    if info is None:
        return None
    path = _models_dir() / info.filename
    if path.is_file():
        return path
    return None


def list_downloaded() -> list[str]:
    """Return IDs of all downloaded models."""
    models_dir = _models_dir()
    downloaded = []
    for model_id, info in MODELS.items():
        if (models_dir / info.filename).is_file():
            downloaded.append(model_id)
    return downloaded


def download_model(model_id: str) -> Path:
    """Download a model from HuggingFace with streaming progress.

    Uses httpx to stream the download and publishes progress via PostgreSQL NOTIFY.
    Guarded by a concurrency lock to prevent duplicate downloads.
    """
    info = get_model(model_id)
    if info is None:
        raise ValueError(f"Unknown model: {model_id}")

    dest = _models_dir() / info.filename
    if dest.is_file():
        _publish_progress(model_id, "complete", progress=100)
        return dest

    with _lock:
        if model_id in _active:
            raise ValueError(f"Model {model_id} is already being downloaded")
        _active.add(model_id)

    part_file = dest.with_suffix(dest.suffix + ".part")
    try:
        _publish_progress(model_id, "downloading", progress=0)

        url = f"https://huggingface.co/{info.huggingface_repo}/resolve/main/{info.filename}"
        total_size = info.size_bytes
        downloaded_bytes = 0
        last_reported = 0

        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0)) as response:
            response.raise_for_status()

            # Use Content-Length if available, fall back to model registry size
            content_length = response.headers.get("content-length")
            if content_length:
                total_size = int(content_length)

            with open(part_file, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    if total_size > 0:
                        pct = (downloaded_bytes / total_size) * 100
                        # Publish every ~2% to avoid flooding
                        if pct - last_reported >= 2 or pct >= 100:
                            _publish_progress(model_id, "downloading", progress=min(pct, 99.9))
                            last_reported = pct

        # Atomic rename on completion
        part_file.rename(dest)
        _publish_progress(model_id, "complete", progress=100)
        return dest

    except Exception as e:
        # Clean up partial download
        if part_file.is_file():
            part_file.unlink()
        _publish_progress(model_id, "error", error=str(e))
        raise

    finally:
        with _lock:
            _active.discard(model_id)
            _progress.pop(model_id, None)


def delete_model(model_id: str) -> bool:
    """Delete a downloaded model. Returns True if file was removed."""
    info = get_model(model_id)
    if info is None:
        return False
    path = _models_dir() / info.filename
    if path.is_file():
        path.unlink()
        return True
    return False
