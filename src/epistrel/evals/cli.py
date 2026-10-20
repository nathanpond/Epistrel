"""The eval CLI (#134): ``python -m epistrel.evals`` or ``epistrel evals``.

    uv run python -m epistrel.evals run --all --base-url http://localhost:1234/v1 \\
        --model <loaded model> [--api-key lm-studio] [--seeds 3] [--sample N] \\
        [--concurrency 1] [--hardware "Apple M3 Max"] [--server-version "LM Studio 0.4"] \\
        [--database-url postgresql+psycopg://…]
    uv run python -m epistrel.evals run --adapter stub --all --seeds 2 --fail-on-gate
    uv run python -m epistrel.evals run --suite smoke …     # rerun one suite into today's run
    uv run python -m epistrel.evals index                    # regenerate docs/evals/INDEX.md
    uv run python -m epistrel.evals --check                  # suites ↔ guarantees ↔ index
    uv run python -m epistrel.evals --check-sets             # labeled sets (#157), lint job
    uv run python -m epistrel.evals author                   # regenerate #157's sets
    uv run python -m epistrel.evals digest                   # regenerate the review digest

``run`` needs PostgreSQL: ``--database-url`` (a scratch database; the run writes the endpoint
into its model configuration and plays its turns there), else a throwaway one started through
testcontainers (Docker). ``--adapter`` wins over ``EPISTREL_MODEL_ADAPTER`` (default
``openai``). Seeds are ``--seed-base`` + 0..N-1. An unreachable endpoint exits 1 before any
report. Otherwise the run writes its JSON to ``build/evals/<run>.json`` after every suite, then
its Markdown: ``docs/evals/<fingerprint[:12]>-<YYYY-MM-DD>.md`` and the regenerated index for a
real model, ``build/evals/stub-report.md`` for the stub. It exits 0 whatever the floors say —
the verdicts are in the report — and 1 on a harness error (a malformed labeled set, a suite
that could not run, a run that touched a story it did not create) or, with ``--fail-on-gate``
(CI's stub run), when a suite marked ``ci_gate`` fails.

A ``--suite`` rerun merges into the run ``--run-id`` names, by default the latest run of the
same fingerprint on the same UTC day, replacing that suite's rows and rewriting the Markdown in
place; ``--all`` (or ``--new-run``) starts a new run, suffixed ``-2``… when the day already has
one. ``--all`` runs every registered suite; the judge calibration that #158's prose suites need
runs first once they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from epistrel.evals import authoring
from epistrel.evals import check as checks
from epistrel.evals import report as reports
from epistrel.evals import sets as labeled_sets
from epistrel.evals.gate import Verdict
from epistrel.evals.runner import (
    BUILD_DIR,
    DEFAULT_SEED_BASE,
    DEFAULT_SEEDS,
    DEFAULT_SETS_DIR,
    AdapterConfig,
    AdapterName,
    EndpointUnavailable,
    default_run_id,
    run_fingerprint,
    run_suites,
    seeds_from,
)
from epistrel.evals.suites import SUITES, get_suites

EX_FAILED = 1
EX_USAGE = 2
STUB_MARKDOWN = BUILD_DIR / "stub-report.md"


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m epistrel.evals",
        description="Model-quality evals: run suites, regenerate the index, check agreement.",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="check suites, the guarantee table and docs/evals/INDEX.md agree (exit 1 if not)",
    )
    p.add_argument(
        "--check-sets",
        action="store_true",
        help="check the labeled sets: schema, sizes, provenance, authoring, digest (exit 1)",
    )
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run eval suites against a model (or the stub)")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="a suite to run (repeatable)",
    )
    which.add_argument("--all", action="store_true", help="every registered suite")
    run.add_argument(
        "--adapter",
        choices=("openai", "stub"),
        help="the model adapter (default: EPISTREL_MODEL_ADAPTER, else openai)",
    )
    run.add_argument("--base-url", help="the OpenAI-compatible endpoint, e.g. LM Studio's /v1")
    run.add_argument("--model", help="the model every purpose uses")
    run.add_argument("--api-key", default="lm-studio", help="the endpoint's key (lm-studio)")
    run.add_argument(
        "--embedding-model",
        help="an embedding model at --base-url: the retrieval suites embed their stories with it",
    )
    run.add_argument("--seeds", type=_positive, default=DEFAULT_SEEDS, help="seeds (default 2)")
    run.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE, help="the first seed")
    run.add_argument("--sample", type=_positive, help="a seeded subset of each set's cases")
    run.add_argument("--concurrency", type=_positive, default=1, help="cases at once (1)")
    run.add_argument("--database-url", help="a scratch database (default: a testcontainer)")
    run.add_argument("--sets-dir", type=Path, default=DEFAULT_SETS_DIR, help="labeled sets")
    run.add_argument("--run-id", help="the run to write or merge into")
    run.add_argument("--new-run", action="store_true", help="never merge into an earlier run")
    run.add_argument("--hardware", default="", help="where the model ran, e.g. 'Apple M3 Max'")
    run.add_argument("--server-version", default="", help="the model server and its version")
    run.add_argument(
        "--host",
        choices=("ci", "local"),
        help="the host's kind (default: ci under CI, else local); never a machine name",
    )
    run.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="exit 1 when a suite marked ci_gate fails (CI's stub run)",
    )
    run.add_argument("--reports-dir", type=Path, default=checks.REPORTS_DIR, help=argparse.SUPPRESS)
    run.add_argument("--json-dir", type=Path, default=BUILD_DIR, help=argparse.SUPPRESS)
    run.add_argument("--markdown", type=Path, help="where the Markdown goes (see the docs)")
    sub.add_parser("author", help="regenerate #157's labeled sets from their authoring modules")
    sub.add_parser("digest", help="regenerate docs/evals/sets-digest.md")
    index = sub.add_parser("index", help="regenerate docs/evals/INDEX.md from the reports")
    index.add_argument(
        "--reports-dir", type=Path, default=checks.REPORTS_DIR, help=argparse.SUPPRESS
    )
    return p


def _adapter(args: argparse.Namespace) -> AdapterName:
    chosen = args.adapter or os.environ.get("EPISTREL_MODEL_ADAPTER") or "openai"
    return "stub" if chosen == "stub" else "openai"


def _run_id(args: argparse.Namespace, base: str) -> tuple[str, Path | None]:
    """The run's id, and the JSON of the run it merges into (``None``: a new run)."""
    json_dir: Path = args.json_dir
    if args.run_id:
        existing = json_dir / f"{args.run_id}.json"
        return args.run_id, existing if existing.is_file() else None
    same_day = sorted(
        (
            p
            for p in json_dir.glob(f"{base}*.json")
            if p.stem == base or p.stem.startswith(f"{base}-")
        ),
        key=lambda p: (len(p.stem), p.stem),
    )
    if args.suite and not args.new_run and same_day:
        return same_day[-1].stem, same_day[-1]
    taken = [*json_dir.glob(f"{base}*.json"), *reports.report_files(args.reports_dir)]
    return reports.unique_run_id(base, taken), None


