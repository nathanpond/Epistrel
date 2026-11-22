"""Recompute ``purpose_latency_estimates``' shipped seeds (#602; ``core.latency.SEEDED``).

    uv run python -m epistrel.tools.latency_seeds            # print the recomputed map
    uv run python -m epistrel.tools.latency_seeds --check    # exit 1 when SEEDED differs

**Hosted** ``claude-haiku-5-5``: p90 per purpose over #818's baseline traces
(``.evals-results/claude-haiku-5-5/<preset>-<fingerprint>.calls.jsonl.gz``, Standard, Full
and Max pooled; the traces are local and never committed, so ``--check`` skips the hosted
model when they are absent). Counted: first attempts (``attempt == 1``) of completed
case-phase calls with no error; left out: judge calls, and the first call per schema per
session (#785's first-use compile).

**Local** ``cydonia-24b-v4.3``: ``docs/model-matrix.json``'s Lite rows, which publish p50 and
p95 turn latency (not p90): ``generation`` is the policies-off Lite turn's p95 (one generation
call), ``guardrail_check`` half the difference between the minors and policies-off turns' p95
(the minors policy checks input and output: two calls).

Seconds, two decimals. No case content is read: only each call's purpose, schema name,
session, flags and latency.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from epistrel.core import latency
from epistrel.tools.generated import REPO_ROOT

HOSTED = "claude-haiku-5-5"
LOCAL = "cydonia-24b-v4.3"
TRACES = tuple(
    REPO_ROOT / ".evals-results" / HOSTED / f"{name}.calls.jsonl.gz"
    for name in ("standard-775abe41184a", "full-4a6a6e02a0c4", "max-eb4d3335eb58")
)
"""#818's baseline traces at the fingerprints it certified against (Standard, Full, Max)."""
MATRIX = REPO_ROOT / "docs" / "model-matrix.json"
QUANTILE = 0.9


def quantile(values: Iterable[float], q: float = QUANTILE) -> float:
    """The nearest-rank ``q`` quantile of ``values`` (non-empty)."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def hosted(paths: Iterable[Path] = TRACES) -> dict[str, float] | None:
    """The hosted model's p90 per purpose, in seconds; ``None`` when a trace is missing."""
    found: dict[str, list[float]] = defaultdict(list)
    seen: set[tuple[str, object, object]] = set()
    for path in paths:
        if not path.is_file():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            for line in lines:
                call = json.loads(line)
                if call.get("judge") or call.get("phase") != "case" or call.get("attempt") != 1:
                    continue
                if call.get("error") or call.get("raised") or not call.get("complete"):
                    continue
                first = (str(path), call.get("session"), call.get("schema"))
                if first not in seen:
                    seen.add(first)
                    continue
                latency_ms = call.get("call_latency_ms")
                if isinstance(latency_ms, int | float):
                    found[str(call["purpose"])].append(latency_ms / 1000)
    return {purpose: round(quantile(seconds), 2) for purpose, seconds in sorted(found.items())}


def local(path: Path = MATRIX) -> dict[str, float]:
    """The local reference tier's estimates from the model matrix's Lite rows."""
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    lite = {
        row["policies"]: row["p95_ms"] / 1000
        for row in rows
        if row["model"] == LOCAL and row["turn_kind"] == "lite"
    }
    generation = lite["off"]
    return {
        "generation": round(generation, 2),
        "guardrail_check": round((lite["minors"] - generation) / 2, 2),
    }


def recomputed() -> tuple[dict[str, dict[str, float]], bool]:
    """The seeds as measured now, and whether the hosted traces were there to read."""
    table: dict[str, dict[str, float]] = {latency.WILDCARD: {latency.WILDCARD: latency.FALLBACK_S}}
    measured = hosted()
    table[HOSTED] = measured if measured is not None else dict(latency.SEEDED[HOSTED])
    table[LOCAL] = local()
    return table, measured is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="epistrel.tools.latency_seeds", description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 when SEEDED differs")
    args = parser.parse_args(argv)
    table, traced = recomputed()
    if not args.check:
        print(json.dumps(table, indent=2, sort_keys=True))
        return 0
    if not traced:
        print(f"{HOSTED}: traces absent, its seeds not rechecked", file=sys.stderr)
    if table != latency.SEEDED:
        print("core.latency.SEEDED differs from the recomputed seeds:", file=sys.stderr)
        print(json.dumps(table, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print("purpose_latency_estimates seeds are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
