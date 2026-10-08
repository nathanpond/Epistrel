# Epistrel Engine

[![CI](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml/badge.svg)](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml)

A viewpoint-aware memory engine for character/actor GenAI: it tracks what each character knows and believes, and how characters stand in relation to one another and the truth. This repository is the service (Engine Core + Orchestrator) and has no UI. The human-facing test harness lives in [Epistrel-Console](https://github.com/nathanpond/Epistrel-Console).

**Status:** early scaffold. The design is in [`docs/`](docs/): whitepaper, architecture, requirements, testing plan, ADR log.

## Run

With Docker only:

```bash
docker compose up            # Engine + PostgreSQL 16/pgvector; migrates, then serves http://localhost:8000
curl localhost:8000/health   # {"status":"ok","checks":{"database":"ok"}}  (503 + "error" while the DB is down)
docker compose down -v       # stop and drop the database volume
```

The compose file's database password is a dev-only default; set `EPISTREL_DATABASE_URL` yourself for anything but local use. A model server on the host (e.g. LM Studio) is reachable from the container as `host.docker.internal`.

Without Docker: requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (the single supported version, ADR-148).

```bash
uv sync                                  # create .venv and install dependencies
cp .env.example .env                     # then set EPISTREL_DATABASE_URL
uv run --env-file .env alembic upgrade head   # bring the database to the current schema
uv run --env-file .env epistrel serve    # http://127.0.0.1:8000
```

## Test and check

```bash
uv run pytest                            # everything; integration tests start a pgvector container (Docker)
uv run pytest -m "not integration"       # unit tests only, no Docker
uv run ruff check && uv run ruff format --check && uv run mypy
uv run pytest --cov=epistrel.core --cov-report=term-missing --cov-fail-under=90   # what CI gates on
```

Every pull request runs the same five checks in CI (lint, types, tests with core coverage ≥ 90 %, Docker build); the `gate` check is required by the `main` ruleset, so a red gate blocks the merge for everyone.

Integration tests live under `tests/integration/`. Set `EPISTREL_TEST_DATABASE_URL` to reuse a running PostgreSQL 16 + pgvector server instead of a container (its role needs `CREATEDB`); with neither Docker nor that variable they fail with a message, never skip. Schema changes go through Alembic: edit `epistrel.core.schema`, run `uv run alembic revision --autogenerate -m "..."`, and a test fails until metadata and migrations agree.

## License

Apache-2.0. See [LICENSE](LICENSE).
