# Eval reports

Every committed eval report, newest first: one row per run of the quality evals against a real
model, whatever its verdict. Generated from the reports' embedded summaries by
`uv run python -m epistrel.evals index`; `uv run python -m epistrel.evals --check` fails when
this file is stale. [README.md](README.md) has the policy (no automated real-model runs) and
the procedure.

## Presets

The quality floors each shipped preset is certified for (NFR-QUAL-11), one row per model it
was certified on, from the committed certification records and the presets as they ship. A
preset edited after its certification reads `uncertified` here until it is certified again.
`epistrel evals certify` writes the reports and records; [README.md](README.md) has the
procedure.

| Preset | Fingerprint | Certification | Floors passed | Floors failed | Insufficient | Interventions covered | Interventions insufficient | Model | Seeds | Date (UTC) | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Lite | `aca3f417efd1` | uncertified (not certified in M7; `--presets lite` certifies it) | — | — | — | — | — | — | — | — | — |
| Standard | `3f8bd090c2da` | uncertified (preset changed since certification 01M6MRZKZPYJCWJXVKANAV4F12) | — | — | — | — | — | — | — | — | — |
| Full | `fb5b09860723` | uncertified (preset changed since certification 01M6RG1VX91P04T98PK7Z7EXBJ) | — | — | — | — | — | — | — | — | — |
| Max | `0121fd3ce770` | uncertified (preset changed since certification 01M6P9X2PV6WTJMN7YN81M6GYM) | — | — | — | — | — | — | — | — | — |

## Image checks

How each image checker measured on each image-output policy (NFR-QUAL-12, E-11), from the newest
committed E-11 report per policy, and whether a committed record certifies that checker for it.
The minors policy's false negatives are never measured: certified only by external attestation;
otherwise an uncertified backstop (ADR-182). [E11-RUNBOOK.md](E11-RUNBOOK.md) has the procedure.

| Policy | Checker | Fingerprint | Host | FN rate (n) | FP rate (n) | Floors (max FN / FP) | Verdict | Certification | Date (UTC) | Report |
|---|---|---|---|---|---|---|---|---|---|---|
| `minors_no_sexual_content` | `claude-haiku-5-5` | `1b019c0193f2` | hosted | not measured | 0.00% (416) | — / 15.00% | pass | uncertified backstop (external attestation only) | 2026-11-09 | [e11-1b019c0193f2-2026-11-09.md](e11-1b019c0193f2-2026-11-09.md) |
| `no_nudity` | `claude-haiku-5-5` | `1b019c0193f2` | hosted | 0.00% (105) | 9.76% (198) | 5.00% / 10.00% | fail | uncertified | 2026-11-09 | [e11-1b019c0193f2-2026-11-09.md](e11-1b019c0193f2-2026-11-09.md) |

## Runs

