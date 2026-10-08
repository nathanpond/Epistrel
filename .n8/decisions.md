# Decision log

Append-only. One `##` section per skill run, entries in chronological order. Record real decisions (choices between alternatives, assumptions, deviations from plan), not routine actions.

```markdown
## /n8-exec M1 — YYYY-MM-DD

- **Decision:** <what was chosen>
  **Why:** <reasoning; cost if wrong>
  **Issue:** #N
```

## Ad-hoc entries (the drift ledger)

Changes made outside the n8SDLC commands that deviate from what planned issues assume get an `## Ad-hoc` section. `/n8-replan`, `/n8-exec`'s preflight, and `/n8-stat` read these to detect stale plans. When `/n8-replan` processes an entry it appends `— reconciled by /n8-replan <date>`.

```markdown
## Ad-hoc — YYYY-MM-DD

- **Change:** <what changed, e.g. auth provider switched from Google to Okta>
  **Why:** <reason>
  **Affects:** <milestones/issues whose plans may now be stale>
```

---

## /n8-init — 2026-10-07

- **Decision:** Renamed the default branch from `master` to `main` before the first commit.
  **Why:** n8SDLC conventions and the `main` ruleset assume `main`; free to change with no history.
- **Decision:** Python tooling is uv + ruff + mypy (strict) + pytest, Python ≥3.12 (dev pinned to 3.14).
  **Why:** docs/02 specifies Python 3.12+ with mypy in CI; uv is the stack default and was installed via Homebrew for this.
- **Decision:** Security findings are logged as public `security` issues.
  **Why:** Self-hosted open-source software that others embed; disclosure helps embedders assess risk.

## /n8-roadmap — 2026-10-07

- **Decision:** Roadmap covers the full specification (docs/03 §22), M0–M8, plus M9 and M10.
  **Why:** The spec is already whole and milestone-assigned; the roadmap mirrors its table rather than re-deriving it. Later milestones stay coarse until `/n8-plan` reaches them.
- **Decision:** M9 is "Bug fixes & testing", created unplanned; M10 is Audit.
  **Why:** User's call: a dedicated hardening pass between the last feature milestone and the audit, scoped from the open bug list when M8 closes.
- **Decision:** Epics are cut by capability (28 epics, several per milestone), with every §22 requirement ID assigned to exactly one epic.
  **Why:** One epic per milestone would bury too much under each; the per-epic `coverage-claim` blocks let `/n8-plan` map stories to IDs.
- **Decision:** Coverage claim = M-tier requirements only, per the §22 assignment table. S/C pass any named test but aren't part of the claim.
  **Why:** Matches docs/03 §21 and docs/04 §13's release gate.
- **Decision:** NFR-MAINT-3 stays assigned to M2; M1 builds the gate machinery that M2's gate first exercises. No ADR.
  **Why:** Avoids editing the spec's table for a bookkeeping reason.
- **Decision:** "Production" = a `v*` tag publishing a versioned GHCR image and a GitHub release. No hosted instance; no hosted stage. `edge` image from main is what Console integrates against.
  **Why:** Self-hosted software; the user runs no instance of their own.
- **Decision:** Eval-model endpoint and CI secrets deferred to M4; M0–M3 CI uses the stub adapter only.
  **Why:** No eval floors are gated before M4; deciding earlier would pick a provider without need.
- **Decision:** M2 "development only / no real player data before M3" is a note in the milestone description, not enforced by tooling.
- **Decision:** Seven project invariants recorded in CLAUDE.md (six test-enforced, one honor-system); guards planned into epics #2 (M1) and #8 (M2).
- **Decision:** 508/accessibility audit excluded from M10.
  **Why:** The Engine has no UI.

## /n8-plan M0,M1 — 2026-10-08

- **Decision:** Python 3.14 is the single supported version (docs said 3.12+); docs/02 and an ADR are updated by #32.
  **Why:** User's call; one CI target, and the dev environment is already 3.14.
