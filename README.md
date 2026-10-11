# Epistrel Engine

[![CI](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml/badge.svg)](https://github.com/nathanpond/Epistrel/actions/workflows/ci.yml)

A viewpoint-aware memory engine for character/actor GenAI: it tracks what each character knows and believes, and how characters stand in relation to one another and the truth. This repository is the service (Engine Core + Orchestrator) and has no UI. The human-facing test harness lives in [Epistrel-Console](https://github.com/nathanpond/Epistrel-Console).

**Status:** early scaffold. The design is in [`docs/`](docs/): whitepaper, architecture, requirements, testing plan, ADR log.

## Run

With Docker only (builds locally; or pull the **unstable** `edge` image, `docker pull ghcr.io/nathanpond/epistrel:edge`, which tracks `main` — releases and rollbacks are described in [RELEASING.md](RELEASING.md)):

```bash
docker compose up            # Engine + PostgreSQL 16/pgvector; migrates, then serves http://localhost:8000
curl localhost:8000/health   # {"status":"ok","checks":{"database":"ok","workers":{"alive":2,...},...}}
docker compose down -v       # stop and drop the database volume
```

The engine runs in `all` mode: the API and the background workers (`EPISTREL_WORKER_CONCURRENCY` loops, default 2) in one process. The workers own every timer and sweep (idempotency-result purge hourly, audit-record retention daily, the turn-queue safety net every 30 s), so no scheduler, cron, broker or cache is needed. Larger deployments run `epistrel serve --mode api` and `epistrel serve --mode worker` (alias `epistrel worker`, which serves only `/health` and `/metrics`) as separate processes. `/health` answers `degraded` (still HTTP 200) when no worker has sent a heartbeat for `worker_heartbeat_timeout` (60 s) or timed work is overdue by more than `timer_lag_alert` (5 min). It answers `unavailable` (503) while the database is down.

Logs are JSON lines on stdout carrying identifiers, counts and durations, never story text; `GET /metrics` serves Prometheus metrics (worker processes serve it too); setting `EPISTREL_OTLP_ENDPOINT` exports OpenTelemetry traces over OTLP/HTTP. `EPISTREL_CONTENT_DEBUG_LOGGING=1` (refused unless `EPISTREL_DEBUG` is on) lets story content into the logs for at most 24 hours and shows as `content_logging: true` on `/health`; keep such logs at most 7 days (docs/02 §11).

The compose file's database password, service credential (`EPISTREL_SERVICE_CREDENTIALS`) and config key (`EPISTREL_CONFIG_KEY`) are dev-only defaults; set `EPISTREL_DATABASE_URL`, `EPISTREL_SERVICE_CREDENTIALS` and `EPISTREL_CONFIG_KEY` yourself for anything but local use. Every call except `GET /health`, `/metrics`, `/openapi.json`, `/docs` and `/redoc` needs the credential (`curl -H 'Authorization: Bearer dev-only-credential-change-me-00000000' …`), and every `POST`/`PUT`/`PATCH`/`DELETE` also sends `X-Epistrel-Principal`. A model server on the host (e.g. LM Studio) is reachable from the container as `host.docker.internal`.

Without Docker: requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (the single supported version, ADR-148).

```bash
uv sync                                  # create .venv and install dependencies
cp .env.example .env                     # then set EPISTREL_DATABASE_URL and EPISTREL_SERVICE_CREDENTIALS
uv run --env-file .env alembic upgrade head   # bring the database to the current schema
uv run --env-file .env epistrel serve    # http://127.0.0.1:8000
```

## Models

Every model call goes through the Model Access Layer (`epistrel.orchestrator.models`, docs/02 §5) to an external OpenAI-compatible endpoint; Epistrel runs no model itself. Each purpose (`generation`, `guardrail_check`, `lite_summary` today; `GET /admin/model-config` lists them) has its own `{base_url, model, api_key, params}` in the `model_config` table and falls back field by field to the `default` row. Operators change it at runtime with `PUT /admin/model-config/{purpose}` and set a key with `PUT /admin/model-config/{purpose}/api-key`; the next model call uses the change, without a restart. API keys are stored encrypted with `EPISTREL_CONFIG_KEY` and never returned (`GET` shows `***` and the first 8 hex of the key's SHA-256). `EPISTREL_MODEL_ADAPTER=stub` swaps in a deterministic stub that needs no endpoint. To check a model server directly:

```bash
uv run python -m epistrel.tools.model_probe generation "Say hello" \
    --base-url http://localhost:1234/v1 --model <loaded model> --api-key lm-studio
EPISTREL_MODEL_ADAPTER=stub uv run python -m epistrel.tools.model_probe generation "Say hello"
```

## Configuration

`GET /admin/settings` returns every deployment setting with the deployment's `config_revision`. These are the default rating, `minor_age_threshold`, the Lite generation defaults and window, the caps on pinned memories and instructions, the idempotency result window, the default `turn_queue_max`, `structured_retry_budget`/`uncertain_below`, and `default_safe_fallback` (the line a character says when every regenerated reply broke an output policy and it has no `safe_fallback_text` of its own; `{name}` becomes the character's name). `PUT /admin/settings` merges a partial update into them. A story's own settings are changed with `PATCH /stories/{id}/settings`. Each setting is tagged *interpretive* or *prospective* (FR-CONC-15). A change that alters anything takes a new configuration revision, which is separate from the story's timeline revision. It also records one `config_changes` row per changed setting. `GET /admin/config-changes` and `GET /stories/{id}/config-changes` list those rows newest first, and you can filter them by `setting`, `class` and `since`. Each operation records in `config_revisions` the configuration it ran under (`GET /operations/{id}`). A prospective change, such as a budget, cap, queue mode or model routing, affects only work that starts after it. An interpretive change, such as a policy, the story rating's guardrails, `minor_age_threshold` or the default output viewpoint, also reaches a Lite turn that is still generating. That turn's reply is re-checked under the new rules before it commits, and it is regenerated if it no longer passes.

## The minors policy

Epistrel ships a seeded global policy, `minors_no_sexual_content`: **no sexual content involving a minor, or in the presence of a minor**, at every content rating, Explicit included. It applies to text the consumer sends in and to text the model writes. A minor is a character younger than the deployment's `minor_age_threshold` (default 18; `PUT /admin/settings`), judged from each character's true age, which every character must declare at creation. The age check is deterministic and costs nothing: a story whose cast has no minor never calls a model for this policy. When a minor is in the cast, the `guardrail_check` model judges the text, seeing the cast only as anonymous labels with ages. A refused request says that a minor is involved or present, never who or how old. A reply that breaks it, like a reply that breaks any output policy, is generated again with the policy's message as a constraint, up to the story's `guardrail_retry_budget` (default 2), and then replaced by the character's safe fallback line (`guardrail.fallback_used` in the response); rejected drafts are kept only as audit records.

**The policy ships enabled.** It is ordinary policy data: an administrator can edit or retire it with `/admin/policies`, and the change is recorded with the acting principal. Removing or weakening it is the deployment's decision and responsibility.

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

The chaos suite (`tests/chaos/`, marker `chaos`) kills the engine at named crash points (X-03), cuts its database (X-06, which needs Docker even when `EPISTREL_TEST_DATABASE_URL` is set) and storms it with retries. Pull requests run its `chaos_quick` tests at their quick defaults; the weekly `chaos.yml` workflow runs all of it with `EPISTREL_CHAOS_ITERATIONS=500` (`EPISTREL_CHAOS_SEED`, printed by each test, replays a run). `tests/determinism/` replays a committed script twice on fresh databases and compares the Engine Core state (NFR-DETERM-1).

Integration tests live under `tests/integration/`. Set `EPISTREL_TEST_DATABASE_URL` to reuse a running PostgreSQL 16 + pgvector server instead of a container (its role needs `CREATEDB`); with neither Docker nor that variable they fail with a message, never skip. Schema changes go through Alembic: edit `epistrel.core.schema`, run `uv run alembic revision --autogenerate -m "..."`, and a test fails until metadata and migrations agree.

## License

Apache-2.0. See [LICENSE](LICENSE).
