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
purpose's, and a second judge model as `quality_judge`'s when one is set; with
`--real-purpose`, the stub as every other purpose's, listed as **Routed purposes**), a suite's
own configuration and fingerprint when its stories run another (#179's M5 modules), the judge,
the host,
the seeds and the sample size, and one row per metric: floor, rule,
mean ± SD across seeds, cases per seed, seeds, verdict and, for a zero-tolerance metric, the
one-sided 95% upper bound.

- A **rate** floor passes when the mean clears it by more than one standard deviation.
- A **zero-tolerance** floor passes only with no violation on any seed over at least 300
  cases, and is reported as "0 observed in n cases, one-sided 95% upper bound ≈ x%" — evidence
  of a low rate, never a claim that the rate is zero.
- A **relative** floor (E-12, NFR-QUAL-14) is judged only on at least 300 non-tied pairs per
  seed and passes when the mean + SD is at most the floor; fewer pairs is *insufficient
  evidence*, never a pass. A judge-based row also states the judge calibration it ran under.
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
| `e12` | E-12 (M4): engine interventions and prose. Every turn where a repair rung, the composition or its polish changed the first draft is a pair; the masked pair is judged by `quality_judge` in both orders. First-draft preference rate per intervention type and fingerprint, with its Wilson 95 % interval. | mean + SD ≤ 55 % on ≥ 300 non-tied pairs per seed, else insufficient evidence (relative) | `scenarios/` (21 scenarios × 4 turns) | #158 |
| `e13` | E-13 (M4): text-quality signals per fingerprint: repetition, repeated openings, length accuracy (model-free), voice consistency and story-driver adherence (judge scores, uncalibrated in M4), and the Spearman correlation with users' ratings | report only (no gate) | `scenarios/` | #158 |
| `e01_viewpoint` | E-01 (M5): a character's own claims through `Verifier.verify` against canon and its belief partition, with the declared intent: lies, false beliefs and belief-consistent claims, none of which may be repaired | false contradiction ≤ 2 %, false support ≤ 1 % (rate; NFR-QUAL-2's floors gate from M9, ADR-164); accuracy reported | `entailment_viewpoint/` (300) | #179 |
| `q04` | Q-04: knowledge asymmetry. A character is asked about another character's private fact through `respond` (per-character partitions, perception `full`, the output read as the player's character); the player-facing answer must hold none of the fact's forbidden forms. Negative controls (the holder asked) show the oracle detects a disclosure. | 0 observed on ≥ 300 cases a seed (zero) | `asymmetry/` (384 + 32 controls) | #179 |
| `priv_think_leak` | NFR-QUAL-1's private-thought metric: a scripted prelude plants a character's `think`, then another character answers the player; neither turn's player-facing output may disclose the thought (verbatim, paraphrased or as narrated inner state) | 0 observed on ≥ 300 cases a seed (zero) | `think_leak/` (345 + 32 controls) | #179 |

`--check` lists NFR-QUAL-9 as pending until M6 delivers time estimation.

## NFR-QUAL-1's zero-leak run (#179)

`q04` and `priv_think_leak` measure NFR-QUAL-1 on the final player-facing output. Their stories
run the seeded profile with M5's per-character partitions and `full` perception (each result
names that configuration and its own fingerprint). Until #181 delivers those modules, the two
suites deliver them for their own run. A disclosure the pipeline repaired before the output is
reported (`q04.render_repaired`), not counted. A clean run is reported as "0 observed in n
cases" with its one-sided 95% upper bound: evidence of a low rate, never a claim that no
disclosure can happen.

CI runs both on the stub, full sets, two seeds, as the `evals-zero-leak` job (the `--all` job
skips them). The M5 exit evidence is one local run of the three M5 suites, under one run id,
with only `generation` on the local model (`--real-purpose generation`): every other purpose
gets the stub's default answers, and each case's prelude is scripted on every run. The labeled
sets must be approved first (their `PROVENANCE.md`):

```sh
uv run python -m epistrel.evals run --suite q04 --suite priv_think_leak --suite e01_viewpoint \
    --new-run --base-url http://localhost:1234/v1 --model <loaded model> \
    --embedding-model <loaded embedding model> --real-purpose generation --seeds 2 \
    --concurrency 2 --hardware "<machine, memory>" --server-version "LM Studio <version>"
```

