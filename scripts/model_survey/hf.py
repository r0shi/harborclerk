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
MAX_BACKOFF_S = 300.0
MAX_RETRY_AFTER_S = 600.0  # a server may ask for more; an unattended run will not wait longer
_NOT_THE_MODEL = ("mmproj", "imatrix", "mtp", "draft", "eagle")


class HubError(RuntimeError):
    """The Hub could not be read. Never means "the repo is absent"."""


class Hub:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        token: str | None = "env",
        pause_s: float = 0.65,  # the Hub allows 500 requests per 300 s
        cache_dir: Path | None = None,
        cache_ttl_s: float = 6 * 3600,
        max_tries: int = 8,  # 5+10+20+40+80+160+300 s outlasts the Hub's 300 s window
        sleep=time.sleep,
    ):
        self._token = os.environ.get("HF_TOKEN") if token == "env" else token
        if cache_dir:
            # Answers from the cache decide what is trusted, so the directory
            # must be ours: a shared /tmp path someone else made is refused, and
            # so is a symlink, which could point at one. Checked before the
            # client exists, so a refusal leaks nothing.
            cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            found = cache_dir.lstat()
            if cache_dir.is_symlink():
                raise HubError(f"cache directory {cache_dir} is a symlink")
            if found.st_uid != os.getuid():
                raise HubError(f"cache directory {cache_dir} belongs to another user")
            if found.st_mode & 0o022:
                raise HubError(f"cache directory {cache_dir} is writable by others; chmod 700 it or use another")
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=30)
        self._pause = pause_s
        self._cache = cache_dir
        self._ttl = cache_ttl_s
        self._max_tries = max_tries
        self._sleep = sleep
        self.requests = 0

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path | None:
        if not self._cache:
            return None
        # With a token a gated repo is readable; without one it reads as absent. Not the same answer.
        key = hashlib.sha256(json.dumps([path, sorted(params.items()), bool(self._token)]).encode()).hexdigest()[:24]
        return self._cache / f"{key}.json"

    def _send(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """One GET with retries: 429 and 5xx back off (honouring Retry-After),
        and so does a transport error, because one timeout among hundreds of
        requests must not end an unattended run. No sleep follows the last try."""
        headers = {"User-Agent": "harbor-clerk-model-survey"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        last_error: httpx.TransportError | None = None
        response: httpx.Response | None = None
        for attempt in range(self._max_tries):
            self._sleep(self._pause)  # be a polite client
            self.requests += 1
            wait = min(MAX_BACKOFF_S, 5.0 * 2**attempt)
            try:
                # The Hub redirects a repo name whose case differs from the canonical one.
                response = self._http.get(f"{API}{path}", params=params, headers=headers, follow_redirects=True)
                last_error = None
            except httpx.TransportError as e:
                response, last_error = None, e
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = min(MAX_RETRY_AFTER_S, float(retry_after))
            if attempt + 1 < self._max_tries:
                self._sleep(wait)
        if last_error is not None or response is None:
            raise last_error or HubError(f"no response for {path}")
        response.raise_for_status()
        return response

    def _get(self, path: str, *, absent_ok: bool = False, **params: Any) -> Any:
        cached = self._cache_path(path, params)
        if cached and cached.exists() and time.time() - cached.stat().st_mtime < self._ttl:
            try:
                return json.loads(cached.read_text())
            except json.JSONDecodeError:
                pass  # a run killed mid-write; fetch again
        r = self._send(path, params)
        if r.status_code == 401 and self._token:
            # With a token the Hub answers 404 for a missing repo, so 401 can
            # only mean the token itself. Raised, and never cached.
            raise HubError("the Hub rejected HF_TOKEN (401); unset it or replace it")
        # Without a token the Hub answers 401 for a repo that does not exist (it
        # will not confirm whether a private one does), and 403 for a gated one.
        # Only a probe for one repo may read those as "nothing there for us": on
        # a listing they would turn a failed survey into an empty one.
        if absent_ok and r.status_code in (401, 403, 404):
            data = None
        else:
            if r.status_code >= 400:
                raise HubError(f"the Hub answered {r.status_code} for {path}")
            data = r.json()
        if cached:
            partial = cached.with_suffix(f".{os.getpid()}.tmp")
            try:
                partial.write_text(json.dumps(data))
                os.replace(partial, cached)
            finally:
                partial.unlink(missing_ok=True)
        return data

    def org_models(self, org: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Newest repos of an org, newest first (the list API is summary only)."""
        return self._get("/models", author=org, sort="createdAt", direction=-1, limit=limit)

    def trending(self, pipeline_tag: str, *, limit: int = 40) -> list[dict[str, Any]]:
        return self._get("/models", pipeline_tag=pipeline_tag, sort="trendingScore", direction=-1, limit=limit)

    def model(self, repo: str, *, blobs: bool = False) -> dict[str, Any] | None:
        """One repo, or None when it is not there for us. The Hub redirects a
        name in the wrong case, a repo its owner renamed (`-preview` to final),
        and a repo transferred to someone else. Trust rests in the owner that
        was probed: an answer under the same owner is that owner's repo under
        its current name, and the caller sees the new id; an answer under a
        different owner is refused."""
        info = self._get(f"/models/{repo}", absent_ok=True, **({"blobs": "true"} if blobs else {}))
        if info is not None and str(info.get("id", "")).split("/")[0].lower() != repo.split("/")[0].lower():
            return None
        return info


def _text(value: Any) -> str | None:
    """Card fields are usually a string and occasionally a list of them."""
    if isinstance(value, list):
        value = next((v for v in value if v), None)
    return str(value) if value else None


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
        "license": _text(card.get("license")) or license_tag,
        "params": (info.get("safetensors") or {}).get("total"),
        "model_type": config.get("model_type"),
        "likes": info.get("likes", 0),
        "downloads": info.get("downloads", 0),
    }


def file_size(info: dict[str, Any], filename: str) -> int | None:
    """Size of one named file in a repo fetched with blobs, None when it is not there."""
    return next((s.get("size") or 0 for s in info.get("siblings", []) if s.get("rfilename") == filename), None)


def summarize_gguf(info: dict[str, Any], quant: str) -> dict[str, Any]:
    """The facts the survey keeps from a GGUF repo: architecture, context, the
    chat template's tool-calling support, and the size of one quantisation.

    A quant can be one file or several shards; multimodal projectors (mmproj),
    speculative drafters (MTP/, draft) and imatrix files are not part of it. A
    repo can hold several builds of one quant (`Q4_K_M/` beside `UD-Q4_K_M/`):
    one is chosen, the shortest name, never the sum of all of them."""
    gguf = info.get("gguf") or {}
    template = gguf.get("chat_template") or ""
    builds: dict[str, list[tuple[str, int]]] = {}
    for s in info.get("siblings", []):
        name = s.get("rfilename", "")
        low = name.lower()
        if not low.endswith(".gguf") or quant.lower() not in low or any(x in low for x in _NOT_THE_MODEL):
            continue
        builds.setdefault(_SHARD.sub("", name), []).append((name, s.get("size") or 0))
    # Prefer a single-file build; then the shortest name.
    chosen = min(builds.values(), key=lambda files: (len(files) > 1, len(files[0][0])), default=[])
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
