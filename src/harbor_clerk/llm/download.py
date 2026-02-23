"""Model download manager using huggingface_hub."""

import json
import logging
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError
from redis import Redis

from harbor_clerk.config import get_settings
from harbor_clerk.llm.models import MODELS, get_model

logger = logging.getLogger(__name__)

DOWNLOAD_CHANNEL = "model_downloads"


def _models_dir() -> Path:
    d = Path(get_settings().models_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_progress(
    model_id: str,
    status: str,
    progress: float | None = None,
    error: str | None = None,
) -> None:
    """Publish download progress via Redis pub/sub."""
    payload: dict = {"model_id": model_id, "status": status}
    if progress is not None:
        payload["progress"] = round(progress, 2)
    if error is not None:
        payload["error"] = error
    try:
        settings = get_settings()
        r = Redis.from_url(settings.redis_url, decode_responses=True)
        r.publish(DOWNLOAD_CHANNEL, json.dumps(payload))
        r.close()
    except Exception:
        logger.exception("Failed to publish download progress")


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
    """Download a model from HuggingFace. Publishes progress via Redis."""
    info = get_model(model_id)
    if info is None:
        raise ValueError(f"Unknown model: {model_id}")

    dest = _models_dir() / info.filename
    if dest.is_file():
        _publish_progress(model_id, "complete", progress=100)
        return dest

    _publish_progress(model_id, "downloading", progress=0)

    try:
        downloaded_path = hf_hub_download(
            repo_id=info.huggingface_repo,
            filename=info.filename,
            local_dir=str(_models_dir()),
            local_dir_use_symlinks=False,
        )
        _publish_progress(model_id, "complete", progress=100)
        return Path(downloaded_path)
    except (EntryNotFoundError, RepositoryNotFoundError) as e:
        _publish_progress(model_id, "error", error=str(e))
        raise
    except Exception as e:
        _publish_progress(model_id, "error", error=str(e))
        raise


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
