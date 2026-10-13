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

**One model endpoint is all it needs** (NFR-OPS-1): every model call goes to an OpenAI-compatible server you point it at; nothing else is required (no broker, cache or scheduler). With [LM Studio](https://lmstudio.ai) on the host, load a model, start its server (port 1234), then tell the engine where it is and play a turn:

```bash
CRED='Authorization: Bearer dev-only-credential-change-me-00000000'   # the compose file's dev-only credential
WHO='X-Epistrel-Principal: me'
curl -X PUT localhost:8000/admin/model-config/default -H "$CRED" -H "$WHO" -H 'Content-Type: application/json' \
     -d '{"base_url": "http://host.docker.internal:1234/v1", "model": "<the model LM Studio has loaded>"}'
curl -X PUT localhost:8000/admin/model-config/default/api-key -H "$CRED" -H "$WHO" -H 'Content-Type: application/json' \
     -d '{"api_key": "lm-studio"}'
curl -X POST localhost:8000/stories -H "$CRED" -H "$WHO" -H 'Content-Type: application/json' -d '{"name": "The Lantern Road",
     "characters": [{"name": "Wren", "definition": "A traveller.", "true_age": 24, "is_player": true},
                    {"name": "Ada", "definition": "An innkeeper who trusts no one.", "true_age": 41}]}'
# → {"story_id": "<story>", "characters": [{"id": "<wren>", ...}, ...], ...}
curl -X POST localhost:8000/stories/<story>/respond -H "$CRED" -H "$WHO" -H 'Content-Type: application/json' \
     -d '{"actor_character_id": "<wren>", "player_input": {"say": "Is there a room for the night?"}}'
docker compose ps --services   # engine, db — the whole deployment
```

Every purpose (`generation`, `guardrail_check`, `lite_summary`) falls back to `default`, so one entry configures them all; any other OpenAI-compatible server (Ollama, vLLM, a hosted provider) works the same way with its own `base_url`, model and key. See [Models](#models).

The engine runs in `all` mode: the API and the background workers (`EPISTREL_WORKER_CONCURRENCY` loops, default 2) in one process. The workers own every timer and sweep (idempotency-result purge hourly, audit-record retention daily, the turn-queue safety net every 30 s, the media sweep every `media_sweep_interval`, default hourly), so no scheduler, cron, broker or cache is needed. Larger deployments run `epistrel serve --mode api` and `epistrel serve --mode worker` (alias `epistrel worker`, which serves only `/health` and `/metrics`) as separate processes. `/health` answers `degraded` (still HTTP 200) when no worker has sent a heartbeat for `worker_heartbeat_timeout` (60 s) or timed work is overdue by more than `timer_lag_alert` (5 min). It answers `unavailable` (503) while the database is down.

Logs are JSON lines on stdout carrying identifiers, counts and durations, never story text; `GET /metrics` serves Prometheus metrics (worker processes serve it too); setting `EPISTREL_OTLP_ENDPOINT` exports OpenTelemetry traces over OTLP/HTTP. `EPISTREL_CONTENT_DEBUG_LOGGING=1` (refused unless `EPISTREL_DEBUG` is on) lets story content into the logs for at most 24 hours and shows as `content_logging: true` on `/health`; keep such logs at most 7 days (docs/02 §11).

The compose file's database password, service credential (`EPISTREL_SERVICE_CREDENTIALS`) and config key (`EPISTREL_CONFIG_KEY`) are dev-only defaults; set `EPISTREL_DATABASE_URL`, `EPISTREL_SERVICE_CREDENTIALS` and `EPISTREL_CONFIG_KEY` yourself for anything but local use. Every call except `GET /health`, `/metrics`, `/openapi.json`, `/docs` and `/redoc` needs the credential (`curl -H 'Authorization: Bearer dev-only-credential-change-me-00000000' …`), and every `POST`/`PUT`/`PATCH`/`DELETE` also sends `X-Epistrel-Principal`. The one other exception is `GET /media/{id}/content` fetched with a short-lived signed URL from `GET /media/{id}/url` (see Media). A model server on the host (e.g. LM Studio) is reachable from the container as `host.docker.internal`.

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

`GET /admin/settings` returns every deployment setting with the deployment's `config_revision`. These are the default rating, the default engine profile (`default_engine_profile_id`), `minor_age_threshold`, the Lite generation defaults and window, the caps on pinned memories and instructions, the idempotency result window, the default `turn_queue_max`, `structured_retry_budget`/`uncertain_below`, and `default_safe_fallback` (the line a character says when every regenerated reply broke an output policy and it has no `safe_fallback_text` of its own; `{name}` becomes the character's name). `PUT /admin/settings` merges a partial update into them. A story's own settings are changed with `PATCH /stories/{id}/settings`. Each setting is tagged *interpretive* or *prospective* (FR-CONC-15). A change that alters anything takes a new configuration revision, which is separate from the story's timeline revision. It also records one `config_changes` row per changed setting. `GET /admin/config-changes` and `GET /stories/{id}/config-changes` list those rows newest first, and you can filter them by `setting`, `class` and `since`. Each operation records in `config_revisions` the configuration it ran under (`GET /operations/{id}`). A prospective change, such as a budget, cap, queue mode or model routing, affects only work that starts after it. An interpretive change, such as a policy, the story rating's guardrails, `minor_age_threshold` or the default output viewpoint, also reaches a Lite turn that is still generating. That turn's reply is re-checked under the new rules before it commits, and it is regenerated if it no longer passes.

## Media

`POST /media` stores an image or video as an immutable, content-addressed asset: a `multipart/form-data` body with a JSON `metadata` part (`kind`, `content_rating_id`, the `depicted_subjects` every upload must declare, `[]` for none, and optional `alt_text`, `caption`, `generation_metadata` and `owner`) followed by the `file` part. The type is sniffed from the bytes and checked against `media_allowed_types`; the size against `media_max_bytes`. Bytes live in the local blob store under `EPISTREL_MEDIA_ROOT` (the image's `/var/lib/epistrel/media`, the compose `media` volume) as `<sha256[:2]>/<sha256>`, so identical bytes are stored once. `DELETE /media/{id}` retires an asset: it leaves listings and refuses new references, but whatever already shows it keeps reading it until the media sweep finds nothing referencing it and purges it. Epistrel does not moderate media itself. Set `media_moderation_webhook_url` (`PUT /admin/settings`) and every upload is POSTed there first, with a 5-minute signed `content_url` for the bytes (fetched with the service credential), and stored only on `{"decision": "accept"}`; a rejection is 422 `moderation_rejected`, and a hook that is unreachable, slow or answers anything else is 503 `moderation_unavailable`. Verify the requests' `X-Epistrel-Signature` (`sha256=` + HMAC-SHA256 of `<X-Epistrel-Timestamp>.<body>`) with the secret `epistrel moderation-secret` prints, which is derived from `EPISTREL_CONFIG_KEY` without revealing it.

Large media (FR-MEDIA-3). `GET /media/{id}/content` answers one `Range` with 206 (`Accept-Ranges: bytes`), so video seeks; `If-None-Match` and `If-Range` follow its `ETag`, and `HEAD` answers the headers. `GET /media/{id}/url` returns a URL that serves the bytes without the credential for `media_signed_url_ttl` seconds (default 300): hand it to the player or browser that shows the asset, and to no one else. With the local store it is Epistrel's own signed `/media/{id}/content?exp=&sig=` under `public_base_url` (which must be set). An upload larger than `media_multipart_threshold` (default 8 MiB) is 413 `use_multipart` on `POST /media`; open a resumable session instead (`POST /media/uploads` with the metadata and the upload's `size`, `mime_type` and `sha256`), send each `part_size` piece with `PUT /media/uploads/{id}/parts/{n}`, check `GET /media/uploads/{id}` to see which parts arrived (to resume after a failure), and `POST /media/uploads/{id}/complete`. Every video gets a poster: name one (`poster`, an image asset id), send one (a `poster_file` part after `file`), or let Epistrel take the first frame with ffmpeg, which the image installs and runs as a separate program (`EPISTREL_FFMPEG_PATH`; ADR-151). Bytes can live in any S3-compatible bucket instead of the media root: set `EPISTREL_MEDIA_STORE=s3`, `EPISTREL_MEDIA_S3_BUCKET` (it must exist) and, unless it is AWS with its own credential chain, `EPISTREL_MEDIA_S3_ENDPOINT`, `EPISTREL_MEDIA_S3_ACCESS_KEY` and `EPISTREL_MEDIA_S3_SECRET_KEY` (see `.env.example`). Signed URLs are then the store's own presigned URLs, and the store serves the bytes. The store is chosen at the first start; switching it while media exist refuses the start (exit 78). For a local MinIO, run `docker compose -f docker-compose.yml -f docker/compose.minio.yml up`.

## Engine profiles

An engine profile is a named, versioned set of capability modules and dials that sets how hard the engine works for a story (FR-PROF). `GET /engine-profiles/schema` shows what this deployment has delivered, with each setting's tags. Today that is the `lite` pipeline, the rolling summary, pinned memories, the polish pass, budgets, and models (per-purpose routing and the generation defaults).

- `POST /engine-profiles` creates a profile at version 1 with any of those settings. What it leaves out takes the default.
- A setting outside the delivered schema, or a module whose dependencies are off, is 422 `invalid_profile`, and the answer names each one.
- `PUT /engine-profiles/{id}` makes a new version when the settings change.
- `POST …/copy` copies a profile, and `POST …/validate` checks settings without saving them.

The Lite preset ships owned by `system`. It is an ordinary profile: edit it, copy it, or delete it once another profile is the default.

Every story runs under one profile. A new story gets the deployment's default (`default_engine_profile_id` in `/admin/settings`, Lite until you choose another), and a fork keeps its source's. `DELETE /engine-profiles/{id}` hides a profile and answers with warnings that name the stories still using it and the scenarios recommending it. Those stories keep running on it, and the daily retention sweep purges the profile once no story uses it. The default profile cannot be deleted. For now a story only records its profile, and its turns still read the story's and the deployment's settings.

## The minors policy

Epistrel ships a seeded global policy, `minors_no_sexual_content`: **no sexual content involving a minor, or in the presence of a minor**, at every content rating, Explicit included. It applies to text the consumer sends in and to text the model writes. A minor is a character younger than the deployment's `minor_age_threshold` (default 18; `PUT /admin/settings`), judged from each character's true age, which every character must declare at creation. The age check is deterministic and costs nothing: a story whose cast has no minor never calls a model for this policy. When a minor is in the cast, the `guardrail_check` model judges the text, seeing the cast only as anonymous labels with ages. A refused request says that a minor is involved or present, never who or how old. A reply that breaks it, like a reply that breaks any output policy, is generated again with the policy's message as a constraint, up to the story's `guardrail_retry_budget` (default 2), and then replaced by the character's safe fallback line (`guardrail.fallback_used` in the response); rejected drafts are kept only as audit records.

**The policy ships enabled.** It is ordinary policy data: an administrator can edit or retire it with `/admin/policies`, and the change is recorded with the acting principal. Removing or weakening it is the deployment's decision and responsibility.

## Trust boundary

Epistrel runs behind the consumer and must never be exposed to end users. It authenticates only the calling service, which sends one of the deployment's service credentials as `Authorization: Bearer <credential>`, and it holds no user roles, grants, or permissions: which end user may call which operation, act as which character, or request which output viewpoint is the consumer's decision, made before it calls. Its privacy guarantee is relative to the request: output is produced for the output viewpoint the request names, and Epistrel does not verify that the consumer was entitled to request that viewpoint, so Epistrel by itself does not stop a player from reaching another character's secrets. Every mutating call carries an opaque requesting principal in `X-Epistrel-Principal`, which Epistrel records for attribution and never interprets; use opaque IDs rather than names or email addresses, so recorded attribution identifies no one without your own records. Ownership and relations are labels the consumer defines; the engine never grants or withholds an action because of them. Ownership exists for listing, attribution and offboarding only.

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

The deployment smoke (C-10, `tests/smoke/`, marker `smoke`) runs against a live compose stack, with `tests/smoke/fake_openai.py` as the one model endpoint; it is opt-in, and without `EPISTREL_SMOKE_BASE_URL` it fails rather than skips. `publish-edge.yml` and `release.yml` run it on the built image before publishing anything:

```bash
docker compose -f docker-compose.yml -f tests/smoke/compose.override.yml up -d --build --wait
EPISTREL_SMOKE_BASE_URL=http://127.0.0.1:8000 uv run pytest -m smoke tests/smoke   # EPISTREL_SMOKE_FAKE_PORT if 1234 is taken
docker compose -f docker-compose.yml -f tests/smoke/compose.override.yml down -v
```

It stops `db` (expecting 503 `database_unavailable`, then recovery) and restarts `engine` (expecting the start-up recovery pass to fail an orphaned operation), so run it on a stack you can disturb. `tests/test_c_11.py` (C-11) proves each `tools.gates` check fails on a fixture violation and passes on the repository, and that `ci.yml` wires every one into the required `gate` check.

P-04's reference workload (`python -m perf.lite_smoke`, #76) is run by hand, and `tests/perf/test_p_04.py` fails the gate once the newest committed report in `perf/results/` is more than 30 days old: rerun `uv sync --group perf && uv run python -m perf.lite_smoke --minutes 30 --host-class mac` and commit the report before then (the first one is dated 2026-10-11).

Integration tests live under `tests/integration/`. Set `EPISTREL_TEST_DATABASE_URL` to reuse a running PostgreSQL 16 + pgvector server instead of a container (its role needs `CREATEDB`); with neither Docker nor that variable they fail with a message, never skip. Schema changes go through Alembic: edit `epistrel.core.schema`, run `uv run alembic revision --autogenerate -m "..."`, and a test fails until metadata and migrations agree.

## License

Apache-2.0. See [LICENSE](LICENSE).
