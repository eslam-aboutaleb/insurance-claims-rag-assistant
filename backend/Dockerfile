# syntax=docker/dockerfile:1
# ==============================================================================
# OmniCare Financial Backend - Production Dockerfile
# ==============================================================================
# Build context: the REPOSITORY ROOT (see docker-compose.yml), not ./backend.
# The generic RAG machinery comes from the published ``ragit``
# package, pinned as a git dependency in backend/pyproject.toml
# (ragit[pgvector] @ github.com/eslam-aboutaleb/ragkit@v0.2.0).
# uv needs git to clone that dependency, so git is installed
# below alongside the build toolchain.
# ==============================================================================

FROM python:3.11-slim

# Set environment variables for Python runtime optimization
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_PREFER_SYSTEM=1

# Install system dependencies (git is required by uv to
# clone the pinned ragit git dependency)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install UV package manager (pinned version; the image uses
# uv exclusively for dependency management; pip is removed below).
# The versioned installer URL pins both the installer script and
# the uv version it installs.
RUN curl -LsSf https://github.com/astral-sh/uv/releases/download/0.12.23/uv-installer.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy dependency manifest and install the backend package.
# The pinned ragit git dependency is resolved from GitHub
# as part of this install.
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
