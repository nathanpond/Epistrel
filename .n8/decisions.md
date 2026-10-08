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
