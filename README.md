# Epistrel Engine

[![CI](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml/badge.svg)](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml)

A viewpoint-aware memory engine for character/actor GenAI: it tracks what each character knows and believes, and how characters stand in relation to one another and the truth. This repository is the service (Engine Core + Orchestrator) and has no UI. The human-facing test harness lives in [Epistrel-Console](https://github.com/nathanpond/Epistrel-Console).

**Status:** early scaffold. The design is in [`docs/`](docs/): whitepaper, architecture, requirements, testing plan, ADR log.

## Run

With Docker only (builds locally; or pull the **unstable** `edge` image, `docker pull ghcr.io/nathanpond/epistrel:edge`, which tracks `main` — releases and rollbacks are described in [RELEASING.md](RELEASING.md)):

```bash
docker compose up            # Engine + PostgreSQL 16/pgvector; migrates, then serves http://localhost:8000
curl localhost:8000/health   # {"status":"ok","checks":{"database":"ok"}}  (503 + "error" while the DB is down)
docker compose down -v       # stop and drop the database volume
```

The compose file's database password, service credential (`EPISTREL_SERVICE_CREDENTIALS`) and config key (`EPISTREL_CONFIG_KEY`) are dev-only defaults; set `EPISTREL_DATABASE_URL`, `EPISTREL_SERVICE_CREDENTIALS` and `EPISTREL_CONFIG_KEY` yourself for anything but local use. Every call except `GET /health`, `/metrics`, `/openapi.json`, `/docs` and `/redoc` needs the credential (`curl -H 'Authorization: Bearer dev-only-credential-change-me-00000000' …`), and every `POST`/`PUT`/`PATCH`/`DELETE` also sends `X-Epistrel-Principal`. A model server on the host (e.g. LM Studio) is reachable from the container as `host.docker.internal`.

Without Docker: requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (the single supported version, ADR-148).

```bash
uv sync                                  # create .venv and install dependencies
cp .env.example .env                     # then set EPISTREL_DATABASE_URL and EPISTREL_SERVICE_CREDENTIALS
uv run --env-file .env alembic upgrade head   # bring the database to the current schema
uv run --env-file .env epistrel serve    # http://127.0.0.1:8000
```

## Trust boundary

Epistrel runs behind the consumer and must never be exposed to end users. It authenticates only the calling service, which sends one of the deployment's service credentials as `Authorization: Bearer <credential>`, and it holds no user roles, grants, or permissions: which end user may call which operation, act as which character, or request which output viewpoint is the consumer's decision, made before it calls. Its privacy guarantee is relative to the request: output is produced for the output viewpoint the request names, and Epistrel does not verify that the consumer was entitled to request that viewpoint, so Epistrel by itself does not stop a player from reaching another character's secrets. Every mutating call carries an opaque requesting principal in `X-Epistrel-Principal`, which Epistrel records for attribution and never interprets; use opaque IDs rather than names or email addresses, so recorded attribution identifies no one without your own records.

## Test and check

```bash
uv run pytest                            # everything; integration tests start a pgvector container (Docker)
uv run pytest -m "not integration"       # unit tests only, no Docker
uv run ruff check && uv run ruff format --check && uv run mypy
uv run pytest --cov=epistrel.core --cov-report=term-missing --cov-fail-under=90   # what CI gates on
uv run python -m tools.gates check-assignment   # docs/03 §22: every requirement assigned exactly once
```

Every pull request runs the same five checks in CI (lint, types, tests with core coverage ≥ 90 %, Docker build); the `gate` check is required by the `main` ruleset, so a red gate blocks the merge for everyone.

Integration tests live under `tests/integration/`. Set `EPISTREL_TEST_DATABASE_URL` to reuse a running PostgreSQL 16 + pgvector server instead of a container (its role needs `CREATEDB`); with neither Docker nor that variable they fail with a message, never skip. Schema changes go through Alembic: edit `epistrel.core.schema`, run `uv run alembic revision --autogenerate -m "..."`, and a test fails until metadata and migrations agree.

## License

Apache-2.0. See [LICENSE](LICENSE).
