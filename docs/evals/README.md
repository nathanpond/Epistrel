# Model-quality evals

The quality floors of docs/03 (NFR-QUAL) are measured by the eval harness, `epistrel.evals`
(docs/04 §8, NFR-QUAL-8; #134). This directory holds the committed reports and their index,
[INDEX.md](INDEX.md).

## Policy: no automated real-model runs

No automated check spends money or calls a real model (the owner's decision, 2026-10-08):

- **CI** runs every registered suite against the **stub adapter** on every pull request, two
  seeds, scripted per case. That proves the harness itself — loading the labeled sets, playing
  turns through the real code paths, the gate rules, the report — and the deterministic stub
  floors of the suites marked `ci_gate`. A stub report is never quality evidence; it is a CI
  artifact (`evals-stub-report`), not a file here.
- **The reference tier is run by hand**: a local model (LM Studio) on the owner's machine.
  There are no scheduled or nightly real-model runs. The committed report of such a run is the
  evidence a floor was met.
- **Every real-model run is committed**, whatever its verdict: a failing floor is an honest
  record, and the suite's story files a bug for each miss. A floor miss on the reference model
  keeps its milestone open until a model on that tier clears it.

## Procedure (LM Studio)

1. Install LM Studio, load the model (the reference model is the one named in the local row of
   [docs/model-matrix.md](../model-matrix.md)), and start its server (Developer tab, or
   `lms server start`). The default URL is `http://localhost:1234/v1`; use the port your server
   reports (`lms server status`). No API key is needed (`lm-studio` is sent).
2. Docker must be running: the harness starts a throwaway PostgreSQL through testcontainers, so
   it never writes into a deployment's database. To use a database of your own, pass
   `--database-url` — it must be a **scratch** database, because the run writes the endpoint
   into its model configuration and plays its turns there.
3. Run every suite with three seeds, one case at a time (the default `--concurrency 1`; a local
   server answers one request at a time anyway):

   ```sh
   uv run epistrel evals run --all --base-url http://localhost:1234/v1 \
       --model <loaded model> --embedding-model <loaded embedding model> --seeds 3 \
       --hardware "<machine, memory>" --server-version "LM Studio <version>"
   ```

   An unreachable endpoint exits 1 before anything is written. `--sample N` runs a seeded
   subset of each set (the same cases on every seed). `--suite <name>` reruns one suite and
   merges it into the same day's run of the same fingerprint, rewriting its report in place.
4. The run writes `docs/evals/<fingerprint[:12]>-<YYYY-MM-DD>.md` (a second run that day gets
   `-2`), regenerates [INDEX.md](INDEX.md), and keeps its JSON and the failed cases' inputs and
   outputs under `build/evals/` (not committed). It exits 0 whatever the floors say: the
   verdicts are in the report. Commit the report and the index together.
5. `uv run python -m epistrel.evals --check` (CI's lint job runs it) checks that the suites
   measure every model-dependent row of the guarantee table ([docs/guarantees.md](../guarantees.md)),
   that every floored metric has a floor, and that the index matches the reports.

## Reading a report

Each report states the model and the version the server reported, the configuration
fingerprint (FR-PROF-16: the seeded `Structured (M4)` profile with the model as every story
purpose's), the host, the seeds and the sample size, and one row per metric: floor, rule,
mean ± SD across seeds, cases per seed, seeds, verdict and, for a zero-tolerance metric, the
one-sided 95% upper bound.

- A **rate** floor passes when the mean clears it by more than one standard deviation.
- A **zero-tolerance** floor passes only with no violation on any seed over at least 300
  cases, and is reported as "0 observed in n cases, one-sided 95% upper bound ≈ x%" — evidence
  of a low rate, never a claim that the rate is zero.
- One seed, or no observations, is *insufficient evidence*; a model error counts as a failed
  case. Timings are informational.

## Suites

Floors come from the guarantee table (`GUARANTEES`, #132; [docs/guarantees.md](../guarantees.md)):
each suite names the rows it measures, and `--check` fails a model-dependent row no suite
measures.

| Suite | What it measures | Floor (rule) | Labeled set | Ships with |
|---|---|---|---|---|
| `smoke` | Lite turns end to end: the turn completes on its designed calls, and the reply follows a one-word instruction (an inflected form counts; #437). 60 cases per seed. Harness smoke, no NFR-QUAL floor. | ≥ 95 %, ≥ 80 % (rate) | `smoke/` | #134, #437 |
| `e01` | E-01: entailment through `Verifier.verify` (deterministic rules, the gate, the `entailment` purpose on the fuzzy residual) | false contradiction ≤ 2 %, false support ≤ 1 % (rate); accuracy reported | `entailment/` (420) | #157 |
| `e02` | E-02: escalation yield and shadow tightening | report only | `e02/` | #155 |
| `e03` | E-03: consolidation faithfulness (`consolidate_window`; reflections judged by the `entailment` purpose) | ≥ 98 % (rate) | `consolidation/` (150) | #157 |
| `e04` | E-04: importance calibration (the importance job's call; Spearman vs pivotal/trivial) | report only | `importance/` (200) | #135, #157 |
| `e05` | E-05: salience detection (`detect_salience`) | recall ≥ 95 %, precision ≥ 70 % (rate); F-beta (FN x3) reported | `salience/` (300) | #157 |
| `e06` | E-06 (M4): structured generation conformance (`respond`: structure, length, temperature-0 repeatability) | ≥ 98 % (rate) | `generation_conformance/` (200) | #157 |
| `e07` | E-07: structured output valid after retry; injected garbage takes the degradation path | ≥ 99 % (rate); never silent acceptance | `structured_output/` (200) | #157 |
| `e10` | E-10: claim-detection recall per `extraction_mode`, layer and category (`ClaimDetector`) | ≥ 95 % per mode (rate) | `claim_detection/` (300) | #157 |
| `q01` | Q-01: recall@10 and MRR over the 5,000-turn S-THOSEGUYS-style fixture (`Engine.retrieve`) | recall@10 ≥ 0.9 (rate) | `retrieval/recall.jsonl` (100) | #157 |
| `q02` | Q-02: referential and temporal resolution (the retrieval pipeline's resolve stage) | report only | `retrieval/resolution.jsonl` (120) | #139, #157 |
| `q03` | Q-03: abstract-query recall with vs without the reflection tier | report only | `retrieval/abstract.jsonl` (60) | #143, #157 |
| `e12`, `e13` | The prose floor (NFR-QUAL-14) and the text-quality signals (NFR-QUAL-15), with the calibrated `quality_judge` | | | #158 |

Until #158 lands, `--check` lists NFR-QUAL-14 as pending (NFR-QUAL-9 waits for M6).

## Labeled sets

The sets under `evals/sets/` are authored by the builder and approved by the owner (#157).
#157's are generated by `src/epistrel/evals/authoring/` from hand-written templates (no model):
`uv run python -m epistrel.evals author` writes them, and each set directory's `PROVENANCE.md`
says how its set was made and carries the review line. The owner reviews
[sets-digest.md](sets-digest.md) (`uv run python -m epistrel.evals digest`) and records approval
by changing `approved by owner: pending` to `approved by owner YYYY-MM-DD`. CI's
`--check-sets` validates every set against its suite's schema and size, its authoring module
and the digest; a `pending` set fails it on `main`.

Retrieval suites embed their stories only when the run names an embedding model at the same
endpoint (`--embedding-model`, e.g. LM Studio's `text-embedding-nomic-embed-text-v1.5`);
without one their vector arm does not run. A long real-model run can be split: `--suite`
reruns merge into the same day's run, and `--sample N` (stated in the report as the cases per
seed) bounds a slow suite.
