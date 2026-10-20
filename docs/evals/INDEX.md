# Eval reports

Every committed eval report, newest first: one row per run of the quality evals against a real
model, whatever its verdict. Generated from the reports' embedded summaries by
`uv run python -m epistrel.evals index`; `uv run python -m epistrel.evals --check` fails when
this file is stale. [README.md](README.md) has the policy (no automated real-model runs) and
the procedure.

| Date (UTC) | Model | Fingerprint | Host | Suites | NFR-QUAL-2 | NFR-QUAL-3 | NFR-QUAL-4 | NFR-QUAL-5 | NFR-QUAL-6 | NFR-QUAL-7 | NFR-QUAL-10 | Run |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 | `cydonia-24b-v4.3` | `775d2c4f43f3` | local | `e01` fail, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` fail, `e10` fail, `q01` pass, `q02` report_only, `q03` report_only | fail | fail | fail | fail | pass | fail | fail | [fail](775d2c4f43f3-2026-10-20.md) |
| 2026-10-19 | `cydonia-24b-v4.3` | `bafbf3d71ae7` | local | `smoke` pass | — | — | — | — | — | — | — | [pass](bafbf3d71ae7-2026-10-19.md) |
| 2026-10-17 | `cydonia-24b-v4.3` | `9dbec729a92f` | local | `smoke` fail | — | — | — | — | — | — | — | [fail](9dbec729a92f-2026-10-17.md) |
