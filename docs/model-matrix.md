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
turn** is the mean number of model calls by purpose.

**Turn path.** Rows whose turn path is the guarded reply (#53) were measured before `respond`
existed; a live re-run through `respond` (#57) is pending and will replace them.

## Results

| Tier | Policies | Protocol | `generation` | `guardrail_check` | `lite_summary` | E-06 | Turns | p50 | p95 | Calls per turn |
|---|---|---|---|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (tokens 32/32, 128/128; temp 0: 9/10 identical) | 20 | 12.82 s | 19.78 s | 3 (generation 1, guardrail_check 2) |
| local: cydonia-24b-v4.3 | policies off | ✓ | ✓ (context ✓) | ✓ (context ✓; structured_output ✓) | ✓ (context ✓) | ✓ (tokens 32/32, 128/128; temp 0: 9/10 identical) | 20 | 8.00 s | 15.06 s | 1 (generation 1) |

## Runs

| Tier | Policies | Measured | Engine SHA | Hardware | Server | Turn path |
|---|---|---|---|---|---|---|
| local: cydonia-24b-v4.3 | minors policy on | 2026-10-11 | `f0f4b98-dirty` | Apple M3 Max, 128 GB; context 16,384 | LM Studio 0.4.18 | guarded reply (#53), until respond (#57) |
| local: cydonia-24b-v4.3 | policies off | 2026-10-11 | `f0f4b98-dirty` | Apple M3 Max, 128 GB; context 16,384 | LM Studio 0.4.18 | guarded reply (#53), until respond (#57) |

Raw timings for every row are in [model-matrix.json](model-matrix.json).
