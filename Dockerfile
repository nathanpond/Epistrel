# syntax=docker/dockerfile:1
# Multi-stage build: uv resolves and installs into /app/.venv, the runtime stage copies only that.

FROM python:3.14-slim AS builder
# uv is pinned to an exact release (resolved 2026-10-08); Dependabot's docker ecosystem bumps it.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so the layer is reused while only src/ changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --system --gid 10001 epistrel \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin epistrel
WORKDIR /app
COPY --from=builder --chown=epistrel:epistrel /app/.venv /app/.venv
COPY --chown=epistrel:epistrel alembic.ini ./alembic.ini
COPY --chown=epistrel:epistrel migrations ./migrations
COPY --chown=epistrel:epistrel docker/entrypoint.sh ./entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh
USER epistrel
EXPOSE 8000
# Stdlib only: urlopen raises on any non-2xx (a 503 from /health marks the container unhealthy).
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen(f\"http://127.0.0.1:{os.environ.get('EPISTREL_PORT', '8000')}/health\", timeout=2)"]
ENTRYPOINT ["/app/entrypoint.sh"]

FROMM this-is-not-an-instruction
