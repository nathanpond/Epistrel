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
| #120 | C | 30 | 17.1 | 11 | main tree; verified r2, 1 fix round; span 101 min; verifiers: 14 min, 6 runs |
| #121 | C | 58 | 35.8 | 11 | main tree; verified r1, 0 fix rounds; span 73 min; verifiers: 1 min, 3 runs |
| #122 | C | 36 | 16.7 | 17 | main tree; verified r1, 0 fix rounds; span 52 min; verifiers: 13 min, 8 runs |
| #123 | C | 68 | 48.3 | 19 | main tree; verified r2, 1 fix round; span 145 min; verifiers: 16 min, 11 runs |
| #124 | C | 56 | 27.8 | 26 | main tree; verified r2, 1 fix round; span 141 min; verifiers: 30 min, 11 runs |
| #125 | C | 38 | 28.8 | 15 | main tree; verified r2, 1 fix round; plus two group-gate Rule 1 fixes (`62a6d1c`, `106c589`); span 140 min; verifiers: 24 min, 11 runs |
| #126 | C | 60 | 32.5 | 11 | main tree; verified r1, 0 fix rounds; span 74 min; verifiers: 12 min, 4 runs |
| #127 | C | 49 | 34.5 | 25 | main tree; verified r3, 2 fix rounds; span 117 min; verifiers: 38 min, 23 runs |
| #128 | C | 31 | 14.8 | 19 | main tree; verified r2, 1 fix round; span 63 min; verifiers: 21 min, 6 runs |
| **C total** | | 430 | 256 | 154 | verifiers: 173 min, 83 runs; group-gate fix agents not included (81 min, below) |

**For each group run:**

| Group | Full-run wall (min) | Attempts | Failures | Attributed by test name | Needed bisect (runs) | Rework time (min) | Failures the per-story subset missed |
|---|---|---|---|---|---|---|---|
| A | 16.1 (967 s wall; pytest 962.75 s; 1457 passed, 14 deselected; core coverage 97.24 %) | 1 | 0 | n/a | 0 | 0 at the gate | 1, caught before the gate: #319. #105's per-story subset did not run M2's `test_respond.py`, whose FR-RESP-1 test forbids "verified" in OpenAPI descriptions. #105's new description said "unverified". #109's implementer saw the failure in its own per-story run. A per-story full run at #105 would have caught it at once. Rework: one description reworded and the OpenAPI document regenerated, inside #105's 29-minute follow-up fix (not timed separately) |
| B | 19.5 (attempt 2 at `1a55837`: 1172 s wall; pytest 1166.30 s; 1755 passed, 14 deselected; core coverage 97.04 %). Attempt 1 at `329e619`, plain run without coverage: 1156 s wall, 1752 passed, 1 failed | 2 | 1 | 1 (`test_ownership.py::test_every_delivered_type_is_covered`, routes added by #118) | 0 | 4.4 (one fix agent, `1a55837`) + one 19.5-minute re-run | 1, #326. #118's per-story subset did not run `tests/integration/test_ownership.py`, whose coverage test requires every served route under an owned entity to be in `ownership_fixtures.CALLS`. #119's implementer saw it in its own per-story run and filed it, rather than fixing it, because it was another story's. A per-story full run at #118 would have caught it at once |
| C | 22.8 (attempt 3 at `106c589`: 1366 s wall; pytest 1359.74 s; 1883 passed, 14 deselected; core coverage 96.21 %). Attempt 1 at `1e5aa84`: 1370 s, 1876 passed, 5 failed. Attempt 2 at `d179021`: 1358 s, 1881 passed, 1 failed | 3 | 6 tests, 3 causes | 0 by name alone: each needed isolation runs, because the failing tests belonged to M1/M2 areas (X-07, C-01, the fork test) and the cause was cross-test pollution | 0 git bisect; module-range isolation runs instead (about 8 targeted runs, the longest about 10 min) | 81 (two diagnosis-and-fix agent sessions: 45 + 36 min) + two 22.6-minute re-runs | 3. (1) #125: spawned engine processes used the repository's default erasure-ledger file and loaded a stale earlier session's entries. 4 tests failed: X-07 ×2, C-01 [GET /admin/config-changes], and the fork-ledger count. #127's #328 was this symptom. A per-story full run at #125 would very likely have caught it, given the stale file on disk. (2) #113 (group B): C-01's malformed-input complement depended on Hypothesis's draws. It surfaced only now, and a per-story full run could have missed it too. (3) #125's harness gave scratch databases the session ledger, and #128's new C-15 test triggered it. A per-story full run at #128 would have caught it |

Group runs use `uv run pytest --cov=epistrel.core --cov-report=term-missing:skip-covered --cov-fail-under=90`: the full suite plus CI's coverage gate, so the times include coverage overhead. The 550 s baseline is a plain run of 856 tests; group A's run had 1457 tests.
Time saved so far (group A, 11 items), against the baseline: 11 × 550 s = 6050 s of per-story full runs, against one 967 s group run. That is about 85 minutes saved. The figure is conservative, because the suite grew with every story, so each per-story full run would have taken longer than the baseline.

Time saved in group B (10 items): 10 × 550 s = 5500 s of per-story full runs, against two group runs (1156 s + 1172 s = 2328 s). That is about 53 minutes saved, net of the one failure's re-run. Cumulative after groups A and B: about 138 minutes.

Time saved in group C (9 items): 9 × 550 s = 4950 s of per-story full runs, against three group runs (1370 + 1358 + 1366 = 4094 s). That is about 14 minutes saved, but about 81 minutes of diagnosis came on top. Group C was a net loss of about 67 minutes, because its failures were cross-test pollution that the per-story subsets could not see.

**M3 summary (all three groups):** 30 items. Full-run time: per-story gating would have been 30 × 550 s ≈ 275 min at the baseline, and more as the suite grew from 856 to 1883 tests. The six group runs actually took 16.1 + 19.3 + 19.5 + 22.8 + 22.6 + 22.8 ≈ 123 min. That saves about 152 minutes, a conservative figure. Rework for failures caught late: about 86 minutes (B: 4 min; C: 81 min) plus three extra group re-runs, already counted above. Failures a per-story full run would have caught but the per-story subsets missed: 4 causes (#319 in A, #326 in B, and #125's two harness defects in C), plus 1 random one (#113's C-01 complement) that either scheme could miss. Net: about 65 minutes saved over the milestone, against the loss of story-level traceability for 4 regressions. The time saved is concentrated in groups whose stories stay inside their own areas. Groups that add shared test infrastructure (ledgers, spawned processes) give it back in diagnosis.

**Summary to compute at the end:** time saved = (30 × the baseline full-run time) − (the actual group full runs + bisect runs). Weigh that against the rework cost of failures caught late. The key number is failures that per-story tests missed but a per-story full run would have caught.

**Fix pass 2 (#348–#352, 2026-10-15):** five fixes, each gated on its own and area tests (the #350 area run: 23 files, 486 passed in 10.4 min), then one full run at `34dac7e`: 1449 s, 1895 passed, 14 deselected, 0 failed, 1 attempt. No failure was caught late. Against 5 × 550 s of per-story full runs (more at today's 24-minute suite: about 120 min), the single run saved about 96 minutes at the current suite size.

**Fix pass 5 (#369–#376, 2026-10-15):** six fixes (#372 with #373/#374), each gated on its own tests, its mutation checks and its area tests (the erasure area: 22 files, 269 passed in 2.1 min), then one full run at `b546466`: 1537.5 s, 1939 passed, 14 deselected, 0 failed, core coverage 96.28 %, 1 attempt. No failure was caught late.
