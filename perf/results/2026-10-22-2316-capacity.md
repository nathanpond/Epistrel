# Capacity exploration (P-10)

> Written by `python -m epistrel.tools.capacity` or `tests/perf/test_p_10.py` (#153, #498). This page is informational and never a release gate. It extends `docs/capacity.md`'s measured curve and claims no capacity beyond the steps below.

**Host: local-mac-loaded (not the reference host)**, macOS-27.0.1-arm64-arm-64bit-Mach-O, 16 vCPU, 128.0 GB RAM; Postgres 16.15 (Debian 16.15-1.pgdg12+2), pgvector 0.8.7, `shared_buffers` 128MB.

The story starts as the scale fixture at 1,000 memories and 2,000 events and grows by memories only; each step also commits 600 turns. Each index build runs serially under the step's `maintenance_work_mem`, the way a pin builds it.

| Memories | HNSW build (s) | Build memory | Index (MiB) | Bulk memory rows/s | Live memory rows/s | Event rows/s | Commit p50 | p95 | Retrieval p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 250,000 | 378.9 | 1GB, fit in memory | 976.6 | 1,684 | 74 | 100 | 17.1 | 20.4 | 240.5 | 351.9 | 403.7 |

Latencies are in milliseconds. First budget that fails: at 250,000 memories, retrieval p95 351.9 ms > 150 ms.

Unmeasured: 1,000,000, 5,000,000 memories. No figure and no capacity is claimed for them.

Run notes: this is one recorded run of `tests/perf/test_p_10.py` (`-m perf_capacity`, `EPISTREL_TEST_CAPACITY_UP_TO=250000`). It took 10 min 20 s of wall-clock time on a developer Mac shared with other test runs, hence the `local-mac-loaded` label. Postgres ran in Docker through testcontainers, with its default `shared_buffers`. The 1M and 5M steps were not attempted on this host. The raw figures are in `perf/results/2026-10-22-2316-capacity.json`.