async def _run(args: argparse.Namespace) -> int:
    adapter = _adapter(args)
    if adapter == "openai" and not (args.base_url and args.model):
        print("error: a real-model run needs --base-url and --model", file=sys.stderr)
        return EX_USAGE
    config = AdapterConfig(
        adapter=adapter,
        base_url=args.base_url.rstrip("/") if args.base_url else None,
        model=args.model,
        api_key=args.api_key,
        embedding_model=args.embedding_model,
        database_url=args.database_url,
        hardware=args.hardware,
        server_version=args.server_version,
        host=args.host,
    )
    now = datetime.now(UTC)
    run_id, merge_from = _run_id(args, default_run_id(run_fingerprint(config.model_name), now))
    prior = reports.Report.from_json(_load(merge_from)) if merge_from is not None else None
    json_path = args.json_dir / f"{run_id}.json"
    failures = args.json_dir / f"{run_id}-failures.jsonl"

    def merged(report: reports.Report) -> reports.Report:
        if prior is None:
            return report
        combined = prior.model_copy(deep=True)
        for suite in report.suites:
            combined.merge(suite)
        combined.errors = [*prior.errors, *report.errors]
        combined.model_version = report.model_version or prior.model_version
        combined.finished_at = report.finished_at
        combined.verdict = combined.computed_verdict()
        return combined

    def partial(report: reports.Report) -> None:
        reports.write_json(merged(report), json_path)

    try:
        report = await run_suites(
            get_suites(None if args.all else args.suite),
            config,
            seeds_from(args.seeds, args.seed_base),
            sets_dir=args.sets_dir,
            sample=args.sample,
            seed_base=args.seed_base,
            concurrency=args.concurrency,
            run_id=run_id,
            failures=failures,
            on_suite=partial,
            now=now,
        )
    except EndpointUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EX_FAILED
    final = merged(report)
    markdown = _write(args, adapter, final, json_path)
    print(f"wrote {markdown} and {json_path}: run verdict {final.verdict.value}")
    for suite in final.suites:
        print(f"  {suite.name}: {suite.verdict.value}")
    if final.harness_errors:
        print("harness errors:", file=sys.stderr)
        for error in final.harness_errors:
            print(f"  {error}", file=sys.stderr)
        return EX_FAILED
    if args.fail_on_gate and any(s.ci_gate and s.verdict is Verdict.failed for s in final.suites):
        print("a ci_gate suite failed its floors on this run", file=sys.stderr)
        return EX_FAILED
    return 0


def _write(
    args: argparse.Namespace, adapter: AdapterName, final: reports.Report, json_path: Path
) -> Path:
    """Write the JSON and the Markdown (and the index, for a real model's report in
    ``docs/evals``); the Markdown's path."""
    reports.write_json(final, json_path)
    if adapter == "stub":
        markdown: Path = args.markdown or STUB_MARKDOWN
    else:
        markdown = args.markdown or args.reports_dir / f"{final.run_id}.md"
    reports.write_markdown(final, markdown)
    if adapter != "stub" and markdown.parent.resolve() == Path(args.reports_dir).resolve():
        reports.write_index(args.reports_dir)
    return markdown


def _load(path: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def check() -> int:
    found = checks.problems()
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return EX_FAILED
    pending = ", ".join(f"{req} ({story})" for req, story in checks.PENDING.items())
    print(f"evals agree with the guarantee table; pending suites: {pending}")
    return 0


def check_sets() -> int:
    found = labeled_sets.problems(DEFAULT_SETS_DIR)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return EX_FAILED
    print(f"labeled sets: {len(labeled_sets.labeled_suites())} suites' sets check out")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.check:
        return check()
    if args.check_sets:
        return check_sets()
    if args.command == "author":
        for path in authoring.write(DEFAULT_SETS_DIR):
            print(f"wrote {path}")
        return 0
    if args.command == "digest":
        print(f"wrote {labeled_sets.write_digest(DEFAULT_SETS_DIR)}")
        return 0
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "index":
        path = reports.write_index(args.reports_dir)
        print(f"wrote {path}")
        return 0
    parser().print_help(sys.stderr)
    return EX_USAGE


__all__ = ["main", "parser"]
