# Eval reports

Every committed eval report, newest first: one row per run of the quality evals against a real
model, whatever its verdict. Generated from the reports' embedded summaries by
`uv run python -m epistrel.evals index`; `uv run python -m epistrel.evals --check` fails when
this file is stale. [README.md](README.md) has the policy (no automated real-model runs) and
the procedure.

| Date (UTC) | Model | Fingerprint | Host | Suites | Run |
|---|---|---|---|---|---|
| 2026-10-19 | `cydonia-24b-v4.3` | `bafbf3d71ae7` | local | `smoke` pass | [pass](bafbf3d71ae7-2026-10-19.md) |
| 2026-10-17 | `cydonia-24b-v4.3` | `9dbec729a92f` | local | `smoke` fail | [fail](9dbec729a92f-2026-10-17.md) |
