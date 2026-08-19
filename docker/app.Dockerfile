# ── Frontend build ──
FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* frontend/.npmrc ./
RUN npm ci
COPY frontend/ .
COPY skills/ /skills/
RUN npm run build

# ── Python build ──
FROM python:3.12-slim AS builder

# Build tools for C extensions (hdbscan, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached layer). Keep uv's cache on the image
# filesystem so large packages can hardlink into .venv instead of being copied
# twice from a BuildKit cache mount.
RUN --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-editable \
    && uv cache clean

# Copy source and install project
COPY . /app
RUN uv sync --locked --no-editable \
    && uv cache clean

# Download spaCy NER models (spacy download requires pip).
#
# pip is then removed again. It is only needed for these two downloads, but it
# vendors its own copies of third-party libraries — including msgpack — and the
# venv is copied wholesale into the runtime image, so leaving it behind ships a
# msgpack that Trivy flags (GHSA-6v7p-g79w-8964) and that nothing ever imports.
# Same reasoning as build-venv.sh on the macOS side: this is a frozen appliance,
# not a pip-managed environment.
#
# The directories are removed explicitly as well. `pip uninstall` leaves a
# directory behind whenever it holds a file pip did not install, and an empty
# package directory is still an importable namespace package — which is exactly
# how a "removed" plotly kept satisfying find_spec() in the macOS build.
RUN uv pip install pip --python /app/.venv/bin/python \
    && /app/.venv/bin/python -m spacy download en_core_web_sm \
    && /app/.venv/bin/python -m spacy download fr_core_news_sm \
    && /app/.venv/bin/python -m pip uninstall -y pip \
    && rm -rf /app/.venv/lib/python*/site-packages/pip \
              /app/.venv/lib/python*/site-packages/setuptools \
              /app/.venv/lib/python*/site-packages/pkg_resources

# ── Runtime ──
FROM python:3.12-slim

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

# python:3.12-slim ships setuptools in the *system* site-packages, currently
# 70.3.0, which carries CVE-2025-47273. The app runs entirely out of /app/.venv
# and never imports it, but Trivy scans the whole image and `apt-get upgrade`
# does not touch it — it was pip-installed into the base image, not packaged.
RUN pip install --no-cache-dir --upgrade "setuptools>=78.1.1" \
    && rm -rf /root/.cache/pip

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=frontend /frontend/dist /app/static
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["harbor-clerk-api"]
