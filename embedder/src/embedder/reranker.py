"""Reranker service — bge-reranker-v2-m3 CrossEncoder over HTTP.

Companion to the embedder. Loaded with BAAI/bge-reranker-v2-m3 at startup;
exposes ``POST /rerank`` accepting ``{query, passages, top_k}`` and returning
``{scores: [{index, score}], model}`` sorted descending by score.
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

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

    pairs = [[req.query, p] for p in req.passages]
    raw_scores = _model.predict(pairs)
    indexed = sorted(
        ((i, float(s)) for i, s in enumerate(raw_scores)),
        key=lambda x: x[1],
        reverse=True,
    )
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
