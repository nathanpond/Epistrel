# Eval report pilot-2026-11-17-e12-polished

<!-- epistrel-eval-report {"date": "2026-11-17", "fingerprint": "sha256:596a9a6b4dbe167a933cfd0c829be4407cd88ee4e57dc5c9b008b17234dcc42c", "host": "hosted", "judge": null, "judged": [], "judges": {"e12_direct": {"model": "claude-haiku-5-5", "model_version": "claude-haiku-5-5", "purposes": ["quality_judge"], "self_judged": true}}, "metrics": [{"floor": 0.55, "id": "nfr_qual_14.direct.repair_rung_1", "requirement": "NFR-QUAL-14", "sample_size": 1, "value": 1.0, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.repair_rung_1", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.repair_rung_1", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "nfr_qual_14.direct.repair_rung_2", "requirement": "NFR-QUAL-14", "sample_size": 19, "value": 0.0, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.repair_rung_2", "requirement": null, "sample_size": 20, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.repair_rung_2", "requirement": null, "sample_size": 20, "value": 0.05, "verdict": "report_only"}, {"floor": 0.55, "id": "nfr_qual_14.direct.repair_rung_3", "requirement": "NFR-QUAL-14", "sample_size": 1, "value": 0.0, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.repair_rung_3", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.repair_rung_3", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": 0.55, "id": "nfr_qual_14.direct.repair_rung_4", "requirement": "NFR-QUAL-14", "sample_size": 1, "value": 1.0, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.repair_rung_4", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.repair_rung_4", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": 0.55, "id": "nfr_qual_14.direct.composition_polished", "requirement": "NFR-QUAL-14", "sample_size": 6, "value": 0.8333333333333334, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.composition_polished", "requirement": null, "sample_size": 40, "value": 0.375, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.composition_polished", "requirement": null, "sample_size": 40, "value": 0.1, "verdict": "report_only"}, {"floor": null, "id": "nfr_qual_14.direct.composition_fallback", "requirement": "NFR-QUAL-14", "sample_size": 0, "value": null, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.composition_fallback", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.composition_fallback", "requirement": null, "sample_size": 1, "value": 1.0, "verdict": "report_only"}, {"floor": null, "id": "nfr_qual_14.direct.age_repair", "requirement": "NFR-QUAL-14", "sample_size": 1, "value": 0.0, "verdict": "insufficient_evidence"}, {"floor": null, "id": "e12.direct.not_triggered.age_repair", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.mismatch.age_repair", "requirement": null, "sample_size": 1, "value": 0.0, "verdict": "report_only"}, {"floor": null, "id": "e12.direct.completed", "requirement": null, "sample_size": 65, "value": 1.0, "verdict": "report_only"}], "model": "claude-haiku-5-5", "model_version": "claude-haiku-5-5", "preset": "full", "requirements": {"NFR-QUAL-14": "insufficient_evidence"}, "run_id": "pilot-2026-11-17-e12-polished", "sample_size": 65, "seeds": [20261008], "suites": {"e12_direct": "insufficient_evidence"}, "verdict": "insufficient_evidence"} -->

- **Run verdict:** insufficient evidence
- **Model:** `claude-haiku-5-5` (reported version: `claude-haiku-5-5`)
- **Adapter:** `openai` at `https://api.anthropic.com/v1`
- **Judge:** the run model (self-judged)
- **Preset:** `full`
- **Fingerprint:** `sha256:596a9a6b4dbe167a933cfd0c829be4407cd88ee4e57dc5c9b008b17234dcc42c` (Full v4, with the model as every story purpose's)
- **Host:** hosted
- **Seeds:** 1 [20261008]
- **Sample size:** 65 cases per seed
- **Engine:** `e36099e-dirty`
- **Run:** 2026-11-17T01:17:12+00:00 to 2026-11-17T01:19:42+00:00 (UTC)

## `e12_direct`: E-12 (M11): engine interventions on prepared drafts (direct mode)

Suite verdict: insufficient evidence. 65 cases x 1 seeds; mean case time 8.7 s (informational); gated in CI on the stub. Guarantee rows measured: `quality_judge`.

Judge: **self-judged**. The run model `claude-haiku-5-5` (reported version: `claude-haiku-5-5`) judged its own output (`quality_judge`).

Judge calibration: `claude-haiku-5-5` agreed with the human labels on 94.55% (95% CI 90.71% to 96.85%) of 220 pairs (floor 75.00%, 2026-11-05): calibrated.

| Metric | Floor | Rule | Mean ± SD | n | Seeds | Verdict | Bound |
|---|---|---|---|---|---|---|---|
| `nfr_qual_14.direct.repair_rung_1` | mean + SD ≤ 55.00% (n ≥ 300) | relative | 100.00% ± — (95% CI 20.65% to 100.00%) | 1 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.repair_rung_1` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.mismatch.repair_rung_1` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `nfr_qual_14.direct.repair_rung_2` | identity | identity | identity (19 cases) | 19 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.repair_rung_2` | none | informational | 0.00% ± — (95% CI 0.00% to 16.11%) | 20 | 1 | report only | — |
| `e12.direct.mismatch.repair_rung_2` | none | informational | 5.00% ± — (95% CI 0.89% to 23.61%) | 20 | 1 | report only | — |
| `nfr_qual_14.direct.repair_rung_3` | mean + SD ≤ 55.00% (n ≥ 300) | relative | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.repair_rung_3` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.mismatch.repair_rung_3` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `nfr_qual_14.direct.repair_rung_4` | mean + SD ≤ 55.00% (n ≥ 300) | relative | 100.00% ± — (95% CI 20.65% to 100.00%) | 1 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.repair_rung_4` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.mismatch.repair_rung_4` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `nfr_qual_14.direct.composition_polished` | mean + SD ≤ 55.00% (n ≥ 300) | relative | 83.33% ± — (95% CI 43.65% to 96.99%) | 6 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.composition_polished` | none | informational | 37.50% ± — (95% CI 24.22% to 52.97%) | 40 | 1 | report only | — |
| `e12.direct.mismatch.composition_polished` | none | informational | 10.00% ± — (95% CI 3.96% to 23.05%) | 40 | 1 | report only | — |
| `nfr_qual_14.direct.composition_fallback` | identity | identity | identity (0 cases) | 0 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.composition_fallback` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.mismatch.composition_fallback` | none | informational | 100.00% ± — (95% CI 20.65% to 100.00%) | 1 | 1 | report only | — |
| `nfr_qual_14.direct.age_repair` | identity | identity | identity (1 cases) | 1 | 1 | insufficient evidence | — |
| `e12.direct.not_triggered.age_repair` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.mismatch.age_repair` | none | informational | 0.00% ± — (95% CI 0.00% to 79.35%) | 1 | 1 | report only | — |
| `e12.direct.completed` | none | informational | 100.00% ± — (95% CI 94.42% to 100.00%) | 65 | 1 | report only | — |

- `nfr_qual_14.direct.repair_rung_1`: direct: the judge preferred the prepared draft over the final message, over the non-tied pairs where repair rung 1 changed it
- `e12.direct.not_triggered.repair_rung_1`: direct repair_rung_1 cases where repair rung 1 did not fire
- `e12.direct.mismatch.repair_rung_1`: direct repair_rung_1 cases changed by another intervention, not repair_rung_1
- `nfr_qual_14.direct.repair_rung_2`: direct: repair rung 2 fired and released the draft's prose unchanged
- `e12.direct.not_triggered.repair_rung_2`: direct repair_rung_2 cases where repair rung 2 did not fire
- `e12.direct.mismatch.repair_rung_2`: direct repair_rung_2 cases changed by another intervention, not repair_rung_2
- `nfr_qual_14.direct.repair_rung_3`: direct: the judge preferred the prepared draft over the final message, over the non-tied pairs where repair rung 3 changed it
- `e12.direct.not_triggered.repair_rung_3`: direct repair_rung_3 cases where repair rung 3 did not fire
- `e12.direct.mismatch.repair_rung_3`: direct repair_rung_3 cases changed by another intervention, not repair_rung_3
- `nfr_qual_14.direct.repair_rung_4`: direct: the judge preferred the prepared draft over the final message, over the non-tied pairs where repair rung 4 changed it
- `e12.direct.not_triggered.repair_rung_4`: direct repair_rung_4 cases where repair rung 4 did not fire
- `e12.direct.mismatch.repair_rung_4`: direct repair_rung_4 cases changed by another intervention, not repair_rung_4
- `nfr_qual_14.direct.composition_polished`: direct: the judge preferred the prepared draft over the final message, over the non-tied pairs where composition polished changed it
- `e12.direct.not_triggered.composition_polished`: direct composition_polished cases where composition polished did not fire
- `e12.direct.mismatch.composition_polished`: direct composition_polished cases changed by another intervention, not composition_polished
- `nfr_qual_14.direct.composition_fallback`: direct: composition fallback fired and released the draft's prose unchanged
- `e12.direct.not_triggered.composition_fallback`: direct composition_fallback cases where composition fallback did not fire
- `e12.direct.mismatch.composition_fallback`: direct composition_fallback cases changed by another intervention, not composition_fallback
- `nfr_qual_14.direct.age_repair`: direct: age repair fired and released the draft's prose unchanged
- `e12.direct.not_triggered.age_repair`: direct age_repair cases where age repair did not fire
- `e12.direct.mismatch.age_repair`: direct age_repair cases changed by another intervention, not age_repair
- `e12.direct.completed`: direct cases that played (a model error fails its case)

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
- E-12 direct mode (M11) plays one prepared first draft per case through the real turn pipeline (only the render is pinned to the draft) and pairs the draft with the final message when the case's declared intervention changed it. Age repair and composition fallback are identity rows: every fired case released the draft unchanged. The direct rows are reported beside the full-turn rows, never pooled with them.
- E-12 direct fingerprint `structured`: `sha256:596a9a6b4dbe167a933cfd0c829be4407cd88ee4e57dc5c9b008b17234dcc42c`.
- E-12 direct types with enough evidence on this run (300 non-tied pairs per seed; 50 fired cases for an identity type): none.
