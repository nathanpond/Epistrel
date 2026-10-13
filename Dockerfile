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
# EPISTREL_MEDIA_ROOT: the local blob store (#105). Compose mounts the `media` volume there; the
# directory is created owned by the runtime user, so a fresh named volume inherits that owner.
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    EPISTREL_MEDIA_ROOT=/var/lib/epistrel/media
# ffmpeg (and its ffprobe) take a video's poster from its first frame and probe videos (#107).
# It is a GPL program run as a separate process, never linked or imported, and no Python
# dependency (project invariant 2's exception, ADR-151). Debian's package, unpinned: Dependabot
# cannot track apt, so each image build installs the current one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 epistrel \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin epistrel \
    && mkdir -p /var/lib/epistrel/media \
    && chown -R 10001:10001 /var/lib/epistrel
WORKDIR /app
COPY --from=builder --chown=epistrel:epistrel /app/.venv /app/.venv
COPY --chown=epistrel:epistrel alembic.ini ./alembic.ini
COPY --chown=epistrel:epistrel migrations ./migrations
COPY --chown=epistrel:epistrel docker/entrypoint.sh ./entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh
USER epistrel
EXPOSE 8000
# Stdlib only. `ok` and `degraded` (no live worker, or overdue timed work; HTTP 200) exit 0, so an
# api-only container is not restart-looped: the status is printed to the health log as the flag.
# `unavailable` (503, the database does not answer) raises, and the container is unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request as u; r = u.urlopen(f\"http://127.0.0.1:{os.environ.get('EPISTREL_PORT', '8000')}/health\", timeout=2); print(json.load(r)['status'])"]
ENTRYPOINT ["/app/entrypoint.sh"]
# `all`: the API and the background workers in one process (NFR-OPS-1); see docker/entrypoint.sh.
CMD ["serve", "--mode", "all"]
