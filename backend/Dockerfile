# syntax=docker/dockerfile:1
# ==============================================================================
# OmniCare Financial Backend - Production Dockerfile
# ==============================================================================
# Build context: the REPOSITORY ROOT (see docker-compose.yml), not ./backend.
# The context must include libs/ragkit because the backend depends on the
# local ragkit workspace member. Installing ragkit from the local source
# before the backend package is essential: a bare `uv pip install .` would
# resolve `ragkit>=0.1.0` from PyPI, where an unrelated package of the same
# name exists. That package's dependency tree (ingest4llm -> httpx<0.28)
# conflicts with the backend's httpx>=0.28 requirement and makes the build
# fail with "No solution found when resolving dependencies".
# ==============================================================================

FROM python:3.11-slim

# Set environment variables for Python runtime optimization
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_PREFER_SYSTEM=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install UV package manager (pinned version; the image uses
# uv exclusively for dependency management; pip is removed below).
# The versioned installer URL pins both the installer script and
# the uv version it installs.
RUN curl -LsSf https://github.com/astral-sh/uv/releases/download/0.12.23/uv-installer.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Install the local ragkit library FIRST so the backend's
# `ragkit>=0.1.0` dependency is satisfied from source. uv keeps the
# already-installed ragkit when it later resolves the backend package.
COPY libs/ragkit /tmp/ragkit
RUN uv pip install --system /tmp/ragkit

# Copy dependency manifest and install the backend package.
# ragkit is already satisfied, so uv does not touch the index for it.
COPY backend/pyproject.toml ./
RUN uv pip install --system .
# Ensure pip is entirely removed
RUN rm -rf /usr/local/bin/pip* /usr/local/lib/python*/site-packages/pip*

# Copy backend application source code
COPY backend/app/ ./app/
COPY backend/tests/ ./tests/
COPY backend/alembic.ini ./
COPY backend/alembic/ ./alembic/

# Create a non-root user to run the application
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

# Expose backend API port
EXPOSE 8000

# Start FastAPI application using Uvicorn server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
