# Eval reports

Every committed eval report, newest first: one row per run of the quality evals against a real
model, whatever its verdict. Generated from the reports' embedded summaries by
`uv run python -m epistrel.evals index`; `uv run python -m epistrel.evals --check` fails when
this file is stale. [README.md](README.md) has the policy (no automated real-model runs) and
the procedure.

| Date (UTC) | Model | Fingerprint | Host | Suites | NFR-QUAL-2 | NFR-QUAL-3 | NFR-QUAL-4 | NFR-QUAL-5 | NFR-QUAL-6 | NFR-QUAL-7 | NFR-QUAL-10 | NFR-QUAL-14 | NFR-QUAL-15 | Run |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-21 | `cydonia-24b-v4.3` | `84411b5aaf1a` | local | `e06` fail, `e07` pass, `e10` pass | — | — | — | pass | — | fail | pass | — | — | [fail](84411b5aaf1a-2026-10-21.md) |
| 2026-10-21 | `cydonia-24b-v4.3` | `2c61ec2dbc6c` | local | `e01` fail, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` fail, `e10` fail, `e12` insufficient_evidence, `e13` report_only, `q01` pass, `q02` report_only, `q03` report_only | fail | fail | fail | fail | pass | fail | fail | insufficient_evidence | report_only | [fail](2c61ec2dbc6c-2026-10-21.md) |
| 2026-10-20 | `cydonia-24b-v4.3` | `775d2c4f43f3` | local | `e01` fail, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` fail, `e10` fail, `q01` pass, `q02` report_only, `q03` report_only | fail | fail | fail | fail | pass | fail | fail | — | — | [fail](775d2c4f43f3-2026-10-20.md) |
| 2026-10-20 | `cydonia-24b-v4.3` | `775d2c4f43f3` | local | `e12` insufficient_evidence, `e13` error | — | — | — | — | — | — | — | insufficient_evidence | report_only | [fail](775d2c4f43f3-2026-10-20-prose.md) |
| 2026-10-19 | `cydonia-24b-v4.3` | `bafbf3d71ae7` | local | `smoke` pass | — | — | — | — | — | — | — | — | — | [pass](bafbf3d71ae7-2026-10-19.md) |
| 2026-10-17 | `cydonia-24b-v4.3` | `9dbec729a92f` | local | `smoke` fail | — | — | — | — | — | — | — | — | — | [fail](9dbec729a92f-2026-10-17.md) |
