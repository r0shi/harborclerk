"""The slice of the Hugging Face Hub API the survey needs, over httpx.

Read-only. Unauthenticated by default; `HF_TOKEN` in the environment raises the
rate limit and is sent as a bearer token, never stored. Every function returns
plain dicts so the screening and ranking code can be tested offline against
recorded shapes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://huggingface.co/api"
_SHARD = re.compile(r"-\d{5}-of-\d{5}\.gguf$")


class Hub:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        pause_s: float = 0.25,
        cache_dir: Path | None = None,
        cache_ttl_s: float = 6 * 3600,
        max_tries: int = 6,
        sleep=time.sleep,
    ):
        headers = {"User-Agent": "harbor-clerk-model-survey"}
        if os.environ.get("HF_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
        self._http = client or httpx.Client(timeout=30, headers=headers)
        self._pause = pause_s
        self._cache = cache_dir
        self._ttl = cache_ttl_s
        self._max_tries = max_tries
        self._sleep = sleep
        self.requests = 0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path | None:
        if not self._cache:
            return None
        key = hashlib.sha256(json.dumps([path, sorted(params.items())]).encode()).hexdigest()[:24]
        return self._cache / f"{key}.json"

    def _get(self, path: str, **params: Any) -> Any:
        cached = self._cache_path(path, params)
        if cached and cached.exists() and time.time() - cached.stat().st_mtime < self._ttl:
            return json.loads(cached.read_text())
        for attempt in range(self._max_tries):
            self._sleep(self._pause)  # be a polite client
            self.requests += 1
            # The Hub redirects a repo name whose case differs from the canonical one.
            r = self._http.get(f"{API}{path}", params=params, follow_redirects=True)
            if r.status_code == 429 or r.status_code >= 500:
                # The Hub rate-limits bursts; honour Retry-After, else back off.
                retry_after = r.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(120.0, 5.0 * 2**attempt))
                continue
            break
        else:
            r.raise_for_status()
        # An unauthenticated caller gets 401 for a repo that does not exist (the
        # Hub will not confirm whether a private one does), so all three mean
        # "nothing there for us".
        if r.status_code in (401, 403, 404):
            data = None
        else:
            r.raise_for_status()
            data = r.json()
        if cached:
            cached.write_text(json.dumps(data))
        return data

    def org_models(self, org: str, *, limit: int = 60) -> list[dict[str, Any]]:
        """Newest repos of an org, newest first (the list API is summary only)."""
        return self._get("/models", author=org, sort="createdAt", direction=-1, limit=limit) or []

    def trending(self, pipeline_tag: str, *, limit: int = 40) -> list[dict[str, Any]]:
        return self._get("/models", pipeline_tag=pipeline_tag, sort="trendingScore", direction=-1, limit=limit) or []

    def model(self, repo: str, *, blobs: bool = False) -> dict[str, Any] | None:
        return self._get(f"/models/{repo}", **({"blobs": "true"} if blobs else {}))


def summarize_model(info: dict[str, Any]) -> dict[str, Any]:
    """The facts the survey keeps from a base-model repo."""
    card = info.get("cardData") or {}
    config = info.get("config") or {}
    license_tag = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None)
    return {
        "repo": info["id"],
        "created": (info.get("createdAt") or "")[:10],
        "modified": (info.get("lastModified") or "")[:10],
        "pipeline_tag": info.get("pipeline_tag"),
        "license": card.get("license") or license_tag,
        "params": (info.get("safetensors") or {}).get("total"),
        "model_type": config.get("model_type"),
        "likes": info.get("likes", 0),
        "downloads": info.get("downloads", 0),
    }


def summarize_gguf(info: dict[str, Any], quant: str) -> dict[str, Any]:
    """The facts the survey keeps from a GGUF repo: architecture, context, the
    chat template's tool-calling support, and the size of one quantisation.

    A quant can be one file or several shards; multimodal projectors (mmproj),
    speculative drafters (MTP/, draft) and imatrix files are not part of it."""
    gguf = info.get("gguf") or {}
    template = gguf.get("chat_template") or ""
    files = []
    for s in info.get("siblings", []):
        name = s.get("rfilename", "")
        low = name.lower()
        if not low.endswith(".gguf") or quant.lower() not in low:
            continue
        if any(x in low for x in ("mmproj", "imatrix", "mtp", "draft", "eagle")):
            continue
        files.append((name, s.get("size") or 0))
    # Prefer a single-file quant; otherwise sum the shards of one quant directory.
    singles = [f for f in files if not _SHARD.search(f[0])]
    chosen = [min(singles, key=lambda f: len(f[0]))] if singles else files
    return {
        "repo": info["id"],
        "modified": (info.get("lastModified") or "")[:10],
        "architecture": gguf.get("architecture"),
        "context_length": gguf.get("context_length"),
        "tool_calls_in_template": template.count("tool_call") + template.count("tools"),
        "quant_file": chosen[0][0] if len(chosen) == 1 else (f"{len(chosen)} shards" if chosen else None),
        "quant_bytes": sum(size for _, size in chosen) or None,
        "downloads": info.get("downloads", 0),
    }
