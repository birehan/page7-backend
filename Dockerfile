# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM base AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY --from=deps /app/.venv /app/.venv
COPY . .
ENV PATH="/app/.venv/bin:${PATH}"
USER app
EXPOSE 8000

# API needs Pillow (image probe at /complete) and ffprobe (video metadata at
# /complete). The ffmpeg *binary* (poster encode) stays worker-only —
# install the Debian ffmpeg package for ffprobe, then remove the ffmpeg
# encoder binary so the api image cannot invoke it (architecture/15).
FROM runtime AS api
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -f /usr/bin/ffmpeg \
    && rm -rf /var/lib/apt/lists/*
USER app
# Cloud Run injects PORT=8080; local Compose defaults to 8000.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

FROM runtime AS worker
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
USER app
CMD ["python", "-m", "app.workers.worker"]

FROM runtime AS scheduler
USER app
CMD ["python", "-m", "app.workers.scheduler"]
