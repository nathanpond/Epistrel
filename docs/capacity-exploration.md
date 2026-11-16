# Capacity exploration (P-10)

> Written by `python -m epistrel.tools.capacity` or `tests/perf/test_p_10.py` (#153, #498). This page is informational and never a release gate. It extends `docs/capacity.md`'s measured curve and claims no capacity beyond the steps below.
>
> Hand-merged (#502): two runs, written by the tool on different hosts, are combined here. The 2026-11-13 run is the current one; the 2026-10-22 run is kept for reference. Each run's own page and JSON are under `perf/results/`.

Memories are partitioned by story (ADR-187), and each story's partition has its own HNSW index. Both runs grow a single story, so every step's vectors are in one partition and one index: these are one story's figures, the worst case for that story's retrieval.

## 2026-11-13: tuned Postgres, 250k and 1M (#502)

**Host: local-mac-tuned (not the reference host)**, macOS-27.0.1-arm64-arm-64bit-Mach-O, 16 vCPU, 128.0 GB RAM; Postgres 16.15 (Debian 16.15-1.pgdg12+2), pgvector 0.8.7, `shared_buffers` 8GB, `/dev/shm` 16 GB, in Docker Desktop with a 64 GB VM.

The story starts as the scale fixture at 1,000 memories and 2,000 events and grows by memories only; each step also commits 600 turns. Each index build runs under the step's `maintenance_work_mem` with the parallel maintenance workers its row gives (a pin builds serially).

| Memories | HNSW build (s) | Build memory | Index (MiB) | Bulk memory rows/s | Live memory rows/s | Event rows/s | Commit p50 | p95 | Retrieval p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 250,000 | 80.1 | 8GB, 4 workers, fit in memory | 976.6 | 1,612 | 158 | 116 | 14.3 | 18.9 | 231.9 | 349.5 | 369.4 |
| 1,000,000 | 352.4 | 8GB, 4 workers, fit in memory | 3906.3 | 1,580 | 154 | 83 | 14.2 | 19.2 | 606.2 | 882.0 | 899.5 |

Latencies are in milliseconds. First budget that fails: at 250,000 memories, retrieval p95 349.5 ms > 150 ms.

Unmeasured: 5,000,000 memories. No figure and no capacity is claimed for it.

**5M recommendation: no-go (owner's decision, 2026-11-15; #502).** The 5M step was not run, and won't be for now. 1M stays the largest measured point, and anything larger is an unvalidated estimate (NFR-SCALE-1). The reasons, from #502:

- **It would only document a known failure at a larger size.** A power law (`y = a·x^b`) fitted through this run's 250k and 1M points projects retrieval p95 at about 2.6 s at 5M, against a 150 ms budget that retrieval already fails at 250k. Commit p95 is flat, at about 20 ms, and the HNSW build is roughly linear (b ≈ 1.07, about 33 min). So 5M would add only how much further retrieval degrades. That gap belongs to retrieval, not to capacity.
- **It doesn't fit this host as configured.** The projections at 5M are a database of about 52 GiB and an index of about 19.1 GiB. Building that index in memory needs about 20 GB each of `maintenance_work_mem` and `/dev/shm`, plus `shared_buffers`, all inside the Docker VM. After #914 streamed the tool's memory rows, its own process is projected at about 24 GiB, not 114 GiB. That figure comes from a linear projection of the 150k and 1M peaks; it isn't a measurement.

Revisit after retrieval meets its budget at 250k–1M. An approved 5M run gets its own issue.

Run notes: `EPISTREL_PERF_HOST=local-mac-tuned python -m epistrel.tools.capacity --up-to 1000000 --start-memories 1000 --steps 250000,1000000 --maintenance-work-mem 8GB --parallel-workers 4`, against `pgvector/pgvector:pg16` started by hand with `-c shared_buffers=8GB --shm-size=16g`, on an otherwise idle machine. It took 20 min 17 s of wall-clock time. At 1M the database was 10.3 GiB, and the tool's own process peaked at 22.8 GiB resident. Raw figures: `perf/results/2026-11-13-capacity.json`.

## 2026-10-22: default Postgres, 250k (reference only)

**Host: local-mac-loaded (not the reference host)**, macOS-27.0.1-arm64-arm-64bit-Mach-O, 16 vCPU, 128.0 GB RAM; Postgres 16.15 (Debian 16.15-1.pgdg12+2), pgvector 0.8.7, `shared_buffers` 128MB.

Each index build runs serially under the step's `maintenance_work_mem`, the way a pin builds it.

| Memories | HNSW build (s) | Build memory | Index (MiB) | Bulk memory rows/s | Live memory rows/s | Event rows/s | Commit p50 | p95 | Retrieval p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 250,000 | 378.9 | 1GB, fit in memory | 976.6 | 1,684 | 74 | 100 | 17.1 | 20.4 | 240.5 | 351.9 | 403.7 |

Latencies are in milliseconds. First budget that fails: at 250,000 memories, retrieval p95 351.9 ms > 150 ms.

Run notes: this is one recorded run of `tests/perf/test_p_10.py` (`-m perf_capacity`, `EPISTREL_TEST_CAPACITY_UP_TO=250000`). It took 10 min 20 s of wall-clock time on a developer Mac shared with other test runs, hence the `local-mac-loaded` label. Postgres ran in Docker through testcontainers, with its default `shared_buffers`. The raw figures are in `perf/results/2026-10-22-2316-capacity.json`.
