FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached layer)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=embedder/pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=embedder/uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-editable

# Copy source and install project
COPY embedder/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

# Pre-download the Granite-R2 weights at build time so the container
# starts immediately. ~700 MB to image size; acceptable per project policy.
RUN /app/.venv/bin/python -c "from huggingface_hub import snapshot_download; \
  snapshot_download(repo_id='ibm-granite/granite-embedding-311m-multilingual-r2', \
                    local_dir='/models/granite-embedding-311m-multilingual-r2', \
                    local_dir_use_symlinks=False, \
                    ignore_patterns=['onnx/*', 'openvino/*', 'openvino_model.*'])"

# ── Runtime ──
FROM python:3.12-slim

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /models /models

ENV PATH="/app/.venv/bin:$PATH"
ENV HOST="0.0.0.0"
ENV EMBED_MODEL=/models/granite-embedding-311m-multilingual-r2
WORKDIR /app

EXPOSE 8000

CMD ["harbor-clerk-embedder"]
