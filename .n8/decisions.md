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
