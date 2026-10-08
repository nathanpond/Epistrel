# Epistrel Engine

FastAPI service (Engine Core + Orchestrator), no UI. The design lives in `docs/` (01 whitepaper, 02 architecture, 03 requirements with stable `FR-`/`NFR-` IDs, 04 testing plan, 05 ADR log), and it is authoritative: check it before choosing a library or shape, and cite requirement IDs in issues and tests.

- Tooling: `uv sync`, `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` (strict).
- The human-facing test harness is a separate repo, nathanpond/Epistrel-Console. Keep UI concerns out of this repo. When the Engine gains a capability, consider the matching Console surface.
- ADRs are append-only (see the header of `docs/05-adr-log.md` for the amendment rules).

## n8SDLC project

This project is managed by the n8SDLC workflow (GitHub Issues = the plan; `/n8-stat` shows where things stand). If a change made in this session deviates from what planned issues assume — different library, provider, architecture, dropped/added scope, or amending a declared invariant below — do two things before finishing:
1. Append an `## Ad-hoc` entry to `.n8/decisions.md` (format documented in that file's header) naming the change, the why, and the milestones/issues likely affected.
2. Tell the user which future milestones may now have stale plans and suggest running `/n8-replan`.

### Project invariants

Load-bearing constraints no story may breach without an explicit conversation. Changing one is a user decision and plan drift: log it as an `## Ad-hoc` entry and run `/n8-replan`.

1. **Epistrel runs no model of its own.** Every model call goes to an external OpenAI-compatible endpoint; no inference runtime or model weights ship as a runtime dependency or in the image. — **test-enforced** (dependency deny-list; M1, epic #2) — guard: #41 (planned)
2. **Apache-2.0-compatible dependency graph.** Every runtime dependency's license is on the allowlist; no AGPL/GPL/LGPL. — **test-enforced** (license check; M1, epic #2) — guard: #42 (planned)
3. **No story content in operational logs, metrics, or traces by default.** Content-bearing debug logging is opt-in only. — **test-enforced** (log-capture guard; M2, epic #8) — guard: #74 (planned)
4. **Every REST route requires service authentication.** No unauthenticated endpoint except `GET /health`. — **test-enforced** (route-table guard; M2, epic #8) — guard: #44 (planned)
5. **No external broker, cache, or scheduler.** Postgres is the only infrastructure dependency at hobby scale; no broker, cache, or scheduler client library as a runtime dependency. — **test-enforced** (dependency deny-list; M1, epic #2) — guard: #41 (planned)
6. **The event log is the truth.** Nothing removes events except erasure and opt-in compaction, both recorded with a marker. — **honor-system** (checked by `/n8-audit`)
7. **Every requirement is assigned to exactly one milestone, and every test part gates from exactly one milestone.** — **test-enforced** (§22 table and part-marker checks; M1, epic #2) — guard: #37, #38 (planned)

Background work (shells, monitors, background subagents) follows `reference/background.md` in the n8SDLC plugin. Every launch is bounded and announced with what it's for and when it should finish. Overdue work is checked, not waited on. A turn never ends with work still running unless the reply names it. `/n8-subs` lists everything live.

Separately: if a `/n8-*` skill's own instructions failed, misled you, or were silent on something this session, tell the user and offer `/n8-feedback` — it packages the learning as an issue on the plugin repo, stripped of project specifics, and sends nothing until the user has reviewed the exact text.

### Architecture rules (not invariants)

- `epistrel.core` does not import `epistrel.orchestrator` or `epistrel.api` (docs/02: Core is the deterministic layer; the Orchestrator makes the model calls). Enforced by `[tool.importlinter]` from M1 (#41); may be relaxed by an ordinary PR.
