# Model-quality evals

The quality floors of docs/03 (NFR-QUAL) are measured by the eval harness, `epistrel.evals`
(docs/04 §8, NFR-QUAL-8; #134). This directory holds the committed reports and their index,
[INDEX.md](INDEX.md).

## Policy: no automated real-model runs

No automated check spends money or calls a real model (the owner's decision, 2026-10-08):

- **CI** runs every registered suite against the **stub adapter** on every pull request, two
  seeds, scripted per case. That proves the harness itself — loading the labeled sets, playing
  turns through the real code paths, the gate rules, the report — and the deterministic stub
  floors of the suites marked `ci_gate`. CI runs it through the certification command
  (`certify --presets <preset> --adapter stub`, one preset per job, #235), so the certification
  path is exercised too. A stub report or record is never quality evidence; it is a CI artifact
  (`evals-certify-stub-<preset>`), not a file here.
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
3. Run every suite with three seeds (`--concurrency N` plays N cases at once: LM Studio serves
   several requests in parallel, 4 slots by default, and batching them raises its throughput):

   ```sh
   uv run epistrel evals run --all --base-url http://localhost:1234/v1 \
       --model <loaded model> --embedding-model <loaded embedding model> --seeds 3 \
       --hardware "<machine, memory>" --server-version "LM Studio <version>"
   ```

   `--preset lite|standard|full|max` (#227, default `full`) picks the preset every scratch story
   plays under and the fingerprint names; a suite that needs a module the preset leaves off is
   reported `not_applicable`, with what it needs, and never runs (NFR-QUAL-11). Run each preset
   to compare them on quality as well as cost.
   An unreachable endpoint exits 1 before anything is written. `--sample N` runs a seeded
   subset of each set (the same cases on every seed). `--suite <name>` reruns one suite and
   merges it into the same day's run of the same fingerprint, rewriting its report in place.
4. The run writes `docs/evals/<preset>-<fingerprint[:12]>-<YYYY-MM-DD>.md` (a second run that
   day gets `-2`; reports written before #227 are named `<fingerprint[:12]>-<YYYY-MM-DD>.md`, and
   their INDEX row reads preset `structured`), regenerates [INDEX.md](INDEX.md), and keeps its JSON and the failed cases' inputs and
   outputs under `build/evals/` (not committed). It exits 0 whatever the floors say: the
   verdicts are in the report. Commit the report and the index together.
5. `uv run python -m epistrel.evals --check` (CI's lint job runs it) checks that the suites
   measure every model-dependent row of the guarantee table ([docs/guarantees.md](../guarantees.md)),
   that every floored metric has a floor, and that the index matches the reports.

## Certify the presets (#235, NFR-QUAL-11)

One command certifies Standard, Full and Max on one model, in that order, in one process (owner
decision 2). The committed reports and certification records are the preset certification
evidence; a deployment loads the records at start-up, so the presets on a certified model report
`certified` with no run of their own, and on any other model `uncertified` (FR-PROF-16).
Certifications are kept **per model**: records of several models sit side by side, and
[INDEX.md](INDEX.md)'s **Presets** table has one row per preset and model (#730).

**Prerequisites.** An OpenAI-compatible endpoint serving the model, an embedding model (every
preset needs embeddings), and Docker running (the harness starts its own scratch PostgreSQL).

**On a hosted model** (M7's certification: Claude Haiku 5.5 through Anthropic's
OpenAI-compatible endpoint, with the embeddings on LM Studio, since Anthropic has no embeddings
API). Put the key in the environment, not on the command line:

```sh
export EPISTREL_EVALS_API_KEY=<Anthropic API key>
uv run epistrel evals certify --presets standard,full,max \
    --base-url https://api.anthropic.com/v1 --model claude-haiku-5-5 \
    --embedding-model text-embedding-nomic-embed-text-v1.5 \
    --embedding-base-url http://localhost:1234/v1 \
    --seeds 3 --concurrency 8 \
    --hardware "Anthropic API" --server-version "Claude API (OpenAI compatibility)"
```

A run against an endpoint off this network records the host kind `hosted` (#757), and `certify`
refuses one without `--hardware` and `--server-version`. The four Claude Haiku 5.5 reports of
2026-11-05 were run without them and said `local`. They were corrected offline on 2026-11-06
with the strings above, and their records' `report_sha256` was recomputed. The same pass
re-rendered the Full report's refused `q01` section, so its NFR-QUAL-6 floor reads failed with
n=0 instead of being left out (#756). No case result changed.

That refusal was the engine's, not the model's (#759): the endpoint's 400 to `json_schema` was
charged to story creation's guardrail budget, which is exactly one call per text, so the last
definition's check could not run and the minors policy failed closed. With the probe refunded,
Full was resumed on 2026-11-06 with `--retry-errors none --force`. Only `q01` played (300 case
runs), and NFR-QUAL-6 now passes. Every other suite's results came from the same journal, so they
are unchanged. The record now points to `full-4710ae26cb73-2026-11-06.md`.

Claude 4.7 and later models refuse `temperature`; the adapter drops it for them and retries
once, remembered per model (#235). Anthropic ignores `seed`, so a hosted run's seeds vary the
case order, not the sampling. Raise `--concurrency` as far as the account's rate limits allow:
a rate-limited case is a model error and plays again on the next run (below).

**On the local reference model** (the local row of [docs/model-matrix.md](../model-matrix.md),
`cydonia-24b-v4.3`), add `--reference` so the records are `host.tier = reference`:

```sh
uv run epistrel evals certify --presets standard,full,max \
    --base-url http://localhost:1234/v1 --model cydonia-24b-v4.3 \
    --embedding-model <embedding model> --seeds 3 --reference --concurrency 4 \
    --hardware "<machine, memory>" --server-version "LM Studio <version>"
```

It probes the endpoint once and calibrates the judge once, shared by the presets; a stored
calibration of the same judge model and reported version is reused (`--recalibrate` runs it
again). Each preset prints its fingerprint and how many case runs it plans, progress lines with
an ETA go to stderr, and a `metric_id | verdict | n | bound` table prints as it finishes. It
writes, per preset:

- `docs/evals/<preset>-<fingerprint[:12]>-<YYYY-MM-DD>.md`, the report;
- `src/epistrel/core/seeds/certifications/<preset>-<fingerprint[:12]>.json`, the certification
  record (the floors passed, failed and insufficient; the report's SHA-256, with the report
  embedded so the installed wheel validates it too). A preset with no floor passed gets its
  report and no record.

Then it regenerates [INDEX.md](INDEX.md).

**Stop and carry on** (#730). Every case's outcome goes to the results journal,
`.evals-results/<model>/<preset>-<fingerprint[:12]>.jsonl` (ignored by git: it keeps the
model's output), as the case ends. Running **the same command again** plays only the cases the
journal lacks, on any later day and with any engine commit; the report then lists the commits
its results came from (`**Resumed:**`). Press Ctrl-C once to stop: the cases in flight finish
and are journalled, and the command exits 130 without a report. A second Ctrl-C stops at once
(every finished case is still kept). A case a transient model error ended (a timeout, a rate
limit, a provider 5xx) plays again on the next run; `--retry-errors all` replays every model
error, `none` none. `--fresh` sets the journal aside (renamed, never deleted) and plays every
case again. Journals are kept per model and configuration fingerprint, so the results of every
model run are there for later comparison. `--journal-dir` moves them.

**Expected duration.** Standard plans 8,718 case runs at 3 seeds; Full and Max run more
suites, so more (each preset prints its count as it starts). A case is a few model calls (a
structured turn is ~7). Measured: the local reference model on an M3 Max at `--concurrency 1`
ran ~620 calls an hour, well over a week for all three presets; batching (`--concurrency 4`)
cuts that by an estimated 2–3×. Hosted `claude-haiku-5-5` at `--concurrency 8` ran Standard in about
2 hours (~72 cases a minute; the judged quality suites are the slowest) for about $10, about
$0.0011 a case (2026-11-04, #235). The
progress lines give the real ETA. Most
local turns run past the presets' `deadline_ms` (Standard 45 s, Full 60 s, Max 120 s) and are
degraded, which the report shows (#454, M9). The presets are certified as shipped: the run never
overrides a deadline.

**What to commit**, exactly: the reports (`docs/evals/standard-*.md`, `full-*.md`, `max-*.md`),
`docs/evals/INDEX.md`, `docs/evals/judge-calibration.json` and the records
(`src/epistrel/core/seeds/certifications/{standard,full,max}-*.json`). Never the journal. Then
file one bug per floor the Presets table shows failed (`area:evals`, `sev:medium`, linking the
report): a miss of a floor assigned to M9 (NFR-QUAL-2..4, 7, 14) goes to M9; any other follows
ADR-164.

Options: `--host`, `--hardware` and `--server-version` describe the run as `run` does;
`--resume` skips a preset that already has a record for its fingerprint, whatever its date;
`--force` certifies a preset again over its record (a new certification id; without it the
command exits 2 before running anything); `--out <dir>` writes `reports/` (with a scratch
`INDEX.md`), `records/` and the journal there instead of the committed locations. `host.tier` is
`reference` only with `--reference`, a real model and the full sets: `--sample` forces `other`.
Lite is not certified in M7 (its one floor gates from M9); `--presets lite` certifies it the
same way. A preset edited after its certification, or a prompt template changed, turns its
Presets rows `uncertified (preset changed since certification <id>)`, and CI's
`evals index --check` stays red until the index is regenerated: the claim never outlives the
configuration it was measured on.

Exit codes: 0 once every report is written, whatever the floors say; 1 on an endpoint or
harness error (the presets finished so far keep their outputs, and the index is regenerated);
2 on a usage error; 130 when stopped.

## Reading a report

Each report states the model and the version the server reported, the configuration
fingerprint (FR-PROF-16: the run's preset, by default Full, with the model as every story
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
| `e09` | E-09 (M6): time estimation. Each narrated skip runs the live path's `time_estimation` call (no render) in a fixture story with the clock on; an estimate the engine would not take (a model error, an unparsable answer, 0 minutes, a confidence under the floor) is out of range. Also reports, informationally, the model-initiated skips removed per 100 turns over a 10-scenario sample of `scenarios/` played with the clock on. | ≥ 85 % in range (rate) | `time_estimation/` (200) | #201 |
| `e10` | E-10: claim-detection recall per `extraction_mode`, layer and category (`ClaimDetector`) | ≥ 95 % per mode (rate) | `claim_detection/` (300) | #157 |
| `e11` | E-11 (M8): image-policy checking for one `image check` model (standalone: `run --suite e11` only). Each case runs `check_output` with the manifest's subjects and true ages; per policy the false-negative and false-positive rates with Wilson 95 % bounds and the checker fingerprint, per tag too. The minors policy's false negatives are never measured (ADR-182): certified only by external attestation; otherwise an uncertified backstop. | the deployment's `image_check_floors` (rate); none set: report only | `e11/` (generated by `author e11`, images never committed; #271 commits the owner's set) | #270 |
| `q01` | Q-01: recall@10 and MRR over the 5,000-turn S-THOSEGUYS-style fixture (`Engine.retrieve`) | recall@10 ≥ 0.9 (rate) | `retrieval/recall.jsonl` (100) | #157 |
| `q02` | Q-02: referential and temporal resolution (the retrieval pipeline's resolve stage) | report only | `retrieval/resolution.jsonl` (120) | #139, #157 |
| `q03` | Q-03: abstract-query recall with vs without the reflection tier | report only | `retrieval/abstract.jsonl` (60) | #143, #157 |
| `e12` | E-12 (M4): engine interventions and prose. Every turn where a repair rung, the composition or its polish changed the first draft is a pair; the masked pair is judged by `quality_judge` in both orders. First-draft preference rate per intervention type and fingerprint, with its Wilson 95 % interval. | mean + SD ≤ 55 % on ≥ 300 non-tied pairs per seed, else insufficient evidence (relative) | `scenarios/` (21 scenarios × 4 turns) | #158 |
| `e13` | E-13 (M4): text-quality signals per fingerprint: repetition, repeated openings, length accuracy (model-free), voice consistency and story-driver adherence (judge scores, uncalibrated in M4), and the Spearman correlation with users' ratings | report only (no gate) | `scenarios/` | #158 |
| `e01_viewpoint` | E-01 (M5): a character's own claims through `Verifier.verify` against canon and its belief partition, with the declared intent: lies, false beliefs and belief-consistent claims, none of which may be repaired | false contradiction ≤ 2 %, false support ≤ 1 % (rate; NFR-QUAL-2's floors gate from M9, ADR-164); accuracy reported | `entailment_viewpoint/` (300) | #179 |
| `q04` | Q-04: knowledge asymmetry. A character is asked about another character's private fact through `respond` (per-character partitions, perception `full`, the output read as the player's character); the player-facing answer must hold none of the fact's forbidden forms. Negative controls (the holder asked) show the oracle detects a disclosure. | 0 observed on ≥ 300 cases a seed (zero) | `asymmetry/` (384 + 32 controls) | #179 |
| `priv_think_leak` | NFR-QUAL-1's private-thought metric: a scripted prelude plants a character's `think`, then another character answers the player; neither turn's player-facing output may disclose the thought (verbatim, paraphrased or as narrated inner state) | 0 observed on ≥ 300 cases a seed (zero) | `think_leak/` (345 + 96 controls) | #179 |

## E-11: image-policy checking (#270)

E-11 measures an image checker, not an engine configuration, so it runs on its own and no preset
run or certification run includes it. Its set is generated on the owner's machine by a local image
model (`uv run python -m epistrel.evals author e11 --base-url <local endpoint> --model <id>
[--backend a1111] [--policy <policy_id>:nudity]`). The generator applies the eval-set safety rule
(`src/epistrel/evals/e11_safety.py`) to its whole plan before it writes anything: the minors policy
gets no violation cases, and a sexual or nude case shows only clearly adult subjects. Every case is
written `reviewed: false`, with the provenance line reset to pending. No image is committed
(`evals/sets/e11/**/*.png` is gitignored); the manifest records each image's `image_sha256`.

    uv run python -m epistrel.evals run --suite e11 --base-url http://localhost:1234/v1 \
        --model <vision model> --floors docs/evals/e11-floors.json --seeds 3 \
        --hardware "…" --server-version "…"

The run prints the checker's fingerprint. It writes `docs/evals/e11-<fingerprint>-<date>.md`
and, for each policy that passed both its floors on an approved, fully reviewed and covered set,
an `eval_run` certification record under `src/epistrel/core/seeds/certifications/image-check/`.
The minors policy is certified only by recording the checker vendor's published evaluation:
`POST /admin/certifications/image-check` with `evidence_kind: external_attestation`.

**The owner's run is [E11-RUNBOOK.md](E11-RUNBOOK.md) (#271).** It covers a scratch database,
the preflight (`run --suite e11 --preflight`: one synthetic PNG through the checker, plus the
run's time estimate), the generator through Draw Things (`--backend a1111`) or the localhost
shim `tools/e11_image_shim.py` (ComfyUI, mflux), the label review with
`tools/review_e11_set.py`, the PROVENANCE approval, the proposed floors, the run (local or
hosted) and the follow-up PR `m8-e11-evidence`. `INDEX.md`'s "Image checks" table lists the
newest E-11 report per image-output policy and whether a committed record certifies its checker.

## E-09: time estimation (#201)

`e09` measures NFR-QUAL-9 on the reference model tier: `cydonia-24b-v4.3`, served by LM Studio
on the owner's machine (port 1235, or the port `lms server status` reports). Each of the 200
labeled skips (`evals/sets/time_estimation/`, owner-approved) runs #199's `time_estimation`
call exactly as a turn makes it, with the route of a fixture story on the seeded profile with
the clock on; no render is generated. `nfr_qual_9.within_range` counts an estimate in range
when the engine took it and its minutes lie in the case's acceptable range. A model error, an
unparsable answer, 0 minutes, or a confidence under the floor is out of range, as a turn would
remove that skip. The floor is the live path's: the structured layer's `uncertain_below` (0.5
by default) and then `MIN_CONFIDENCE` (0.3). Rows per activity family and per kind (explicit,
implicit, inferred) are reported. The pacing-health row,
`e09.model_initiated_skips_removed_per_100_turns`, is informational: the `unprompted_skip`
repairs per turn over a seeded sample of 10 `scenarios` cases (fewer under `--sample`) played
with the clock on, shown as a percentage, so 2.00% is 2 removals per 100 turns.

CI runs it on the stub in the `evals` job (the stub answers each case's midpoint). The real
model runs only locally, never in CI and never on a schedule. Load only `cydonia-24b-v4.3` (no
judge or embedding model), then:

```sh
uv run epistrel evals run --suite e09 --new-run --base-url http://localhost:1235/v1 \
    --model cydonia-24b-v4.3 --seeds 3 \
    --hardware "<machine, memory>" --server-version "LM Studio <version>"
```

That is about 600 short calls, plus the scenario sample's turns. Commit the report and
`INDEX.md` whatever the verdict. A miss is filed as a bug and fixed for any engine-side cause; a
miss that is the model's own may move to M9 by ADR-164's route.

## NFR-QUAL-1's zero-leak run (#179)

`q04` and `priv_think_leak` measure NFR-QUAL-1 on the final player-facing output. Their stories
run the seeded profile with M5's per-character partitions and `full` perception (each result
names that configuration and its own fingerprint). Until #181 delivers those modules, the two
suites deliver them for their own run. A disclosure the pipeline repaired before the output is
reported (`q04.render_repaired`), not counted. A clean run is reported as "0 observed in n
cases" with its one-sided 95% upper bound: evidence of a low rate, never a claim that no
disclosure can happen.

`priv_think_leak` checks every case against every form of its fact: the thought's words, the
fact's names and paraphrases, and its narrated inner state, whatever the case's category
(#562). Its category rows count each category's cases, and each sees a disclosure of any kind.
Its 96 controls (#575) voice the thought in each kind, 32 each: the thought's own words, a
paraphrase of the fact, and its narrated feeling. The reports of 2026-10-24 and 2026-10-25 predate this: each case was then checked against its own
category's forms only, so a paraphrased or narrated disclosure was looked for in about a third
of the cases each, and their bound on `nfr_qual_1.think_leak` overstates the coverage of those
two kinds. A new local run supersedes them.

**Which phrasings the bound covers (#575).** The oracle (`priv_think_leak.oracle`) sees a
disclosure in three ways. (1) One of the fact's listed forms, the thought's words or a listed
feeling as a phrase (#148's whole-word folded matcher), or an echo of the thought's words
(#152's shingle detector). (2) A rewording within one sentence, by lemma and stem matching
(`src/epistrel/evals/rewording.py`; in-repo tables, no model or NLP library): every content word
of a multi-word form in any order and inflection ("Frank has sold the farm", "the farm's been
sold"); an emotion word of the same family as a listed feeling (fear, shame, pleasure, distress,
intent) beside a word of the fact's topic ("Frank feels guilty about the farm"); or at least 60%
of the thought's content words ("Frank signed the contract on Tuesday"). (3) A `think` or
`latent` beat in the player-facing result. A clean run's "0 observed in n" and its upper bound
cover those phrasings only. A paraphrase sharing no content word with a listed form, feeling or
thought ("he's letting the place go to a developer"), a disclosure spread over several
sentences, or a bare implication is not seen; there is no judged fallback. The rule can also
over-read a sentence that pairs an emotion word with a topic word and discloses nothing, so a
violation is read in its case's output before it is counted as a leak. The reports before #575
matched the listed phrases only.

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

## Canaries (opt-in, #234)

A hosted model can change behind an unchanged name. A deployment may re-check its certifications
in use, but **canaries are off by default** (the owner's decision 3): every canary run costs real
model calls, and the repository's CI never turns them on.

- `certification_canary_enabled` (default `false`), `certification_canary_interval_s` (default 7
  days; 1 hour to 90 days), `certification_canary_sample_size` (cases per suite, default 50;
  10-1000) and `certification_canary_seeds` (default 1; 1-5), through `PUT /admin/settings`.
- Each due time queues one `certification_run` of `kind: canary` per `valid` certification in use
  (a live story's current fingerprint, or a reference certification), on the suites behind its
  passed floors. Its cost is #233's estimate: cases (capped by the sample size) × seeds × the
  suite's calls per case, at about 2,000 tokens a call.
- Only a measured miss invalidates: a floor the certification passed that now fails marks it
  `stale` (`invalidated_reason: canary`, the floors in `failed_floors`). Insufficient evidence
  is `inconclusive` and changes nothing. NFR-QUAL-8 needs two seeds for a standard deviation,
  so a one-seed canary is always `inconclusive`; set `certification_canary_seeds` to 2 or more
  for a canary that can invalidate. A zero-tolerance floor needs 300 cases a seed: a smaller
  sample can only find violations, never confirm a clean rate.
- Sample size is the operator's trade-off: a 50-case canary catches a large regression, not a
  small one.
- `certification_recertify_interval_s` (default `null`: off; 1-365 days) queues a full
  certification run (the capability's suites, 3 seeds) per in-use certified fingerprint.
- `POST /admin/certifications/{id}/invalidate` (`reason: drift|manual`, a `note`) declares drift
  by hand. A later certification run restores `certified` with a new id; the stale record is
  kept.
- A model's reported version (the response's `system_fingerprint`) is part of the fingerprint:
  a version change is a new, `uncertified` configuration, never `stale`.

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
