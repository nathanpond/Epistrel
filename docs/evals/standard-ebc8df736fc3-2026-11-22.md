# Eval report standard-ebc8df736fc3-2026-11-22

<!-- epistrel-eval-report {"date": "2026-11-22", "fingerprint": "sha256:ebc8df736fc3921af8d4d08b7b20df8132698098407db9d77378bfb39aaec333", "host": "hosted", "judge": null, "judged": [], "judges": {}, "metrics": [{"floor": 0.95, "id": "smoke.turn_completed", "requirement": null, "sample_size": 200, "value": 1.0, "verdict": "pass"}, {"floor": 0.8, "id": "smoke.instruction_followed", "requirement": null, "sample_size": 200, "value": 0.9183333333333333, "verdict": "pass"}], "model": "claude-haiku-5-5", "model_version": "claude-haiku-5-5", "preset": "standard", "requirements": {}, "run_conditions": {"note": null, "observed": {}, "parse_validate_retry": {}, "structured_modes": {"belief_adjudication": "json_schema_strict", "consolidation": "json_schema_strict", "directive_parse": "parse_validate_retry", "entailment": "json_schema_strict", "extraction_backstop": "json_schema_strict", "guardrail_check": "json_schema_strict", "importance_scoring": "json_schema_strict", "lite_rewrite_parse": "json_schema_strict", "lite_rewrite_reach": "json_schema_strict", "rewrite_parse": "json_schema_strict", "rewrite_record": "json_schema_strict", "salience_detection": "json_schema_strict", "time_estimation": "json_schema_strict"}}, "run_id": "standard-ebc8df736fc3-2026-11-22", "sample_size": 200, "seeds": [20261008, 20261009, 20261010], "suites": {"smoke": "pass"}, "verdict": "pass"} -->

- **Run verdict:** pass
- **Model:** `claude-haiku-5-5` (reported version: `claude-haiku-5-5`)
- **Adapter:** `openai` at `https://api.anthropic.com/v1`
- **Judge:** the run model (self-judged)
- **Preset:** `standard`
- **Fingerprint:** `sha256:ebc8df736fc3921af8d4d08b7b20df8132698098407db9d77378bfb39aaec333` (Standard v1, with the model as every story purpose's)
- **Host:** hosted; Anthropic API; Claude API (OpenAI compatibility); temperature 0 not honoured (`deterministic` off)
- **Seeds:** 3 (20261008-20261010)
- **Sample size:** 200 cases per seed
- **Engine:** `ef59960-dirty`
- **Run:** 2026-11-22T07:37:02+00:00 to 2026-11-22T07:39:20+00:00 (UTC)
- **Structured calls:** `json_schema_strict` for `belief_adjudication`, `consolidation`, `entailment`, `extraction_backstop`, `guardrail_check`, `importance_scoring`, `lite_rewrite_parse`, `lite_rewrite_reach`, `rewrite_parse`, `rewrite_record`, `salience_detection`, `time_estimation`; `parse_validate_retry` for `directive_parse`; parse-validate-retry requests: none (`DirectiveParseResult` 0)

## `smoke`: Harness smoke: Lite turns end to end

Suite verdict: pass. 200 cases x 3 seeds; mean case time 1.8 s (informational); gated in CI on the stub. Guarantee rows measured: —.

| Metric | Floor | Rule | Mean ± SD | n | Seeds | Verdict | Bound |
|---|---|---|---|---|---|---|---|
| `smoke.turn_completed` | ≥ 95.00% | rate | 100.00% ± 0.00% | 200 | 3 | pass | — |
| `smoke.instruction_followed` | ≥ 80.00% | rate | 91.83% ± 2.75% | 200 | 3 | pass | — |

- `smoke.turn_completed`: the turn released a non-empty reply, not the safe fallback, on its designed calls
- `smoke.instruction_followed`: the reply uses the word the player asked for, or an inflection of it ("candles" for "candle")

## How to read this report

- **Rate rule** (NFR-QUAL-8): a rate metric passes when its mean across seeds clears the floor by
  more than one sample standard deviation (≥ floors: mean - SD above the floor; ≤ floors: mean +
  SD below it). One seed, or no observations, is *insufficient evidence*.
- **Observed-zero rule** (NFR-QUAL-8): a zero-tolerance metric passes only with no violation on any
  seed, over at least 300 labeled cases. The bound column gives the one-sided 95% upper bound on
  the true rate (≈ 3/n): a clean run is evidence of a low rate, not proof of a zero one.
- **Relative rule** (NFR-QUAL-14): an engine-intervention type passes when its first-draft
  preference rate's mean across seeds plus one SD is at most the floor, judged only on at least
  300 non-tied pairs per seed; fewer is *insufficient evidence*, never a pass. It is an aggregate
  target: individual messages may still read worse than their drafts.
- A judge-based row is only as good as the judge: before such a run the quality judge is
  calibrated against human-labeled pairs, and a judge whose agreement is below 75% makes every
  judge-based row *report only*.
- A model error during a case is a failed case, never silently excluded. In a suite with a
  model-error row (NFR-QUAL-1's zero-leak suites) it is counted and reported on that row, which
  is report-only, and it is not scored as a disclosure: the suite's floors are judged on its
  completed cases, and a zero-tolerance metric with fewer than 300 completed cases on a seed is
  *insufficient evidence*, never a pass. Model-robustness floors belong to M11 (ADR-164). In
  every other suite a model error counts as a bad observation on each of its metrics. Timings
  are informational: the model may share its machine with other work.
- A floor applies to the configuration this fingerprint names (NFR-QUAL-11); any other
  configuration is unmeasured until it has its own report.
- `smoke.instruction_followed` counts an inflected form of the requested word (plural -s/-es/-ies/-ves, -ed/-d/-ing: "candles" for "candle") as following the instruction; a compound ("candlestick"), another word, or no use of the word fails (#437).
