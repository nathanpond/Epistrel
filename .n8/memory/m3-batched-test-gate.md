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

How the columns are measured (from each agent's own `date +%s` stamps and command timings):
- **Dev wall** is the implementer's wall time, from start to commit and push.
- **Per-story test time** and **Test runs** cover the per-story gate (pytest, ruff, mypy, lint-imports and the lint-job gates). They are the implementer's and every fixer's runs, including the follow-up rounds after the group workflow.
- **Verifiers** are the independent fresh-context verifiers' runs, including their mutation checks. They are part of this run's verification, not of the per-story gate.
- **Span** runs from the implementer's start to the story's last verification.
- Group A ran in two lanes at once: lane 1 handled the stories in the main tree, and the bug lane handled the bugs in their own worktrees. So the timings of concurrent work include contention.

| Story | Group | Dev wall (min) | Per-story test time (min) | Test runs | Notes |
|---|---|---|---|---|---|
| #305 | A | 53 | 29.6 | 38 | bug lane, own worktree; verified r2, 1 fix round; span 126 min; verifiers: 33 min, 37 runs |
| #93 | A | 13 | 1.1 | 7 | bug lane, own worktree; verified r2, 1 fix round; span 38 min; verifiers: 2 min, 15 runs |
| #94 | A | 28 | 3.0 | 37 | bug lane, own worktree; gaps after 3 verify rounds, 2 fix rounds; accepted with the out-of-scope residue filed as #320; span 131 min; verifiers: 3 min, 54 runs |
| #303 | A | 24 | 1.0 | 5 | bug lane, own worktree; verified r1, 0 fix rounds; span 36 min; verifiers: 3 min, 16 runs |
| #103 | A | 51 | 20.4 | 47 | lane 1; verified r3, 2 fix rounds; span 129 min; verifiers: 23 min, 41 runs |
| #104 | A | 82 | 43.9 | 100 | lane 1; gaps after 3 verify rounds, 2 fix rounds; follow-up round: 1 fix, passed (21 min); span 178 min; verifiers: 38 min, 52 runs |
| #105 | A | 87 | 56.9 | 99 | lane 1; gaps after 3 verify rounds, 2 fix rounds; follow-up round: 1 fix, passed (51 min); span 193 min; verifiers: 36 min, 54 runs |
| #106 | A | 70 | 30.0 | 47 | lane 1; verified r2, 1 fix round; span 132 min; verifiers: 21 min, 36 runs |
| #107 | A | 88 | 25.8 | 38 | lane 1; verified r1, 0 fix rounds; span 109 min; verifiers: 10 min, 21 runs |
| #108 | A | 63 | 27.7 | 72 | lane 1; gaps after 3 verify rounds, 2 fix rounds; follow-up round: 1 fix, passed (16 min); span 153 min; verifiers: 34 min, 65 runs |
| #109 | A | 63 | 43.5 | 78 | lane 1; gaps after 3 verify rounds, 2 fix rounds; follow-up rounds: 2 fix, passed (38 min); span 158 min; verifiers: 41 min, 92 runs |
| **A total** | | 621 | 283 | 568 | verifiers: 244 min, 483 runs |
| #110 | B | 68 | 47.1 | 69 | main tree; gaps after 3 verify rounds, 2 fix rounds; follow-up round: 1 fix, passed; span 916 min, including the owner's overnight pause; verifiers: 46 min, 51 runs |
| #111 | B | 80 | 27.2 | 60 | main tree; verified r2, 1 fix round; span 207 min; verifiers: 23 min, 58 runs |
| #112 | B | 59 | 18.5 | 45 | main tree; verified r2, 1 fix round; span 228 min; verifiers: 22 min, 39 runs |
| #113 | B | 97 | 86.8 | 85 | main tree; gaps after 3 verify rounds, 2 fix rounds; follow-up round: 1 fix, passed; span 665 min, including the pause; verifiers: 66 min, 64 runs |
| #114 | B | 67 | 28.2 | 56 | main tree; verified r3, 2 fix rounds (the second after the pause); span 321 min, including the pause; verifiers: 36 min, 32 runs |
| #115 | B | 65 | 35.7 | 54 | main tree; verified r2, 1 fix round (after the pause); span 217 min, including the pause; verifiers: 31 min, 29 runs |
| #116 | B | 67 | 27.5 | 58 | main tree; verified r1 (after the pause), 0 fix rounds; span 100 min; verifiers: 12 min, 5 runs |
| #117 | B | 50 | 33.4 | 15 | main tree; verified r2, 1 fix round; span 138 min; verifiers: 26 min, 6 runs |
| #118 | B | 53 | 28.9 | 23 | main tree; verified r2, 1 fix round; plus the group-gate Rule 1 fix (4.4 min, #326); span 151 min; verifiers: 24 min, 16 runs |
| #119 | B | 58 | 42.0 | 32 | main tree; verified r2, 1 fix round; span 125 min; verifiers: 22 min, 8 runs |
| **B total** | | 668 | 375 | 497 | verifiers: 311 min, 308 runs; group B paused overnight after #116's implementation and resumed in a new session (run wf_c43e1e52-af3) |

**For each group run:**

| Group | Full-run wall (min) | Attempts | Failures | Attributed by test name | Needed bisect (runs) | Rework time (min) | Failures the per-story subset missed |
|---|---|---|---|---|---|---|---|
| A | 16.1 (967 s wall; pytest 962.75 s; 1457 passed, 14 deselected; core coverage 97.24 %) | 1 | 0 | n/a | 0 | 0 at the gate | 1, caught before the gate: #319. #105's per-story subset did not run M2's `test_respond.py`, whose FR-RESP-1 test forbids "verified" in OpenAPI descriptions. #105's new description said "unverified". #109's implementer saw the failure in its own per-story run. A per-story full run at #105 would have caught it at once. Rework: one description reworded and the OpenAPI document regenerated, inside #105's 29-minute follow-up fix (not timed separately) |
| B | 19.5 (attempt 2 at `1a55837`: 1172 s wall; pytest 1166.30 s; 1755 passed, 14 deselected; core coverage 97.04 %). Attempt 1 at `329e619`, plain run without coverage: 1156 s wall, 1752 passed, 1 failed | 2 | 1 | 1 (`test_ownership.py::test_every_delivered_type_is_covered`, routes added by #118) | 0 | 4.4 (one fix agent, `1a55837`) + one 19.5-minute re-run | 1, #326. #118's per-story subset did not run `tests/integration/test_ownership.py`, whose coverage test requires every served route under an owned entity to be in `ownership_fixtures.CALLS`. #119's implementer saw it in its own per-story run and filed it, rather than fixing it, because it was another story's. A per-story full run at #118 would have caught it at once |

Group runs use `uv run pytest --cov=epistrel.core --cov-report=term-missing:skip-covered --cov-fail-under=90`: the full suite plus CI's coverage gate, so the times include coverage overhead. The 550 s baseline is a plain run of 856 tests; group A's run had 1457 tests.
Time saved so far (group A, 11 items), against the baseline: 11 × 550 s = 6050 s of per-story full runs, against one 967 s group run. That is about 85 minutes saved. The figure is conservative, because the suite grew with every story, so each per-story full run would have taken longer than the baseline.

Time saved in group B (10 items): 10 × 550 s = 5500 s of per-story full runs, against two group runs (1156 s + 1172 s = 2328 s). That is about 53 minutes saved, net of the one failure's re-run. Cumulative after groups A and B: about 138 minutes.

**Summary to compute at the end:** time saved = (30 × the baseline full-run time) − (the actual group full runs + bisect runs). Weigh that against the rework cost of failures caught late. The key number is failures that per-story tests missed but a per-story full run would have caught.
