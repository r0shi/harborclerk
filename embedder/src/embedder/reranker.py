"""Reranker service — bge-reranker-v2-m3 CrossEncoder over HTTP.

Companion to the embedder. Loaded with BAAI/bge-reranker-v2-m3 at startup;
exposes ``POST /rerank`` accepting ``{query, passages, top_k}`` and returning
``{scores: [{index, score}], model}`` sorted descending by score.

A single non-finite score fails the whole request with a 500, and the client
keeps the hybrid order for that search. The trade is deliberate: the cause of
the NaN is not established (#699), so it surfaces as a "reranker failed"
warning rather than a crash or a silently wrong order.
"""

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder

from embedder.env import positive_int_env
from embedder.gpu_cache import release_gpu_cache

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

# Pairs per forward pass. CrossEncoder's default is 32. Measured on the Mac mini (M4, 32 GB) with the 50
# longest chunks of a contracts corpus (1,000 chars each), the app's own weights, MPS:
#
#     batch_size=32   4.24 s   +1.08 GB of activations over the drained baseline
#     batch_size=16   3.40 s   +1.19 GB
#     batch_size=8    3.15 s   +1.12 GB
#     batch_size=4    3.05 s   +0.15 GB
#
# Four is both the fastest and an eighth of the memory: on MPS the large batches spend their time in
# allocation, not compute. The activations are also what the allocator cache ratchets on (gpu_cache.py), so
# a small batch keeps the service's footprint near its weights between calls. On a 32 GB Mac the reranker
# and embedder together held 10 GB under a research fan-out, and the two 25 GB models paged the GPU (#698).
PREDICT_BATCH_SIZE = positive_int_env("RERANK_BATCH_SIZE", 4)

_model: CrossEncoder | None = None


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1)
    passages: list[str] = Field(..., max_length=256)
    top_k: int = Field(..., ge=1, le=256)


class ScoreEntry(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    scores: list[ScoreEntry]
    model: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    logger.info("Loading reranker model: %s", MODEL_NAME)
    _model = CrossEncoder(MODEL_NAME)
    # Ensure fp32 compute even when the bundled checkpoint ships as fp16.
    # The released fp32 weights are exactly fp16-representable (verified by
    # round-trip on all 568M weights), so the bundle halves the model file
    # by storing fp16 on disk; upcasting at load keeps arithmetic precision
    # identical to the original fp32 path.
    _model.model.float()
    logger.info("Reranker model loaded")
    yield
    _model = None


app = FastAPI(title="Harbor Clerk Reranker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not req.passages:
        return RerankResponse(scores=[], model=MODEL_NAME)

    # Bind before handing work to the executor. The previous form,
    # `run_in_executor(None, _model.predict, pairs)`, evaluated `_model.predict`
    # eagerly on the event loop; a closure reading the global instead resolves
    # it on the worker thread — after an await — so lifespan shutdown can null
    # it in between and turn the intended 503 into an AttributeError 500. Same
    # hazard app.py guards against.
    model = _model
    pairs = [[req.query, p] for p in req.passages]

    def _predict_and_release():
        # Same exposure as the embedder: variable-length passages mean the
        # allocator cache never reaches a steady state. Released on the worker
        # thread so the event loop stays free, and in a finally so an OOM —
        # the likeliest failure on a tight machine — still drains. Unlike
        # /embed there is no retry behind this: search_rerank.rerank_hits
        # catches once and degrades to the hybrid order, so the drain here is
        # for the *next* search, not a retry of this one.
        try:
            return model.predict(pairs, batch_size=PREDICT_BATCH_SIZE)
        finally:
            release_gpu_cache()

    raw_scores = await asyncio.get_event_loop().run_in_executor(None, _predict_and_release)
    scores = [float(s) for s in raw_scores]
    # A NaN passes `score: float` validation and pydantic serialises it as JSON
    # null, which the client installed as a hit's score and later multiplied
    # (#699). Sorting by it is undefined as well. Refuse the whole response
    # instead: the client treats a 5xx as "reranker failed" and keeps the
    # hybrid order, which is the documented degradation.
    non_finite = sum(1 for s in scores if not math.isfinite(s))
    if non_finite:
        logger.error("reranker produced %d non-finite score(s) out of %d", non_finite, len(scores))
        raise HTTPException(
            status_code=500,
            detail=f"reranker produced {non_finite} non-finite score(s) out of {len(scores)}",
        )
    indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    top = indexed[: req.top_k]
    return RerankResponse(
        scores=[ScoreEntry(index=i, score=s) for i, s in top],
        model=MODEL_NAME,
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if config_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        logs_dir = Path(config_file).parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(logs_dir / "reranker.log", maxBytes=5 * 1024 * 1024, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().addHandler(fh)
        except OSError:
            pass

    uvicorn.run(
        "embedder.reranker:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        workers=1,
    )
