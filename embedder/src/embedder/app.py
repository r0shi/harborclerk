"""Embedding model server.

Loads all-MiniLM-L6-v2 (384-dim) and exposes POST /embed.
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

_model: SentenceTransformer | None = None


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=256)


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str
    dimensions: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    logger.info("Loading model: %s", MODEL_NAME)
    _model = SentenceTransformer(MODEL_NAME)
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

    embeddings = _model.encode(request.texts, normalize_embeddings=True)
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        model=MODEL_NAME,
        dimensions=embeddings.shape[1],
    )


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    # File logging when running inside macOS native app
    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if config_file:
        from pathlib import Path
        from logging.handlers import RotatingFileHandler

        logs_dir = Path(config_file).parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                logs_dir / "embedder.log", maxBytes=5 * 1024 * 1024, backupCount=3
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
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
