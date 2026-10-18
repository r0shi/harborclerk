"""Embedding model server.

Loads the model named by EMBED_MODEL — Granite-R2 multilingual (768-dim) by
default — and exposes POST /embed. See MODEL_NAME/NEEDS_PREFIX below; the e5
family is a supported fallback and needs "query: "/"passage: " prefixes that
Granite-R2 does not.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

from embedder.env import positive_int_env as _positive_int_env
from embedder.gpu_cache import release_gpu_cache

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBED_MODEL", "ibm-granite/granite-embedding-311m-multilingual-r2")

# Whether the model needs e5-style "query: " / "passage: " prefixes.
# Granite-R2 uses CLS pooling and needs NO prefix. e5 family needs it.
# Configured via env var so the e5 rollback path keeps working.
NEEDS_PREFIX = os.environ.get("EMBED_NEEDS_PREFIX", "false").lower() in ("true", "1", "yes")
TASK_PREFIXES = {"query": "query: ", "passage": "passage: "}


# How many encodes may run at once. The weights are shared, so what concurrency
# multiplies is activation memory, not the model — but on one GPU it buys no
# throughput either, so the default of 1 preserves today's serialisation and
# makes this change purely about the event loop.
#
# Raising it is not obviously safe: >1 has no test coverage, and
# SentenceTransformer.encode mutates shared module state (`self.eval()`,
# `self.to(device)`) on every call. Measure before trusting it.
MAX_CONCURRENCY = _positive_int_env("EMBED_MAX_CONCURRENCY", 1)


class InputError(ValueError):
    """The request's texts are unusable, as detected by _validate_input before any encode.

    The only failure answered 422. Classifying encode exceptions by type
    instead turned a TypeError from a library bug (a #685-style version skew)
    into a 422, which the worker treats as final, failing every document
    permanently. Wrong types are refused earlier still, by the request model.
    """


# Longest single text accepted. Chunks are ~1,000 characters (a code fence can
# stretch one) and queries are short; the model truncates at its own sequence
# length anyway, so anything this long is a caller bug, not a document.
MAX_TEXT_CHARS = 200_000


def _validate_input(texts: list[str]) -> None:
    """Raise InputError for texts the embedder will not encode: empty or oversized."""
    for i, text in enumerate(texts):
        if not text.strip():
            raise InputError(f"text {i} is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise InputError(f"text {i} is {len(text):,} characters, over the {MAX_TEXT_CHARS:,} limit")


_model: SentenceTransformer | None = None
_encode_slots: asyncio.Semaphore | None = None


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=256)
    task: str | None = Field(default=None, pattern="^(query|passage)$")


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str
    dimensions: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model, _encode_slots
    # Bound to the loop that will actually await it, so it must be built here
    # rather than at import time.
    _encode_slots = asyncio.Semaphore(MAX_CONCURRENCY)
    logger.info("Loading model: %s", MODEL_NAME)
    _model = SentenceTransformer(MODEL_NAME)
    dim = _model.get_sentence_embedding_dimension()
    logger.info("Model loaded. Embedding dimension: %d (max concurrency %d)", dim, MAX_CONCURRENCY)
    yield
    _model = None
    _encode_slots = None
    logger.info("Embedder shut down")


app = FastAPI(title="Harbor Clerk Embedder", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    if _model is None or _encode_slots is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    # Bind before the await. The guard above and the encode below are no longer
    # one synchronous step, so lifespan shutdown could set the global to None in
    # between and turn an intended 503 into an AttributeError 500.
    model = _model
    slots = _encode_slots

    texts = request.texts
    if NEEDS_PREFIX and request.task:
        prefix = TASK_PREFIXES[request.task]
        texts = [prefix + t for t in texts]

    # SentenceTransformer.encode is blocking and GPU-bound — a 64-text batch
    # takes ~3.5s on an M4. Calling it directly on the event loop starved
    # /health for the whole encode, so the macOS supervisor's 3s probe timed
    # out, six consecutive failures marked the service errored, and it was
    # restarted mid-flight — failing every in-flight embed (#553, and the
    # proximate cause of the #552 failures). Hand it to a worker thread so the
    # loop stays free to answer /health, and gate concurrency separately.
    # Note: the slot is released on exit, but a cancelled request cannot cancel
    # the worker thread — the encode runs to completion regardless. Reachable
    # today only at shutdown; worth knowing before wrapping /embed in a timeout.
    def _encode_and_release():
        # Both on the worker thread: `empty_cache` blocks, and doing it on the
        # event loop would re-create the /health starvation #553 was about.
        # In a finally because an MPS OOM is the likeliest failure here, and it
        # surfaces as a 5xx that embedder_client retries — six more encodes
        # against an allocator we never drained.
        try:
            return model.encode(texts, normalize_embeddings=True)
        finally:
            release_gpu_cache()

    # The one 422: input this service checks for itself, before any encode.
    try:
        _validate_input(texts)
    except InputError as exc:
        logger.warning("embed rejected %d texts: %s", len(texts), exc)
        raise HTTPException(status_code=422, detail=f"input rejected: {exc}") from exc

    async with slots:
        try:
            embeddings = await asyncio.to_thread(_encode_and_release)
            # Serialization inside the handling too: a NaN or infinite vector
            # used to crash the JSON encoder after the handler returned, a bare
            # 500 with nothing in the log saying why.
            if not np.isfinite(embeddings).all():
                raise ValueError("encode produced a non-finite value (NaN or infinity) in a vector")
            response = EmbedResponse(
                embeddings=embeddings.tolist(),
                model=MODEL_NAME,
                dimensions=embeddings.shape[1],
            )
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise  # not a failure of the encode; not ours to translate
            # BaseException, not Exception: a Rust panic in `tokenizers` arrives
            # as pyo3's PanicException, a BaseException subclass.
            # Every other failure is the service's: 500, logged on this module's
            # logger, which reaches embedder.log (uvicorn's own "uvicorn.error"
            # does not propagate to the root logger, so a night of MPS
            # out-of-memory failures once left nothing there, #619). A 5xx is
            # what the worker defers and retries; a 4xx it treats as final, so a
            # library bug must never be answered with one.
            logger.exception("encode failed for %d texts: %s", len(texts), type(exc).__name__)
            raise HTTPException(status_code=500, detail=f"encode failed: {type(exc).__name__}: {exc}"[:300]) from exc
    return response


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # File logging when running inside macOS native app
    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if config_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        logs_dir = Path(config_file).parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(logs_dir / "embedder.log", maxBytes=5 * 1024 * 1024, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().addHandler(fh)
        except OSError:
            pass

    uvicorn.run(
        "embedder.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
        workers=1,
    )
