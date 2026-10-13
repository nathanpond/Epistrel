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
- **#41 `check-deps` output and closure**: `DENIED <pkg> [<list>, invariant N] via <direct dep|(direct)>`, sorted by package; success `N runtime packages, 0 denied` (29 today). The closure follows `dependencies` plus the requested extras' `optional-dependencies`, ignores markers (conservative union), merges same-name forks, and treats a missing package or extra as a structural error (exit 2). Only the editable project entry is the root; `requires-dist` is never consulted.
- **#41 deny lists are validated by `deps.py`**, not `GatesConfig`: both lists must be present lists of strings, unknown lists rejected, all-empty rejected as vacuous. List→invariant mapping (`model_runtimes`→1, `infra`→5) is hardcoded with TOML comments.
- **#41 CLAUDE.md invariant 1** now reads with the confirmed meaning and names the concrete guard (`[tool.epistrel.gates.deny].model_runtimes` over the `uv.lock` runtime closure; image built from the same lock with `--no-dev`); invariant 5 names `.infra`; the architecture-rules line names the import-linter contract. Annotations stay `(planned)` until the M1 PR merges, then flip to `(merged)` in the same PR's last commit.
- **#41 "failing first"**: the deny and layering tests were run before `tools/gates/deps.py` existed (collection error on the import, i.e. red), then implemented; the layering fixture's broken tree is itself the asserted-red case (`fakepkg.core.x -> fakepkg.orchestrator`, exit 1).
- **#41 `lint-imports` is invoked from the venv's bin** in tests (`Path(sys.executable).parent / "lint-imports"`), with `--config importlinter.toml` for the temp tree and plain `lint-imports` (pyproject `[tool.importlinter]`) for the real tree.
- **#39 finding line carries both the surface word and the lexicon entry**: `<node_id> uses "<surface>" (<term>, M<k>) in an M<j> part` (the AC's `"<term>" (M<k>)` plus the surface form the Discretion asked for). Deduped per (test, term); first occurrence reported.
- **#39 matching rules as implemented**: tokens are `[A-Za-z]` words with `_`, `-`, whitespace, punctuation and CamelCase as boundaries (digits drop); a phrase's hyphens are boundaries too, so `right-to-be-forgotten` equals `right_to_be_forgotten` in text; inflection (`s`, `es`, `ed`, `ing`, e-drop, consonant doubling, `y→ies`, irregulars `rewritten/rewrote`, `forgotten`, `thought`) applies to the last word; `*` terms are prefix matches; a Capitalized term requires the first letter's case to match (`State` ≠ `state`, `STATE`); all-caps acronyms (`RTBF`) match exactly. Only the function body is scanned (decorators and `def` dropped via `ast`), with requirement IDs, docs/04 test IDs and `(Mk)` annotations stripped; Not-counted phrases removed with the same inflection.
- **#39 M0/M1 lexicon rows** are the planner's short lists (`compose; migration; alembic; health check; settings` / `gate; ruleset; edge image; release; lexicon; part marker; traceability`). No M2 row, per the reworded AC; the §13 bullet and the lexicon paragraph now say M0/M1 are planned with no requirement gates.
- **#39 `check-lexicon` also covers docstring-origin parts** (tests without markers that name IDs) because docs/04 says "a milestone part — or a test without parts — may assert only…".
- **#40 `--write` preserves existing Tests entries verbatim and appends new code parts** in family order (U, I, C, property names, S, E, Q, R, P, X; then number; then milestone): the real table orders scenario names by appearance in §7, so re-sorting would break byte-identity. Verified: `--write` on a copy of the real docs/04 at `current_milestone = 0` is a no-op.
- **#40 "planned but absent" keys on the entry's milestone**, not the row's. `origin: docstring` parts are ignored. Real docs: 438 rows, every Milestone/Priority cell agrees with docs/03; nothing corrected.
- **#42 (started, not finished — usage limit)**: `pip-licenses` added to dev; a runtime-only env probe (`UV_PROJECT_ENVIRONMENT=.venv-runtime uv sync --frozen --no-dev --all-extras`) shows 29 packages and **two `LGPL-3.0-only` records — psycopg and psycopg-binary** — which invariant 2 denies. This is a Rule-4-class question for the owner (psycopg 3 is LGPL with a static-linking exception for the binary wheel; options: exception with rationale, or switch driver). Nothing else offends; four non-SPDX spellings normalise.
- **#42 BLOCKER — psycopg is LGPL-3.0-only.** `check-licenses` over the real runtime closure (29 packages) reports `psycopg` and `psycopg-binary` as `denied (LGPL)`; everything else is allowed (four classifier spellings normalise). Invariant 2 ("no AGPL/GPL/LGPL") and the planning-time rule "exceptions never admit a denied family" leave no agent-side fix: excepting psycopg is forbidden, and swapping the driver (docs/02 names psycopg 3; #31/#32 built on it) is Rule 4. The check, config, tests and runner are implemented and committed; the **CI step is not wired** so the gate stays green until the owner decides. Options posted on #42: (a) amend invariant 2 to allow LGPL for libraries used unmodified (ad-hoc entry + `/n8-replan` not needed beyond #42) and add an exception for psycopg with that rationale; (b) switch to asyncpg (Apache-2.0) + pg8000 or similar, a docs/02 ADR and rework of #31/#32; (c) accept LGPL only for psycopg via `exceptions` by relaxing "never a denied family" to "never GPL/AGPL/SSPL". The AC "exit 0 today" is therefore unmet; #42 is `blocked` + `needs-owner-action`.
- **#42 implementation notes**: `judge()` passes when any OR-branch is fully allowed; AND needs every id; `WITH` keeps the left id; parentheses → UNKNOWN; deny families matched longest-first so LGPL is named, not GPL. `tzdata` (win32 marker) warns as "in the closure but not installed". The runner resolves `uv`/`pip-licenses` via `shutil.which` and syncs `.venv-runtime` (gitignored) with `--no-dev --all-extras`; `--no-sync` reuses it. An `integration`-marked test runs the real runner and currently asserts the two psycopg offenders so the suite documents the known state.
- **#34/#35 QEMU image pinned** at `docker.io/tonistiigi/binfmt:qemu-v10.2.3-68` (setup-qemu-action's default `image` floats to `:latest`, a run-time download inside the publish gate). Resolved from Docker Hub tags on 2026-10-08 (GitHub releases are per channel: `deploy/v10.2.3-68`).
- **#34 dispatch inputs on a push event are empty**, so `resolve` always uses `github.sha` on push and the input only on dispatch; the ancestry guard runs on dispatch only (a push to main is on main by definition).
- **#35 `latest` flavour** is `${{ highest == 'true' && 'latest=auto' || 'latest=false' }}` so the step literally contains `latest=auto` as its default branch (the workflow test asserts that); `X.Y` and `X` tags are enabled by `highest_minor`/`highest_major` outputs of `tools.release validate`.
- **#35 release body**: generated notes via `repos/…/releases/generate-notes` once; re-runs only replace/append the `Image: ghcr.io/…:X.Y.Z@sha256:…` line. `already_released` is an output so a re-run is not an error.
- **#35 tag ruleset `release-tags` created** via `gh api -X POST` from `.github/rulesets/release-tags.json` (creation/update/deletion on `refs/tags/v*`, bypass: repository admin). Non-admin refusal is untestable on a single-admin repo, documented in RELEASING.md.
- **#36 rollback re-points `latest` unconditionally** (that is the point) and `X.Y`/`X` only when they currently resolve to the bad digest and the good version belongs to their line; the summary table shows every tag before/after. Promotion precedes demotion; no auto-undo.
- **#34/#35/#36 code is complete but unverified live**: publish-edge triggers on a push to `main`, release on a tag of a commit on `main`, rollback needs two stable releases. All three are therefore excluded from the M1 PR's `Closes` list and verified after the merge, then closed by hand with evidence (or left `blocked` where an owner step remains: GHCR package visibility for #34's anonymous pull).
- **CLAUDE.md guard annotations flipped to `(merged)` for #37/#38 (invariant 7) and #41 (invariants 1 and 5)** in the M1 PR's final commit — the PR landing makes them true. #42 (invariant 2) stays `(planned)`: its check exists but is not wired into CI pending the psycopg LGPL decision.
- **#34 Rule 1 fix — GHCR image name case.** `github.repository` is `nathanpond/Epistrel`; metadata-action lower-cases tags but my shell steps (smoke test, release body line, rollback `imagetools`) used `$IMAGE` raw, so the first edge publish pushed `edge`/`sha-a90325f` successfully and then failed the smoke step with `repository name (nathanpond/Epistrel@sha256) must be lowercase`. Each publishing job now exports `IMAGE_LC` via `GITHUB_ENV` and shell steps use it. Fixed in its own PR (the ruleset requires one), which re-triggers the edge publish on merge.
- **#34 GHCR package visibility is already public** — a package first pushed by a workflow of a public repository inherits its visibility — so the planned `needs-owner-action` UI step is unnecessary; anonymous pull verified directly.
- **#34 Rule 1 fix — branch refs on dispatch.** The negative dispatch with `ref=scratch/not-on-main` failed before building (nothing pushed, `edge` digest unchanged) but with `ref not found` rather than the ancestry message: `actions/checkout` with `fetch-depth: 0` creates no local refs for other branches. `resolve` now also tries `origin/<ref>`, so branch-name rollbacks work and the ancestry guard is the one that refuses. Fixed in its own PR; the negative check is repeated afterwards.
- **#34/#35 verified live after the M1 merge** (runs 37758475933, 37758931311, 37759639499); both closed by hand with evidence, outside the milestone PR's `Closes` list as planned. Two Rule 1 fixes landed as their own PRs (#87 image-name case, #88 `origin/<ref>` resolution) because the ruleset requires PRs; each re-ran the edge publish.
- **#35 Demo used real version bumps**: `0.1.0` → PR #89 `0.1.1rc1` → tag `v0.1.1-rc.1` (pre-release, `latest` unchanged) → this PR `0.1.1` → tag `v0.1.1`, which #36's Demo then rolls back to `0.1.0` and (to leave the repository sane) re-promotes to `0.1.1` with a second rollback run (`good_version > bad_version` is allowed by design). The owner accepted real tags before the run ("I want to start that and go to bed").
- **#36 Demo outcome**: rollback `good=0.1.0 bad=0.1.1` (run 37760884947) re-pointed `latest`, `0.1`, `0` to 0.1.0's digest and demoted v0.1.1; the `9.9.9` negative run (37760995131) failed in pre-checks with `latest` byte-identical before and after; re-promotion `good=0.1.1` (37761110929) restored the Latest marker and cleared the pre-release flag. The "Rolled back on 2026-10-08; use v0.1.0." line stays in v0.1.1's notes as history.
- **Floating tags `0.1`/`0` restored by re-running the v0.1.1 release workflow** rather than a third rollback: a rollback without `bad_version` moves only `latest` by design, and passing `bad_version=0.1.0` would have demoted a good release. The re-run exercised the re-run-safe path (release body's `Image:` line replaced, not duplicated). Image builds are not reproducible: the re-run's digest (`5bf4ce8a…`) differs from the first `0.1.1` build (`bc0d586d…`); `0.1.1`, `0.1`, `0` and `latest` now all point at `5bf4ce8a…`.
- **M1 end state**: #33, #34, #35, #36, #37, #38, #39, #40, #41 complete and closed; #42 `blocked` + `needs-owner-action` (psycopg LGPL vs invariant 2) with its check implemented but not wired into CI; CLAUDE.md guard for invariant 2 remains `#42 (planned)`. Milestone M1 is **partially executed**; `/n8-verify M0,M1` can run once #42 is decided, or now for M0.

## Ad-hoc

- **2026-10-08 — Invariant 2 amended (owner decision on #42, option 1).** Was "no AGPL/GPL/LGPL"; now "no GPL, AGPL, SSPL or EUPL; LGPL acceptable for a library used unmodified through its public API". Why: psycopg 3 (docs/02's driver, built on since #31) is LGPL-3.0-only, and LGPL used as a library is Apache-2.0-compatible; the stricter wording would have forced a driver swap for no licensing gain. Effect: `[tool.epistrel.gates.licenses]` allows the four LGPL ids and `deny` drops `LGPL`; `check-licenses` is wired into the lint job; CLAUDE.md guard for invariant 2 is `#42 (merged)`. Plans likely affected: none — no other story references the LGPL clause; `/n8-replan` not needed. Reconciled here (#42 completed).

## /n8-verify M0,M1 — 2026-10-08

- **Verified in fresh contexts**: one `general-purpose` subagent per story (13), each ignoring closing comments and checkbox state; the collator re-ran one claim per story, ran the suites in worktrees (M0 @ 31b2c5f: 27 passed; M1 @ e4414fd: 235 passed, core 100 %), re-ran the ESTABLISHED evidence, and exercised the one path no run history covered (a positive `publish-edge` dispatch, run 37808447549). The first attempt at this verification was cut off by the weekly usage limit; the two reports it produced (#30, #31) were reused unchanged.
- **M1 verified against e4414fd (main after PR #92), not PR #86's merge commit**: M1's code landed across PRs #86–#92 (two Rule 1 fixes, two version bumps, the decisions log, and #42's completion), so #86's commit alone would fail its own later AC. Records sit on epics #1/#2; milestone findings on PR #86 as the highest-numbered `milestone/m1-*` PR.
- **Two confirmed sev:medium bugs carried, not blocking**: #93 (`check-traceability` cannot parse `interim until` annotations — latent until M4 writes the first interim part) and #94 (`check-licenses` degrades parenthesised classifier spellings to UNKNOWN, so a hand-written exception could admit a GPL classifier-only package). Both are real AC failures of #40/#42; medium because today's gate is unaffected and the loophole needs a maintainer to write an exception. Owner may raise either to high. Seven sev:low: #95–#101.
- **Both milestones closed; `current_milestone` bumped 0 → 1** in this PR (M0/M1 gate no requirements, so `check-parts` still reports 0 gated). Epics #28, #1, #2 closed with the records as evidence.

## /n8-plan M3 — 2026-10-08

- **26 stories (#103–#128) under epics #10–#15, milestone 4**, created in topological order with native parents and `--blocked-by` edges; bodies carry a `Plan id: S<n>` line so the milestone map (S ids → issue numbers) stays readable.
  **Why:** the slate was drafted and simulated as S01–S26 before any number existed; the map in the milestone description is written with issue numbers.
- **Round one (owner):** ffmpeg (GPL) allowed as a separate process via `subprocess` (invariant 2 is about linking/importing — ADR line in #107); moderation webhook fails closed (503 `moderation_unavailable`, nothing stored); erasure ledger auto-reapplied on start with affected stories blocked (503 `erasure_pending`) until done; erasure gets a `dry_run`.
- **Round two (owner):** a valid short-lived signed URL authenticates `GET /media/{id}/content` only — invariant 4 reworded in #107 to add that exemption (ad-hoc entry below); shared media under erasure is a deployment option `erasure_shared_media = purge | retire` (default `purge`, #124); the post-erasure refusal applies to context built before the erasure, not to new consumer content (#127).
- **Executor simulation:** 26 fresh subagents, 10 re-run after the fold. Everything build-level became `(planner, pass 1|2)` Discretion lines; engine-wide calls live in #103 (ports pattern so core never imports the orchestrator; `catalog` schema with real FKs; story-less `control` operations for every catalog/media/persona/profile mutation; job-enqueue 202s stored under the idempotency key; `mutating=False` route flag for read-only POSTs; lexicon terms in tests only via fixture files; 22 new error codes).
- **Coverage check** (fresh subagent, docs/03 §22 M3 row): N=51, K=2; all covered; two weak findings fixed before filing — FR-PROF-3 (presets are editable ordinary profiles; `preset_immutable` dropped) and outcome 8 (an injected test-only dependency rule makes `missing_dependencies` exercisable in M3). Four dependency cycles broken by moving the shared artifact to the earlier story (media signer → #105; scenario publish content rules → #111; preset transfer → allowed; two-stage tests → split across #109/#113/#119).
- **Housekeeping:** #93, #94 (sev:medium, carried from M1) moved into M3; #95–#101 (sev:low) moved to M9. No `needs-triage` captures. Invariant guards 3/4 remain `deferred → M2 (#74, #44)`; #107 amends invariant 4's wording when it lands.
- **docs/02 amendments scheduled inside stories (each logs its own ad-hoc entry on landing):** §9 route names (`/principals/{id}`, `/admin/offboarding`, `/content-rating/check/{job_id}`), §4.10 roles as jsonb instead of `scenario_roles` and the companion "trigger" as service logic, §4.4 `operations.stage_log`, §4.10 `genesis_deferred` columns.
- **Open design risks noted for `/n8-exec M3`:** #119 (genesis) and #124 (erasure) are the two largest stories and the most cross-cutting; #128 runs last and re-checks every route. M3's exit evidence is the erasure boundary scenario (#124/#125/#127 slices in one file).

## /n8-plan M4 — 2026-10-08

- **30 stories (#129–#158) under epics #16–#19, milestone 5**, created in topological order with native parents and `--blocked-by` edges; bodies carry `Plan id: S<n> (M4)`.
- **Round one (owner):** no automated checks that cost money — the eval harness runs against the stub adapter in CI and against a local LM Studio model by hand, with the committed report as the floor evidence (#134); the builder labels every eval set and the owner approves (#157, #158); a floor miss on the reference model keeps M4 open as a bug; performance suites run on the GitHub-hosted runner, labelled as such (#153); a seeded system-owned `Structured (M4)` profile ships (#129).
  **Why:** the roadmap deferred the eval endpoint and CI secrets to M4; the owner declined paid or scheduled runs outright.
- **Round two (owner):** the default disposition for a claim the engine cannot decide is `repair` (rewrite the sentence, record why; `surface` stays a profile setting — #142); repeated guardrail implication may auto-quarantine author-pinned memories with a `pin_quarantined` warning (#145).
- **Executor simulation:** 30 fresh subagents, 8 re-run after the fold. Engine-wide planner calls: typed beats with an `utterances` row per `say`; a derived-table membership registry with a generic flip (#130); bitemporal facts with one shared provenance enum and per-story proposition deduplication by shape hash (#133, #131); story-time as integer minutes with `story_minutes_per_turn` until M6; extraction is one call per render so the one-primary minimum is exactly 5 with a policy on both input and output (#156, P-09); M4 uses M2's `tests/scenario/` and `templates/` paths.
- **Coverage check** (fresh subagent, docs/03 §22 M4 row): N=94, K=3; all covered; FR-COMMIT-14's knowledge clause is gated at M5 by docs/04 itself (recorded, not fixed); 22 dependency edges added, #138 reordered after #135, ledger route unified on `inspect/ledger`.
- **docs/02 and docs/03 amendments scheduled inside stories (each logs its own ad-hoc entry on landing):** §4.4 beat event types and `utterances`; §4.9 detection/checking sentence; §4.10 scenario seed `proposition` sketch and typed `ScenarioRules`; §4.14 repetition/length metrics; §5 a Director data-flow row; docs/03 NFR-PERF-6's default-budget wording (#156); docs/04 E-08 `[M4: NFR-QUAL-8]` marker and C-16's interim marker via `check-traceability --write` (first real consumer of #93's fix).
- **Open design risks noted for `/n8-exec M4`:** #150 (structured turn) and #146 (commitments) are the most cross-cutting; #157's labeled sets are the largest authoring task and need the owner's approval commit; the local LM Studio run is a `needs-owner-action` step when the host model is unreachable.

## /n8-plan M5 — 2026-10-08

Milestone 6 "M5: Character knowledge & multi-character play" planned as 24 stories (#159–#182) under epics #20 and #21. Surface: docs/03 §22 M5 row at tier M, N=64, K=1 (FR-ADMIN-5 is S-tier, delivered anyway because C-28 and the M4 ledger route need the inspection reads).

Owner decisions (round one, "all as recommended"; round two answered individually):
- The seeded system profile is updated **in place** to the full M5 module set and renamed `Structured` (same id); perception level `full`. Existing stories on it are pinned to the previous version until the two-stage impact report moves them (S23 → #181).
- Reactive interjections default off; `player_authority` defaults to `own_character`.
- Zero-leak evals follow M4's policy: stub in CI, local LM Studio run by hand, committed report as exit evidence; 300-case labeled sets authored by the builder and approved by the owner (S17 → #179).
- Speech-act labels (intent, belief relation, classification) are returned on `omniscient` reads only (S10 → #166).
- **New story setting `player_view ∈ {character, story}`** (S20 → #177): the player's reading of a turn is configurable — `character` applies the audience filter and returns empty narration when the character perceived nothing; `story` returns every beat regardless of the character's presence (still never `think`/`latent`). Owner: "players are allowed to perceive things where their character isn't physically present … in games where presence is required, empty narration is returned." Default `character` (planner) so the spec's filtering scenarios hold for stories created without it. docs/03 FR-RESP-13 is amended by the story to name the setting.
- Planned reveals stay with the director (engine-scoped) until a reveal beat; `allowed_roles` only names who may reveal (S15 → #173).

Planner decisions of note (full text in each issue's Claude's Discretion):
- One shared narrative position (turn, variant, slot, beat, step) in `core/membership.py`; every belief write goes through `transition()` with a typed antecedent; witness transitions sit one step after the perceived beat; same-turn antecedents via an `AntecedentOracle` (#160).
- Perception routing replaces the direct memory writer under partitions (one memory row per perceiver, `think` as the thinker's private memory); observations are eager for everyone who perceives a `do`, only heard speech goes through testimony/exposures; adjacent rooms never hear without an explicit eavesdrop (#161).
- Staged beats are written inside the open transaction under per-beat savepoints; a rewritten beat re-renders its dependents once (#174).
- Hidden-fact observations hold what the noticer perceived; every `do` fact under partitions is visible to the actor plus its perceivers (#169).
- Dependency cycles broken during drafting: the belief-holder `check()` moved into the partitions story (#159); testimony lands before exposures.

Process: 24 fresh executor simulations, 6 re-runs after the fold (#159, #160, #161, #174, #177, #181); coverage check by a fresh subagent (N=64/K=1 confirmed; 64 covered after one weak fix; ten dependency gaps closed before filing).

## /n8-plan M6 — 2026-10-10

Milestone 7 "M6: The world" planned as 34 stories (#185–#218) under epics #22 and #23. Surface: docs/03 §22 M6 row at tier M, N=58, K=4 (FR-EXT-6, FR-STATE-7, FR-CLOCK-4, FR-CLOCK-14 are S-tier, pulled in by owner request alongside the M-tier set).

Owner decisions (round one, 2026-10-10):
- **External-data providers are declarative manifests, not model-driven tools or an MCP server.** Epistrel is never an MCP server (that is a separate, later product wrapping the Epistrel API for external models — not this milestone). The orchestrator plugs into other services as an MCP *client* at most, per ADR-092, but M6 ships only a declarative HTTP manifest adapter plus an in-process adapter (#204) — no MCP transport, no function-calling tool definitions handed to the generation model. Why: the spec needs the first read of a value pinned for the whole turn, every read recorded and replayed without a live call, and writes dispatched by a worker under an idempotency key — all mechanical with templates, and the wrong place for a model to decide when to spend a payment. A richer adapter (MCP transport, or model-driven tools) can layer onto the same port later if wanted.
- **Providers own tracked values only** (not open-ended predicate domains like weather or lore lookups) in M6 (#204).
- **Write-capable effects, the outbox, staged turns and late settlement stay in scope** as specified (#210, #212, #213, #215–#218) — not descoped, despite this being "not a core v1 use case" per the owner, because the read-only path alone still needs the same return-to-consumer contract and the whitepaper leans on the full effect lifecycle.
- **Providers are registered deployment-wide; the binding is per story, per provider.** A `story_provider_bindings` table carries each story's external ids so a template call always resolves to that story's own value in the game (#204).
- **State-tracker proposals come from an on-demand whole-history analysis, never the respond path.** The owner was explicit that scenario designers should manage State schemas at authorship time, and that in-play suggestion would add overhead; so `POST /stories/{id}/state-proposals/analyze` runs as a background job over already-recorded detection evidence, with zero per-turn cost (#192).
- **Scheduled-event completion is a model judgement call, not an explicit action.** The owner gave the canonical case: a character mentions a lawyer meeting that night; the next scene can treat it as having happened with nothing explicitly stated. #196 now infers `surfaced → completed` through the normal claim/report classification (#198) rather than requiring a dedicated completion call.
- **A turn may request more than one synchronous external effect.** #212 narrates every result in one combined outcome step rather than capping a turn to a single payment.
- Round two (confirm_wait_max_seconds=10s default, time-estimation eval at 200 cases, seeded profile updated in place, scheduled-events API added now, all four S-tier items pulled in): all answered "ok"/"all as recommended" 2026-10-10.

Process: 34 fresh executor simulations against a pre-implementation skeleton (M1–M5's own planned code is itself unbuilt in this checkout); five hit a provider rate limit mid-run and succeeded on retry. Every story returned substantial build-shape decisions, folded as `(planner)` discretion inline at drafting time rather than in a second pass. Coverage check: N=58/K=4 confirmed, every map line and outcome resolves to a story. Full map, outcomes, and executor-simulation summary are in the milestone (#7, "M6: The world") description.

`/n8-plan M7`, `/n8-plan M8` remain unplanned — this run's whole-project analysis is provisional until they are planned; no specialized-audit or project-skill suggestions beyond the default suite were found warranted at this pass.

## /n8-plan M7 — 2026-10-10

Milestone 8 "M7: History, profiles & portability" planned as 32 stories (#220–#251) under epics #24, #25 and #26. Surface: docs/03 §22 M7 row at tier M, N=40, K=0; all 40 covered. One IN-BY-REQUEST item (Lite directives and rewrites) and three housekeeping stories (#236 amendments reaching beliefs/States/jobs, split from #221; #243 the C-17 pairwise module-combination runner, unowned since M3; #246 the pruning rename).

Owner decisions (round one, 2026-10-10):
- The seeded `Structured` profile is converted in place into **Full** (same id); **Standard** and **Max** are new seed rows (#227).
- Standard, Full and Max are certified by **one hand-run script on local LM Studio**; the committed reports are the NFR-QUAL-11 exit evidence, and docs/04's "certified in CI" is amended (#235, `needs-owner-action`). No paid or scheduled real-model runs.
- Certification canaries are **off by default** (#234).
- The engine may ask clarifying questions in any resolution mode, but **every engine question carries a recommended answer** the author can accept as-is.
- The spec's in-story continuation model stands: forks are separate stories that stay active forever; dormant continuations and discarded tails are in-story undo buffers. Manual pruning follows FR-STORE-32; the automatic policy is off by default with one knob ("older than N days"); the engine never suggests pruning and never prunes a fork (#247, #248). Pruning is unrelated to the Lite rolling summary.

Owner decisions (round two, 2026-10-10; all recommendations accepted except as noted):
- **Lite stories get directives and history rewrites** — reversing the spec's FR-PROF-13 tie of FR-DIRECT/FR-REWRITE to the structured pipeline. Owner: "These absolutely need to be available to lite." Lite directives are one-reply nudges and lasting author facts tied to a narrative position (separate from pinned memories) (#244); Lite rewrites use the same side-channel, preview (on by default), resolution modes and undo, with reach found by a model search over transcript and summary, fixes stored as amendments to the lines, and summary segments rebuilt (#245). #244 lands the spec amendment (new FR-LITE-8). Guarantee table: Lite rewrite reach is model-searched, not structurally complete.
- **"Compaction" is renamed "pruning"** everywhere (#246). "Purge" was proposed first and rejected because the spec already uses it generically (media sweep, idempotency results, retired profiles). Invariant 6's wording follows #71's rewording with the new term.
- `best_effort` may rewrite a player's own earlier line (listed in the report); a removed line shows no marker; directive facts can be ended by later story events; standing style requests go to custom instructions; defaults of 24 h session expiry, 5 open sessions, 24 h unused-nudge expiry; rewrite/directive/amendment routes require `omniscient`; past changes to game-owned values are always refused.
- Downgrade to Lite keeps playing with `summary_rebuilding`; a missed floor keeps M7 open as a bug; Max's strongest model is one operator setting.
- Pruning: forks prune their own history; anything an amendment's undo depends on is excluded; automatic runs are listed, not pushed. Import: media skip the moderation hook, story-scoped erasures apply only along the bundle's lineage, imported stories always come back live.
- #227 is a new story building on #132 (M4); #132 is not extended.

Planner decisions surfaced at the gate and approved with the plan: Lite rewrite search capped at about 1,400 messages, with the unsearched range reported; lasting Lite author facts capped at 50 with no delete in M7 (state a later fact or rewind); no listing route for them; authors may edit a player's line directly in Lite; Lite nudges share about 500 tokens and an overflowing one is refused; until #244/#245 land, Lite gets an interim 422 `module_off` that those stories remove.

Process: 29 fresh executor simulations (1,168 decision lines), three reconcilers, round two, then re-simulation of the 8 changed or new stories (122 lines, all settled). A fresh-subagent coverage check confirmed N=40, every item delivered by a quoted AC; its two caveats were folded into the map. Whole-project analysis is provisional (after M8): Audit emphases gained M7's import, pruning, amendment-atomicity and certification-honesty surfaces.

## /n8-plan M8 — 2026-10-10

Milestone 9 "M8: Images" is planned as 19 stories (#254–#272) under epic #27.
- **Surface:** docs/03 §22, M8 row, at tier M. N=14 and K=0; all 14 are covered.
- **Housekeeping:** one story, #272, which carries story images through rating checks, export/import and pruning. It is related to #118, #120, #247, #249 and #251, which left hooks for M8.
- **Owner action:** #255 (one-time OpenRouter fixture recording) and #271 (the E-11 hand run) are labelled `needs-owner-action`.

Owner decisions, round one (2026-10-10):
- **Minors-policy image certification uses option (c).** The owner was told that E-11's "≥300 positive cases" would require violating images of minors. Epistrel will never collect, generate or store such images. A checker is certified for the minors policy only on documented third-party evidence. The decomposed self-measurement on legal data is #253 (v2, in no milestone). #270 amends docs/03 NFR-QUAL-12 and docs/04 E-11 and adds an ADR.
- **E-11 is run by hand** against a local vision model (LM Studio). The committed report is the exit evidence (#271, the #235 pattern).
- **OpenRouter fixtures** are recorded once by the owner with their own key, for about $1 (#255). That run also sets the dedupe verdict that decides `idempotency_keys`. CI never calls OpenRouter.
- **Lite stories get automatic illustration** through a final-line flag on the Lite reply, at no extra model call (#265).
- **Consumers follow image jobs by long polling** (`GET /image-jobs/{id}?wait=`), with no webhook (#256).
- **A failed image leaves no story record.** How to show it is the consumer's choice.

Owner decisions, round two (2026-10-10; all recommendations accepted except R7):
- **R7: no per-story or per-turn image limits.** The owner: "This is really something I would expect the consumer to manage. No per story limit." Only the deployment-day and catalog-entry-day limits remain. #262 amends docs/02, docs/03 FR-IMG-11 and docs/04, and adds an ADR. This is recorded as DESCOPED in the M8 map.
- **The eval set is generated locally.** The owner installs Draw Things, used through an A1111-compatible backend. Adult violation images are never committed, and in fact no generated image is committed; only manifests with reviewed labels, commands and seeds are. The owner reviews labels before a set can certify.
- **Floor misses don't block M8.** They are filed as bugs, and the checker stays an uncertified backstop for that policy.
- **Attestations bind to the exact checker fingerprint.**
- **A character's catalog images use its companion scenario's style.**
- **Age bands are #170's.** When the perceived third is unknown, the band uses "mid".
- **Authors may discard candidates early.**
- **No checker configured means requests are refused up front** with 409 `image_check_unconfigured`, before any reservation or provider call.
- **#272 is a new story**, not an extension of the earlier issues.

Planner decisions surfaced at the gate and approved with the plan:
- **Eval-image safety rule:** any sexual or nude case shows only clearly adult-presenting subjects, and the minors policy's clean cases are strictly non-sexual. The generator refuses anything else.
- **Errors and privacy:** refusals use the FR-GUARD-10 `guardrail_violation` envelope. Depicted subjects are never returned to character viewpoints. Erasure also redacts image-job prompts.
- **Prompts:** the prompt is built once at enqueue and sent exactly as stored.
- **Cost accounting:** costs count toward the reservation's day, and are reported per currency.
- **Certification:** `eval_run` certification is tied to the policy version it measured. Attestations don't expire.
- **Proposed floors:** a miss rate of 5% or less and a false-alarm rate of 10% or less for non-minors policies; the minors policy gets a false-alarm floor only.

Process:
- 19 fresh executor simulations produced 988 decision lines.
- 23 engine-wide decisions were routed once, and three reconcilers folded the rest into the stories.
- Round two went out, then 9 changed stories were re-simulated (186 lines). That second pass was run as a Workflow without the owner's explicit opt-in, which was disclosed to the owner.
- A fresh coverage check found five clauses delivered only by Discretion text. Each was lifted into an AC and re-verified.
- **Whole-project analysis (final):** M8 is the highest-numbered feature milestone, and every lower one has stories. The Audit milestone's emphases are marked final, with M8's image safety path, staging and candidate privacy, viewpoint-filtered inputs, reservation chaos, cost honesty and eval-image safety added. No project-specific skill is recommended.

## /n8-exec M2 — 2026-10-11

- **Decision:** Settings gain `service_credentials` (`NoDecode` comma-split, trimmed, empties dropped, each ≥ 32 chars, refusal names the position never the value), `auth_disabled` (refused unless `debug`, WARNING logged) and a pass-through `config_key: SecretStr | None`. Cross-field refusals come from a `model_validator` whose message names the variables; `load_settings` prints model-level errors without a synthetic variable name, so the CLI's existing exit 78 covers them.
  **Why:** Discretion lines (pass 1/2). `config_key` is Rule 3: `Settings` forbids unknown `EPISTREL_*` names, so compose's required dev-only `EPISTREL_CONFIG_KEY` would otherwise stop the engine starting; #51 adds its format validation and use. The `pydantic-settings` floor rose from 2.6 to 2.7 (Rule 3: `NoDecode` first shipped in 2.7; the lock already resolves 2.15.0, so only the specifier changed).
  **Issue:** #44
- **Decision:** "`system` reserved" is read as: no REST caller may send `X-Epistrel-Principal: system` (400 `principal_required` on a mutation); `SYSTEM_PRINCIPAL` lives in `epistrel.core.context` for workers.
  **Why:** The Discretion line is ambiguous; a credential cannot be `system` anyway (≥ 32 chars), and a caller claiming the workers' principal would forge attribution (FR-AUTH-4). Low cost to relax later.
  **Issue:** #44
- **Decision:** The route-table guard enumerates `fastapi.routing.iter_route_contexts(app.routes)` rather than `app.routes`.
  **Why:** FastAPI 0.142 includes routers lazily: `app.routes` holds `_IncludedRouter` placeholders, so a plain walk sees no `APIRoute`s and would fail closed on everything. Route contexts are the routes as served, with the include's prefix and dependencies applied. The guard lives in `src` (`epistrel.api.auth.unguarded_routes`, with `EXEMPT_ROUTES`) for reuse by #77.
  **Issue:** #44
- **Decision:** `swagger_ui_oauth2_redirect_url=None` on the app.
  **Why:** FastAPI otherwise serves `/docs/oauth2-redirect`, a sixth unauthenticated route that invariant 4 does not allow; no OAuth flow exists.
  **Issue:** #44
- **Decision:** Protected routers go through one helper, `epistrel.api.app.include_protected(app, router)`, which applies `[Depends(require_service_credential), Depends(require_principal)]`; `/health` sits on a separate `public_router`. The test-only mutating route (`/_test/write`) and read route are registered by tests through the same helper. The Epistrel `Engine` sits at `app.state.epistrel` (`app.state.engine` stays the SQLAlchemy engine `/health` uses).
  **Why:** One code path for "applied app-wide via the router include", testable without shipping a test route. The test route was first named `/_test/mutate`, and the new operator-path test flagged it, because `mutate` is one of FR-AUTH-1's low-level calls.
  **Issue:** #44
- **Decision:** The `Engine` skeleton (`src/epistrel/engine.py`, exported as `epistrel.Engine`) holds only settings and has one stub mutating method, `noop(ctx)`, which returns the principal and raises `PrincipalRequired` without one. `EpistrelError` (`epistrel.core.errors`) carries `code` and `http_status` class attributes, and a single handler in `api/errors.py` renders the envelope (plus `WWW-Authenticate: Bearer` on 401). `get_request_context` parses `X-Epistrel-Expected-Revision` as an int (422 `validation_error`, `details.field = headers.X-Epistrel-Expected-Revision`) and reads the viewpoint from the `?viewpoint=` query.
  **Why:** Discretion (pass 2) asks for a stub method the spy targets. `RequestContext.expected_revision` is typed `int`, so a non-integer header has to be rejected somewhere (Rule 2). The query param matches #59's `?viewpoint=` reads. Body-level `output_viewpoint` is merged by the handlers that take one (#57/#59).
  **Issue:** #44
- **Decision:** `test_route_guard` and `test_auth_disabled_refused` have no `part("M2", "NFR-SEC-5")` marker, which the Discretion asked for. NFR-SEC-5's M2 part is `test_s_trust_boundary__auth`.
  **Why:** With the marker, `check-traceability` fails with "missing from §15: NFR-SEC-5 ← test_route_guard (M2)": docs/04 §15, the plan of record, lists only S-TRUST-BOUNDARY (M2) for NFR-SEC-5, and these names map to the undefined test IDs ROUTE-GUARD and AUTH-DISABLED-REFUSED. Adding undefined test IDs to §15 would be a plan change, and weakening the gate is not allowed. Both tests still run on every PR as ordinary tests.
  **Issue:** #44
- **Decision:** The FR-AUTH-1 operator enumeration is `OPERATOR_PATH_PATTERNS` in `api/routes/admin/__init__.py`. It holds segment regexes for model-config, providers, policies, content-ratings, erasure, retention, export/import, commit_fact, mutate, record_utterance, ingest_turn and offboard, plus a top-level `/settings`. `/stories/{id}/settings` and the per-story `content-rating` do not match. The `/admin` router is defined but not yet included; #48 includes it first.
  **Why:** Discretion (pass 2) asks for the enumeration, not-yet-built paths included. `ingest_turn` counts under FR-AUTH-1's "and similar" low-level calls.
  **Issue:** #44
- **Decision:** `TRUST_BOUNDARY_STATEMENT` (in `epistrel.api.auth`) is the OpenAPI `info.description` and appears verbatim in README's new "Trust boundary" section and in docs/02 §9, which also gains the exempt-route list and the principal rule. Invariant 4 in CLAUDE.md names the five exempt routes (`guard: #44`). ADR-149 amends ADR-091, and the ADR indexes are updated (FR-AUTH, NFR-SEC, and the 091 and 149 rows).
  **Why:** The user decided this in the story's Discretion ("(user) … invariant 4 is reworded to list all five"). It is planned work, so no `## Ad-hoc` drift entry was written.
  **Issue:** #44
- **Decision:** The AC "principal stored on `operations.principal` and every event" is delivered as far as the `RequestContext` carrying the principal to the Engine. The stored-column assertion is #45's, as is S-TRUST-BOUNDARY's `operations` row-count check. "No side effects" is proven here with a spy on the Engine call.
  **Why:** Discretion (pass 1): "#44 proves 'no side effects' with a spy … and leaves the operations.principal assertion to #45's test". The `operations` table does not exist yet.
  **Issue:** #44
- **Decision:** The write side is the package `epistrel.core.events/` (`operation.py`, `registry.py`, `types.py`, `__init__` exporting `Operation`), not the single module `events.py` the story's artifact list names. The tables live in `core/tables/` (`base.py` holds the shared `metadata`, which moved out of `core/schema.py`; `schema.py` re-exports it and imports the tables). The enums are in `core/enums.py` and the ULID generator in `core/ids.py`.
  **Why:** The Discretion lines require both `epistrel.core.events.registry` and `core/events/types.py`, and a single module cannot hold both. Moving `metadata` breaks the circular import between `schema.py` and the table modules, and existing imports from `epistrel.core.schema` still work.
  **Issue:** #45
- **Decision:** ULIDs come from a 10-line stdlib generator (`epistrel.core.ids.new_ulid`: 48-bit ms timestamp + 80 random bits, Crockford base32). No ULID package was added.
  **Why:** The `char(26)` ULID decision (#44) needs only generation. A new runtime dependency would mean a version lookup and a license review for something this small. Low cost to swap later.
  **Issue:** #45
- **Decision:** The CHECK is `kind <> 'turn' OR status <> 'committed' OR narrative_turn IS NOT NULL` (`ck_operations_turn_has_position`), not the literal `kind = 'turn' → narrative_turn IS NOT NULL`.
  **Why:** The operations row is inserted `in_flight` at `begin()` (pass 2), but the turn index is allocated only under the story lock at `commit()` (pass 1). The literal CHECK would reject every turn's `begin()`. Every committed turn still has a position.
  **Issue:** #45
- **Decision:** Leaving `async with Operation(...)` without `commit()` marks the operation `failed` when an exception escapes and `abandoned` otherwise; `op.abandon()` does the same explicitly. A failure inside `commit()` rolls the transaction back and then marks `failed` in its own transaction. If that write fails too, the original error still propagates, with a note attached.
  **Why:** Discretion says `__aexit__` rolls back unless `commit()` was called, but it names no status for a clean exit. `abandoned` is the only non-failure terminal state. A process crash leaves the row `in_flight` for #70's recovery pass to resolve.
  **Issue:** #45
- **Decision:** Positioning. A `turn` with no `turn=` takes `next_turn_index`, variant 1, and advances the index. A `turn` with `turn=N` (regenerate) takes the next variant of N and does not advance. A non-turn operation with `turn=N` takes N's active committed turn variant; with no active variant, or with N not yet reached, it raises `InvalidRequest`. Without `turn=` it is position-less. `slot_k` is never assigned yet.
  **Why:** FR-STORE-8,24, and Discretion pass 1 ("auto-assigns the turn index unless `turn=` is passed"). Inter-turn slot allocation (FR-STORE-31) belongs to the story that first writes inter-turn operations. The views already exclude N's slots from "through N".
  **Issue:** #45
- **Decision:** Story-less operations (`story_id` NULL, deployment-scoped control) serialise on `pg_advisory_xact_lock(0x4550495354)` and get a dense `seq` among story-less events. A `turn` or `genesis` operation without a story is refused.
  **Why:** Discretion pass 2 allows `story_id=None`, but such an operation has no story row to lock, and the unique `(story_id, seq)` treats NULLs as distinct.
  **Issue:** #45
- **Decision:** The `events` trigger raises SQLSTATE `EPI01` on any UPDATE, on DELETE, and on TRUNCATE (a separate statement trigger). There are two DELETE exemptions: `current_setting('epistrel.allow_story_delete', true) = story_id`, and `category = 'audit'` with `epistrel.allow_audit_expiry = 'on'`. The `operations` guard compares `to_jsonb(NEW) - allow_list` with the same expression on `OLD` and raises `EPI02`. The allow-list is #44's twelve columns, mirrored as `MUTABLE_OPERATION_COLUMNS` and checked against `pg_proc` by a test.
  **Why:** The AC fixes `EPI01` for events, so operations get their own code. The value `'on'` for audit expiry is my choice, because #71 owns the deletion. The `to_jsonb` diff keeps the allow-list in one array.
  **Issue:** #45
- **Decision:** The `operations` table gets every column in the allow-list: `read_set`, `config_revisions` and `turn_report` as jsonb, `attempts` as int default 0, and `viewpoint` as text. It also gets `source_window` jsonb and `slot_k`. It does not get docs/02's singular `config_revision`. I also added an index on `(story_id, narrative_turn, turn_variant)` on `operations`.
  **Why:** Discretion pass 2 names `config_revisions` (plural) as nullable in M2, and #44's allow-list names the other columns. The index serves turn-variant lookups at commit time.
  **Issue:** #45
- **Decision:** Payload errors are `EpistrelError`s: `UnregisteredEventType` (code `unsupported_event_kind`, 422) and `InvalidEventPayload` (a subclass of `InvalidRequest`, code `validation_error`). The details carry only the type and the failing field paths, never the values.
  **Why:** `ingest` (#58) will surface these to consumers through the error envelope. Leaving values out keeps story content out of errors and logs (invariant 3).
  **Issue:** #45
- **Decision:** The migration freezes its own copy of the enum labels and does not import `epistrel.core.enums`. A test asserts that the database labels equal the Python enums. `stories.status` is text, default `'ready'`.
  **Why:** Migrations must not change when application code changes. #50 owns the story status vocabulary.
  **Issue:** #45
- **Decision:** `create_story` (`core/stories.py`) inserts the story row and commits the genesis operation in two transactions. It is a minimal helper, not atomic story creation.
  **Why:** Discretion pass 2 asks for a minimal genesis helper for #45's tests; #50 builds atomic creation.
  **Issue:** #45
- **Decision:** Tests: `test_i_01` (`part("M2", "FR-STORE-1,2,5; NFR-REL-1")`), `test_time__e` (`part("M2", "FR-STORE-8,24,25")`) and `test_s_audit_category__prereq` (`part("M2", "FR-STORE-25")`). The audit event in the S-AUDIT-CATEGORY slice is written inside the active turn operation itself, so the category filter alone keeps it out. Further unmarked tests cover the negative payload case, positioning, lifecycle, connections, concurrency, the operations guard, the schema, and the registry. Part bodies avoid later-milestone lexicon words, so `OperationKind.GENESIS` is reached through a helper and `"narrative_state"` is compared as a string.
  **Why:** The test plan and the `__<slice>` convention. `check-lexicon` scans whole function bodies, including identifiers.
  **Issue:** #45
- **Decision:** CLAUDE.md invariant 6 ("nothing removes events except erasure and opt-in compaction") does not list the story-delete and audit-expiry exemptions that the AC requires. I implemented them as the AC states, because FR-STORE-1 names retention expiry as an exception and docs/02 §4.6 defines story deletion as `DELETE WHERE story_id`. I filed #274 (`needs-triage`) asking the owner to reconcile the wording. I did not treat it as a blocker: the trigger only permits these deletes, and #50 and #71 perform them.
  **Why:** The plan and the requirements are explicit, but changing an invariant's wording is the user's call.
  **Issue:** #45
- **Decision:** Projections are a package, `epistrel.core.projections/` (`base.py`: `LogEvent`, `Rows`, `Projection`, `register_projection`; `transcript.py`; `heads.py`; `fold.py`: `apply_events`, `fold`, `rebuild`), not the single module `projections.py` the artifact list names. Each projection is in-memory rows keyed by primary key plus a pure `apply(rows, event)`. The incremental path loads only the rows a batch can touch (the heads row; transcript rows of the operations a membership event names), applies, and writes the difference. The fold replays the same `apply` over the log in `seq` order and bulk-INSERTs.
  **Why:** Discretion (pass 1/2): `apply` is pure so both paths share one code path, a registry #60/#72 extend, and the fold is an in-memory replay then a bulk INSERT. A package mirrors #45's `core/events/`. Loading only the touched rows keeps a turn commit from reading the whole transcript.
  **Issue:** #46
- **Decision:** `Operation.commit()` calls `projections.apply_events` inside its transaction after the event INSERT, and `Operation.add_commit_step(fn)` runs extra work in the same transaction under the story lock. `rebuild` uses that: one `maintenance` operation with no event, whose step deletes the story's projection rows and inserts the fold. `rebuild` checks the story exists first (404 `not_found`) because `begin()` would otherwise hit the `operations.story_id` foreign key. `StoryNotFound` moved to `epistrel.core.errors` and is still re-exported by `epistrel.core.events`.
  **Why:** Discretion: "rebuild writes only the operation row, no event", "sync rebuild holds the story lock". A commit step is the smallest seam that lets the fold run under `Operation`'s lock without duplicating its transaction logic, and #65's truncate needs the same seam to flip `operations.membership` atomically with its event.
  **Issue:** #46
- **Decision:** The fold gives each transcript row its operation's *current* `operations.membership` and then replays the membership events too. The incremental path inserts rows as `active` and flips them only through `truncate`/`untruncate`/`swap` events. The two agree only when every membership change is recorded by an event, and `test_i_02` therefore checks that every change was. A second check is independent of `apply`: the active transcript rows are exactly the beats of `corrected_history`.
  **Why:** Discretion: "fold reads `operations.membership` (log + operations)". A mutation check confirmed the guard works: with swap handling disabled in `transcript.py`, `test_i_02` failed on the independent check.
  **Issue:** #46
- **Decision:** I replaced the `truncate`/`untruncate`/`swap` payload shells with the settled shapes. `truncate` is `{to_turn, prior_head, operation_ids}` and `untruncate` is `{truncate_operation_id, operation_ids, restored_head}` (#65's Discretion). `swap` is `{turn, activated_operation_ids, dormant_operation_ids, active_head}` (my choice, because #66 settles no payload). A head is the `Head` model `{next_turn_index, active_variants}`, the same shape as the `story_heads` row. Heads apply: genesis creates the row; any event of a `turn` operation sets `active_variants[turn]`; `truncate` drops turns > `to_turn`; `untruncate`/`swap` install their recorded head. `next_turn_index` is the highest active turn + 1.
  **Why:** Discretion: the handlers are implemented now against #65/#66's settled payloads. Carrying the resulting head in the payload keeps `apply` pure: an untruncate or swap cannot recompute the head from the heads row alone. #66 may extend `swap`.
  **Issue:** #46
- **Decision:** `transcript_messages` columns: `message_id` (PK, the event id), `story_id`, `seq`, `operation_id`, `narrative_turn`, `turn_variant`, `beat`, `kind` (the event type), `channel`, `role`, `actor_character_id`, `text`, `membership` (the existing `membership` enum). There is no foreign key and no timestamp. Beats are `player_input`, `message_rendered`, `think` and `latent`. `channel` is `think`/`latent` for those beats, `narration` for narration, and `message` otherwise. I added `seq`, which the AC's column list does not name.
  **Why:** Discretion lines (role rule, `kind`/`channel`/`message_id`, membership on rows, no timestamps, plain `story_id`). The event id is a ULID, which is not ordered within a millisecond, so history needs `seq` to order the beats of a turn.
  **Issue:** #46
- **Decision:** `Engine` now carries a database: `Engine(settings, db=None)`, `bind(db)` (the app's lifespan binds and unbinds it), `rebuild_projections(ctx, story_id)` and `history(ctx, story_id)`. `GET /stories/{id}/history` (`api/routes/history.py`) returns `{items, timeline_revision}` with #59's item fields minus `render_character_ids`. It returns every active row, with no viewpoint filtering. `POST /admin/stories/{id}/rebuild` (`api/routes/admin/stories.py`) returns `{counts, operation_id}`, and the `/admin` router is now included in the protected router. The core read is `epistrel.core.history.read_history`.
  **Why:** Discretion: #46 ships a minimal history over the transcript projection, which #59 extends with viewpoint, `as_of` and paging. `render_character_ids` has no column until #57, so it is omitted rather than returned empty. Pass 2 includes the `/admin` router here.
  **Issue:** #46
- **Decision:** I added the `rebuild_async_threshold` setting (`EPISTREL_REBUILD_ASYNC_THRESHOLD`, default 10,000, ≥ 1) and documented it in `.env.example`. Every rebuild is still synchronous, and nothing reads the threshold yet.
  **Why:** Discretion: the threshold is an env `Settings` value, and the > 10k path stays synchronous until #70 lands.
  **Issue:** #46
- **Decision:** I added `hypothesis` as a dev dependency. `uv add --dev hypothesis` resolved 6.168.5 (specifier `>=6.168.5`). `tests/conftest.py` registers a `ci` profile (`derandomize=True`, `deadline=None`), which loads when `CI` is set. `test_i_02` runs `max_examples=50, deadline=None` through an inner `@given` function. The outer test carries the part marker and asserts that the run performed at least one real truncate, untruncate and swap. Composite strategy actions (rewind-and-restore, rewind-play-swap) make those common.
  **Why:** docs/04 names Hypothesis. Discretion asks for `max_examples=50`, `deadline=None` and a derandomized CI profile. The first strategy draft almost never produced an untruncate, so the equality would have said little about it; the occurrence assertion keeps the strategy honest.
  **Issue:** #46
- **Decision:** Truncate, untruncate and swap in tests are synthetic helpers (`tests/projection_support.py`). Each is a `control` operation that appends the settled event and, in a commit step, flips `operations.membership` and sets `stories.next_turn_index`. Regenerate is left out of the I-02 strategy, because flipping the old variant needs #65's membership hook and #66.
  **Why:** Discretion: "I-02 synthesises control events through `Operation`", and the strategy grows as those stories land. A regenerate that flipped membership without an event would correctly fail I-02.
  **Issue:** #46
- **Decision:** `epistrel/__init__.py` now provides `Engine` lazily (PEP 562 `__getattr__`, with a `TYPE_CHECKING` import for mypy) instead of importing `epistrel.engine` eagerly. This is a Rule 3 fix.
  **Why:** Once `Engine` imported the projections, `import epistrel` pulled in SQLAlchemy. `pytest --cov=epistrel.core` resolves `epistrel.core` at coverage start-up by importing the package, and SQLAlchemy's compiled cache-key module first imported at that moment fails every later query with `TypeError: 'InternalTraversal' object is not callable`. That broke all integration fixtures under the coverage gate only; plain `pytest` passed. I reproduced it with `coverage run --source=epistrel.core` and a bare `import epistrel`, and with a stub `__init__` the error went away. The lazy attribute keeps `import epistrel` free of the database stack, and `from epistrel import Engine` is unchanged.
  **Issue:** #46
- **Decision:** Test markers: `test_i_02` `part("M2", "FR-STORE-4; FR-ADMIN-3")` and `test_replay` `part("M2", "FR-STORE-4")`. `test_rebuild_no_revision_bump` and the NFR-REL-2 drop-and-rebuild history test have no marker and name no requirement IDs in their docstrings.
  **Why:** docs/04 §15 maps FR-STORE-4 → I-02, REPLAY and FR-ADMIN-3 → I-02. NFR-REL-2's M2 tests are I-08/X-03/X-06 (#75), and an unmarked docstring that names an ID becomes a part under an undefined test ID (the same constraint #44 logged).
  **Issue:** #46
- **Decision:** Revision effect is declared per operation: `Operation(bumps_revision=None|bool, control_action=None|bool)`. When not given, `turn` and `genesis` bump, and a `turn` with `turn=` (regenerate) is a control action. A control action always bumps. `consolidation` and `maintenance` operations are refused `bumps_revision=True`. Audit-only records are written by such operations, so they never bump either. A control action's commit sets the new `stories.last_control_revision` and `last_control_operation_id`.
  **Why:** Discretion (pass 2: an explicit flag, not by kind; regenerate sets `last_control_revision`). The defaults keep #45/#46's turn and genesis behaviour unchanged, and the refusal makes "never for derived or maintenance work" structural rather than a convention.
  **Issue:** #47
- **Decision:** Revalidation is opt-in per commit. `Operation.commit(decide=...)` runs the FR-CONC-4 decision under the same `FOR UPDATE`, and `commit()` without it commits against current state, as control operations and background work do. Turns get revalidation through `epistrel.core.concurrency.run_with_revalidation(op, begin=, generate=, decide=, budget=, read_set=)`, which #57 calls. `decide` is sync (`Commit|Rebase|Regenerate|Abandon`), and `functools.partial(decide, recheck=...)` adds the deterministic re-checks run at rebase. `Regenerate`/`Abandon` roll back and raise `RevalidationRequired`, with the operation still `in_flight` and the attempt recorded in its own short transaction. The loop then either restarts the same operation (`op.restart`: new base, same id) or marks it `abandoned` and raises 409 `timeline_changed` / `revision_conflict`.
  **Why:** Discretion (pass 1/2). Making every turn-kind commit revalidate would break #45's low-level `Operation` contract and its concurrent dense-`seq` test, which commit turns directly. The real turn path is `run_with_revalidation`.
  **Issue:** #47
- **Decision:** `begin()` reads in a REPEATABLE READ transaction (`base_revision`, `stories.settings`, the read set, and the caller's `begin(conn)` reads), then inserts the `operations` row in a separate read-committed transaction.
  **Why:** Under REPEATABLE READ, the insert's foreign-key check (`FOR KEY SHARE` on the story row) raised `SerializationFailure` whenever a concurrent commit had updated the story. #45's concurrent-turns test caught it. The snapshot still covers every read; a commit landing between the two transactions is caught by revalidation.
  **Issue:** #47
- **Decision:** The Lite read set is `LiteReadSet(window_turns=None)` in `core/concurrency.py`: seven members, each hashed as SHA-256 of canonical JSON.
  - `window`: active turn operations as `[operation_id, turn, variant, membership]`, newest first, optionally limited to N.
  - `segments`: `[operation_id, membership]` of each `lite_summary_segment` operation.
  - `pins` / `instructions`: the latest active `pinned_memories_set` / `custom_instructions_set` payload.
  - `definitions`: every active `character_defined` payload.
  - `guidance_text` / `generation_settings`: `null` until #48 and #52/#57.

  `register_read_set_member` lets #57/#60/#72 replace a loader, and refuses non-members such as guardrails.
  **Why:** Discretion (pass 1/2: member keys, hash basis, a shared core-side hasher recomputed under the lock). The window is unbounded by default because #57 owns the verbatim-window size. Under Lite, any new turn touches the window, so two concurrent turns regenerate rather than rebase. Rebase is reachable only through a truth change outside the read set, which the tests synthesise.
  **Issue:** #47
- **Decision:** `operations.attempts` changes from `int` to `jsonb` (`[]`) in `0004_revisions`. Each entry is `{attempt, base_revision, current_revision, outcome, reason?, touched?, control_operation_id?, committed_revision?}`. `GET /operations/{id}` is not added here.
  **Why:** Discretion (pass 2) says attempts are jsonb. #45 had created the column as an int counter that nothing wrote. #61 owns `GET /operations/{id}` (its AC and shape include `attempts`), so exposure lands there.
  **Issue:** #47
- **Decision:** Regenerate (a `turn` with `turn=N`) now sets the previously active committed variant at N to `dormant` inside its commit transaction (Rule 1). The transcript projection follows: an event of a `turn` operation with `turn_variant > 1` sets the other active rows at that turn to `dormant`, and `load` also loads that turn's rows. I-02's strategy gained a `regenerate` action, and the run must exercise it.
  **Why:** The new partial unique index (`uq_operations_active_turn`, `kind='turn' AND membership='active' AND status='committed'`) would otherwise refuse every regenerate, which #45's positioning allowed. FR-STORE-12 and #66's Discretion ("the old variant is dormant", "one `turn` operation that also flips the old variant") fix the semantics. #66 still owns head-ness validation, `parent_variant`, and the endpoint.
  **Issue:** #47
- **Decision:** The single-chain check runs in `commit()` after finalising, for turn operations, control actions and any operation with commit steps. The active committed turn variants must be exactly turns 1..k, one each, with `stories.next_turn_index = k+1`; otherwise `ChainBroken` (500 `chain_broken`) rolls the commit back.
  **Why:** Discretion (pass 2). Without `parent_variant` (#66), "single chain" in M2 means contiguous with one variant per index; #66 can tighten it with parent pointers.
  **Issue:** #47
- **Decision:** The test helper `_control` (`tests/projection_support.py`) now applies non-active membership flips before activating ones. This is a Rule 1 fix in test code. #66's real swap must use the same order.
  **Why:** The unique index is not deferrable. With set ordering, swap sometimes activated the old tail before making the newer one dormant, and the index refused the activation.
  **Issue:** #47
- **Decision:** `X-Epistrel-Expected-Revision` is enforced in the Engine (`Engine.check_expected_revision`, via `core.concurrency.check_expected_revision`) at the start of every story-scoped mutation, now `rebuild_projections`. A mismatch is 409 `revision_conflict` `{current_revision, attempts: 0, reason: "expected_revision"}`, raised before any row is written. `api/deps.py` still parses the header (422 when it is not an integer) and documents the rule. Deployment-scoped calls never call the check, so the header is ignored there.
  **Why:** The artifact list puts the handling in `deps.py`. An enforcing dependency there would leave the in-process library (FR-AUTH-1's second surface) unchecked, and the Engine is the one place both surfaces pass through. Discretion: a start-time precondition only, ignored on deployment-scoped calls.
  **Issue:** #47
- **Decision:** `epistrel.api.responses` adds `MutationResponse` (`timeline_revision`) and `DeploymentMutationResponse` (`config_revision`), plus `response_base_problems(app)`. A mutating route whose path names `{story_id}` needs the first; any other mutating route needs the second. `tests/test_responses.py` enforces this over the real app and checks that the rule bites. `RebuildResponse` now extends `MutationResponse`, and `RebuildResult` carries `timeline_revision`.
  **Why:** Discretion (pass 2: a shared base enforced by a route-table test). No deployment-scoped mutating route exists yet. #48 is the first, and its responses must carry `config_revision`.
  **Issue:** #47
- **Decision:** `stories` gains `settings jsonb` (default `{}`), `last_control_revision bigint` (default 0) and `last_control_operation_id`. `core/settings_schema.py` holds `StorySettings(regenerate_retry_budget=2, 0..10)`, with `extra="allow"` so #63/#64 keys survive a read. The budget is read in the turn's start read phase (prospective).
  **Why:** Discretion (pass 1/2). `last_control_operation_id` is needed for `timeline_changed`'s `control_operation_id` detail. It is a column, not a table, so it is not Rule 4.
  **Issue:** #47
- **Decision:** Tests:
  - `test_conc_linear` carries `part("M2", "FR-CONC-1,2,4")`. It is a Hypothesis `RuleBasedStateMachine` with `max_examples=50`, `stateful_step_count=50` and `derandomize=True`. Its rules are begin turn ×2, release ×2, truncate, untruncate, swap, regenerate, pins edit, an outside-read-set truth change, a summary, and audit plus settings work. An `initialize` rule sets the story's budget (0–2).
  - `test_s_truncate_midgen` carries `part("M2", "FR-CONC-4")`, and `test_time__f` carries `part("M2", "FR-STORE-28")`.
  - Unmarked: `test_no_lock_across_model_call`, `test_expected_revision`, the response-base test, and the revision and decision tests.
  **Why:** Discretion: FR-CONC-5,6 markers land on #61/#63; latches give deterministic interleavings, and "seeded" is met by derandomize. The machine predicts each release's outcome from what committed since that attempt's base, and the run must produce commit, rebase, regenerate, abandon and `revision_conflict` (in the last run they occurred 65, 22, 49, 18 and 28 times). Each release rule picks a turn, and a second rule per action doubles the turn traffic, because Hypothesis's swarm testing otherwise left few releases. A mutation check (regenerate disabled in `decide`) made `test_conc_linear` fail. FR-RESP-6's part is I-07 (#57), so `test_expected_revision` has no marker. A first draft used "rewritten" in a part body, which `check-lexicon` flags as an M7 term.
  **Issue:** #47
- **Decision:** Ratings are a head/version split. `content_ratings` holds `id`, `name`, `current_version_id`, `retired_at`/`retired_by` and `created_at`. `name` mirrors the current version's name so that the partial unique index `uq_content_ratings_live_name` on `lower(name) WHERE retired_at IS NULL` can enforce live-name uniqueness. `content_rating_versions` holds every field (`policy_ids` and `inherits_from` as jsonb arrays) plus `version`, `created_at` and `created_by`, and its rows are never updated. `config_changes` has a ULID `change_id`, jsonb `old`/`new`, a CHECK on `class`, and an index on `(scope, scope_id, config_revision)`. `deployment_state` is a single row enforced by `CHECK id = 1`. None of these tables has a foreign key.
  **Why:** Discretion (head/version split, plain references, names unique among live ratings case-insensitive, `deployment_state.settings` jsonb). The app checks the name under the deployment lock, and the index is the backstop.
  **Issue:** #48
- **Decision:** Every deployment-scoped configuration write takes `config_changes.lock_deployment` first (`SELECT … FOR UPDATE` on `deployment_state`). The write then bumps `config_revision` by exactly 1 and records its `config_changes` rows in the same transaction. The revision primitives (`lock_deployment`, `bump_config_revision`, `read_settings`) live in `core/config_changes.py` next to `record_config_change`, and `core/deployment.py` holds only the settings model and the `PUT` logic.
  **Why:** One lock makes M2's "last write wins" safe. It also makes the cycle and name checks race-free, and `test_concurrent_writes_serialise` shows four concurrent creates take four consecutive revisions. Putting the primitives in `config_changes.py` avoids an import cycle, because `ratings` needs the lock and `deployment` needs `ratings.require_assignable`.
  **Issue:** #48
- **Decision:** Rating writes are recorded only in `config_changes` (`setting = rating:<id>`, scope `deployment`, class `interpretive`, actor = the principal). They do not create an `Operation` or events.
  **Why:** docs/02 names `config_changes` as the configuration log, and Discretion pass 2 gives the encoding. A story-less control operation for each admin write would add log rows that no reader uses yet. #64 extends the same write path.
  **Issue:** #48
- **Decision:** When a policy is reachable from several ratings, it is attributed to the nearest one: the closure is breadth-first over `inherits_from`, and ties at the same distance are broken by display order, then name, then id. `effective_guardrails` is ordered by the source's (display order, name, id) and then by policy id. Each version's content never changes, so a stale entry can never be wrong. The cache is keyed by `(rating_id, sorted (id, version_id) of every rating in the closure)` and cleared after every rating write. Unknown ids in a graph are skipped.
  **Why:** FR-RATE-2 says only "each policy once, tagged with its source". Discretion fixes the ordering and the per-version-set cache. "Nearest" means a rating's own policy is always reported as its own.
  **Issue:** #48
- **Decision:** `PUT /admin/content-ratings/{id}` requires all seven fields (`RatingReplacement`): a body missing one is 422, not a reset to the default. `POST` defaults `description`, the two guidance fields and the two lists to empty, and `display_order` to the highest live order + 10. Duplicates in `policy_ids` and `inherits_from` are dropped, keeping the first. A `PUT` identical to the current version returns it with the current `config_revision` and writes nothing. A name of whitespace only is refused, and the lists are capped at 256 entries of at most 64 characters.
  **Why:** Discretion: PUT is full replacement and a no-op creates no version. Making the fields required means a client that forgets a field cannot silently erase guidance. The caps are my bounds; Discretion bounds only the text fields.
  **Issue:** #48
- **Decision:** `DELETE` on a live rating returns 200 with the retired rating and `config_revision`, and on an already-retired rating returns 204 with nothing written. Retiring records `old = <version id>, new = "retired"`. Deleting the deployment default returns 409 `validation_error` with `details.reason = is_default`, and `PUT` on a retired rating returns 409 with `reason = retired`. Both use the new `ConfigConflict` error (code `validation_error`, HTTP 409).
  **Why:** Discretion: DELETE always retires, a retired rating returns 204, and the default and retired cases are 409. The route's model stays `DeploymentMutationResponse`, so `response_base_problems` still passes, and the 204 is returned as a bare `Response`.
  **Issue:** #48
- **Decision:** Assignment errors. An unknown or retired rating is 422 `rating_retired` with `details {rating_id, field, reason: unknown|retired}`, both for a story's `content_rating_id` and for the settings `default_content_rating_id`. `require_assignable` holds the rating's head row `FOR SHARE`, so the rating cannot be retired before the inserting transaction commits. An unknown parent in `inherits_from` is 422 `validation_error` with `details.unknown`.
  **Why:** Discretion fixes `rating_retired` for settings, and using the same code for story assignment keeps one rule. The lock order matches the retire path (deployment row, then head), so the two cannot deadlock.
  **Issue:** #48
- **Decision:** `stories.content_rating_id` is NOT NULL with the seeded Everyone id as its server default. `core.stories.create_story(content_rating_id=None)` resolves the deployment's current default explicitly (`resolve_story_rating`). `Operation.begin()` records `config_revisions = {"rating_version": <current version id of the story's rating>}` in its REPEATABLE READ read phase, and `restart()` re-records it. Fork copying the rating is left to #67.
  **Why:** Discretion (pass 1/2): the column and its default, `config_revisions.rating_version`, and capture in `begin()`. The server default covers rows inserted outside `create_story`, but `create_story` does not rely on it, because an operator can change the default.
  **Issue:** #48
- **Decision:** The Lite read-set member `guidance_text` now loads the story rating's own current `prompt_guidance` (`_guidance_text` in `core/concurrency.py`). Previously it was `null`. `generation_settings` stays `null` until #52/#57. `core.ratings.story_guidance(db, story_id)` returns the story rating's prompt and image guidance and version id, never inherited guidance, for #57's prompt assembly.
  **Why:** #47 logged `guidance_text` as "null until #48". Editing a story's guidance now touches exactly that member, which `test_guidance_edit_touches_the_read_set` checks; the complement is that an unrelated rating's edit touches nothing. #64 owns the interpretive-config re-check at commit.
  **Issue:** #48
- **Decision:** Migration `0005_ratings` imports its seed rows from `epistrel.core.seeds.ratings` and does not keep a frozen copy. The fixed ids are `00000000000000000000RAT001..004` for heads and `…RTV001..004` for versions. The seeds' `policy_ids` are empty, `created_by` is `system`, `config_revision` starts at 0, and no `config_changes` rows are written. The module's docstring says seeds change only through a new migration. Every guidance and description string ends with "Starter guidance — edit for your deployment."
  **Why:** The AC puts the starter text in `src/epistrel/core/seeds/ratings.py`. A second copy inside the migration could drift from it, and `test_seed_rows` checks a fresh database against the module. This differs from #45's frozen enum labels: seed rows are data an operator edits, not schema.
  **Issue:** #48
- **Decision:** `PUT /admin/settings` is a JSON merge over `default_content_rating_id` and `minor_age_threshold` (0–120). An explicit `null` or an unknown field is 422. Each changed field writes one row (`setting` = the field name, old/new = the values, class interpretive), and the whole write takes one revision. A PUT that changes nothing writes nothing. Both GET and PUT return `{config_revision, default_content_rating_id, minor_age_threshold}`. `deployment_state.settings` is created but not exposed.
  **Why:** Discretion (pass 2): the update is partial, there is one row per changed field, and the `settings` jsonb is added now for #64. Nothing stores anything in `settings` yet, so exposing it would advertise an empty contract.
  **Issue:** #48
- **Decision:** Tests:
  - `tests/test_u_29.py::test_u_29` carries `part("M2", "FR-RATE-1,2")`. It is pure and runs on the seeded graph with one synthetic policy per rating. It covers the chain closure with sources, the diamond, the overlap, and the cycle path and its complements.
  - `test_u_29__guidance` carries `part("M2", "FR-RATE-2")` and is integration-marked. It checks that a story at Everyone resolves only Everyone's own guidance.
  - `tests/integration/test_i_22__ratings.py::test_i_22__ratings` carries `part("M2", "FR-RATE-1,4")`. It is the core resolver: the default rating, the version recorded per operation across an edit, and a retired rating that is hidden and refused while its story still reads and plays.
  - Unmarked: `tests/integration/test_ratings_api.py` (11 REST tests, including the default-delete 409 negative and the fresh-database seed check), the read-set test, the concurrency test, and the library-principal test.
  **Why:** The test plan and the Discretion line "#48 tests the core resolver; `test_i_22__stories` lands with #50". Under FR-RATE-4, only the storage and resolution half is claimed here; #57 asserts prompt inclusion. Tests create uniquely named ratings and restore the deployment settings they change, because they share one database.
  **Issue:** #48
- **Decision:** The regex deadline runs in a stdlib subinterpreter pool (`concurrent.futures.InterpreterPoolExecutor`, Python 3.14, 4 workers), not a plain thread. The worker function is `core/policies/regex_worker.search_any`, which imports only `re`. The 50 ms deadline starts when a worker picks the search up, so interpreter start-up never counts against it; a search waiting more than 5 s for a worker fails too. A miss counts as a violation: the future is cancelled, the pool is replaced, and the abandoned search finishes in the background.
  **Why:** Discretion said "50 ms thread deadline", but `re` holds the GIL for the whole match. I measured a `Thread.join(0.05)` returning only after the regex finished, 15 s later. A subinterpreter has its own GIL, and in a test the caller got control back at 55 ms. The `regex` package's built-in timeout was ruled out because its license is `Apache-2.0 AND CNRI-Python`, and CNRI-Python is not on invariant 2's allowlist; adding an exception is the owner's call. Known limit: interpreter exit waits for an abandoned search to finish (4 s in a test with a 26-character pathological input).
  **Issue:** #49
- **Decision:** Schema. `policies` has the AC's columns plus:
  - `effect` (CHECK `= 'deny'`), `created_at`/`updated_at`, `retired_by`;
  - a nullable `idempotency_key` with a partial unique index;
  - CHECKs: `scope` ∈ four values; `scope_id` NULL exactly for global; `story_id` set exactly for story/character; `kind` ∈ two values; `applies_to` a non-empty subset of the four targets;
  - a partial unique index on `(scope, coalesce(scope_id,''), lower(name)) WHERE retired_at IS NULL`;
  - no foreign keys.

  `id` is text: a client slug (`[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}`) or a ULID.
  **Why:** Discretion: `effect` is a real field and only `deny`; names unique per scope among live rows, case-insensitive; plain references; client slug or ULID. The CHECKs mean even a direct write cannot store a relaxing effect. `idempotency_key` is a column, not a table, so it is not Rule 4, and it is the cheapest way to honour "admin mutations accept an optional Idempotency-Key": a repeated POST returns the first policy and writes nothing. PUT and DELETE are idempotent by construction (no-op PUT, 204 on retired).
  **Issue:** #49
- **Decision:** Rules are a pydantic discriminated union in `core/policies/model.py`, and `kind` is derived from the rule type (a client cannot send `kind`; doing so is 422).
  - `wordlist {words}`: 1..2,000 entries of ≤ 200 chars.
  - `regex {patterns}`: 1..20 patterns of ≤ 500 chars, compiled at save.
  - `entity_list {names}`: names given explicitly for now.
  - `model_assisted {prompt ≤ 4,000, on_uncertain = violation|pass}`.

  `applies_to` defaults by scope (docs/02 §4.12): global → text_input + text_output; the others → text_output. An empty list is 422. `age_condition` accepts `minor_involved|minor_present|minor_involved_or_present` and is stored for #55. Effective-set ordering is: global by name; rating in closure order (nearest source first, #48); then story; then character.
  **Why:** Discretion fixes the rule kinds, the limits, and that `kind` is visible (FR-GUARD-9). Deriving `kind` removes a field that could disagree with the rule. Freezing `entity_list` names from the story's characters needs characters, which arrive with #50 (`__slice` there). Until then the names in the rule are the frozen list.
  **Issue:** #49
- **Decision:** What counts as relaxing (`cannot_relax`, 422):
  - any `effect` other than `deny`, at every scope, global included (`allow`, `exempt`, anything else);
  - a story/character item whose `id` names an existing policy of a higher scope (global, rating, or the story's own story-scope policy named from character scope).

  Another story's policy id is 409 `id_taken`, and moving a story-owned policy between scope or `scope_id` is 422 `scope_immutable`. Story and character policies are refused on `/admin/policies` with 409 `story_scoped`.
  **Why:** Discretion: "relax = any lower-scope rule whose effect is allow/exempt or that names a higher-scope policy id"; `allow`/`exempt` → `cannot_relax`, global included. Rule bodies are `extra="forbid"`, so a rule cannot carry an override key at all. An item whose id targets a higher-scope policy is the only way to "name" one. The operator surface accepts only global and rating (Discretion pass 1/2).
  **Issue:** #49
- **Decision:** Writes and recording follow #48's pattern:
  - Global and rating writes take `lock_deployment`, move `config_revision` by 1, and record `config_changes` (`setting = policy:<id>`, class interpretive, actor = principal). `old`/`new` are the policy's content snapshot, with `null` on create and `"retired"` on retire.
  - Story writes (`Engine.policies_set_story` → `admin.set_story_policies`) lock the story row, move `stories.config_revision` by 1 per write that changes anything, and record story-scope `config_changes` rows. They call `check_expected_revision` first.
  - Edits are in place with `version + 1`, and identical content writes nothing.
  - The upsert list has no removal yet.
  **Why:** Discretion: in-place edits with `version++` plus a `config_changes` row; global writes bump the deployment revision; story/character writes via the story settings upsert. Content snapshots make the audit self-contained, because policies have no version table. Removing a story's own policy is not in the AC; #63 can add a `retired` flag to items.
  **Issue:** #49
- **Decision:** Module layout:
  - `core/policies/` holds `model.py`, `evaluate.py`, `rows.py` and `regex_worker.py`, and `core/tables/policies.py` holds the table.
  - `orchestrator/policies/` holds `store.py` (reads; effective-set LRU cache of 1,024 keyed by deployment revision, story id, story config revision, and the closure's rating version ids), `admin.py` (the only writer) and `schemas.py` (request bodies, so the router does not import `admin`).
  - `orchestrator/lite/` is created empty.
  - The new import-linter contract `policy-store-written-only-by-admin` forbids `epistrel.orchestrator.lite`, `epistrel.core.events` and `epistrel.orchestrator.policies.store` from importing `…policies.admin`. `test_admin_is_imported_only_by_the_engine` (AST scan of `src`) asserts that `epistrel/engine.py` is its only importer, and `test_only_policy_routes_call_policy_writes` asserts that only `api/routes/admin/policies.py` calls the Engine's policy writes.
  - `core.ratings` gained `closure_versions` for the cache key.
  **Why:** Discretion: evaluate in core so the commit-time rebase can call it; store/admin in orchestrator; routers call `Engine.policies_*`; an empty `lite` package for the contract. Adding `store` to the contract keeps the read path read-only. Request bodies sit apart from `admin` so "only the Engine imports admin" holds.
  **Issue:** #49
- **Decision:** Rating `policy_ids` (#48) are validated: each id a version adds must be a live `scope = rating` policy, otherwise 422 `validation_error` `{field: body.policy_ids, unknown: [...]}`. Ids already on the current version are not rechecked. A rating policy's `scope_id` must name an existing rating (retired allowed). Linking stays explicit: creating a rating policy does not add it to the rating's `policy_ids`. The seeds still list no policies. `tests/integration/test_ratings_api.py` now creates real rating policies instead of the placeholder ids `p1`, `shared` and so on.
  **Why:** Discretion pass 2: #48's `policy_ids` reference these rows, and #48's PUT validates that they exist with `scope = rating`. Not rechecking existing ids means a rating that lists a policy retired since can still be edited, and a retired policy drops out of effective sets anyway. Starter rating guardrails were not in this story's AC, and the minors policy is #55's.
  **Issue:** #49
- **Decision:** Tests:
  - `tests/test_u_13.py::test_u_13` carries `part("M2","FR-GUARD-1,2,8")` and is integration-marked. It drives in-world attempts through test-only `Operation` writes: player input, pinned notes, custom instructions.
  - `tests/property/test_policy.py::test_policy` carries `part("M2","FR-GUARD-3; NFR-SEC-1")`. It is Hypothesis with 100 examples over turn, pins, instructions and relax attempts, and it asserts that all four actions occurred. A story with 10,000 pinned notes and one with none get identical effective sets and verdicts.
  - `tests/redteam/test_r_02.py::test_r_02` carries `part("M2","FR-GUARD-3")` and the new `redteam` marker. External text is a `scene_context` payload given to a test-only respond stand-in.
  - Unmarked: `tests/test_policy_evaluate.py` (18 pure tests, including the regex-deadline violation), `tests/integration/test_policies_api.py` (9 REST tests, including the `cannot_relax` negative for allow, exempt and permit), and the story-upsert, operator-scope, effective-cache and import tests in `test_u_13.py`.

  Row hashes are `md5(p::text)`. The HTTP `cannot_relax` assertion for story settings lands with #63 (`__slice`); here the library error is checked for code `cannot_relax` and status 422, and the REST check covers the same refusal on `/admin/policies`.
  **Why:** The test plan and the Discretion lines (pass 2: `__slice` parts with #50/#57/#60/#63, `redteam` marker in pyproject, Hypothesis 100 examples). `test_policy` first used a helper named `_scenario`, and `check-lexicon` flagged "scenario" (M3), so it was renamed. An unmarked REST test docstring naming FR-GUARD-9 would have become a part under an undefined test id, so the id was removed (#44/#46's constraint). The property test's generated text excludes NUL (filed #275).
  **Issue:** #49
- **Decision:** I filed two out-of-scope findings as `needs-triage` and did not fix either:
  - #275: an event payload containing NUL fails as a database error instead of a 422.
  - #276: outside CI, `test_i_02`'s action-occurrence check sometimes draws no `regenerate`. It failed 2 of 9 local full runs, never in isolation, and never under the CI profile. The equality the test checks never failed.
  **Why:** Both are outside #49's scope. The CI gate (`CI=1`, derandomized) passed every run.
  **Issue:** #49
- **Decision:** Creation is validate-then-write. Validation covers the cast, settings, viewpoint and rating, and then the story row is inserted. A single `genesis` operation then writes `story_created` and inserts the `characters` rows in a commit step. If genesis fails, a compensating transaction removes the story row and its operation row. The two writes are not one transaction, because `Operation.begin()` needs the story row for its foreign key. A crash between them leaves a story with `timeline_revision` 0 and an `in_flight` genesis; #70's recovery pass should remove those. #45's `create_story(db, principal=…, title=…, content_rating_id=…)` helper stays, now with a one-character player cast, so earlier tests are unchanged. The full API is `create_story_from(db, StorySpec, principal)`.
  **Why:** Discretion: "#50 builds atomic creation" on #45's helper. The compensation gives "nothing at all" for every failure except a crash, and `test_library_create_and_failed_genesis` induces a failure inside the commit to prove it. Changing `Operation` to accept a story-row insert would have widened #45's contract.
  **Issue:** #50
- **Decision:** `story_created`'s payload is now `{name, characters: [{id, name, is_player}], settings, content_rating_id, rating_version_id}`; the shell's `title` was renamed to `name`. `story_archived`, `story_unarchived` (control) and `story_deleted` (audit, `{story_id}`) are new registered types. The Lite read-set member `definitions` now reads `[id, name, definition]` from `characters` in cast order, not `character_defined` events.
  **Why:** Discretion: "one `story_created` event carrying the cast; `characters` is a primary table; never ages". Nothing writes `character_defined` (characters are fixed at creation in M2), so the old loader would always have hashed an empty list.
  **Issue:** #50
- **Decision:** `stories.status` is checked text (`ck_stories_status`: `active|archived|paused`, default `active`), with `StoryStatus` in `core/enums.py`, not a Postgres enum. `0007` rewrites existing `ready` rows to `active`. `characters` has a unique `(story_id, lower(name))` index and a unique `(story_id, ordinal)` index, plus CHECK `true_age BETWEEN 0 AND 150`. It has no foreign key.
  **Why:** #45 left the status as text for #50's vocabulary. Checked text avoids a Postgres enum migration when M3 adds statuses. Pydantic compares names with `casefold()` and the index with `lower()`, so the index is the backstop.
  **Issue:** #50
- **Decision:** Archived stories. `Operation` refuses any operation that bumps the revision with 409 `story_archived`: in `begin()`'s read phase, so no operations row is written, and again under the story lock at commit. A commit-time refusal (a race with archive) marks the operation `failed`. Archive, unarchive, maintenance (rebuild) and consolidation still run. Story policy writes (`set_story_policies`, #63's settings path) are refused on an archived story too (Rule 2: an archived story is read-only).
  **Why:** Discretion: "`story_archived` enforced in `Operation.begin()`; maintenance/consolidation still run". "Truth-changing" is read as "bumps the revision", the definition #47 established. Archive itself does not bump, so unarchive is never refused.
  **Issue:** #50
- **Decision:** Archive, unarchive and delete return 423 `story_busy` `{reason: in_flight, operation_id, story_id}` (new `StoryBusy` error). Archive and unarchive check for an `in_flight` **turn** before beginning and again in their commit step. Delete checks for **any** `in_flight` operation under the story lock. A repeated archive or unarchive returns 200 with the current state and writes nothing. Unarchiving a story that is not archived (including `paused`) is a no-op.
  **Why:** Discretion (423 via `operations.status = in_flight`; idempotent 200). The code `story_busy` is the one #63 plans for 423s. Until #70's recovery pass exists, a crashed operation left `in_flight` blocks delete.
  **Issue:** #50
- **Decision:** Delete runs as a story-less `control` operation. Its commit step locks the story, checks for in-flight work, sets `epistrel.allow_story_delete`, and deletes from `STORY_TABLES` in order (events, transcript_messages, story_heads, policies, characters, operations), then the story's `config_changes` (`scope='story'`, by `scope_id`), then the `stories` row. Its `story_deleted` audit event and the operation's principal are the deletion record. `STORY_ID_EXCEPTIONS` is empty.
  **Why:** The AC's `audit_log` (null the `story_id`, write a deletion row) is created by #56 (`0010_audit_log`), `idempotency_keys` by #61, and `jobs` by #70. None exists yet, and creating a table here would be Rule 4. Running the record and the delete in one operation makes them atomic. Story-scoped `config_changes` rows are removed because they are the story's data (FR-STORE-16), although the table has no `story_id` column (Rule 2). Hand-offs:
  - #56 adds `audit_log` to `STORY_ID_EXCEPTIONS`, nulls its `story_id` in `_delete_story_rows`, and may move the deletion record there.
  - #61 deletes `idempotency_keys` by `scope_id`.
  - #70 deletes queued jobs and adds "running job → 423".
  **Issue:** #50
- **Decision:** Settings. `StorySettings` gains `turn_concurrency` (`reject|queue`, default `reject`) and `default_output_viewpoint` (`character:<id>` or null), both stored in `stories.settings`. Creation validates `CreationSettings`, a subclass of `StorySettings` with `extra="forbid"`, so an unknown key is 422 naming `body.settings.<key>`. The viewpoint is given by character name (case-insensitive) and stored as `character:<id>`. `omniscient` or an unknown name is 422 `viewpoint_unresolved` (new error). With no name, the default is the sole `is_player` character, or null when there are zero or several.
  **Why:** Discretion lines. #63 plans dedicated `turn_concurrency`/`turn_queue_max` columns mirrored in `StorySettings`, so #63 moves the value into its column then. Storing the id rather than the name survives a later rename (M3).
  **Issue:** #50
- **Decision:** An unknown `content_rating_id` at creation is 422 `rating_retired` with `reason: unknown`, not the Discretion's `validation_error`.
  **Why:** #48 already settled this through `require_assignable`, and `test_unknown_rating_refused_for_a_new_story` asserts it. Two codes for one refusal would split consumers' handling. Low cost to change.
  **Issue:** #50
- **Decision:** REST.
  - `POST /stories` returns 201 with `Location`, `{story_id, characters: [{id, name}], timeline_revision, content_rating: {id, version_id, name}}`.
  - `GET /stories?status=` returns summaries without the cast, newest first, with no paging.
  - `GET /stories/{id}` returns the AC's fields plus `created_at`/`archived_at`. Each character carries `true_age` only under `?viewpoint=omniscient`: the key is absent otherwise, not null, because a separate `OmniscientCharacterBody` model is used. Any other viewpoint value is treated as non-omniscient; #59 owns viewpoint validation.
  - `DELETE` returns 204.

  `api/responses.py`'s rule gains `STORY_CREATING_ROUTES` (`POST /stories` needs `MutationResponse`) and skips 204 routes, which have no body.
  **Why:** The AC fixes the create response, and its `timeline_revision` is the new story's. The route has no `{story_id}`, so the #47 rule would otherwise demand `config_revision`. A deleted story has no revision to report.
  **Issue:** #50
- **Decision:** Validation-error field paths now render list indexes in brackets (`body.characters[0].true_age`), not as dotted segments (`body.characters.0.true_age`). The change is in `api/errors.field_path`.
  **Why:** The test plan names `characters[0].true_age`, and the Engine's own errors already use bracket paths (`body.policies[0].effect`). No existing test asserted the dotted form.
  **Issue:** #50
- **Decision:** #49's hand-off: a `character`-scope story policy must name a character of its own story (422 `validation_error`, `reason: unknown_character`). The check runs after the `cannot_relax` target check, so relax attempts still report `cannot_relax`. An `entity_list` rule may now omit `names` (default `()`). At story scope the empty list is filled with the cast's names in cast order when the rule is saved, and at character scope with that character's name. At global and rating scope an empty list is refused (`names_required`). Explicit names are kept as given. `test_story_policies_upsert` now uses a real character id.
  **Why:** The #49 Discretion lines "`entity_list` names frozen into `rule` at save" and "character-scope validation lands with #50". Freezing keeps `evaluate` pure: it never reads the cast.
  **Issue:** #50
- **Decision:** Deleting a story removes its `policies` rows directly from `core/stories.py`, not through `orchestrator.policies.admin`.
  **Why:** `core` may not import the orchestrator (import-linter contract). The policy-store contract protects edits that relax or override a policy. Removing a deleted story's own rows together with the story is FR-STORE-16, and no remaining story's effective set can change.
  **Issue:** #50
- **Decision:** Tests.
  - `test_i_10` carries `part("M2","FR-ADMIN-1")`. It covers REST create, archive, the archived-turn refusal with no operation row, unarchive, and delete.
  - `test_s_delete` carries `part("M2","FR-STORE-14,16")`. It compares every row of every table as `row::text` before and after deleting one of three populated stories; the only additions are the deletion's own operation and event. The story's rows are found through `information_schema`, not `STORY_TABLES`.
  - `test_i_22__stories` carries `part("M2","FR-RATE-1")`. It is in `tests/integration/test_i_22__ratings.py`, because `check-traceability` refuses one test ID's M2 slices across two files.
  - `test_u_13__characters` carries `part("M2","FR-GUARD-2,8")`.
  - Unmarked tests: `test_story_isolation_schema` (enumeration, FKs, story-like columns, `characters.story_id` NOT NULL, no cross-story events), the bad-request negatives (missing `true_age` → `body.characters[0].true_age`, and seven more, with no story or operation written), 423 busy, viewpoint defaults, and library creation with a failed genesis.

  A mutation check removed `policies` from `STORY_TABLES`. The first draft of `test_s_delete` still passed, because it found the story's rows through `STORY_TABLES`. After the fix, both S-DELETE tests fail under that mutation. The `test_i_10` part body avoids the M3 lexicon word "genesis".
  **Why:** The test plan and Discretion ("schema isolation test folded into `test_s_delete`"). `test_story_isolation_schema` has no marker because it maps to no docs/04 test ID (#44's constraint). REST tests use `httpx2.ASGITransport` with the Engine bound directly (`tests/story_support.py`).
  **Issue:** #50
- **Decision:** The Model Access Layer is the package `epistrel.orchestrator.models/`: `registry.py`, `adapter.py` (protocol, `Message`, `ModelParams`, `ModelResult`, `ModelError`/`ModelUnavailable`, `UnknownPurpose`, `UndeclaredInput`), `config.py`, `prompts.py` + `templates/*.j2`, `openai_compatible.py`, `stub.py`, and `access.py` (`ModelAccessLayer`). The `Engine` owns one as `engine.models` (built in `Engine.__init__`, bound with the database), and tests reach the stub as `engine.models.stub`. The `model_config` table is declared in `core/tables/model_config.py`; only the orchestrator reads or writes it.
  **Why:** The artifact list names these files, and Discretion pass 2 says the MAL is injected via `Engine.from_settings`. The table sits with the others so the drift test and migrations keep one `metadata`. Declaring a table imports nothing from the orchestrator, so the import-linter contract still holds.
  **Issue:** #51
- **Decision:** I resolved the dependencies by lookup (`uv add`). The runtime dependencies are `openai>=3.28.0`, `cryptography>=50.0.2` and `jinja2>=3.1.6`. The Discretion floor was `openai>=2.20`, but 3.28.0 is the current release. `respx` was not added. openai 3 sends requests through `httpx2`, and respx 0.23.1 only intercepts `httpx`, so the openai adapter's tests use a stdlib `ThreadingHTTPServer` on 127.0.0.1 (`tests/model_support.py::mock_openai`) that records each request's path, `Authorization` header and body.
  **Why:** Versions are never recalled. The test plan asks for "a local mock server records the request path", and a real socket proves `base_url` is honoured without injecting a transport.
  **Issue:** #51
- **Decision:** `cffi` 2.1.1 (pulled in by `cryptography`) is licensed `MIT-0`, which is not on invariant 2's allowlist. I added the exception `cffi = "MIT-0 …; runtime dependency of cryptography, used unmodified (#51)"` under `[tool.epistrel.gates.licenses].exceptions`. The allowlist itself is unchanged. `tests/gates/test_licenses.py` now builds its synthetic `CONFIG` from the repo's allow and deny lists without its exceptions (Rule 3). Its closures never contain `cffi`, so the real exception showed up there as "stale".
  **Why:** `/n8-plan M2` deferred "the license-exception rationales if the M2 closure surfaces any" to execution. MIT-0 is MIT without the attribution clause, as permissive as the allowed 0BSD and Unlicense, and in no denied family. Fernet needs `cryptography` (Discretion). `check-licenses`: 40 judged, 1 excepted, 0 offending. The owner may prefer adding `MIT-0` to the allowlist instead.
  **Issue:** #51
- **Decision:** The ADR is **ADR-150**, not the ADR-148 the Discretion names. ADR-148 (Python 3.14) and ADR-149 (#44) already exist. ADR-150 amends ADR-007: ADR-007 gains an "Amended by" line and an inline *[superseded by ADR-150]* marker on "default adapter over LiteLLM", and its standing becomes "Partly superseded". The ADR indexes were edited by hand: FR-MODEL gains 150, and a new NFR-PORT row lists 150. docs/02 §5's transport bullet and the §13 row are rewritten, with LiteLLM moved to Alternatives. docs/04 C-04 now names the `openai`-SDK adapter, and the §13 tooling line names the local mock server. The footprint was measured with `uv pip install --dry-run` into an empty 3.14 venv: `litellm` 1.104.2 installs 58 packages, `openai` 3.28.0 installs 14. Discretion's "edits docs/02 §3 line 86" was not applied: that line is the Python-version bullet, and §3 has no transport text to change.
  **Why:** ADR numbers are append-only. The AC asks for §5 and §13.
  **Issue:** #51
- **Decision:** Configuration rules.
  - Resolution is per field from `default`, and `params` inherit per key; call params override row params.
  - A null effective `base_url` or `model` is 503 `model_unavailable` `reason=not_configured`, and the SDK default endpoint is never used. No key is `reason=no_api_key`, raised now by the openai adapter. An undecryptable key is `reason=auth`.
  - `ModelParams` (`extra="forbid"`): temperature 0–2, top_p 0–1, max_tokens 1–4,000, timeout in (0, 600] (default 60 s), stop up to 4 strings.
  - The library-level writes are `config.write_config` (upsert of the given fields; the key is Fernet-encrypted before SQL; `base_url` must be http(s) with its trailing slash stripped) and `remove_override`. There is no REST write.
  - `GET /admin/model-config` returns `{adapter, default, purposes: {<delivered>: {base_url, model, params, api_key: "***"|null}}}`.
  **Why:** Discretion (pass 1/2). `no_api_key` is #52's AC, but the adapter has to do something without a key, and the SDK would otherwise read `OPENAI_API_KEY` from the environment. #52 adds the PUT/DELETE routes, `source`, the fingerprint and `config_revision` on top of these functions.
  **Issue:** #51
- **Decision:** `EPISTREL_CONFIG_KEY` must be URL-safe base64 of 32 bytes (44 characters), and the error never echoes the value. `EPISTREL_MODEL_ADAPTER` is `openai|stub` (default `openai`), and any other value exits 78. I also added `EPISTREL_STUB_LATENCY_S` (0–600, default 0) now. A stored key with no config key makes `epistrel serve` exit 78 from a pre-flight check before uvicorn starts. The lifespan runs the same check and raises `ConfigError`. Both skip the check under the stub. When the database is unreachable, the check is skipped with a warning, so `/health` still reports the problem. #44's `test_config_key_is_a_known_secret_setting` used `"k" * 44`, which is not a valid key, so it now uses a real one, and a negative test was added.
  **Why:** Discretion: "missing key with any configured purpose → startup exit 78", checked in the lifespan and skipped under the stub. uvicorn exits 3 on any lifespan failure, so exit 78 needs the CLI check. The stub-latency variable is set now because `Settings` refuses unknown `EPISTREL_*` names.
  **Issue:** #51
- **Decision:** Registry rules.
  - A field's category is its own `Annotated` tag, or else the nearest enclosing field's tag. A field holding models needs no tag when every field of those models has one.
  - More than one tag on a field, an untagged leaf, or a category in `never_receives` raises `RegistryError` at import.
  - Reserved purposes (name → milestone): `regeneration`, `entailment`, `consolidation`, `importance_scoring`, `salience_detection`, `extraction_backstop` and `quality_judge` → M4; `belief_adjudication` → M5 (defaults to entailment); `time_estimation` → M6 (defaults to extraction_backstop); `rating_suggestion` → M3 (defaults to guardrail_check); `image_prompt` → M8 (defaults to generation); `image_check` → M8 (no default); and the ports `embedding` (M4) and `image_generation` (M8).
  - `guardrail_check` declares the response schema `GuardrailVerdict(violation, confidence 0..1, reason="")`.
  - Calling a reserved purpose is 422 with `details.reason=reserved` and its milestone.
  **Why:** Discretion fixes the categories, schemas, floors and `never_receives`. The milestones come from docs/03 §22, where each requirement that uses a purpose is assigned. The AC's stub verdict (`violation: false, confidence: 1.0`) needs a schema to conform to; #53 validates it.
  **Issue:** #51
- **Decision:** Templates are loaded with `PackageLoader` (`StrictUndefined`, autoescape off, `trim_blocks`/`lstrip_blocks`, built-in filters only). The `system` and `user` blocks are rendered separately into one message each. The import-time check combines `meta.find_undeclared_variables` against the schema's top-level fields with a render against `example_instance(schema)`, where every optional field and list is filled so that conditional and loop branches run. The template wording is a placeholder that #55, #57 and #72 replace.
  **Why:** Discretion (pass 1/2). An attribute a nested model lacks (`player_input.true_age`) is only found by rendering. Before the tests were written, a mutation run that disabled both checks made 3 of the 4 fixture cases in `test_template_with_undeclared_field_fails` fail; the fourth, a missing block, is caught separately.
  **Issue:** #51
- **Decision:** Adapters.
  - The openai adapter passes the timeout as `create(timeout=…)` rather than through `client.with_options(timeout=)`.
  - Clients are cached per `(base_url, sha256[:8] of the key)`, and the cache is dropped when the revision changes. The revision is the newest `updated_at` among the purpose's own row and the `default` row. Old clients are not closed until `aclose()`; #52 closes them once idle.
  - An endpoint that answers `max_completion_tokens` with a 400 is retried once with `max_tokens` and remembered per `base_url`.
  - SDK exceptions propagate unmapped from both adapters; #53 maps them.
  - The stub records declared field paths through the `REQUEST_FIELDS` context variable, which `call()` sets. It reports the configured model when a database is bound and `stub` otherwise. Faults raise `openai.APITimeoutError` and `InternalServerError`, while `garbage` returns unparseable text.
  - The conformant default fills a declared default where there is one, and otherwise `False`, `0`/`ge`, `min(1.0, le)`, or empty.
  **Why:** `create(timeout=)` has the same effect and does not copy the client on every call. A field-paths-only record lets the payload test check "⊆ declared" without storing values. Pass 2 says faults raise SDK types and the stub path bypasses `model_config`.
  **Issue:** #51
- **Decision:** Tests.
  - `tests/conformance/test_c_04.py::test_c_04` carries `part("M2","FR-MODEL-1,2,3,8,11,13")` and is parametrised over `openai` (against the local server) and `stub`. It runs the shared routine in `tests/conformance/suite.py`.
  - `tests/conformance/test_c_07.py::test_c_07__swap` carries `part("M2","NFR-PORT-1,2")`. It runs the routine under both adapters and asserts that nothing under `src/epistrel/core` changed (a SHA-256 over the files) or names the MAL or the adapter setting.
  - Unmarked: `tests/test_registry.py` (14 tests; the undeclared-field fixtures run through the same `check_template`/`load_templates` used at import, with the complement that the shipped templates load), `tests/test_model_adapters.py` (13 tests; cross-process determinism by running `epistrel.tools.model_probe` twice under the stub), and `tests/integration/test_api_key_at_rest.py` (ciphertext at rest, with the complement that it decrypts; the start-up refusal through the lifespan and the CLI).
  - `epistrel.tools.model_probe` covers the Demo.
  **Why:** The test plan. C-07's capability-reporting half and NFR-PORT-3 belong to #54. The part bodies avoid M4 and later lexicon words: the scan helpers that use `ast.Import` sit outside the test function, because "import" is an M7 term.
  **Issue:** #51
- **Decision:** Model-configuration writes go through #48's path in `orchestrator/models/config.py`. Each write takes `lock_deployment`, bumps `deployment_state.config_revision` by one, and records `config_changes` rows with class `prospective`, scope `deployment` and the principal as actor. There is **one row per changed field**, all at one revision: `setting = model_config:<purpose>.<base_url|model|params|api_key>`, with old/new as the values. API-key rows record fingerprints (or `"unreadable"` for a stored key that cannot be decrypted), never the key. A write that changes nothing writes nothing, including setting the same key again. `write_config` (#51's library write) now runs this path too and returns `ConfigWrite(changed, config_revision)`. `replace_config`, `remove_override`, `set_api_key` and `remove_api_key` are built on it.
  **Why:** Discretion pass 1 says "one `config_changes` row per PUT with JSON old/new", and pass 2 says `setting = "model_config:<purpose>.<field>"`. A per-field setting cannot also be one row per PUT that changes several fields, so I followed the later pass and #48's settings pattern (one row per changed field, one revision per write).
  **Issue:** #52
- **Decision:** The adapters' revision is now the deployment `config_revision` (an int), not #51's newest `updated_at`. `ConfigCache` (on `ModelAccessLayer.cache`, cleared by `bind`) reads the revision on a short connection of its own on every lookup. Only when the revision moved does it re-read all `model_config` rows, in one REPEATABLE READ snapshot together with the revision. In the `openai` adapter, a revision change moves every current client to "retired". An idle client is closed at once; a busy one is closed by the last of its in-flight calls (`closed_clients` counts both). `tests/model_support.restored_model_config` now also bumps `config_revision`, so a raw-SQL reset is never served from a cache.
  **Why:** AC 1 and Discretion: the cache is keyed by config revision, the revision is read once per call on a separate short connection, and old clients are closed after their in-flight calls. Any other configuration write (ratings, settings) also moves the revision, which costs one extra re-read of a small table. I checked that the runtime test bites: with the cache refreshing only once, `test_c_04__runtime` failed (`'model-a' == 'model-b'`).
  **Issue:** #52
- **Decision:** REST shapes.
  - `GET /admin/model-config` returns `{adapter, config_revision, purposes}`. `purposes` is keyed by `default` plus every delivered purpose, and #51's separate top-level `default` is gone. Each entry has `base_url`, `model`, `params`, `api_key` (`"***"`/null), `api_key_fingerprint` and `sources {base_url, model, api_key, params: {key: own|default}}`. Every field of `default` is `own`.
  - `GET /{purpose}` adds `purpose` and `config_revision`.
  - `PUT /{purpose}` returns 200 with the entry plus `config_revision` (`DeploymentMutationResponse`).
  - `DELETE /{purpose}` returns 200 with the now-inherited entry; it returns 204 when there is nothing to drop (an all-null row is dropped silently) and 409 `validation_error` `reason=is_default` for `default`.
  - `PUT|DELETE /{purpose}/api-key` return 204.
  - Configurable purposes are `default` plus the delivered ones. A reserved purpose (`entailment`) or an unknown one is 422 `field=path.purpose`.
  **Why:** Discretion lines (GET map incl. `default`, `source: own|default`, fingerprint, single-item GET, PUT 200 / key 204, `default` undeletable, no-op DELETE 204). I put `sources` in a sibling object rather than wrapping each value as `{value, source}`, so #51's flat fields and C-04's assertions keep working. The 409 for `default` matches #48's "delete the default rating" conflict, and `remove_override` now raises `ConfigConflict` instead of #51's `InvalidRequest`.
  **Issue:** #52
- **Decision:** Validation (`ConfigReplacement`, `ApiKeyBody` in `models/config.py`, both `extra=forbid` and whitespace-stripped):
  - `base_url` must be `http(s)` with a host, no spaces, at most 2,048 characters, and has its trailing slash stripped.
  - `model` must be 1..200 characters after trimming.
  - `params` must satisfy #51's `ModelParams`.
  - The key is a `SecretStr` of 1..512 characters after trimming.

  An omitted field and `null` both inherit. `api_key` in the PUT body is 422 `body.api_key`. The idempotency header is accepted and not needed, because a repeated PUT is a no-op.
  **Why:** Discretion (pass 1/2). The 2,048 cap is my bound, since Discretion gives none for `base_url`. `SecretStr` keeps the key masked in any repr of the request model.
  **Issue:** #52
- **Decision:** The runtime test is `tests/conformance/test_c_04.py::test_c_04__runtime` with `part("M2","FR-MODEL-4")`, parametrised over `openai` and `stub`. It is not named `test_model_config_runtime`. The REST tests live in `tests/integration/test_model_config_api.py`, not in `tests/test_model_config_api.py`, and carry no marker.
  **Why:** docs/04 §15 maps FR-MODEL-4 only to C-04 (M2). A part on `test_model_config_runtime` maps to the undefined test ID MODEL-CONFIG-RUNTIME, which `check-traceability` refuses (#44's constraint). C-04's M2 slices must share one file (#50's constraint). The test runs the "same PID" check, three successive changes (model, then `base_url`, then key), and for `openai` asserts the exact `(path, Authorization, model)` sequence the mock saw. The integration directory gives the DB tests their marker.
  **Issue:** #52
- **Decision:** `test_c_09` (`part("M2","NFR-SEC-3")`) scans every public table except `model_config` and `alembic_version`, which is a superset of the planned list. It asserts that the scanned set covers every table with a `story_id` column plus `stories` (from `information_schema`), `config_changes`, and `idempotency_keys`/`jobs` when they exist. Neither of those two exists yet; #61 and #70 add them, and the test picks them up automatically. Telemetry is every record captured at DEBUG (formatted message, args and `__dict__`), every REST response body and header, and `/metrics`, which is unrouted until #74 and returns 404 for now. Complements: the decrypted `model_config` ciphertexts contain both canaries, both were sent as `Authorization` to the mock provider, and the `model_config` rows hold no plaintext. A mutation that logged the key instead of its fingerprint made the test fail.
  **Why:** Discretion: "C-09 enumerates story tables via `information_schema`, captures caplog + `/metrics`; widened by #74", and "excludes `model_config`". Scanning every table is strictly stronger than scanning only the named ones.
  **Issue:** #52
- **Decision:** No-key handling stays where #51 put it: the `openai` adapter raises 503 `model_unavailable` `reason=no_api_key` on the call, and startup never checks for a key. No REST route makes a model call yet (turns are #57), so the test asserts the library error, and asserts separately that the app's lifespan starts with no key configured.
  **Why:** AC 2 ("on first use, not at startup") and the Discretion "`no_api_key` applies to the openai adapter only".
  **Issue:** #52
- **Decision:** The Model Access Layer's error family moved to `orchestrator/models/errors.py` (`adapter.py` re-exports `ModelError`/`ModelUnavailable`). `ModelAccessLayer.send` is the one place provider failures stop: every adapter call goes through `map_provider_error`, which maps `APITimeoutError` → `timeout`, `APIConnectionError` → `connection`, 429 → `rate_limited` (with `Retry-After` passed through), 401/403 → `auth`, a 400 whose body names `response_format` → `ResponseFormatRejected` (a `ModelUnavailable`, so an uncaught one is still a clean 503), any other status → `http_error` with `provider_status`, and `APIResponseValidationError`/`JSONDecodeError`/`ProviderGarbage` → `garbage`. Anything else (an engine bug) propagates as itself. The mapped error is raised `from None` and its message names only the class, never the provider's text. `EpistrelError.response_headers()` carries `WWW-Authenticate` and `Retry-After` to the single REST handler.
  **Why:** #51 left SDK exceptions unmapped for this story. One mapping in the MAL covers both adapters and every caller. Provider error bodies can echo the prompt, so chaining them would put story content into tracebacks and logs (invariant 3).
  **Issue:** #53
- **Decision:** Rule 1 fixes in #51's `openai` adapter. A 400 whose message names `response_format` no longer flips the base URL to legacy `max_tokens`, and a 200 that is not a chat completion (an HTML page arrives from the SDK as a `str`; `{}` has `choices=None`) now raises `ProviderGarbage` instead of `AttributeError` or an empty answer. A probe against a local server confirmed both shapes before the fix. `ModelResult` gained `refused` (the message's `refusal`).
  **Why:** Without the first fix, the structured fallback's 400 would have poisoned the `max_tokens` memo for that endpoint. Without the second, a garbage body would surface as a 500 or as an empty generation.
  **Issue:** #53
- **Decision:** `orchestrator/budget.py::TurnBudget(max_calls=None, deadline_s=None)` ships in minimal form (`can_afford`, `debit`, `remaining_s`, `expired`; monotonic clock). `send` refuses an unaffordable call with `BudgetExhausted` (code `budget_exhausted`, 503) and refuses an expired deadline with `ModelUnavailable(deadline)`, both before calling. It bounds each call by `asyncio.timeout(min(per-call timeout, time left))`, and the expiry maps to `timeout` or `deadline` depending on which limit was the binding one. The per-call timeout is the call's `params.timeout`, else the purpose's configured `params.timeout`, else 60 s, and the stub is now bounded too. #69 adds reservations, planning and the turn report on top. `structured_call` takes `budget` as a required keyword; tests pass `TurnBudget()` for "unbounded".
  **Why:** The AC needs a budget and a deadline that bound every call, and #69 (which owns `TurnBudget` in this file) is not executed yet. A required keyword makes every structured caller decide its budget. The stub ignored `params.timeout` before, so a hung stub call never ended; the pre-fix probe showed a held call still running after 3 s.
  **Issue:** #53
- **Decision:** `structured_call` is `orchestrator/models/structured.py::structured_call(mal, purpose, schema, inputs, *, params, deterministic_first, budget)`, exposed as `ModelAccessLayer.structured_call`, and returns `T | Uncertain(reason, attempts)`. Its behaviour follows the Discretion lines:
  - the deterministic step runs before any call;
  - `json_schema` mode with `strict: false` and the schema left out of the prompt;
  - a 400 naming `response_format`, or a 200 that is not JSON, flips `ResponseFormatMemo` for `(base_url, model)` at the current `config_revision`; the probe debits the turn budget but not the retry budget;
  - in fallback, the schema dump is appended to the system message;
  - each retry appends the invalid answer as an `assistant` turn plus a `user` correction listing the error locations and pydantic messages (never the values), at `temperature=0`;
  - code fences are stripped;
  - validation is `model_validate_json(strict=True)` against the `extra="forbid"` schema;
  - `refusal` or a `length` finish is `parse_error` at once, and low confidence is `low_confidence` at once;
  - when the budget cannot afford a call, the result is `budget_exhausted`; a deadline expiry is `ModelUnavailable(deadline)`, and transport faults are never retried.

  The `(base_url, model)` key comes from `ModelAccessLayer.endpoint` (the adapter's name stands in under the stub without a database). `payload_fields` moved from `access.py` to `prompts.py` (re-exported) to avoid an import cycle.
  **Why:** These are the Discretion lines (pass 1/2). Keeping the memo on the MAL instance, cleared by `bind`, matches #52's `ConfigCache` lifetime.
  **Issue:** #53
- **Decision:** `structured_retry_budget` (0–10, default 2) and `uncertain_below` (0–1, default 0.5) are read from `deployment_state.settings` through #52's `ConfigCache`. It now re-reads the model rows and these settings in one REPEATABLE READ snapshot whenever `config_revision` moves, so a write to them must move the revision, as every configuration write does. An invalid stored value falls back to the defaults, with a warning that names only the keys. There is no REST write yet; `GET/PUT /admin/settings` still exposes only #48's two fields.
  **Why:** Discretion: "settings live in #48's `deployment_state.settings`". #48 created that document unexposed, and #64 extends the settings surface. The defaults are the conservative values, so a bad document cannot loosen validation.
  **Issue:** #53
- **Decision:** `GuardrailVerdict` is now `{violation: bool, confidence: float 0..1}`; #51's `reason: str = ""` was removed. The guardrail template's JSON hint and the C-04 mock's verdict changed to match.
  **Why:** Discretion pass 2: "no free-text field". With `extra="forbid"`, a model that still sends `reason` gets a `schema_error` and is retried.
  **Issue:** #53
- **Decision:** `deterministic_first` is a zero-argument callable returning `T | None`. A structured call for any purpose except `generation` or `lite_summary` without one raises `MissingDeterministicStep` (a `TypeError`) before any call. For guardrails, `guardrail_deterministic_first(text, policies, target)` wraps #49's `evaluate`: a deterministic violation is conclusive (`violation=True, confidence=1.0`), and anything else goes to the model. `uncertain_verdict(on_uncertain)` and `resolve_uncertain` give the conservative default.
  **Why:** Discretion pass 1/2. #56/#68 build the real gate on these.
  **Issue:** #53
- **Decision:** `EmptyCompletion` is a `ModelError` with code `model_unavailable`, status 503 and `details.reason = empty`. It is not a `ModelUnavailable` subclass. `complete`/`call` raise it only when no response schema is in play.
  **Why:** Discretion: it is a `ModelError` the caller handles (#57 regenerates once). Keeping it out of the `ModelUnavailable` subclasses means a transport-fault handler cannot swallow it, and if it escapes, the REST response is already #57's final 503 `reason=empty`.
  **Issue:** #53
- **Decision:** Stub faults now cover `timeout`, `connection`, `http_500`, `http_429` (`Retry-After: 2`), `http_401`, `response_format_400`, `bad_body`, `garbage`, `empty`, `refusal`, `length` and `hang`. A `fault_hook(purpose)` draws faults at random for chaos runs. A purpose that declares a response schema answers conformant JSON whether or not `response_format` was sent, which keeps the stub's fallback path realistic.
  **Why:** Discretion pass 2 (the `response_format_400` kind, stub-driven tests). The hook lets X-05 apply its 20 % rate to every call instead of guessing how many calls each request makes.
  **Issue:** #53
- **Decision:** The test-only operation is `epistrel.testing.ops.guarded_reply`, with a router that tests register through `include_protected`: `POST /_test/stories/{story_id}/guarded-reply`. It runs one `turn` `Operation`: an input check (deterministic-first, then `guardrail_check` per model-assisted policy), `generation`, an output check, and one `test_event` (`GuardedReplyEvent`: the said text, the generated reply or null, and the verdicts with what decided them; it holds no guardrail model text). `test_event` is registered as `narrative_state`, only when the module is imported. `create_app` and the Engine never import `epistrel.testing`.
  **Why:** Discretion pass 2 ("test-only operation lives in `epistrel.testing.ops` and writes a `test_event`"). A `turn` operation makes a failure's "no revision change" meaningful. The payload class is not named `TestEvent` because pytest tried to collect that name.
  **Issue:** #53
- **Decision:** Tests.
  - `tests/conformance/test_c_04.py::test_c_04__structured_output` carries `part("M2","FR-MODEL-6,7")` and `test_c_04__deterministic_first` carries `part("M2","FR-MODEL-5")`. Both run under both adapters.
  - `tests/chaos/test_x_05.py::test_x_05` carries `part("M2","NFR-REL-3; FR-MODEL-6")` and the new `chaos` marker (registered in pyproject for #75). It runs `EPISTREL_CHAOS_ITERATIONS` (default 50) requests at a 20 % fault rate, with a 5 s turn deadline, a 1 s call timeout, and random holds that the harness releases. The seed is printed, and `EPISTREL_CHAOS_SEED` replays a run.
  - Unmarked: `tests/test_structured_output.py` (51 tests, no database) and `tests/integration/test_model_unavailable.py` (10 REST cases: 503 with purpose, reason, `Retry-After` only on 429, a `failed` operation, no event, no revision change, and as the complement the same request then commits).

  The story's artifact list named the plan's FR-MODEL slices `tests/test_structured_output.py`. They are C-04 slices instead.
  **Why:** docs/04 §15 maps FR-MODEL-5,6,7 only to C-04 (M2), so a part on `test_structured_output` would map to an undefined test ID, and C-04's slices must share one file (#44/#50/#52's constraint). X-05 runs in the normal suite, which is how it joins PR CI as `chaos_quick`; there is no separate CI job. Before the implementation, a probe showed the faults misbehaving: a held stub call was still running after 3 s, `timeout` and `http_500` escaped as raw `openai` exceptions (REST 500, not 503), and garbage reached the caller as a pydantic `ValidationError`. #51's `test_c_09` expected the raw `InternalServerError`, and now asserts the mapped, redacted 503 instead.
  **Issue:** #53
- **Decision:** These Discretion items have nothing to act on yet and are left to the stories that build the pieces. "A 503 releases #63's lease and leaves #61's key re-executable": neither the lease nor the idempotency table exists. "Counted against the turn budget": the budget is passed explicitly until #57/#69 put it on the `TurnContext`.
  **Why:** Those stories own the pieces. The behaviour they need already holds: a failed operation stores nothing, so a retry runs again from scratch.
  **Issue:** #53

- **Decision:** The model guide is `src/epistrel/tools/model_guide.py` → `docs/models.md`. It lists the purposes in registry order. Each has a summary row and a section with description, hot path, default model key, template, capability floor, the returned schema, **Receives** (each leaf field path with its category) and **Never receives**. Then come the reserved purposes, a content-category table and one global hosted-retention footer, under a "generated" banner. `--check`/`--write` come from the shared helper `src/epistrel/tools/generated.py` (`main(argv, prog, description, default_path, generate)`), which #73's OpenAPI tool can reuse. The check is a byte-exact comparison that prints a unified diff. It runs as the `model-guide` step of `ci.yml`'s `lint` job. A purpose with no `CapabilityFloor`, or whose input schema declares no field (no data-flow row), makes the generator raise `GuideError`, and so does a content category without a guide description. Before `docs/models.md` existed, `--check` exited 1 (the guard was run against the pre-fix state first).
  **Why:** AC 1 and the Discretion lines (pass 1/2). "Data-flow row" is read as "declares at least one input": `never_receives` is a required `Purpose` field (#51), so it always exists, and it may be empty (rendered "—"), as some docs/02 rows are.
  **Issue:** #54
- **Decision:** `.github/CODEOWNERS` has `* @nathanpond` and names `/src/epistrel/orchestrator/models/`, both tools, `docs/models.md`, `docs/model-matrix.md` and `docs/model-matrix.json`. docs/02 §5's data-flow lead-in now says the table "is superseded by the generated `docs/models.md` from M2".
  **Why:** AC 1 and AC 4, Discretion (CODEOWNERS also covers the tools and the matrix, plus a `*` default owner).
  **Issue:** #54
- **Decision:** Counting happens in `ModelAccessLayer.send`, not in an `InstrumentedAdapter` wrapper. `orchestrator/models/metrics.py` uses `prometheus-client` 0.26.0 (resolved by `uv add`; Apache-2.0, `check-licenses` 41 judged / 0 offending). It defines `epistrel_model_calls_total{purpose,model,outcome}`, `epistrel_model_tokens_total{purpose,kind}`, `epistrel_model_cost_total{purpose,model}` (USD) and the histogram `epistrel_model_latency_seconds{purpose}`, all in the default registry. The `model` label is the configured model name, lower-cased and reduced to `[a-z0-9._-]` (at most 100 characters). Outcomes are mapped as follows: `timeout`/`deadline` → `timeout`; `garbage`/`empty` → `invalid_output`, as is an answer `send`'s new `usable` callback rejects (empty text, or a structured answer that fails `parse`, refuses, or stops at `length`); an external cancellation → `cancelled`; every other `ModelError` reason (connection, 4xx/5xx, 429, auth) → `http_error`. `not_configured`/`no_api_key` are not recorded because no request was sent, and neither is a call the budget refused.
  **Why:** The wrapper sits below `send`, so it would see unmapped SDK exceptions, and an `asyncio.timeout` expiry would reach it as a `CancelledError`. `send` is the one place where the outcome is already known (#53). The outcome set is the Discretion's, so `connection` falls into `http_error`.
  **Issue:** #54
- **Decision:** `model_calls` entries are `metrics.ModelCall {purpose, model, attempt, outcome, latency_ms, tokens_in, tokens_out, cost?, request_id, finish_reason}`. An absent `cost` is omitted from the dump (`exclude_if`), and tokens are 0 when the endpoint reports no usage. Entries accumulate in a `CallLog` on the contextvar `CALL_LOG`, opened with `metrics.recording()`. `structured_call` passes its attempt number. The openai adapter now reads `usage.cost` from the SDK's extra fields (`reported_cost`; it was always `None`, Rule 1), and the stub keeps reporting 0.0. There is no `respond` until #57, so #53's guarded-reply test route now returns `model_calls`. #57 opens `recording()` around the turn and returns `log.calls`, and #69's `TurnBudget` can wrap the same log.
  **Why:** Discretion: "accumulation on `TurnBudget` (#69) via contextvar"; #57's "`model_calls` accumulate on a contextvar `TurnContext` that #69's `TurnBudget` later wraps". #69 has not run, so the log stands alone for now, and the budget stays an explicit argument as #53 left it.
  **Issue:** #54
- **Decision:** `ModelAdapter` gains `probe(purpose) -> ProbeResult(models, model)`, and `ModelAccessLayer.probe` bounds it at 30 s and maps its errors. For the openai adapter it is `GET /models` plus one 16-token completion. The stub completes once (a queued fault applies to it) and lists its model. `orchestrator/models/capability.py` keeps protocol (`check_protocol`) apart from per-purpose capability (`assess_purpose` → `context` and `structured_output` `FloorCheck`s). Context: about `min_context_tokens - 192` estimated tokens (characters / 4) must complete non-empty within 120 s. Structured output: at least 95 % of 20 first-attempt calls must return a valid synthetic `ProbeAnswer`. The check starts in `json_schema` mode, falls back to the schema in the prompt on a `response_format` 400 (the 400 is not counted) or on prose, and records the mode it ends in. The structured check is skipped (`passed=None`) for floors of `none`. When protocol fails, no capability is reported at all.
  **Why:** Discretion pass 1/2 (protocol ✓ = `/models` + one completion; json_schema acceptance is capability; 8k probe and 120 s; synthetic schema; the mode is recorded). Not reporting capability after a protocol failure keeps the two results separate in both directions.
  **Issue:** #54
- **Decision:** `epistrel.tools.model_matrix` runs an in-process `Engine` on the `openai` adapter, against a testcontainers PostgreSQL or `--database-url`. Its flags are `--api-key` (default `lm-studio`), repeatable `--purpose-model P=M`, `--tier` (default `local`), `--hardware`, `--server-version` and `--turns` (default 20). It probes protocol, assesses every delivered purpose, runs E-06, and then for each row (policies off / minors policy on, with a cast that includes a 12-year-old and benign scripted inputs) plays 1 warm-up turn plus 20 timed turns. It reports nearest-rank p50/p95 and the mean calls per turn by purpose. Rows are keyed `tier|model|policies` in `docs/model-matrix.json`, which holds the raw timings, and a re-run replaces its own rows. `docs/model-matrix.md` is rendered from all rows under a "Reported, not gated" banner. An unreachable endpoint, or a `ModelError` part-way through, exits 1 and writes nothing. The recorded SHA carries `-dirty` when the working tree has changes.
  **Why:** Discretion pass 1/2 (in-process Engine, row metadata, raw JSON committed beside the matrix, re-runs replace, nearest-rank, unreachable → exit 1). `-dirty` keeps the row honest when it is measured before the commit, as this story's own row was.
  **Issue:** #54
- **Decision (deviation):** A matrix "Lite turn" is #53's guarded reply (`epistrel.testing.ops.guarded_reply`): the input check per model-assisted policy, one `generation` call, and the output check. It goes through a `TurnDriver` protocol (`GuardedReplyDriver`), and each row records `turn_path`. The minors policy is a model-assisted policy whose prompt describes the minors rule. The guarded reply does not pass the cast's ages as subjects and does not accumulate story history, so the "accumulating story" part of the 20-turn fixture is not met until #57.
  **Why:** `respond` is #57, which depends on this story (#54 is blocked only by #53), so no real Lite turn exists yet. The guarded reply has the same call shape (1 call with policies off, 3 with the minors policy on) and makes real model calls through the same layer. #57 should add a `RespondDriver` and re-measure. Low cost: one class and a re-run.
  **Issue:** #54
- **Decision:** The `live_model` marker is registered in pyproject. `tests/conftest.py` deselects `live_model` tests unless `-m` names the marker, and `ci.yml` also passes `-m "not live_model"`. `tests/live_support.py` reads `EPISTREL_LIVE_BASE_URL/_MODEL/_API_KEY` (the key defaults to `lm-studio`) when it is imported, because the autouse fixture strips `EPISTREL_*`. A selected test with no endpoint `pytest.fail`s. `test_e_06` (`part("M2","FR-RESP-3")`, `tests/eval/`) runs on a database-free layer. `test_p_08` (`part("M2","NFR-PERF-8")`, `tests/perf/`) runs the matrix's turn measurement on the test database.
  **Why:** Discretion: "registered in pyproject, deselected in ci.yml" and "fail when unset under the marker". Deselecting by default as well keeps a plain `uv run pytest` (the local gate) from failing for want of a model.
  **Issue:** #54
- **Decision:** Tests. `tests/test_model_guide.py` (7, unmarked): committed == generated; a registry change (`generation` gains a field) makes `--check` exit 1 with the field in the diff, and `--write` fixes it; missing file; truncation; idempotent write. `test_c_04__guide` has `part("M2","FR-MODEL-10,13")` and uses a synthetic purpose with no floor and one with no data-flow row; the complement is that the synthetic purpose renders when complete. `test_c_04__call_counts` (both adapters) and `test_c_04__call_counts_on_the_response` have `part("M2","FR-MODEL-12")`. `test_c_07__capability` has `part("M2","NFR-PORT-3")` and runs against the stub: 20 garbage answers make `guardrail_check` incapable while protocol stays ✓, and with a connection fault the run is a protocol failure with no capability results. Unmarked: `tests/test_model_metrics.py` (16), `tests/test_model_matrix.py` (10), `tests/integration/test_model_matrix_run.py` (2: a 3-turn end-to-end run against the local mock server, and the unreachable endpoint writing nothing) and a marker-selection test in `tests/test_markers.py`. The local mock server gained `GET /v1/models`, `usage.cost` and a canned `ProbeAnswer`. The new C-04 parts import at module level: `import` inside a part is an M7 lexicon term (#51's constraint). The first suite run tripped `check-lexicon` on exactly that.
  **Why:** The test plan. docs/04 §15 maps FR-MODEL-10/12/13 only to C-04 (M2), so these are C-04 slices in `test_c_04.py` (#50/#52/#53's constraint). "Registry reset fixture" became counter deltas (`metrics.sample` before and after), because prometheus counters cannot be reset in the default registry.
  **Issue:** #54
- **Decision:** The committed matrix comes from a live run on this machine. LM Studio 0.4.18 was already installed but had no server running. Port 1234 was taken by an unrelated Docker container, so I started the server on 1235 and loaded `cydonia-24b-v4.3` (Q4_K_M, the only installed LLM) with a 16,384-token context. The tool ran for about 9 minutes and exited 0. Both rows (policies off, minors policy on) show protocol ✓, every purpose capable (`guardrail_check` structured output in `json_schema` mode), and E-06 ✓ (32/32 and 128/128 tokens; 9 of 10 identical at temperature 0). Policies off: p50 8.00 s, p95 15.06 s, 1 call per turn. Minors policy on: p50 12.82 s, p95 19.78 s, 3 calls per turn (generation 1, guardrail_check 2). The live `test_e_06` also passed (1 passed, 17 s). Afterwards I unloaded the model and stopped the server. The SHA is recorded as `f0f4b98-dirty` because it was measured from this story's uncommitted tree.
  **Why:** AC 3 needs the committed matrix to hold the two local-tier rows. The planner expected the owner to run the tool, but the model was already present locally, the run spends nothing and is reversible, and it gives real evidence. The owner should re-run after #57 (the turn path changes) and may also want to fill in a more specific `--hardware` value.
  **Issue:** #54
- **Decision:** The seed lives in `orchestrator/policies/seeds/policies.py` (`MINORS_POLICY`), and `0009_seed_minors_policy` imports it, as `0005` imports the rating seeds. The row has id `minors_no_sexual_content`, `applies_to = {text_input, text_output}`, `kind = model_assisted`, `rule.on_uncertain = violation`, `age_condition = minor_involved_or_present`, and `created_by`/`updated_by` = `system`. The insert is silent: no `config_changes` row, and `config_revision` does not move. The prompt holds the literal placeholder `{minor_age_threshold}`, which `age.policy_prompt` replaces with the live `deployment_state.minor_age_threshold` when each check is built. The prompt also says that a claim or pretence of being older does not change a subject's listed age.
  **Why:** Discretion: the prompt is copied into the row so it can be edited, the seed inserts silently like #48's, and the threshold is read live. A placeholder keeps the prompt editable data and the threshold live. The other option, a new `GuardrailCheckInputs` field, would have changed the registry and the generated guide for one number.
  **Issue:** #55
- **Decision:** `orchestrator/policies/age.py` is pure over a cast its caller passes in. `CastMember` holds `character_id`, `ordinal`, `true_age` and `age_recorded_at`. `current_age` returns `true_age`, because M2 has no clock and #50's `age_recorded_at` is a wall-clock stamp, not story time. Lite presence is the whole cast. `applies`/`partition` split policies into active and inactive. Subjects are `character_<n>` in `ordinal` order, every cast member included. `check_inputs` sends subjects only for an age-conditioned policy and `[]` for any other. `store.guard_context(db, story_id)` returns the effective policies, the cast (`read_cast`) and the live threshold, with `active`/`inactive` properties for #56's `inactive_policies`.
  **Why:** Discretion: `age.py` is pure, all cast subjects are sent, labels follow creation order, the threshold is read live, and condition-false policies are listed as inactive. Keeping the reads in the store keeps `age.py` testable without a database.
  **Issue:** #55
- **Decision:** `AgeCondition` narrows from #49's three values to `minor_involved_or_present` alone. `minor_present`/`minor_involved` are now 422 on `/admin/policies`. The condition is allowed on any scope and on either kind. No DB CHECK was added.
  **Why:** Discretion: "accepted values: `minor_involved_or_present` only" and "allowed on any scope and both kinds". Under Lite, "involved" and "present" cannot be told apart, so a narrower value would promise something M2 cannot evaluate. No stored row used the other values. The API is the only writer (#49's contract), so a CHECK would add a migration for no gain.
  **Issue:** #55
- **Decision:** The registry's `Subject.label` now has the pattern `^character_[1-9][0-9]{0,3}$`, and `age` is bounded 0–150. Pydantic refuses any other label, a name included. The `examples=["character_1"]` metadata feeds `prompts.example_instance`, which now prefers a field's declared example: the template check's `"example"` placeholder would fail the pattern.
  **Why:** AC 2: "the registry schema enforces" labels, never names. The example change is the smallest way to keep #51's import-time render check working for constrained fields.
  **Issue:** #55
- **Decision:** The guardrail check moved from `epistrel.testing.ops.check` into `orchestrator/policies/check.py::check_text(models, text, policies, target, *, budget, cast=(), threshold=18) -> CheckOutcome(violations, decided_by)`. It runs #53's sequence unchanged: deterministic-first per model call, `on_uncertain`, and the first violation ends the check. Before any call it now drops age-conditioned policies whose condition is false. `guardrail_violation(violations, policies, what=)` builds the new `core.errors.GuardrailViolation` (422 `guardrail_violation`, `details.violations [{policy_id, name, message}]`). Its message is "Input violates N polic(y|ies)", with ": a minor is involved or present." appended when a violated policy is age-conditioned. It carries nothing about the cast. `testing.ops.check` is now a thin wrapper, and C-04 and X-05 are unchanged.
  **Why:** #56 and #68 need one shared, age-aware check with violations they can name, and #56's Discretion fixes the message form. Code under `epistrel.testing` cannot be the production home. The error lives in `core.errors` so the single REST handler renders it, as it does every other `EpistrelError`.
  **Issue:** #55
- **Decision:** The 422-on-input and the output block are proven through #53's test-only operation, not real routes. `GuardedReplyBody.story_policies: true` loads `guard_context`, and its input check becomes a gate that runs **before** the operation opens: a violation raises `GuardrailViolation` with no operation, event or revision change. An output violation withholds the reply (`reply: null`, `output_violation: true`). The ingest path is `POST /_test/stories/{id}/ingest-check {texts}` (204 or 422), which gates each text on its own. Hand-offs: #56 calls `check_text` + `guardrail_violation` from `input_gate` and adds the audit row; #58 adds `test_r_07__ingest` through the real ingest route; #68 replaces "withhold" with regeneration and then the safe fallback, and adds the `__output` slice; #57 wires respond.
  **Why:** No respond, ingest or output gate exists yet (#56/#57/#58/#68 all depend on this story). AC 3's "triggers #68's regeneration then fallback" cannot be met before #68 exists, so the block is the part this story can deliver, and the hook #68 extends is `check_text` on `TEXT_OUTPUT`. Low cost: the test route registers only under tests.
  **Issue:** #55
- **Decision:** `StubAdapter.when(substring, response, purpose=None)` gained the purpose filter. It also accepts a tuple of substrings that must all appear. R-07's fixture phrases are registered as `(phrase, "## Subjects (label: true age)")` for `guardrail_check`, so only checks of an age-conditioned policy recognise them. The guardrail template's subjects heading became "Subjects (label: true age)", and its "placeholder wording" note was removed.
  **Why:** Discretion pass 2 adds the purpose filter. The tuple is a Rule 3 fix for the shared test database: `test_policies_api` leaves a live global model-assisted policy ("Cruel?"). With a phrase-only match, that policy's own check would flag R-07's fixture text in the adult-only complement and fail the test for a reason unrelated to the minors policy.
  **Issue:** #55
- **Decision:** Tests. `tests/redteam/test_r_07.py::test_r_07` carries `part("M2","FR-GUARD-10,11; FR-CAT-12")`. It covers euphemism, "pretend she's 18", and an older claim (each a 422 on input with the story state unchanged; the check saw `character_2: age 16`). Split across turns passes the first half and catches the second on output. The ingest path is a 422. The complement is the same cast with the 16-year-old made an adult: everything passes, and no check carries subjects. Unmarked: `tests/integration/test_minors_policy.py` covers the seed row, `test_minor_error_redaction` (no name, name parts, `16`, `character_` or the word "age" in the body; the id, name and message present), `test_no_call_without_minor` (zero `guardrail_check` calls and an unchanged `epistrel_model_calls_total` for an adult cast, with the complement of exactly one call with a minor), the live threshold (raised to 35 and restored), an audited edit-and-restore of the seed, and the `age_condition` values. `tests/test_age.py` holds 7 pure tests, and `test_model_adapters.py` gained the `when` filter test. Two mutation checks bit: with `applies` forced false, R-07 failed on the 422; forced true, it failed on the complement.
  **Why:** The test plan. `test_minor_error_redaction` and `test_no_call_without_minor` carry no marker because they map to no docs/04 test ID (#44's constraint). Their requirement coverage is R-07's and C-20's (#56).
  **Issue:** #55
- **Decision:** `epistrel.tools.model_matrix`'s "minors policy on" row still uses an unconditioned in-memory model-assisted policy and passes no cast. I did not switch it to the seeded policy here.
  **Why:** The committed matrix rows (#54) were measured that way, and the turn path changes with #57, when the owner should re-measure anyway. When #57 adds the `RespondDriver`, it should load the story's policies through `guard_context` so the row measures the real seeded policy. Low cost.
  **Issue:** #55
- **Decision:** The gate is `orchestrator/policies/input_gate.py`.
  - `input_gate(models, context, texts, *, budget, actor, reserved)` returns `None | InputViolations`.
  - `enforce_input(...)` runs the gate, writes the audit row and raises the 422.
  - `Engine.gate_input(ctx, story_id, entry_point, texts, ...)` is the entry point respond (#57), ingest (#58) and pins/instructions (#60) will call after their other prechecks and before their `Operation` opens.

  Deterministic policies run first (`evaluate` over every text), then one `guardrail_check` per model-assisted input policy per text, sequentially, through #55's `check_text` with a single policy. Nothing short-circuits. Violations are aggregated per policy (`locator` for one text, `locators` for several) and ordered deterministic-first, then by policy id. The 422 carries `model_calls` and `elapsed_ms`. `guardrail_violation` gained `locators`/`extra`.
  **Why:** Discretion lines: no short-circuit, deterministic first, locator shape, ordering, cost in the 422, and "gate invoked inside `Engine` methods". Calling `check_text` per model-assisted policy reuses #55's age, subject and `on_uncertain` handling unchanged, and passes an empty deterministic step, so the model call is never skipped by a deterministic hit on another policy.
  **Issue:** #56
- **Decision:** Budget.
  - The model-assisted checks need `G = model-assisted input policies × texts` calls, plus `reserved` (the fixture's respond passes 1 for generation). If the budget cannot afford that, the request is refused with the new 422 `budget_below_safety_minimum` (`core.errors.BudgetBelowSafetyMinimum`, `details {minimum_required, max_calls}`) before any call. Nothing is written, not even an audit row.
  - A gate outside a turn gets `TurnBudget(max_calls=G, deadline_s=45)`. `DEFAULT_TURN_DEADLINE_S` was added to `budget.py`.
  **Why:** Discretion (#69 rule; non-turn budget). `max_model_calls_per_turn` and `turn_deadline` are not story settings yet (#63/#69), so the non-turn budget is exactly G under NFR-PERF-6's default deadline. Batch ingest can therefore only trip `budget_below_safety_minimum` once #63/#69 bound it by the story setting. No audit row is written for a budget refusal because no policy decided anything.
  **Issue:** #56
- **Decision:** `audit_log` (`0010_audit_log`, `core/tables/audit_log.py`, writer `core/audit.py`) has these columns: `id char(26)`, `at`, nullable `story_id`, `principal`, `kind` (CHECK `guardrail_input_rejected|story_deleted`), `policy_ids text[]` and `details jsonb`. It has indexes on `(story_id, at)` and `(at)`. The append-only trigger allows exactly one update, nulling `story_id` with every other column unchanged, and allows a delete only under `epistrel.allow_audit_expiry` (the setting the events trigger already uses for audit expiry, for #71's sweep). TRUNCATE is refused. A rejection row's details are `{entry_point, text_length}`, where `text_length` is the total over all gated texts. The row is written in its own transaction with no operation. A `ModelUnavailable` writes no row.
  **Why:** The planned artifact and its Discretion lines (no hash, no read surface, 90-day sweep by #71). Reusing `allow_audit_expiry` gives #71 one switch for both audit stores. The `(at)` index serves that time-based sweep.
  **Issue:** #56
- **Decision:** #50's hand-off. `STORY_ID_EXCEPTIONS = {"audit_log"}`. `_delete_story_rows` nulls the story's `audit_log.story_id` (`audit.detach_story`), so creation compensation does too. The delete's commit step writes a `story_deleted` audit row (null `story_id`, the principal, `details {story_id, operation_id}`) in the same transaction as the delete. The `story_deleted` event and the story-less operation remain. `test_s_delete` now gives each story a rejected input. It asserts that the victim's audit row survives with `story_id` nulled, that exactly one `story_deleted` row is added, and that no other audit row changes. #50's AC box was already ticked, so the clause is completed here and noted on #50.
  **Why:** #50's AC and Discretion pass 2 ("delete writes an `audit_log` row (null `story_id`, principal)"). The deleted id sits in `details` because a deletion record that does not say what was deleted is useless. A story id is not story content.
  **Issue:** #56
- **Decision:** `POST /stories` gates in `Engine.create_story`. It first runs `stories.validate_spec` (unique names, default viewpoint) and resolves the rating, then gates every `characters[i].definition` against `store.creation_context`: live global policies plus the chosen rating's closure (a new story has no policies of its own), with the spec's cast as `CastMember`s. So the minors policy sees the new cast's true ages. `entry_point = story_create` and `story_id` is null. `core.stories.create_story_from` (the library helper tests use) is not gated.
  **Why:** AC 5 and Discretion ("gate invoked inside `Engine` methods"). Body validation stays first, as #44's order requires. The new story's rating policies apply only when set to input, like any rating policy.
  **Issue:** #56
- **Decision:** The fixture caller lives in `epistrel.testing.ops` (`gated_write` + `POST /_test/stories/{id}/gated-input {entry_point, texts, actor?, max_calls?}`), not on `Engine`. Its order is `require_writable` (404/`story_archived`), then `check_expected_revision`, then `Engine.gate_input`, then one operation (`turn` for respond, `inter_turn` otherwise) recording a text-free `test_gated_input` event. Respond joins its texts into one (`player_input`). The other entry points gate each item (`events[i]`, `items[i]`, `custom_instructions[i]`). #53's guarded reply (`story_policies: true`) and `ingest-check` now gate through `Engine.gate_input`, so R-07 and the minors tests run the real gate and write audit rows.
  **Why:** Discretion said "a test-only `Engine` method", but `create_app` and the Engine never import `epistrel.testing` (#53), and test code on the production surface is what #53 avoided. The production half, `Engine.gate_input`, is on the Engine. Low cost to move.
  **Issue:** #56
- **Decision:** `GET /stories/{id}/settings` (in `routes/stories.py`, via `Engine.story_settings` and `store.story_guardrails`) returns `settings` (`StorySettings` with defaults), `effective_policies` and `inactive_policies` (`id, name, scope, source, applies_to, age_condition, kind`), and `guardrail_calls {input, output}`. `source` is `global`, `rating:<contributing rating>` (nearest in the closure), `story`, or `character:<id>`. `guardrail_calls.input` counts active model-assisted input policies per text with no character acting; character-scoped ones are excluded because they apply only to their actor. `output` counts per output checkpoint.
  **Why:** Discretion lines (pass 1/2). The counts are the per-text unit an operator multiplies by the texts a request carries.
  **Issue:** #56
- **Decision:** Kept #49's `on_uncertain` default `violation` rather than the Discretion's "default `pass`" for generic policies. The gate follows each policy's stored `on_uncertain`.
  **Why:** #49/#53 already ship `violation` as the default, stored rows rely on it, and it is the conservative choice. Changing a stored-data default belongs to the policy model, not the gate. Low cost to flip in `ModelAssistedRule`.
  **Issue:** #56
- **Decision:** The C-20 part is `part("M2","FR-GUARD-8,10,11")`, not the test plan's "FR-GUARD-8,9,10,11".
  **Why:** docs/04 maps FR-GUARD-9 only to I-18 (M2), and `check-traceability` refuses a part outside the test ID's §15 slice. FR-GUARD-9's visibility is still exercised by unmarked `test_story_settings_show_policy_kinds`.
  **Issue:** #56
- **Decision:** Tests. Both C-20 tests are in `tests/conformance/test_c_20.py`.
  - `test_c_20` covers every entry point through the fixture caller, the rating-default/input pair (the rating policy is created with `POST /admin/policies {scope: rating}`), minors redaction, and an edited global policy recorded with its principal.
  - `test_audit_log_no_text` uses a canary: no 12-character piece of it appears in `audit_log`, events, operations, stories, characters or captured logs, on every entry point or at creation.
  - `tests/integration/test_input_gate.py` holds 10 unmarked tests: ordering and locators, budget refusal, character actor, `on_uncertain`, no audit row on 503, precheck order, creation gating with the minors policy, the settings endpoint, the violation counter, and the append-only trigger.
  - Two mutation checks failed C-20 as they should: a gate that never raises, and the gate moved after the operation commits.

  Rule 3 test fixes for the shared database:
  - `test_policies_api` now retires its global model-assisted "Cruel?" policy. Left live, it makes every later story creation call `guardrail_check`, which is 503 under the unconfigured `openai` settings fixture.
  - `test_u_29` clears the closure cache first. Its in-memory graph reuses the seeded rating and version ids, so a closure the new creation gate had computed from the real rows earlier in the session answered instead.
  - `test_minor_error_redaction` pops the numeric cost fields before its "no 16 in the body" scan and expects the new `locator`.
  **Why:** The test plan. The two shared-DB failures were order-dependent pollution exposed by the new creation gate, not product bugs: production version ids are unique ULIDs, and failing closed when no guardrail model is configured is the intended behaviour.
  **Issue:** #56
- **Decision:** The Lite turn is split between Core and the Orchestrator. `core/turns.py` reads the prompt inputs inside `Operation.begin`'s REPEATABLE READ snapshot: the cast, the rating guidance (via `ratings.story_guidance`), the latest instructions and pins, the active summary segments, and the window (the latest `lite_window_messages` active `message`/`narration` transcript rows, joined to their events for `say`/`do`). It also stages the beats. `orchestrator/lite/` holds `schemas.py` (request and result), `prompt.py` (`assemble`, `model_params`, `prompt_hash`) and `pipeline.py` (`respond`). `Engine.respond` passes the input gate in as a callable, because `lite` may not import `policies.admin` even indirectly through `epistrel.engine` (import-linter). The route is `api/routes/respond.py`.
  **Why:** The artifact list, the core/orchestrator rule, and #47's "read set = prompt input hashes": the prompt is built from the same snapshot the read set hashes.
  **Issue:** #57
- **Decision:** The template is now `lite_generation.j2` (the artifact name), replacing `generation.j2`. `Purpose` gains `history_field`. For `generation`, `prompts.render` sends the `system` block, then each `recent_messages` line as its own `user`/`assistant` message through the template's `line` macro, then the `user` block (the player input only). Consecutive messages with the same role are merged (`merge_roles`). A purpose with a history field whose template has no `line` macro fails the import-time check. `TranscriptLine` gains `text` (a reply, verbatim) and `GenerationInputs` gains `voiced_characters`. `lite_summary.j2` also renders `text`. `docs/models.md` was regenerated.
  **Why:** User Discretion: one `system` message plus an alternating `user`/`assistant` transcript, and the input last. The other purposes keep #51's two-message shape.
  **Issue:** #57
- **Decision:** The `system` message order is: rating guidance → custom instructions → `### Author notes: <name>` (the actor and the render characters, in cast order) → pinned memories → "Story so far" → the constant preamble ("## Your part"), which comes **last**. Empty sections are left out.
  **Why:** AC 2 and AC 7 put the guidance first and fix the order of the listed sections. Putting the Discretion's preamble at the end keeps those sections contiguous and in order.
  **Issue:** #57
- **Decision:** Generation settings. The models are `GenerationSettings {temperature, top_p, length}` (`length` is `short|medium|long` or 1..4,000) and `resolve_generation(*layers)` → `ResolvedGeneration`, both in `core/settings_schema.py`. The deployment's `lite_generation_defaults` (0.8 / 0.95 / medium) and `lite_window_messages` (30) live in `deployment_state.settings` (`LiteDefaults`, read by `config_changes.read_lite_defaults`; an invalid stored value falls back to the defaults). They are written by the library function `deployment.update_lite_defaults`, which takes one revision and writes one prospective `config_changes` row per changed key. The story overrides are `StorySettings.generation_settings` and `lite_window_messages`. The request's `prompt_params` is the same `GenerationSettings` model. The resolved settings are recorded as `operations.config_revisions.generation_settings`. The read-set member `generation_settings` now hashes deployment + story (not the request).
  **Why:** Discretion (pass 1/2). `/admin/settings` is not extended: #64 owns the settings surface, and #53 also left its settings in that document unexposed. `config_revisions` already holds "the configuration it runs under" and is rewritten on restart, so no new column is needed. The request's params are fixed for the turn, so they never change between attempts.
  **Issue:** #57
- **Decision:** Migration `0011_respond` adds `operations.idempotency_key` (written once at `begin()`) and `transcript_messages.render_character_ids` (jsonb, null on input beats). `Operation` gains `idempotency_key=` and `viewpoint=` (both inserted at begin) and `append(..., beat=)`. The beat is written to `events.beat` and carried to the transcript. `MessageRendered` gains `render_character_ids` (a tuple, so payloads stay hashable; #45's `test_time__e` hashes them), `model`, `prompt_hash` and `finish_reason`. `PlayerInput` gains `say`/`do`, and `text` remains the newline-joined pair. The history item gains `render_character_ids`.
  **Why:** Discretion: the key is stored on the operation before #61, the reply payload fields, and #59's item shape. Columns, not tables, so this is not Rule 4.
  **Issue:** #57
- **Decision (deviation):** The player input is stored as a `player_input` event of category **`input`** (#45's registration), not `narrative_state` as AC 3 says. The reply is `narrative_state`.
  **Why:** #45 registered `player_input` as `input` per FR-STORE-25, and `test_s_audit_category`, `test_event_registry` and `test_time__e` depend on it. Story views read both categories and the event carries its position, so FR-PROF-11 holds. Re-categorising would mean revisiting #45's tests for no behavioural gain. Low cost to change if the owner disagrees.
  **Issue:** #57
- **Decision:** Precheck order: story exists (404) → cast check (422 `validation_error` with `unknown_actor` / `unknown_render_ids` / `unknown_present_ids`) → archived (409) → expected revision (409) → viewpoint (422 `viewpoint_unresolved`) → input gate (budget + policies) → operation. The viewpoint is the body's `output_viewpoint`, else `?viewpoint=`, else the actor, else the story default (`core/viewpoint.py`, provisional for #59). It is stored on `operations.viewpoint`. `audience_filter` is always `none`. `characters_present` is validated and then ignored. Render ids default to every other cast member in cast order; explicit ids keep their order with duplicates dropped; an explicit `[]` is 422. A single render character becomes the reply event's `character_id`, and several leave it null.
  **Why:** #44's canonical order, with cast validation treated as part of body validation, because it needs the cast to decide. Discretion lines (pass 1/2) fix the rest. #62 owns the full cast rules (`cast.py`) and can move `resolve_cast` there.
  **Issue:** #57
- **Decision:** The turn budget is `TurnBudget(deadline_s=45)` with no call cap. The gate reserves 1 call for generation. An empty reply is asked once more and then gives 503 `reason=empty`. `model_calls` comes from #54's `recording()` around the gate and the turn, so input-gate calls are included.
  **Why:** `max_model_calls_per_turn` is #63/#69's. The Discretion says "regenerate once".
  **Issue:** #57
- **Decision:** The model-matrix tool gains `RespondDriver` (now the default; `TURN_PATH = "respond (#57)"`), which plays real `Engine.respond` turns. Its minors row uses the **seeded** `minors_no_sexual_content` policy through the turn's own guard context (#55's hand-off), not an in-memory policy. `GuardedReplyDriver` stays under `GUARDED_REPLY_PATH` for the rows measured before. The minors row now makes 1 `guardrail_check` + 1 `generation` per turn (input only; #68's output check will bring it back to 2). `test_model_matrix_run` and `test_p_08` were updated to match. **The live matrix was NOT re-run**: `docs/model-matrix.md` gained an intro note that the guarded-reply rows are pending a live re-run through `respond`, and the committed rows and JSON are unchanged.
  **Why:** #54's and #55's hand-offs. The brief forbids starting local model servers, so the owner runs the re-run.
  **Issue:** #57
- **Decision:** Tests:
  - `test_i_07` (`FR-RESP-1,2,4`), `test_i_18` (`FR-LITE-1,4,5; FR-PROF-11`) and `test_s_input_ingest__respond` (`FR-RESP-9`, in the new `tests/integration/test_s_input_ingest.py` that #58 extends).
  - `test_i_22__prompt` (`FR-RATE-4`), in `test_i_22__ratings.py` because of #50's same-file constraint.
  - `test_s_trust_boundary__respond` (`FR-AUTH-1,4`, #44's respond half), in `tests/test_trust_boundary.py` and integration-marked.
  - Unmarked: `tests/integration/test_respond.py` (10 tests: refusals and their order, render defaults, the viewpoint, the gate first, the empty reply, the window size, a pins edit during generation that regenerates, the library surface, OpenAPI "finished" and never "verified", and the Lite-defaults write) and `tests/test_lite_prompt.py` (6 pure tests).

  `test_e_06` is unchanged; it is `live_model` and was not run.

  A mutation check bit: leaking `scene_context.recent_messages` into `say` made `test_i_07` fail.
  **Why:** The test plan. AC 6's "docs test greps `docs/openapi.json`" runs against the app's live OpenAPI schema instead, because `docs/openapi.json` does not exist yet (#73's export tool). FR-RESP-3's only §15 test is E-06 (live), so the stub-side param mapping is asserted under I-18's FR-LITE-4 slice.
  **Issue:** #57
- **Decision:** I filed #277 (`needs-triage`): re-rendering the model matrix from its JSON reorders the E-06 lengths, because the JSON keys are string-sorted. I did not fix it.
  **Why:** It is outside #57's scope. It is why the matrix note was inserted by hand rather than re-rendered.
  **Issue:** #57
- **Decision:** Ingest is split like respond. `core/ingest.py` holds the request models (`IngestRequest`/`IngestEvent`), the checks, the payload hash, the turn-slot wait and the recording. `Engine.ingest` passes the input gate in as a callable, because Core never imports the Orchestrator. The route is `POST /stories/{id}/ingest` in `api/routes/stories.py`, per the pass-2 Discretion. It returns 201 when every event is new and 200 otherwise, with `Idempotent-Replay: true` only when every event replayed. The migration is `0012_client_events` rather than the planned `0011`, which #57's `0011_respond` already took. #63's planned `0013` is unaffected.
  **Why:** The artifact list (pass 2) and the import-linter contract. Kept the planned shape otherwise.
  **Issue:** #58
- **Decision:** `client_events(story_id, client_event_id, payload_hash char(64), operation_id, result jsonb, created_at)` uses the PK `(story_id, client_event_id)` as its uniqueness rule. `story_id` and `operation_id` are plain columns with no foreign keys, as `characters` has. `result` is `{operation_id, turn, timeline_revision}`, where `timeline_revision` is the revision the whole batch left. It holds no story content. The table joins `STORY_TABLES` first, so story deletion empties it. `operations.origin` (text, CHECK `generated|ingested`, default `generated`) is written once at `begin()` (`Operation(origin=)`), so the immutability trigger covers it unchanged. History items gain `origin`.
  **Why:** Discretion (dedupe table, result lives forever, `origin` surfaced on history). A plain PK is the planned "unique index". The replay's revision is "the original's", per the Discretion.
  **Issue:** #58
- **Decision:** `OperationBatch` lives in `core/events/batch.py`, not in a `core/events.py` module (one does not exist; `events` is a package). It begins N operations, then commits them in one transaction under one story lock. It runs `before` steps under the lock, then each operation's new `Operation.write_in(conn, decide)`, then `after` steps. Each operation then records its outcome with `settle()`. A new `Withdrawn` signal (in `operation.py`) ends the operations `abandoned` rather than `failed`.
  **Why:** Pass-2 Discretion ("a batch wrapper runs N operations on one connection/transaction"). Reusing `_write` per operation keeps every position, `seq`, projection and chain rule identical to a single commit.
  **Issue:** #58
- **Decision:** The checks run in this order: kinds (422 `unsupported_event_kind`, with `details.milestone = M6` for `outcome`/`state_change`; any other unknown kind is refused the same way), then the story (404), then `player_turn` actors against the cast (422 `validation_error` `unknown_actor {events[i]: id}`), then replay or conflict, then archived (409), then the expected revision (409), then the input gate, then the turn slot, then the batch commit. A replay therefore answers even on an archived story or with a stale expected revision. The gate runs once per actor group (narration is the `None` group), with each event's text on its own at locator `events[i]`. Groups run in sequence, so the first violating group raises. A retry after a concurrent duplicate does not re-gate events already gated.
  **Why:** #44/#56's order. Discretion: "replay returns the original before expected-revision checks", "texts gated separately", and "turn gate last". The gate takes one actor per call, because character-scoped policies apply only to their actor, so a mixed batch needs one call per actor.
  **Issue:** #58
- **Decision:** Validation: `client_event_id` follows #61's rule (≤ 255 chars of `[A-Za-z0-9._:-]`). `say`/`do`/`text` are each ≤ 8,000 code points and are NFC-normalised and trimmed before validation and hashing, and whitespace alone counts as absent. `player_turn` needs an actor and a `say` or `do`, and refuses `text`. `narration` needs `text` and refuses an actor, `say` and `do`. A body has 1..50 events, and a duplicate ID within one body is 422. The hash is `hash_member(event.model_dump(exclude_none=True, exclude={"client_event_id"}))`. The stored texts are the normalised ones.
  **Why:** Discretion (pass 1/2, #60's text rule). Storing what was hashed keeps a replay and its record consistent.
  **Issue:** #58
- **Decision:** An ingested `player_turn` is a `player_input` event (provenance `consumer`, the actor as `character_id`, beat 1). Its transcript role is now `player` for any `player_input`: `role_of` checks the payload type as well as provenance `player`. A `narration` is a `message_rendered` event (provenance `consumer`, no character, beat 1, `render_character_ids []`, `model`/`prompt_hash` null, `finish_reason consumer`) on the `narration` channel. In the Lite window it is an `assistant` row rendered verbatim, and #57's code already labels it `Narrator` in `TranscriptLine.name`. Ingest operations have no viewpoint. The request's `Idempotency-Key` is stored on the batch's first operation only.
  **Why:** Discretion lines. Without the `role_of` change, a consumer-provenance `player_input` would have been labelled `character`, which is a Rule 1 fix in the projection this story feeds. The projection is deterministic, so rebuilds match (asserted).
  **Issue:** #58
- **Decision:** Concurrency ahead of #63. There is no lease, no `turn_queue` row and no `Prefer: respond-async`. Instead, before its operations begin, ingest checks the story's in-flight `turn` operations. Under `reject`, an in-flight **generated** turn is 423 `story_busy` `{reason: in_flight, operation_id}` at once, with nothing written; the commit repeats this check under the story lock, excluding the batch's own operations. Under `queue`, the ingest polls (50 ms doubling to 500 ms) until the turn finishes, then takes the next index. Another in-flight **ingest** is always waited for, never refused, so a concurrent duplicate waits and becomes a replay. Inside the lock the commit re-checks `client_events`; if a duplicate committed first, the batch is withdrawn (operations `abandoned`) and rebuilt, up to three times. Waits are bounded by `DEFAULT_TURN_DEADLINE_S` (45 s) and then give 423. The commit decision honours only `timeline_changed`: an abandon after a control action becomes 409 `timeline_changed`, and a moved revision or a touched read set commits.
  **Why:** AC 1 and the test plan need "busy under `reject`" and "queues under `queue`", but #63's lease, queue table and drainer do not exist, and building them here would be #63's Rule 4 work. Polling `operations.status` is the smallest behaviour-correct stand-in. Hand-off: #63 replaces `await_turn_slot` with `turn_gate`, sets the in-flight lease for the batch (the first operation's id), makes a `queue` batch one `turn_queue` row, and adds `Prefer: respond-async` and the 202. As with #50, until then an operation a crash left `in_flight` blocks ingest until the 45 s wait ends in 423.
  **Issue:** #58
- **Decision:** These Discretion items are left to the stories that build the surfaces they need. `GET /operations/{id}` showing `origin` and `batch_operation_ids` goes to #61, which creates the route. History's `message: null` for reply-less turns goes to #59: the interim history is a flat beat list with no per-turn `message`, and ingested turns appear there as beats with `origin: ingested`. Copying `client_events` on fork goes to the fork story; no fork exists yet. Replaying through #61's idempotency store also goes to #61, as does sharing the `Idempotent-Replay` header constant.
  **Why:** None of those surfaces exists yet, and building them here would be another story's scope.
  **Issue:** #58
- **Decision:** #55's test-only `POST /_test/stories/{id}/ingest-check` is retired. R-07's ingest slice now goes through the real `POST /stories/{id}/ingest` inside `test_r_07` itself, with no separate `test_r_07__ingest` function. It also asserts the violation's locator `events[1]` and an unchanged story, and the adult complement now expects 201. `test_s_delete`'s `_populate` now ingests a narration, so `client_events` has rows for the deleted story (Rule 3: the test requires every story-scoped table to be populated).
  **Why:** #55's hand-off. A second R-07 function would duplicate R-07's part, and the slice belongs to the one test. C-20 still uses #56's `gated-input` fixture for its ingest entry point because it covers pins and instructions in the same loop.
  **Issue:** #58
- **Decision:** Tests. `test_s_input_ingest__ingest` carries `part("M2","FR-RESP-10")`. It checks E twice (one record, the same result, 200 + `Idempotent-Replay`), E with a changed payload (409, still one record), the next index with revision +1, and 423 at once (< 2 s) under `reject` while a respond is held. The complement is `queue`: the ingest waits and then takes the turn after the held one. Unmarked: `tests/integration/test_ingest.py` (10 tests: body validation and M6 kinds, check order, the all-or-nothing batch with payload shapes, mixed replay with NFC/trim equivalence and the key on the first operation, concurrent duplicates ×5, history and prompt window plus rebuild equality, `timeline_changed` with a non-control complement, the library surface, the pure decision, and batch guards). Three mutation checks bit: no under-lock duplicate re-check (IntegrityError in the concurrent test), `queue` forced (S test failed on the busy timing), and replay lookup disabled (S test failed).
  **Why:** The test plan. FR-RESP-9's part stays on `test_s_input_ingest__respond`, and S-INPUT-INGEST's §15 row covers FR-RESP-9 and 10 in M2.
  **Issue:** #58
- **Decision:** `core/viewpoint.py` is final (#57's provisional resolver is kept as is). The other pieces it now holds: `with_requested(ctx, *named)` folds the library keyword (`output_viewpoint=` on `Engine.respond`; `viewpoint=` on `Engine.history`/`get_story`), the body's `output_viewpoint` and `?viewpoint=` into `RequestContext.viewpoint`, the one carrier, and two different named values are 422 `validation_error` `conflicting_viewpoints`; `visible_to`/`PRIVATE_CHANNELS` (moved here from the Lite pipeline); `require_omniscient` (403 `omniscient_required`, new error); `validate_default_viewpoint` for settings writes; and the constant `AUDIENCE_FILTER = "none"`. `GET /stories/{id}?viewpoint=` is now validated: an unknown or malformed viewpoint is 422 `viewpoint_unresolved` (before, any non-`omniscient` value counted as a character read). With no viewpoint named, the read stays character-safe and is never refused.
  **Why:** Discretion: "`RequestContext.viewpoint` is the single carrier; the kwarg fills it". A silent precedence between two named viewpoints would hide a caller bug, so a clash fails closed. #50 left viewpoint validation on the story read to #59.
  **Issue:** #59
- **Decision (interim, ahead of #61):** Reusing a key for another viewpoint is checked by `check_key_viewpoint`. It looks up the story's operation with the request's `Idempotency-Key` (`operations.idempotency_key`, written by #57) and compares that operation's stored viewpoint with the request's *resolved* viewpoint. A difference is 409 `idempotency_conflict` (new error, `details.reason = viewpoint`), with nothing written and no result returned. So is a request that does not resolve at all, and so is a key first used by an ingest, which has no viewpoint. The check sits in #44's idempotency slot: after the archived check and before the expected revision and viewpoint refusal. A same-viewpoint reuse still runs a new turn.
  **Why:** #61 (the store, the replay and the request hash) is blocked by #59, but AC 3 and C-12 need the conflict now. Because the comparison is on the resolved viewpoint, an omitted viewpoint and an explicit `character:<actor>` count as the same request (pass-2 Discretion). Hand-off to #61: fold the resolved viewpoint into the request hash, replace `check_key_viewpoint`, and return the replay for a same-viewpoint retry.
  **Issue:** #59
- **Decision:** A story's default `omniscient` is refused by `StorySettings` itself: a field validator gives 422 `validation_error` at `body.settings.default_output_viewpoint`. Creation used to answer `viewpoint_unresolved` (#50). `test_i_10`'s case now expects `validation_error`, and `stories._stored_settings`' dead branch for it is removed. A non-member default stays 422 `viewpoint_unresolved`. `PATCH /stories/{id}/settings` does not exist yet, because #63 creates it after #59. AC 1's PATCH clause is therefore delivered as the model rule every write inherits, plus `validate_default_viewpoint` for #63 to call. The REST assertion is handed to #63 (commented there).
  **Why:** Discretion: "default `omniscient` → 422 `validation_error`; non-member default rejected at write". Putting the rule on the settings model means no write path can store the value. Building #63's PATCH route here would take over another story's work.
  **Issue:** #59
- **Decision:** The history envelope is `{items, audit?, turns, output_viewpoint, audience_filter, as_of, timeline_revision, next_before_turn}`. `audit` is absent unless requested. `turns` is an addition: one `{turn, variant, message}` per turn on the page, where `message` (`{message_id, text}` of the turn's `message_rendered`) is `null` for a reply-less turn such as an ingested `player_turn`.
  **Why:** #58's hand-off ("history returns `message: null` for reply-less turns") assumes a per-turn `message`, but #59's pass-2 shape is a flat beat list. A per-turn summary meets the hand-off literally without repeating the reply on every item. It is additive, so this is not Rule 4.
  **Issue:** #59
- **Decision:** FR-CAT-12 on history: an omniscient item carries `true_ages`, `{character_id: age}` for the characters the item names (the actor and the render characters). On a character viewpoint the key is absent, not null. REST uses a separate `OmniscientHistoryItemModel`, as #50 did for characters. Respond's `beats` share the builder (`core.history.turn_beats`), so an omniscient respond's beats carry the ages too.
  **Why:** AC 5 says "omniscient history items" but names no field. Naming the ages per character keeps an item self-describing, and #50's precedent keeps the key absent for character reads.
  **Issue:** #59
- **Decision:** History items split a `player_input` with both `say` and `do` into two items (channels `say`, `do`) that share its `message_id`. The parts are read by joining the transcript row to its event payload by `message_id`, which is what #57's prompt window already does; there is no new projection column or migration. A `player_input` without parts keeps its row channel. `beats` on respond changed the same way, and `test_s_input_ingest__respond` and `test_ingest` were updated to match.
  **Why:** Pass-2 Discretion ("a `say`+`do` row yields two items sharing `message_id`"). A join keyed by projection rows still reads the projection's membership and order, so a rebuild stays observable (NFR-REL-2). A column would mean a migration for data the log already holds.
  **Issue:** #59
- **Decision:** Paging and windows:
  - Position-less rows count as turn 0, and `as_of=0` returns only those (in Lite, none).
  - `as_of` defaults to the head (`next_turn_index - 1`); greater than the head, or negative, is 422.
  - `limit` (1..1,000, default 200) counts items after the say/do split and the viewpoint filter.
  - A page holds whole turns, newest first, and returns one turn whole even when it exceeds `limit`.
  - `before_turn` must be ≥ 1. `next_before_turn` is the page's lowest turn while older items exist; on the last page it is null, and that page reaches down to turn 0. Pages therefore tile `[0, as_of]`, and `include_audit` returns the audit events of the page's turn range only (active, committed operations).
  - Audit items use the channel `audit`, and their `text` is the payload's canonical JSON ("full content").

  Refusal order on history: 404 → window and actor 422s (a non-member `actor_character_id` is 422 `validation_error` `unknown_actor`) → viewpoint 422 → 403 `omniscient_required`.
  **Why:** Pass 1/2 Discretion lines (limit/max, `before_turn`, `as_of` rules, 422 before 403, `audit` as history items). Whole-turn pages keep a say/do pair and its reply on one page. Tiling keeps audit records from being repeated or skipped across pages.
  **Issue:** #59
- **Decision:** `Think` and `Latent` payloads gain an optional `character_id` (`{character_id, text}`). The filter is exercised by a `think` beat and a `guardrail_output_rejected` audit record that C-12 inserts at turn 1 through a `maintenance` operation.
  **Why:** Discretion (payload shape; "a fixture event of type `think` inserted directly"). Optional, so #46's I-02 strategy, which appends `{text}`, is unchanged.
  **Issue:** #59
- **Decision:** Tests:
  - `test_c_12` (`part("M2","FR-RESP-12; FR-AUTH-2")`, integration-marked) runs every check through the dual-surface fixture `surface` (`tests/conformance/conftest.py`, parametrised `rest`/`library`). Library errors are normalised to `EpistrelError.code`, or `validation_error` for a pydantic refusal, and library results are rendered through the REST response models. Its complement is the omniscient read with audit records, the private beat and the true ages.
  - Unmarked: `tests/conformance/test_viewpoint_parity.py` (REST equals the library on respond, history and story reads, refusals included) and `tests/integration/test_history.py` (5: window and paging, `message: null`, the refusal order, viewpoint agreement, the pure helpers).
  - C-12's body avoids "think" and `audience_filter` (M4/M5 lexicon terms; `check-lexicon` flagged both on the first run) by using helpers and a `PRIVATE_TEXT` constant.
  - Three mutation checks made C-12 fail on both surfaces, as intended: the private-channel filter disabled, the key/viewpoint check disabled, and `require_omniscient` disabled.
  **Why:** The test plan and #44's lexicon constraint. NFR-SEC-5 gets no marker: its M2 §15 row is S-TRUST-BOUNDARY, as #44 logged.
  **Issue:** #59
- **Decision:** docs/02 §5 gains the viewpoint guarantee table: perception level → `audience_filter`, what a character viewpoint withholds, and the line "no audience filtering applies" for `none`. The history route's OpenAPI description states the same rule and that Epistrel does not decide which caller may request which viewpoint.
  **Why:** Discretion ("guarantee-table line added to docs/02 §5"), and FR-AUTH-2's wording of the guarantee.
  **Issue:** #59
- **Decision:** Story-level text is `core/story_texts.py` (rules, reads, writes) over a new projection `story_texts(story_id PK, pinned jsonb, custom_instructions text)` in `core/projections/story_texts.py`, registered after `story_heads`; the row is created by the genesis commit, and migration `0013_story_texts` creates the table and inserts an empty row for every existing story. The routes are `api/routes/story_texts.py` over four `Engine` methods (`pinned_memories`, `set_pinned_memories`, `custom_instructions`, `set_custom_instructions`). The table joins `STORY_TABLES`. #63's planned `0013` becomes `0014`.
  **Why:** The artifact list and Discretion ("projection rebuilt from the control events", "one row", "created at genesis"). Fork (#67) creates its row through its own genesis or fold; nothing exists to copy yet. Pre-existing pins/instructions events used the shell payload and only test helpers wrote them, so the migration does not fold them.
  **Issue:** #60
- **Decision:** The event shells are replaced: `pinned_memories_set {items: [{id, text}], previous: [...]}` and `custom_instructions_set {text, previous}` (`previous` defaults to empty). The test helpers `set_pins`/`set_instructions` (`tests/concurrency_support.py`) write the new shape raw, with minted ids and no `previous`, so tests that pin past any cap (U-13's 10,000 notes) still can.
  **Why:** Discretion (pass 2: full ordered snapshot plus `previous`). The helpers stay raw because the real path enforces caps and the gate, which those tests deliberately bypass.
  **Issue:** #60
- **Decision:** The read-set members `pins` and `instructions` (#47) and the Lite prompt inputs (#57's `read_turn_inputs`) now read the `story_texts` projection; `pins` hashes the ordered texts only, `instructions` the text. The event-scanning `_latest` helper is gone.
  **Why:** Discretion ("hash texts only"; prompt sections 2 and 4 come from the projection). Reordering changes the hash, so a reorder regenerates an in-flight turn, as the pass-2 line says.
  **Issue:** #60
- **Decision:** An edit is a `control` operation with `bumps_revision=True` but **not** a control action. It runs through #47's `run_with_revalidation` with its own one-member read set (the stored row, ids included) and a decide that re-resolves (up to 3 times) only when a concurrent edit changed the stored texts; a turn or timeline edit in between commits. `previous` and id resolution therefore always describe the value actually replaced.
  **Why:** A control action would make an in-flight turn abandon (409 `timeline_changed`) instead of regenerate, contradicting AC 3. Reusing the revalidation loop avoids a second locking scheme.
  **Issue:** #60
- **Decision:** Order of checks: body (pydantic 422, locator `body.items[n].text`) → story 404 → caps, unknown/duplicate ids, duplicate texts 422 → archived 409 → expected revision 409 → no-op (returns the stored value, `operation_id: null`, nothing written, no gate) → input gate over every item (`items[n].text`) or the instructions (`text`; `""` and `[]` are not gated) → operation.
  **Why:** #44/#56's order with caps treated as body validation (they need the deployment read, as #57's cast check needs the cast). A no-op skips the gate because nothing is recorded, so there is nothing to protect and no model calls should be spent.
  **Issue:** #60
- **Decision:** Id rule: a named id must exist on the story (else 422 at `body.items[n].id`); an id-less item takes the id of a stored item with the same normalised text that no other item names, else a new ULID. So re-sending the same texts without ids is a no-op, and a reorder keeps ids.
  **Why:** Discretion ("server-minted ULIDs; unknown client id → 422"; "no-op edit writes nothing"). Without text matching, every id-less PUT would mint new ids and could never be a no-op. Low cost to change.
  **Issue:** #60
- **Decision:** Caps `max_pinned_memories` (50, 1 to 500) and `max_custom_instructions_chars` (4,000, 1 to 20,000) are `StoryTextCaps` in `deployment_state.settings`, read by `config_changes.read_story_text_caps` and written by the library function `deployment.update_story_text_caps` (one revision, prospective `config_changes` rows), the same shape as #57's Lite defaults. `/admin/settings` is not extended. A cap breach is 422 `validation_error` `details {field, cap, limit, actual}`; an item is ≤ 2,000 code points after NFC and trimming.
  **Why:** Discretion (caps are prospective deployment settings, bounds, `details.{cap, limit, actual}`). #64 owns the settings surface, as #57 logged.
  **Issue:** #60
- **Decision:** Tests. `test_i_18__pins` (`part("M2","FR-LITE-3")`) is in `tests/integration/test_i_18.py` (one file per test id's M2 slices): edit → revision +1 with the event; an edit during a held generation → 2 generation calls, the final prompt has the new pin and not the old; a gate violation → 422 at `items[1].text` with the value, revision and events unchanged; a repeat → nothing written. Unmarked: `tests/integration/test_story_texts.py` (6: caps incl. 51 pins → 422 unchanged, caps as settings, id/text/duplicate rules, refusal order, `previous` + reorder + clear + prompt sections, rebuild equality + library + idempotency key). I-02's strategy gains `pins`/`instructions` edits, a corrupted `story_texts` row, and the occurrence check covers them. Two mutation checks failed `test_i_18__pins` as intended: an edit that does not bump the revision, and a `pins` read-set member that ignores the pins.
  **Why:** The test plan. FR-CONC-1's M2 test is CONC-LINEAR (#47), so `test_i_18__pins` claims FR-LITE-3 only.
  **Issue:** #60
- **Decision:** `test_i_02` gains a Hypothesis `@example` that always plays turns, pins, instructions and a regenerate (Rule 1, test code). The first full-suite run failed the occurrence check with no `regenerate` or `instructions` drawn in 50 examples.
  **Why:** Random draws favour the first entries of `sampled_from`, so adding two actions at the end made the existing occurrence assertion flaky. An explicit example strengthens the run rather than weakening the check.
  **Issue:** #60
- **Decision:** The store is migration `0014_idempotency` (not the planned `0012`: #58 and #60 took 0012 and 0013; #63's planned slot moves to 0015). `idempotency_keys(scope, scope_id, key, request_hash, operation_id?, status in_flight|done, result jsonb?, revision?, owner_token, lease_until, created_at, expires_at?)`, PK `(scope, scope_id, key)` as the unique rule, CHECKs on scope and status, indexes on `operation_id` and `expires_at`, no foreign keys. Beyond the AC's columns: `revision` (the committed timeline/config revision, kept with the tombstone so the reference can carry it after the purge), `owner_token`/`lease_until` (pass-2 Discretion). The same migration adds `operations.timeline_revision` (set at commit) and puts it on the operations trigger's allow-list (`MUTABLE_OPERATION_COLUMNS`), so `GET /operations/{id}` can report the revision.
  **Why:** AC 1 and the Discretion. Columns on an existing table, not a new table, so not Rule 4; the new table is the story's planned one.
  **Issue:** #61
- **Decision:** One mechanism for every mutating Engine method: `Engine._keyed(ctx, operation, run, story_id=, material=)` runs the call as a `core.idempotency.IdempotentCall` held in a context variable. The key is the caller's (validated: 1..255 of `[A-Za-z0-9._:-]`, else 422 at `headers.Idempotency-Key`) or a fresh ULID; REST's `get_request_context` mints it for every mutating request and echoes it in the `Idempotency-Key` response header. Scope `story` (+ story id) for rebuild, respond, ingest, pins, instructions, archive, unarchive and story policies; scope `deployment` (`scope_id = "deployment"`) for create, delete and every admin write. `Engine.noop` (a test stub) is not keyed. Per-method `ResultCodec`s (`engine.CODECS`) hold the result type (a pydantic `TypeAdapter` over the existing result dataclasses), the REST status the result answers with (201 create, 204 delete/api-key/no-op retire, ingest 201|200) and the revision field a reference carries.
  **Why:** FR-CONC-6 covers every mutating request and #44 makes the Engine the one place both surfaces pass through. A context variable lets `Operation` and the configuration writer bind the key inside their own commit transactions without threading a parameter through ~20 core and orchestrator functions.
  **Issue:** #61
- **Decision:** Binding. `Operation._write` ends by calling `call.bind(conn, op)`: inside the commit transaction it sets `operation_id = coalesce(operation_id, op)` only where `owner_token` is still the call's, else raises 409 `idempotency_conflict` `{reason: superseded}` and the commit rolls back (a late original whose lease a retry took over never commits). Configuration writes (ratings, settings, policies, model config), which run no `Operation`, bind in `record_config_change`: the call's first change inserts a committed `control` operations row (story-less for deployment configuration; the story for story policies) and binds it, in the write's own transaction. A write that changes nothing binds nothing, so its key keeps `operation_id` null (the reference then has `reference: null`).
  **Why:** Discretion pass 1/2 ("admin changes get an operations row of kind control with story_id null", owner token, late commit refused). Binding in the commit transaction means `operation_id` set ⇔ the call's write committed, which is what a takeover decision needs.
  **Issue:** #61
- **Decision (deviation):** `done` + result are written in the commit transaction only for the Lite turn: `respond` sets `call.result_in_commit`, so the turn's `RespondResult` (beats read on the commit connection, `elapsed_ms` fixed at the commit) is built and stored with the turn. Every other call stores its result in a short transaction right after its commit. If a process dies between the two, the key holds a bound operation with no result: a retry (once the lease lapses) marks it `done` with no result and answers with the reference, never re-executing.
  **Why:** The key_link says "`done` + result in the commit transaction". Building every result type inside its commit would mean restructuring each core write; the turn is where a lost result matters (S-DOUBLE-SUBMIT's "same message on every retry"), and the crash window for the rest only downgrades a replay to a reference, never to a second commit.
  **Issue:** #61
- **Decision:** Claim order. Respond claims at #59's slot (after the cast and archived checks, before the expected revision), with the hash over the body minus `output_viewpoint`, the actor, the **resolved** viewpoint (`None` when it does not resolve) and the expected revision; `check_key_viewpoint` is removed. Every other call claims at the start of its Engine method, before the expected-revision check and before the core function's own 404/archived checks, so an archived story still replays a stored result (as #58's ingest replay already did). A refused precheck after the claim deletes the row (failures are never stored).
  **Why:** #44's order ("idempotency lookup … before any revision check") and #59's hand-off. Claiming generic calls before their core-level archived check is a small deviation from "after the cheap prechecks": those checks live inside the core functions, and the only effect is that a retry of a stored result answers on an archived story.
  **Issue:** #61
- **Decision:** Waiting and leases. A duplicate polls the row from 50 ms backing off to 250 ms, up to `DEFAULT_TURN_DEADLINE_S + 5` (50 s), then 503 `original_in_flight_timeout` (new `OriginalInFlightTimeout`) with `details.operation_id` = the original's in-flight operation found by its stored key (null before it began one). The lease is 60 s for every call (`idempotency.LEASE_S`; #63's turn lease does not exist yet), renewed every 20 s by a task the call owns. An expired lease with nothing bound is taken over (compare-and-set on `owner_token`) and re-executed; an expired lease with a bound operation is settled `done` without a result and answered with the reference.
  **Why:** Discretion (250 ms poll, turn deadline + 5 s, lease renewal, dead original re-executes) and the AC's "poll with backoff". The 50 ms start keeps a fast original's duplicate from waiting a full poll interval.
  **Issue:** #61
- **Decision:** REST surface. Replays answer with the **original status** (201 for a create, 204 for a delete) — the AC says 200, Discretion pass 1 says "replay returns the original status", and the later, more specific line wins — the re-rendered identical body and `Idempotent-Replay: true`. The stored result is `{status, body}` where `body` is the **library** result's JSON dump; REST re-renders it through the same route code, so replay bytes equal the original's (the echoed key is the same, `Location` is derived from the stored story id, and the stored `elapsed_ms` is replayed, so nothing is excluded from byte-identity). Model-config mutation routes read the purpose's entry back after the write, so their replay shows the current entry with the stored write's revision. After the window the reference is 200 `{operation_id, reference: "/operations/{id}", timeline_revision|config_revision, idempotency_key}` with `Idempotent-Replay: true`, rendered by an exception handler for `ReferenceReplay`. `MutationResponse`/`DeploymentMutationResponse` gain `idempotency_key` (excluded when null, so reads that share the model are unchanged); 204 routes echo the key in the header only.
  **Why:** AC 2/5 and Discretion. Storing the library result keeps one store for both surfaces (FR-AUTH-1); the route layer is deterministic, so re-rendering is byte-identical (asserted in I-14 and S-DOUBLE-SUBMIT).
  **Issue:** #61
- **Decision:** Library surface. Every mutating result dataclass gains `replayed: bool = False` (pass-2 Discretion); `IngestResult` gets `key_replayed` instead, and its existing `replayed` property becomes "key replay, or every event replayed". `Engine.delete_story` now returns `stories.DeletedStory(removed, replayed)` instead of a bare dict (nothing used the dict). After the window the library raises `idempotency.ReferenceReplay` (not an `EpistrelError`) carrying `operation_id`, `reference` and the revision, because the methods' result types cannot hold a reference.
  **Why:** A union return type on ~20 methods would ripple through every strictly typed caller; an answer that is not an error is a separate exception class with its own REST handler.
  **Issue:** #61
- **Decision:** Deletion and secrets. Deleting a story removes its story-scoped keys by `scope_id` (#50's hand-off) **and** the deployment-scoped key bound to one of its operations (its creation), whose stored result names the cast; docs/02's idempotency paragraph now says so and lists the new columns. `set_model_api_key`'s hash covers the key's SHA-256, never the key. Respond's hash covers `scene_context` only as part of the SHA-256; scene context is still never stored.
  **Why:** FR-STORE-16 (no row of a deleted story's content survives) and NFR-SEC-3; C-09 and S-DELETE pick the table up (S-DELETE now also asserts the deletion's own key row: deployment-scoped, `done`, status 204).
  **Issue:** #61
- **Decision:** `GET /operations/{id}` (`api/routes/operations.py`, `Engine.get_operation`, `core/operation_view.py`) returns `{id, story_id, kind, status, narrative_turn, turn_variant, timeline_revision, principal, started_at, committed_at, viewpoint, origin, batch_operation_ids, attempts, config_revisions, idempotency_key, result?}`; `result` is `{status, body}` of the operation's key while the window is open. A viewpoint-bound result is returned only when `?viewpoint=` names that viewpoint; another named viewpoint is 409 `idempotency_conflict` `{reason: viewpoint}`, and a read naming none gets the view without `result` (so status polling never fails). `batch_operation_ids` (ingested operations only) are the story's ingested operations with the same `committed_at`, which a batch's single transaction shares. Unknown is 404 `not_found` (new `OperationNotFound`).
  **Why:** AC 4, pass-2 shape, #58's (`origin`, batch ids) and #47's (`attempts`) hand-offs. "Else 409" is applied to a named mismatch only; refusing an unnamed read would make the reference itself unreadable for turns.
  **Issue:** #61
- **Decision:** `idempotency_result_window` (hours, 1..720, default 24) is `IdempotencySettings` in `deployment_state.settings`, read when a call claims (prospective) by `idempotency.read_idempotency_settings`, written by the library function `deployment.update_idempotency_settings` (one revision, one prospective `config_changes` row). `/admin/settings` is not extended (#64 owns that surface). `purge_expired(db, now=None)` nulls every result past `expires_at` and keeps the tombstone (#71 calls it). `core/clock.py` holds the injectable `now()` (`frozen_at`, `set_clock`) every window rule reads.
  **Why:** Discretion (setting in hours, prospective; `purge_expired(now)`; injectable clock). Same shape as #57/#60's deployment settings.
  **Issue:** #61
- **Decision:** `epistrel.testing.faults` (new) runs the engine in a child process (`python -m epistrel.testing.faults serve`, stub adapter, `LEASE_S` set by `--lease-s`) that a test can `SIGKILL` and restart on the same port; `--pause-before-commit` makes every `turn` operation print `EPISTREL-FAULT before-commit <id>` and hang instead of committing. Nothing in `create_app` or the Engine imports it.
  **Why:** Pass-2 Discretion ("#61 lands `epistrel.testing.faults` early; #75 extends"; "subprocess kill for (c) and X-04"). A marker printed at the exact point makes (c) "between generation and commit" deterministic.
  **Issue:** #61
- **Decision:** Tests.
  - `tests/integration/test_i_14.py::test_i_14` (`part("M2","FR-CONC-6; NFR-REL-4")`): identical bytes and the replay header in the window; after `expires_at` (clock frozen past it) the reference, before and after `purge_expired`, with the tombstone kept, no stub call and no operation; 409 throughout; the operations read with and without the result.
  - `tests/scenario/test_s_double_submit.py::test_s_double_submit` (same part; new `tests/scenario/` directory per the artifact list, integration-marked): (a) after completion, (b) a duplicate held while the original generates, (c) a child engine killed at `before-commit` and the retry taking over after the 1 s lease — one committed turn and the same bytes each time; the crashed attempt stays `in_flight` for #70's recovery; a different payload is 409 in every case.
  - `tests/chaos/test_x_04.py::test_x_04` (`part("M2","NFR-REL-4; FR-CONC-6")`, `chaos`): 20 keys × `EPISTREL_CHAOS_ITERATIONS` (50) requests against a child engine with 0.4 s stub latency, 30 % client-side cancellations, three `SIGKILL` restarts (the first within 0.15–0.4 s, so it lands on turns in flight), seed printed; every key ends with one committed turn bound to its key and one message id across all answers. Last local runs: 24–46 originals killed in flight, ~14 s. The queued-turn clause waits for #63's queue.
  - `tests/integration/test_conc_linear.py::test_conc_linear__idempotency` (`part("M2","FR-CONC-6")`): 12 seeded rounds of concurrent duplicates, retries, a changed payload under a used key and pins edits; each key ≤ 1 committed operation (bound), one message per key, single chain, revision = turns + pins.
  - Unmarked: `tests/integration/test_idempotency.py` (9: server keys, the key rule, failures not stored, the waiter timeout, dead originals — takeover, superseded late commit, reference after a bound death, tombstone after a post-commit failure — admin and delete replays with the control operation, the operations read, the window setting, the library) and `tests/test_idempotency.py` (6 pure).
  - Updated for the new behaviour: `test_i_07` (response fields), `test_i_10` (bodies carry the key), `test_ingest` (the first keyless ingest's operation now carries the server key), `test_ratings_api` (settings PUT body), `test_s_delete` (the store).
  **Why:** The test plan. `test_i_14` imports `frozen_at` rather than the `clock` module because `check-lexicon` flags "clock" as an M6 term in an M2 part body (first run).
  **Issue:** #61

- **Decision:** #57's `resolve_cast`/`ResolvedCast` moved to `orchestrator/lite/cast.py` (the artifact; #58 reuses it), and `pipeline.py` no longer exports them. New rules: a default render set that would be empty (cast = the actor alone) renders the actor; `ResolvedCast.noted` gives the author-notes order (the actor, then each render character in request order, without repeats). `prompt.assemble(inputs, cast, ...)` and `model_params(resolved, cast)` take the resolved cast instead of `actor=`/`render=` keywords. The definitions still come from the turn's REPEATABLE READ snapshot.
  **Why:** Discretion (pass 1: "#58 reuses `cast.py`", "definitions in request order", "empty default renders the actor"). Taking the resolved cast in one argument keeps the order rule in one place.
  **Issue:** #62
- **Decision (deviation from #57):** The author notes are now in **request order** (actor first, then renders as listed), not cast order. `test_i_18`'s order assertion was updated (render `[SECOND, FIRST]` now puts SECOND's notes before FIRST's).
  **Why:** #62's pass-1 Discretion overrides #57's earlier choice. The voicing line already used request order, so notes and voicing now agree.
  **Issue:** #62
- **Decision (deviation from #57):** The reply event's `character_id` is always null, even when one character is rendered; who it voices is `render_character_ids`. #57 set `character_id` to the single render character.
  **Why:** Discretion: "a single-render reply is still 'combined': `character_id` null". Nothing else read it: the prompt window names a reply from `render_character_ids`, and history's `actor_character_id` for a reply is now null in every case. A mutation check (the old rule restored) made `test_c_13` fail.
  **Issue:** #62
- **Decision:** Request character ids are typed `core.ids.Ulid` (`^[0-9A-HJKMNP-TV-Z]{26}$`, upper case as `new_ulid` writes them) on `actor_character_id`, `render_character_ids[]` and `scene_context.characters_present[]`. A malformed id is 422 `validation_error` with `details.field` (e.g. `body.actor_character_id`). `RecentMessage.character_id` is left a free string, because scene context's recent messages are non-authoritative, never checked against the cast, and dropped.
  **Why:** Discretion ("ids typed as ULIDs (malformed → `details.field`)"). Lower-case Crockford is refused rather than normalised, because ids are stored upper case and a normalised id would silently differ from the caller's.
  **Issue:** #62
- **Decision:** The respond idempotency hash replaces the body's `render_character_ids` with the resolved, deduped set. An omitted render list and an explicit list that resolves to the same characters in the same order replay; a different order is 409 `idempotency_conflict`.
  **Why:** Discretion ("the idempotency hash uses the resolved, deduped render set"). Order counts because it changes the voicing line and the reply. A mutation check (raw body hashed) made `test_i_07__fields` fail.
  **Issue:** #62
- **Decision:** `lite_generation.j2`'s "Your part" gains the constant line "Each character knows only what their notes and the scene show." after the voicing line, in every turn (with one render character too). docs/02 §5 gains the FR-LITE-5 paragraph under the viewpoint guarantee table: one combined generation, knowledge separated by instruction only, every voiced character's notes in the shared context, `audience_filter` `none`. The respond route's OpenAPI description now states who may act, the default render set, the one composed message, the instruction-only separation and the two optional headers. `docs/models.md` is unchanged (`model_guide --check` passed).
  **Why:** Discretion (the knowledge-separation line; the guarantee-table line added here). FR-AUTH-3 b is structural: `RespondRequest` is `extra="forbid"` and has no field for another character's words.
  **Issue:** #62
- **Decision:** Tests:
  - `tests/conformance/test_c_13.py::test_c_13` (`part("M2","FR-RESP-1; FR-AUTH-3,4")`, integration, both surfaces). It covers the player acting with Jenni and Bobby rendered; Jenni acting; a `say` quoting Bobby stays the actor's speech, and extra fields that would carry another character's words are 422; an unknown render, an actor from another story, both together, and a malformed id are each 422 with zero operations, events and transcript rows and no model call; and two principals get the same message, with each principal recorded on the turn's events.
  - `tests/integration/test_i_07.py::test_i_07__fields` (`part("M2","FR-RESP-5,6")`): one reply beat listing every render id (single render included, `actor_character_id` null), the separation line, `audience_filter` `none`, key + expected revision accepted with the new revision, an identical-bytes replay (also for a differently spelled render set that resolves the same), and a stale revision that is 409 with `current_revision` and no rows, including the key store. The complement is a reordered render set under the same key, which is a conflict.
  - Unmarked: `tests/test_lite_cast.py` (5 pure tests) and a cast-of-one case added to `test_render_defaults_and_duplicates`. `tests/conformance/conftest.py`'s `Outcome` gains `details`; a library pydantic refusal reports REST's `body.`-prefixed field.
  **Why:** The test plan. `audience_filter` is asserted through the helper `_unfiltered`, because `check-lexicon` flags "audience filter" and "perception" (M5 terms) in an M2 part body (first run), as with C-12.
  **Issue:** #62
- **Decision:** Migration `0015_turn_queue` (the planned `0013`, moved: #60 and #61 took 0013 and 0014). `stories` gains `turn_concurrency` (text, CHECK `reject|queue`, default `reject`; the migration moves #50's value out of `settings`), `turn_queue_max` (smallint, 1..50, null = the deployment default) and the in-flight marker as two columns, `in_flight_turn` (operation id) and `in_flight_lease_until`. `operation_status` gains `queued` (added in an autocommit block; the downgrade turns queued rows `failed` and leaves the value, since Postgres cannot drop one and 0002's downgrade drops the type). `operations` gains `error_code` and `error` (jsonb `{status, message, details}`), both on the trigger's allow-list. `turn_queue(story_id, position identity, operation_id unique, kind respond|ingest, request jsonb, principal, idempotency_key, attempts, enqueued_at)`, PK `(story_id, position)`, joins `STORY_TABLES`.
  **Why:** AC 1-2 and the pass-1 Discretion (dedicated columns mirrored in `StorySettings`). Columns on existing tables plus the story's planned table, so not Rule 4. `StorySettings.from_row(settings, **columns)` and `stories.settings_of(row)` merge the columns back, so every read still sees one settings document.
  **Issue:** #63
- **Decision:** The turn slot (`core/turn_gate.py`). A turn takes it last in the precheck order (after the input gate, #56), in its own short transaction under the story row lock, with lease = `turn_deadline` + 10 s on the database clock (`clock_timestamp()`); `Operation.holds_turn_slot` makes the commit and failure transactions release it, and every release sends `NOTIFY epistrel_turns`. The holder renews the lease every third of it while it runs. An expired lease is reclaimed by the next request: an ordinary turn is marked `failed` with the new 503 `turn_interrupted` (its late commit is refused because it is no longer `in_flight`); a queued turn goes back to `queued` with `attempts + 1`, and its third interrupted run fails it. A newcomer never takes a free slot while turns are queued, under either setting, so `queue` → `reject` drains the backlog and a `reject` newcomer is 423 meanwhile.
  **Why:** AC 1, the Discretion (lease length, reclaim by `lease_until < now()`, orphan `failed`, re-run capped at 3, `queue → reject` drains). Renewal is a deviation from "no renewal needed at turn_deadline + 10 s": it costs one UPDATE per lease/3 and keeps a slow but live turn (or a short fault-test lease) from being reclaimed under it. The database clock keeps processes with skewed clocks, and tests that freeze `clock.now()`, out of the lease rule.
  **Issue:** #63
- **Decision:** 423 shape: `story_busy` `{reason: in_flight|queue_full, operation_id, queue_depth, story_id}` with `Retry-After` = the lease's seconds left (≥ 1). Under `reject` respond refuses any holder (an ingest too). Ingest keeps #58's rule: a generated holder is 423, another ingest (or a holder with no operations row yet, i.e. just starting) is waited for up to `turn_deadline`, so a concurrent duplicate still becomes a replay. `StoryBusy` gained `retry_after`.
  **Why:** Discretion (details and header). Respond had no overlap check before; refusing every holder is FR-CONC-5's "refuses a second in-flight turn".
  **Issue:** #63
- **Decision:** #58's hand-off: `await_turn_slot` (polling `operations.status`) is gone; ingest takes the slot with its batch's first operation id, and under `queue` the whole batch is one `turn_queue` row (`kind = ingest`) whose first operation is the queued row. The under-lock "generated turn in flight" re-check was removed (the slot makes it redundant); the under-lock duplicate re-check stays. A queued batch that hits a concurrent duplicate (only possible past an expired lease) fails `story_busy` `{reason: contended}` instead of rebuilding.
  **Why:** #58's hand-off. Rebuilding a batch would abandon the queued operation the 202 already named.
  **Issue:** #63
- **Decision (deviation):** A queued turn's `operations` row is inserted `queued` at enqueue (with its key, resolved viewpoint and origin) and adopted by `Operation(operation_id=, queued=True)` at drain time (`queued` → `in_flight` with the base read then); its commit deletes its `turn_queue` row in the same transaction. The turn index is **not** reserved at enqueue (pass-2 Discretion said it is).
  **Why:** The index is allocated under the story lock at commit by #47's positioning; reserving it at enqueue would need a second allocation path. The drainer plays the queue in order, so queued turns still commit as consecutive indices (asserted). Low cost to add later.
  **Issue:** #63
- **Decision:** Idempotency (#61) with the queue. At enqueue, in the enqueue transaction, the request's key is handed to the queue: `owner_token = queue:<operation id>` (`IdempotentCall.hand_over`); the HTTP call then stops renewing, binding, storing or releasing it. The drainer runs the turn under `IdempotentCall.owned_by(...)`, so the commit binds the key and stores the result (respond: in the commit transaction). A failure deletes the key (failures are never stored); a crash leaves it with the queue. A retry that finds a queue-owned key (no lease applies) waits for that turn exactly like the original (`QueuedOriginal`): the result with `Idempotent-Replay`, or 202 for the same operation.
  **Why:** Discretion ("idempotency in_flight row at enqueue", "a queued original's duplicate key gets 202 too"); a queued turn must survive its HTTP request ending, so the key cannot stay with that process.
  **Issue:** #63
- **Decision:** The drainer is `orchestrator/queue_drainer.py::QueueDrainer`, owned by the Engine: one detached asyncio task per story, kicked at enqueue; `Engine.start_workers()` (the app lifespan) runs the recovery pass in the background, so an unreachable database never stops start-up (`test_health_db` caught the blocking version); `stop_workers()` cancels the tasks and a turn stopped mid-run goes back to `queued` with the slot released. Waiters and drainers are woken in-process at once (asyncio futures keyed by story) and otherwise poll (drainer 0.1 s, waiter 0.25 s); there is no `LISTEN` connection, but every release and enqueue sends `NOTIFY epistrel_turns` for #70.
  **Why:** Discretion (detached per-story task, in-process before #70, client disconnect leaves the turn queued). Deviation from "LISTEN/NOTIFY wake": a `LISTEN` needs a dedicated raw driver connection with its own reconnect handling, which #70 builds for jobs; in `all` mode the wake is immediate and cross-process polling is ≤ 250 ms, inside P-04's 1 s.
  **Issue:** #63
- **Decision:** Outcomes reach waiters through the operation: success via the result stored under the request's key (a waiter gives a committed ingest's drainer 5 s to store it, then answers with the reference); failure via `operations.error_code`/`error` (written by `turn_gate.fail_item` in the transaction that deletes the queue row), re-raised as an `EpistrelError` with the same code, status and details. The long-poll is bounded by the story's `turn_deadline` (no request `deadline_s` exists until #69); then `TurnAccepted` → REST 202 `{operation_id, position}` (`position` = turns ahead) with `Location: /operations/{id}` and the key header. `respond_async: bool` is a keyword of `Engine.respond` and `Engine.ingest`; REST reads `Prefer: respond-async` (`api/deps.prefers_async`). An idle story plays at once even with `respond-async`.
  **Why:** Discretion (pass 2: result via the key, `error_code`, 202 body and `Location`, `respond_async` kwarg). A provider `Retry-After` on a queued turn's 429 is not carried to its waiter (the error record keeps code, status and details only).
  **Issue:** #63
- **Decision:** Story settings: `turn_queue_max` (1..50, null = deployment default) and `turn_deadline` (seconds, > 0, ≤ 600, default 45) join `StorySettings`. `turn_deadline` is the one source for the respond turn budget's deadline, the lease and the long-poll; `budget.DEFAULT_TURN_DEADLINE_S` now comes from `settings_schema`. The deployment default `turn_queue_max` (5) is `TurnQueueDefaults` in `deployment_state.settings`, read by `turn_gate.read_concurrency`; it is **not** added to `DeploymentSettings`/`/admin/settings` (the pass-2 line said `DeploymentSettings.turn_queue_max`).
  **Why:** The lease rule needs a per-story `turn_deadline`, and NFR-PERF-6 makes it a story setting (#69 adds `max_model_calls_per_turn` and request overrides on top). #64 owns the settings surface; #57/#60/#61 kept their deployment settings in the same document, unexposed.
  **Issue:** #63
- **Decision:** `PATCH /stories/{id}/settings` (`core/story_settings.py`, `Engine.update_story_settings`): an RFC 7396 merge patch over `StorySettings`; an unknown key or bad value is 422 `validation_error` at `body.<field>`; `default_output_viewpoint` goes through `validate_default_viewpoint` (`omniscient` 422 `validation_error`, a non-member 422 `viewpoint_unresolved`, stored as `character:<id>`; unlike creation, a name is not accepted). A write that changes something moves the story's `config_revision` once and records one `config_changes` row per changed key (scope `story`, class `prospective` until #64 tags each, the acting principal) through `record_config_change`; a no-op writes nothing; an archived story is 409. An optional `policies` list upserts the story's own policies through `set_story_policies`; every item's relax check runs before anything is written, but the settings and the policies are two writes (each moves `config_revision`). The response is the GET shape plus `config_revision` and `timeline_revision` (the `MutationResponse` base); a replay re-reads the guardrails live.
  **Why:** AC 3 and the Discretion (merge patch, `null` resets, GET shape, `record_config_change`). #49's hand-off: the HTTP `cannot_relax` check on story settings is asserted (unmarked) in `test_settings_patch`. #59's hand-off: both 422s are asserted on the real route.
  **Issue:** #63
- **Decision:** Hand-offs closed: #53 — a 503 releases the slot and the key re-executes (`test_a_failed_turn_frees_the_slot`); #56 — the respond turn budget's deadline is now the story's `turn_deadline` (the non-turn gate budget and `max_model_calls_per_turn` stay with #69, nothing else to act on); #61 — X-04's queued-turn clause (below). S-DOUBLE-SUBMIT (c) now expects the crashed attempt `failed` (the retry reclaims its expired lease), and X-04 counts crashed originals as `in_flight` or `failed`.
  **Why:** The orphan rule is the Discretion's ("orphaned operation `failed`"); #61 had left it `in_flight` for #70 only because no lease existed.
  **Issue:** #63
- **Decision:** Tests:
  - `tests/scenario/test_s_turn_concurrency.py::test_s_turn_concurrency` (`part("M2","FR-CONC-5")`, integration): a story created without the setting refuses the overlapping turn (423, holder, `Retry-After`, no generation call), and plays it once free; a `queue` story holds the second turn `queued` with no generation while the first runs, then plays it as turn 2 whose prompt carries the first reply; `turn_queue_max = 1` → a 202 then 423 `queue_full`; two stories with a 1 s stub latency both finish in < 1.8 s; `reject` → `queue` → `null` (reject) applies to the next request, and the backlog drains.
  - `tests/integration/test_conc_linear.py::test_conc_linear__turn_concurrency` (`part("M2","FR-CONC-5")`, CONC-LINEAR's §15 FR-CONC-5 row, which its docstring promised to #63): 12 seeded bursts of respond / `respond_async` / ingest while the setting flips; every committed turn started from its predecessor's revision with no regeneration, one chain, revision = start + turns; the run refused (10) and queued (14) turns.
  - X-04's queued clause: 4 `queue` stories × 3 keys under the same drops and restarts; each ends with one committed turn per key at turns 1..3, distinct bound operations, one message per key, no queue rows left, and at least one turn queued behind a running one (last runs: 8).
  - Unmarked `tests/integration/test_turn_concurrency.py` (7): the settings PATCH, lease reclaim after a crash, a failed turn frees the slot, a queued ingest batch (202, duplicate 202, replay), long-poll then 202, a queued failure reaching its waiter, and the recovery pass (plain, an interrupted re-run, the `MAX_RUNS` cap).
  - Updated: `test_s_delete` gives the victim a queued turn; `test_ingest::test_timeline_changed_abandons`'s complement uses a pins edit (a turn can no longer commit while the ingest holds the slot); `respond_client` waits for the drainer to go idle before its loop ends.
  - Mutation checks: the slot disabled → S-TURN-CONCURRENCY fails (503 instead of 423) and CONC-LINEAR fails (`revision_conflict`); `queue` forced to refuse → S fails.
  **Why:** The test plan; FR-CONC-5's §15 rows are CONC-LINEAR and S-TURN-CONCURRENCY. The S test avoids M5/M6 lexicon words ("sequential", "clock").
  **Issue:** #63
- **Decision (Rule 3):** `tests/property/test_policy.py`'s text strategy now passes `codec="utf-8"`. It generated a lone surrogate (`'\ud800'`), which Postgres refuses in jsonb; the failure reproduced on the branch without this story's changes.
  **Why:** A flaky generator blocked the full gate; the property itself is unchanged.
  **Issue:** #63
- **Decision:** No migration. #48 already created `config_changes` and the one-row `deployment_state` (with `settings` jsonb), #47 created `stories.config_revision`, and #45 created `operations.config_revisions`. The planned `0014_config_changes` would have added nothing that is not already there (pass-2 line: "#64's migration adds nothing #48 created").
  **Why:** An empty migration would only add a revision for nothing to change.
  **Issue:** #64
- **Decision:** Classification lives in `core/settings_schema.py`. `ConfigClass.interpretive|prospective` is carried as `Annotated[..., ConfigClass.x]` on every leaf (`GenerationSettings`' leaves included). `leaves`, `untagged`, `setting_class` (untagged or unknown → interpretive) and `flatten` walk nested models. `DeploymentSettings` is a pydantic model that inherits #53/#57/#60/#61/#63's section views (`StructuredSettings` moved here from `orchestrator/models/config.py`; it is still imported there). It adds the two `deployment_state` columns. Classes: `default_output_viewpoint`, `default_content_rating_id`, `minor_age_threshold` and `uncertain_below` are interpretive; the rest is prospective. #48's GET/PUT result dataclass, also called `DeploymentSettings`, is renamed `DeploymentState` and gains the full `settings` document. `ChangeClass` stays as an alias of `ConfigClass`.
  **Why:** AC 2 and the Discretion (class tags per leaf, nested walk, the class list, `uncertain_below` interpretive). Inheriting the section views keeps each bound in one place. `default_safe_fallback` belongs to #68 and is not added here.
  **Issue:** #64
- **Decision:** `/admin/settings` covers every `DeploymentSettings` field, flat. GET returns all fields plus `config_revision`. PUT is a JSON merge in which `lite_generation_defaults` merges key by key, a top-level `null` or an unknown key is 422, and bounds are checked on the merged document as 422 `validation_error` at `body.<path>`. A write that changes something takes one revision and writes one row per changed leaf (e.g. `lite_generation_defaults.temperature`), each with its own class. The library writers `update_lite_defaults`/`update_story_text_caps`/`update_idempotency_settings` now go through the same `apply_settings`. Story-settings PATCH rows are also per leaf with their class (#63 wrote one prospective row per top-level key). #57/#63's tests that expected whole-object rows were updated.
  **Why:** Pass-2 "a mixed-class PATCH writes one row per leaf field", "one row per PATCH with JSON old/new; one revision bump", and the deferrals from #53/#57/#60/#61/#63. Replacing `lite_generation_defaults` through the library still means replacement: unset leaves are written as null.
  **Issue:** #64
- **Decision:** `record_config_change` remains the only writer of `config_changes` (an AST test checks this). The first change in each write transaction (keyed by `txid_current()` in `conn.info`) inserts that write's committed `control` operation: story-less for deployment scope, the story's own for story scope. Under an idempotent call it also binds that operation to the key; `IdempotentCall.bind_configuration(conn, operation_id=)` now only binds, where before it inserted its own row. A story-scoped change also appends one `config_changed` event per row, under the story lock the write already holds, with no timeline bump. Deployment-scoped writes emit no event.
  **Why:** Pass-1 "settings writes are `control` operations with a `config_changed` event" and pass-2 "deployment-scoped writes emit no event; `config_changes` + a story-less `control` operation". Before this, a control operation existed only when the write ran under an `Idempotency-Key`. The event payload is written as a raw dict, because the registry would make `config_changes` import `core.events` and create a cycle.
  **Issue:** #64
- **Decision:** Capture and detection live in `core/config_dependencies.py`. At begin, every story operation records `config_revisions` = `{story, deployment, rating_version}`. A generated `turn` adds `rating_guardrail_set` (hash of the story rating's effective guardrail ids), `policy_set` (hash of sorted `[id, version]` of the live output-applicable effective policies: global, the rating closure's, the story's and its characters'), `minor_age_threshold` and `default_output_viewpoint`. An ingested operation records `policy_set` only and compares nothing at commit, because its input was gated before it opened. Every other kind records the revisions only. `Operation._revalidate` re-captures the dependency members under the story lock and passes `config_changed` to `decide`. The order is abandon > read-set regenerate > `ConfigRecheck` > commit/rebase. Core holds the capture because `core.ratings` and the `policies` table give everything it needs, so no orchestrator hook is required.
  **Why:** Discretion: the dependency set is static per kind; the jsonb holds exactly that set plus the revisions (`rating_version` kept for #48); detection compares the hashes; ingest records policies only. Comparing values rather than revisions is what makes a prospective change, or an interpretive change elsewhere, cause no re-check.
  **Issue:** #64
- **Decision:** In the re-check loop (`run_with_revalidation(config_recheck=)`), a `ConfigRecheck` first re-captures the configuration (`Operation.recapture_config`, persisted to the row). It then runs the re-check with no lock held: `orchestrator/policies/rebase.output_recheck` checks every staged `message_rendered` text against `guard_context`'s active output policies. Deterministic policies always run. A model-assisted policy runs only while the turn budget can afford one more call; otherwise it resolves by `on_uncertain`. If the re-check passes, the turn commits again without generating, recording the new configuration. If it fails, the turn regenerates, and the regenerated output is re-checked before its own commit (`verify_next`). Up to `budget + 1` re-check rounds may run, regenerations spend `regenerate_retry_budget`, and beyond either limit the turn gets 409 `revision_conflict` (`reason` `config_changed` or `rebase_failed`). `attempts` records `outcome: recheck`, `config_changed` and `recheck: passed|failed`. `Operation.abandon()` now persists `attempts`.
  **Why:** The canonical rebase rule (pass 2). Re-checking the regenerated output is a deviation needed for "tightening a rule mid-turn never lets a reply through under the old rule": without #68's first-pass output gate, a regenerated reply would otherwise commit unchecked (CONFIG-REVALIDATION's mutation check showed exactly that). The extra round lets a story with `regenerate_retry_budget = 0` still commit a reply that passes.
  **Issue:** #64
- **Decision:** Listings: `GET /admin/config-changes` (deployment scope) and `GET /stories/{id}/config-changes` (story scope; 404 for an unknown story) return `{items: [{config_revision, setting, class, old, new, actor, at}], next}`, newest first by (revision, change id). Filters are `setting` (repeatable; a name also matches its leaves, and a name ending in `:` is a prefix such as `rating:`), `class` and `since`. Paging uses `limit` (1–200, default 50) and `after=<next>`. Values whose keys look secret-bearing (`api_key`, `token`, `secret`, `password`, `authorization`, `credential`) are shown as `***`; model-config key rows already store only fingerprints.
  **Why:** Discretion (pass 2: the shape, the filters, secret redaction). `GET /operations/{id}` already exposes `config_revisions` (#61).
  **Issue:** #64
- **Decision:** Tests:
  - `tests/test_u_18.py::test_u_18` (`part("M2","FR-CONC-14,15")`): every leaf is tagged with the planned classes. A fixture schema reports its untagged fields, top-level and nested, and those read as interpretive. A guard check confirmed the test bites: untagging `turn_deadline` failed `test_u_18`.
  - `test_u_18__writes` (`part("M2","FR-CONC-14")`, integration): story settings, story policies, a rating and deployment settings each take one revision of their scope, rows per leaf with their classes, and a control operation; the story-scoped writes also leave events; nothing moves the timeline revision; a no-op writes nothing.
  - Unmarked: `test_config_changes_has_one_writer`.
  - `tests/integration/test_config_revalidation.py::test_config_revalidation` (`part("M2","FR-CONC-14..16")`): a Hypothesis machine, 100 × 40, derandomized. It interleaves held turns with story-policy, rating-guardrail, rating-policy and threshold changes, an interpretive change to another rating, and prospective story and deployment changes. It predicts each release's first outcome from the dependency values, not from the log of changes. Every committed reply passes the rules at its commit, and its recorded dependencies equal the current ones. The last run: 116 commits, 58 rechecks (27 failed), 23 commits that were quiet despite a prospective change, 15 despite an interpretive change elsewhere. The stand-in model repeats a refused word every other time it is asked again after a failed re-check. Mutation checks failed the test as intended: no detection, prospective revisions counted as dependencies, and no re-check of the regenerated output.
  - `test_i_22__in_flight` (`part("M2","FR-CONC-15")`): a guardrail edit mid-turn leads to a re-check, regeneration and the clean reply, with the new rating version recorded. Complements: a relaxing edit re-checks and commits as generated; a `turn_deadline` PATCH mid-turn re-checks nothing; history is not re-checked.
  - `test_i_18__no_bypass` (`part("M2","FR-PROF-14")`, Hypothesis over every `StorySettings` field, 25 examples): the output re-check and the input gate both still catch the word. Complement: with the global policy retired, both let it through.
  - Unmarked `tests/integration/test_config_changes.py` (2): the settings surface and the listings.
  **Why:** The test plan. FR-PROF-14's §15 row is I-18 (M2), so the planned `test_fr_prof_14` is `test_i_18__no_bypass`. The output-side check uses the re-check path, because #68's output gate does not exist yet.
  **Issue:** #64
- **Decision:** Truncate and untruncate live in `core/timeline.py` as `control` operations (`bumps_revision`, `control_action`). The only mutation is `operations.membership`/`membership_tx` plus `stories.next_turn_index`; events, rows and valid-times are untouched (invariant 6 holds, nothing is deleted). Truncate flips every **active** committed operation of any kind positioned after N, and N's own inter-turn slots (`slot_k` not null; none in M2). Already-`discarded` operations from a stacked truncation keep their stamp, and `dormant` regenerated variants after N stay `dormant`. Only active operations are flipped, so an untruncate that reactivates `operation_ids` restores exactly the prior state.
  **Why:** AC 1 and the Discretion ("stacked truncations don't re-stamp", O(operations after N), payload `{to_turn, prior_head, operation_ids}`). Leaving dormant variants alone keeps the payload enough to undo exactly, and #66 owns dormant continuations. Lite summary segments (consolidation operations positioned at their `covers_to`) fall under the same rule, so no segment-specific code is needed.
  **Issue:** #65
- **Decision:** Both edits commit against current state. The payload is planned in the read phase and staged; a commit step recomputes the plan under the story lock, and if it differs it raises `RevalidationRequired(Regenerate("timeline_moved"))`. The same operation then restarts and restages, up to `EDIT_RETRY_BUDGET` = 3 times, after which it is 409 `revision_conflict`. Validation (`to_turn` < 0 or ≥ head → 422 at `body.to_turn`) runs at both points.
  **Why:** A non-bumping operation (consolidation) can commit after N without moving the revision, so `decide`'s revision-based read-set check would miss it. Recomputing under the lock is exact, and one operation id per edit is preserved. `test_i_04__timeline_moved` exercises the path.
  **Issue:** #65
- **Decision:** Undo is LIFO over "undoable" truncations, meaning the `truncate` events whose discarded operations are all still `discarded`, newest first. Untruncate reverts the top one. A named `truncate_operation_id` that is not the top one, or an empty stack, is 409 `nothing_to_undo`. Active turns after its `to_turn` mean 409 `swap_required` with `details {truncate_operation_id, to_turn, options: ["swap","fork"]}`. `GET /stories/{id}` reports `undo_available` (the stack is non-empty, so a reactivation or a swap exists) and `dormant_chains: [{truncate_operation_id, from_turn, through_turn, operations}]`, which lists the undoable truncations with active turns over them. The new errors are `NothingToUndo` and `SwapRequired` in `core/errors.py`.
  **Why:** Discretion (pass 1 and pass 2). This is the same definition the projection test helpers used, so #46's fold and TIME (f) needed no change. The `dormant_chains` item shape was not specified, and this shape is the minimum a client needs to offer the swap.
  **Issue:** #65
- **Decision:** REST lives in `api/routes/timeline.py`: `POST /stories/{id}/truncate {to_turn}` returns `{operation_id, timeline_revision, next_turn_index, to_turn, discarded_operations}`, and `POST /stories/{id}/untruncate` (optional body `{truncate_operation_id}`) returns `{operation_id, timeline_revision, next_turn_index, truncate_operation_id, restored_operations}`. Both are idempotent `Engine` methods (`truncate`/`untruncate` codecs, expected revision honoured), and requests use `extra="forbid"`. `discarded_operations` and `restored_operations` are counts, not id lists.
  **Why:** Discretion said "responses carry operation_id, next_turn_index, discarded count" (pass 1) and "responses name `discarded_operations`" (pass 2). A count keeps a 1,000-message rewind's answer small, and the ids are in the control event.
  **Issue:** #65
- **Decision:** Queued turns are failed inside the truncation's transaction with `timeline_changed {current_revision, control_operation_id}` through the new `turn_gate.fail_queued`. This applies only to turns still `queued`; one the drainer already started abandons through its own revalidation (#47). Waiters are woken after the commit. Untruncate does not fail queued turns, because the Discretion names truncate only.
  **Why:** Discretion ("queued turns at truncate are failed with `timeline_changed`", "truncate proceeds while in flight").
  **Issue:** #65
- **Decision:** The membership hook registry is `timeline.on_membership_change(hook, phase=HookPhase.IN_TRANSACTION|POST_COMMIT)` and `remove_membership_hook`. A hook is `hook(conn, MembershipChange(story_id, operation_ids, old, new, cause, control_operation_id))`. In-transaction hooks run inside the commit after the flip, and their failure rolls the edit back. Post-commit hooks get their own connection; a failure is logged with only the hook name, cause, operation id and exception type (invariant 3) and never undoes the edit. #72 registers here.
  **Why:** Discretion pass 2 (signature and two phases). `story_id` and `control_operation_id` were added to the change so a hook does not have to re-read them.
  **Issue:** #65
- **Decision:** `tests/projection_support.truncate`/`untruncate` now call `core.timeline`. `truncate` returns `None` without writing anything when `to_turn` ≥ head, where the stand-in used to write a no-op truncation and the real call answers 422. `untruncate` returns `None` on `nothing_to_undo`/`swap_required`. `swap` stays a stand-in until #66. `test_i_02` and `test_conc_linear` count a truncation only when one happened, and `test_s_truncate_midgen` asserts the result is not `None`.
  **Why:** Rule 3: earlier tests' timelines now run through the real operation, and the old no-op truncation is a 422 by the Discretion.
  **Issue:** #65
- **Decision:** Tests:
  - `tests/integration/test_i_03.py::test_i_03` (`part("M2","FR-STORE-6")`) uses a `lite_summary_segment` consolidation positioned at its `covers_to`. The state as of N includes the late-recorded earlier-positioned segment and excludes the later-positioned one; the complement is that a `seq` cut differs.
  - `tests/integration/test_i_04.py` has five functions:
    - `__truncate` (`FR-STORE-10,11,18`): row-level flip, events byte-identical, view and projections restored.
    - `__undo_stack` (`FR-STORE-10,18`): LIFO, no re-stamp, `nothing_to_undo`, `swap_required`, 422 bounds, `to_turn=0`.
    - `__queued_turns_and_hooks` (`FR-STORE-10`).
    - `__rest` (`FR-STORE-10,11,18`): endpoints, `as_of`, `undo_available`/`dormant_chains`, idempotent replay, archived and 404.
    - `__timeline_moved` (`FR-STORE-10`).
  - `tests/scenario/test_s_rewind.py` (`part("M2","FR-STORE-10..12,18")`): 1,000 player-only turns, truncate to 800, then a row-level diff after untruncate in which only `membership_tx` moves. It then regenerates message 1,000, and the old variant is kept dormant. The test takes about 7 s.
  - `test_time__b` (`part("M2","FR-STORE-10,11")`): for every N of a story with background work between turns, the truncation only flips membership and the untruncation restores every view through every turn and every projection row.
  
  No pre-fix guard run was planned.
  **Why:** The test plan. FR-STORE-18 is not in TIME's M2 bracket, so `test_time__b` claims 10 and 11 only.
  **Issue:** #65
- **Decision:** Migration `0016_variant_chains` adds `operations.parent_variant` (the active variant of turn N-1 a turn variant was built on; `NULL` for turn 1 and non-turn operations), set at commit and added to the operations guard's allow-list. Existing committed turns are backfilled from the active chain (falling back to 1). Every `turn` now takes variant `max(existing)+1` at its index (a turn written over a rewound tail used to reuse variant 1), and `_check_chain` also refuses an active turn whose `parent_variant` is not the active variant before it.
  **Why:** Discretion ("chain = `parent_variant` pointer", "`parent_variant` is a column on `operations`", "variant = `max(existing)+1`"). A column, not a table, so Rule 4 does not apply.
  **Issue:** #66
- **Decision:** Every `turn` commit at index t makes the still-`discarded` operations at ≥ t `dormant` (a rewound tail becomes a dormant continuation at the first new turn over it), and a regenerate makes every active operation at ≥ N dormant (the old variant and the work positioned on it), not just the turn row. The transcript projection mirrors the shelving from the turn's own events (rows at ≥ t that are `discarded` become `dormant`), so the fold reproduces it with no extra event. Membership only; no event is updated (invariant 6).
  **Why:** Discretion pass 2 ("a discarded tail becomes `dormant` at the first new turn over it"); FR-STORE-12 ("truncating to N-1").
  **Issue:** #66
- **Decision:** `core/variants.py` holds the variant tree. A chain starts at turn 1, where its parent has more than one child, or where membership changes, and it is named by `{turn, variant}` of its first node. `GET /stories/{id}/variants` (also `variant_chains` in `GET /stories/{id}`) lists `{turn, variant, membership, tip_turn, turn_count, membership_tx, parent}`. A selector resolves to its tip through the single same-membership child; several give 409 `unknown_chain` with `candidates`. A swap dormants every active operation after the branch turn and activates every non-active operation positioned on the selected path. It is one `control` operation with one `swap` event, built on `timeline._edit`, fails queued turns like truncate, and runs the membership hooks once per (old, new) pair. Swapping to the active chain writes nothing and answers 200 with `operation_id: null`.
  **Why:** Discretion (chain identity, wire shapes, `unknown_chain`, swap-to-active idempotent). Failing queued turns on swap was not specified; it matches truncate and regenerate, which are both control actions.
  **Issue:** #66
- **Decision:** `untruncate` no longer raises `swap_required` (the error class is removed). It reactivates the tail when nothing is active after `to_turn` and the tail continues the active turn there. Otherwise it swaps to the tail's chain and answers `swapped: true, active_chain, dormant_operations`. A truncation stays undoable while none of its operations is active. When dormant chains exist, `nothing_to_undo` lists `options: ["swap","fork"]` and `dormant_chains`. `dormant_chains` in `GET /stories/{id}` is now `{from_turn, variant, through_turn, turns, truncate_operation_id|null}` for every dormant chain. `undo_available` is true while a truncation is undoable or a dormant chain exists.
  **Why:** The #65 hand-off ("delegate untruncate→swap at SwapRequired"); FR-STORE-28 ("never silently rejects"). The changed `dormant_chains` shape is a deviation from #65's minimum shape; #65's REST test was updated.
  **Issue:** #66
- **Decision:** `POST /stories/{id}/regenerate {turn?, render_character_ids?, output_viewpoint?, prompt_params?}` → `lite.regenerate` with the new `EntryPoint.REGENERATE`. It reuses the head turn's recorded `player_input` (actor, say/do), render set, viewpoint and resolved generation settings unless overridden; re-gates the input; reads prompt inputs from before turn N; and runs one `turn` operation at N under `run_with_revalidation`. That operation is a control action that fails queued turns in its commit and dormants the old variant. A busy story is 423 `story_busy` in either concurrency mode (a regenerate is never queued). `Idempotency-Key` is optional, as for respond. The response is the respond body plus `previous_variant`. `Operation` now refuses `turn=` on a turn that is not the head (422).
  **Why:** Discretion. "Requires the key" was read as "is keyed like respond"; queueing a control action behind the turns it would fail is contradictory.
  **Issue:** #66
- **Decision:** `GET /stories/{id}/history?variant=turn:variant` reads the active history up to the branch and that chain's operations above it; `as_of` defaults to and is bounded by the chain's tip. A malformed selector is 422.
  **Why:** Discretion ("`history?variant=` reads along that chain above the divergence").
  **Issue:** #66
- **Decision:** AC3 (fork `variant_chain`) is only partly delivered. `/fork` does not exist yet (#67). `variants.fork_selection(db, story, variant_chain, at_turn)` is the selector #67 uses: the chain's operations through `at_turn`, which defaults to the tip; `at_turn` above the tip or below the divergence is 422. S-UNDO-SWAP asserts the selection. The two-independent-stories assertion is handed to #67, and AC3 stays unticked.
  **Why:** #67 is not built, and building fork here would be #67's scope.
  **Issue:** #66
- **Decision:** "A still-leased dormant tip → 423" is not implemented. Leases are per story (`in_flight_turn`), not per chain. A swap during a turn proceeds and the turn abandons (#47), as Discretion also says.
  **Why:** Nothing in M2 can lease a dormant tip.
  **Issue:** #66
- **Decision:** Tests:
  - `tests/scenario/test_s_undo_swap.py::test_s_undo_swap` (`part("M2","FR-STORE-28")`) covers 1,000 turns, truncate to 800, and 801'–850'. Undo swaps the chains, with the original active, the primes dormant and one chain. Swapping back gives a row diff of operations and an equal projection snapshot. A second swap to the active chain is a no-op. It asserts the fork selection, and the complement is an undo with no new turns as a plain reactivation. It takes about 34 s.
  - `test_time__regenerate` (`part("M2","FR-STORE-12")`) and `test_time__swap_chains` (`part("M2","FR-STORE-28")`) are in `tests/integration/test_time.py`, because the parts gate allows one file per test ID. They are not in a separate `test_regenerate.py`.
  - `tests/projection_support.swap` now calls `timeline.swap`, and its stand-in and helpers are removed. `test_i_04__undo_stack`/`__rest` were updated to the swap semantics.
  
  No pre-fix guard run was planned.
  **Why:** The test plan, and the part-uniqueness gate.
  **Issue:** #66
- **Decision:** Fork lives in `core/fork.py`. One connection runs `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` as its first statement, and the copy happens inside that transaction. A temp table maps old ids to new ULIDs: new ids are sorted so the source's relative order holds, and events get a dense new `seq`. Two temp plpgsql functions remap values: every `*_id`/`id` column, and every jsonb string (and object key) that is a mapped id, alone or as `prefix:<id>` (`character:<id>`, `policy:<id>`). Unmapped values (ratings, `client_event_id`) are kept. Every id is mapped before any row is copied. Each table in `STORY_TABLES` minus `STORY_TABLES_NOT_COPIED` is copied with one `INSERT … SELECT`. A row follows its operation when the table has `operation_id`; otherwise the row belongs to the story. Exceptions are declared in `fork.OVERRIDES`/`ROW_FILTERS`. `story_heads` is rebuilt from the copied turns, and story-scoped `config_changes` are copied.
  **Why:** Discretion (temp mapping table, ULIDs regenerated, `*_id` convention plus jsonb paths, generic enumeration). Value-based jsonb remapping replaces an explicit per-event-type path list: a new event type with an id in its payload is remapped without a code change.
  **Issue:** #67
- **Decision:** What is copied is the corrected history through N, as `views.corrected_history(through_turn=N)` reads it (the #65 hand-off): position-less active operations, plus operations positioned before N or at N with no slot, on the selected chain (`variants.fork_selection`, the #66 hand-off), committed only, and flattened to `active`. `membership_tx` is re-stamped only on rows that were not active. Left out: `audit` events, `maintenance` operations, and operations holding `truncate`/`untruncate`/`swap`/`fork`/`story_archived`/`story_unarchived` events. `story_archived`/`story_unarchived` were added to the planner's skip list because the fork is always active. Operations' and policies' `idempotency_key` are nulled, since keys stay with the source.
  **Why:** Discretion, plus FR-STORE-15 "current corrected history through N".
  **Issue:** #67
- **Decision:** The fork's `config_revision` is the source's, not 0. Its story-scoped `config_changes` rows keep their revisions.
  **Why:** This deviates from Discretion ("fork `config_revision` 0 + copied rows"). With 0, the copied rows would sit at revisions the story has not reached, and the next change would reuse their numbers in `GET /stories/{id}/config-changes`, which is ordered by revision.
  **Issue:** #67
- **Decision:** An unknown or ambiguous `variant_chain` is 409 `unknown_chain` (from `fork_selection`), not 422.
  **Why:** This deviates from Discretion pass 1. #66 fixed `unknown_chain` at 409 for every chain selector (swap, history), and fork reuses that selector.
  **Issue:** #67
- **Decision:** The fork's own genesis is written directly in the snapshot transaction: one committed `genesis` operation (`timeline_revision` 1) with one `story_created` event carrying `forked_from {story_id, revision, at_turn, variant_chain}`, appended after the copied events, with projections applied. The deployment-scoped idempotency key is bound to it (`IdempotentCall.bind`). It does not go through `Operation`, because the story row only exists inside this transaction. `forked_from.story_id` in the fork, and `fork_story_id` in the source's `fork` event, are the only cross-story names. They are provenance only, and the I-04 test excludes exactly those from its no-cross-reference check.
  **Why:** Discretion (fork keeps the copied genesis and adds its own; payload shapes). The `Fork` shell payload `{new_story_id, through_turn}` became `{fork_story_id, at_turn, revision, variant_chain}`.
  **Issue:** #67
- **Decision:** The source-side `fork` event is a non-bumping `control` `Operation` written after the copy commits. If it fails, a warning (ids only) is logged and the fork is still returned; this is the crash window Discretion accepted. Post-commit membership hooks fire on the fork with cause `fork`, one change per source membership of the copied operations, now `active`. Active→active is included so #72 can build the fork's summary. A serialization failure retries the whole copy 3× and then raises the new 503 `DatabaseUnavailable` (`database_unavailable`).
  **Why:** Discretion pass 2.
  **Issue:** #67
- **Decision:** `POST /stories/{id}/fork {at_turn?, variant_chain?, name?}` (body optional) → `Engine.fork`: deployment-scoped key, expected revision checked against the source inside the snapshot. The answer is 201 with `Location` and `{story_id, operation_id, timeline_revision: 1, next_turn_index, name, forked_from, characters [{id, name, source_id}], counts}`. `counts` gives copied rows per table and excludes the fork's own genesis. The default name is `"<source name> (fork)"`, or `null` when the source has no name. `at_turn` above the head, or below 0, is 422.
  **Why:** Discretion.
  **Issue:** #67
- **Decision:** Tests:
  - `test_i_04__fork`, `__fork_isolation` and `__fork_rest` (`part("M2","FR-STORE-15")`) check: a full copy through N equal to the source's corrected history through N (id-normalised); a rebuild equal to the copied projections; no shared ids and no row naming the source; a row-level diff of each story unchanged by turns, edits, truncations and deletion on the other; REST, idempotent replay, the hook, policies and `client_events` remapped (the #58 hand-off), and the refusals.
  - `tests/scenario/test_s_fork_snapshot.py` (`part("M2","FR-CONC-10")`) has three tests. The first interleaves a turn, a pin edit and a truncation inside the fork's transaction. The second forks concurrently with 30 turns and pin edits, and each fork equals the snapshot taken at its revision. The third covers the serialization retry and the 503.
  - Guard run against the pre-fix state: with `SET TRANSACTION … REPEATABLE READ` removed, both snapshot tests fail with a mixed copy (3 of 3 runs). With it restored, they pass.
  - `epistrel.testing.normalize` (`Normalizer`, `history`) is the shared id-free normaliser, for #75.
  - `test_s_undo_swap` now forks the promoted dormant chain, the active chain, and a partial chain. All three are complete and independent, and the source gained only its `fork` records (#66 AC3).
  **Why:** The test plan and the #66 hand-off.
  **Issue:** #67
- **Decision:** The output gate is `orchestrator/policies/output_gate.py`. `check_output` runs every deterministic output policy, then one `guardrail_check` per model-assisted output policy through `check_text`, sequentially. Every policy is checked, so a regeneration hears about each one the draft broke. A check the budget cannot pay for resolves by `on_uncertain`. `gate_output` drives the regeneration loop and the fallback chain. Character-scoped output policies apply only to the render characters. `CheckOutcome` gained `confidence`.
  **Why:** Discretion (checks sequential, all policies checked, character policies on render characters).
  **Issue:** #68
- **Decision:** The gate checks every draft under the output policies read when the turn's generate step starts (`guard_context`, which is about when the operation captures its configuration). A change that lands while the model is generating reaches the turn through #64's re-check at commit. That way no draft is checked twice under the same configuration. #64's `test_i_22__in_flight` complement ("relaxed mid-turn, the reply commits as is") now starts the turn with no guardrails and tightens mid-turn with a policy the reply passes. Under the old setup, the gate would have caught the dragon reply under the guardrails in force at its start, before any relaxing edit.
  **Why:** Assumption. A gate reading the configuration at check time would also be correct, but every change mid-generation would then cost a second, duplicate check at commit.
  **Issue:** #68
- **Decision:** The #64 hand-off. `rebase.output_passes` and the re-check now call `output_gate.check_output`, with the render set taken from the staged reply. The step in `run_with_revalidation` that verified the regenerated reply after a failed re-check (`verify_next`) is removed: every regenerated reply passes the output gate inside `generate`. Rebase-triggered violations still regenerate and then return 409; they never fall back. In #64's Hypothesis test, the stand-in model now replies through `gate_output`, as a Lite turn does. The gate catches its stubborn repeat (`gate_caught` > 0 is asserted), and the outcome prediction skips a call that the gate itself regenerated. Mutation check: with the gate's check forced to pass, `test_config_revalidation` fails.
  **Why:** The #64 → #68 hand-off ("route that path through your output gate rather than keeping duplicate checks").
  **Issue:** #68
- **Decision:** These follow Discretion:
  - Constraint lines are `Your previous reply violated: <message>. Reply again without it.` (the message's trailing period is stripped). There is one line per policy; lines accumulate over the turn and go in `GenerationInputs.policy_constraints` (category `policy_text`). The generation purpose no longer lists `policy_text` under `never_receives`, so the model guide, docs/02's data-flow row and the category description were updated.
  - Each regeneration's temperature is +0.1 per attempt, capped at 2.
  - `guardrail_retry_budget` is a `StorySettings` field (0–5, default 2, prospective) and spans the turn. `default_safe_fallback` is a new prospective deployment section, `GuardrailSettings` (`{name} changes the subject.`), and is exposed through `/admin/settings`.
  - The fallback chain is: the first render character's `safe_fallback_text`, then the deployment line, then the hard-coded `{name} says nothing.`. `{name}` is substituted in every line. Each line is checked deterministically only, and a failing line is recorded as rejected. The last resort is released whatever it breaks, with its violations reported.
  - A regeneration is attempted only when the budget can afford 1 generation plus one re-check per model-assisted output policy. Running out of budget or deadline mid-regeneration leads to the fallback; any other `ModelUnavailable` is a 503.
  **Why:** Discretion passes 1 and 2.
  **Issue:** #68
- **Decision:** Rejected texts are buffered and staged with the turn as `guardrail_output_rejected` events (category `audit`, beat 2) between `player_input` and `message_rendered`, so they commit with the turn and follow its membership. The payload extends #45's shell with `attempt`, `policy_ids`, `violations [{policy_id, name, message, kind}]` and `confidence`. `reason` is `output_policy` or `fallback_policy`. `message_rendered` gains `fallback_used` (default false). A fallback reply has `model`/`prompt_hash` null and `finish_reason` `fallback`. The respond and regenerate results always carry `guardrail: {violations, fallback_used}`, where `violations` covers the released text only, and `degradation` is `["safe_fallback"]` when the fallback was used. A new counter, `epistrel_guardrail_rejections_total{policy_id,stage}`, has `stage` set to `output` or `fallback`, and output violations also count in `epistrel_guardrail_violations_total{stage="output"}`.
  **Why:** Discretion (payload, flag, fallback `message_rendered`, violations of the released text, metric).
  **Issue:** #68
- **Decision:** Reservation. The input gate now reserves 1 generation plus G_out, one call per model-assisted output policy, through `Engine.gate_input(reserve_output_for=render_ids)`. Deterministic-only stories reserve no guardrail call. `Gate` now takes the render ids. `TurnBudget.max_calls` is still unbounded on respond until #69, so `test_i_18__call_count` asserts the reservation through `gate_input` with an explicit `max_calls`.
  **Why:** FR-LITE-7 / docs/02 §per-turn budget (G includes the first output checkpoint) and the AC's "none reserved".
  **Issue:** #68
- **Decision:** Tests:
  - `tests/scenario/test_s_selfpolice.py`: `test_s_selfpolice` covers violate twice then comply, violate always then the character fallback, a budget of 0, a pin and instructions that try to lift the rule, and policy rows unchanged. `test_s_selfpolice__deployment_fallback` covers the deployment line.
  - `tests/scenario/test_s_audit_category.py`: the full scenario plus #45's prerequisite slice, moved from `tests/integration/` because `check-traceability` refuses a part split over two files.
  - `test_i_18__call_count`, with its no-policy complement.
  - `test_minor_output_regenerates_then_falls_back`, for #55 AC3.
  - Pure tests in `tests/test_output_gate.py`.
  - The changed assertions in other tests follow from the new shape: `guardrail` in I-07's response fields, `fallback_used` in ingest's narration payload, and 2 checks per turn in the model-matrix minors row.
  **Why:** The test plan and the hand-offs.
  **Issue:** #68
- **Decision:** Extended the existing `orchestrator/budget.py` `TurnBudget`; no second class was added. It gains `reserved {guardrail_input, guardrail_output}` and `held` slots, which are 1 generation + G. Only a first attempt may claim a held slot. Retries, meaning #53 structured retries and the empty-reply retry, need `can_afford_unreserved`. Unplanned work checks the remainder and then holds its calls first; that covers guardrail regenerations and #47/#64 revalidation regenerations. Held slots are released after the input gate, after the first generation and after the first output check. The planned `BudgetedAdapter` wrapper and the TurnContext contextvar were not built. Instead, `ModelAccessLayer.send` (from #51/#53) stays the single debit and clamp point, and the budget is still passed explicitly.
  **Why:** Every adapter call already goes through `send`, which records exactly once. Its pre-check also means a refused call is never recorded, which is what the planner asked of the wrapper. A wrapper would have duplicated that logic.
  **Issue:** #69
- **Decision:** Planning (`plan_lite_budget`). The cap is resolved in this order: the request's `budget.max_calls`, then the story's `max_model_calls_per_turn`, then the deployment's `max_model_calls_per_turn`, then `9 + G`. Both settings are new, prospective and tagged; null means the formula, and the deployment PATCH accepts `null` for this one key. G is the model-assisted input policies for the actor (`say`+`do` is one text) plus the model-assisted output policies for the render set. Condition-false age policies are already excluded by `guard_context`. A cap below 1 + G is 422 `budget_below_safety_minimum`, with `minimum_required`, `max_calls` and `reserved`, and it is raised before the input gate and before any call. An override above the story cap is 422 `validation_error`, and so is a `deadline_s` above `turn_deadline`. A queued turn keeps the cap and deadline it was planned with in its queue row (`turn_budget`). Its clock starts when the drainer plays it.
  **Why:** The AC and the planner's discretion items (null = formula, deployment defaults, overrides above the cap → 422, budget check before the gate).
  **Issue:** #69
- **Decision:** Interpreted the "500 ms floor" as the lower bound of a request's `budget.deadline_s` (>= 0.5 s). It is not a floor on the per-call clamp. The clamp is exactly `min(call_timeout, remaining)`. I also kept the story's `turn_deadline` bounds from #63 (> 0, <= 600 s) instead of narrowing them to 5–300 s. Calls are bounded 1–50 as planned.
  **Why:** A clamp floor lets a call outlive the deadline, and it broke the existing #53 deadline test. Narrowing `turn_deadline` would break #63's queue tests and X-04, which use 0.5 s deadlines, and existing stored settings. The discretion item gave no rationale that outweighs either. Low cost to revisit.
  **Issue:** #69
- **Decision:** Degradation and failure paths:
  - If the budget or the deadline runs out during a guardrail regeneration, or during an output re-check (the first check included), the safe fallback is released, because an unchecked draft is never released. The cause is recorded as `retry_budget`, `call_budget` or `deadline` in a new `degradation_details: [{step, cause}]` beside `degradation`.
  - A deadline or budget failure on the first generation, or on the input check, is still 503.
  - If the remainder cannot afford the empty-reply retry, the turn is 503 `reason=empty`.
  - If it cannot afford a revalidation regeneration, the turn is 409 `revision_conflict` with `reason=budget`.
  - A turn-ending ModelError carries `model_calls` (failed attempts with their outcome) and `elapsed_ms` in `details`.
  - A not-attempted call (`not_configured`, `no_api_key`) is refunded so that counted calls equal recorded calls.
  **Why:** NFR-PERF-7 step 2 and the planner's discretion items (deadline mid-generation, an unaffordable empty retry → 503 empty, a regeneration → 409, the 503 body carries both).
  **Issue:** #69
- **Decision:** The turn report is persisted on the existing `operations.turn_report` jsonb column (no migration). It holds `model_calls`, `call_counts`, `degradation`, `degradation_details`, `budget {max_calls, reserved, used}` and `elapsed_ms`, and it is written by a commit step that also fixes `elapsed_ms`. The respond and regenerate results (library and REST) gain `call_counts`, `budget` and `degradation_details`. `GET`/`PATCH /stories/{id}/settings` echo `effective_budget {max_model_calls_per_turn, source, turn_deadline, reserved, minimum_required}`. A turn's deadline and `elapsed_ms` start at request receipt. A new outermost ASGI middleware (`ReceiptStamp`) stamps receipt into `RequestContext.received_at`; for a library call the clock starts on entry. Non-turn gates (ingest, pins, instructions) now run under the story's `turn_deadline`. **Deferred:** adding `model_calls`/`elapsed_ms` to the ingest, pins and instructions results is left to #73 (surface).
  **Why:** The planner's discretion items. The column already existed from 0002, and the ingest and pins result shapes belong to #73's surface pass.
  **Issue:** #69
- **Decision:** Tests:
  - `tests/perf/test_p_09.py` has three slices: rejection before any call (stub counter 0), a policy on both sides counting 2, the formula default, the overrides, counted == recorded == stored; degradation by `call_budget` and by `retry_budget`, plus the empty retry; and a slow stub against the deadline, plus a hang → 503 deadline, plus HTTP 500 carrying `model_calls`.
  - `tests/perf/test_p_05.py` runs over a session census (`tests/perf/census.py`, installed by `tests/conftest.py`, which also orders P-05 last). The census gates every turn in the session (counted == recorded, within the cap, and a clean turn = 1 + G), requires turns from the S-tests when they ran, and plays its own deterministic and assisted turns.
  - `tests/test_turn_budget.py` holds the unit tests.
  - Rule 3: `tests/test_markers.py` assumed that `tests/perf` holds only `live_model` tests. It now asserts the live ones are deselected and P-05 is collected.
  - I-07's response field set now includes the three new fields.
  - The word "scenario" in P-05's body was rephrased because of the M3 lexicon term.
  **Why:** The test plan.
  **Issue:** #69
- **Decision:** The migration is `0017_jobs.py`, not the planned `0015_jobs.py`, because 0015 and 0016 were already taken. `jobs` has the AC columns plus these:
  - `failures` (pass 2), separate from the `attempt` fencing token;
  - `coalesce_key`, with a partial unique index `uq_jobs_coalesce_key_queued` (`WHERE status = 'queued'`);
  - `created_at` and `updated_at`;
  - indexes `(status, due_at)` and `(story_id)`.

  `status` is checked text. `story_id` is plain, with no foreign key. The table is `core/tables/jobs.py`.
  **Why:** The issue plans this table, so this is not Rule 4. Checked text and a plain `story_id` follow `turn_queue`'s conventions, and story deletion enumerates `STORY_TABLES`.
  **Issue:** #70
- **Decision:** Every jobs time is `core.clock.now()`, passed as a parameter, not the database's `now()`. This covers due, lease, reap and backoff. Claim only takes `queued` rows that are due. Lapsed leases go back to `queued` through the reaper (`attempt++`), which runs every poll tick and in the recovery pass. A reclaim advances `attempt` twice: once at the reap and once at the next claim. The token is the row's `attempt`. Heartbeat, fail, drop and commit all match `(id, attempt, status = running)`. If a row is re-queued (reap, retry or drop) while another `queued` row holds its `coalesce_key`, it ends `done` with `result.coalesced_into` instead.
  **Why:** Discretion: "tests drive leases via the injectable clock", and "reaper flips expired leases to queued with attempt++, continuously in every poll tick". The coalesce rule keeps the partial unique index from rejecting a requeue.
  **Issue:** #70
- **Decision:** `commit_job` runs the job's operation through `Operation.write_in` in its own transaction, under the story lock. The kind is `maintenance` or `consolidation`; a story-less job runs as story-less `maintenance`. The first commit step locks the job row and checks the token, raising `Fenced` (REFUSED) on a mismatch. It then rechecks the declared inputs, raising `StaleInputs` (DROPPED) if any changed. The handler's events and steps run next, and the last step marks the job `done` with `result` (plus `operation_id`) and `input_versions`. Both exceptions subclass `Withdrawn`, so a refused or dropped attempt leaves its operation `abandoned`, not `failed`. A drop puts the same row back to `queued`, due now, with no failure counted. Retry backoff is `min(5·2^n, 300)` s ± 20 %, where n is the failure count before this one (5, 10, 20 s …). After `job_max_attempts` (5) failures the job is `failed` with `{"error": <exception class>}`. An unknown kind, or a payload that fails validation at claim time, fails the job terminally. The 10-minute runtime cap counts as a failed attempt.
  **Why:** The Discretion lines. `abandoned` matches what `Withdrawn` already means in batches: work that is no longer wanted, although nothing went wrong.
  **Issue:** #70
- **Decision:** Input versions live in `core/input_versions.py`, a core module, as `{"<table>:<id>": version}`. Readers are registered per table:
  - `operations` → `[membership, membership_tx]`, and must be `active`;
  - `stories` → `timeline_revision`;
  - `story_texts` → #47's `hash_member([pinned, custom_instructions])`.

  A row that is gone reads as `None` and is always stale. Handlers get `JobContext(db, job, payload)` with `snapshot()`, a REPEATABLE READ READ ONLY transaction, and `declare_input(conn, key)`. This deviates from the pass-2 `JobContext(conn, …)`: a handler that calls a model must not hold an open transaction across the call.
  **Why:** Discretion (pass 1/2: one generic recheck, versions per table). The recheck reads core tables, so it lives in core, and the import contract keeps core free of orchestrator imports.
  **Issue:** #70
- **Decision:** Worker settings go in env `Settings`: `worker_poll_interval` (5 s), `job_lease_seconds` (60), `job_max_attempts` (5), `worker_listen_enabled` (true) and `worker_concurrency` (2), all documented in `.env.example`. They are not in `core/settings_schema.py` and carry no `ConfigClass` tag.
  **Why:** Discretion pass 2 says "job settings live in env `Settings`". These are per-process knobs read when a worker starts, like `rebuild_async_threshold` and `stub_latency_s`, not deployment configuration that `record_config_change` versions. The coordinator's note was that every new setting goes in `settings_schema` with a tag; that applies to deployment and story settings. Low cost to move later.
  **Issue:** #70
- **Decision:** The worker (`orchestrator/jobs/worker.py`) has these parts:
  - One raw psycopg `LISTEN epistrel_jobs` connection per process, reconnecting with 1–30 s backoff and waking every loop after each reconnect. `NOTIFY` is sent in `enqueue`'s transaction with the kind as payload.
  - N loops. Each one clears its wake event, reaps, claims one job, and otherwise sleeps until `min(poll, next due)` or a wake.
  - A heartbeat every `lease/3`. If it finds the claim fenced it cancels the handler, and nothing is written.
  - `stop()` drains for 30 s and then cancels; a cancelled job keeps its lease, which then lapses.
  - `recovery_pass(db)`: reap, then the registered `async (conn)` hooks in order, each in its own transaction. A failing hook is logged and reported, and the others still run. `Worker.recovery_pass()` runs once per worker.

  The CLI is `epistrel worker [--concurrency N]` with SIGTERM/SIGINT → `stop()`. `serve --mode` is #71's.
  **Why:** Discretion lines. Logs carry job ids, kinds and exception class names only (invariant 3).
  **Issue:** #70
- **Decision:** The rebuild path from #46 is flipped. If a story has more events than `rebuild_async_threshold`, `Engine.rebuild_projections` enqueues `rebuild_projections` (coalesce key `rebuild_projections:<story>`, so asking again returns the queued job) and returns `RebuildQueued`. REST answers 202 `{job_id, timeline_revision, idempotency_key}`. The handler declares `stories:<id>` and folds in a commit step under the lock as a `maintenance` operation. Its result is `{counts, operation_id}`. The 202 body adds `timeline_revision` to the planned `{job_id}`, and the route returns it as a `JSONResponse`, documented under `responses={202: …}`.
  **Why:** #46's Discretion ("async 202 `{job_id}`"). `test_every_mutating_route_carries_its_revision` requires every story-scoped mutation response to be a `MutationResponse`, so the 202 model carries the revision (Rule 3).
  **Issue:** #70
- **Decision:** Story delete now covers jobs. `jobs` joins `STORY_TABLES`, so it is deleted with the story. Delete is refused with 423 `story_busy` `{reason: job_running, job_id}` while a job of the story holds a live lease. A job whose lease lapsed is deleted. `test_s_delete` now gives the victim a queued job, so that comparison still bites.
  **Why:** #50's hand-off ("#70 deletes queued jobs and adds running job → 423") and the pass-2 line "deleted story: row removed by #50's delete".
  **Issue:** #70
- **Decision:** Tests (`tests/integration/test_jobs.py`, 13):
  - `test_x_07__jobs` (`part("M2","NFR-REL-5")`) covers these clauses:
    - claimed within one poll with LISTEN off;
    - a worker killed (task cancel) mid-job, the lease lapsed on the movable clock, the next worker's recovery pass reclaiming it once, and the job done at attempt 3;
    - a second pass changing nothing (table byte-equal);
    - the lost claim's late commit REFUSED with no derived row, and the current claim's commit accepted.
  - The FR-CONC-8 clause is a separate part, `test_s_consolidation_race__jobs` (`part("M2","FR-CONC-8")`): a truncate while the job runs gives DROPPED, no `lite_summary_segment`, an `abandoned` operation, and the row `queued` and due now with 0 failures. Rerun, it declares only turn 1 and commits.
  - Complements: NOTIFY wake under a 30 s poll, backoff → `failed`, reclaim not counted, enqueue validation and coalescing, an unknown kind at claim, fenced heartbeat cancelling the handler, the runtime cap, recovery-hook order, the async rebuild (library and REST 202), delete with a running job, the CLI exit codes, and input versions.
  - Mutations that skip the stale check or the fence check each fail `test_x_07__jobs` or its sibling.

  **Why:** The test plan names one `test_jobs` part for "NFR-REL-5 half, FR-CONC-8", but `check-traceability` refuses FR-CONC-8 on X-07: §15 maps FR-CONC-8 to S-CONSOLIDATION-RACE in M2. The `__jobs` suffix mirrors X-07's split. `check-lexicon` (M4 "consolidat*", M6 "clock") required the bodies to avoid those words.
  **Issue:** #70
- **Decision:** Hand-offs:
  - #71 should register #63's queue-drainer recovery as a recovery hook, and run `Worker` beside the API in `all` mode (`serve --mode`). Worker mode currently drains no turn queues, because the drainer needs the Engine's handlers.
  - #72 should register `lite_summary` as a `consolidation` `JobHandler` that declares its turn operations, and complete S-CONSOLIDATION-RACE.
  - #71 owns `retention_sweep` and `idempotency_purge`, the 7-day purge of `done`/`failed` rows, and `worker_heartbeats`.
  - Nothing implements a `turn_queue_drain` kind; #63's in-process drainer still owns the queue.
  - Orphaned `in_flight` operations (#45/#50: a crashed genesis or maintenance op) are not swept by this recovery pass. A hook can do it later.

  **Why:** Keeps #70 to the planned queue, worker and rebuild flip.
  **Issue:** #70
- **Decision (invariant amendment applied):** CLAUDE.md invariant 6 now reads "Only erasure, opt-in compaction, and audit-record retention expiry remove events; erasure and compaction leave markers". This is the user-approved text recorded at `/n8-plan M2`. It keeps "compaction", because #246's rename to "pruning" is still open; #246 will swap the term. Whole-story deletion is still not named in the invariant. #274 tracks that, and I commented there; I did not widen the approved wording.
  **Why:** #71's acceptance criteria apply the amendment, and `/n8-plan M2` logged it as user-approved. I did not add a new `## Ad-hoc` section: the amendment was planned, not drift, and a section after this one would capture later stories' entries.
  **Issue:** #71
- **Decision:** The migration is `0018_timers` (the issue says `0016`, which is taken). It creates three tables:
  - `timers`: surrogate ULID, unique `(kind, target)`, nullable plain `story_id`.
  - `recurring_tasks`: `name` PK, Postgres `interval`, `last_status ok|failed` plus `last_error` (the exception class).
  - `worker_heartbeats`: `worker_id` PK, `mode`, `pid`, `started_at`, `at`.

  Seeds: `idempotency_purge` (1 h) and `retention_sweep` (1 d), due at migration time. `turn_queue_drain` (30 s) is not seeded; start-up reconcile writes it from the code registry. `timers` joins `STORY_TABLES`, so story deletion removes it, and joins `STORY_TABLES_NOT_COPIED`, so a fork does not copy it.
  **Why:** Discretion (pass 1 and 2). `test_s_delete` gives the victim a timer, so the comparison still bites.
  **Issue:** #71
- **Decision:** Firing (`orchestrator/jobs/timers.py`):
  - `fire_due` uses one transaction per row, `FOR UPDATE SKIP LOCKED`, with `core.clock.now()` passed into the SQL.
  - A fired timer is deleted and enqueues a job of its kind `{target}`, with coalesce key `timer:<kind>:<target>`.
  - A fired recurring task sets `last_run_at = now` and `next_run_at = now + interval`, and enqueues its job with coalesce key `recurring:<name>`. Three missed intervals therefore fire once.
  - An unknown timer kind, or a target the handler's payload refuses, is deleted and logged.
  - Only registered tasks with a handler fire. Unregistered rows are left alone and logged by `reconcile`. `/health` counts only registered rows, so a leftover row cannot hold a deployment in `degraded`.
  - Every worker loop calls `fire_due` each tick. The idle sleep also wakes for the next timer or task.
  - The recovery pass now runs reap → reconcile → fire_due → hooks, and reports what it `fired`.
  **Why:** Discretion: firing enqueues a job, the transaction that fires sets `last_run_at`, catch-up is aligned, the recovery pass fires overdue rows, rows without a handler are skipped and logged.
  **Issue:** #71
- **Decision:** `JobHandler` gains `max_attempts`, which the sweeps set to 1 ("failed run waits one interval"). It also gains `records_operation`; when that is False, `commit_job` finishes the job under its fencing token with no `operations` row. `recurring.recording(run)` makes the job set `last_status`.
  **Why:** `turn_queue_drain` runs every 30 s, and an operation per run would add about 2,900 empty maintenance operations a day. Sweeps write no story state. This is Rule 2 (needed so the planned 30 s task is sane) and is a small extension of #70's handler type.
  **Issue:** #71
- **Decision:** `retention_sweep` (`handlers/retention.py`):
  - It deletes `audit` events whose `tx_time <` the cut-off, using the story's own `audit_retention_days` when that is a number, else the deployment's. Operations still `in_flight` or `queued` are exempt.
  - It deletes `audit_log` rows by `at`, with the same story override. A row with a null or deleted story uses the default.
  - It deletes `done`/`failed` jobs whose `updated_at` is older than `job_retention_days`.
  - Each 1,000-row chunk is its own transaction under `epistrel.allow_audit_expiry`, at most 100 chunks per table per run. No marker is written.
  - No M2 operation kind writes only audit events, so no operation rows are deleted.
  - Fired timers delete themselves, so there are no timer rows to prune.
  **Why:** Discretion (pass 1 and 2). The settings are new `DeploymentSettings` sections:
  - `RetentionSettings`: `audit_retention_days` 1–3650, default 90; `job_retention_days`, default 7.
  - `WorkerHealthSettings`: `worker_heartbeat_timeout` 1–3600, default 60; `timer_lag_alert` 1–86400, default 300.

  All four are prospective and tagged. `/admin/settings` PATCH accepts them. `StorySettings.audit_retention_days` (null → default, 1–3650) is tagged prospective.
  **Issue:** #71
- **Decision:** `idempotency_purge` = #61's `purge_expired` (results nulled, tombstones kept) plus stale keys. An `in_flight` key whose lease lapsed more than one result window ago is handled by its operation's status:
  - `committed`: becomes a `done` tombstone.
  - none, a missing operation, `failed` or `abandoned`: deleted.
  - `queued` or `in_flight`: left alone.
  **Why:** Discretion: "stale `in_flight` idempotency rows past expiry purged". A committed one must keep at-most-once semantics (FR-CONC-6), so it is settled rather than deleted.
  **Issue:** #71
- **Decision:** Process modes. `EPISTREL_MODE` (`all|api|worker`, default `all`) is an env `Settings` field, following #70's convention for per-process knobs. `epistrel serve --mode/--concurrency` overrides it, `epistrel worker` is `serve --mode worker`, and uvicorn runs with `workers=1`.
  - `all` and `worker`: `Engine.start_workers` runs `Worker` as an asyncio task, registers the recovery hook `turn_queue` (the drainer's `recover`), and binds the drainer to the `turn_queue_drain` job.
  - `api`: keeps #63's one-off start-up recover.
  - `worker` mode builds the same app with only the public router (`/health`; `/metrics` arrives with #74).
  - The entrypoint migrates only when the mode is not `worker` (from `--mode`, `--mode=`, or `EPISTREL_MODE`). The Dockerfile gains `CMD ["serve","--mode","all"]`. The HEALTHCHECK prints the status, exits 0 on `ok`/`degraded` and raises on 503. Compose gains `command: ["serve","--mode","all"]`.
  - `stop_workers` stops the worker (DRAIN_S 30 s < lease 60 s), unregisters the hook, and unbinds the drainer.
  **Why:** Discretion (pass 1 and 2) and the #70 hand-off ("worker-only mode drains no turn queue").
  **Issue:** #71
- **Decision:** `tests/auth_support.auth_settings` now defaults to `mode="api"`. Any test that builds an app gets no background worker unless it asks for one; X-07's `all`-mode test asks explicitly.
  **Why:** With `all` as the default, every app under test would start a worker. That worker would fire the seeded sweeps (purge, retention) and claim test jobs behind other tests' backs. `api` keeps #63's start-up recover, so the existing queue-recovery tests are unchanged.
  **Issue:** #71
- **Decision:** `/health` returns `{status, checks: {database, workers: {alive, last_heartbeat_s}, oldest_overdue_timer_s, oldest_overdue_job_s}}`. It is one statement using the DB's `now()`, under a single 2 s timeout.
  - `alive` counts heartbeat rows within `worker_heartbeat_timeout`. Heartbeats are one row per loop, upserted every poll tick by a per-process task, pruned after 10 min, and deleted on a clean stop.
  - The overdue fields are `null` when nothing is overdue. The timer field folds in registered recurring rows. Running (leased) jobs are not counted.
  - `degraded` (200): `alive == 0` or something is overdue for more than `timer_lag_alert`.
  - `unavailable` (503): the DB check fails or times out, and every field is null.
  - If the snapshot fails while the DB answers, the result is `degraded` with null fields.
  - An app that never started reports `ok` with `database: skipped`.
  - `test_health*.py` were updated to the new shape. A migrated DB with no worker is now `degraded`.
  **Why:** Discretion: DB-down gives null fields, `last_heartbeat_s` is the newest heartbeat's age (null → degraded), `oldest_overdue_job_s` is added, `/health` arithmetic uses DB `now()`, and the probe has its own 2 s timeout.
  **Issue:** #71
- **Decision:** Tests:
  - `tests/chaos/test_x_07.py` has 8 `test_x_07__*` functions (`chaos` marker; parts `NFR-REL-5`, `NFR-REL-5; NFR-OPS-1`, `NFR-OPS-1,2`):
    - `timers`: polled within one interval with no NOTIFY; 10 timers × 4 racers fire once each; upsert and cancel; unknown kind and refused target dropped.
    - `recurring`: three missed intervals and 4 racers fire once; `ok`/`failed` plus the class after one attempt; an orphan row is never fired; reconcile updates the interval.
    - `restart`: a 4 h outage on the movable clock; recovery reclaims the job and fires the purge once each; the result is purged and the tombstone kept; the fresh result is kept; a second pass does nothing.
    - `sweep_rerun`: the sweep is cancelled inside its second audit_log chunk; the first chunk stands; the rerun deletes the rest; a third run deletes nothing.
    - `health`: degraded and ok cases, the lag complement, an overdue job vs a leased job, a stale heartbeat.
    - `all_mode`: lifespan worker with alive == 3; heartbeats removed on stop; worker mode serves no API routes; api mode starts no worker.
    - `turn_queue`: the worker-mode hook and the 30 s task kick the drainer, and the unbound case is a no-op.
    - `processes`: two real `epistrel worker` subprocesses run the purge, which missed three runs, once; 404 for API routes; after SIGKILL an api process reports degraded with alive 0.
    - `compose`: services `== {engine, db}`, `command` is `all`, no broker/cron images; the HEALTHCHECK command is run against a stub server (degraded exits 0, 503 exits non-zero); entrypoint migration per mode with stubbed `alembic`/`epistrel`.
  - `tests/scenario/test_s_retention.py` (`FR-STORE-30`) covers:
    - The deletes: 91-day audit under the default, and 31-day audit in a story with a 30-day override, are gone; the matching `audit_log` rows are gone.
    - The complements that are kept: 89-day, 31-day under the default, 29-day under the override, fresh pipeline audit, and an unscoped 89-day `audit_log` row.
    - Every non-audit event and every operation (active, discarded and dormant) is byte-equal.
    - The test plan's "pre-change trigger" check: the same sweep run without the exemption (`_allow_audit_expiry` made a no-op) is refused with `EPI01` and deletes nothing.
  - `test_idempotency_purge_settles_stale_keys` covers the stale-key cases.

  **Why:** The issue's test plan. Workers run as subprocesses only in `processes`; in-process `Worker`s elsewhere keep the run time to seconds. The heartbeat timeout and the 400 s lags are aged in SQL, because `/health` uses DB `now()`. `check-lexicon` (M6 "clock", M7 "import") kept those words out of test bodies (`_now()`, `_due()` helpers).
  **Issue:** #71
- **Decision:** The "pre-change trigger" fail-first step could not be run literally. #45 already shipped the events and audit_log triggers with the `epistrel.allow_audit_expiry` exemption, so no pre-change trigger exists to run against. The equivalent check is the sweep without the exemption, which was observed failing with EPI01 and is kept as the complement in `test_s_retention`.
  **Why:** The trigger change the plan anticipated landed earlier (#45, `0002_event_store`/`0010_audit_log`).
  **Issue:** #71
- **Decision (Rule 1, orchestrator):** `test_p_09__deadline` failed once in the full suite after #71 (`outcomes[-1] == "ok"`, not `"timeout"`). Its 0.3 s stub calls let the deadline fall in the gap between calls under load, so no call was cut. The stub latency is now 0.7 s: the deadline lands inside the second generation unless the gaps exceed 0.6 s. No product change; the assertion is unchanged.
  **Why:** Make a #69 timing test deterministic without weakening it. Before the fix it passed 5/5 alone and failed under full-suite load; after, 3/3 alone and the full suite passed (738).
  **Issue:** #69
- **Decision:** The migration is `0019_lite_summary`, not the planned `0017_lite_summary`, because 0017 (jobs) and 0018 (timers) are taken. `lite_summary_segments` is a projection table: `segment_id` (the event id), `story_id`, `seq`, `operation_id`, `covers_from`, `covers_to`, `level`, `turn_variant`, `text`, `membership`, `source_versions` (`[{operation_id, membership}]`), `model`, `prompt_hash`. It is unique on `(story_id, covers_from, covers_to, level, turn_variant)`. The `lite_summary_segment` payload (shell) became `{text, covers_from, covers_to, level, source_versions, model, prompt_hash}` and lost `through_turn`. Eight test call sites were moved to the new shape, using `tests/event_support.segment_payload`, which gives each synthetic segment its own `level` so it never collides with the unique index.
  **Why:** AC 1 and the Discretion (`source_versions` list; `model`/`prompt_hash` stored now; the unique key).
  **Issue:** #72
- **Decision:** Segment membership is kept by the projection, not by a `timeline` hook. `core/projections/lite_summary.py` follows the control events exactly as `transcript_messages` does: truncate/untruncate/swap flip the named operations; a turn of variant > 1 at N makes active rows at ≥ N dormant; every turn makes discarded rows at ≥ N dormant. `settle()` then recomputes `superseded`: a row whose operation is active is superseded while an active merged row names it as a source. A truncation through a merged segment therefore returns its surviving children to active, and an untruncate supersedes them again. The #65 post-commit hook (`timeline.on_membership_change`, phase `POST_COMMIT`) only schedules the story's job, for truncate, untruncate, swap and fork.
  **Why:** The Discretion says "membership stored on the row, flipped by #65's hook". A hook-only flip would not survive a rebuild. Doing the flip in `apply` keeps fold == incremental (FR-STORE-4), and I-02's property test now snapshots and corrupts this table as well. It also covers the flips that fire no hook (#66's note: regenerate's retire and the turn-commit shelving), so dormant is treated like discarded.
  **Issue:** #72
- **Decision:** Prompt inclusion (`core.summary.included_segments`) keeps segments whose operation is active, whose every source operation is still active, and which no in-scope merged segment holds. For a regenerated turn N only segments wholly before N are in scope, so the children of a merged segment that covers N stand in for it. The included run must be contiguous from turn 1. Verbatim rows are every row after its `covers_to`, and never fewer than the latest `lite_window_messages` rows. As a consequence, `test_respond::test_window_size_counts_transcript_rows` (#57) now asserts that the window is bounded by the summary: with no summary yet every row is sent, and after the job runs the rows start after the segments. `test_i_18`/`test_i_07` are unchanged, because their synthetic segment overlaps the latest 30 rows.
  **Why:** The Discretion says "the prompt sends every verbatim row after covers_to" (no row drops out before a segment covers it). FR-LITE-1's "most recent N messages verbatim" must still hold after a rewind leaves few uncovered rows. The regenerate case was found by `test_i_18__summary`: the regenerated prompt lost turn 1's summary while its merged parent was out of scope.
  **Issue:** #72
- **Decision:** Chunks are measured in turns. `lite_summary_chunk` (10 rows) / 2 = 5 turns, on a fixed grid from turn 1 that never splits a turn. A chunk is compressible when it is wholly older than the trailing `lite_window_messages` rows of the uncovered tail. The job is enqueued only when there is work: such a chunk exists, or the summary is over `lite_summary_max_tokens` with more than one segment. Either condition implies more than `lite_window_messages` verbatim rows. A turn commit with 31 rows but no whole chunk outside the window does not enqueue.
  **Why:** AC 1's trigger, made precise. Enqueueing on ">30 rows" alone would commit an empty consolidation operation on every turn until a chunk qualifies.
  **Issue:** #72
- **Decision:** Each segment is its own `consolidation` operation positioned at its `covers_to` with `operations.source_window = {from_turn, to_turn}`. To commit several of them atomically, `JobOutput` gained `positioned: tuple[PositionedOutput, ...]` (turn, stage, a pre-assigned operation id, source window) and `source_window`. `queue.commit_job` begins those operations and writes them in the job operation's transaction, after its fencing/input-recheck step, and settles them all together. `Operation` gained `source_window=` (inserted at `begin()`). A commit step on the job operation drops the output (`StaleInputs`, requeue) when another run already wrote one of the same segments. If any declared input changed, everything is dropped and the same row is requeued (#70's path).
  **Why:** Discretion: "each segment is its own consolidation operation". #70's `commit_job` commits one operation per job. Merged segments name in-run children, which need ids before commit.
  **Issue:** #72
- **Decision:** Scheduling goes through a Core port, `core/summary_port.py`. `set_scheduler` is set by the job module at import. `Operation` calls `notify` after every committed `turn` operation (respond, regenerate, queued turns, ingest), but only when bound to an `AsyncEngine`, and failures are logged by class name. The timeline post-commit hook calls the same `schedule_in`. One queued job per story is kept via `coalesce_key = lite_summary:<story>`. The recurring task `lite_summary` (10 min, #71's `register_recurring`, reconciled at start-up, no seed) shares the handler. A story-less run fans out to every story with more than two active transcript rows that has work. Every `lite_summary` job has `max_attempts = 1`; the next trigger retries. `recurring_tasks.last_status` is recorded only for the story-less run.
  **Why:** Discretion: "hooks in core/timeline.py registry (#65) with a port in core for enqueue", "control operations enqueue", "one queued job per story (coalesce), enqueued after commit"; #71's hand-off (one kind, NoPayload, max_attempts 1, per-story fan-out).
  **Issue:** #72
- **Decision:** The job calls `lite_summary` through the Model Access Layer bound by `Engine.start_workers` (`lite_summary.bind`, unbound on stop; tests bind via `tests/summary_support.run_due_jobs`). It runs under its own `TurnBudget(max_calls=20)` and never a turn's, so no call reaches a turn's `model_calls`. Params are temperature 0.3 and `max_tokens` 600, and 400 tokens are asked for in the prompt. The `lite_summary.j2` template has one `mode` (`compress`/`merge`): third person, past tense, keep names and commitments, the prior segment as context only. `LiteSummaryInputs` gained `mode`, `merge_segments`, `covers` and `target_tokens` (all `rolling_summary`), and `docs/models.md` was regenerated. The Lite prompt heading is now "Earlier in the story", with `Turns a-b:` labels. A hyphen is used because ruff's RUF001 refuses an en dash.
  **Why:** Discretion items (level target, temperature, ≤ 20 calls, one template with `mode`, heading and labels, prompt wording).
  **Issue:** #72
- **Decision:** Each new text passes the story's deterministic output policies (`output_gate.deterministic_check` over `output_policies` minus model-assisted ones). A violating text is asked for once more. A second violation writes no segment, stops the later chunks, and leaves the rows verbatim. Each rejected text is a `guardrail_output_rejected` audit event (`reason output_policy`, `attempt` 0/1) on the job's position-less consolidation operation. A merge that is rejected twice stops merging for that run.
  **Why:** AC 3 and the user's Discretion call.
  **Issue:** #72
- **Decision:** The fork rebuilds `lite_summary_segments` by folding the copied events (`STORY_TABLES_REBUILT`; `projections.fold(..., only=...)`) instead of copying rows. `stories.STORY_TABLES` gained the table, so deletion empties it. The fork's post-commit `fork` hook schedules the fork's own job for the turns after the copied segments.
  **Why:** #67's note: a new table is copied by default unless it is rebuilt. A copied `superseded` flag could be wrong in the fork when the merged parent lies past the fork point; the fold gets it right.
  **Issue:** #72
- **Decision:** `GET /stories/{id}/history?include_summary=1` (and `Engine.history(include_summary=)`) returns a separate `summary` array of the included segments wholly at or before `as_of`, as `{level, covers_from, covers_to, text, operation_id}`. Segments never become history items. New settings `lite_summary_chunk` (10, 2–200) and `lite_summary_max_tokens` (2000, 100–100000) are prospective `LiteDefaults` fields (deployment, `/admin/settings` patch included) with story overrides. Both are tagged in `test_u_18`.
  **Why:** Discretion: "`?include_summary=1` → separate `summary: [...]`", "settings deployment-level, story-overridable, prospective".
  **Issue:** #72
- **Decision:** Tests. `tests/integration/test_i_18.py` has three slices: `test_i_18__summary` (`part("M2","FR-LITE-2")`: queue + coalesce, three segments with a level-1 merge, prompt and `include_summary`, fork copy plus the fork's own job, truncate/untruncate/regenerate memberships with the rows and events kept, the inactive-source complement), `test_i_18__summary_policy` (wordlist policy + scripted stub) and `test_i_18__summary_budget` (`part("M2","FR-LITE-7")`). The test plan's `test_summary_not_in_turn_budget` is that last slice, because a part on an unnamed test id fails `check-traceability`. `tests/scenario/test_s_consolidation_race.py` holds `test_s_consolidation_race` (`part("M2","FR-CONC-8; FR-STORE-24,27")`): turns 1–901, a run over 1–700 commits, then a held run over 701–900 is truncated to 800, giving DROPPED, the same row queued with 0 failures, its three operations abandoned, and every one of turns 701–900 declared. After one more turn the rerun declares only turns 701–800 and commits the segment with `source_window {701, 800}`. No turn position moved, and every segment event is at its `covers_to`. The test takes about 30 s, because it plays 901 turns with the scheduler off. #70's `test_s_consolidation_race__jobs` moved into the same file, since the parts gate allows one file per test id. Unmarked tests are `tests/test_lite_summary.py` (grid, merge choice, labels, projection membership rules, port failure) and `tests/integration/test_lite_summary_job.py` (recurring fan-out, no models bound → `failed` with `NoModels`, two concurrent runs → one DROPPED, story-less positioned output refused).
  **Why:** The issue's test plan. `check-lexicon` (M6 "clock") kept `clock.now()` out of the race test's body.
  **Issue:** #72
- **Decision (Rule 3, tests):** `tests/chaos/test_x_07.py`'s `registry` fixture now reconciles and then pushes every recurring row 30 days out, restoring them afterwards, as its `quiet` fixture does. With `lite_summary` registered, the first reconcile creates that row due now, and step 3's `queue.claim` picked up the story-less summary job instead of the test's own job.
  **Why:** A new recurring task broke an assumption of #71's test, not its behaviour.
  **Issue:** #72
- **Decision (Rule 1, tests):** `test_a_second_run_over_written_segments_is_dropped` failed once in the coordinator's full run (`[0, 2] == [2, 0]`). The test released two concurrent runs from one stub latch, then listed the job results in creation order, which assumes the first-created job wins the commit. Nothing promises that: whichever run takes the story lock first commits both segments, and the other is dropped, requeued, and finds nothing on its rerun. The test now compares the sorted results (`[0, 2]`), as it already did for the outcomes. It still asserts exactly one winner with 2 segments and one empty rerun. No product code depends on that order.
  **Why:** The order was a race in the test, not in the product. The failure did not reproduce in 6 isolated runs; the full suite passed afterwards (752).
  **Issue:** #72
- **Decision:** `Engine` operations are every public coroutine method not marked `@library_only(reason)` (`epistrel.engine.operations()`); nine methods are marked (lifecycle, `noop`, `check_expected_revision`, `gate_input`, and the reads/writes served inside another route: `undo_state`, `effective_budget`, `policies_effective`, `policies_set_story`). Every route's `operation_id` is its method's name; the two config-change listings are `config_changes_deployment` / `config_changes_story` (suffix by scope instead of the planned `_list`/`_get`, because both are lists).
  **Why:** Discretion ("`@operation` decorator registry", "operation_id = method name", "two routes on one method get suffixes"). Marking the exceptions keeps a new method in the parity list by default, so a library-only addition fails C-02/C-03.
  **Issue:** #73
- **Decision:** Library and REST do **not** share response *types*: the Engine still returns its dataclasses and each route renders them through `…Body.of(result)` classmethods (added for every route that built its body inline). C-03 renders library results through the same `.of` and compares JSON.
  **Why:** Making the Engine return the REST pydantic models would invert the layering (core results → api models) across ~40 methods; shared renderers make parity structural without that (Discretion "shared models, so parity is structural" read as shared rendering). Request models were already shared.
  **Issue:** #73
- **Decision:** `epistrel.api.openapi.build` post-processes FastAPI's document: OpenAPI 3.1, `info.version "m2"`, no `servers`, FastAPI's `HTTPValidationError`/`ValidationError` removed, every error status of every operation referencing one `Error` envelope with the codes listed (`OPERATION_ERRORS` plus method/path rules), header parameters the handlers read (`X-Epistrel-Principal` on mutations, `Idempotency-Key`, `X-Epistrel-Expected-Revision`, `Prefer`, `?viewpoint=`), response headers (`Idempotency-Key`, `Idempotent-Replay`, `Location`, `Retry-After`, `WWW-Authenticate`), a `QueuedTurn` 202 on respond/ingest, tags (stories, timeline, history, operations, settings, admin, ops) and a doc-only `StorySettingsPatch` body for `PATCH /stories/{id}/settings`. The app serves it (`openapi.install`); `python -m epistrel.tools.openapi --check|--write` writes the same document (sorted keys, 2-space) to `docs/openapi.json`; CI's `lint` job runs `--check` (`if: always()`).
  **Why:** AC1 and the Discretion items (3.1, fixed version, no servers, stripped validation components, envelopes at router level, response headers, tags, offline build). FastAPI's `exclude_none` strips `null` from examples, so no example uses `null`.
  **Issue:** #73
- **Decision (Rule 2):** Boundary validation tightened (FR-API-4): (a) every 422 `validation_error` now carries `details.field` and `details.errors[{field, message, type}]` (`InvalidRequest` fills them when a raiser gives neither; non-field failures are `field: "body"`; respond's cast errors name `body.actor_character_id` / `body.render_character_ids` / `body.scene_context.characters_present`); (b) JSON bodies are validated in pydantic **strict JSON mode** against the route's body type before FastAPI's lax pass (`api/validation.strict_json_body`, a protected-router dependency after auth): `"5"`→int and `true`→float are refused; (c) NUL characters are refused in path, query and body (REST) and in every Engine mutation's input (`core.errors.refuse_nul`); (d) a body that is not UTF-8 is 422 at `body`, and Starlette's 404/405 use the envelope; (e) story/operation/rating path ids are ULID-validated (422 at `path.<name>`), policy ids against the policy-id pattern; (f) `?since=` takes RFC 3339 only; (g) `X-Epistrel-Principal` is trimmed of spaces/tabs only (other control characters now refuse). Eight existing tests that compared `details` exactly were updated to the new keys.
  **Why:** All found by C-01's schemathesis run (lax coercion accepted schema-violating bodies; a NUL in a rating name was a 500; a non-UTF-8 body answered FastAPI's bare `{"detail"}`; `since=3.6e16` parsed as a timestamp; a trailing `\x1f` in the principal was silently stripped). They are input-validation gaps on routes this story owns.
  **Issue:** #73
- **Decision:** C-01 runs schemathesis 4.x (`schemathesis>=4.28.0`, MIT, dev group; locked 4.30.1; 3.14 supported) over the **committed** schema with its security requirements removed and the credential added to every call (auth has its own guards), 100 examples per operation, positive + negative modes, checks: no 5xx, documented status, content type, response schema, response headers, negative data rejected. Excluded from generation: `delete_story`, `archive_story` (they would end the fixture story) and the two API-key routes; everything else, admin writes included, runs on a **scratch database** migrated to head so generated policies/ratings/settings never reach the session DB. Negative cases assert zero new `operations`/`events`/`config_changes` rows. `test_c_01__schema` checks the committed file equals the served/generated one, 3.1/`m2`/no servers, every component has a title and description, every operation an operationId/tag/summary, every 4xx/5xx the `Error` envelope, every body forbids unknown fields and has an example that validates, and that the settings PATCH description names every `StorySettings` field; `test_c_01__malformed` hand-checks one malformed input per kind. Derandomisation comes from the existing `ci` Hypothesis profile.
  **Why:** Discretion (schemathesis, excludes, fixture story, 100 examples, row counts, coverage check in C-01). Removing security from the test copy avoids masking schemathesis's "missing Authorization" negations with the injected credential. About 2 minutes locally.
  **Issue:** #73
- **Decision:** C-03 runs one scripted sequence of 51 steps (all 43 operations, a few twice, plus error paths: 403/409/422 and a NUL body) on each surface against **its own fresh scratch database**, so deployment state, revisions and config revisions evolve identically and are compared exactly; normalised: ULIDs (values and keys) by first-seen position, timestamps, `elapsed_ms`/`latency_ms`, the stub's `request_id`, `config_revisions.policy_set` (a hash of ids) and the echoed `idempotency_key`. Stub generation replies are fixed with `stub.when("", REPLY, purpose="generation")`. `parity_gaps` (operations without a route or a step) is asserted empty; the complement test adds a library-only method to a subclass and sees it reported.
  **Why:** Discretion ("each surface against its own fresh story", normalisation set, stub replies scripted, `httpx.ASGITransport`); separate databases make "same inputs" exact for deployment-scoped calls too, so revisions need no normalising.
  **Issue:** #73
- **Decision:** #69's deferred hand-off: ingest, `PUT pinned-memories` and `PUT custom-instructions` results gain `model_calls` (their input gate's calls, as a turn reports them) and `elapsed_ms` (from `ctx.received_at`). The Engine fills them (`_metered`, using `metrics.recording()`), so the stored idempotent result and its replay carry the same report. Tested by `test_input_gate.py::test_passing_gates_report_their_calls`.
  **Why:** #69 → #73 hand-off.
  **Issue:** #73
- **Decision:** Hand-offs to #73 checked one by one: #66 (regenerate/swap/variants routes, `history?variant=`, `unknown_chain` 409, `nothing_to_regenerate` 422 documented; no `swap_required` remains anywhere), #67 (fork 201 + `Location` declared, `database_unavailable` 503 documented; the `fork` event payload and `story_created.forked_from` are event-log content with no REST read in M2 — not exposed), #68 (`guardrail` in respond/regenerate bodies, `default_safe_fallback` in `/admin/settings`; `guardrail_retry_budget` was missing from the settings PATCH description — added, with a test that every `StorySettings` field is named there), #69 (budget request field, `call_counts`/`budget`/`degradation_details`, `effective_budget`, 503 details documented; deferred results done), #70 (rebuild 202 `RebuildQueuedResponse` declared; delete's 423 `job_running` documented), #71 (`/admin/settings` fields are generated from `DeploymentSettings`; story `audit_retention_days` added to the PATCH description; `/health` shape documented as `HealthResponse`, still exempt), #72 (`include_summary`, `lite_summary_*` settings documented). The PATCH description also now says `default_output_viewpoint` is interpretive (it said "every setting is prospective").
  **Why:** The coordinator's instruction: every note addressed to #73 implemented or explained.
  **Issue:** #73
- **Decision:** `epistrel.__all__` exports `Engine`, `Settings`, `RequestContext`, the request models, the main result types, every `EpistrelError` subclass a caller can branch on, and `ReferenceReplay`/`TurnAccepted`; all lazily imported (`_EXPORTS`), keeping #45's "import epistrel pulls in no database stack".
  **Why:** Discretion (`__all__` contents).
  **Issue:** #73
- **Decision:** Discovered and filed #278 (needs-triage): library *reads* with a NUL in an argument raise `sqlalchemy.exc.DataError` instead of 422; mutations and REST are covered here.
  **Why:** Out of this story's scope (read-method argument validation across the library).
  **Issue:** #73
- **Decision:** Telemetry lives in a new `epistrel.observability` package: `content` (the one content rule: drop `CONTENT_FIELDS` and `*_text` at any depth, cut unknown strings to 64 chars, `principal` exempt to 256), `logging` (structlog JSON, `ContentFilter`, content debug policy), `metrics` (`/metrics`, request histogram, `epistrel_operations_total`, scrape-time gauges), `tracing` (OTLP, content-filtering span processor), `instrument` (Engine wrapper, `background()`, ASGI middleware) and `context` (request-scoped `Scope`). `epistrel.core` imports only `context` and `tracing`; a new import-linter contract keeps `observability` from importing the orchestrator, API or Engine.
  **Why:** Must-have artifacts (logging/metrics/tracing modules); one rule shared by logs and spans so they cannot drift.
  **Issue:** #74
- **Decision:** The planner's `@instrumented` decorator, "folded into #73's `@operation` decorator", is `instrument.instrument_engine(Engine)` applied once at the end of `engine.py`: it wraps every public coroutine method (operations and `library_only` ones; `start_workers`/`stop_workers` excluded because they spawn long-lived tasks). #73 shipped no `@operation` decorator (it marks the exceptions with `@library_only`), so there was nothing to fold into; wrapping by class keeps a new method instrumented by default, like #73's parity rule. Only the outermost call logs/spans/counts; nested `self.*` calls are part of it.
  **Why:** Discretion ("one decorator, `epistrel.observability` owns it"; "one summary line per Engine call").
  **Issue:** #74
- **Decision:** Outcome mapping: `ok`; `rejected` for any error whose `http_status` < 500; `error` otherwise. `TurnAccepted` (202 queued) and `ReferenceReplay` count as `ok`. Jobs: `error` for a failed attempt, `rejected` when the claim was fenced. Queued turns are background units `respond_queued`/`ingest_queued` carrying the requester's principal (not `system`), since they are a player's turn; jobs and the timer pass (`fire_timers`, DEBUG when nothing fired) use `system` and start a new trace.
  **Why:** Discretion (`ok|rejected|error`, `rejected` = 4xx; jobs/timers principal `system`, no parent). The queued-turn principal is my reading: the work is the requester's.
  **Issue:** #74
- **Decision:** Process-wide logging and tracing are configured by `epistrel serve` only: uvicorn now loads `epistrel.cli:app_factory`, which runs `observability.configure_process(settings)` and then `create_app` (a `--reload` child runs it too); uvicorn gets `log_config=None, access_log=False`. `create_app` does not touch the root logger, so an embedder keeps its own. Epistrel's structlog loggers are built with `structlog.wrap_logger` (the ContentFilter runs before any handler), never through `structlog.configure`, so an embedder's structlog configuration is untouched and an unconfigured process cannot print unfiltered content.
  **Why:** A library must not reconfigure its host's logging; keeping the filter inside each Epistrel logger means content never reaches any handler, configured or not. Moving configuration out of `_load` also keeps a refused start (`serve` exit 78) side-effect free.
  **Issue:** #74
- **Decision:** Tracing uses Epistrel's own `TracerProvider` (not the process-global one) when `EPISTREL_OTLP_ENDPOINT` is set, else a no-op tracer; `/v1/traces` is appended to the endpoint unless present; `deployment.environment` is `development` under `EPISTREL_DEBUG`, else `production`, unless `OTEL_RESOURCE_ATTRIBUTES` names one. Model-call spans are recorded retroactively from `metrics.record` (start = end − latency), so every attempt (including failures) gets one without restructuring `ModelAccessLayer.send`. Spans never record exceptions; status is the code only.
  **Why:** Discretion (OTLP when set, always-on sampler, resource attrs, child spans for adapter calls and commits, span status code only). Not touching the global provider avoids clobbering an embedder's tracing.
  **Issue:** #74
- **Decision:** New settings `content_debug_logging` (bool, default false) and `otlp_endpoint` (http(s) URL or unset) are process `Settings` fields (env `EPISTREL_*`), not `core/settings_schema.py` deployment settings, so they carry no `ConfigClass` tag.
  **Why:** Discretion pass 2 ("are `Settings` fields"); they are per-process knobs like #70's worker settings, read before any database exists.
  **Issue:** #74
- **Decision:** Content debug logging: refused unless `EPISTREL_DEBUG` (a `Settings` validator, so `serve` exits 78), WARNING at start-up, every line marked `content_logging: true`, `GET /health` gains `content_logging` (always present, bool; `docs/openapi.json` regenerated), and it switches itself off after 24 h of process time (monotonic). Retention (7 days, operator-only, subject to erasure) is stated in docs/02 §11, `.env.example` and the README.
  **Why:** AC1 + Discretion (exit 78, startup WARNING, `/health.content_logging`, 24 h engine-side bound).
  **Issue:** #74
- **Decision:** `/metrics` is on `public_router` (worker-mode processes serve it, #71's hand-off) with `operation_id="metrics"`, documented in the OpenAPI schema as `text/plain` (schemathesis C-01 now covers it). Default process/platform/GC collectors are unregistered (#44's rule). Gauges are read at scrape through a pool-of-one sync engine bound in the app lifespan (2 s statement timeout; a failing database yields no gauge samples rather than a failed scrape); the route renders in a worker thread.
  **Why:** Discretion (gauges at scrape on a dedicated sync connection; `render_metrics()` for embedders; label rule per #44, which also admits HTTP statuses and sanitised model names, so `test_metrics_labels` uses that grammar rather than the issue's narrower regex).
  **Issue:** #74
- **Decision:** The guard is `tests/conformance/test_c_08.py` (Discretion: "one test file `tests/conformance/test_c_08.py`"; the AC's `tests/test_telemetry_content_guard.py` name is not used). It runs `tests/scenario` + `tests/conformance/test_c_03.py` (every operation over REST and the library) + `tests/conformance/canary_scenario.py` in a **subprocess** (`python -m pytest -p tests.conformance.telemetry_canary`) on a fresh migrated database, rather than `pytest.main` in-process: a fresh process gives a clean metrics registry and logging state and cannot disturb the parent session. Capture is a root handler with Epistrel's own JSON formatter (what `serve` writes) plus an in-memory exporter behind the production span filter, not `structlog.testing.capture_logs` (which bypasses the filter and would see raw content). Besides 12 planted canaries (story name, definition, pins, instructions, say, do, ingest text, stub reply, regenerated reply, fork name, a refused body, an unknown story), the plugin **harvests** every string ≥ 12 chars with a space from every REST request body, staged event payload and stub reply of the run (about 1,000) and asserts none appears in any non-identifier log field, span or metric; identifier fields are format-checked instead. Complement: the canary story re-run with content debug logging on shows the say/reply canaries and harvested content. About 2 minutes in the gate. C-01's schemathesis corpus (#73's suggestion) was not reused: it takes ~2 min on its own and sends random text, while C-03 already covers every operation with realistic content.
  **Why:** Discretion (guard re-runs `tests/scenario`, canaries < 64 chars in input/pins/instructions/definitions/replies, `pg_url` passed as `EPISTREL_TEST_DATABASE_URL`, runs at DEBUG, all loggers, in the PR gate).
  **Issue:** #74
- **Decision (Rule 3):** `tools/gates/licenses.py` looked up a parenthesised classifier (`Mozilla Public License 2.0 (MPL 2.0)`, certifi, pulled in by the OTLP exporter via `requests`) only after declaring every parenthesised value UNKNOWN, although the normalisation table already maps that exact string to `MPL-2.0` (allowed). A whole value found in the table is now that id; other parentheses stay UNKNOWN. Regression test in `tests/gates/test_licenses.py`.
  **Why:** check-licenses blocked the planned dependency; the gate is not weakened (an exact-match lookup in its existing table).
  **Issue:** #74
- **Decision:** Dependencies added by `uv add` (versions resolved by the resolver, not memory): `structlog>=26.1.0` (MIT/Apache-2.0), `opentelemetry-sdk>=1.45.1` and `opentelemetry-exporter-otlp-proto-http>=1.45.1` (Apache-2.0; pull in `protobuf` BSD-3-Clause, `googleapis-common-protos` Apache-2.0, `requests`/`certifi` MPL-2.0). check-deps and check-licenses pass; no collector or broker is a runtime dependency.
  **Why:** Discretion (library choice).
  **Issue:** #74
- **Decision:** CLAUDE.md invariant 3 still reads `guard: #74 (planned)`; per the n8-exec convention it becomes `(merged)` when this guard lands on `main`, so the orchestrator rewrites it after the merge. The guard's docstring states what it does not cover (unreached code paths, third-party loggers below the WARNING caps, content formatted into a message `event` on an unreached path, telemetry added outside Epistrel's handler/exporter).
  **Why:** Brief instruction; AC4's "CLAUDE.md invariant 3 annotation updated (C-08)" completes at merge.
  **Issue:** #74
- **Decision:** Filed #279 (needs-triage): C-01 flakes locally on `GET /admin/config-changes` and `/stories/{id}/config-changes` (`setting` query parameter accepts values that violate its documented `anyOf`); reproduced on f6dfbd4 without this story's changes. CI is derandomised.
  **Why:** Out of scope.
  **Issue:** #74
- **Decision:** The crash-point hook lives in a new stdlib-only module, `epistrel.testing.crash` (`POINTS`, `crash_at(point, nth=1, kind=None) -> CrashPlan`, `hit(point, kind)`, env `EPISTREL_TEST_FAULTS=1` + `EPISTREL_TEST_CRASH_AT=<point>:<nth>[:<kind>]`, `os._exit(137)` after printing `EPISTREL-FAULT crash <point> <kind>`). `epistrel.testing.faults` re-exports `crash_at`, `hit` and `POINTS`, and `EngineProcess` gains `crash=` and `mode=`. The hook is not in `faults` itself because core calls it, and `faults.serve` imports `epistrel.api`: import-linter follows function-level imports, so `core → testing.faults → api` would break `core-does-not-import-orchestrator-or-api`.
  **Why:** AC1 names `epistrel.testing.faults`. Discretion: "`faults.hit()` is a permanent no-op call … `epistrel.testing` ships (tiny)". Keeping the layering contract intact needs the split.
  **Issue:** #75
- **Decision:** Hits are placed as follows. `Operation._write` has `before_lock` at the start of the commit work, `after_events` after the event insert and before `apply_events`, and `before_commit` after the key bind. `projections.apply_events` has `during_projection` between consecutive projections; it gained a keyword-only `kind`. `core.fork` has `before_commit` at the end of the copy transaction and `after_commit_before_response` after it commits, before the source's `fork` event. `Engine._keyed` has `after_commit_before_response` after `run` and before `call.finish`. The queue drainer has `before_lock` before `turn_gate.acquire`. `kind` is the operation kind for `Operation` hits, `fork` for the fork, the Engine method name in `_keyed`, and the queue item's kind in the drainer. With no kind in the plan, every hit of the point counts.
  **Why:** Discretion pass 2: points, "`nth` counted per operation kind", "an unreached point counts as no-kill". X-03 draws plans without a kind (uniform points, `nth` 1..8) because its operations mix kinds.
  **Issue:** #75
- **Decision (Rule 2):** New module `core/orphans.py`. `fail_orphans` runs in the worker's `recovery_pass`, right after the job reap (`RecoveryReport.orphaned`). It first reclaims expired turn slots through `turn_gate`'s own reclaim (now public as `turn_gate.reclaim`). Then it marks `failed` every `in_flight` operation older than `GRACE_S` (60 s, the idempotency lease; the fault child and the X-03/X-06 harness use 1 s) that nothing alive claims. A claim is a live turn slot, a `turn_queue` row, a live in-flight idempotency key (its own, any of its story's, or any deployment key when the op has none) or a running job on its story. Without it, a crashed truncate, swap, genesis or rebuild stayed `in_flight` for ever (#70/#71 had noted this as not swept). Pre-fix guard: X-03 with the sweep disabled failed ("still in flight after 20 s of recovery").
  **Why:** The X-03 predicate (Discretion: "no `in_flight` operations/keys after recovery … cleared by the recovery pass"). No schema change: a status update on existing rows.
  **Issue:** #75
- **Decision (Rule 2):** REST maps SQLAlchemy `OperationalError`/`InterfaceError` to 503 `database_unavailable` (`details.reason: connection`) and disposes the app's pool (`api/errors.database_error_handler`). Every operation now documents it (`openapi._rule_errors`), and `docs/openapi.json` was regenerated. The library still raises the SQLAlchemy error: the mapping is REST-only. Pre-fix guard: X-06 without the handler got 500.
  **Why:** Discretion: "`OperationalError`/`InterfaceError` → 503 `database_unavailable`, pool disposed; auto-reconnect required". `pool_pre_ping` already reconnects once the server is back.
  **Issue:** #75
- **Decision:** X-03 (`tests/chaos/test_x_03.py`) runs on its own migrated scratch DB. Its child engine is in `api` mode with a 1 s lease. Each of `EPISTREL_CHAOS_ITERATIONS` (default 20) iterations draws a plan (20 % none) and sends up to 10 ops (respond/truncate/swap/fork 60/20/10/10; infeasible draws skipped; a 409 `unknown_chain` swap counts as refused). On a crash the harness recovers in-process (`tests/crash_support.recover`, the real `recovery_pass`), restarts without a plan, retries the request under its key and continues. Each iteration ends with a SIGKILL, recovery, a summary-job drain and the predicate on the stories it touched. The predicate is no in-flight op or held slot, dense `seq`, every event's op committed, `next_turn_index` = active head + 1, `timeline_revision` = the highest revision committed since the story's own genesis (a fork's copied ops keep the source's revisions), and projections == `projections.rebuild`. It also checks a head-turn oracle. At the end every answered key must be `done` and bound to a committed op, and no key may have two commits. About 60 s locally.
  **Why:** Discretion (mix, 20 % no-kill, ≈ 20 kills × 10 ops, `api` mode, harness-run recovery and jobs, keyed retry, forks join the pool, own scratch DB). The fork-revision rule is my reading of "counters consistent".
  **Issue:** #75
- **Decision:** X-06 (`tests/chaos/test_x_06.py`) uses a dedicated `PostgresContainer` bound to a fixed free host port, so `docker start` keeps the address. It fails (never skips) when `EPISTREL_TEST_DATABASE_URL` is set. Each outage is a `docker kill` between requests: a respond and a truncate must answer 503 `database_unavailable`, a read 503, and the engine must stay up. Then `docker start`, a read answers within 20 s without an engine restart, and both requests are retried under their keys and answered 200. `EPISTREL_CHAOS_DISRUPTIONS` is 1 in PR CI and 3 weekly. About 5 s locally.
  **Why:** Discretion ("dedicated container", "docker SDK kill/start, between requests, 20 s recovery bound, interrupted requests retried", "X-06 runs on PRs with 1 disruption"). The disruption count is a separate env var rather than derived from the iteration count.
  **Issue:** #75
- **Decision:** DETERMINISM. The comparator is `epistrel.testing.determinism` (`state`, `Normalizer`, `differences`, `ORDER`, `OPERATIONAL_TABLES`, `VOLATILE_KEYS`). It compares every table with a `story_id` plus `stories`, `deployment_state` and story-scoped `config_changes`, minus the operational six, each read in a natural-key `ORDER BY` built from story creation order, seq, turn and ordinal, never from an id. An unknown story table raises, so a new table must be given an order. ULIDs become `«n»` (first met), timestamps (columns and ISO strings) become `«ts»`, `*_hash` keys are dropped, whole SHA-256 hex digests become `«hash»` (the `read_set` hashes digest id-bearing content and were the only state that differed), and `elapsed_ms`/`latency_ms`/`worker_id`/`attempt` are dropped anywhere. The script is `tests/determinism/script.json`: 60 steps covering two stories forked twice, regenerate, truncate, swap, pins, instructions and drains, with a small Lite window so summaries are written. It replays in-process over REST on two scratch DBs, and response bodies are compared too. The complement is a third replay with `stub.nondeterministic = True` (the stub's per-call salt), which must differ in bodies and state; I used that instead of a `time.time()`-seeded stub. About 8 s.
  **Why:** Discretion (comparator module, committed JSON replayed in-process, two scratch DBs, bodies compared, permanent nondeterministic-stub test, hash columns excluded, natural-key order). The existing `epistrel.testing.normalize` (#67) keeps its `<id:n>` form for the fork tests; the determinism normaliser uses `«n»` as planned and adds timestamps, digests and volatile keys.
  **Issue:** #75
- **Decision:** Markers and CI. Everything under `tests/chaos/` is auto-marked `chaos`, and `tests/chaos/` and `tests/determinism/` are auto-marked `integration`. A new `chaos_quick` marker goes on X-03, X-04, X-05, X-06 and all of X-07. `ci.yml`'s test job runs `-m "not live_model and (not chaos or chaos_quick)"`, so the quick chaos tests stay inside the one core-coverage gate rather than in a separate `chaos` job: a second job would split the coverage gate, and the orphan sweep's coverage comes from X-03. The new `.github/workflows/chaos.yml` runs on `cron '0 4 * * 1'` and `workflow_dispatch` (inputs `seed`, `iterations` default 500) with `EPISTREL_CHAOS_DISRUPTIONS=3`, a 60 min timeout, `issues: write` on the job only, the step summary (seeds and tallies), the log as an artifact, and on red a comment on the open "Weekly chaos run failed" issue or a new one labelled `bug`/`needs-triage`. Actions were resolved by lookup: `actions/checkout@v7` (v7.0.1), `actions/upload-artifact@v7` (v7.0.2) and `astral-sh/setup-uv@v10.3.0` (exact, no v10 major tag). uv stays at ci.yml's `0.12.23` (latest is 0.13.0) so the chaos run uses the gate's toolchain. The grep gate printed nothing.
  **Why:** AC5 + Discretion. Deviation: "separate `chaos` CI job" became a marker expression in the existing test job, for the reason above.
  **Issue:** #75
- **Decision (Rule 1, test robustness):** `tests/integration/test_conc_linear.py::test_conc_linear` (#47) gained a rule, `turn_over_an_outside_change`. On one draw in eight it begins a turn, makes a truth change outside the turn's read set, and releases the turn: a guaranteed `rebase`. Before, rebase only came from a chance interleaving (2 of 115 releases). Its derandomised draw stream shifted with this story's changes when `tests/eval` was collected before it, and then no rebase was drawn and the outcome-coverage assertion failed. That happened deterministically in every local full run, and baseline 023d52a passed the same subset. The run now draws commit 76, rebase 93, regenerate 47 and abandon 58, the same with or without `tests/eval`. No assertion was relaxed.
  **Why:** The gate must pass with this story's changes. The property and its coverage assertion are unchanged; only the rebase interleaving no longer depends on luck.
  **Issue:** #75
- **Decision:** Filed #280 (needs-triage, bug): a content-rating `display_order` outside int32 gives 500 `DataError` instead of 422. Locally, randomised C-01 hit it on `POST /admin/content-ratings` and `PUT /admin/content-ratings/{rating_id}`. It is pre-existing and not touched by this story. #279 (C-01 `setting`) also hit once and passed on re-run.
  **Why:** Out of scope.
  **Issue:** #75
- **Decision:** Engine measurements for P-04. A new `observability/stages.py` holds `epistrel_turn_stage_seconds{stage}`, labelled by stage only, with stages `queue_wait, front_to_first_call, assemble, generate, policy_check, commit`, plus `post_commit`. Its buckets are 1-2-5 log-spaced from 1 ms to 60 s plus 250 ms, so NFR-PERF-3's 250 ms and the 1 s front-to-first-call bound are bucket edges and the gate reads them exactly. `Operation.commit()` observes `commit` for a committed `turn` operation: its transaction, the atomic commit NFR-PERF-3 bounds. It observes `post_commit` separately for what follows before the turn answers (the summary port's scheduling). The Lite pipeline's `TurnTiming` observes the other stages once per committed turn. `assemble` is the read phase plus prompt assembly. `generate` is the generation calls' recorded latency. `policy_check` is the input gate's wall time plus the output gate's wall time minus the drafts it produced. `front_to_first_call` runs from `CallLog.first_started` (set by `ModelAccessLayer.send` via `metrics.call_started()`) back to the turn's front. `queue_wait` is `clock_timestamp() - operations.started_at` on the database's clock when the drainer starts the queued turn. `epistrel_turn_queue_depth{story_id}` is read at scrape and counts turn-queue rows whose operation is still `queued`, so the running one is excluded. Respond/regenerate results, `operations.turn_report` and `GET /operations/{id}` gain `queued_ms` (null when the turn was never queued) and `first_call_offset_ms`. `docs/openapi.json` is regenerated.
  **Why:** Discretion (pass 1/2): stage histograms without `story_id`, the per-story gauge, the `queued_ms`/`first_call_offset_ms` fields, `elapsed_ms` starting at the queue front (already so in `run_queued`), and the bucket range. "Reaching the front" is read as the drainer starting the turn: it takes the head as soon as the slot frees.
  **Issue:** #76
- **Decision:** `EPISTREL_STUB_LATENCY_S` (#51) is unchanged and still applies to every stub call, not only generation. The reference stories use the seeded Teen rating, which has no policies, so a turn makes exactly one call, a `generation`, and the turn path sees the latency on generation only, as planned. The background `lite_summary` calls also take 2 s, which only delays summaries.
  **Why:** Making the stub's latency purpose-specific would change existing tests (P-09, structured output) that set `latency_s` for other purposes. The turn path is the same either way.
  **Issue:** #76
- **Decision:** Seeding lives in `epistrel.testing.perf`, shared by P-03 and `perf/lite_smoke/seed.py`. Each story is created and pinned through the `Engine` (cast Wren and Ada, Teen, `turn_concurrency=queue`, `turn_queue_max=10`, `default_output_viewpoint` = the player, 50 pins of about 200 characters). Then 10,000 real `turn` operations, each with two beats of about 300 characters of deterministic filler, commit through `OperationBatch` 250 per transaction, with the summary port muted by a re-entrant `summaries_muted()`. `drain_summaries` then runs the story's `lite_summary` jobs in-process on the zero-latency stub until no work remains. Seeding one 10,000-turn story takes about 3 minutes locally, because each turn's chain check reads every earlier turn (#281).
  **Why:** Discretion: "bulk `Operation` seeding, then drain summaries", "messages ~300 chars; pins ~200 chars", "cast 1+1, Teen, queue at creation", "fixture reuses `seed.py` from `epistrel.testing.perf`". Real operations keep every projection right, which a raw SQL bulk insert would not.
  **Issue:** #76
- **Decision (deviation):** P-03 has two tests. `test_p_03` (`perf_long`) is the AC's measurement: a 20,000-event story, 50 warm-up rounds and 1,000 timed commits. It takes about 5 minutes (3 to seed, 2 for the rounds), so it is deselected unless `-m` names `perf_long` (conftest's opt-in list, beside `live_model`). The nightly `perf.yml` runs it. `test_p_03__quick` (`perf_quick`) runs the same commit on a 2,000-event story, 20 warm-up rounds and 200 timed ones, in about 30 s. PR CI runs it. Both gate p95 ≤ 250 ms and fail on any single commit of 2 s or more. Each round's setup is untimed: it takes the turn slot, claims an `Idempotency-Key`, runs the Lite read-set read phase and stages two beats, a turn-report commit step and a result built in the commit. The timed span is `Operation.commit(decide=…)` under the key. The summary port is muted during the timed commit (`summaries_muted()`): its scheduling runs after the transaction, so it is not part of the atomic commit, and P-04 reports it as `post_commit`. Measured locally: 20,000 events p50 31 / p95 34 / max 94 ms; 2,000 events p50 17 / p95 21 / max 26 ms. Before the mute, the 20,000-event figures were p50 70 / p95 76 ms.
  **Why:** The AC says P-03 "is gated in CI (`perf_quick` marker, nightly workflow `perf.yml`)", and the orchestrator asked to keep the PR gate's perf cost small. A 5-minute test in the PR gate, and in every local full run, is not small. The thresholds stay the requirement's own: 250 ms is about 7× the p95 seen at 20,000 events and 12× at 2,000. 2 s is under the commit's 5 s `lock_timeout` and twenty times the slowest commit seen. Neither depends on the machine's speed within an order of magnitude.
  **Issue:** #76
- **Decision:** pytest-benchmark (`pytest-benchmark>=5.3.0`, resolved by `uv add`) goes in the **dev** group, not the `perf` group. P-03 is a synchronous test that drives its own `asyncio.Runner`, because `benchmark.pedantic` calls its target synchronously. The setup is `setup=` (untimed) and each round runs `runner.run(op.commit())`. The gate reads `benchmark.stats.stats.data`. `--benchmark-json` in `perf.yml` uploads the timings, and P-03 also writes `build/perf/p_03_<events>_events.json`.
  **Why:** Discretion said "`pytest-benchmark` in the `perf` group, installed in the nightly job", but `perf_quick` runs in PR CI, which installs only the dev group. The `benchmark` fixture must exist there. pytest-benchmark is BSD-2 and only a test dependency.
  **Issue:** #76
- **Decision:** Markers. `perf_quick` covers the performance tests PR CI runs. `perf_long` covers long performance tests, deselected by default (conftest `OPT_IN_MARKERS`, as with `live_model`). `ci.yml`'s expression is extended, not replaced, to `-m "not live_model and (not chaos or chaos_quick) and (not perf_long or perf_quick)"`, and `test_workflows` pins it. P-04's Locust run is the CLI `python -m perf.lite_smoke`, not a pytest test. `test_p_04` itself is unmarked and runs in PR CI: it asserts that a full-length report (30 minutes, 10,000-turn stories) newer than 30 days is committed, with its host class, verdict and JSON sidecar. Local deselection count is now 3 (was 2).
  **Why:** Discretion: "markers `perf_quick`/`perf_long`", "`test_p_04` requires a report newer than 30 days with a host class; runs in PR CI". It also says "`test_p_04` is the Locust run; marked `perf_long`". A 30-minute compose-stack run cannot be a test in any session, so the run stays the CLI and the marker went to P-03's long test.
  **Issue:** #76
- **Decision:** `.github/workflows/perf.yml` runs nightly at `cron '0 3 * * *'` and on `workflow_dispatch`, with `pytest tests/perf/test_p_03.py -m "perf_quick or perf_long" --benchmark-json=…`. It follows `chaos.yml`'s pattern: `contents: read` at the top, `issues: write` on the job only, a step summary, the `build/perf/` artifact, and on red a comment on the open "Nightly perf run failed" issue or a new one labelled `bug`/`needs-triage`. Actions were resolved by lookup on 2026-10-11: `actions/checkout` v7.0.1 → `@v7`, `actions/upload-artifact` v7.0.2 → `@v7`, `astral-sh/setup-uv` v10.3.0 → `@v10.3.0` (exact; its manifest's `version` input defaults to empty, so uv is pinned to the gate's `0.12.23`). The grep gate printed nothing. `dependabot.yml` already lists `github-actions`.
  **Why:** Discretion: "`perf.yml` nightly 03:00, dispatch, ubuntu + testcontainers, red run auto-opens an issue", plus #75's hand-off.
  **Issue:** #76
- **Decision:** `perf/` is a top-level package (`perf/lite_smoke/{__init__,__main__,seed,locustfile,sample,report}.py`, `compose.perf.yml`), added to mypy's `files` and ruff's `src`. Locust (`locust>=2.46.7`, MIT; pulls gevent, flask, pyzmq) is in a new `perf` dependency group, and mypy ignores missing `locust`/`gevent` imports, so the gate passes without the group. The orchestrator works as follows:
  - Compose project `epistrel-perf`, starting from `down -v`, with an override for stub, 2 s latency, ports 8100 and 5442, and image `epistrel:perf`.
  - Seed in-process, or `pg_restore` the cached `build/perf/cache/lite-<alembic head>-<seed hash>-<git sha>.dump`, then start the engine.
  - Diff a `/metrics` snapshot at start and end.
  - Run Locust headless in a subprocess, because gevent monkey-patching must not meet the asyncio seeder.
  - A 5 s sampler thread reads `pg_stat_activity`, `docker stats` and the queue gauge.
  - Drain the queues, reconcile `history` by the `[tick k]` embedded in `say`, write `perf/results/<YYYY-MM-DD-HHMM>-lite.md` plus a `.json` sidecar (`build/perf/results/` when shorter than 30 minutes), delete the stories, then `down -v`.
  - Exit 1 only for `--host-class reference` with FAIL.
  - `--turns`/`--stories` exist for demo runs; the report states the size.
  
  The driver behaves as follows:
  - One user per story; story i's ticks are offset by i × 15 s / 10.
  - Each submission runs in its own greenlet, with `Prefer: respond-async`, the key `<story>-<tick>`, and `GET /operations/{id}?viewpoint=…` polled every 500 ms.
  - A 423 or any non-2xx counts as a failure.
  - Submissions stop at T−2 min.

  The gate is computed as follows:
  - Commit p95 is the share of server `commit` observations ≤ 250 ms (≥ 95 %).
  - "≤ 1 waiting" is the driver's outstanding turns minus the running one, at every submission after 5 min.
  - Queue wait uses `queued_ms` ≤ 15,000; front-to-first-call p95 uses `first_call_offset_ms`.
  - Engine-only turn time is `elapsed_ms − Σ model_calls.latency_ms`, reported only.
  **Why:** Discretion items (Locust over k6, `Prefer: respond-async` + polling, 500 ms poll, 423 counts as failure, tick in `say`, key `<story>-<tick>`, stop at T−2 min, metrics snapshot diffed, `pg_stat_activity` + `docker stats` every 5 s, `epistrel-perf` compose project with an override file, pg_dump key, `EPISTREL_PERF_*` env, per-minute table + JSON sidecar, `--host-class mac|reference|ci`, exit 0 on mac, demo runs to scratch, stories deleted afterwards). The stagger of story offsets is my choice, so ten stories do not hit the engine in the same second.
  **Issue:** #76
- **Decision:** Filed #281 (needs-triage): every turn commit's FR-STORE-28 chain check (`Operation._check_chain`) reads every active turn of the story, so commit cost grows linearly. The figures are p95 24 ms at 2,000 events and 76 ms at 20,000; extrapolated to about 350 ms at NFR-SCALE-1's ~100k events. NFR-PERF-3 holds at the reference size. I did not fix it in #76.
  **Why:** Out of scope: #76 measures. The fix is a design choice for the chain invariant.
  **Issue:** #76
- **Decision (deviation, finding):** The first full P-04 run on this Mac (10 × 20,000-event stories with summaries, 30 minutes) reported **FAIL**. Every per-story service criterion and every count passed, but only 75.9 % of 1,121 commits were ≤ 250 ms. At that time the `commit` stage included the post-commit summary scheduling. Profiling one reference story sequentially gave a whole `commit()` of p50 176 / p95 226 ms, of which `summary_port.notify` (`core.summary.needs_work`) was p50 125 / p95 177 ms and the chain check p50 13 ms. NFR-PERF-3 bounds "an atomic turn commit", and the planner's P-03 span names the transaction, projections, done-row and lease clear, not the scheduling. So `commit` is now the transaction and the scheduling is its own `post_commit` stage, reported in every P-04 report. Filed #282 (needs-triage) for the scheduling cost and #281 for the chain check. The first run's report is not committed; it is kept locally in `build/perf/results/2026-10-11-1942-lite.md`. A second run restored the cached seed and passed, but the first commit of every story took about 15 s: `pg_restore` leaves no planner statistics. Its report is also kept locally (`build/perf/results/2026-10-11-2112-lite.md`). The smoke now runs `vacuumdb --analyze-only` after a restore (a Rule 1 fix to this story's own tool). The committed report, `perf/results/2026-10-11-2143-lite.md`, is the third run: **PASS** on host class mac. It shows 1,121/1,121 turns, 0 failed, lost or duplicated; 99.8 % of commits ≤ 250 ms (p95 bucket ≤ 200 ms); `post_commit` p95 ≤ 250 ms; front-to-first-call p95 446 ms; engine-only p50/p95/p99 442/482/555 ms; and every story PASS.
  **Why:** The engine-only turn time still shows the cost (≈ 390 ms p50 in the first run), so nothing is hidden. Gating the scheduling as part of the "atomic commit" would misname it. Fixing the scheduling is #72's area and a design choice, not this story's.
  **Issue:** #76
- **Decision:** The committed report's engine line reads `187ea42 + uncommitted changes`: the run used this story's working tree, which is then committed unchanged as the story's commit (plus the report). Seeding the ten reference stories cold took 3,139 s, including 195 summary runs per story; the cached dump restores in seconds.
  **Why:** The report must say what code ran. Running after the commit would need a second commit for the report, and the brief asks for one commit per story.
  **Issue:** #76
- **Decision:** The deployment smoke is `tests/smoke/` (`test_c_10__<slice>` × 11: health ok with a live worker, `/admin` needs the credential, respond through the fake, ingest + replay, truncate, fork, rebuild, `/metrics`, `docker compose` services = {engine, db}, losing `db` → 503 `database_unavailable` then recovery, and an orphaned `in_flight` operation failed by the start-up recovery pass after `docker compose restart engine`). The last two are #75's hand-off ("losing the DB is 503", "start-up recovery fails orphaned operations"); the orphan is planted with `psql` in the `db` container (values bound as psql variables), because a real crash cannot be timed from outside the container. `smoke` is an opt-in marker beside `live_model`/`perf_long` (conftest `OPT_IN_MARKERS`; everything under `tests/smoke/` is auto-marked), so `ci.yml`'s `-m` expression is unchanged and the local full run now reports 14 deselected (3 + 11). Settings are read at import because the root conftest strips `EPISTREL_*` per test; a missing `EPISTREL_SMOKE_BASE_URL` fails the session fixture, never skips.
  **Why:** Discretion items (fake on FastAPI/uvicorn, `0.0.0.0:1234`, `fake-1`, "The fake model says hello.", `/_calls`, `{violation:false}` on `response_format`, `PUT …/default` + `…/api-key`, adult-only cast, dev credential fallback, readiness 120 s with `alive ≥ 1`, compose via subprocess `-p epistrel`). `EPISTREL_SMOKE_FAKE_PORT`/`_MODEL_HOST`/`_COMPOSE_PROJECT` are my additions: port 1234 is taken on this Mac by an unrelated container, and LM Studio uses it on most developer machines.
  **Issue:** #77
- **Decision:** `publish-edge.yml` and `release.yml` gain an inline `smoke` job (`needs: ci`; `publish` needs it). It builds amd64 with `load: true` as `epistrel:dev`, reading the GHA cache scopes `publish` and `smoke` and writing only `smoke` (writing `publish` from an amd64-only build would replace the multi-arch cache), then `compose up -d --no-build --wait --wait-timeout 180`, `uv run pytest -m smoke tests/smoke`, compose logs as the `smoke-compose-logs` artifact on failure, `down -v` always. In publish-edge the suite comes from the workflow's commit and the image from a second checkout of the resolved ref (`path: image-src`) instead of a `repo#sha` git build context: same effect (current main's smoke against the old image on a rollback dispatch), and it needs no token for a private repo. Actions resolved 2026-10-11: checkout v7.0.1 → `@v7`, upload-artifact v7.0.2 → `@v7`, setup-buildx v4.4.1 → `@v4`, build-push v7.4.0 → `@v7`, setup-uv v10.3.0 (exact; no floating major). Untouched `setup-uv@v10.2.0` lines elsewhere are left to Dependabot. Grep gate printed nothing. Consequence: a rollback dispatch to a commit older than #75 fails the smoke (it expects 503 on database loss), which is the intent ("edge is never published from an image that cannot pass its own smoke").
  **Why:** Discretion items (inline job, amd64 only, `epistrel:dev` + `--no-build`, shared cache, rollback runs main's smoke, release workflow too, logs on failure, `down -v`).
  **Issue:** #77
- **Decision:** C-11 is `tests/test_c_11.py` (`integration`, in the PR gate): the five CLI gates each exit 1 on a fixture violation (M1 fixtures `unassigned.md`, `tests_bad_milestone`, `locks/redis_runtime.lock`; for lexicon and traceability the repository's own parts plus one planted M2 part using an M5 term under an id §15 does not list) and then exit 0 on the repository. `check-licenses` judges a new fixture, `tests/gates/fixtures/licenses/gpl_runtime.json`, through `evaluate()` over the real `uv.lock` closure and policy, then runs for real against `EPISTREL_RUNTIME_ENV` (default `.venv-runtime`, `--no-sync` when it exists, synced otherwise). It also parses `ci.yml` (every gate an `if: always()` step of `lint`, nothing `continue-on-error`, coverage ≥ 90 on core, `gate` needs every job) and both publishing workflows (`publish` needs `ci` and `smoke`). Guard checked against the pre-fix state: the workflow test failed on both files before the smoke job existed.
  **Why:** Discretion items (all six gates, one violation each, license via `evaluate()` on fixture JSON, `ci.yml` parsed for wiring, runtime env handling, `integration` marker). Part markers sit on each function: `check-parts` refuses module-level `pytestmark` parts.
  **Issue:** #77
- **Decision:** README's Run section documents the one-endpoint requirement with an LM Studio `curl` walk-through (configure `default` + key, create story, respond, `docker compose ps`), and the test section documents `pytest -m smoke`, C-11 and #76's hand-off that `test_p_04` fails once the newest committed P-04 report is over 30 days old (first report 2026-10-11, so ~2026-11-10). RELEASING.md says every publish now passes the smoke. Filed #283 (needs-triage) as the reminder for the P-04 refresh.
  **Why:** AC4 and the #76 hand-off; the expiry would otherwise surface as an unexplained red PR gate.
  **Issue:** #77
- **Decision (orchestrator):** CLAUDE.md's guard annotations now read `guard: #74 (merged)` (invariant 3) and `guard: #44 (merged)` (invariant 4), committed on the milestone branch so they land on `main` with the guard code in the milestone PR.
  **Why:** n8-exec rewrites `(planned)` to `(merged)` when the guard lands; landing both in the same squash keeps the annotation true at every commit on `main`.
  **Issue:** #74, #44
- **Decision (Rule 1, orchestrator, PR #284 CI attempt 1):** CI's C-08 guard re-runs the scenario suite in a subprocess, and there `test_s_double_submit` (c) got 423 `story_busy` on the retry after the child engine was killed. The crashed attempt's idempotency claim and its turn slot both have `lease_s` (1 s) leases, started at different moments; a retry landing between the two expiries meets the still-leased slot. The test now waits `2·lease_s + 0.5` s before the retry. The assertions are unchanged; the product's 423 is the documented #63 behaviour for a slot still leased.
  **Why:** A timing race in the test, seen only on the slower CI runner under C-08's extra load; the same test passed in CI's main run.
  **Issue:** #61, #74

## /n8-exec M2 (verify fixes) — 2026-10-11

Re-execution of M2 after `/n8-verify M2` filed #285–#297 against PR #284. Branch `milestone/m2-verify-fixes` (a new name, because `milestone/m2-lite-core` was deleted by #284's merge).

- **Decision (#295):** Background job runs are reported as `committed` → `ok`, `refused` (commit fenced) and `dropped` (stale inputs, requeued) → `rejected`, a failed attempt (`queued`/`failed`) → `error`, and a fenced claim → `rejected`. This goes through a new pure helper, `worker.run_outcome`.
  **Why:** docs/02 fixes the outcome vocabulary at `ok`/`rejected`/`error`, and the docstring already said "rejected when fenced". New labels would have changed a documented metric contract. A dropped output is a designed concurrency outcome, not an error.
  **Issue:** #295
- **Decision (#290, Rule 1):** Regex policies now run in a pool of up to `REGEX_WORKERS` child processes (`python -I regex_worker.py`, stdlib only, JSON lines over pipes) instead of an `InterpreterPoolExecutor`. A search that misses its deadline is killed and reaped. Idle workers are reused and exit on stdin EOF, and an `atexit` hook kills any that remain. The contract is unchanged: the deadline starts once a worker is ready, start-up stays under `STARTUP_GRACE_S`, and a timeout is a violation (fail closed).
  **Why:** A subinterpreter cannot be interrupted, so the abandoned search kept a core busy and blocked exit. Of the issue's two suggestions, the `regex` package's timeout would be a new runtime dependency and a library swap (Rule 4 territory). A killable stdlib process is neither. Mutation-checked: the new exit test times out on the old code.
  **Issue:** #290
- **Decision (#287):** Fixed P-08's assertion to 2 `guardrail_check` calls per minors turn and verified it against `tests.model_support.mock_openai`. Filed the related sev:low (model-matrix.md rows measured on the pre-respond path; tier label) as #298 (`needs-triage`) instead of re-measuring the matrix here.
  **Why:** The re-measurement is a docs/report task with its own AC (#54 AC3), not part of the test fix.
  **Issue:** #287, #298
- **Decision (#291):** The 409 `rebase_failed` test is a new integration module (`test_config_rebase_conflict.py`) driven through `respond` with a held stub generation and a global output policy added mid-turn. Its complement shows that budget 1 regenerates and commits.
  **Why:** `test_config_revalidation.py` is a Hypothesis state machine that tolerates the conflict by design. A deterministic case reads better on its own.
  **Issue:** #291
- **Decision (#292):** The FR-PROF-14 strategy is derived from `StorySettings.model_fields`. Bounded int/float fields come from their `ge`/`gt`/`le` metadata. Three fields keep explicit strategies (viewpoint from the cast, the `turn_concurrency` literal, nested `generation_settings`). A field that cannot be derived fails the test by name, and every draw is asserted to cover all fields.
  **Issue:** #292
- **Decision (#297):** `perf/lite_smoke/__main__.main`'s inline exit decision moved to `report.exit_code(evaluation)` (same expression), so the test asserts the real decision without a 30-minute run.
  **Issue:** #297
- **Decision (#296):** `_forkable` (I-04) now creates its source with a new, non-default rating, so every fork test runs against a rating the default fallback can't fake.
  **Issue:** #296
- **Decision (#293):** Closed 10 of the 11 planned-but-absent §15 parts and FR-STORE-26's coverage. Where an existing test already proved the requirement, only the marker changed (I-22 `__prompt` + FR-LITE-1; I-04 `__fork` + FR-STORE-14; `__fork_isolation` + FR-STORE-14..16, with a new assertion that the deleted source keeps no rows). New tests: `test_i_04__regenerate`, `test_time__derived` (FR-STORE-26), `test_time__source_window` (FR-STORE-27), the ISOLATION property test, and `test_i_08__round_trip` / `__rebuild`.
  **Why:** The markers and §15 IDs must agree, and a marker is added only where the test's assertions already cover the requirement's text.
  **Issue:** #293
- **Decision (#293, ISOLATION):** The fork's `story_created` (names its source) and the source's `fork` event (names the fork) are excluded from the "no row names another story" check.
  **Why:** Both are #67's designed provenance, and I-04 already excludes the first. Read strictly, FR-STORE-14 ("no … event shall reference another story") would forbid both. Raise it with `/n8-audit` or a spec note if provenance should not count as a reference.
  **Issue:** #293
- **Decision (#293, I-08):** The schema fingerprint compares function bodies with whitespace collapsed and allows `operation_status.queued` to remain after a downgrade below 0015.
  **Why:** The round trip found two differences at revision 0002. (1) 0014/0015/0016's downgrades restore `epistrel_operations_guard` with its allow-list on one line rather than 0002's three: semantically identical, diffed and confirmed. (2) 0015 documents that its downgrade leaves the `queued` enum label, because PostgreSQL cannot drop one. Both are allowed explicitly in `KEPT_ON_DOWNGRADE` and the normalisation, not silently.
  **Issue:** #293
- **Blocker (#293):** FR-MODEL-9 (S, bring-your-own-completion) is OUT-OF-TIER in M2's plan but planned as `C-04 (M2)` in docs/04 §15, so `check-traceability` fails at `current_milestone = 2`. The options are on the issue: build it, amend §15 (recommended), or change the gate rule. `current_milestone` stays 1. #293 is `blocked` + `needs-owner-action`.
  **Issue:** #293
- **Finding (#279, not fixed):** On this branch C-01 `GET /admin/config-changes` fails deterministically under `CI=1`, while clean `main` passes. #290's change shifts which examples the derandomized run draws, and the branch draws an object-valued `setting` that serializes into valid query strings. I wrongly assumed invalid UTF-8 at first, then traced it and reverted that attempt. It's a harness/schema mismatch for which #279 already poses the options. I did not weaken the check. CI decides.
  **Issue:** #279
- **Blocker (merge of PR #299):** CI run 38193067001 was red on `test_c_01[GET /admin/config-changes]` only (#279); 853 passed. Both fixes are owner calls: tighten `setting` to an enum of known setting names (an API contract change, recommended) or narrow C-01's negative-data check for nullable string-array query parameters (weakening a gate). The PR is left open and unmerged, and #279 is labelled `needs-owner-action`.
  **Issue:** #279, PR #299

## /n8-exec M2 (owner decisions on #279, #293) — 2026-10-11

- **Owner decision (#279):** Tighten the contract. The owner chose option 1.
  **Applied:** the `setting` filter on both config-change listings is now a closed vocabulary per scope: a setting name (each leaf path and each dotted parent from `flatten`), or a row family that scope writes (`<family>:` alone, or `<family>:<member>`). The deployment scope's families are `model_config`, `policy` and `rating`; the story scope's family is `policy`. Anything else is 422 `validation_error` at `query.setting`. The OpenAPI schema documents each item as `anyOf: [enum, pattern]`, and `docs/openapi.json` was regenerated.
  **Deviation from the literal option ("an enum of the known setting names"):** a pure enum would have broken the documented family filters (`rating:`, `policy:<id>`, `model_config:<purpose>.<field>`), which are open-ended ids. The pattern covers exactly the families each scope writes, so every name that can be recorded stays filterable, and unknown names are now refused.
  **Consumers:** Epistrel-Console does not call config-changes. Its recorded `openapi.json` picks this up on its next re-record.
  **Issue:** #279
- **Owner decision (#293):** Amend §15 so that FR-MODEL-9 is not release-gated. The owner chose the "—" option.
  **Applied:** docs/04 §15 row `FR-MODEL-9 | S | M2 | —`. The §15 footer now lists `FR-MODEL-9 (S)` as not release-gated. C-04's heading and M2 tag drop FR-MODEL-9 (`FR-MODEL-1..8,10..13`), and its prose says the BYOC path joins the suite when a milestone builds it. §22 still assigns FR-MODEL-9 to M2 (invariant 7 holds). `current_milestone = 2`, and all six gates exit 0.
  **Issue:** #293
- **Decision (#293, Rule 3):** Two gate self-tests encoded the M1 state. `test_parts::test_real_tests_pass_today` asserted "0 requirements gated" and now asserts a non-zero gated count at the current milestone. `test_parts_json` ran fixture tests against the real milestone and now pins `fixtures/pyproject_m1.toml`, since it tests the JSON shape, not coverage.
  **Issue:** #293
- **Decision (#279, Rule 1, test harness):** C-01 shares one scratch database across all operations. A generated, valid `PUT /admin/model-config/generation` (a `base_url` or model) moved generation off the stub, so a later `POST /regenerate` answered a documented 503 `model_unavailable`, which `not_a_server_error` flags. This draw surfaced once the schema change shifted the derandomized examples. The harness now undoes each successful generated write through `DELETE /admin/model-config/generation`, the same reasoning as the excluded `delete_story`/`archive_story`. No check was removed and the operation is still generated. A logged run shows 23 restores.
  **Issue:** #279

## /n8-exec M2 (re-verify fixes) — 2026-10-12

Re-execution of M2 for the two bugs `/n8-verify M2` left open after PR #299 (#289 reopened, #300 new). Branch `milestone/m2-reverify-fixes` (`milestone/m2-verify-fixes` already carries #299's squashed history).

- **Decision (#289):** The actor-conflict case uses its own key (`i14-actor`). Its first request (as Wren) pins `output_viewpoint=character:<Brann>` and `render_character_ids=[Brann]`, and the conflicting request repeats it as Ada with the same pins. The case does not reuse `i14-key`.
  **Why:** `i14-key`'s original request leaves the viewpoint and render set to be derived, so reusing that key with pins would change three hashed fields. A fresh key makes the actor the only difference. Mutation-checked: with the actor removed from both places in the hash material (the body dump and the separate key), the second request gets 200 with Wren's reply and the test fails.
  **Issue:** #289
- **Decision (#300):** The in-process test spies on `_RegexPool._discard` (wrapping the real method) to record discarded worker handles. The subprocess test uses `start_new_session=True` and `os.killpg(pid, 0)` to show that no worker outlives the child, and it is skipped on Windows.
  **Why:** A process group is the only handle on a re-parented orphan that does not depend on matching the command line. The spy observes exactly the workers the pool gives up on.
  **Finding:** Of the two mutants, `_discard` → `pass` (worker kept tracked, never killed) is caught only by the in-process test, because the child's `atexit` hook kills tracked workers. Removing only `worker.kill()` (the issue's repro: untracked, left running, 6 orphans) fails both tests.
  **Issue:** #300

## /n8-exec M2 (#280, adopted by re-verify) — 2026-10-12

Re-execution of M2 for #280, which `/n8-verify M2` at 9361eb1 adopted into M2 (`confirmed`, `sev:high`). Branch `milestone/m2-rating-order-bounds`.

- **Decision (#280):** `display_order` is bounded to the full int32 range of the `INTEGER` column (`DISPLAY_ORDER_MIN = -2**31`, `DISPLAY_ORDER_MAX = 2**31 - 1`, a shared `DisplayOrder` annotated type) on both `RatingFields` and `RatingReplacement`. Out-of-range values are 422 `validation_error` at `body.display_order`.
  **Why:** The issue's fix shape names int32. A smaller "documented" range would have refused values the API accepts today, which is a contract narrowing with no need behind it. Negative orders stay legal because they always were.
  **Issue:** #280
- **Decision (#280, Rule 1):** The order assigned when `display_order` is omitted, `max(live) + DISPLAY_ORDER_STEP`, is clamped to `DISPLAY_ORDER_MAX`. Without the clamp, a create that omits the field after a live rating at the maximum reached Postgres and returned the same 500. With it, the new rating ties at the maximum, and the documented tiebreak orders it by name, then id.
  **Why:** This is the same `DataError` 500 from the same route, reachable only through values the new bound still allows, so fixing the bound alone would have left #73 AC2 false. A 422 was not an option, because the caller sent no `display_order` to blame. Mutation-checked: without the clamp, the new test fails with `integer out of range`.
  **Issue:** #280
- **Decision (#280):** The C-01 cases go in `test_c_01__malformed` (POST `2**31`, PUT on the seeded Everyone rating with `-2**31 - 1`). The path template gained `{rating}`. The boundary and clamp test runs on a scratch database.
  **Why:** That is the issue's fix shape. The scratch database matters because a rating left at the int32 maximum in the shared session database would change every later default order, and `test_display_order_default_and_listing` asserts `+10`.
  **Issue:** #280

## Ad-hoc — 2026-10-12

- **Change:** For M3 only (as a trial), `/n8-exec` runs the test gate per group of stories instead of per story. M3's 30 work items (26 stories + bugs #93, #94, #303, #305; the 6 epics carry no code) are split into three groups that each depend only on earlier groups: **A** = #305 (moved from M2 by the owner, first in A), #93, #94, #303, #103, #104, #105, #106, #107, #108, #109; **B** = #110, #111, #112, #113, #114, #115, #116, #117, #118, #119; **C** = #120, #121, #122, #123, #124, #125, #126, #127, #128. For each story: its own new tests, plus the existing tests for the areas it touched, plus `ruff check`, `ruff format --check`, `mypy` and `lint-imports`, then one commit (`Refs #N`), as now. At the end of each group: one full `uv run pytest`. A group-run failure that its test name doesn't explain is traced to a story with `git bisect` over that group's commits. A story's completion comment states "own tests green @ SHA, awaiting group gate" until its group's full run passes; only then does it get the full evidence line and its AC boxes ticked. Per-milestone CI at the PR is unchanged. Full plan and metrics: `.n8/memory/m3-batched-test-gate.md`.
  **Why:** Running the full local suite after every story is the main cost of `/n8-exec`. The owner accepts some loss of per-story test traceability in exchange for speed: any regression must be fixed either way, and per-story commits plus bisect recover which story caused it. The groups are cut along the dependency graph (blocked-by links), so no group builds on code that hasn't passed a full run, and stories that touch the same files (e.g. the erasure cluster #123–#127) share a group.
  **Affects:** Changes how M3 is executed, not what it delivers. No story's acceptance criteria, scope or libraries change, so M4+ plans are unaffected and `/n8-replan` is not needed. After M3, the owner decides from the recorded metrics whether to propose this upstream via `/n8-feedback`. — reconciled (process only, no plan drift) 2026-10-12

## /n8-exec M3 — 2026-10-12

- **Decision:** Drift check before any code. Six fresh checker agents read the 30 work items (stories #103–#128, bugs #93, #94, #303, #305) and verified 733 concrete claims about existing code against main @ 1740e0d. Every AC-level finding went to three independent refuters. 28 items have implementation-detail drift only. M2 built several things differently from what the M3 plans assumed: there is no barrier fault fixture (`epistrel.testing.faults` is the subprocess harness), no route-level idempotency wrapper (mutating is decided by HTTP method in `api/auth.py`, `api/deps.py` and `api/openapi.py`), the OpenAPI tag list is closed (`openapi.TAGS`, checked by C-01), `audit_log.kind` is CHECK-constrained, deployment-scoped mutation bodies must extend `DeploymentMutationResponse`, a 413 needs a `STATUS_TEXT` entry, and docs/04 §15 maps some requirements to fewer test IDs than the planned part markers name. Each implementer receives its story's corrections and pointers. Deviations that follow from them are logged per story.
  **Why:** The M3 plans were written on 2026-10-08, before M2 was executed (2026-10-11/12). Under the skill, implementation-detail staleness is adjusted inline and logged.
  **Issue:** #93, #94, #103–#128, #303, #305
- **Blocker (drift):** #106 has two AC-level defects, each upheld 3/3 by the refuters.
  (1) AC4 says "preset profiles cannot be deleted or edited (409), only copied", and the test plan has "presets immutable". Both contradict FR-PROF-3 ("Presets are ordinary profiles and can be copied and edited"), ADR-084, FR-PROF-7 and the story's own pass-1 Discretion. They also contradict the `/n8-plan M3` coverage fix that recorded "`preset_immutable` dropped": that fix reached the Discretion lines but not AC4.
  (2) AC1 seeds the Lite budgets as `{model_calls: 2, deadline_ms: 30000}`, on the premise "the Lite preset's 2 replaces #69's 4". #69 never had a default of 4. M2 shipped NFR-PERF-6's defaults: a total cap of 9 + G that includes G, and a 45 s `turn_deadline`. Once #113 makes the Lite preset the source of defaults, AC1's values would cut every story to 2 + G calls and 30 s. That contradicts NFR-PERF-6 and fails the M2-gated tests P-09 and S-SELFPOLICE.
  The question and options are on #106, and the owner was asked in-session. #106 is labelled `blocked` + `needs-owner-action`, and its group-A dependent #109 is labelled `blocked`.
  **Why:** Under the skill, AC-level drift is a hard stop and a `/n8-replan` matter. `/n8-verify` checks the AC text, so only the owner changes it.
  **Issue:** #106 (it transitively holds #109, #111–#113 and #115–#128)
- **Decision:** The stop is scoped to #106 and its dependents, not the whole milestone. Group A's other items (#305, #93, #94, #303, #103, #104, #105, #107, #108) start now. None of them depends on #106, and no resolution of #106's defects changes their acceptance criteria. If the owner prefers the skill's literal milestone-level stop, the run stops. Nothing has merged, so nothing needs reverting.
  **Why:** The milestone-level stop exists so that stale plans are not executed. These nine items' plans are not stale: they have implementation-detail drift only. A full stop would idle the run for hours while the owner is away, and would protect nothing. Cost if wrong: the owner stops the run, and the work done stays on an unmerged branch.
  **Issue:** #106
- **Decision:** Group A runs as two lanes.
  Lane 1 uses the main working tree and is strictly sequential in dependency order: #103 → #104 → #105 → #107 → #108, then #106 → #109 once #106 is resolved.
  Lane 2 runs the four carried bugs in parallel, each on its own branch `m3/bug-<n>` in its own git worktree (`.claude/worktrees/`, excluded locally through `.git/info/exclude`). Their commits are cherry-picked onto the milestone branch before the group A gate, and their decisions are appended here at that point.
  **Why:** The bugs touch only `tests/chaos/test_x_04.py`, `tools/gates/traceability.py`, `tools/gates/licenses.py` and `src/epistrel/tools/model_matrix.py` (plus their tests and `docs/model-matrix.*`). No group-A story touches these files, and no bug depends on anything. Running the bugs beside lane 1 saves wall time without changing what is gated, because the group gate runs after the cherry-picks. #305 still lands before group A's full run, which is what "first in A" protects.
  **Issue:** #305, #93, #94, #303
- **Decision:** Each story is independently verified after its implementer commits. A fresh agent that did not write the code checks every AC clause against the code and tests, using temporary mutations to show that a test fails when the behaviour breaks. It also checks the test plan's tests and part markers, the Discretion lines, the gates and the invariants. Gaps go to a fix agent. After at most two fix rounds, the story is flagged for the orchestrator.
  **Why:** Under the batched gate the full suite waits for the group run, so each story's correctness rests on its own tests. An independent check catches AC gaps before later stories build on them, and before `/n8-verify` does.
  **Issue:** M3
- **Decision:** Each group-gate run is `uv run pytest --cov=epistrel.core --cov-report=term-missing:skip-covered --cov-fail-under=90`, timed. That is the plain full suite plus CI's coverage flags.
  **Why:** CI's test job enforces 90 % branch coverage of `epistrel.core`, and M3 adds a lot of core code. Checking coverage at each group gate attributes a shortfall to that group's stories, rather than finding it at the milestone PR. The baseline (550 s) was a plain `uv run pytest`, so the recorded group timings include coverage overhead, and the metrics table says so.
  **Issue:** M3
- **Decision:** Installed ffmpeg 9.0.2 on the owner's Mac with Homebrew (`HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 brew install ffmpeg`).
  **Why:** #107's poster tests generate tiny MP4 fixtures with ffmpeg at test time, and CI installs it on the runner. Without ffmpeg, those tests could not run locally in the per-story gate or the group gate. This changes the local dev environment only, not the repository. Rule 3 (blocker: missing tool).
  **Issue:** #107
- **Decision:** Catalog writes do not run an `Operation(story_id=None)`. Each write (create, save, publish, archive, unarchive, delete) is one transaction of row writes. The same transaction inserts a committed, story-less `control` operation with the requesting principal and binds the call's `Idempotency-Key` to it (`IdempotentCall.bind_configuration`). The helper `core/control.py::record_control_operation` was extracted from `config_changes._write_operation`, which now calls it, so configuration writes behave exactly as before. A no-op archive or unarchive records no operation, and its key keeps `operation_id` null.
  **Why:** This is the drift note's "#61 pattern". `Operation.begin()` writes an `in_flight` row in its own transaction, and a story-less commit takes the global advisory lock. So a refused write (409 `draft_stale`, 422) would leave a `failed` operations row, which C-01's zero-row check forbids, and every catalog write would serialise on one lock. Cost if wrong: later catalog, media, persona and profile stories copy this helper, and moving to `Operation` would mean rewrapping their writes.
  **Issue:** #103
- **Decision (drift):** `catalog.save.before_cas` is an in-process pause point, not a crash point. It lives in the new `epistrel.testing.pauses` (standard library only, called from core the way `crash.hit` is, and armed only by tests through `held()`); `crash.POINTS` is unchanged. A second point, `catalog.publish.locked`, makes the publish race deterministic: the test holds the first publish inside its row lock and waits until `pg_stat_activity` shows the second blocked on a lock. Both race tests use `asyncio.gather` over `httpx.ASGITransport`.
  **Why:** The planned barrier fixture does not exist (`epistrel.testing.faults` is the subprocess harness). Mutation-checked: removing the compare-and-set condition makes the two-saves test answer [200, 200], and removing `FOR UPDATE` from publish fails the two-publishes test.
  **Issue:** #103
- **Decision (drift):** The `mutating=False` route flag is `api/deps.py::route_flags(mutating=False)`, i.e. `openapi_extra={"x-epistrel-mutating": false}`, and `deps.mutates(method, route)` reads it. `get_request_context` mints and echoes no key for a flagged route. The OpenAPI builder documents a flagged route with its principal but without `Idempotency-Key`, the expected revision, the idempotency errors and the replay headers, and keeps the marker in the operation object. `response_base_problems` stops requiring a revision base for it. `require_principal` stays method-based. No #103 route uses the flag; `tests/test_route_flags.py` proves it with test-only routes, and `docs/openapi.json` is byte-identical for existing routes.
  **Why:** No route-level idempotency wrapper exists: three method-based checks decide what is mutating, and #128's replay test, #109, #111 and #121 need one flag that all three honour. docs/02 §9 requires the principal on every POST/PUT/PATCH/DELETE.
  **Issue:** #103
- **Decision (drift):** FR-CAT-9 moves out of the C-15 marker. `test_c_15__catalog_lifecycle` in `tests/conformance/test_c_15.py` carries `part("M3", "FR-CAT-1,3; FR-SCN-10")`: create, save, publish v1, edit, publish v2, v1 unchanged and immutable, versions listed, the card's nine keys without the planted phrase that `full` has, and an archived entry read in both projections. The byte-identical story checks across edit, archive, unarchive and delete are in `test_i_17__catalog_never_touches_stories` in `tests/integration/test_i_17.py`, with `part("M3", "FR-CAT-3,9")`. Its complement archives the story and sees the snapshot change. The detailed tests are unmarked, in `tests/integration/test_catalog_characters.py`.
  **Why:** docs/04 §15 maps FR-CAT-9 only to I-17 and I-21, so the planned C-15 marker fails `check-traceability`. Moving the ID costs no coverage, whereas adding `C-15 (M3)` to §15 would edit docs/04 for nothing. Each test ID keeps its M3 parts in one file, as traceability requires. Cost if wrong: add `C-15 (M3)` to the §15 FR-CAT-9 row and put the ID back in the marker.
  **Issue:** #103
- **Decision:** `card` is exactly AC3's nine keys (`id, version, name, public_description, images, labels, content_rating, listing_status, status`). The pass-2 envelope (`current_version`, `draft_revision` on drafts only, `created_at`, `updated_at`, `published_at`, `companion_id`) is applied only to `full`, which adds `version` and the `definition` group. Both are core Pydantic models (`CharacterCard`, `CharacterFull`) with `extra="forbid"`. `Engine.get_character(..., projection=)` returns the projected model, so REST and the library share it. The GET's response model is the union `CharacterCard | CharacterFull`.
  **Why:** AC3 says the card returns "only" these keys, and that wins over the Discretion line. #110 plans list cards as "the single-GET card plus `owner_principal`, `updated_at`", which also assumes a card without timestamps. `extra="forbid"` makes the union unambiguous and stops a definition field from slipping into a card. Cost if wrong: adding envelope keys to the card later is additive.
  **Issue:** #103
- **Decision:** The interim `CharacterDraft` is `{name?, public_description?, definition: {personality?}}` with `extra="forbid"`. Every field may be missing on a draft, and `publish_report` requires `name` (422 `publish_invalid`, report entry `field_required`/`name`) on top of a schema re-validation. Field limits are taken from #115's plan: name ≤ 120 (stripped), public description and personality ≤ 4000. `draft_revision` and `?version=` are bounded to int32 at the boundary. The 256 KiB cap applies to the merged canonical JSON (`idempotency.canonical`). Because the field limits keep a draft far below the cap, the 413 test lowers `DRAFT_MAX_BYTES` with monkeypatch.
  **Why:** The Discretion makes `name` the only publish requirement before #115. Matching #115's limits now means nothing saved today fails #115's validation for length. The int32 bounds follow #280's lesson (an `INTEGER` column must never see an out-of-range value).
  **Issue:** #103
- **Decision:** Catalog mutation responses extend `DeploymentMutationResponse` and carry the deployment's `config_revision`, which catalog writes leave as it is. Every catalog codec uses `revision_field=config_revision`.
  - Create: 201 with the draft in `full`. There is no `Location` header, because `CREATING_OPERATIONS` documents `/stories/{id}`.
  - Save: 200 with the draft in `full`.
  - Publish: 201 `{id, version, draft_revision, status, published_at, report}`.
  - Archive and unarchive: 200 with the envelope.
  - Delete: 204.
  - `content_rating_version_id` is NULL on every #103 publish.
  **Why:** `response_base_problems` (`tests/test_responses.py`) requires a revision base on every deployment-scoped mutation. The interim draft names no rating, and #115 adds `metadata.content_rating_id` together with the NOT NULL.
  **Issue:** #103
- **Decision (drift):** The `catalog_entry_deleted` audit row records the principal in its `principal` column. Its details are `{entity_type: "character", entity_id, operation_id}`, mirroring `story_deleted`, which also names its operation; the plan had `{entity_type, entity_id, principal}`. Migration `0020_catalog_characters` drops and re-creates `ck_audit_log_kind`. Its downgrade deletes those rows under `epistrel.allow_audit_expiry` (resetting the setting afterwards) before it restores the old check. `test_catalog_downgrade_keeps_other_audit_rows` covers this.
  **Why:** `audit_log.kind` is CHECK-constrained, and `principal` is already a column (drift note).
  **Issue:** #103
- **Decision:** The versions trigger refuses TRUNCATE always, and UPDATE or DELETE of any column unless `epistrel.erasure = on`, with SQLSTATE `EPI01`. Its function is `catalog.epistrel_character_versions_immutable()`. Delete locks the entry row, sets the setting transaction-locally, and deletes the entry, letting the FK cascade remove the versions.
  **Why:** The setting keeps the story's and #124's name, `epistrel.erasure`, rather than the `epistrel.allow_*` convention. A complement test shows an UPDATE passing under the setting.
  **Issue:** #103
- **Decision:** `EntryStatus` lives in `core/enums.py` and is registered in `PG_ENUM_TYPES` as `catalog.entry_status`. `pg_enum` gained a `schema` argument, and `AUTOGENERATE_OPTS` gained `include_schemas` with an `include_name` filter for public and `catalog`. I-08's fingerprint now covers `catalog`: a `schemas` aspect, schema-qualified columns, indexes, functions and enums, the `KEPT_ON_DOWNGRADE` key qualified, and an assertion that base leaves only `public`.
  **Why (Rule 3, blocker):** `test_event_store_schema.py::test_enum_labels_match_python_enums` compared every enum type in the database by bare name, so it failed once the catalog enum existed. It now schema-qualifies non-public enums. That keeps the check over every enum; the test is not weakened.
  **Issue:** #103
- **Decision:** `epistrel.core.ports` ships four Protocols with no-op defaults, and `Engine(..., ports=None)` uses `Ports()`:
  - `InputGate.enforce(texts, *, entry_point, principal, rating_id)`: the default passes every text.
  - `PolicyEvaluator.evaluate(texts, *, rating_id)`: the default reports no hit.
  - `ModelCaller.complete(purpose, prompt) -> str | None`: the default returns `None`.
  - `JobStarter.start(conn, kind, payload, *, story_id) -> str | None`: the default returns `None`.
  Nothing in core calls a port yet.
  **Why:** This is the pass-2 Discretion. `None` from the model caller and the job starter means "unavailable", which the planned callers already degrade on (#111 lists it, #124 redacts). Cost if wrong: the gate default fails open. A story that adds a gated core path without wiring the Orchestrator's gate would skip input policies, so each such story must prove a refusal through the wired port.
  **Issue:** #103
- **Decision:** A creation's `owner` follows the principal rule: spaces and tabs trimmed, 1 to 256 characters, no control character, never `system`. The rule is an `AfterValidator` with the principal pattern in the schema; the pattern constant moved to `core/context.py`, and `api/openapi.py` imports it. A bad owner is 422 at `body.owner` before the Engine runs. The row records the owner, and the control operation records the requesting principal. `owner_principal` is not returned in either projection yet, because #104 adds it.
  **Why:** AC5 and FR-AUTH-5 ("`owner: system` is refused 422 on create too").
  **Issue:** #103
- **Decision:** Filed #311 (`needs-triage`) and did not fix it. When `tests/test_markers.py` runs before any test has set up `pg_url`, its in-process pytester run removes `epistrel.core.schema` from `sys.modules`. Alembic's `env.py` then re-declares `schema_meta` on the surviving `MetaData`. The error reproduces on main @ fa0c78d in a temporary worktree and does not affect the full suite's order.
  **Why:** It is outside #103's scope; only subset runs that put the marker tests first hit it.
  **Issue:** #103, #311
- **Decision:** #103 verification fixes, round 1. Publish now answers with the status it actually stored. The entry `UPDATE` returns `status` (`RETURNING`), and `Published.status` is built from that instead of the constant `EntryStatus.PUBLISHED`. The value, the response model and the idempotency codec are unchanged.
  **Why:** The verifier found that no test would fail if publish stopped setting `status = published` (AC2). One reason was that the answer was a constant. Reading the row back turns the existing response assertion into a real check, at the cost of one returned column. The new tests below also catch that mutation with the constant restored, so if this change is judged unnecessary it can be reverted without losing coverage.
  **Issue:** #103
- **Decision:** #103 verification fixes, round 1. AC2's status and AC3's version list are now asserted from what is stored, not only from the publish answer.
  - `test_draft_save_publish_and_versions`:
    - after the first publish, it reads the entry row (`status`, `current_version`, `draft_revision` = `published`, 1, 3) and a fresh card (`published`, version 1);
    - the version-1 `full` read and the version list carry the publish response's `published_at`;
    - each list item has exactly `{version, published_at, published_by}` (new `tests/catalog_support.VERSION_KEYS`).
  - `test_c_15__catalog_lifecycle` asserts that the card is `published` before archiving. Going beyond the fix hint, it also asserts the list's keys and publish times, because docs/04 C-15 names version listing for characters.
  - Mutation-checked, with the sources restored from a scratch backup each time, because `git checkout` would have discarded the uncommitted fix. Each of these mutations fails both tests (2 failed, 38 passed over the verifier's module set, which passes unmutated inside the 98-test gate run):
    - dropping `status` from the publish `UPDATE`, with the answer read back;
    - the same, with the constant answer restored;
    - dropping `published_at` from `CharacterVersionBody`;
    - shifting the listed `published_at` by one second.
  **Why:** These are the verifier's two test-coverage gaps (AC2, AC3). Comparing the publish times as strings also pins one serialization across the publish answer, the version read and the list. The comparison is exact because Postgres `timestamptz` keeps the microseconds of the Engine's `now()`. Cost if wrong: none to behaviour; at worst a stricter test.
  **Issue:** #103
- **Decision:** #103 verification fixes, round 2. All three gaps were real, and all three were gaps in test coverage only. The code already did what AC3 and AC5 require: `core/catalog/characters.py::get` reads, the attribution (`published_by`, the control operation's actor, the delete's audit principal) and the delete's key scope (`Engine.delete_character`). No source file changed. The tests:
  - `test_draft_save_publish_and_versions` now reads the entry between the edit and the second publish, while the draft differs from version 1. The default card and the `full` read serve version 1: the `full` read equals the `?version=1` read on every content key and has no `draft_revision`. The draft read serves the edit in both projections, at draft revision 4 with `version` 1.
  - The new `test_writes_record_the_requester_not_the_owner` covers attribution.
  - `test_delete_removes_the_entry_and_its_versions` asserts that the delete key's `idempotency_keys` row is `('deployment', 'deployment', 'done')`.
  - Mutation-checked over the verifier's module set (`test_catalog_characters`, `test_c_15`, `test_i_17`, `test_catalog`, `test_c_03`). Each mutation was applied with the sources restored from a scratch backup afterwards, and the sources were byte-identical at the end. Each of these now fails (unmutated: 41 passed):
    - the verifier's F (the default read serves the draft) and G (the draft read serves the latest version): `test_draft_save_publish_and_versions` and `test_c_15__catalog_lifecycle` fail;
    - `published_by` set to the owner, and the operation's actor set to the owner (at save, publish and archive together, at publish alone, and at archive/unarchive alone): the new test fails;
    - delete recording the owner on its operation, or on its audit row: the new test fails;
    - delete keyed per entry (`story_id=character_id`, scope `story`): the delete test fails.
  **Why:** The verifier showed that each of its mutations passed every test (40 passed and 20 passed, 0 failed). The two delete mutations are mine: run against the round-1 test files, each gave 40 passed, 0 failed. The single-site actor mutations are parts of the verifier's combined one. These are the core of FR-CAT-3 (an unpublished edit is not what readers see) and of AC5 (attribution and the deployment key scope). Cost if wrong: none to behaviour; at worst the tests are stricter.
  **Issue:** #103
- **Decision:** `test_c_15__catalog_lifecycle` also checks the unpublished edit, between its second save and its second publish. The card still serves version 1 with the old description, and the draft card serves the edit. The fix hint called this optional, and I took it.
  **Why:** C-15 is #103's M3 part for FR-CAT-3, and docs/04 C-15 names "drafts are editable and published versions immutable". Keeping the draft separate from what readers see is the heart of that, so the milestone part should pin it, not only the unmarked detailed test. Cost if wrong: two extra reads in C-15.
  **Issue:** #103
- **Decision:** Attribution has its own test, `test_writes_record_the_requester_not_the_owner`, rather than an extension of `test_owner_is_the_requester_unless_named`, and it goes further than the fix hint. Three principals are involved: the creator (`p-catalog`), the named owner (`p-owner`), and two editors (`p-editor`, `p-reviewer`). The editors take turns across save, publish, a second publish, archive, unarchive and delete. The test asserts four things:
  - each write's operations row, looked up by its `Idempotency-Key`, names that write's requester;
  - the version list gives `published_by` `p-editor` for version 1 and `p-reviewer` for version 2;
  - the delete's audit row names its requester;
  - `owner_principal` stays `p-owner`.
  Its docstring cites FR-AUTH-8, FR-AUTH-4 and FR-AUTH-5, so check-parts derives a docstring part from it that gates at M3, the latest milestone it cites (FR-AUTH-4 is M2). check-parts therefore counts 145 parts, not 144. check-parts, check-lexicon and check-traceability all pass.
  **Why:** FR-AUTH-8 says ownership confers no permission, so non-owner editors are the normal case. The hint covered save, publish, archive and unarchive by one editor. Delete records the requester in two places (the operation and the audit row), and two different publishers show that each version keeps its own publisher. Keeping creation (`test_owner_is_the_requester_unless_named`) and later writes in separate tests keeps each test name accurate. Cost if wrong: one more test of about 50 lines.
  **Issue:** #103
- **Decision (drift):** Story owner and relation routes are `/stories/{story_id}/owner` and `/stories/{story_id}/relations…`. Their bodies extend `MutationResponse` and answer the story's unchanged `timeline_revision`. Every other type's bodies extend `DeploymentMutationResponse` with the unchanged `config_revision`. The idempotency codecs are keyed per method and type (`transfer_owner_story`, `add_relation_character`, …), the same as the routes' operation ids. A story write checks `X-Epistrel-Expected-Revision` first, like every other story-path mutation. Keys are deployment-scoped, as the Discretion says.
  **Why:** `response_base_problems` requires `MutationResponse` on any path that names `{story_id}` and `DeploymentMutationResponse` elsewhere; there is no third base. A reference replay after the result window must name the right revision field. Cost if wrong: the response shapes are additive, so only the revision field name would change.
  **Issue:** #104
- **Decision (drift):** The planned `test_c_15__ownership` becomes three I-21 slices in the new `tests/integration/test_i_21.py`, the one file every later I-21 (M3) part must join:
  - `test_i_21__ownership`, `part("M3", "FR-AUTH-5,6; FR-CAT-9")`: creation, transfer, `system`-owned transfer, relations, filters, and a story left byte-identical by its catalog entry's transfer and relation changes;
  - `test_i_21__nothing_gates`, `part("M3", "FR-AUTH-8")`: the matrix;
  - `test_i_21__companion`, `part("M3", "FR-AUTH-5; FR-CAT-9")`: companions, and the scenario half of the FR-CAT-9 clause.
  No C-15 slice: #110's `test_c_15__listing_status` claims FR-AUTH-8 there. The detailed tests (`tests/integration/test_ownership.py`, `tests/test_ownership_rules.py`) name no requirement ID in their docstrings, so they are not parts.
  **Why:** docs/04 §15 maps FR-AUTH-5,6 and FR-CAT-9 only to I-21 (M3) among this story's tests, so the planned C-15 marker fails `check-traceability`. Splitting into slices gives each clause a precise test name. check-parts now counts 148 parts.
  **Issue:** #104
- **Decision:** `ownership_events` uses the AC's names, not the pass-1 Discretion's. The column is `acting_principal`, not `principal`. The kinds are `transfer`, `relation_added`, `relation_removed` and `listing_status`, plus the Discretion's `deleted` for `drop_entity`. The Discretion's `owner_changed` and `listing_status_changed` are not used. The table also has `id` (ULID), `old`/`new` jsonb (owners as JSON strings, relations as `{principal, relation}`, and `deleted`'s `old` as `{owner_principal, relations}`), `at` (transaction time), and a nullable `job_id`, which the Discretion lists. So #117's migration must not add `job_id` again. Both `kind` and `entity_type` are CHECK-constrained text. `core.ownership.record_event` is the one writer, and #110 and #117 call it.
  **Why:** `/n8-verify` checks the AC text, and the AC names the columns and kinds explicitly. The Discretion's renames came later and never reached the AC. Cost if wrong: renaming two kinds and a column, before anything reads them.
  **Issue:** #104, #110, #117
- **Decision:** `owner_principal` appears in a catalog character's `full` projection (and so in the create and save answers), in `GET /stories/{id}` and in each `GET /stories` item. It is not in `card`, although the pass-1 Discretion lists `card`.
  **Why:** FR-SCN-10 lists the card's contents exactly and leaves the owner out. #103's verified AC3 and its decision fix the card at nine keys. `CARD_KEYS` in `tests/catalog_support.py` asserts that the card has nothing else. Cost if wrong: one key added to the card, plus updated card tests.
  **Issue:** #104
- **Decision (drift):** No `stories.updated_at` is added. A transfer bumps `updated_at` only on tables that have it (`catalog.character_catalog` now). `core/ownership.py` reads each table's columns at runtime from `pg_attribute` through `to_regclass`, so `updated_at` and `companion_of` are used wherever a later table has them. A table that does not exist yet means its type is not delivered: calls on it answer 404 `not_found`. The registry-generated tests cover only delivered types.
  **Why:** The drift note offers this alternative. A `stories.updated_at` that only ownership moves would mislead readers, because turns and settings changes would not move it. #117's lookup already plans `(created_at, id)` keysets for tables without `updated_at`. Cost if wrong: one column and an application-side bump.
  **Issue:** #104, #117
- **Decision (drift):** A fork is owned by its requester, or by the optional `owner` that `ForkRequest` now accepts (the same rule as `POST /stories`'s new `owner`). `_copy_story_row` overrides `owner_principal`, and the source's relations are not copied. The migration backfills each existing story's owner from the principal of its newest committed `genesis` operation (`committed_at DESC`, then `operation_id DESC`; a fork's own genesis is newer than the one it copied), else `system`. `test_ownership_backfill_and_downgrade` covers both cases and the downgrade.
  **Why:** Without the override, `_insert_select` would copy the source's owner into the fork. FR-AUTH-5 says the owner is set at creation and defaults to the requester, and a creation may name another owner. A fork is a creation. Cost if wrong: drop the optional field. The default does not change.
  **Issue:** #104
- **Decision:** These are the registry and route conventions later stories follow:
  - `REGISTRY` in `core/ownership.py` names every type's schema, table, URL segment, path parameter and noun: `story`/`story_id`, `character`/`character_id`, `scenario`/`scenario_id` (`catalog.scenario_catalog`), `persona`/`persona_id` (`personas`), `media`/`media_id` (`media_assets`) and `engine_profile`/`profile_id` (`engine_profiles`).
  - `mount_ownership_routes(router, entity_type)` adds four routes per type on the type's own router. Each handler is built per type, with an explicit `__signature__` that names the type's path parameter. The routes are tagged `ownership` through `openapi.ROUTE_TAGS`, a new tag in the closed list.
  - `companion_follows_character` (422) is documented on the write routes of types that follow a parent.
  - Ownership writes need `ownership_fixtures.KINDS` and `CALLS` entries for every new type and route. `test_every_delivered_type_is_covered` fails until a story that creates an owned table adds them and mounts the routes.
  - Every delete path calls `ownership.drop_entity` in its transaction, before the row goes.
  **Why:** These are the pass-1 Discretion's prescriptions, made concrete enough that #105, #106, #108, #114 and #116 add a type without changing this module. Cost if wrong: a later story renames a path parameter in the registry.
  **Issue:** #104, #105, #106, #108, #114, #116
- **Decision:** Each ownership write is one transaction under the entity's row lock: `FOR UPDATE` for a transfer, `FOR SHARE` for a relation change. It writes its `ownership_events` row or rows and one committed `control` operation by the acting principal through `core/control.py`, which binds the key. The operation is story-scoped for a story, so `delete_story` removes it and its key with the story. It is story-less for every other type.
  - A no-op records nothing. That covers a transfer to the current owner (200 with `previous_owner` equal to the owner) and an add that finds the relation (200 with the existing row, its `created_at` and `created_by`).
  - A relation add is an `INSERT … ON CONFLICT DO NOTHING`, so two simultaneous adds give one 201, one 200 and one event.
  - No story event is written and no revision moves.
  - The writes run on archived stories and entries and while a turn is in flight.
  - A character transfer also moves every scenario whose `companion_of` names the character, locked character first, with one event per entity. It skips a companion already at the new owner.
  **Why:** This is the Discretion's "control operation, no story event, row lock only, always allowed", built on #103's `record_control_operation` pattern so that a refused write leaves no row (C-01's zero-row check). An event-less story-scoped control operation is not copied by a fork (`_select_operations` needs a non-audit event) and is invisible to history.
  **Issue:** #104
- **Decision:** Relation names follow the pass-1 Discretion, not the AC's looser "free text (≤ 64 chars)":
  - 1 to 64 of `A-Z a-z 0-9 _ -`, case-sensitive, trimmed of spaces and tabs in a body;
  - checked again by `ck_entity_relations_relation`;
  - listed in byte order (`COLLATE "C"`, so `Editor` sorts before `editor` whatever the database's collation).
  The removal route is `DELETE …/relations/{relation}/{principal:path}`, because principals may contain `/` (drift). Its principal, the transfer's `principal` and the relation's `principal` all follow the `X-Epistrel-Principal` rule, and `system` is refused (422 at `path.principal` or `body.principal`). `Owner` and the new `Principal` are one rule (`ownership.principal_rule`). #103's `Owner` type moved from `core/catalog/characters.py` to `core/ownership.py` unchanged, and stories and forks use it too. `GET /stories` takes `?owner=` (`system` allowed, because presets are system-owned) and a repeatable `?relation=` (AND), split at the first colon. Errors land at `query.relation[i]`.
  **Why:** The character set keeps names from colliding with a path segment or the filter's colon. "Free text" is read as consumer-defined and never interpreted. Cost if wrong: widening the pattern and the CHECK.
  **Issue:** #104
- **Decision:** The relation filter is `name:principal`, or `name:` (an empty principal) for any principal. The Discretion's bare `relation=name` is refused (422), because the colon is required. Both filters are documented without a `null` alternative: `owner` is a pattern-constrained string, and `relation` an array of pattern-constrained strings (`WithJsonSchema`). The validation is unchanged.
  **Why:** A randomized C-01 run failed `GET /stories` with `AcceptedNegativeData`. Schemathesis turned `relation` into the boolean `false` and `owner`'s `null` branch into an object, and both serialize to query strings that a bare relation name or an opaque principal accepts. This is #279's class of flake, and the owner settled it there by tightening the contract. With the colon required, a bare `true`, `0` or other junk string is refused. With the null branch gone, schemathesis does not change the type of a string query parameter at all. A temporary stress run of C-01's checks over the nine new or changed operations, 1,500 examples each, passed; the file was deleted afterwards. Twelve randomized runs of `GET /stories` and a `CI=1` run passed too. Cost if wrong: a consumer writes `editor:` instead of `editor` to filter on any principal.
  **Issue:** #104
- **Decision:** Indexes are `ix_stories_owner_principal`, `catalog.ix_character_catalog_owner_principal`, `ix_entity_relations_principal` and `ix_ownership_events_entity (entity_type, entity_id, at)`. The `(entity_type, entity_id)` index the Discretion names is the leading prefix of `entity_relations`' primary key `(entity_type, entity_id, principal, relation)`, not a separate index.
  **Why:** A second index on the primary key's prefix would only cost writes. Cost if wrong: one `CREATE INDEX`.
  **Issue:** #104
- **Decision:** `test_i_21__companion` tests the companion rule against a stand-in `catalog.scenario_catalog (id, owner_principal, companion_of, updated_at)` on its own scratch database (`migrated_database`), through the library, because #108 and #116 have not built scenarios or companions yet. The test asserts that the real table does not exist, so it fails once #108 creates it. #108 or #116 must then rebuild the test on real scenarios. It also plants an out-of-step second companion to cover the skip of a companion already at the new owner.
  **Why:** AC1's companion clauses need a test now. A stand-in in the session database would break the drift tests. A loud failure is better than a test that silently stops covering the rule. Cost if wrong: the #108 implementer rewrites one test.
  **Issue:** #104, #108, #116
- **Decision:** These clauses are verified only partly, or are left to the story that owns them:
  - The Lite preset clause is checked generically: a catalog character set to owner `system` transfers like any other, with `old` `system` in the event. #106 seeds the preset owned by `system` and mounts its routes, and the generated tests then cover profiles.
  - The story-creation clauses go to #119, as the Discretion says: player slots recorded as `player` relations, and creating a story from another owner's entry.
  - `GET /catalog/characters?relation=` waits for #110's list, which uses `ownership.list_filters`.
  - The scenario, persona, media and profile routes do not exist yet.
  **Why:** The tables and endpoints those clauses need belong to #106, #108, #110, #114, #105 and #119, all later in M3.
  **Issue:** #104, #106, #110, #119
- **Decision:** The nothing-gates matrix (`test_i_21__nothing_gates`) is parametrised over every route under an owned entity, reads included and `/admin` excluded. That is 35 cases for stories and catalog characters. Each case sends one route's minimal request three times, each time about a fresh entity owned by A in the same state:
  - as A;
  - as a stranger B;
  - as C, who holds relations named `admin` and `owner` on the entity.
  It asserts that all three get the route's own success status. Catalog entries are the default `private`, and the character read uses `projection=full`. The matrix uses three principals, not the Discretion's two, so that it also proves "adding `admin` grants nothing".
  **Why:** Equal success codes prove that every request ran, not that every request failed alike. Mutation-checked: making transfers owner-only, or making a draft save owner-only, fails the matrix.
  **Issue:** #104
- **Decision:** `tests/test_ownership_rules.py` scans the AST of `src/` and allows `entity_relations` to be named only in:
  - `core/ownership.py`;
  - `core/tables/ownership.py` and `core/tables/__init__.py` (the declaration and its registration, which the Discretion's allow-list leaves out);
  - `core/principals.py` (#117).
  It also refuses any comparison between a `.relation` attribute and a string literal. A complement plants a module with `row.relation == "admin"` and checks that the scan flags it. #110 adds its catalog list module to the allow-list only if it filters without `ownership.list_filters`.
  **Why:** This is AC2's repository test, and the literal-comparison check is the "no code path interprets a name" half. Mutation-checked: a raw SQL string naming `entity_relations` in `core/stories.py` fails it.
  **Issue:** #104, #110
- **Decision:** `TRUST_BOUNDARY_STATEMENT` ends with the Discretion's sentence, verbatim ("Ownership and relations are labels the consumer defines; the engine never grants or withholds an action because of them."), followed by AC4's "Ownership exists for listing, attribution and offboarding only." It appears in the README, docs/02 §9 and `info.description`. Every ownership route's description and the `ownership` tag repeat the point. docs/02 §4.10's ownership paragraph and §9's route list are amended to the shapes built here. `docs/openapi.json` is regenerated, and the regeneration documented the add-relation 200 answer after C-01 found it undocumented.
  **Why:** AC4 asks for "listing, attribution and offboarding only", which the Discretion's sentence does not say on its own. `test_trust_boundary` keeps the three copies identical.
  **Issue:** #104
- **Decision (drift):** S-DELETE and C-03 are extended, and no existing assertion is loosened.
  - S-DELETE: every story gets a `player` relation. `_rows_of` counts the victim's `entity_relations` rows as its own. The added tables now include `ownership_events`. The victim's `deleted` event is asserted: owner `p-author`, the relation removed, actor `p-author`.
  - C-03 gains parity steps for `transfer_owner`, `add_relation`, `list_relations` and `remove_relation` on a story and `transfer_owner`, `add_relation` and `list_relations` on a catalog character, plus a `list_stories` step with `owner` and `relation` filters.
  **Why:** These follow the drift note on S-DELETE and the `parity_gaps` rule that every new Engine operation needs a route and a step.
  **Issue:** #104
- **Decision:** Mutation checks were run with the sources restored byte for byte afterwards. Each of these fails at least one of the story's tests:
  - transfers only by the owner;
  - draft saves only by the owner;
  - companions not moved;
  - a story delete without `drop_entity`;
  - a fork copying the source's owner;
  - the relation filter ignored, or the owner filter ignored;
  - a no-op transfer that records an operation;
  - an out-of-step companion re-recorded;
  - the new owner recorded as the actor;
  - story creation ignoring `owner`;
  - a raw SQL string naming `entity_relations` outside the allow-list;
  - an add that always reports `created`;
  - companions not refused.
  The two that survived the first round, both no-op variants, led to the added assertions on no-op operations and on the out-of-step companion.
  **Why:** Under the batched gate, each story's correctness rests on its own tests until group A's full run.
  **Issue:** #104
- **Decision:** #104 verification fixes, round 1 (Rule 1). A relation change now locks its entity's row `FOR NO KEY UPDATE`, not `FOR SHARE`. So the relation writes on one entity take turns with each other and with the entity's deletion. This replaces the `FOR SHARE` half of the row-lock decision above; a transfer still takes `FOR UPDATE`.
  - The bug: an add whose `INSERT … ON CONFLICT DO NOTHING` found the relation stored read it back in a second statement. A removal also held only `FOR SHARE`, so it could delete the row and commit in between. The add then raised `NoResultFound`, answered as a 500. The verifier reproduced it, and so did the new test before the fix (`NoResultFound` in `add_relation`, request status 500).
  - Now the removal waits on the row lock until the add commits. The add answers 200 with the stored row, and the removal 204. The verifier's repro script now prints 200 and 204.
  - `FOR NO KEY UPDATE`, not `FOR UPDATE`, because it leaves free the `FOR KEY SHARE` that a foreign key check takes (an operation row inserted for the story).
  - A new pause point, `ownership.add.found` (`epistrel.testing.pauses.OWNERSHIP_ADD_FOUND`), sits between the two statements. `test_an_add_racing_the_removal_of_its_relation` holds it, starts the removal, waits until the removal waits on a lock, then releases. For both delivered types, the add is 200 with the first add's `created_at`, the removal is 204, no relation is left, and the only events are the first add's and the removal's. Mutation-checked: the old `FOR SHARE` fails it.
  **Why:** The verifier offered three fixes. An upsert (`ON CONFLICT DO UPDATE … RETURNING (xmax = 0)`) leans on an undocumented system column and writes a dead row version on every repeated add. A retry loop adds a branch that cannot run under the lock and could in principle spin. The lock follows the Discretion ("concurrency is the row lock only") and the transfer's existing `FOR UPDATE`. Cost: two relation writes on one entity no longer run side by side, and each lasts milliseconds. Cost if wrong: one lock mode.
  **Issue:** #104
- **Decision:** #104 verification fixes, round 1. `test_i_21__ownership` now lists stories as A, as B and as a stranger, not only without a principal. For each asker, the unfiltered list holds both A's and B's stories, and `owner=a`, `owner=b` and `relation=editor:c` narrow it exactly. The slice's marker gains FR-AUTH-8 (`part("M3", "FR-AUTH-5,6,8; FR-CAT-9")`), because "filtering happens only when a request asks for it" is an FR-AUTH-8 clause. docs/04 §15 maps FR-AUTH-8 to I-21 (M3), so check-traceability still agrees. Mutation-checked: `Engine.list_stories` defaulting `owner` to the asker now fails the slice. It passed every test before.
  **Why:** A read carries a well-formed `X-Epistrel-Principal` into the request context (`api/auth.require_principal`), so a list narrowed to the asker was possible. Every list call in the tests sent bearer-only headers. Cost if wrong: one marker.
  **Issue:** #104
- **Decision:** #104 verification fixes, round 1. The nothing-gates matrix (`test_i_21__nothing_gates`) now covers every form of a read, and compares read bodies as well as status codes. For reads only, this goes beyond the Discretion's "equal status codes (not equal bodies)".
  - `Call.params` became `Call.queries`, and each query string is its own case. A `private` catalog character is read as its card (the default) and in `full`, as version 1, and as its draft in both projections: FR-AUTH-8 says any entry "can be read in either projection". A story and its history are read in their default viewpoint and as `omniscient` (FR-CAT-12's true ages). That is 41 cases, up from 35.
  - A read must give all three requesters the same body once ids and times are blanked (in keys too) and every list is put in one order. Each read's `prepare` now puts the owner's content where the read finds it: changed settings, pinned memories, custom instructions, a regenerated turn. So an empty answer cannot pass for a full one.
  - Writes still compare status codes only, because their bodies name the acting principal.
  - Mutation-checked. Each of these fails the matrix: a card read refused to anyone but the owner (the verifier's, which passed every test before); a draft read refused to anyone but the owner; a relation list narrowed to the relations the asker added, which still answers 200.
  - Later types' reads (#105, #106, #108, #114) get the body check too. Their `CALLS` entries need a `prepare` that puts content there, and a field that varies between two fresh entities needs blanking in `_blanked`.
  **Why:** Equal success codes prove that every request ran, not that each returned the same thing. The Discretion's reason for not comparing bodies, attribution, holds for writes. A read's body names the entity's creator, never its reader: the inspected bodies show `p-owner-a` as owner, actor, publisher and `created_by` for all three readers. Cost if wrong: a later read whose body rightly varies by reader needs an exception in the matrix.
  **Issue:** #104, #105, #106, #108, #114
- **Decision:** #104 verification fixes, round 2 (Rule 1, test fix). `test_i_21__ownership` now pins both halves of a relation filter. Two more stories cross the names and principals of `mine`, which holds `editor:c` and `co_author:d`. One holds `editor:d` (the name, with another principal) and the other `co_author:c` (the principal, with another name). Both are owned by a fresh principal `e`.
  - The slice asserts every cell of {`editor`, `co_author`} × {c, d}, and each name for any principal (`editor:`, `co_author:`), as exact newest-first lists. It also asserts that `editor:d` and `co_author:c` together list nothing.
  - The existing `co_author:` assertion changed from the set {mine, theirs} to the exact list [the `co_author:c` story, theirs, mine], because the test now creates a third story holding that name. This makes it stricter, not looser.
  - Mutation-checked on `ownership.list_filters`. Each mutant ran against the slice alone (`pytest tests/integration/test_i_21.py::test_i_21__ownership`), on a fresh container database holding only the slice's rows. The sources were restored afterwards.
    - M1, the principal clause dropped: passed the old slice; fails the new one (`editor:c` also lists the `editor:d` story).
    - M2, the name clause dropped: passed the old slice; fails the new one (`editor:c` also lists the `co_author:c` story).
    - Two more, beyond the verifier's. M3 matches the name and the principal on different relations (two `EXISTS`). M2b drops the name only from the any-principal `name:` form. Both passed the old slice. M3 fails the new one at `editor:d`, and M2b at `editor:`.
  - `test_ownership.py::test_principals_and_names_are_validated` is unchanged. Its filter check is about splitting `editor:team/ana:1` at the first colon, and under the real filter it gives the same answer whatever ran before. Its order-dependent catch of M1 is no longer needed.
  **Why:** The verifier showed that M1 and M2 passed every test unless the nothing-gates matrix ran first. In the old data, each relation name had one principal and each principal one name. The fresh owner keeps the `owner=` assertions unchanged. M3 is the third way to get the halves wrong, and the same 2×2 grid catches it at no extra cost. Cost if wrong: two stories, two relations and five list requests more in one test.
  **Issue:** #104
- **Decision (drift):** The moderation hook calls the consumer with httpx2. httpx2 was already in the runtime closure through openai, and is now declared in `[project].dependencies` (and dropped from the dev group). Pillow 12.3.0 and python-multipart 0.0.32 were added with `uv add`. Pillow declares `MIT-CMU`, so `[tool.epistrel.gates.licenses].allow` gains `MIT-CMU` and `HPND`, as the Discretion says; both are permissive and Apache-2.0-compatible. The test hook is a stdlib `ThreadingHTTPServer`, following the pattern of `tests/model_support.py`.
  **Why:** The drift note: httpx is not in the lock, and respx does not intercept httpx2. `check-deps` and `check-licenses` pass. I built the image under a throwaway tag (since removed), and Pillow imports there. Cost if wrong: the dependency's name.
  **Issue:** #105
- **Decision (drift):** An unknown or retired `content_rating_id` is 422 `rating_retired` (`reason` `unknown` or `retired`, field `metadata.content_rating_id`), through `require_assignable`. It is checked once before the hook and again, `FOR SHARE`, in the commit transaction, which stores the rating's current version. Only an omitted `content_rating_id` is a `validation_error`.
  **Why:** #50 deliberately kept one code for unknown and retired ratings. The pass-1 `validation_error` for unknown would contradict it.
  **Issue:** #105
- **Decision (drift):** The media settings are a `MediaSettings` section of `DeploymentSettings`, every leaf prospective:
  - `media_allowed_types`: lowercase `type/subtype`, unique, 1–50;
  - `media_max_bytes`: 1 B–10 GiB, default 50 MiB;
  - `media_moderation_webhook_url` and `public_base_url`: absolute http(s), ≤ 2048, default null;
  - `media_moderation_timeout_ms`: 100–60000, default 5000;
  - `media_sweep_interval`: 60 s–7 days, default 3600.

  They are in `SettingsPatch`, and `NULLABLE` gains the two URLs. `MediaSettings` also joins `config_changes.SECTIONS`; without it, `/admin/settings` would read the media values back as their defaults.
  **Why:** Deployment settings live in the `deployment_state.settings` jsonb, not in columns. While adding the section I found that `SECTIONS` already misses `RetentionSettings` and `WorkerHealthSettings` (#71). That is out of scope, so it is filed as #315.
  **Issue:** #105, #315
- **Decision (drift):** `media_sweep_interval` stays a deployment setting, and it drives the recurring task:
  - `RecurringTask` gains `interval_setting`, and `timers.reconcile` takes that setting's value at every worker start-up.
  - A settings write moves the `recurring_tasks` row at once: its interval, and `next_run_at` no later than one new interval after the last run. The mapping is `core.settings_schema.RECURRING_INTERVAL_SETTINGS`.

  **Why:** The AC says "every `media_sweep_interval`", and pass 1 puts it under `/admin/settings`. Reconcile only ran at start-up and wrote code constants, so a setting it ignored would only change after a restart, or never. Cost if wrong: one optional field and one helper.
  **Issue:** #105
- **Decision (drift):** `subject_references` is created in #123's full shape:
  - `story_id` is nullable, with a CHECK that a row names a story exactly when `scope = 'story'`;
  - a unique constraint on `(subject_kind, subject_id, record_table, record_id, field)` with `NULLS NOT DISTINCT`;
  - indexes on `(subject_id, scope, story_id)` and `(record_table, record_id)`.

  So the table joins `STORY_TABLES` and `STORY_TABLES_NOT_COPIED`, and `determinism.ORDER` (story-scope rows only). S-DELETE plants a story-scope row, because there is no story writer before #123. `record_table` is `media_assets`, as the AC writes it, not the schema-qualified name #123's pass 1 proposes.
  **Why:** The Discretion says "#123's full shape", and the drift note lists what that requires. The AC is what `/n8-verify` reads. Cost if wrong: #123 qualifies the name with one `UPDATE`.
  **Issue:** #105, #123
- **Decision (drift):** The parts are:
  - `test_c_15__media_upload`, `part("M3", "FR-MEDIA-1; FR-API-6")`, in `test_c_15.py`;
  - `test_i_17__media_sweep`, `part("M3", "FR-MEDIA-2")`, in `test_i_17.py`;
  - `test_i_21__media_retire`, `part("M3", "FR-MEDIA-2")`, in `test_i_21.py`.

  `test_media_moderation` is unmarked and lives in `tests/integration/test_media_assets.py` with the other detailed tests, including the sweep's, as the planned artifact says. The database-free tests are in `tests/test_media_units.py`. No unmarked docstring names an FR id. `check-lexicon` flagged three part bodies, and I reworded rather than amend the lexicon:
  - C-03 (an M2 part) said `upload`, so its media expectations moved to the module constant `MEDIA_ANSWERS`;
  - the I-17 slice said `clock.now()`, and now uses `datetime.now(UTC)`;
  - the I-21 slice had a local `import`, and now imports at module level.

  **Why:** A plain `test_media_sweep` maps to no §15 row. "clock" (M6) and "import" (M7) are lexicon terms that here mean the system clock and a Python import, not those capabilities. Cost if wrong: renames only.
  **Issue:** #105
- **Decision (drift):** Signing:
  - `config_key_missing` (503) gets a core-side `SigningKeyMissing`.
  - Both signers use an HKDF-SHA256 subkey of `EPISTREL_CONFIG_KEY`: `media-url` for content URLs (#107's planned info string; HMAC of `GET|<path>|<exp>`, unpadded base64url, 30 s skew) and `media-moderation-hook` for hook requests.
  - The new `epistrel moderation-secret` command prints the hook subkey in hex, and exits 78 without a valid key.
  - Deviation: `X-Epistrel-Signature` is `sha256=` + the HMAC of `"<X-Epistrel-Timestamp>." + body`, not of the body alone.

  **Why:** The drift note: a consumer must never need the master key, which also decrypts the stored model API keys. Binding the timestamp means a captured request cannot be replayed under a fresh timestamp. Cost if wrong: a consumer who verifies the body alone must add the prefix.
  **Issue:** #105, #107
- **Decision (drift):** Invariant 4 is unchanged: `GET /media/{id}/content` still needs the service credential, and the hook fetches `content_url` with it. A valid `exp`/`sig` (checked against `/media/{id}/content`) only adds access to an upload that is still staged, waiting for the hook. The staged file is `<root>/tmp/<asset id>`, and its type is sniffed again for the response. A missing, invalid or expired signature falls through to committed content (404 while the upload is staged). `content_url` is `public_base_url` plus the signed path. When `public_base_url` is null, the URL is the signed path alone, relative to Epistrel's root, rather than a refusal.
  **Why:** The signed-URL exemption is #107's invariant-4 amendment and the owner's round-two decision. Pulling it forward would mean editing CLAUDE.md, adding an ADR and an Ad-hoc entry for #107's work. A hook that only matches hashes does not need the URL at all. Cost if wrong: an operator sets `public_base_url`.
  **Issue:** #105, #107
- **Decision:** The upload route parses the multipart stream itself, with python-multipart's `MultipartParser`, instead of FastAPI's `UploadFile` as pass 1 says. The rules:
  - the parts are `metadata` (JSON, ≤ 256 KiB) and then `file`, and nothing may follow;
  - each part has at most 8 headers of 4 KiB each;
  - a declared `Content-Length` above `media_max_bytes` plus a 272 KiB envelope is 413 before the file is read;
  - the file's bytes are counted while they stream to `<root>/tmp`.

  OpenAPI documents the body through `MULTIPART_BODIES` (the `MediaUpload` component). C-01 excludes `upload_media` from generation and adds hand-written malformed cases. `_body_problems` reads a multipart body's `metadata` component.
  **Why:** FastAPI parses a form body before any dependency runs. With `UploadFile`, an unauthenticated client's whole upload would be spooled to disk before the 401, and the size could only be checked after the fact. Cost if wrong: a route with an `UploadFile` parameter.
  **Issue:** #105
- **Decision:** An upload's request hash covers the metadata and the bytes' SHA-256, size and MIME type, so the bytes are staged before the `Idempotency-Key` is claimed. The work is split around the claim:
  - before it: the metadata, size and type checks, which are pure checks of the request;
  - after it: the rating check, the probe, the hook and the commit, so a replay never calls the hook again.

  The commit is one transaction under `pg_advisory_xact_lock(0x4D454449, <first 32 bits of the checksum>)`. That two-key space is separate from the one-key space of `DEPLOYMENT_LOCK_KEY`. The transaction inserts the row, indexes it, records the `control` operation and, last, renames the staged file into place: read-only, or dropped when the blob already exists.
  **Why:** The same key with other bytes must be 409 `idempotency_conflict`. Mutation-checked: dropping the SHA-256 from the hash fails `test_upload_is_idempotent`. A commit that fails after the rename leaves a blob no row names, and the sweep removes it after an hour.
  **Issue:** #105
- **Decision:** The sweep runs in three steps:
  - **Rows.** Each purge is its own transaction: lock the row (`FOR UPDATE SKIP LOCKED`), re-check its references under the lock, `drop_entity` (a `deleted` ownership event by `system`), `drop_references`, then the row.
  - **Bytes.** A blob is deleted only under its checksum lock, and only when no row of any state names it: at once after a purge, and after an hour for any other blob nothing names.
  - **Staging.** Staged files older than an hour are removed. A live asset younger than 24 hours is never purged, and candidates are never touched. Each run handles at most 20 batches of 500.

  References are discovered through `MEDIA_REFERENCE_SOURCES` entries of `(table, jsonb column, jsonpath)`. The catalog draft and version sources use `strict $.** ? (@.type() == "string")`, which matches an asset id wherever a later story puts it. Tests plant references in #115's planned `public_card.images` shape.
  **Why:** Querying the records, never a counter, is what the AC requires. A path-agnostic source keeps #115's shape free, and over-matching keeps an asset, which is the safe direction. A new pause point, `media.sweep.before_purge`, proves the re-check under the lock. Mutation-checked: without the re-check, `test_a_reference_added_mid_sweep_keeps_the_asset` fails. The pre-lock reference query only saves work, so no test can see it.
  **Issue:** #105, #115
- **Decision:** Files and probes:
  - The job lives at `orchestrator/jobs/handlers/media_sweep.py`, beside the other recurring sweeps and registered in `handlers/__init__.py`, not at `orchestrator/jobs/media_sweep.py`. `Engine.start_workers` binds its store.
  - The sweep's logic is `core/media/sweep.py`, the hook's HTTP exchange `core/media/moderation.py`, and the probes `core/media/probe.py`.
  - Pillow reads image headers. `ffprobe` is taken from `PATH`, with `-protocol_whitelist file` and a 10 s timeout; a failed probe leaves the values null. #107 points it at `ffmpeg_path` and installs ffmpeg.
  - The content route sends `ETag` (the SHA-256), `Cache-Control: private, max-age=31536000, immutable` and `Content-Length`, but not pass 1's `Accept-Ranges: bytes`: #107 adds that together with real ranges.

  **Why:** That is the handlers' convention. Advertising ranges that the route ignores would mislead video players. Cost if wrong: moving files.
  **Issue:** #105, #107
- **Decision:** The routes:
  - `GET /media` lists `live` assets by default and `retired` ones on request. `state=candidate` is 422. Filters are `owner`, `relation` (`ownership.list_filters`) and `kind`; `limit` is 1–200 (default 50); `cursor` is opaque (base64url of `[created_at, id]`), newest first. The answer is `{items, next_cursor}`.
  - `DELETE` always answers 204. A second delete records nothing. The asset keeps its relations until the sweep purges it.
  - The ownership fixtures gain `Kind.retires`, and `test_deleting_an_entity_drops_its_relations` now asserts retire semantics for media. This changes an existing test because FR-MEDIA-2 requires the behaviour.
  - Media joins `KINDS` and `CALLS`, and the nothing-gates matrix compares the content route's bytes.
  - `CONTENT_FIELDS` gains `caption`, `negative_prompt`, `generation_metadata` and `reason`.

  **Why:** Pass 1 names the list filters, and #110's keyset is still to come. The matrix and the delivered-type guard must cover every owned type.
  **Issue:** #105, #110
- **Decision:** The media root:
  - `Settings.media_root` (`EPISTREL_MEDIA_ROOT`) defaults to `var/media`. The image sets `/var/lib/epistrel/media`, created owned by uid 10001, and compose mounts the `media` volume there. `/var/` is gitignored.
  - The app's lifespan and `epistrel serve`'s preflight both prove the root writable, and refuse the start with exit 78 otherwise.
  - Test apps default to a per-process temporary root (`tests/auth_support.MEDIA_ROOT`). The fault harness's child and X-07's spawned workers get their own roots (Rule 3).

  **Why:** Pass 1 asks for a fail-fast start-up. A full gate run showed that without these roots, the fault harness's child engine created `var/media` under the repository.
  **Issue:** #105
- **Decision (Rule 1):** The start-up writability probe is now unique per process (pid plus random). Previously every process wrote and removed the same `.writable` file. X-07's two spawned workers start on one media root at once, and one of them exited 78 with `FileNotFoundError` when the other unlinked the shared probe. The regression test is `test_processes_prepare_one_root_at_once` (8 threads × 50 checks); mutation-checked, the shared name fails it.
  **Why:** An API and its workers sharing a volume start together.
  **Issue:** #105
- **Decision:** These mutations were checked, each run against the story's tests with the sources restored byte for byte afterwards. Each one fails a test:
  - no hook call;
  - a malformed answer accepted;
  - bytes deleted while a row still names them;
  - candidates swept;
  - retired assets listed;
  - retired assets referenceable;
  - retired content refused;
  - no size limit;
  - subjects optional;
  - the type unchecked;
  - generation metadata dropped;
  - one blob per asset;
  - no subject index;
  - no re-check of references under the lock;
  - a shared probe name.

  **Why:** Under the batched gate, a story's correctness rests on its own tests until group A's full run.
  **Issue:** #105
- **Decision:** #105 verification fixes, round 1 (Rule 1). Each sweep run now goes on where the previous one stopped, so the sweep frees what nothing uses however many rows and blobs there are.
  - **Rows.** A batch now holds only assets nothing references. `assets.unreferenced` is the reference test as an anti-join (`NOT EXISTS` over each source whose table exists), applied before the batch's `LIMIT`. Before, every run began at the oldest eligible row, and referenced live assets older than a day filled all 20 × 500 places; nothing newer was ever examined.
    - The per-batch `referenced` pre-check is gone, since the anti-join does that work. The re-check under the row lock stays.
    - This replaces the sweep decision's line above, "The pre-lock reference query only saves work, so no test can see it": the progress test now sees it.
    - Postgres plans the anti-join as one hash anti-join per batch (probe on a scratch database: 5,000 assets and 400 drafts, 5 ms).
  - **Blobs.** `list_checksums(after=)` lists in ascending checksum order. The store keeps a sweep cursor, the last checksum a run examined, in `<root>/.sweep-cursor` (written by rename). The next run starts after it, and a run that reaches the last blob clears it.
  - The `BlobStore` port gains the ordering and `sweep_cursor`/`set_sweep_cursor`. #107's S3 adapter implements them with `ListObjectsV2`'s `StartAfter` and a small object.
  - Regression test: `test_each_run_goes_on_where_the_last_stopped`, with `BATCH` 2 and `MAX_BATCHES` 1 (monkeypatched). Three referenced assets older than a day must not keep a newer retired one from being purged in the first run. A blob nothing names, sorting after the named ones, must be deleted in the second run. Mutation-checked: it fails without the anti-join, with the cursor not read, and with `list_checksums` ignoring `after`. The store's ordering and cursor have a unit test.
  **Why:** I chose the anti-join over carrying a `(created_at, id)` resume point between runs. A resume point needs somewhere to live: a new `recurring_tasks` column is a schema change to an M2 table (Rule 4), and a job's `result` gets purged and does not exist when `sweep()` is called directly. The anti-join needs no state, and every place in a batch becomes a purge. For blobs, a random start shard would only probably reach every blob, and an in-memory cursor resets at every restart. The cursor file sits with the blobs it indexes and is shared by every process on the root. Cost if wrong: one file in the media root, and two port methods for #107.
  **Issue:** #105, #107
- **Decision:** #105 verification fixes, round 1. `test_i_17__media_sweep` now proves "no asset row of any state" (AC4) with two more pairs of identical bytes.
  - A retired asset that a catalog version references, and its unreferenced retired twin.
  - A candidate, and its retired twin.

  After the twins are purged, both blobs remain, and the version's retired asset still serves its content. The part's first sweep now purges 4 assets instead of 2, because the part plants more; no assertion was loosened. Mutation-checked: `collect_blob` counting only `live` rows, or ignoring `candidate` rows, fails the part. Both mutations passed every test before.
  **Why:** These are the verifier's cases. They sit in the I-17 part, the milestone's evidence for FR-MEDIA-2, rather than in a detailed test. Cost if wrong: none; the part only gained assertions.
  **Issue:** #105
- **Decision:** #105 verification fixes, round 1. The moderation hook's content URL is now proven short-lived (AC2).
  - `test_media_moderation` checks that `exp` is 5 minutes after the signing moment. The bounds are `time.time()` read before and after the upload.
  - `test_the_hook_reads_the_staged_bytes` freezes the injectable time source. The staged content still serves at `exp` + 30 s (the skew) and is 404 one second later.
  - The unit test checks `sign_path`'s default lifetime.

  Mutation-checked: a one-year lifetime fails all three tests, and a signer that never expires fails the staged-bytes test.
  **Why:** The tests use the literal 5 minutes, not the module constant, so a changed constant cannot pass them.
  **Issue:** #105
- **Decision:** #105 verification fixes, round 1. New pause point `media.upload.before_commit` (`pauses.MEDIA_UPLOAD_BEFORE_COMMIT`). It sits in `assets.upload` after `store.put` and before the commit, while the checksum's advisory lock is held.
  - `test_the_sweep_waits_for_an_upload_of_the_same_bytes`:
    1. Retire an asset, then upload its bytes again. The upload's `put` keeps the existing blob, and the upload is held at the point.
    2. Start a sweep that purges the retired asset.
    3. Wait until `pg_locks` shows a session waiting on a checksum lock (`classid = LOCK_CLASS`), then release the upload.
    4. Assert that no blob was deleted, the bytes are there, and `GET /media/{id}/content` is 200 with them.
  - Mutation-checked: a no-op `lock_checksum` fails it, because the sweep deletes the bytes under the uncommitted row. Removing the pause point also fails it (the hold times out). Five repeated runs passed.
  **Why:** I chose the purge path because it is the realistic race: the row is purged, then its bytes are collected at once. Waiting on `pg_locks` instead of sleeping makes the race deterministic. Cost if wrong: one no-op call per upload outside tests.
  **Issue:** #105
- **Decision:** #105 verification fixes, round 1. `test_uploads_index_their_subjects` now names the character as a whole word in the negative prompt and expects that row (`generation_metadata.negative_prompt`, `mentioned`). The non-matching `x{name}y` case moved to the second upload's negative prompt. Mutation-checked: dropping the field from `INDEXED_FIELDS` fails it.
  **Why:** This is the coverage line's registration. Before, no test could tell whether the field was indexed.
  **Issue:** #105
- **Decision:** #105 verification fixes, round 1 (discovered work). #105 does not register #108's scenario sources or #119's story-snapshot sources. I filed that handoff as #316 (`needs-triage`) and cross-referenced it on #105.
  - #316 asks #108 to register `catalog.scenario_catalog.draft` and `catalog.scenario_versions.content`, and to call `assert_referenceable` in its saves.
  - It asks #119 to register `public.character_snapshots.content` and `public.stories.scenario_snapshot`, to add the I-17 slice "a media asset referenced by a story survives deletion of its catalog entry", and to decide whether genesis copies must pass `assert_referenceable`.
  - The `MEDIA_REFERENCE_SOURCES` docstring now names #316.
  **Why:** The work is out of #105's scope. It also cannot land early: `public.stories` already exists without `scenario_snapshot`, and a registered source whose column is missing fails every sweep. The discovered-work rule allows a new issue, not edits to #108 or #119. Cost if wrong: if #108 or #119 lands without the registration, the sweep purges assets only they reference. #316 is the guard against that.
  **Issue:** #105, #108, #119, #316
- **Decision:** #105 verification fixes, round 2. The subject-reference scan now reads catalog characters' aliases as well as their names, as the Discretion line says ("catalog character names and aliases"). The first build scanned names only. It deferred aliases to #123 in `core/erasure/index.py`'s docstring, and no entry here recorded that deviation.
  - `_catalog_terms` adds the strings of `metadata.aliases` (`ALIASES_PATH`) from each catalog draft and from every version. That is where #115's plan puts FR-CAT-2's aliases, and #123's fixture alias comes from there too.
  - Until #115, no draft can hold the field (`CharacterDraft` forbids extra keys), so the scan finds nothing there. An `aliases` value that is not a list, and an entry that is not a string, are skipped.
  - #123 still adds the story side's aliases and name overrides, which #105's catalog-scope rows do not use.
  - Test: `test_uploads_index_alias_mentions`, on a scratch database. It plants the shape straight into a draft and a version row, as the sweep tests plant `public_card.images`.
  - Mutation-checked: the test fails with no aliases read, with the draft's or the versions' aliases dropped, without the string check (the upload fails) and without the list check (an object's key is indexed).
  **Why:** I followed the Discretion line rather than only logging the deferral, which was the verifier's other option. With the read in place, a caption, alt text or prompt that names a character only by alias is indexed from the day #115 lands. Otherwise it would stay unindexed until #123's scan and rebuild. I chose the explicit path over a search for any `aliases` key, because #115's opaque `visual_identity.adapter_params` could carry such a key, and its strings would name nobody. Cost if wrong: if #115 stores aliases somewhere else, the read finds nothing. The planted test would still pass, so the gap would reopen silently until #123. #115 must keep `metadata.aliases`, or move `ALIASES_PATH` and the test's planted shape along with it.
  **Issue:** #105, #115, #123
- **Decision (drift):** Every "#110" in #107 means the S3-compatible adapter, as the drift note says. The planner's S-id substitution turned "S3" into "#110", the catalog listing story, which has nothing to do with object storage. #107 depends only on #105.
  **Why:** The text means the S3 adapter in every place. Cost if wrong: none.
  **Issue:** #107, #110
- **Decision (drift):** The test store is MinIO RELEASE.2025-07-23T15-54-02Z. The image is `bitnamilegacy/minio:2025.7.23-debian-12-r5`, pinned by digest (`sha256:6dabb4a2…`) as `tests/conftest.py::MINIO_IMAGE`, next to `PG_IMAGE`. Its `minio` binary runs directly (`MINIO_ENTRYPOINT`), with no Bitnami setup scripts. The planned `minio/minio` tag could not be used.
  - At execution, `gh api repos/minio/minio/releases/latest` resolved `RELEASE.2025-10-15T17-29-55Z`.
  - Docker Hub no longer has `minio/minio`: the API answers "object not found", and `docker manifest inspect` is denied. Quay has no tag for that release either.
  - Bitnami's legacy registry has the newest MinIO release that still has an image. Its setup scripts start and stop the server once before serving, so a readiness check could pass and then fail.
  - The `minio` session fixture fails, never skips, without Docker. Pulling by tag plus digest works through docker-py; I checked by removing the local image and pulling again.
  **Why:** The AC names MinIO through testcontainers, and this is still MinIO. A different S3 server (SeaweedFS, LocalStack) would change what the test proves. Bitnami's legacy images get no updates, which is acceptable for a pinned test service. MinIO is AGPL, so it is a test and development service only. Cost if wrong: move the pin when an official image returns.
  **Issue:** #107
- **Decision (drift):** MinIO for local development is a separate override file, `docker/compose.minio.yml`, not a profile in `docker-compose.yml`. X-07 and C-10 pin the default stack's services to `engine` and `db`. The override runs the tests' pinned image and creates the bucket in a one-shot `minio-bucket` service; the engine waits for it to finish (`service_completed_successfully`). It sets the engine's `EPISTREL_MEDIA_S3_*` settings, publishes MinIO on 127.0.0.1:9000 (the presign endpoint), and labels its secrets dev-only. `test_compose.py` checks all of this. A trial `up minio-bucket` created the bucket and exited 0.
  **Why:** This is the drift note's option. A bucket created by the setup scripts would hit the same stop-and-start race as above. Cost if wrong: one file.
  **Issue:** #107
- **Decision (drift):** The signed-URL exemption is built as a third router:
  - `GET /media/{media_id}/content` and a schema-less `HEAD` twin move to `routes.media.content_router`.
  - That router is on `routes.signed_router`, which `create_app` includes only in `api` and `all` modes (`include_signed`). Its guard is `api.auth.require_credential_or_signed_url`.
  - `auth.SIGNED_ROUTES = {("GET", "/media/{media_id}/content")}` names the route. `unguarded_routes` accepts the signed guard on that method and path and nowhere else.
  - The guard's rules:
    - a recognised credential passes, whatever `exp`/`sig` say;
    - a signature of `GET|<path>|<exp>` that has not expired passes;
    - a signature that does not match the URL is 403 `signature_invalid`;
    - a matching signature past `exp` plus the 30 s skew is 403 `url_expired`;
    - neither credential nor signature is the credential's 401.
  - The OpenAPI document marks the route `security: [{HTTPBearer: []}, {}]`.
  - `test_route_guard.py` gains bite tests: the guard on another path or method is reported, and the route with no guard is reported. It also gains the behavioural 401/403 test and the worker-mode check.
  - C-02 needs no change: its worker-mode assertion still holds.
  - The OpenAPI builder's rules key on `SIGNED_ROUTES`.
  - CLAUDE.md invariant 4, the exempt-route sentence in docs/02 §9 and the README are reworded. ADR-152 amends ADR-149: ADR-149 gets an Amended-by line, a *[refined by ADR-152]* marker and the indexes.
  **Why:** This is the drift note's design. `unguarded_routes` reads the route table and cannot see query parameters. C-02 forbids the route on `public_router`, which worker mode serves. A signed URL authenticates one asset's bytes only. Cost if wrong: one router.
  **Issue:** #107, #44
- **Decision (drift):** The six new Engine operations (`get_media_url`, `open_upload`, `get_upload`, `put_upload_part`, `complete_upload`, `abort_upload`) get C-03 parity steps, with no `PARITY_EXCLUDED` set:
  - a settings write first sets `public_base_url`;
  - `Rest` gains a raw `content` body;
  - `normalise` blanks a signed URL's `exp` and `sig`, since the signature covers the real asset id.
  `MEDIA_ANSWERS` gains `get_media_url`. The new `SESSION_ANSWERS` pins the session steps' statuses. The session check is written without the word "upload" in C-03's body, because C-03 is an M2 part and "upload*" is an M3 lexicon term.
  **Why:** `parity_gaps` has no exclusion list, and #128 is in group C. The drift note's first option keeps the parity rule whole. #128's plan of excluding "part uploads, signed URLs" is now unnecessary. Cost if wrong: none; the steps are small.
  **Issue:** #107, #128
- **Decision (drift):** The part `PUT` is in neither `_keyed` nor `CODECS`, so it stores no idempotency result. `get_request_context` still mints and echoes an `Idempotency-Key` for it, as for any PUT. Its 200 body is `PartStoredBody(DeploymentMutationResponse)`: `{n, size, received_parts, config_revision, idempotency_key}`. Open is 201, complete 201 and abort 204, each with a codec.
  **Why:** A part sent again replaces the first, so the request is idempotent by itself. `response_base_problems` still requires the revision base on a non-204 mutating answer. Cost if wrong: one codec.
  **Issue:** #107
- **Decision (Rule 1):** The video probe now works. #105's ffprobe invocation passed `-nostdin`, an ffmpeg-only option that ffprobe refuses ("Option not found"). So every real probe failed, and every video's width, height and duration were stored as `null`. #105's tests used a stand-in script that ignores its arguments, so nothing caught it.
  - The flag is gone; stdin was already `DEVNULL`. The input is now `file:<path>`, matching the extraction.
  - Checked against Homebrew's ffmpeg 9.0.2 and against Debian's 7.1.5 in the built image; both refuse `-nostdin` from ffprobe.
  - Regression test: `test_the_real_tools_read_and_frame_a_video` probes a real 2000×1000 one-second video and expects `(2000, 1000, 1000)`. C-23 also asserts `duration_ms == 1000`.
  **Why:** The Discretion's "ffprobe fills `duration_ms`" was impossible without the fix. Cost if wrong: none.
  **Issue:** #107, #105
- **Decision:** The store port has one local base. `store.LocalStaging` owns the media root's staging area and the sessions' part directories (`<root>/uploads/<id>/`). `LocalFilesystemStore` and `core/media/s3.py::S3BlobStore` both extend it.
  - New port methods: `open_range`, `presign` (local: `None`), `put(..., content_type=, assembled=)`, `begin_parts`, `part_staging`, `store_part`, `stored_parts`, `assemble`, `reopen_parts`, `finish_parts`, `abort_parts` and `stale_part_uploads`.
  - A part always streams to a local temp file first, so its size and optional SHA-256 are checked before the store sees it. The local store renames it into place; the S3 store sends it with `UploadPart`.
  - Completion assembles into the new asset's staging file while hashing. On S3 that means `CompleteMultipartUpload`, then streaming the object back. #105's whole path then runs on that file: sniff, rating, probe, hook, poster.
  - On S3, `put(assembled=)` copies `uploads/<id>` to the checksum key with s3transfer's managed copy, which also handles objects over CopyObject's 5 GB limit.
  - S3 calls map a missing object to `FileNotFoundError` and every other failure to 503 `media_store_unavailable`, logging the operation and the error code only.
  - `core/media/stores.py::make_store` picks the adapter and imports boto3 only for S3.
  **Why:** ffprobe, ffmpeg, Pillow and the hook's `content_url` all need a local file, and hashing needs the bytes anyway. One staging path keeps a completion's checks identical to a direct upload's. Cost if wrong: one local copy of a completed upload, removed when the call ends.
  **Issue:** #107
- **Decision:** On S3, a refused completion re-opens the session. The refusal may come after the multipart upload was completed (a checksum mismatch, a hook refusal, a poster failure). `reopen_parts` then starts a new multipart upload, copies each part's range back from the assembled object (`UploadPartCopy`), deletes the object, and records the new `UploadId` on the row. `stored_parts` answers `None` while an assembled object exists, so a retry reuses it.
  `test_a_refused_completion_leaves_the_session_open[s3]` covers this: the session completes after a hook refusal, and after a wrong part is sent again.
  **Why:** The plan says a refused completion leaves the session open with its parts. On S3 a completed multipart upload takes no more parts, so "send the wrong part again" would otherwise fail. Cost if wrong: copies inside the store, only on that path.
  **Issue:** #107
- **Decision:** The `media_upload_sessions` columns go beyond the Discretion's list. They add:
  - `created_by` (the opener);
  - `s3_upload_id` (pass 2's "UploadId in a column");
  - `media_id` (the asset a completion created, also in `GET`);
  - `created_at` and `updated_at`;
  - `closed_at`, which with `idempotency_result_window` decides when the sweep deletes a closed row.
  `owner_principal` is the asset's owner: `owner`, or the opener. Each `CHECK` is text: `state`, a lowercase SHA-256, `size > 0` and `part_size > 0`. One index covers `(state, expires_at)`. `GET` also answers `part_count`.
  Migration 0023 also:
  - adds `media_assets.poster_media_id` with its index;
  - extends `ck_media_assets_source` with `extracted`;
  - adds `deployment_state.media_store`, `CHECK` `local`/`s3`, backfilled `local` when any asset exists, since only the local store existed before.
  Its downgrade turns `extracted` rows into `uploaded`, so the old check holds again. I-08 and the drift test pass.
  **Why:** The AC needs these facts, and the table is the one the story names. A new column the story's own AC need is not Rule 4. `media_store` is the pass-2 Discretion's column. Cost if wrong: migration edits before M3 merges.
  **Issue:** #107
- **Decision:** A completion holds the session's row `FOR UPDATE` for its whole run, hook and ffmpeg included, so a second completion waits and then finds the session closed (409 `upload_closed`). It is waiting `FOR UPDATE`, not `NOWAIT`, so a completion that meets a part `PUT` in flight waits for it instead of being refused.
  - A part `PUT` takes the same lock to record its part.
  - The planner's "completion is idempotent on `upload_id`" is read as: keyed under `Idempotency-Key` with material `{upload_id}`. A retry under the same key replays the asset; another key gets 409 `upload_closed` (pass 2).
  - New pause point `media.complete.locked`. `test_a_second_completion_waits_and_finds_it_closed` waits until `pg_stat_activity` shows the second completion blocked on a lock, then expects `[201, 409]`.
  **Why:** A `NOWAIT` would refuse a completion that only met a part upload. The row lock serialises without a fourth state. Cost if wrong: one connection held while the hook answers (at most `media_moderation_timeout_ms`).
  **Issue:** #107
- **Decision:** Open, complete and abort each record one committed `control` operation by the requester (`core/control.py`), as #105's upload and retire do. A part records none. Opening checks what it can at once:
  - the size against `media_max_bytes` (413);
  - the declared type against `media_allowed_types` and `kind` (415);
  - the rating (422 `rating_retired`);
  - a named `poster` (422 `media_retired` or `poster_invalid`);
  - for a video with no poster, that ffmpeg is there (422 `poster_required`).
  Completion checks the size and type again, against the settings then in force and the sniffed bytes.
  **Why:** This is the attribution pattern the other catalog and media writes follow. A session that can never complete should be refused when it is opened. Cost if wrong: one row per write.
  **Issue:** #107
- **Decision:** How the poster is delivered and checked.
  - A `poster_file` part must follow `file`. `UploadForm.poster` reads it only after the main file has streamed; a REST caller passes the bound method, a library caller an `UploadFile` or `None`.
  - A `poster_file` on an image, or beside `poster`, is 422 at `body.poster_file`. `poster` on a non-video is 422 at `body.metadata.poster`, or `body.poster` for a session.
  - A supplied poster must be `live` (`assert_referenceable`: 422 `media_retired`) and an image. A non-image is the new code 422 `poster_invalid` (`reason: not_image`).
  - An extracted poster skips the `media_allowed_types` check. Epistrel makes it, always PNG, and it is sniffed to be one.
  - The hook sees the video first, then a poster stored with it.
  - A poster's alt text is truncated to the 500-character limit.
  - The poster row's reference to its video is a source with `path = None` in `MEDIA_REFERENCE_SOURCES` (`public.media_assets.poster_media_id`): a column that holds an id itself. `referenced`/`unreferenced` handle both shapes.
  - Retiring a video retires its `live` poster in the same transaction, whatever the poster's source, as the Discretion says.
  **Why:** These are the AC's two supply routes, plus the Discretion's rules. An allowed-types list governs what consumers upload, not what Epistrel derives. Cost if wrong: a deployment that drops `image/png` from its allowed types still gets PNG posters.
  **Issue:** #107
- **Decision:** `media_multipart_threshold` applies to the whole direct request: the file plus any `poster_file`. It is checked by declared length before reading, or counted as the bytes stream. #105's `media_too_large` pre-check is unchanged (`media_max_bytes` plus the envelope), so a body carrying a poster may hold at most that in total. The new deployment settings, all prospective, are:
  - `media_signed_url_ttl`: 1–86400 s, default 300;
  - `media_multipart_threshold`: 1 KiB–10 GiB, default 8 MiB;
  - `media_upload_ttl`: 60 s–7 days, default 24 h.
  **Why:** "Uploads larger than the threshold use sessions" reads as the request. The two pre-checks stay simple. Cost if wrong: a poster near the limit on a video near the limit needs a session.
  **Issue:** #107
- **Decision:** How ranges and conditions are split. The API parses the headers: `routes.media.parse_range`, one `bytes` range of at most 18 digits a side, and `entity_tags`. Core applies the semantics: `assets.open_content(byte_range=, if_none_match=, if_range=, with_bytes=)`, with `ByteRange.resolve` clamping.
  - `If-None-Match` matching weakly, or `*`, is 304 before any range.
  - `If-Range` applies a range only when it is exactly the current `ETag`; a weak tag or a date gives the whole file.
  - `HEAD` is a second route with no schema entry (`get_media_content_head`): headers only, no blob opened.
  - Content answers (200, 206, 304, `HEAD`) carry `Accept-Ranges: bytes`, `Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers: Accept-Ranges, Content-Range, Content-Length, ETag`. Error answers do not.
  - Streams use 64 KiB chunks on both stores; S3 reads go through `asyncio.to_thread`.
  **Why:** One Engine call per handler, and a `HEAD` or a 304 should open nothing, which matters for an S3 `GET`. Documenting `HEAD` would duplicate the `operationId`, and C-02 skips it anyway. Cost if wrong: library callers get typed conditional reads.
  **Issue:** #107
- **Decision:** `GET /media/{id}/url` works as follows.
  - `ttl` is accepted from 1 to 2³¹−1 and cut to `media_signed_url_ttl`.
  - On the local store it needs `EPISTREL_CONFIG_KEY` (503 `config_key_missing`) and `public_base_url` (503 `config_missing`, `details.setting`), as the Discretion says. This differs from #105's hook `content_url`, which falls back to a relative path.
  - `expires_at` is the signed `exp` locally, and the moment of presigning plus the TTL on S3.
  - A retired asset gets a URL; a candidate or an unknown asset is 404.
  - FastAPI's header validation errors are now reported at `headers.<name>`, as docs/02 §9 documents, instead of `header.<name>`. `X-Part-SHA256` is the first FastAPI-validated header.
  **Why:** A relative URL is useless to a player outside Epistrel's own origin. The header path was a defect of the generic validation handler, which until now had no header parameter to report. Cost if wrong: one setting an operator must set.
  **Issue:** #107
- **Decision:** `/health`'s `checks` gains `ffmpeg` and `ffprobe` (`ok`/`missing`). `FfmpegTools.status()` checks each tool's `-version` once, 5 s at most, and keeps the answer. The lifespan calls it at start-up; an app that was never started checks on the first `/health` or video. Neither value changes the status. The ffmpeg seam is `Engine.posters` (`PosterExtractor`: `status`, `probe_video`, `extract`), and C-23's spy replaces it on the app's Engine. `test_health.py`'s exact body now expects `ok` for both, since the suite needs ffmpeg; a new test covers `missing`.
  **Why:** These are the Discretion's "probes once at start-up, ffprobe reported separately" and "a PosterExtractor protocol on app state". Cost if wrong: none.
  **Issue:** #107
- **Decision:** Existing tests changed because the story changes the behaviour they pin. No assertion was loosened.
  - The media sweep's exact result dicts (test_media_assets, I-17) gain the session pass's three counts, at 0.
  - `MEDIA_COLUMNS` gains `poster_media_id`.
  - `test_list_filters_and_pages`' video names one of the listed images as its `poster`, since every video needs one. Its listing assertions are unchanged.
  - I-21's `stub_settings` gains a config key. `_blanked` blanks a signed URL's `exp`/`sig`. `Call.cleanup` unsets the `public_base_url` the URL case sets.
  - `CALLS` gains `HEAD` content and `GET` url, so the nothing-gates matrix covers them.
  - C-01's live app gets a config key and a seeded `public_base_url`, which is put back after each generated settings write: a generated write may unset it, and the 503 would trip `not_a_server_error`. C-01 also seeds an upload session, and `_body_problems` accepts a binary `application/octet-stream` body. No operation was excluded from generation. A full C-01 run passed: 73 passed.
  **Why:** The AC require each behaviour; these are the smallest test changes that hold them. Cost if wrong: none.
  **Issue:** #107
- **Decision:** The C-23 part is `test_c_23__delivery`, `part("M3", "FR-MEDIA-3")`, parametrised over `local` and `s3`, in `tests/integration/test_media_delivery.py`, the file the plan names.
  - #115's `test_c_23__optional_images` (FR-CAT-4) must join this file: traceability counts a test ID per milestone in one file.
  - The ffmpeg complement runs inside the part, on a second scratch deployment whose `EPISTREL_FFMPEG_PATH` names nothing.
  - check-lexicon: the part avoids M4's "extraction" and M6's "clock". The skew constant is aliased `SKEW_S`, and `frozen_at` is imported by name.
  - Detailed tests in the same file cover:
    - RFC 9110 ranges and conditions on both stores;
    - the local store's signed URLs;
    - the threshold;
    - part checks;
    - a session from open to close;
    - checks at open;
    - concurrent completions;
    - refused completions on both stores;
    - a multipart video's poster;
    - the session sweep;
    - posters, and an extracted poster the hook refuses;
    - posters in the sweep;
    - the store-kind start-up check through `epistrel serve` (exit 78) and a missing bucket;
    - the S3 store itself.
  - `tests/test_media_units.py` covers the range parser, `Signer.check`, `ffprobe_beside`, the real tools, extraction failures and the local session parts.
  **Why:** The plan names this file. Cost if wrong: renames only.
  **Issue:** #107, #115
- **Decision:** The image builds and still runs as uid 10001, with Debian trixie's ffmpeg 7.1.5. I built it under a throwaway tag and removed it afterwards. It measured 763 MB, against 301 MB for the last `epistrel:dev` build. Most of the difference is ffmpeg's codec libraries, which `--no-install-recommends` still brings, against the plan's "~150 MB"; boto3 and botocore add a little. Inside the image, `boto3` imports, the probe's new invocation reads a video, and the extraction command writes a PNG.
  **Why:** AC5 needs the image to run as uid 10001, and the size is a fact the owner should see. Cost if wrong: a smaller ffmpeg build would be a separate decision.
  **Issue:** #107
- **Decision:** Hand-offs, not fixed here:
  - `exports/` is a reserved key prefix on both stores, but the `BlobStore` port has no object API for it. #126 adds the put, open and delete of `exports/<id>.jsonl` it plans.
  - #128's planned `PARITY_EXCLUDED` and binary-route exclusions are unnecessary for #107's routes: they have C-03 steps and C-01 generation.
  - M8's #260 plans a candidate content route served through "#107's mechanism" with an Epistrel-signed local URL. Invariant 4 now exempts media content only, so that route would need its own ADR and invariant change. That plan may be stale; see the Ad-hoc entry below.
  **Why:** The discovered-work rule: out of scope, and not a defect today.
  **Issue:** #107, #126, #128, #260
- **Decision:** I mutation-checked the core behaviours, applying each mutation to the source and restoring it byte for byte afterwards. Each of these fails a test:
  - ranges ignored (C-23);
  - signatures that never expire (C-23);
  - the assembled checksum unchecked (C-23);
  - videos stored without a poster (C-23);
  - the signed guard accepted on any route (`test_route_guard`);
  - a retired video's poster left live (`test_posters`);
  - expired sessions never swept (`test_the_sweep_ends_stale_sessions`);
  - part sizes unchecked (`test_parts_are_checked`);
  - a forged signature accepted (`test_signed_urls_on_the_local_store`).
  One mutation survived: dropping `parse_range`'s explicit comma check. The range pattern already refuses a second range, so the check was redundant and is removed.
  **Why:** Under the batched gate, a story's correctness rests on its own tests until group A's full run. Cost if wrong: none.
  **Issue:** #107
- **Decision (Rule 1):** `store_kind_problem` passes, logging the exception type only, when the database cannot be reached at start-up. The config-key check already does this. The per-story gate caught the bug: `test_health_reports_database_error_and_keeps_serving` starts the app against a closed port and expects it to keep serving 503 `/health`. My first version raised from the lifespan instead, so the app never started. That test's exact body also gains `ffmpeg`/`ffprobe` (`ok`), as the drift note foresaw.
  **Why:** An unreachable database is `/health`'s to report (NFR-REL-3). The check runs again at the next start. Cost if wrong: a start against a down database records nothing, which is harmless.
  **Issue:** #107

## Ad-hoc — 2026-10-12

- **Change:** Project invariant 2 gains an explicit exception (CLAUDE.md, ADR-151): "ffmpeg is a GPL command-line tool invoked as a separate process for poster extraction; it is not linked and not a Python dependency". The image's runtime stage installs Debian's `ffmpeg` and CI installs it on the runner. `check-licenses`, over the Python runtime closure, is unchanged and passes.
  **Why:** This is the owner's round-one decision on #107, applied by #107 for FR-MEDIA-3's poster extraction: "ffmpeg (GPL) may be installed in the image and run as a separate process by subprocess; it is never linked or imported, so invariant 2 holds — recorded as an `## Ad-hoc` decision and an ADR line".
  **Affects:** No other plan names ffmpeg or the license allowlist. The image is much larger (763 MB against 301 MB), which the release and smoke plans (M1, C-10) absorb without change.
- **Change:** Project invariant 4 is reworded (CLAUDE.md; ADR-152 amends ADR-149). Every REST route requires the service credential except `GET /health`, `/metrics`, `/openapi.json`, `/docs` and `/redoc`, and media content fetched with a valid signed URL: `GET /media/{id}/content` with an unexpired `exp` and `sig`. Without them the Bearer credential is still required. The route-table guard (`tests/test_route_guard.py`) checks the one signed route over `api.auth.SIGNED_ROUTES`.
  **Why:** This is the owner's round-two decision on #107, applied by #107.
  **Affects:** M8's #260 plans `GET …/image-candidates/{cid}/content`, read through an Epistrel-signed `…/content?exp=&sig=` URL "through #107's mechanism". The reworded invariant does not exempt that route. It needs its own exemption (a new ADR and an invariant change) or a credential-only design, so `/n8-replan M8` should look at #260. #255 (reference images as #107 signed URLs) and #126 (export content behind the credential, with no signed URL) fit the new wording unchanged.

## /n8-exec M3 — 2026-10-12 (continued)

- **Decision:** The M3 section continues here, after the two `## Ad-hoc` entries #107 recorded for its owner-decided invariant changes. Later stories append below.
  **Why:** #107's owner lines ("(user, round one)", "(user, round two)") say both decisions are recorded as `## Ad-hoc` entries. M2 logged its planned amendments (#44, #71) as decisions, to keep the run's section last. Appending the entries and reopening the section keeps both: the drift ledger has the entries, and the M3 section is still last. Cost if wrong: one heading.
  **Issue:** #107
- **Decision (drift):** The parts follow docs/04 §15. `test_c_15__scenario_lifecycle` (`part("M3", "FR-SCN-1,2,10")`) and `test_c_15__scenario_field_classes` (`part("M3", "FR-SCN-2")`, the build-time check and its untagged-fixture complement) are in `tests/conformance/test_c_15.py`. `test_u_26__roles_and_limit` (`part("M3", "FR-SCN-3,4")`) is in the new shared `tests/test_u_26.py`, which #111 and #119 join. The detailed tests are unmarked in `tests/integration/test_scenario_catalog.py`, and their docstrings name no FR id. The parts take every field name from `tests/scenario_support.py`, because the scenario's fields carry later milestones' lexicon terms (the clock, predicates, `reactive`, `backfill_conflict_policy`). check-parts, check-lexicon and check-traceability pass (155 parts, 0 lexicon violations).
  **Why:** §15 maps FR-SCN-3 and FR-SCN-4 to U-26 (M3) only, so the planned C-15 marker for FR-SCN-1,2,3,4,10 fails check-traceability, and one test ID's M3 parts must share one file. Cost if wrong: renames only.
  **Issue:** #108
- **Decision:** #103's lifecycle moved to `core/catalog/service.py` as `CatalogService`, an abstract base with hooks: `stored` (validate and normalise), `checked` (database checks inside the write's transaction), `version_values`, `recorded`/`forgotten` (what a write or delete keeps beside the rows), `project` and `envelope`. `CharacterCatalog` and `ScenarioCatalog` subclass it. It is generic by subclassing, not by the plan's type parameter (`CatalogService[CharacterDraft]`). `characters.py` keeps every public name, message and behaviour: `merge_patch`, `Projection`, `PublishRequest`, `Published`, `PublishReport`, `ReportEntry`, `VersionSummary` and `CardRating` now live in the service and are re-exported, and `DRAFT_MAX_BYTES` is still read at call time, so `test_drafts_are_capped`'s monkeypatch works. The Validator protocol is `async validate(draft, ctx) -> PublishReport`, where `ctx` is `ValidationContext(conn, settings, ports)`. `Engine` now passes `self.ports` to both publishes. The report type stays #103's `PublishReport`; #111 brings `ValidationReport` and replaces both validators.
  **Why:** This is the pass-1 Discretion's "#103's service becomes `catalog/service.py` … refactored onto it". The kinds differ in their hooks, not in their types, so a type parameter would add nothing. All #103, #104 and #105 tests pass unchanged, except the truncate assertion below. Cost if wrong: hook signatures change before #111 and #115 build on them.
  **Issue:** #108, #103, #111
- **Decision:** The document groups are the pass-1 groups, with these deviations:
  - Relationships live on each role (`cast.roles[].relationships`, the AC's role shape). There is no `cast.relationships` list.
  - `recommendation {engine_profile_id, capabilities}` is its own group (pass 1), not part of `story_settings_defaults` (the first planner line).
  - "Retention options" is `audit_retention_days`, the story's only retention setting at HEAD. M7's compaction options will be an additive key.
  - `schema_version: 1` is tagged `configuration`/`creation_only`, because genesis reads it to interpret the snapshot and every field needs a class.
  - `scenario_schema_classes()` paths carry their group: `cast.roles[].public_label`.

  Classes follow FR-SCN-11 and docs/02 §4.10:
  - a role's `id` is `public_card` (the card shows it);
  - its `kind`, `required`, `fill`, `character` and eligibility are `configuration`/`creation_only`, and its participation is `configuration`/`story_setting`;
  - pre-scheduled events, starting locations, name overrides and the truth of a planned reveal are `knowledge_seed`;
  - the opening's narration and `roles_present` are `starting_state`;
  - predicates, closed sets and rules are `story_rule`.
  **Why:** The AC fixes the role shape, and FR-SCN-11 names the class of each field. Cost if wrong: a field moves between groups before #119 and #122 snapshot it.
  **Issue:** #108, #119, #122
- **Decision (drift):** `story_settings_defaults` is a dedicated core model, `StorySettingsDefaults`, with `extra="forbid"`, so unknown names are 422.
  - M2's keys: `turn_concurrency` (`reject`/`queue`), `default_output_viewpoint` (a role id the save checks; genesis resolves it to `character:<id>`) and `audit_retention_days` (1–3650).
  - The later keys, typed with docs/03's value sets: `interaction_mode` (`sequential`/`simultaneous`, FR-MULTI-5), `player_authority` (`own_character`/`co_author`), `ambiguous_retcon_timing` (`include_if_possible`/`ask_author`) and `backfill_conflict_policy` (`last_mention`/`first_mention`/`ask`).
  - `policies` are `ScenarioPolicy` items `{name, applies_to?, rule, age_condition?, message}` built from `core.policies.model`'s `Rule`, `Target` and `AgeCondition`. They have no `effect` and no scope: #119 writes them as `story` policies.
  - `required_providers` is a list of distinct names.
  **Why:** This is the drift note's correction. `StoryPolicyItem` is the orchestrator's and core cannot import it. Cost if wrong: a value set widens later, which is additive.
  **Issue:** #108, #119
- **Decision:** These save rules go beyond the plan (Rule 2, input validation). Each is a 422 `validation_error` at its field, and every finding is listed in `details.errors`:
  - Role ids `world`, `self`, `acquaintances`, `perceivable` and `public` are reserved, because a seed's holder and audience use them with another meaning. `npc` and `player` stay free for #116.
  - The player role must be `open`. FR-CAT-6 says a catalog character never plays the player.
  - An explicit `character.version: null` is refused. Omitting it means floating; the plan keeps `null` for companions, which #116 adds.
  - Every reference is checked: seed holders (a role or `world`), `relationships[].to_role` (never the role's own id), role and opening starting locations, `roles_present`, a reveal's `allowed_roles`, and the default viewpoint.
  - Location, entity and seed ids are unique, and a scenario holds at most 200 seeds across all lists.
  - Labels are trimmed and lower-cased, at most 30 of up to 40 characters. The pattern admits upper case, so the OpenAPI schema never refuses what the server accepts (C-01's negative-data check).
  - A non-finite number (`1e400` parses as Infinity) in a free-form structure is refused. A JSON-mode dump would otherwise store it as `null` without a word. `ScenarioPatch.document()` keeps the parsed values for the same reason, since `null` in a merge patch removes a key. The same defect exists in media generation metadata, out of scope; I filed it as #317.
  **Why:** Each rule closes an ambiguity that a later story would otherwise inherit, or a silent data change. Cost if wrong: a rule is relaxed later, which is additive.
  **Issue:** #108, #116, #317
- **Decision:** Seed ids and seed presence:
  - Every seed without an `id` gets a ULID when it is stored, and one sent back keeps its id. A save that sends `roles` again without the seed ids mints new ones.
  - A draft seed may leave out `holder`, `audience` and `disclosure` (#116 pass 2).
  - Publish requires all three on every seed, with a `field_required` report entry at each gap, as well as `public_card.name`. It also re-runs the save rules.
  This goes beyond the plan's "publish in #108 is schema-level only". The AC says audiences are "validated for presence in M3", and #116's plan says they are required at publish.
  **Why:** Without a publish check, a scenario with seeds that have no audience could be published before #111 lands. Cost if wrong: #111 replaces these entries with its `seed_incomplete`.
  **Issue:** #108, #111, #116
- **Decision:** `PATCH /catalog/scenarios/{id}` takes `ScenarioPatch {draft_revision, <group>: object | null}`, with the groups as free JSON objects. A group merges into the draft by RFC 7386, an array replaces the stored one, and `null` resets a group. The merged draft is validated as a whole, and errors land at `body.<path>`, wherever the field came from. I did not derive typed patch models for every nested group.
  **Why:** Typed patch models would double the schema surface (a `…Patch` for every nested object) and still mis-type RFC 7386's `null`. C-01's negative-data check only needs the schema to be no stricter than the server, and it is not. Cost if wrong: typed patch models can be added later without changing the wire format.
  **Issue:** #108
- **Decision (drift):** The starting-cast limit's settings, as the drift note says:
  - `max_characters_at_start_default` (16) and `max_characters_at_start_ceiling` (null) are `deployment_state` columns. They are listed in `DEPLOYMENT_COLUMNS`, carried by `DeploymentState`, declared prospective on `DeploymentSettings`, and in `SettingsPatch`; the ceiling is in `NULLABLE`.
  - The scenario value, the default and the ceiling are all bounded 1–1000 (`CAST_LIMIT_MAX`, int32-safe).
  - The ceiling is checked against the merged document on every create and save, even one that does not touch the value. A scenario stored above a since-lowered ceiling is refused on its next save until its value is lowered.
  - `effective_cast_limit(scenario, request, deployment)` and `CastLimits` live in `scenario_models` (pure), for #119. The order is the request's override, else the scenario's value, else the default, capped by the ceiling.
  - Also fixed: `PUT /admin/settings`'s description now names every key that accepts `null` (#105's URL keys were missing).
  **Why:** The AC says "saving a scenario above the ceiling → 422". 1000 is generous next to the 32-role cap, and no consumer value should reach an INTEGER column's edge. Cost if wrong: a bound or the save-time check is relaxed.
  **Issue:** #108, #119
- **Decision:** Table details:
  - `scenario_catalog.companion_of` is a foreign key to `character_catalog` with `ON DELETE CASCADE` (#124's "companion cascades"), and it is indexed.
  - The pin `(pinned_character_id, pinned_character_version)` is a composite foreign key to `character_versions` with `ON DELETE SET NULL` (#124), indexed and guarded by `ck_scenario_versions_pin_complete` (both columns or neither). Both are NULL until #116.
  - Scenario versions get their own trigger function, `catalog.epistrel_scenario_versions_immutable()`, honouring the same `epistrel.erasure` setting. Under that setting, a character delete's cascade and the pin's SET NULL pass it.
  - The downgrade deletes scenarios' `entity_relations` and `subject_references` rows, so nothing dangles, and keeps their ownership events and audit rows as history.
  - Scenario drafts are capped at 1 MiB (characters keep 256 KiB).
  **Why:** A scenario holds a world, up to 32 roles and up to 200 seeds. #116 still writes the companion's own audit row and `deleted` event when it deletes a companion explicitly. Cost if wrong: a foreign-key action changes in a migration before M3 merges.
  **Issue:** #108, #116, #124
- **Decision:** The projections:
  - `card` is #103's envelope (`id, version, listing_status, status`) plus `companion_of` (the pass-1 Discretion), plus AC5's `name, public_description, media, labels, content_rating, roles[{id, public_label, setup_description, kind, fill}]`.
  - `media` keeps the AC's `cover`/`gallery`/`videos`, each item `{media_id, alt_text, caption}`, rather than a flat list. The alt text is the asset's; a video's caption is the scenario's, else the asset's.
  - `content_rating` is `{id, version_id, name}`: the version a published version recorded, or for a draft the rating's current version.
  - `full` is the envelope plus every group of the document as stored, the shape a PATCH targets, rather than the card's flattened fields plus the private ones.
  **Why:** A flat media list would lose which image is the cover. Editors read `full` to patch it. Cost if wrong: an additive card key, or a reshaped `full` before a consumer depends on it.
  **Issue:** #108
- **Decision:** References (#316's #108 half, cross-referenced on #108, not on #316):
  - `catalog.scenario_catalog.draft` and `catalog.scenario_versions.content` join the static `MEDIA_REFERENCE_SOURCES` list. I did not use `register_reference_source` at import, because a sweep in a process that never imported `scenarios` would miss them.
  - A create or save checks only the media ids it adds (`assert_referenceable`, 422 `media_retired`). An asset retired after it was added does not block unrelated edits.
  - A changed `content_rating_id` must be assignable (422 `rating_retired`). Publish records the rating's current version through `rating_version`, which is 422 `rating_retired` if the rating was retired since.
  - Drafts and versions are indexed with `index_references`: `record_table` `scenario_catalog` (`record_id` the id) and `scenario_versions` (`<id>@<n>`). The fields are every prose field, and the declared subjects are the role characters' catalog ids. A delete drops the scenario's rows.
  **Why:** FR-MEDIA-2 forbids purging an asset a draft or version references. The Discretion has #108 call `index_references` "for drafts/versions". The publish-time rating check is the minimum needed to record a version id. Cost if wrong: #111 moves the rating and media checks into its report.
  **Issue:** #108, #316, #111, #123
- **Decision:** Existing tests changed because the story changes the behaviour they pin; no assertion was loosened:
  - `test_catalog_tables_have_the_planned_shape` reads only the two character tables. The scenario tables have their own shape test.
  - A plain `TRUNCATE catalog.character_versions` is now refused by PostgreSQL with `0A000`, because the scenario pin references the table. The trigger's `EPI01` is now asserted with `TRUNCATE … CASCADE`, so both refusals are asserted (I-01 already uses this form for `events`).
  - `test_every_delivered_type_is_covered` no longer lists scenarios as undelivered.
  - `test_i_21__companion` now runs on real scenarios in its own scratch database. `companion_of` is set by SQL until #116, and the stand-in table is gone. Y's `updated_at` is compared before and after, because real rows are created "now". The test also asserts that Y2's timestamp does not move, which is stricter.
  - The ownership fixtures gain the scenario kind and 11 `CALLS`, so the nothing-gates matrix covers scenarios.
  - C-01 seeds a published scenario, excludes `delete_scenario` from generation, and adds three malformed cases. Its full run passed: 84 passed, up from 73.
  - C-03 gains 16 scenario steps, and their statuses are checked against `ENTRY_ANSWERS` through `Step.group`, since C-03 is an M2 part and "scenario" is an M3 lexicon term.
  - `test_u_18` pins the two new settings as prospective.
  **Why:** These are the smallest changes that hold each new behaviour. Cost if wrong: none.
  **Issue:** #108, #104
- **Decision:** I mutation-checked the parts, applying each mutation and restoring the sources from a scratch copy byte for byte afterwards. Each mutation fails the named test:
  - no ceiling check (U-26);
  - a `locked` role without a character allowed (U-26);
  - a second player role allowed (U-26);
  - the age range dropped when stored (U-26);
  - the world description shown as the card's public description (C-15 lifecycle);
  - a role's seed text shown in its card setup description (C-15 lifecycle);
  - an untagged `conditions` field (C-15 field classes).
  **Why:** Under the batched gate, a story's correctness rests on its own tests until group A's full run. Cost if wrong: none.
  **Issue:** #108

## Ad-hoc — 2026-10-13

- **Change:** docs/02 §4.10's `scenario_roles` table is replaced by the roles inside each scenario draft and version document (jsonb), and docs/02 is amended to match. A role carries the fields the table planned: kind, required, fill mode, public label and setup description, default or locked `character_id@version`, eligibility filters, role seeds with holder, audience and disclosure, relationships, role-scoped State values, starting location, default participation role and name overrides. `scenario_catalog` gains `companion_of`, and `scenario_versions` gains the companion pin `(pinned_character_id, pinned_character_version)`. Applied by #108.
  **Why:** #108's planner line: "Seeds and roles are jsonb documents inside the version content", and "docs/02 §4.10's `scenario_roles` table is replaced by jsonb roles — logged as an `## Ad-hoc` entry and a docs/02 amendment in this story". A version is immutable and copied whole into a story's snapshot at genesis, so its roles belong with it.
  **Affects:** No other plan names `scenario_roles` (searched: only #108). The stories that read roles already plan to read them from the document or the snapshot: #110 (search over role public labels), #111 (validation), #116 (companion slots fixed by the validator, "not by DB constraint (roles are jsonb)"), #119 and #122 (genesis from `stories.scenario_snapshot`) and #124 (redaction in versions and drafts). No `/n8-replan` is needed.

## /n8-exec M3 — 2026-10-12 (continued)

- **Decision:** The M3 section continues here, after the `## Ad-hoc` entry #108's planner line asked for, as #107 did for its entries. Later stories append below.
  **Why:** This keeps the drift ledger entry and keeps the run's section last. Cost if wrong: one heading.
  **Issue:** #108
- **Decision:** #108 verification fixes, round 1. The gap was real, and it was a gap in test coverage only. The code already did what AC4's "defaults to `deployment_state.max_characters_at_start_default`" requires: `config_changes._state` reads the column, `DeploymentState.as_settings()` carries it, `CastLimits.of` maps it and `effective_cast_limit` falls back to it. But every test resolved the limit from a hand-built `CastLimits`, and the deployment's own default was only ever 16. No source file changed. `test_u_26__roles_and_limit` gained these assertions after its existing ones, none of which changed:
  - The deployment's stored settings, read the way a scenario save reads them (`CastLimits.of` over `read_settings`), equal `CastLimits()`: 16 and no ceiling.
  - `PUT /admin/settings` sets the default to 14, and `GET /admin/settings` returns 14 with no ceiling. Three stored scenarios (`max_characters_at_start` null, 20 and 12) resolve through the stored settings to 14, 20 and 12.
  - A ceiling of 13 reads back as `CastLimits(default=14, ceiling=13)` and resolves them to 13, 13 and 12.
  - The `finally` restores the default to 16 as well as the ceiling to null.
  - Mutation-checked on `tests/test_u_26.py`, each mutation applied with the sources restored from a scratch backup afterwards (byte-identical at the end, by SHA-1). Each fails the part: the verifier's M7 (`CastLimits.of` ignores the stored default) and M7b (`_state` reads the default as 16), and two of mine: a scenario's own value never below the default (`max(scenario, default)`) and a ceiling that does not cap the default. Mine both passed the pre-fix tests over the verifier's set (U-26, C-15, `test_scenario_catalog`: 28 passed each).
  **Why:** U-26 (M3) is FR-SCN-4's only test in docs/04 §15, so the proof belongs in the part rather than in `test_the_ceiling_refuses_a_larger_scenario`. I chose 14 and 13 over the fix hint's 12 and 10 so that the same values also pin a scenario's own value below the default and a ceiling that leaves a smaller value alone; a default of 12 would equal the third scenario's 12 and prove neither. I added no production helper for the read: `ScenarioCatalog.checked` already reads the settings that way, the behaviour was correct, and #119 decides how story creation reads them. Cost if wrong: none to behaviour; at worst a stricter test.
  **Issue:** #108
- **Decision:** #108 verification fixes, round 2. Both gaps were real, and both were gaps in test coverage only. The code already stored every role field and every seed's holder, audience and disclosure as sent, and the card already showed each role's kind and fill. But no test compared those values with what was sent, and every role in a card fixture was `open`. The changes:
  - `COMPLETE` (`tests/scenario_support.py`) gains a role locked to a catalog character at version 3 (`brother`, formerly `open`) and a role pre-filled by default (`aunt`, new, with a floating version), so its cast has every fill mode and the player's role. Its seeds use all three disclosures (the brother's is `guarded`), and every seed but the world's names two audience tokens (levels, a role id and a named group). It also sets eligibility `content_ratings`, a seed's `kind` and `valid_time`, a canonical name override and a policy's `applies_to`.
  - `test_c_15__scenario_lifecycle` now asserts three things. The created draft equals the document sent (`document_of` against `stored_form`). Version 2's `full` read also equals it, with the edit applied. The card's roles equal `CARD_ROLES`, which is written out in full.
  - `test_u_26__roles_and_limit` asserts that `COMPLETE`'s whole cast is stored as sent, and that a role which leaves `required` out is required.
  - `test_card_and_full_projections` asserts `CARD_ROLES` on all four card reads and the whole document on both `full` reads.
  - Existing assertions changed only for the fourth role: `[CARD_ROLE_KEYS] * 3` became `* 4` in C-15 and in the detailed test, and C-15's label list gained "Aunt Ivy". None was loosened.
  **Why:** AC3 is FR-SCN-3, whose only part is U-26, so the cast's values are asserted there. AC5's "`full` adds every private field" is FR-SCN-10, and "a scenario shall carry" every field is FR-SCN-2. Both belong to C-15, so the whole document is asserted there. Cost if wrong: none to behaviour.
  **Issue:** #108
- **Decision:** The expectation, `stored_form(body)`, works in three steps:
  - It fills each field the body leaves out with the default the scenario models declare, and lower-cases labels. It runs no validator.
  - It takes the seed ids out.
  - `document_of` takes them out of the read, after checking that each stored seed has its own ULID.
  I rejected two alternatives:
  - Comparing with `ScenarioDraft.model_validate(body).model_dump()`. It would run the save's own validators, so it would have passed the verifier's M6 (`Seed._distinct` rewriting every audience).
  - A hand-written expected document. It would duplicate the fixture and break on every additive field.
  **Why:** Because only the declared defaults are read, a changed *value* cannot change the expectation too. The price is that a changed *default* is mirrored on both sides. So U-26 pins the one default the plan names: `required` defaults to true, and mutation M13 fails it. Cost if wrong: another default needs its own assertion.
  **Issue:** #108
- **Decision:** `COMPLETE`'s locked and default roles name fixed catalog ids (`LOCKED_CHARACTER`, `DEFAULT_CHARACTER`) that belong to no character. The alternative was a character each test creates and publishes. In M3 neither a save nor a publish checks a role's character, but #111's publish validation will (`character_missing`, `character_not_published`). So #111 must give this fixture real published characters, covering every test that publishes it: `test_c_15__scenario_lifecycle`, C-03's scenario steps (`SCENARIO = complete_scenario()`) and `test_card_and_full_projections`. `COMPLETE` also has no content rating, which #111's planned `content_rating_missing` may want too.
  **Why:** Real characters would turn C-03's static `SCENARIO` body into one built per run. That is a larger change to an M2 part and outside this round's gaps. The existence check is #111's, so #111 is where real characters become necessary. Cost if wrong: #111 edits one fixture and C-03's scenario step.
  **Issue:** #108, #111
- **Decision:** `scenario_models.card_roles` now takes the validated `ScenarioDraft` rather than a raw document. `ScenarioCatalog.project` calls it instead of building the list inline, so one function builds a card's roles. This was the first of the verifier's two options. Nothing else called the function and no plan names it, so changing its signature breaks nothing.
  **Why:** Two builders could drift apart, and `project` has already validated the document, so validating it again would be wasted. Cost if wrong: a future caller that holds a raw document has to validate it first.
  **Issue:** #108
- **Decision:** I mutation-checked round 2's assertions. A script applied each mutation and restored the sources from a scratch copy, and the sources were byte-identical at the end (by SHA-1). The runs covered `test_c_15__scenario_lifecycle`, `test_u_26__roles_and_limit` and `test_card_and_full_projections`.
  - Fail all three: the verifier's M6 (audience always `["public"]`), M9 (every disclosure stored `open`) and M10 (relationships, participation and name overrides dropped). Also mine: M12 (a role's character dropped), M15 (a seed's `kind` and `valid_time` dropped), M16 (an audience cut to its first token) and M17 (a `guarded` seed stored `secret`).
  - Fail C-15 and the detailed test: the verifier's M11 (every card role shown `npc`/`open`), and mine M14 (a default role shown `open`) and M18 (the player's role shown `npc`).
  - Fails U-26 alone, as designed: M13 (`required` defaults to false).
  M16 and M17 are why the fixture needs audiences with several tokens and a guarded seed. I did not re-run my own mutations against the pre-fix tests. The verifier's evidence covers M6, M9, M10 and M11 there.
  **Why:** Under the batched gate, the story's correctness rests on its own tests until group A's full run. Cost if wrong: none.
  **Issue:** #108
- **Decision:** I applied the owner's decision of 2026-10-12 on #106's AC-level drift (option A, "Amend #106 to the docs"; recorded on #106 and in the session's owner file).
  - Presets are ordinary profiles (FR-PROF-3, ADR-084). An edit makes a new version, copy and delete follow the usual rules, and `preset` records provenance only. The only refusal is deleting the deployment's default (409).
  - The Lite preset's budgets are `{model_calls: null, deadline_ms: 45000}`. `null` is NFR-PERF-6's formula (9 + G in M3). An explicit `budgets.model_calls` is the turn's total cap including the reserved guardrail calls G, as M2's `max_model_calls_per_turn` is, and it is refused below 1 + G; nothing is added on top of G. `deadline_ms` maps to `turn_deadline`. Both are documented on `Budgets`; #113 wires them into the turn.
  - The pass-1 Discretion sentence "G added on top, so the Lite preset's 2 replaces #69's 4" is superseded, because its premise was wrong.
  - No turn reads a profile before #113. P-09 and S-SELFPOLICE ran unchanged and pass (5 passed).
  - No `## Ad-hoc` entry is written here; the orchestrator writes it.
  **Why:** The owner decided. The AC text was amended in place to match FR-PROF-3 and NFR-PERF-6. Cost if wrong: none; this is the owner's call.
  **Issue:** #106
- **Decision (drift):** `stories.engine_profile_id` lands in #106's migration (`0025_engine_profiles`), not #113's.
  - It is NOT NULL and indexed, with a foreign key to `engine_profiles`. Every existing story is backfilled to `LITE_PRESET_ID` through a temporary server default that the migration then drops.
  - Story creation sets it to the deployment's default, read with the deployment row held `FOR SHARE`. A fork copies its source's (`_copy_story_row` copies every column).
  - `deployment_state.default_engine_profile_id` is NOT NULL with a foreign key, as the pass-1 Discretion says.
  **Why:**
  - The column is needed by AC4's delete warnings, AC4's "purge once no story uses it", and AC5. The drift note found a single additive step that meets them as written.
  - #113's AC1 will find the column in place.
  - The two foreign keys mean the purge can never remove a profile that a story or the default names.
  - Cost if wrong: #113 drops a foreign key or moves the backfill.
  **Issue:** #106, #113
- **Decision:** Shape of the module schema. `ProfileSettings` is one full model with every FR-PROF-2 module, every module off by default.
  - **Levels instead of flags.** A module with more than two levels carries its level, not the Discretion's `{enabled: bool, ...}`:
    - `perception.level`: `none`, `co_location` or `full`;
    - `verification.level`: `off`, `declared`, `extraction` or `entailment`;
    - `clock.level` and `states.level`: `off`, `prompt_only` or `on`;
    - `multi_character.renders`: `combined` or `per_character`;
    - `character_knowledge.testimony`: `off`, `model` or `defaulted`;
    - `pipeline`: `lite` or `structured`.
    On/off modules keep `enabled`.
  - **Verification** is a ladder. FR-PROF-2 says "plus …" and FR-PROF-4 says "at least declared". `extraction_mode` and `entailment_gate` belong to its levels.
  - **Contradiction checking** is a dial of the verification module, `verification.contradiction_checking`, with the capability name `contradiction_checking`.
  - **Repair and polish:**
    - `repair.max_attempts` is module `repair`, the structured repair ladder.
    - The polish pass is `repair.polish {enabled, max_regenerations}`. Its Lite form is M2's `guardrail_retry_budget` (0–5, default 2), as the drift note maps it.
  - **Hot-set budgets** (FR-MEM-8) belong to the `structured` level of the pipeline.
  - **Models** is `{purposes: {<purpose>: {model, params?}}, generation: {temperature, top_p, response_length}}`. FR-PROF-2 puts the generation defaults under Models, and a `generation` key beside the purposes would collide with the `generation` purpose.
  - **Capability names** are the Discretion's list plus `lite`, `contradiction_checking` and `repair`.
  **Why:**
  - A flag plus a level would accept contradictory pairs such as `enabled: false, level: full`.
  - M4–M6 only flip `DELIVERED` entries, as the Discretion wants.
  - Cost if wrong: these modules are undelivered, so their shape changes freely until M4.
  **Issue:** #106, #109, #113, #122
- **Decision (drift):** Profile bounds are widened to M2's:
  - `window_messages` 1–500;
  - `compress_threshold_tokens` 100–100,000;
  - `model_calls` 1–50;
  - `deadline_ms` 1–600,000. M2's `turn_deadline` is above 0 and at most 600 s, and the request floor of 0.5 s stays a request rule.

  The Lite seed's `compress_threshold_tokens: 6000` is AC1's value. It is three times M2's `lite_summary_max_tokens` default (2,000), which #113 maps it onto as the merge cap. #113 must decide whether 6,000 becomes the effective merge cap for every Lite story.
  **Why:** With the planned narrower bounds, #113's migration of stored M2 values would fail validation (drift note). Cost if wrong: a bound narrows later; nothing stored exceeds M2's.
  **Issue:** #106, #113
- **Decision:** How the settings are tagged.
  - `Tag` follows FR-PROF-6's lists: the pipeline, partitions, nested beliefs, perception, the verification ladder and `extraction_mode`, commitment, the clock, States and consolidation are data-producing; everything else is behavioral.
  - `ConfigClass` follows FR-CONC-15: verification, perception, partitions and the beliefs, commitment, the clock and States are interpretive; models, budgets, thresholds and memory are prospective. Every setting delivered in M3 is prospective, as the pass-1 Discretion says.
  - `Quality` follows FR-PROF-16: every setting is relevant except pinned memories (the Discretion's "not pins"; #109's complement uses `pins.max_pinned`).
  - No profile setting names a prompt-template version, because templates are files. #109 folds their hashes into the fingerprint (its own plan).
  **Why:** These are the planner's sources. `missing_tags` (U-28) refuses a leaf without exactly one tag of each kind. Cost if wrong: a tag moves before anything reads it (#109, #113).
  **Issue:** #106, #109
- **Decision:** Naming an undelivered module is refused at any value, its off value included (422 `invalid_profile`, with `details.undelivered` naming each setting). The pass-1 Discretion says a module named at its off value is accepted.
  - A level switch whose value picks an undelivered level is refused too (`pipeline: structured`).
  - `validate_profile` reads the names in the document. So #113 must validate merged sparse documents, never a full model dump. `profile_defaults`, seeded from Lite, names no undelivered key.
  **Why:**
  - The published schema (`additionalProperties: false`) has no place for such a key. Accepting it would let a request the contract forbids succeed: C-01's `negative_data_rejection`, FR-API-4.
  - AC2 reads "a profile naming an undelivered module or level is rejected".
  - Cost if wrong: one condition in `undelivered_settings`.
  **Issue:** #106, #113
- **Decision:** `POST /engine-profiles/{id}/validate` answers 200 `{valid: true, missing_dependencies: [], undelivered: []}` for a valid profile and 422 `invalid_profile` for an invalid one. The pass-1 Discretion's 200 `{valid: false, …}` is not used.
  - The body `{settings?}` is a dry run of an edit; without settings, the latest version is validated.
  - The route is flagged `mutating=False` (#103's `route_flags`): no `Idempotency-Key` and no revision base. The principal is still required, by method.
  **Why:** AC3 lists the validate route among those that "reject an invalid combination with 422 `invalid_profile`", and `/n8-verify` reads the AC. Cost if wrong: the 422 becomes a 200 body.
  **Issue:** #106
- **Decision:** `DEPENDENCIES` is a tuple of 26 rule objects, `DependencyRule {clause, module, levels, requires: (Requirement(module, levels), …), when}`. AC3 writes it as `{module: [requires...]}`, which cannot express:
  - perception other than `none`;
  - "in the structured pipeline";
  - a requirement at a given level.

  How the rules map to FR-PROF-4:
  - `FR_PROF_4` quotes the requirement's nine clauses verbatim. U-28 checks that each is in docs/03 and that each has a rule.
  - The module docstring lists the 26 rules. U-28 compares that list with `rule.describe()` one by one, and its count.
  - The `structured` clause gives one rule for each of the 16 modules it constrains. It exempts what the clause exempts, and also pinned memories (FR-LITE-3: under every profile) and the polish pass's Lite form (the Discretion's "repair.polish under lite is allowed").
  - Every rule is exercised on settings that switch its module on (`tests/profile_support.violating`), with a complement (`satisfying`).
  - `epistrel.testing.profiles.extra_rule` rebinds `schema.DEPENDENCIES` for C-17's delivered path. Validation reads the tuple at call time, and Core never imports the helper.
  **Why:** These are the pass-1 Discretion's rule objects, made checkable against the requirement's own text. Cost if wrong: renames only.
  **Issue:** #106
- **Decision (drift):** Core never imports the Orchestrator's purpose registry. `Engine.purposes` passes `Purposes(delivered, reserved)` from `orchestrator.models.registry` into every profile write and validation:
  - a delivered purpose may be routed;
  - a reserved purpose (or port) is reported in `undelivered` (`models.purposes.<name>`);
  - an unknown name is 422 `validation_error` at its key.

  The published schema documents the purpose key pattern, with `additionalProperties: false`. `GET /engine-profiles/schema` lists the delivered purpose names in `purposes`.
  **Why:** The `core-does-not-import-orchestrator-or-api` contract (lint-imports); the drift note's first option. Cost if wrong: none.
  **Issue:** #106
- **Decision:** Profile names are not unique per owner. The pass-1 Discretion says "unique per owner case-insensitively".
  **Why:**
  - FR-AUTH-5 transfers, including #117's offboarding to a curator, always succeed. A per-owner unique name would make a transfer to an owner who already holds that name fail with a constraint error.
  - Names are labels, as catalog names are; the id identifies a profile.
  - Cost if wrong: a partial unique index plus a rule for transfers.
  **Issue:** #106, #117
- **Decision:** Delete semantics.
  - A delete is always soft. The answer is 200 `{id, deleted_at, warnings, stories_using, scenarios_recommending, config_revision}`.
  - Warnings are `{kind: profile_in_use, story_id}` for each story on the profile, and `{kind: profile_recommended, scenario_id}` for each scenario whose draft or latest published version recommends it (#108's `recommendation.engine_profile_id`). Each list is capped at 1,000, with counts.
  - Deleting a deleted profile answers its `deleted_at` and records nothing.
  - A deleted profile still reads, lists with `include_deleted=true`, and copies. The Discretion's later "PUT/copy on a deleted profile → 422" contradicts its own "copying a deleted profile allowed", and the copy rule is kept. Only an edit, or naming the profile as the default, is 422 `profile_deleted`.
  - `profile_in_use` is a warning kind, never an error code (the Discretion drops it from the code list).
  **Why:** AC4 and FR-PROF-1 say "removes it from listings and from new assignment, with a warning naming the stories". A copy assigns nothing and is the way to recover a deleted profile's settings. Cost if wrong: one refusal in `copy_profile`.
  **Issue:** #106, #117, #119
- **Decision:** #71's `retention_sweep` gains `engine_profiles` (its result's new key), from `core.profiles.store.purge_deleted`.
  - It purges deleted profiles that no story uses and that are not the default, at most 1,000 per run.
  - Each purge is one transaction under `FOR UPDATE SKIP LOCKED`, re-checks that no story uses the profile, records its ownership `deleted` event by `system` (`drop_entity`), and deletes the row; the versions cascade.
  - Edits and deletes lock a profile `FOR NO KEY UPDATE`, which leaves free a story insert's foreign-key `FOR KEY SHARE` (#104's lesson).
  **Why:** AC4 says "a sweep (#71) purges the profile once no story uses it". The re-check under the lock is defensive: no M3 path makes a deleted profile newly used, and the foreign key is the backstop. Mutation-checked, removing the re-check survives every test, as expected; it stays so that one profile cannot abort a whole sweep with a foreign-key error. Cost if wrong: none.
  **Issue:** #106, #71
- **Decision (drift):** Deleting the deployment's default reuses `ConfigConflict`: 409, code `validation_error`, `details {id, reason: is_default}`, documented as `config_conflict`. The Discretion's new `profile_is_default` code is not added.
  **Why:** This is the drift note's option. The default rating (#48) and the default model configuration (#52) are refused the same way. Cost if wrong: one code added to `ERROR_STATUS`.
  **Issue:** #106
- **Decision (drift):** `default_engine_profile_id` is a column-backed deployment setting:
  - in `DEPLOYMENT_COLUMNS`;
  - prospective on `DeploymentSettings`;
  - in `SettingsPatch`, where `null` is refused;
  - pinned in U-18.

  A change records one `config_changes` row through `apply_settings`. An unknown or deleted profile is 422 `profile_deleted` (`reason: unknown|deleted`, field `body.default_engine_profile_id`), held `FOR SHARE` until the write commits. This mirrors `rating_retired`'s one code for unknown and retired.
  **Why:** This is the pass-1 Discretion's "changed via PUT /admin/settings (prospective, config_changes row)" and the drift note's pattern. Cost if wrong: a different code for unknown.
  **Issue:** #106
- **Decision:** Every profile write records one committed, story-less `control` operation by the requester (`core/control.py`): create, edit, copy and delete. An edit that changes nothing records nothing and makes no version.
  - A version bump writes no `config_changes` row. That is #113's AC3 ("a new profile version is a configuration change"), with its class-per-setting design, and the Discretion credits it to #113.
  - Bodies extend `DeploymentMutationResponse` with the unchanged `config_revision`. Codecs: create and copy 201, edit and delete 200.
  **Why:** This is #103's pattern: a refused write leaves no operation row (C-01's zero-row check). No story follows a profile before #113, so a version is not yet configuration any operation runs under. Cost if wrong: #113 adds the change row, as planned.
  **Issue:** #106, #113
- **Decision:** A profile version stores its settings sparse: the document as sent, normalised (`exclude_unset`). It is validated strictly (JSON types, no coercion), with every omitted setting at the model's default. The model's defaults equal Lite's delivered values until #113's `profile_defaults`.
  - Responses show what the version sets.
  - The Lite seed names every delivered setting, and each delivered purpose routed explicitly to `/admin/model-config` (`null`).
  - A `PUT` makes a version only when the canonical JSON of the settings differs.
  - The response also carries `current_version` beside `version` (AC1's column).
  **Why:** This is the drift note's interim merge base. Strict validation is needed because `strict_json_body` checks only that `settings` is an object; without it, `true` would pass for an integer (mutation-checked). Cost if wrong: responses can merge the defaults later.
  **Issue:** #106, #113
- **Decision (drift):** The parts follow docs/04 §15. All are in `tests/integration/test_engine_profiles.py` except the I-21 slice:
  - `test_u_28__schema`, `part("M3", "FR-PROF-2,3,4")`;
  - `test_c_16__crud`, `part("M3", "FR-PROF-1; FR-API-6")`;
  - `test_c_17__profiles`, `part("M3", "FR-PROF-2,4")`;
  - `test_c_17__dependencies`, `part("M3", "FR-PROF-4")`;
  - `test_i_21__profiles`, `part("M3", "FR-PROF-1; FR-AUTH-5")`, in `tests/integration/test_i_21.py`.

  #109's `test_c_16__two_stage`, `test_c_16__upgrade_unavailable` and #113's `test_u_28__effective` and `test_c_16__follow_latest` must join `tests/integration/test_engine_profiles.py`, not `test_two_stage.py` or `test_effective_config.py`, because traceability counts a test ID per milestone in one file.

  Module names come from `tests/profile_support.py`, built from the schema's own registry, rather than a JSON fixture; the purpose is the same lexicon rule. C-16 and C-17 run on their own scratch databases, because they change the default and the Lite preset. check-parts counts 160 parts; check-lexicon reports 0 violations; check-traceability passes.
  **Why:** The drift note: §15 maps FR-PROF-1 to I-21 and C-16, FR-PROF-3 to U-28 (M3), and C-17 (M3) to FR-PROF-2,4 only. Cost if wrong: renames only.
  **Issue:** #106, #109, #113
- **Decision:** C-17's M3 part is a pairwise covering array over the delivered modules' settings:
  - the rolling summary, pinned memories and the polish pass, each on and off;
  - a call cap or the formula;
  - a named length or a token cap.

  The array is greedy, and its pairwise coverage is asserted. Each configuration is validated and created, becomes the default, and a story created under it plays a turn. A configuration that picks an undelivered level is refused by validation and never runs.
  **Why:** docs/04 C-17. Before #113 no turn reads the profile, so "runs cleanly" is trivially true for now. The structure is in place for #113, which makes it meaningful. Cost if wrong: none.
  **Issue:** #106, #113
- **Decision:** Existing tests changed because the story changes what they pin; no assertion was loosened.
  - `test_every_delivered_type_is_covered`: personas are now the only undelivered type.
  - `test_deleting_an_entity_drops_its_relations`: each kind's delete status (`Kind.deleted_status`), because a profile delete answers 200 with its warnings.
  - `ownership_fixtures`: a profile `Kind` (`retires`, 200) and ten `CALLS`, so the nothing-gates matrix covers every profile route and compares read bodies.
  - C-01: a seeded profile and its `profile_id` path id. C-01's full generated run passed in the gate run (117 passed across C-01, C-02, C-03, the route guard, the response bases, the route flags and U-18).
  - C-03: 13 profile steps and `PROFILE_ANSWERS`.
  - U-18: the new setting pinned as prospective.
  **Why:** Each new behaviour needs these, and they are the smallest changes that hold it. Cost if wrong: none.
  **Issue:** #106, #104
- **Decision:** `GET /stories/{id}` does not show the story's profile yet. #113's pass-1 line adds `engine_profile: {id, version}`; the tests read `stories.engine_profile_id` directly, and the delete warnings prove the link through the API. The README gains an "Engine profiles" section and names the default in its Configuration section.
  **Why:** Choosing the field's shape is #113's job. Cost if wrong: one additive field.
  **Issue:** #106, #113
- **Decision:** I mutation-checked the behaviour, applying each mutation with a script that restored the sources byte for byte (SHA-1 checked). Each of these fails at least one of the story's tests:
  - undelivered names ignored;
  - no dependency fires;
  - the default deletable;
  - no delete warnings;
  - the deployment default ignored at story creation;
  - a new version on every edit;
  - the listing showing deleted profiles;
  - a copy keeping its source's owner;
  - a deleted profile assignable;
  - a deleted profile editable;
  - an edit storing the old settings;
  - the owner filter ignored;
  - the Quality tag optional;
  - the schema unpruned;
  - no `profile_recommended` warning;
  - an edit skipping validation;
  - validate made mutating;
  - a reserved purpose allowed;
  - lax (coercing) settings validation.

  The purge's re-check under its lock survives, as described above.
  **Why:** Under the batched gate, the story's correctness rests on its own tests until group A's full run. Cost if wrong: none.
  **Issue:** #106
- **Decision:** Filed #318 (`needs-triage`) and did not fix it. `tests/chaos/test_x_07.py::test_x_07__recurring` fails when it runs on its own, because the seeded recurring tasks fire beside its own. It passes in module order and in the full suite, and it reproduces at 87ce58e, before #106.
  **Why:** It is outside #106's scope; the discovered-work rule applies.
  **Issue:** #106, #318
- **Decision (verification round 1):** A fork of a story whose engine profile is deleted keeps that profile, the verifier's option (a). The fork is created (201) and runs on the profile's last version. A repeated delete of the profile names the fork among the stories using it, and the purge waits until no story uses the profile, forks included. Two options were rejected: moving the fork to the deployment default with a warning, and refusing it with 422 `profile_deleted`.
  - The behaviour is unchanged; `_copy_story_row` already copied the column. It is now pinned by `test_a_fork_keeps_its_sources_deleted_profile`: the fork is on the deleted profile, a turn plays on it, and the sweep keeps the profile until the source and then the fork are deleted. As a complement, a story created meanwhile gets the default. The earlier test `test_the_default_is_a_configuration_change` covered only a live source profile.
  - Documented in the `DELETE /engine-profiles/{id}` and `POST /stories/{id}/fork` descriptions (`docs/openapi.json` regenerated) and in the docstrings of `core/profiles/store.py`, `core/fork.py` and the retention handler.
  - The fork's answer carries no warning. No AC asks for one, and adding a field would change M2's fork contract.
  **Why:**
  - FR-STORE-15 makes a fork a full, independent copy of its story, and a story's profile is part of what is copied.
  - FR-RATE-1 uses the same words for a retired rating ("hidden from listings and from new assignment, kept for what already uses it") and adds "a fork copies its story's rating". FR-PROF-1's delete clause follows it, so a fork is a continuation, not a new assignment.
  - Moving a story to another profile is "assigning or changing a story's profile", which FR-PROF-7 runs as a warned two-stage operation, with FR-PROF-8's upgrade modes once profiles differ in data-producing modules. A fork that switched silently would skip that, and the story overrides it copies (FR-PROF-5) would then apply to a different profile. Refusing the fork would stop a story that FR-PROF-1 says "keeps running" from branching.
  - Cost if wrong: one override in `fork._copy_story_row` plus an assignment check, and the new test flips.
  **Issue:** #106, #113
- **Decision (correction):** The #106 `retention_sweep` entry above says "no M3 path makes a deleted profile newly used" and calls the purge's re-check under its lock merely defensive. Both statements are wrong.
  - A fork gives a deleted profile another story (the entry above).
  - A fork reads its source in one REPEATABLE READ snapshot without locking it. So a fork that began before its source was deleted can commit a story on the deleted profile after the sweep's scan found the profile unused.
  - The re-check under the lock is what keeps that profile. Without it, the purge's delete fails on `fk_stories_engine_profile_id_engine_profiles`, and the whole sweep run raises.
  - A new pause point, `profiles.purge.before_lock` (`epistrel.testing.pauses`, called in `purge_deleted` before each candidate's lock), lets `test_a_fork_committing_mid_sweep_keeps_the_profile` produce the race with the real fork:
    1. The fork is held after its snapshot through S-FORK-SNAPSHOT's `_select_operations` seam.
    2. Its source is deleted, and the sweep scans and stops at the point.
    3. The fork commits, then the sweep resumes and keeps the profile.
    4. Complement: once the fork is deleted, the sweep purges the profile.
  - The delete and the sweep run in fresh `contextvars` contexts, as separate requests and the job worker do. In the fork's context, the delete took the fork's `Idempotency-Key` call, and the fork's own bind then failed with a serialization error. That was an artifact of the test, not a production path.
  - The mutation that survived before (the re-check removed) now fails that test with the foreign-key violation. I did not re-run the other 19 earlier mutations; no assertion they failed on was changed.
  **Why:** The verifier showed the claim was false, and the log is the owner's record of why the code is shaped as it is. Cost if wrong: none.
  **Issue:** #106, #71
- **Decision (Rule 1):** `POST /engine-profiles/{id}/validate` now takes an optional body: `Annotated[ProfileValidate | None, Body()] = None`, as fork, regenerate and untruncate already do. No body, `{}` and `{"settings": null}` all validate the stored latest version, as `Engine.validate_profile(ctx, id)` does without a request.
  - The earlier entry's "`{settings?}`" described the field, but the route declared the body required. So a call with no body was 422 `validation_error` (`Field required`), contrary to the pass-1 Discretion line ("takes an optional `settings` body (dry run) else validates the stored latest") and to the library.
  - `docs/openapi.json` is regenerated: the request body is no longer required.
  - `test_validate_without_a_body_checks_the_latest_version` covers it:
    - A test-only rule is held, and the stored version 1 breaks it: no body, `{}` and `{"settings": null}` are each refused 422 `invalid_profile`, and so is the library call.
    - A dry run of settings that meet the rule is accepted.
    - After an edit makes version 2 meet the rule, a call with no body is accepted.
    - Without the rule, the stored version is accepted, and an unknown profile is 404.
  - C-03 gains a no-body parity step, so `PROFILE_ANSWERS` gains a 200.
  - Mutation-checked, with the sources restored byte for byte (SHA-1 checked). Each of these fails a new test:
    - the body required again (it also fails C-03, where REST answers 422 and the library 200);
    - no body validating the model defaults;
    - no body validating version 1;
    - the body's settings ignored;
    - the purge re-check removed;
    - a fork taking Lite.
  **Why:** The Discretion line prescribes it, and REST must answer as the library does (C-03). Cost if wrong: none; an optional body only adds accepted requests.
  **Issue:** #106
- **Decision (Rule 3, blocker):** I cherry-picked #93's two verified commits onto the milestone branch before writing #109: `146fe95` → `26d21c3` and `8681341` → `d4ccffd` (`git cherry-pick -x`, so each names its source). They touch only `tools/gates/traceability.py` and `tests/gates/test_traceability.py`.
  **Why:** AC3 needs a test part with `interim_until="M7"`, which needs an interim annotation in docs/04 §15 (`C-16 (M4, interim until M7)`). check-traceability at the milestone's HEAD splits a §15 cell on every comma, so it cannot parse that annotation (exit 2): exactly #93's bug. The drift note says #93's fix must land first, and the plan cherry-picks it before group A's gate anyway. Cost if wrong: when the orchestrator cherry-picks `m3/bug-93`, both commits are already applied, so git stops on an empty cherry-pick that should be skipped (`git cherry-pick --skip`). #93's decisions still go in with the lane-2 entries.
  **Issue:** #109, #93
- **Decision (drift):** The upgrade-unavailable slice is `test_c_16__upgrade_unavailable`, `part("M4", "FR-PROF-7", interim_until="M7")`, the drift reviewers' Option G. It uses the slot docs/04's C-16 text already defines: "[M4: FR-PROF-7] *(interim until M7)*". The test plan's `part("M3", …)` was not used. §15's FR-PROF-7 row now reads `C-16 (M3), C-16 (M4, interim until M7), S-PROFILE-SWITCH (M7)`; `--write` cannot produce this, because the row is above `current_milestone`. `test_c_16__two_stage` keeps `part("M3", "FR-PROF-7; FR-SCN-2")`. Both C-16 slices are in `tests/integration/test_engine_profiles.py`, beside #106's `test_c_16__crud`. M4's own C-16 (M4) slices (the consolidation interim clause, the gate-threshold edit) must join that file. C-18 (M3) and the detailed tests are in `tests/integration/test_two_stage.py`. With the annotation removed, check-traceability reports `interim mismatch FR-PROF-7 C-16 (M4)`, so the gate does read it.
  **Why:** Two `(C-16, M3)` parts for FR-PROF-7, one interim and one not, can never both match one §15 entry. Dropping FR-PROF-7 from the M3 slice (Option A) would make all of FR-PROF-7's M3 coverage interim, which is wrong for the two-stage flow, a permanent rule. Cost if wrong: the interim part gates from M4, not M3. The behaviour is still tested in every CI run, and M3's FR-PROF-7 gate is the two-stage slice.
  **Issue:** #109
- **Decision (drift):** #109 builds the minimal per-story resolution and read the AC need, ahead of #113, the drift reviewers' sequencing (1)+(2):
  - `core/profiles/effective.py` resolves a story's profile at the version it runs on: its pin, else the latest. That version's settings are read over `ProfileSettings`' defaults, with optional preview overrides laid on top, and the result is validated as a whole.
  - `GET /stories/{id}/effective-config` answers `{story_id, profile: {id, version, pinned, deleted}, effective, fingerprint, capabilities, quality_status}`, where `effective` holds the delivered settings at their effective values.
  - Migration `0026` adds `stories.engine_profile_version_pin`: nullable, with a composite foreign key `(engine_profile_id, engine_profile_version_pin)` to `engine_profile_versions`. A fork copies the pin, as it copies every column.

  #113 extends these: `deployment_defaults` and `overrides` in the read, `profile_defaults` and stored overrides in the resolution. Its assign route must clear the pin, or set it for an upgrade-unavailable single story, and its routes join `api/routes/story_profile.py`, the file #109 created.
  **Why:** AC3 asserts through the effective-config read and AC4 names the read; the turn needs the same resolution to stamp itself. The column is the Discretion's (`stories.engine_profile_version_pin`) and a new column the AC need, not Rule 4. Cost if wrong: #113 reshapes a route and a module nobody consumes yet.
  **Issue:** #109, #113
- **Decision:** The answer and library result of `PUT /engine-profiles/{id}` are now the two-stage envelope.
  - REST: `ProfileEditBody`, `{applied, impact, result, config_revision, idempotency_key}`, where `result` is the profile, or `null` when held. The status is 200 either way.
  - The library returns `TwoStageResult[ProfileWrite]`.
  - `ignore_warnings` is a field of the request model (`ProfileUpdate` extends `TwoStageRequest`), not a separate keyword on the method, so REST and the library carry it the same way. `ignoreWarnings` is accepted and documented in the OpenAPI schema. Both spellings with different values are 422 at `body`.
  - `Engine._keyed` gains `keep=`: a held result (`TwoStageResult.kept()` false) releases the key instead of storing it. A held call therefore leaves no operation and no key, and the confirmation under the same key is its first execution.
  - Existing tests changed because the shape changed: `test_c_16__crud`, `test_i_21__profiles`, `test_validate_without_a_body_checks_the_latest_version` and `test_edits_record_their_requester_and_replay` read `["result"]`, and `test_library_matches_rest` reads `.result.profile`. C-03 renders `ProfileEditBody`. No assertion was loosened.
  **Why:** The Discretion's `{applied, impact, result}` and `TwoStageResult(applied, impact, result)`, and the drift note's rule that a report-only path must not be stored under `_keyed`. Cost if wrong: a flat body (profile fields plus `applied` and `impact`) would be one route's change.
  **Issue:** #109
- **Decision:** The fingerprint is `sha256:` over the canonical JSON of the AC's three keys:
  - `profile_version: {profile_id, version}`;
  - `models`, holding every delivered purpose, each `{id, reported_version}`: the purpose's own `model_config` model, else the `default` row's; the reported version is null in M3;
  - `quality_relevant_settings`, holding every leaf tagged `Quality.relevant` at its effective value (delivered or not), plus `prompt_templates`, the SHA-256 of each delivered purpose's template file.

  The Orchestrator resolves the models and templates (`orchestrator/models/facts.py`) and passes them as `RuntimeFacts`. A turn reads the models in its read-phase snapshot. One consequence: a new profile version changes the fingerprint even when it changes only a quality-irrelevant setting, because FR-PROF-16 makes the version an input. So the complement for "a non-quality-relevant change keeps it" is a story override in a preview (`pins.max_pinned`) and the story's `turn_concurrency`, never a profile edit. `test_a_new_profile_version_is_a_new_fingerprint` pins this.
  **Why:** FR-PROF-16 lists the profile version, each purpose's model and reported version, and the tagged settings. The Discretion names the profile id: a bare version number would make two profiles' configurations share a fingerprint. Cost if wrong: dropping the version or the id changes every fingerprint once, before anything stores them (#112 comes later).
  **Issue:** #109, #112
- **Decision:** Every Lite turn stamps itself at turn start (`pipeline.begin`, in its REPEATABLE READ read phase): respond, regenerate and queued turns. `operations.config_revisions` gains `fingerprint`, `profile`, `profile_version`, `models`, `quality_status` (`{status}`) and `capabilities`. `RespondResult` and `RespondResponse` gain `fingerprint`, `capabilities` and `quality_status`. They are nullable, or empty, only for a stored result from before #109, so that a replay still loads. I-07's `RESPONSE_FIELDS` gains the three names. Ingest operations record no stamp: no configuration produced their text.
  **Why:** The AC says respond responses (#57), and the key_link says the stamp is computed at turn start from the effective configuration. `recapture_config` merges keys, so the stamp survives a re-check. Cost if wrong: one more call site if #112 wants ingested messages stamped.
  **Issue:** #109, #112
- **Decision:** What a report holds and when it warns:
  - Capabilities are module names, sorted, with the pipeline named by its level. `capabilities_gained` and `capabilities_lost` also count a module that stays on at a higher or lower level.
  - A data-producing increase is a module in `schema.DATA_PRODUCING_MODULES` (derived from the switches' `Tag`) switched on or raised a level, or a data-producing dial (`schema.DATA_PRODUCING_DIALS`) raised while its module stays on.
  - "History" means at least one committed, active `turn` operation, generated or ingested. That is the drift correction: turn 1 is the first played turn.
  - An upgrade-unavailable entry keeps its configuration, so its `fingerprint_after` is its `fingerprint_before`, while `capabilities_gained` still shows what the change would add.
  - `warnings` lists the reasons in `impact.WARNING_KINDS` order. This field is beyond the AC's shape: it saves consumers from deriving them.
  - `guarantees_lost` is the FR-VERIFY-15 delta. A report over zero stories warns of nothing.
  - A profile edit's report covers only the stories that follow the latest version (pin `NULL`): a pinned story is neither listed nor moved by later edits.
  - The Discretion's `neutralised: true` is not implemented, because no story state of that name exists anywhere, plans included. The erasure stories add it if they need it.
  - `recommendations_unmet` is `[]` for story mutations until a story carries a scenario snapshot (#119). The machinery is in place: `Recommendation`, `unmet` and `creation_report`.
  **Why:** The Discretion lines (sorted flat names, `lite` included, per-entry fingerprints, cap and count), filled in where they are silent. Cost if wrong: the shapes are additive.
  **Issue:** #109, #119
- **Decision:** In M3 no delivered module is data-producing, so `epistrel.testing.profiles.data_producing(*modules)` marks one for a block, as #106's `extra_rule` does for dependencies. The test switch is the polish pass. `test_c_16__upgrade_unavailable` has a complement: unmarked, the same edit moves a story with history to the new version.
  **Why:** The Discretion: "the fixture marks a module data-producing through `epistrel.testing`". Cost if wrong: none.
  **Issue:** #109
- **Decision:** Locks and the residual race. A profile edit reads the deployment row `FOR SHARE` first, keeping M2's order, then holds the profile `FOR NO KEY UPDATE`. Only when the edit is a data-producing increase does it also hold every following story `FOR NO KEY UPDATE`, in id order, before reading their history. A turn's commit then waits, while a story insert's foreign-key check does not (#104's lesson). Report and apply run in one transaction, and there is no `impact_changed` code (pass 1). Known residual race: a fork whose snapshot was read before the edit commits a story that follows the new version even though it has history. That is #106's purge race again; there is no real data-producing module before M4, so it is not closed here.
  **Why:** Locking every following story on every edit would block their turns even for a rename. Cost if wrong: one lock clause.
  **Issue:** #109
- **Decision (drift):** The estimate's constants are in `core/profiles/estimates.py`, and nothing reads `docs/` at run time:
  - chunks of 30 messages, one call per chunk for each module whose data the change adds;
  - tokens are the characters over 4, read once per module;
  - the calls run one after another at `CALL_LATENCY_MS = 13_398`.

  That latency is the model matrix's largest median per-call latency: the policies-off row, at one generation call per turn. `test_the_estimate_and_its_measured_latency` derives it from `docs/model-matrix.json` again, so a re-measured matrix fails until the constant follows it. The matrix has no cost field, so the estimate carries none: FR-PROF-7 asks for model calls, tokens and duration.
  **Why:** The drift note: `docs/` does not ship, and no per-call figure exists. The largest per-call latency makes an estimate err long. Cost if wrong: one constant.
  **Issue:** #109
- **Decision:** The preview, `POST /stories/{id}/impact-preview`, is `mutating=False`: no `Idempotency-Key`, no expected revision, but the principal is still required by its method. It refuses:
  - a body naming neither `profile_id` nor `overrides` (422 at `body`);
  - a profile that cannot be assigned (422 `profile_deleted` with `reason` `unknown`/`deleted`, at `body.profile_id`, like `require_assignable`);
  - a result that cannot run (422 `validation_error` at `body.overrides.<path>`, or `invalid_profile`).

  Naming the story's own profile is no reassignment: the story keeps its pinned version, even when the profile is deleted. Its body is the `ImpactReport` itself.

  `overrides` is a sparse settings document, validated strictly as a profile's settings are, whose settings replace the profile's (`effective.layered`). It is not an RFC 7396 merge patch, which my first build used: C-01 caught it. Schemathesis sent `"overrides": {"repair": null}`, which the published schema forbids, and the merge patch accepted it as "remove the key" (`AcceptedNegativeData`). A `null` is now accepted only where a setting takes one. `test_preview_refusals_write_nothing` asserts 422 at `body.overrides.repair`, and the preview's 100 generated C-01 examples pass. #113 decides how its `PATCH …/profile-overrides` (a merge patch over the stored overrides) is previewed; if it previews a patch, its published schema must allow the `null`s. The route, and `GET …/effective-config`, are in the closed `profiles` tag. Both have C-03 parity steps (`PROFILE_ANSWERS` gains two) and nothing-gates `CALLS` entries. C-03 normalises a fingerprint by first-seen position, like an id, since it hashes a profile id each surface mints.
  **Why:** The Discretion and the drift note's route-flag pointer. Cost if wrong: none.
  **Issue:** #109
- **Decision:** The creation path is #119's, so `test_c_16__two_stage` covers the test plan's "creation with an unmet recommendation → report first, success with `ignore_warnings`" at the machinery level: `creation_report` (only an unmet recommendation can make a creation warn) through `two_stage`, held and then confirmed. #113 covers its two story routes. These follow the coverage line ("#109's tests exercise `two_stage` on `PUT /engine-profiles/{id}` and `impact_preview` only").
  **Why:** No story is created from a scenario before #119. Cost if wrong: #119 adds the route-level assertion, as planned.
  **Issue:** #109, #113, #119
- **Decision:** `SettingsDocument` and `SETTINGS_SCHEMA` moved from `core/profiles/store.py` to `schema.py`; `store` re-exports both. The dependency order is now `store → impact → effective → fingerprint → schema`, with no cycle. The status enum is `QualityStatusValue` and its model `QualityStatus {status}`: an enum named `QualityState` would publish an OpenAPI component whose name a part must spell, and "State" is an M6 lexicon term. Parts reach later terms through `tests/profile_support.py` constants: `REDERIVATION` ("backfill", M7), `QUALITY_STATUSES` and `STATUS_COMPONENT`. README and docs/02 §4.11 describe the two stages, the preview, the read, the fingerprint and the pin.
  **Why:** An acyclic import order; the lexicon (the M2 lesson); docs that match the code. Cost if wrong: renames only.
  **Issue:** #109
- **Decision:** Filed #319 (`needs-triage`) and did not fix it. `tests/integration/test_respond.py::test_openapi_calls_the_message_finished_never_verified` fails on the milestone branch. #105's `DepictedSubject` description says "unverified", and the test checks every OpenAPI description for the substring "verified". The committed `docs/openapi.json` at `9b855a9` already holds that text, so the failure predates #109. None of #109's descriptions trips the check. Group A's full run will fail on this test until #319 is decided. It is cross-referenced on #109.
  **Why:** It is out of #109's scope, and choosing between rewording #105's description and narrowing an M2 assertion is a triage call.
  **Issue:** #109, #319, #105
- **Decision:** I mutation-checked the story. Each mutation was applied by a script, which ran the named tests and restored the sources (SHA-1 checked over every changed file afterwards). An unmutated control passes. All 20 mutations fail a test:
  - in `two_stage`: always applying, or ignoring the confirmation (C-16);
  - in `_keyed`: storing a held result under the key (C-16, and the held-key test);
  - in the following report: ignoring history, writing no pin, resolving without the pin, no data increase ever, the kept story reporting the new fingerprint, listing not by id, the cap ignored (the interim slice, C-16 or the cap test);
  - in the fingerprint: no relevant settings, irrelevant settings included, the own model ignored, the edit's report built without the models (C-18 or C-16);
  - in the turn: no stamp recorded, no stamp answered (C-18);
  - in the preview: overrides ignored (C-16, C-18);
  - in the estimate and request: tokens not divided (the interim slice); the two spellings at odds accepted, a lost capability not warned (C-16).

  Branch coverage of `epistrel.core.profiles` over its three test modules is 98%.
  **Why:** Under the batched gate, a story's correctness rests on its own tests until group A's full run. Cost if wrong: none.
  **Issue:** #109
- **Decision:** #109 verification fixes, round 1 (Rule 1). Every impact report now states the capabilities enabled, as `capabilities_after`: the sorted module names (`lite` included) of the configuration a story would run under. It sits between `fingerprint_after` and `quality_status_after`, with their placement and nullability (`list[str] | None`):
  - in each `AffectedStory`, from the stamp the story will run under. So an `upgrade_unavailable` entry states the list it keeps, while `capabilities_gained` still shows what the change would add;
  - at the top of a one-story report (the preview, and #113's assignment and overrides) and of a creation report (#119);
  - null at the top of a profile edit's report, which may list many stories, like its fingerprints.

  I added no `capabilities_before`: the report's naming is `_before`/`_after`, FR-PROF-16 asks for the capabilities enabled, and `capabilities_gained`/`capabilities_lost` already give the delta. `docs/openapi.json` is regenerated, and the preview and edit route descriptions and README name the field. The tests gained assertions, and none was removed or loosened:
  - C-18 (M3): the preview's top and entry (Lite's list under a `pins.max_pinned` override; the list without `rolling_summary` under a new override that turns it off, which also gives a new fingerprint); the edit report's entry, with its top null, and the read after the edit.
  - C-16 (M3): the held edit's entries (no `rolling_summary`) with the top null, the preview, and the creation report.
  - C-16 (M4, interim): the kept entry without the polish pass and the moved entry with it, each equal to its story's effective configuration after the confirmation, and both previews.
  - `tests/test_profile_facts.py`: the entry and one-story report, and the creation report.

  Mutation-checked, with `impact.py` restored from a scratch backup each time (SHA-1 identical at the end). Each mutation fails a test: an unavailable entry stating the new list, an available entry stating the old list, the field dropped from the entry, from a one-story report's top, and from a creation report.
  **Why:** AC4, FR-PROF-16 and C-18 (M3) require every impact report to state the capabilities enabled, and the report carried only the deltas (the verifier's gap 1). Cost if wrong: one field's name or nullability, before anything consumes it; #113 and #119 build their reports on these builders.
  **Issue:** #109
- **Decision:** #109 verification fixes, round 1 (test gap). The gap was real, and it was a gap in test coverage only: `with_history` already counted only committed, active turn operations. No logic changed; its docstring, the module docstring, the two route descriptions and README now say what history is. The new detailed test is `test_only_a_committed_active_turn_is_history` in `tests/integration/test_two_stage.py`:
  - Four stories follow a profile with the polish pass off: one has played a turn; one's only turn failed (a stub `http_500`, 503 `model_unavailable`); one played a turn, then truncated it away (`to_turn: 0`); one never played.
  - It first asserts each story's turn operations (status, membership): `committed`/`active`, `failed`/`active`, `committed`/`discarded`, and none. So neither case is vacuous.
  - Under `data_producing("polish")`, the edit's report lists only the first as `upgrade_unavailable`, with its re-derivation needed. The other three gain the polish pass with nothing to re-derive and Lite's capabilities after it, in the report and in a preview of each alone; the first's preview is unavailable.
  - Confirmed, the first stays pinned on version 1, and the other three run on version 2, unpinned, with the polish pass.

  It is a detailed test, not part of the C-16 (M4) slice: the qualifier is #109's own definition of history, and the slice follows docs/04's text. The verifier's mutations E (status filter removed) and E2 (membership filter removed) each fail it, at the entry's `upgrade_unavailable`, with `impact.py` restored afterwards (SHA-1 checked).
  **Why:** The verifier's gap 2: removing either filter left all 38 story tests green, so a story whose only turn failed or was truncated could have been pinned as `upgrade_unavailable` without any test noticing. Cost if wrong: none; a test only.
  **Issue:** #109
- **Decision:** #109 verification fixes, round 2 (Rule 1). An edit of a profile that no story follows now states the profile's own stamp at the top of its impact report: `fingerprint_before` and `fingerprint_after` are the fingerprints before and after the edit, and `capabilities_after` and `quality_status_after` are the configuration's after it. A story following the profile would run under exactly that. Until now such a report listed no entry and left those four top fields null, so it stated no fingerprint, capabilities or quality status at all. Editing a profile before any story is assigned to it is the common case.
  - The top stays null whenever the report lists a story, because each entry states the stamp its story would run under (the pass-1 Discretion and round 1).
  - The condition is that no entry is listed (`not entries`), not `affected_count == 0`. The two agree except when a following story is created or deleted between the report's count and its listing, which are two READ COMMITTED statements. Keyed on the listing, no report can be left without a stamp. That count-versus-listing race is otherwise benign: a story created mid-edit has no history and would follow the new version anyway. It is not changed here.
  - I rejected the alternative of always stating the profile's own stamp at the top. It contradicts the Discretion's null top, and the top's `fingerprint_after` would then disagree with any `upgrade_unavailable` entry's.
  - The `ImpactReport` and module docstrings, `following_report`'s docstring, the `PUT /engine-profiles/{id}` description and README say what the top holds. `docs/openapi.json` is regenerated (two descriptions).
  - `test_c_18__fingerprint` gains the case, and no assertion was removed or loosened. It creates a profile no story follows and previews assigning it to the test's story. Then it edits the profile to turn the rolling summary off. The edit applies at once, with no entry, a count of 0 and no warnings. The top's `fingerprint_before` is the first preview's `fingerprint_after`. Its `fingerprint_after` is a new `sha256:` fingerprint, and a second preview of the assignment gives that same fingerprint and the same capabilities. The capabilities are Lite's without the rolling summary, and the status is `uncertified`. On the unfixed code the case fails at `None == 'sha256:…'`.
  - Mutation-checked, with `impact.py` restored after each mutation (SHA-1 identical at the end). Each of these 5 mutations fails `test_c_18__fingerprint`: the top's capabilities taken from before the edit; its `fingerprint_after` taken from before; its `fingerprint_before` taken from after; its status dropped; and the top stamped even when stories are listed. The last is caught by the existing null-top assertion on the Lite edit.
  **Why:** AC4, FR-PROF-16 and C-18 (M3) require every impact report to state the fingerprint, the capabilities enabled and the quality status (the verifier's round-2 gap). Cost if wrong: one function's top-level placement. The four fields are already nullable, and nothing consumes them yet; #113 and #119 build their reports on `single_report` and `creation_report`, which this does not change.
  **Issue:** #109
