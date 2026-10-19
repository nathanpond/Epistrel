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
       --model <loaded model> --seeds 3 \
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

| Suite | What it measures | Ships with |
|---|---|---|
| `smoke` | Lite turns end to end: the turn completes on its designed calls, and the reply follows a one-word instruction (an inflected form of the word counts; #437). 60 cases per seed. Harness smoke, no NFR-QUAL floor. | #134, #437 |
| named by their docs/04 ids (`e01`, `q01`, …) | NFR-QUAL-2..7, 10 on labeled sets | #157 |
| `e12`, `e13` | The prose floor (NFR-QUAL-14) and the text-quality signals (NFR-QUAL-15), with the calibrated `quality_judge` | #158 |

Until #157 and #158 land, `--check` lists their metrics as pending, and the milestone's exit
evidence (every NFR-QUAL-2..7, 10 floor on the reference model) is produced by the first run
after #157.