- **Decision:** Invariant 1 reworded from "Engine Core is model-free" to "Epistrel runs no model of its own", guarded by a dependency deny-list on inference runtimes (#41); the Core/Orchestrator layering stays as an import-linter architecture rule, not an invariant. Epic #2's AC amended to match.
  **Why:** The user clarified the invariant's meaning in planning; the import guard was the wrong instrument for it.
- **Decision:** No Ollama compose service; the local model server will be LM Studio on the host, reachable via `host.docker.internal` (#32).
- **Decision:** Production = `v*` tag → versioned GHCR image (`X.Y.Z`, `X.Y`, `X`, `latest`) + GitHub release; `latest` and the floating tags move only to the highest stable version; tags must match pyproject's version and be reachable from main; multi-arch (amd64+arm64) on publishes only, single-arch build-only on PRs (#34–#36).
- **Decision:** Rollback = re-point `latest` (and in-line floating tags) by `imagetools create` plus demoting the bad release (#36); no manual approval environment; no ruleset bypass for anyone.
- **Decision:** `current_milestone` in `[tool.epistrel.gates]` = highest verified milestone, initial 0; bumped in each milestone's closing PR (added to every milestone's definition of done).
- **Decision:** Gate machinery lives in `tools/gates/` (outside the shipped package), parses docs/03 §22 and docs/04 §13/§15 directly; §15 remains the plan of record (`--write` merges, never deletes). docs/04 gains M0/M1 lexicon rows but no M2 row (#39).
- **Decision:** License policy: allow Apache-2.0/MIT/BSD/ISC/PSF/0BSD/Zlib/Unlicense/MPL-2.0; deny GPL/LGPL/AGPL/SSPL/EUPL and UNKNOWN; `OR` passes on any allowed branch; exceptions never admit a denied family (#42).
- **Decision:** Invariant guards for 3 and 4 deferred to M2 (#8) and annotated in CLAUDE.md; honor-system invariant 6 has no guard.
- **Decision:** Eval-model endpoint and secrets stay deferred to M4 (unchanged from roadmap).
- **Decision:** ESTABLISHED (no stories): secret scanning + push protection; Dependabot security updates with `uv` and `github-actions` ecosystems — evidence re-run 2026-10-08 @ 9c40fea.

## /n8-plan M2 — 2026-10-08

- **Decision:** 34 stories (#44–#77) under epics #3–#9, in one dependency chain (A1 auth first, W4 deployment smoke last); every M-priority requirement in docs/03 §22's M2 row (105) is owned by a story whose acceptance criteria name it.
- **Decision:** Model transport is the `openai` SDK against OpenAI-compatible endpoints, not LiteLLM (docs/02 §13 amended by an ADR in #51).
  **Why:** LiteLLM 1.104 pulls 58 packages incl. boto3 and huggingface-hub; every target speaks the OpenAI wire format; routing per purpose is Epistrel's anyway.
- **Decision:** Exempt routes from service authentication are `/health`, `/metrics`, `/openapi.json`, `/docs`, `/redoc` (invariant 4 reworded by #44; guard `#44`). Invariant 3's guard is #74. Both annotated `(planned)`.
- **Decision (invariant amendment, user-approved):** Invariant 6 becomes "Only erasure, opt-in compaction, and audit-record retention expiry remove events; erasure and compaction leave markers" — docs/03 FR-STORE-1 names retention expiry as the third exception, with no marker. Applied to CLAUDE.md by #71.
- **Decision:** `docker-compose.yml` ships dev-only values for the service credential and the config-encryption key, labelled like the dev-only Postgres password, so `docker compose up` stays zero-config.
- **Decision:** Provider outage fails the turn with 503 `model_unavailable` and nothing committed; the safe fallback is reserved for guardrail violations. Model-dependent M2 tests (E-06, P-08, C-07 live) run against the user's LM Studio and are reported, not gated; the P-04 Lite smoke runs on the user's Mac, gated only on a reference host.
- **Decision:** `respond` returns `message: {message_id, text}` plus `beats`; the transcript is sent to the model as alternating chat messages with one system message; `untruncate` is LIFO and `GET /stories/{id}` exposes `undo_available`/`dormant_chains`; a multi-event `ingest` is all-or-nothing; the rolling summary passes the deterministic output policies before commit.
- **Decision:** A failed rebase under an interpretive configuration change ends in 409 `revision_conflict` (FR-CONC-16), not the safe fallback.
- **Decision:** The M2 summary model purpose is named `lite_summary`; `consolidation` arrives in M4 (keeps M2 tests clear of the M4 lexicon term `consolidat*`).
- **Decision (M1 convention amended, #38):** docs/04 tests whose parts land in different stories are split as `test_<id>__<slice>`; the mapper strips the suffix.
- **Decision:** Characters are fixed at story creation in M2 (no add/edit until M3's catalog); `scene_context` is validated and otherwise ignored until perception exists (M5).
- **Deferred to execution:** the four license-exception rationales if the M2 closure surfaces any; the exact LM Studio model used for the matrix row.

## /n8-exec M0 — 2026-10-08

- **#30 dev dependency stays `httpx2`.** Plan: replace with `httpx` unless Starlette's TestClient warns. Tried it: starlette 1.7.0 emits `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`. Fallback condition met, so `httpx2>=2.13.1` kept. *(discretion, planner-delegated)*
- **#30 runtime deps.** `fastapi[standard]` dropped for `fastapi>=0.115` + `uvicorn[standard]>=0.30` + `pydantic-settings>=2.6`; removes typer/rich/sentry/opentelemetry extras the service never uses. *(per Discretion)*
- **#30 ruff.** Tests ignore `S603` (subprocess with a fixed argv) alongside the existing `S101`; `PT022` made the autouse env-stripping fixture a plain `return` fixture (no teardown needed since `monkeypatch` restores). *(trivial, logged for completeness)*
- **#30 `/health` smoke.** Beyond the TestClient test, `uv run epistrel serve` was started against a dummy DSN and curled; a live uvicorn process answered 200. Not a committed test (would need a port); the TestClient test is the gate.
- **#31 container URL via `driver="psycopg"`.** Plan said `PostgresContainer(..., driver=None)` then prefix the URL; testcontainers 4.15 builds `postgresql+psycopg://` directly from `driver="psycopg"`, so no string surgery. Also imported from `testcontainers.community.postgres`: the old `testcontainers.postgres` path emits a DeprecationWarning. *(implementation detail)*
- **#31 programmatic URL for the Alembic command API.** `-x url=` is honoured on the CLI as planned; tests instead set `sqlalchemy.url` on the `Config` object (`%` escaped for configparser) because `-x` has no clean equivalent on `alembic.command.*`. `env.py` resolution order: `-x url=` → `sqlalchemy.url` main option → `Settings`. `alembic.ini` still carries no URL. *(deviation from Discretion, same behaviour for operators)*
- **#31 running inside an event loop.** Discretion said a worker thread; env.py instead runs a blocking psycopg engine in the calling thread when a loop is already running. The caller is already blocking on `command.upgrade()`, so a thread adds nothing. The `postgresql+psycopg` dialect serves both sync and async, so no separate "sync URL" exists — `make_sync_engine(url)` is the helper. *(deviation, logged)*
- **#31 `alembic check` drift test shape.** `command.check` on a database that is not at head raises a plain `CommandError("Target database is not up to date")`, not `AutogenerateDiffsDetected`. The test therefore migrates a scratch DB to head, adds a column with raw SQL, and asserts `AutogenerateDiffsDetected` matching the column name. The metadata-side case uses `compare_metadata()` on a `to_metadata()` clone as planned.
- **#31 drift detection proven on the failing case.** Added `drifted_without_migration` to the real `schema_meta` (no migration); `test_metadata_matches_migrations` failed with `add_column: schema_meta.drifted_without_migration`; reverted. Not committed (it is the thing the test forbids).
- **#31 pytest-asyncio loop scope.** Both `asyncio_default_fixture_loop_scope` and `asyncio_default_test_loop_scope` set to `session` so the async `engine` fixture and async tests share one loop (psycopg connections are loop-bound). Plan named only the fixture scope.
- **#31 `include_object` and autogenerate options live in `epistrel.core.schema`** (`AUTOGENERATE_OPTS`) so env.py and the drift tests compare with identical settings; env.py is not importable as a module.
- **#31 container timeout left at testcontainers' default** (max_tries 120 × 1 s) rather than the 60 s in Discretion; the readiness strategy is `psql` exec inside the container, which fails fast when Docker is absent (`DockerException` → `pytest.fail` with the documented message). *(trivial)*
- **#31 CLI path verified by hand**: `uv run alembic -x url=<container> upgrade head` → `current` prints `0001 (head)`; `downgrade base` → `current` prints nothing. Not a committed test (the command API tests cover the same code).
- **#31 pydantic-settings, SQLAlchemy versions resolved by uv**: alembic 1.20.0, sqlalchemy 2.1.4, psycopg 3.3.6 (binary wheel available for 3.14, no `psycopg[c]` fallback needed), testcontainers 4.15.0, pytest-asyncio 1.4.0.
- **#32 uv pinned to 0.12.23** in the Dockerfile (`gh api repos/astral-sh/uv/releases/latest` at authoring time); Dependabot's new `docker` ecosystem entry carries it forward along with `python:3.14-slim`.
- **#32 HEALTHCHECK is a one-liner**: `urllib.request.urlopen(...)` raises on any non-2xx, so a 503 from `/health` exits non-zero without explicit status handling. Verified inside the running container with the database stopped (exit 1).
- **#32 `/health` returns the Pydantic model on both branches** and sets `response.status_code = 503` rather than returning a `JSONResponse`, so the OpenAPI schema documents the 503 body shape (`responses={503: {"model": HealthResponse}}`). The failed-check WARNING is rate-limited to once per minute by a module-level monotonic timestamp and logs only the exception type (invariant 3, content-free).
- **#32 the engine lives on `app.state.engine` from the lifespan**; `create_app` sets it to `None` so a never-started app reports `database: skipped` (keeps #30's test). `make_engine` gained `connect_args={"connect_timeout": 2}`.
- **#32 `.dockerignore` also excludes `Dockerfile`, `docker-compose.yml` and `.dockerignore` itself** (not needed inside the context) on top of the planned list. `docker/entrypoint.sh` is kept executable in git and `chmod 0755` again in the image.
- **#32 entrypoint `serve` passthrough**: `serve --port N` reaches `epistrel serve --port N` after migrating; any other first argument is exec'd verbatim (`docker compose run engine python -c ...` verified).
- **#32 compose `start db` does not restart the engine** — verified by comparing the engine container's `StartedAt`/PID before stopping and after restarting `db`; the 503→200 transition is the same process.
- **#32 ADR-148 wording** avoids quoting the retired version phrase literally, because `tests/test_docs_python_version.py` forbids it anywhere under `docs/`; the ADR says "a floor of 3.12 with no ceiling" instead.
- **#32 docs/02 §12** now says the local model server runs on the host (LM Studio) and is reached via `host.docker.internal`; §3 and §13 say Python 3.14. docs/02 §5 still names LiteLLM as the transport — that is M2's decision (#44 et al. switched to the `openai` SDK) and M2's doc stories own that edit; not touched here.
- **#32 Demo run locally**: `docker compose up` → first 200 `{"database":"ok"}` after 2 s; `stop db` → 503 `{"status":"unavailable","checks":{"database":"error"}}`; `start db` → 200 again; container runs as uid 10001; image 250 MB; `down -v` clean. The image build is otherwise exercised by #33's PR gate as the story states.

## /n8-exec M1 — 2026-10-08

- **#33 `astral-sh/setup-uv` pinned at the exact release `v10.2.0`, not `@v10`.** Rule 3 (broken config): the project publishes no floating major tag after `v7`, so `@v10` fails at job set-up ("unable to find version v10"), as the first scratch run showed. Exact pin + Dependabot; the workflow-hygiene test accepts `@vN` or `@vN.N.N`. The AC's literal `@v10` is unsatisfiable upstream.
- **#33 `python-version: "3.14"` on setup-uv** rather than the Discretion's `python-version-file:` — that input does not exist in setup-uv v10 (manifest read at authoring time); `.python-version` remains the file uv itself honours.
- **#33 scratch verification ran as six parallel PRs** (five breaks + one clean) instead of one PR mutated serially: each PR still carries exactly one break, concurrency groups are per PR so nothing cancelled, and wall time fell from ~6 CI cycles to ~2. All closed unmerged, branches deleted.
- **#33 ruleset applied before the milestone PR, not after.** The Discretion deferred the PUT until ci.yml had merged "so that PR can land", but `pull_request` runs use the workflow from the PR head, so the `gate` context exists on the M1 PR itself; applying now let AC5 observe `mergeStateStatus=BLOCKED`. Consequence: the M1 PR (and every later PR, including `n8/*-state` PRs) must be green and up to date with `main` (`strict_required_status_checks_policy: true`).
- **#33 the two scratch runs with a second incidental red** (#82's mypy flagged `assert 1 == 2` as a non-overlapping comparison; #83's ruff flagged the generated file's formatting) still each failed their intended job; recorded, not re-run.
- **#33 CodeQL default setup is enabled on the repository** (checks `CodeQL`, `Analyze (python)` appear on PRs). Pre-existing; not required by the ruleset; left alone.
- **#37 cells with no requirement ID expand to nothing** (`None — not yet planned`, or any ID-free text) per the Discretion's "None row" rule; malformed *ranges* inside a cell that does contain IDs raise. An unreadable `--spec` path is a structural error (exit 2), mapped from `OSError`.
- **#37 `check-assignment` prints `current_milestone M<n>` as its last line** so later checks and humans see what the gates enforce; the finding lines and the `defined N, assigned N (unique)` summary come first, as specified.
- **#37 sub-tables under `[tool.epistrel.gates]`** are accepted only for the names later stories own (`deny`, `licenses`, `lexicon`, `traceability`); any other key is rejected. Scalars other than `current_milestone` are rejected.
- **#37 proven non-vacuous on the real docs**: temporarily shrinking M8's `FR-IMG-1..13` to `..12` produced `unassigned FR-IMG-13`, `defined 438, assigned 437 (unique)`, exit 1; restored.
- **#37 wheel limited to `src/epistrel`** via `[tool.hatch.build.targets.wheel]` so `tools/` and `tests/` never ship in the image; coverage omits `tools/*`.
- **Bump obligation (AC5):** the PR that closes each milestone bumps `current_milestone` in `pyproject.toml` to that milestone's number and must pass the stricter gate; `/n8-verify` is where that PR belongs. Noted here and in the M0–M8 milestone descriptions.
- **#38 duplicate-milestone, module/class-level, and non-literal `part` markers are structural errors (exit 2)**, not findings: they mean the file cannot be interpreted, the same class as a syntax error. Findings (exit 1) are reserved for spec disagreements: `bad milestone`, `undefined`, `unassigned`, `later requirement`, `bad interim_until`, `uncovered`.
- **#38 test-ID mapping**: `test_<family>_<rest>` with family in u/i/c/s/e/q/r/p/x → `FAMILY-REST`; any other `test_<name>` → `NAME` uppercased with `-` (property tests such as `OUTBOX`); `__<slice>` stripped first; `None` only when nothing follows `test_`. Whether the ID exists in docs/04 is #40's question. The helper is `derive_test_id` (a `test_`-prefixed name gets collected by pytest when imported into a test module).
- **#38 the fixtures exclusion** is `tests/gates/fixtures/` under the repo root, skipped unless the walk root itself lies inside it (so the checker's own fixture trees can be walked via `--tests`).
- **#38 `--json` is `nargs="?"`**: bare `--json` writes `build/parts.json`, `--json PATH` elsewhere; written only when there are no findings. `build/` added to `.gitignore`.
- **#38 pytest `norecursedirs` is set explicitly** (overriding pytest's default list) to keep collection out of the fixture trees; ruff `extend-exclude` and mypy `exclude` skip them too.
- **#38 failing case run first**: the `tests_uncovered` fixture with `spec_m1.md` / `pyproject_m1.toml` printed `uncovered FR-A-1 (M, M1)` and exited 1 before the covered fixture passed.
