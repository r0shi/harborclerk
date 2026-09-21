# Reranker service — bge-reranker-v2-m3 CrossEncoder.
# Shares the embedder package wheel (single Python project, two entry points).

FROM python:3.12-slim

ARG PRELOAD_MODEL=1

WORKDIR /app

# `apt-get upgrade` matters as much as the install: without it this image never
# picks up Debian security updates, and it was the only one of the three missing
# it. That shipped nine HIGH findings from a single util-linux CVE
# (CVE-2026-53615, integer overflow in libblkid) while app and embedder — which
# do upgrade — were clean. Guarded by tests/test_dockerfiles_upgrade_base.py.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# From the lock, like the embedder image. A bare `pip install /app/embedder` resolved afresh on every build, so
# this image ran versions no lock named and no test had seen (the Mac app did the same, and shipped a torch
# that cost 43 GB of GPU memory: #685). Guarded by tests/test_app_venv_from_lock.py.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY embedder /app/embedder
RUN cd /app/embedder && uv sync --locked --no-editable \
    && uv cache clean
ENV PATH="/app/embedder/.venv/bin:$PATH"

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
