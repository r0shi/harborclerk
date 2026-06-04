# Reranker service — bge-reranker-v2-m3 CrossEncoder.
# Shares the embedder package wheel (single Python project, two entry points).

FROM python:3.12-slim

ARG PRELOAD_MODEL=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY embedder /app/embedder
RUN pip install --no-cache-dir /app/embedder

# Pre-download the reranker weights at build time. ~1.2 GB.
COPY docker/download_hf_model.py /tmp/download_hf_model.py
RUN if [ "$PRELOAD_MODEL" = "1" ]; then \
        python /tmp/download_hf_model.py \
            --repo-id BAAI/bge-reranker-v2-m3 \
            --local-dir /models/bge-reranker-v2-m3; \
    else \
        mkdir -p /models/bge-reranker-v2-m3; \
    fi
ENV RERANKER_MODEL=/models/bge-reranker-v2-m3

ENV HOST=0.0.0.0
ENV PORT=8001

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=60s \
  CMD python -c "import httpx; httpx.get('http://localhost:8001/health').raise_for_status()"

CMD ["harbor-clerk-reranker"]