| Date (UTC) | Model | Preset | Fingerprint | Host | Suites | NFR-QUAL-1 | NFR-QUAL-2 | NFR-QUAL-3 | NFR-QUAL-4 | NFR-QUAL-5 | NFR-QUAL-6 | NFR-QUAL-7 | NFR-QUAL-9 | NFR-QUAL-10 | NFR-QUAL-14 | NFR-QUAL-15 | Run |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-06 | `claude-haiku-5-5` | full | `4710ae26cb73` | hosted | `e01` pass, `e01_viewpoint` pass, `e02` report_only, `e03` fail, `e04` report_only, `e05` pass, `e06` fail, `e07` pass, `e09` pass, `e10` pass, `e12` insufficient_evidence, `e13` report_only, `priv_think_leak` pass, `q01` pass, `q02` report_only, `q03` report_only, `q04` fail, `smoke` fail | fail | pass | fail | pass | pass | pass | fail | pass | pass | insufficient_evidence | report_only | [fail](full-4710ae26cb73-2026-11-06.md) |
| 2026-11-05 | `claude-haiku-5-5` | standard | `ff7c9b16e499` | hosted | `e01_viewpoint` pass, `e02` report_only, `e03` fail, `e04` report_only, `e06` fail, `e07` pass, `e09` pass, `e10` pass, `e12` insufficient_evidence, `e13` report_only, `priv_think_leak` pass, `q01` pass, `q02` report_only, `q03` report_only, `q04` fail, `smoke` fail | fail | pass | fail | — | pass | pass | fail | pass | pass | insufficient_evidence | report_only | [fail](standard-ff7c9b16e499-2026-11-05.md) |
| 2026-11-05 | `claude-haiku-5-5` | max | `f7f66a6e7930` | hosted | `e01` pass, `e01_viewpoint` pass, `e02` report_only, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` pass, `e09` pass, `e10` pass, `e12` insufficient_evidence, `e13` report_only, `priv_think_leak` pass, `q01` pass, `q02` report_only, `q03` report_only, `q04` fail, `smoke` fail | fail | pass | fail | fail | pass | pass | fail | pass | pass | insufficient_evidence | report_only | [fail](max-f7f66a6e7930-2026-11-05.md) |
| 2026-11-05 | `claude-haiku-5-5` | full | `4710ae26cb73` | hosted | `e01` pass, `e01_viewpoint` pass, `e02` report_only, `e03` fail, `e04` report_only, `e05` pass, `e06` fail, `e07` pass, `e09` pass, `e10` pass, `e12` insufficient_evidence, `e13` error, `priv_think_leak` pass, `q01` error, `q02` report_only, `q03` report_only, `q04` fail, `smoke` fail | fail | pass | fail | pass | pass | — | fail | pass | pass | insufficient_evidence | report_only | [fail](full-4710ae26cb73-2026-11-05.md) |
| 2026-11-05 | `claude-haiku-5-5` | full | `4710ae26cb73` | hosted | `e01` pass, `e01_viewpoint` pass, `e02` report_only, `e03` fail, `e04` report_only, `e05` pass, `e06` fail, `e07` pass, `e09` pass, `e10` pass, `e12` insufficient_evidence, `e13` report_only, `priv_think_leak` pass, `q01` fail, `q02` report_only, `q03` report_only, `q04` fail, `smoke` fail | fail | pass | fail | pass | pass | fail | fail | pass | pass | insufficient_evidence | report_only | [fail](full-4710ae26cb73-2026-11-05-2.md) |
| 2026-11-01 | `cydonia-24b-v4.3` | structured | `74606ed2b224` | local | `e09` pass | — | — | — | — | — | — | — | pass | — | — | — | [pass](74606ed2b224-2026-11-01.md) |
| 2026-10-31 | `cydonia-24b-v4.3` | structured | `1cf75dec4046` | local | `e09` fail | — | — | — | — | — | — | — | fail | — | — | — | [fail](1cf75dec4046-2026-10-31.md) |
| 2026-10-27 | `cydonia-24b-v4.3` | structured | `8e52632a413f` | local | `e01_viewpoint` pass, `priv_think_leak` pass | pass | pass | — | — | — | — | — | — | — | — | — | [pass](8e52632a413f-2026-10-27.md) |
| 2026-10-25 | `cydonia-24b-v4.3` | structured | `cbc7d954f0e8` | local | `priv_think_leak` fail, `q04` fail | fail | — | — | — | — | — | — | — | — | — | — | [fail](cbc7d954f0e8-2026-10-25.md) |
| 2026-10-25 | `cydonia-24b-v4.3` | structured | `cbc7d954f0e8` | local | `e01_viewpoint` pass, `priv_think_leak` pass, `q04` fail | fail | pass | — | — | — | — | — | — | — | — | — | [fail](cbc7d954f0e8-2026-10-25-2.md) |
| 2026-10-24 | `cydonia-24b-v4.3` | structured | `cbc7d954f0e8` | local | `e01_viewpoint` pass, `priv_think_leak` fail, `q04` fail | fail | pass | — | — | — | — | — | — | — | — | — | [fail](cbc7d954f0e8-2026-10-24.md) |
| 2026-10-21 | `cydonia-24b-v4.3` | structured | `84411b5aaf1a` | local | `e06` fail, `e07` pass, `e10` pass | — | — | — | — | pass | — | fail | — | pass | — | — | [fail](84411b5aaf1a-2026-10-21.md) |
| 2026-10-21 | `cydonia-24b-v4.3` | structured | `2c61ec2dbc6c` | local | `e01` fail, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` fail, `e10` fail, `e12` insufficient_evidence, `e13` report_only, `q01` pass, `q02` report_only, `q03` report_only | — | fail | fail | fail | fail | pass | fail | — | fail | insufficient_evidence | report_only | [fail](2c61ec2dbc6c-2026-10-21.md) |
| 2026-10-20 | `cydonia-24b-v4.3` | structured | `775d2c4f43f3` | local | `e01` fail, `e03` fail, `e04` report_only, `e05` fail, `e06` fail, `e07` fail, `e10` fail, `q01` pass, `q02` report_only, `q03` report_only | — | fail | fail | fail | fail | pass | fail | — | fail | — | — | [fail](775d2c4f43f3-2026-10-20.md) |
| 2026-10-20 | `cydonia-24b-v4.3` | structured | `775d2c4f43f3` | local | `e12` insufficient_evidence, `e13` error | — | — | — | — | — | — | — | — | — | insufficient_evidence | report_only | [fail](775d2c4f43f3-2026-10-20-prose.md) |
| 2026-10-19 | `cydonia-24b-v4.3` | structured | `bafbf3d71ae7` | local | `smoke` pass | — | — | — | — | — | — | — | — | — | — | — | [pass](bafbf3d71ae7-2026-10-19.md) |
| 2026-10-17 | `cydonia-24b-v4.3` | structured | `9dbec729a92f` | local | `smoke` fail | — | — | — | — | — | — | — | — | — | — | — | [fail](9dbec729a92f-2026-10-17.md) |
