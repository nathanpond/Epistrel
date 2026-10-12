# M3 batched test gate (trial) — plan and metrics

Owner decision 2026-10-12 (see the `## Ad-hoc — 2026-10-12` entry in `.n8/decisions.md`). M3 only. Afterwards the owner decides from these metrics whether to send it upstream via `/n8-feedback` as a batched-gate option for `/n8-exec`.

## How `/n8-exec M3` departs from the skill

The skill's rule "write the tests and run the **full** suite for each story" is replaced by:

- **For each story:** run the story's own new tests, plus the existing tests for the areas it touched (`pytest tests/<area>/...`), plus `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run lint-imports`. Then commit with `Refs #N` and push, as now. The completion comment says "own tests green @ <sha>, awaiting group gate". **Don't tick its AC boxes yet.**
- **At the end of each group:** run the full `uv run pytest` once and time it.
  - Green → tick the AC boxes and post a completion comment on every story in the group, citing the group-run SHA and its result.
  - Red → work out which story caused each failure, from the test name or else by `git bisect` over the group's commits. Fix it as a Rule 1 fix on that story and re-run the full suite. The three-attempts-then-blocker rule applies to each group run.
- **Group order is a hard barrier.** Group B starts only after group A's full run is green, and C only after B's.
- Everything else is unchanged: CI at the milestone PR, the decisions log, the Rule 1–4 handling, `Refs` without `Fixes`/`Closes`.
- **#305 is the first story in group A** (moved from M2 to M3 by the owner, 2026-10-12): the flaky X-04 PR-gate test, which otherwise makes full runs fail at random. Until it is fixed, recognise its failure mode and do not count it as a group failure.

## Groups (cut along the dependency graph; topological layers L0–L14)

| Group | Stories | Theme |
|---|---|---|
| A (11) | #305, #93, #94, #303, #103, #104, #105, #106, #107, #108, #109 | carried bugs; catalog drafts and versions, ownership and relations, media store and serving, scenario catalog, engine profiles and fingerprint |
| B (10) | #110, #111, #112, #113, #114, #115, #116, #117, #118, #119 | listing and search, scenario validation, message feedback, effective config, personas, character card, companion scenario, offboarding, rating relax/tighten, genesis |
| C (9) | #120, #121, #122, #123, #124, #125, #126, #127, #128 | guardrail re-check, rating suggestions, Lite materialization, subject index and erasure cluster, feedback reporting, REST/library parity |

Order within each group (respects blocked-by): A: 305 (first: it stabilises the full-run gate), 93, 94, 303, 103, 104, 105, 106, 107, 108, 109 · B: 110, 111, 112, 113, 114, 115, 116, 117, 118, 119 · C: 120, 121, 122, 123, 124, 125, 126, 127, 128.

## Metrics to record (fill in during exec)

Baseline (main @ 1740e0d, before M3, measured 2026-10-12 on the owner's Mac): full `uv run pytest` = **550 s (~9.2 min) wall**, 856 passed, 14 deselected. The slowest are C-08 setup 95 s, X-03 59 s, config revalidation 31 s, X-04 15 s. Per-story gating would be about 30 × 9.2 ≈ 4.6 h of full runs; three group runs are about 28 min. The suite will grow during M3, so record each group run's actual time.

**For each story:** wall time from start to commit; time spent on per-story tests; number of per-story test runs.

| Story | Group | Dev wall (min) | Per-story test time (min) | Test runs | Notes |
|---|---|---|---|---|---|

**For each group run:**

| Group | Full-run wall (min) | Attempts | Failures | Attributed by test name | Needed bisect (runs) | Rework time (min) | Failures the per-story subset missed |
|---|---|---|---|---|---|---|---|

**Summary to compute at the end:** time saved = (30 × the baseline full-run time) − (the actual group full runs + bisect runs). Weigh that against the rework cost of failures caught late. The key number is failures that per-story tests missed but a per-story full run would have caught.
