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
releases the safe fallback. If it is the input check or the first generation, the turn fails and
the run writes no row.

**Not recorded.** Rows measured before the breakdown existed show *not recorded* in the
**Calls** columns (`null` in the JSON). Those runs kept only the mean calls per turn, and
nothing is derived from it. The next live run of the tier fills them.

## Results

| Tier | Policies | Protocol | `generation` | `guardrail_check` | `lite_summary` | `importance_scoring` | E-06 | Turns | p50 | p95 | Calls per turn |
|---|---|---|---|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | not run | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 18.31 s | 45.06 s | 3.15 (generation 1.1, guardrail_check 2.05) |
| local: cydonia-24b-v4.3 | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | not run | ✓ (tokens 32/32, 128/128; temp 0: 10/10 identical) | 20 | 13.40 s | 16.87 s | 1 (generation 1) |

## Calls

| Tier | Policies | Designed per turn | Extra calls | Failed calls | Turns with a deadline or error | p50 / p95 without them | Safe fallbacks |
|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | not recorded | not recorded | not recorded | not recorded | not recorded | not recorded |
| local: cydonia-24b-v4.3 | policies off | not recorded | not recorded | not recorded | not recorded | not recorded | not recorded |

## Runs

| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |
|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 2026-10-12 | `cd0261d` | Apple M3 Max, 128 GB; context 16,384 | LM Studio 0.4.26 | respond (#57) |
| local: cydonia-24b-v4.3 | policies off | 2026-10-12 | `cd0261d` | Apple M3 Max, 128 GB; context 16,384 | LM Studio 0.4.26 | respond (#57) |

Raw timings for every row are in [model-matrix.json](model-matrix.json).
