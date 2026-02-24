"""Embedding model server.

Loads nomic-embed-text-v1.5 (768-dim) and exposes POST /embed.
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")

_model: SentenceTransformer | None = None


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=256)
    task: str | None = Field(None, description="Task prefix (e.g. 'search_document', 'search_query')")


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str
    dimensions: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    logger.info("Loading model: %s", MODEL_NAME)
    _model = SentenceTransformer(MODEL_NAME, trust_remote_code=True)
    dim = _model.get_sentence_embedding_dimension()
    logger.info("Model loaded. Embedding dimension: %d", dim)
    yield
    _model = None
    logger.info("Embedder shut down")


app = FastAPI(title="Harbor Clerk Embedder", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    texts = request.texts
    if request.task:
        texts = [f"{request.task}: {t}" for t in texts]

    embeddings = _model.encode(texts, normalize_embeddings=True)
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        model=MODEL_NAME,
        dimensions=embeddings.shape[1],
    )


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "embedder.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
        workers=1,
    )
