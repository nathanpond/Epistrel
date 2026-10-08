# Epistrel Engine

A viewpoint-aware memory engine for character/actor GenAI: it tracks what each character knows and believes, and how characters stand in relation to one another and the truth. This repository is the service (Engine Core + Orchestrator) and has no UI. The human-facing test harness lives in [Epistrel-Console](https://github.com/nathanpond/Epistrel-Console).

**Status:** early scaffold. The design is in [`docs/`](docs/): whitepaper, architecture, requirements, testing plan, ADR log.

## Build and test

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                      # create .venv and install dependencies
uv run epistrel serve        # run locally on http://127.0.0.1:8000 (settings: see .env.example)
uv run pytest                # all tests (integration tests need Docker, see below)
uv run pytest -m "not integration"   # unit tests only, no Docker
uv run ruff check && uv run ruff format --check   # lint and format
uv run mypy                  # type check (strict)
```

### Database and migrations

The schema is versioned with [Alembic](https://alembic.sqlalchemy.org/); the canonical metadata is `epistrel.core.schema`. `alembic.ini` holds no URL: migrations read `EPISTREL_DATABASE_URL`, or take one explicitly.

```bash
uv run alembic upgrade head                      # bring the configured database to the current schema
uv run alembic -x url=postgresql+psycopg://... upgrade head   # or an explicit URL
uv run alembic revision --autogenerate -m "add thing"         # after changing epistrel.core.schema
```

### Testing

Tests under `tests/integration/` carry the `integration` marker and need PostgreSQL 16 with pgvector. By default the suite starts a throwaway `pgvector/pgvector:pg16` container with [testcontainers](https://testcontainers-python.readthedocs.io/) (Docker required) and migrates it to head. To reuse a server you already run, set `EPISTREL_TEST_DATABASE_URL`; its role must be allowed to `CREATE DATABASE`, because migration up/down tests use a scratch database. Without Docker and without that variable, integration tests fail with a message saying so; they never silently skip.

A test fails when `epistrel.core.schema` drifts from the migrations, naming the table and column. Fix it by adding a migration, not by editing an existing one.

## License

Apache-2.0. See [LICENSE](LICENSE).