A disclosure on the local model is a privacy defect: file one `sev:high` `area:orchestrator`
bug per suite naming the case ids and the fingerprint (never the output text). It blocks M5:
NFR-QUAL-1 does not take ADR-164's route to M9. The `e01_viewpoint` rows are reported; their
NFR-QUAL-2 floors gate from M9.

## The quality judge and its calibration (#158)

E-12 is judged by the `quality_judge` purpose (the `default` model unless `/admin/model-config`
routes it; never in a turn, never on a live story). Before its rows count, the judge is
calibrated against human preference labels:

```sh
uv run epistrel evals calibrate-judge --base-url http://localhost:1234/v1 --model <loaded model>
```

It judges the 220 labeled pairs of `evals/sets/judge_calibration/` (builder-labeled,
owner-approved; see its `PROVENANCE.md` for what the set does and does not measure) in both
orders. It writes the agreement and its Wilson 95 % interval, by judge model, to
[judge-calibration.json](judge-calibration.json); commit it with the run's report. A judge
below 75 % agreement makes every E-12 row *report only*. `run --all` calibrates first;
`run --suite e12` reads the stored calibration of its judge model and exits 1 before any case
when there is none. Changing the judge model needs a new calibration: earlier reports keep the
judge they recorded.

## A second judge model (#475)

By default the run model is its own judge. E-03 judges the reflections it wrote with the
`entailment` purpose, and E-12/E-13 judge its prose with `quality_judge`. A model can be lenient
with its own output, so a run can name a second **judge model**. The harness's judge calls then
go to it: E-03's entailment judgement, E-12's pairwise judgements, E-13's scores and the judge
calibration. Every engine call stays on `--model`: turns, the verifier's own entailment
fallback (E-01, E-02), consolidation and every job. `e03.judge_agreement` then measures the
judge model.

To serve both models from one LM Studio:

1. Load the run model as usual. Then load the judge as well: in the Developer tab, use
   *Load model* again, or run `lms load <judge model>`. LM Studio serves every loaded model at
   the same URL, each under its own identifier. `lms ps` lists the loaded models and their
   identifiers, and `GET /v1/models` shows them as well. Both models must fit in memory together.
   If they don't, load a smaller judge, or serve the judge on another machine or port.
2. Pass the judge by its identifier. For example, the reference run (`cydonia-24b-v4.3`) with
   `qwen/qwen3-32b` (a 4-bit quant) as its judge, both loaded in one LM Studio on port 1235:

   ```sh
   uv run epistrel evals calibrate-judge --base-url http://localhost:1235/v1 \
       --model cydonia-24b-v4.3 --judge-model qwen/qwen3-32b
   uv run epistrel evals run --all --base-url http://localhost:1235/v1 \
       --model cydonia-24b-v4.3 --judge-model qwen/qwen3-32b \
       --embedding-model <embedding model> --seeds 3 \
       --hardware "<machine, memory>" --server-version "LM Studio <version>"
   ```

   Under structured output, LM Studio suppresses Qwen3's thinking, so each judgement comes
   back as plain JSON.

   `--judge-base-url` and `--judge-api-key` default to `--base-url` and `--api-key`. Set them
   when the judge is served elsewhere. The judge's endpoint is probed before any case, like
   the run's. An unreachable judge exits 1 before anything is written.

The calibration is stored under the judge model, so `calibrate-judge --judge-model X` and
`run --suite e12 --judge-model X` share it. The report has a **Judge** line that names the judge
model, the version it reported and its endpoint. Each judged suite (`e03`, `e12`, `e13`) says
which model judged it. Without `--judge-model`, the report says the run model judged
(**self-judged**). The configuration fingerprint then also names the judge, as the
`quality_judge` purpose's model (FR-PROF-16), so a judged run gets its own report file and index
row, apart from a self-judged run of the same model. E-13 pools the committed reports' voice
means only with means from the same judge. A report written before #475 records no judge: it
was self-judged.

Voice fidelity and plot adherence are outside the guarantee (NFR-QUAL-13): the engine delivers
each character's definition and the director's drivers to the right contexts (C-25), and E-13
reports how the output reads as signals, never as a floor or a gate. The only prose floor is
E-12's NFR-QUAL-14, an aggregate, relative target for the engine's own interventions: individual
messages may still read worse than their drafts.

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
