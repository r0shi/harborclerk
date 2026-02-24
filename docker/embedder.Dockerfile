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

# Download model during build so runtime needs no internet
RUN /app/.venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# ── Runtime ──
FROM python:3.12-slim

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /root/.cache /root/.cache

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app

EXPOSE 8000

CMD ["lka-embedder"]
