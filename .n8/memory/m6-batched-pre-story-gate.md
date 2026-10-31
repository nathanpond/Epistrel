# M6 batched test gate for the pre-story group: rules

Owner decision 2026-10-30 (see the `## Ad-hoc — 2026-10-30 (M6 test gate)` entry in `.n8/decisions.md`). It applies to M6's **24 pre-story issues only**. Every M6 feature story (#185 onward) keeps the per-story full-suite gate.

## Batches (in order; each is a hard barrier)
1. **Flakes:** #537, #514, #517, #446, #468. They run first and alone, because a batched full run is only meaningful once #514, #517 and #537 stop failing at random. Until they are fixed, the known-flake rule applies (one logged re-run; a second failure is real).
2. **M5 bugs:** #567, #566, #568, #516, #563, #483, #521.
3. **Decided changes and Console additions:** #519, #515, #456 (items 3–5), #588 (b), #536, #533, #535, #526, #527, #531, #534.
4. **Spike:** #522, alone (profiling; its rerun is P-04, not the PR gate).

## Per issue
Run its own new tests, the existing tests of the areas it touched, and `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run lint-imports`. Commit with `Refs #N`.

## Per batch
One full `uv run pytest` (with CI's coverage gate) over the batch's head. Tick an issue's AC boxes only after its batch's full run is green. A failure goes back to the issue that caused it, by test name, or by isolation runs or `git bisect` when the name is not enough (M3 group C's lesson: test pollution across areas).

## Metrics to record
For each batch: full-run wall time, attempts, failures, how each was attributed, rework minutes, and failures the per-issue subsets missed. Compare against per-issue full runs at the current suite time (about 19 min sharded).

## Metrics (filled in during /n8-exec M6)

| Batch | Head | Full-run wall | Attempts | Failures | Attribution | Rework | Missed by per-issue subsets |
|---|---|---|---|---|---|---|---|
| 1 flakes (#537 #514 #517 #446 #468) | b47fe89 → 7923759 | 24.7 min (12 shards ×6) + `rest` re-run 4.1 min | 2 (the `rest` shard only) | 2 tests + the check-lexicon gate, one cause | By test name, to #446: "clock" (an M6 term) in three M2-part comments | ~5 min (comment rewording) | 1 (the agent didn't run check-lexicon) |
| 2 M5 bugs (#567 #566 #568 #516 #563 #483 #521) | 1032e43 | 24.7 min | 1 | 0 | — | 0 | 0 |
| 3 decided + Console (#519 #515 #456 #588 #536 #533 #535 #526 #527 #531 #534) | e7d6ce8 | 26.5 min | 1 | 0 | — (one cherry-pick conflict in test_c_03 imports, resolved before the run) | ~2 min | 0 |
