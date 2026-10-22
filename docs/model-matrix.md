# Model matrix

**Reported, not gated.** Each row is one model tier measured by
`uv run python -m epistrel.tools.model_matrix` on the owner's hardware, and committed after a
local run. CI does not run it.

**Protocol** means the endpoint speaks the OpenAI-compatible format: `GET /models` and one
completion work. It is not a quality claim. **Capability** is reported separately for each
purpose against that purpose's floor in [models.md](models.md) (NFR-PORT-3). The context check
is an ~8k-token prompt that completes. The structured-output check needs ≥ 95 % valid results
over 20 calls (`json_schema`, or the schema in the prompt when the endpoint refuses it).
**E-06** checks the prompt parameters: `tokens_out`/`max_tokens` per length, and how many of 10
samples at temperature 0 are identical (≥ 8 passes). **p50 / p95** are nearest-rank end-to-end
turn latencies over the measured Lite turns, after one warm-up turn (NFR-PERF-8). **Calls per
turn** is the mean number of model calls by purpose, counting every attempt (failed ones too).

**Calls** breaks those calls down (#303):

- **Designed per turn**: what each turn planned before its first call. That is one `generation`
  plus one `guardrail_check` per model-assisted policy on the input and on the output, the
  guardrail calls its budget reserved (#69).
- **Extra calls**: the calls the measured turns made beyond that design, summed over the turns.
  They are structured-output retries, the empty-reply retry, regenerations after an output
  policy flagged the draft (with their re-checks), and regenerations after the story changed
  under the turn.
- **Failed calls**: the calls whose outcome was not `ok`, by purpose and outcome. The outcome is
  `timeout` (the call's timeout or the turn's deadline), `http_error`, `invalid_output` or
  `cancelled`.
- **Turns with a deadline or error**: the turns that had a failed call, or whose deadline
  released the safe fallback. They are numbered from 1 in the order they were measured, which is
  the order of the raw timings.
- **p50 / p95 without them**: the percentiles of the other turns, so the latency those turns
  added stands out against the rest.
- **Safe fallbacks**: the turns whose reply is the safe fallback line, by cause: `deadline`,
  `retry_budget` (every regeneration the story allows was flagged too) or `call_budget`.

A call the deadline cuts is never retried. If it is an output check or a regeneration, the turn
releases the safe fallback. If it is the input check or the first generation, the turn fails:
the run writes no row for it, keeps the rows that finished, and lists the row under **Failed
runs** (#454).

**Turn kinds (P-08 M4).** A row marked *structured turns* plays single-character structured
turns (#150) on the `Structured (M4)` profile: render, detection, verification, composition and
commit. One marked *repair-heavy turns* plays the same turns on a story with planted canon
(each character owns a brass lantern), and on the stub half its renders deny it, so the repair
ladder reaches rung 3 (a `regeneration` call). For these rows the **Designed per turn** column
is the first render and the reserved guardrail calls, so **Extra calls** holds the stages a
structured turn adds (salience, entailment, composition, regeneration) as well as retries.

**Turn deadline override (#454).** A row whose turn path says *turn deadline … s (override)*
ran its turns under that per-story `budgets.deadline_ms` override, not its profile's own
deadline (the `Structured (M4)` profile's is 45 s). The matrix measures how long a turn takes,
so the profile's deadline must not cut it. Whether the profile's deadline fits a tier is read
from the p50 / p95 against it.

## Results

| Tier | Policies | Protocol | `generation` | `guardrail_check` | `consolidation` | `lite_summary` | `importance_scoring` | `entailment` | `salience_detection` | `extraction_backstop` | `regeneration` | E-06 | Turns | p50 | p95 | Calls per turn |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 12.85 s | 15.42 s | 3 (generation 1, guardrail_check 2) |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 114.18 s | 160.01 s | 9.2 (extraction_backstop 2.05, generation 2, guardrail_check 3.9, regeneration 0.25, salience_detection 1) |
| local: cydonia-24b-v4.3 | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 10.45 s | 13.20 s | 1 (generation 1) |
| local: cydonia-24b-v4.3 (structured turns) | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 126.36 s | 223.41 s | 6.1 (extraction_backstop 2.5, generation 2.15, regeneration 0.45, salience_detection 1) |

## Calls

| Tier | Policies | Designed per turn | Extra calls | Failed calls | Turns with a deadline or error | p50 / p95 without them | Safe fallbacks |
|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 3 (generation 1, guardrail_check 2) | 0 | 0 | 0 of 20 | 12.85 s / 15.42 s | 0 |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | 3 (generation 1, guardrail_check 2) | 124 (extraction_backstop 41, generation 20, guardrail_check 38, regeneration 5, salience_detection 20) | 3 (extraction_backstop timeout 2, generation invalid_output 1) | 3 of 20 (turns 14, 16, 18) | 106.68 s / 145.54 s | 5 (repair 5) |
| local: cydonia-24b-v4.3 | policies off | 1 (generation 1) | 0 | 0 | 0 of 20 | 10.45 s / 13.20 s | 0 |
| local: cydonia-24b-v4.3 (structured turns) | policies off | 1 (generation 1) | 102 (extraction_backstop 50, generation 23, regeneration 9, salience_detection 20) | 5 (extraction_backstop timeout 5) | 4 of 20 (turns 7, 8, 9, 11) | 117.27 s / 223.41 s | 7 (repair 7) |

## Runs

| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |
|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 2026-10-22 | `e9d6a7f` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | respond (#57) |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | 2026-10-22 | `debd407` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | structured respond (#150); turn deadline 300 s (override) |
| local: cydonia-24b-v4.3 | policies off | 2026-10-22 | `e9d6a7f` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | respond (#57) |
| local: cydonia-24b-v4.3 (structured turns) | policies off | 2026-10-22 | `debd407` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | structured respond (#150); turn deadline 300 s (override) |

## Embedding

The embedding endpoint (the `embedding` port, #138), measured when the run names one:
**Protocol** is `GET /models` and one embedding; **Dimension** is what every vector of that
model has (the deployment pins it at the first embedding); **Max batch** is the largest of 64,
16 and 1 texts one request embeds; **p50** is over single-text calls.

| Tier | Protocol | Reported model | Dimension | Max batch | p50 | Measured | Engine SHA |
|---|---|---|---|---|---|---|---|
| local: text-embedding-nomic-embed-text-v1.5 | ✓ | text-embedding-nomic-embed-text-v1.5 | 768 | 64 | 0.01 s | 2026-10-22 | `debd407` |

Raw timings for every row are in [model-matrix.json](model-matrix.json).
