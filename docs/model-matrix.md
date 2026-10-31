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

**Three-character turns (P-08 M5, #182).** A row marked *three-character sequential turns* plays
S-SEQUENTIAL's key room with a third primary: the player speaks and three co-located characters
render one after another (per-character renders) on the seeded `Structured` profile, under a
story override of `budgets.model_calls` = 16. Its **Designed per turn** is #156's plan for three
renders: a render and an extraction per primary, the composed message's detection, a salience
pass per render and the polish (11 calls), plus the reserved guardrail calls. *Director order*
adds the director's ordering call; *default budget* runs under the profile's own call cap (no
override). The turn path names the profile and version and the perception level the turns ran
under, how often budget planning reduced or deferred a primary, and the degradation steps the
turns took (an optional call the budget could not fit is skipped, never a failed turn).

**Pending.** The three-character rows above are the stub's; a live tier's three-character rows
are pending until its next run (`--turn-kinds three_character_sequential`).

**Turn deadline override (#454).** A row whose turn path says *turn deadline … s (override)*
ran its turns under that per-story `budgets.deadline_ms` override, not its profile's own
deadline (the `Structured (M4)` profile's is 45 s). The matrix measures how long a turn takes,
so the profile's deadline must not cut it. The override is 300 s per primary
the turn renders, up to the profile's largest deadline of 600 s (#588), so
a three-character row measured since #588 runs under 600 s.
Whether the profile's deadline fits a tier is read from the p50 / p95 against it.

## Results

| Tier | Policies | Protocol | `generation` | `guardrail_check` | `consolidation` | `lite_summary` | `importance_scoring` | `entailment` | `belief_adjudication` | `salience_detection` | `extraction_backstop` | `regeneration` | E-06 | Turns | p50 | p95 | Calls per turn |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | not run | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 12.85 s | 15.42 s | 3 (generation 1, guardrail_check 2) |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | not run | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 114.18 s | 160.01 s | 9.2 (extraction_backstop 2.05, generation 2, guardrail_check 3.9, regeneration 0.25, salience_detection 1) |
| local: cydonia-24b-v4.3 | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | not run | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 10.45 s | 13.20 s | 1 (generation 1) |
| local: cydonia-24b-v4.3 (structured turns) | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | not run | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 126.36 s | 223.41 s | 6.1 (extraction_backstop 2.5, generation 2.15, regeneration 0.45, salience_detection 1) |
| stub: stub-model (three-character, default budget turns) | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 0.96 s | 1.09 s | 16 (extraction_backstop 4, generation 4, guardrail_check 5, salience_detection 3) |
| stub: stub-model (three-character, director order turns) | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 0.93 s | 1.03 s | 14 (extraction_backstop 3, generation 4, guardrail_check 4, salience_detection 3) |
| stub: stub-model (three-character sequential turns) | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 0.95 s | 1.05 s | 16 (extraction_backstop 4, generation 4, guardrail_check 5, salience_detection 3) |
| stub: stub-model (three-character, default budget turns) | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 0.95 s | 1.09 s | 11 (extraction_backstop 4, generation 4, salience_detection 3) |
| stub: stub-model (three-character, director order turns) | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 1.03 s | 5.24 s | 12 (extraction_backstop 4, generation 5, salience_detection 3) |
| stub: stub-model (three-character sequential turns) | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓; structured_output ✓) | ✓ (tokens 17/32, 17/128; temp 0: 10/10 identical) | 20 | 0.90 s | 0.98 s | 11 (extraction_backstop 4, generation 4, salience_detection 3) |

## Calls

| Tier | Policies | Designed per turn | Extra calls | Failed calls | Turns with a deadline or error | p50 / p95 without them | Safe fallbacks |
|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 3 (generation 1, guardrail_check 2) | 0 | 0 | 0 of 20 | 12.85 s / 15.42 s | 0 |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | 3 (generation 1, guardrail_check 2) | 124 (extraction_backstop 41, generation 20, guardrail_check 38, regeneration 5, salience_detection 20) | 3 (extraction_backstop timeout 2, generation invalid_output 1) | 3 of 20 (turns 14, 16, 18) | 106.68 s / 145.54 s | 5 (repair 5) |
| local: cydonia-24b-v4.3 | policies off | 1 (generation 1) | 0 | 0 | 0 of 20 | 10.45 s / 13.20 s | 0 |
| local: cydonia-24b-v4.3 (structured turns) | policies off | 1 (generation 1) | 102 (extraction_backstop 50, generation 23, regeneration 9, salience_detection 20) | 5 (extraction_backstop timeout 5) | 4 of 20 (turns 7, 8, 9, 11) | 117.27 s / 223.41 s | 7 (repair 7) |
| stub: stub-model (three-character, default budget turns) | minors policy on | 15 (extraction_backstop 4, generation 4, guardrail_check 4, salience_detection 3) | 20 (guardrail_check 20) | 0 | 0 of 20 | 0.96 s / 1.09 s | 0 |
| stub: stub-model (three-character, director order turns) | minors policy on | 16 (extraction_backstop 4, generation 5, guardrail_check 4, salience_detection 3) | 0 | 0 | 0 of 20 | 0.93 s / 1.03 s | 0 |
| stub: stub-model (three-character sequential turns) | minors policy on | 15 (extraction_backstop 4, generation 4, guardrail_check 4, salience_detection 3) | 20 (guardrail_check 20) | 0 | 0 of 20 | 0.95 s / 1.05 s | 0 |
| stub: stub-model (three-character, default budget turns) | policies off | 11 (extraction_backstop 4, generation 4, salience_detection 3) | 0 | 0 | 0 of 20 | 0.95 s / 1.09 s | 0 |
| stub: stub-model (three-character, director order turns) | policies off | 12 (extraction_backstop 4, generation 5, salience_detection 3) | 0 | 0 | 0 of 20 | 1.03 s / 5.24 s | 0 |
| stub: stub-model (three-character sequential turns) | policies off | 11 (extraction_backstop 4, generation 4, salience_detection 3) | 0 | 0 | 0 of 20 | 0.90 s / 0.98 s | 0 |

## Runs

| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |
|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 2026-10-22 | `e9d6a7f` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | respond (#57) |
| local: cydonia-24b-v4.3 (structured turns) | minors policy on | 2026-10-22 | `debd407` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | structured respond (#150); turn deadline 300 s (override) |
| local: cydonia-24b-v4.3 | policies off | 2026-10-22 | `e9d6a7f` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | respond (#57) |
| local: cydonia-24b-v4.3 (structured turns) | policies off | 2026-10-22 | `debd407` | Apple M3 Max, 128 GB | LM Studio 0.4.26 | structured respond (#150); turn deadline 300 s (override) |
| stub: stub-model (three-character, default budget turns) | minors policy on | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation composition_fallback 20 |
| stub: stub-model (three-character, director order turns) | minors policy on | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation director_order_skipped 20, polish_skipped_budget 20, skip_polish 20 |
| stub: stub-model (three-character sequential turns) | minors policy on | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation composition_fallback 20 |
| stub: stub-model (three-character, default budget turns) | policies off | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation composition_fallback 20 |
| stub: stub-model (three-character, director order turns) | policies off | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation composition_fallback 20, director_order_skipped 20 |
| stub: stub-model (three-character sequential turns) | policies off | 2026-10-24 | `d680e09-dirty` | the stub adapter (no model) | in-process stub | three-character sequential respond (#174); turn deadline 300 s (override); profile Structured v2, perception full; no primary reduced or deferred; degradation composition_fallback 20 |

## Failed runs

A row whose turns stopped on a model failure partway. The run wrote the rows that finished
before it, and kept this row's previous measurement (if any) in the tables above. The turn is
numbered from 1 in the measured turns; turn 0 is the warm-up. The next run that measures the
row clears its entry.

| Tier | Policies | Measured | Engine SHA | Turn | Error | Turn path |
|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 (three-character sequential turns) | policies off | 2026-10-31 | `28af800-dirty` | 3 | model_unavailable, purpose `generation`, reason `invalid_structure`; `the object: Value error, beats.0.step: the render declares no steps`, `the object: Value error, beats.0.step: the render declares no steps`, `the object: Value error, beats.0.step: the render declares no steps` | three-character sequential respond (#174); turn deadline 600 s (override) |

## Embedding

The embedding endpoint (the `embedding` port, #138), measured when the run names one:
**Protocol** is `GET /models` and one embedding; **Dimension** is what every vector of that
model has (the deployment pins it at the first embedding); **Max batch** is the largest of 64,
16 and 1 texts one request embeds; **p50** is over single-text calls.

| Tier | Protocol | Reported model | Dimension | Max batch | p50 | Measured | Engine SHA |
|---|---|---|---|---|---|---|---|
| local: text-embedding-nomic-embed-text-v1.5 | ✓ | text-embedding-nomic-embed-text-v1.5 | 768 | 64 | 0.01 s | 2026-10-22 | `debd407` |

Raw timings for every row are in [model-matrix.json](model-matrix.json).
