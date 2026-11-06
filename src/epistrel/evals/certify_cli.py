"""``epistrel evals certify``: certify the presets in one command (NFR-QUAL-11; #235).

    uv run epistrel evals certify --presets standard,full,max \\
        --base-url http://localhost:1234/v1 --model cydonia-24b-v4.3 \\
        --embedding-model <embedding model> --seeds 3 --reference
    uv run python -m epistrel.evals certify --presets standard,full,max --adapter stub \\
        --seeds 2 --sample 100 --out build/evals/certify/          # CI's path, on the stub

**One process, in order.** The presets run in canonical order (Lite, Standard, Full, Max,
whatever order was typed) on one in-process Engine over one scratch database
(``--database-url``, else a testcontainer). The endpoint is probed once and the quality judge
calibrated once (#158), and both are shared by every preset. A stored calibration of the same
judge model and reported version is reused (``--recalibrate`` runs it again).

**Stop and continue** (#730). Every case's outcome is written to the results journal
(``evals.journal``: ``.evals-results/<model>/<preset>-<fingerprint[:12]>.jsonl``, ignored by
git; ``--journal-dir`` moves it) as the case ends, and a run plays only the cases its journal
lacks. So the same command, run again, carries on where the last one stopped, on any day and
with any engine commit (the report lists the commits). Results are kept per model and
configuration fingerprint and never discarded: ``--fresh`` sets the journal aside and starts
over. A case a transient model error ended (a timeout, a rate limit, a provider 5xx) plays
again; ``--retry-errors all`` replays every model error, ``none`` none. The first Ctrl-C lets
the cases in flight finish and be journalled, then exits 130 without a report; a second one
stops at once (the journal keeps every finished case either way). Progress lines with an ETA
go to stderr (``evals.progress``).

**A hosted model** (#235). ``--embedding-base-url`` (and ``--embedding-api-key``) put the
embedding model on its own endpoint, for a chat endpoint with no embeddings API (Anthropic's):
the embeddings stay on a local server. ``--api-key`` defaults to ``EPISTREL_EVALS_API_KEY``
when set, so a key need not appear in the process list.

**Per preset:**

- its fingerprint is computed as a deployment computes a turn's (``certification_run.
  profile_configuration``: the seeded profile version over the deployment's defaults, with
  ``--model`` for every purpose, Max's ``"@strongest"`` included) and printed;
- every suite whose capability the preset enables (``EvalHarness.applicable``, #227) runs
  with ``--seeds`` and the full labelled sets (``--sample`` narrows them);
- the Markdown report goes to ``docs/evals/<preset>-<fingerprint[:12]>-<YYYY-MM-DD>.md``
  (``-2``… on a same-day clash), and its certification record (#233's ``record_from``) to
  ``src/epistrel/core/seeds/certifications/<preset>-<fingerprint[:12]>.json``. A record keeps
  ``report_path`` relative to its own directory and embeds the report (``report_md``), so the
  start-up loader validates it from a checkout and from the installed wheel alike. A run with
  no floor passed writes its report and no record (the preset stays ``uncertified``);
- a table of its floors is printed: ``metric_id | verdict | n | bound``.

Afterwards ``docs/evals/INDEX.md`` is regenerated (#235's Presets table, ``evals.index``).
``--out <dir>`` writes ``reports/`` (with a scratch ``INDEX.md`` beside them, so its links
resolve), ``records/`` and the judge calibration there instead, and never touches the committed
files.

**The tier.** ``model_tier`` (``host.tier``) is ``reference`` only with ``--reference``, a real
model and the full sets; ``--sample`` on a real model forces ``other`` with a warning, and so
does the stub. ``--reference`` with a ``--model`` other than ``REFERENCE_MODEL_ID`` warns. The
command never refuses for the 300-case rule: a floor short of it is ``insufficient_evidence``
(NFR-QUAL-8).

**Exit codes.** 0 once every report was written, whatever the floors say (with
``--fail-on-gate``, CI's stub run, 1 when a suite marked ``ci_gate`` failed). 1 on an endpoint
or harness error: the remaining presets are not run, the finished ones' outputs are kept and the
index regenerated. 2 on a usage error: an unknown or repeated preset, a real model without
``--base-url``, ``--model`` or ``--embedding-model``, a hosted endpoint (``runner.is_hosted``)
without ``--hardware`` and ``--server-version`` (#757), no repository found without ``--out``, or
a record that would be overwritten without ``--force`` (``--resume`` skips a preset that already
has a record for the same fingerprint instead, whatever its date: #730). 130 when stopped. A
model-output error on a case is that case's failure (#179's rule), never a harness error; so is
the model's guardrail refusing a case's content, and a refused suite setup fails that suite
(#738).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from epistrel.core.profiles.certification import CertificationRecord
from epistrel.evals import report as reports
from epistrel.evals.certify import (
    TIER_OTHER,
    TIER_REFERENCE,
    EvalHarness,
    floors_of,
    record_from,
)
from epistrel.evals.gate import Verdict
from epistrel.evals.index import REFERENCE_MODEL_ID
from epistrel.evals.journal import Journal, default_root, journal_path
from epistrel.evals.presets import PRESET_NAMES, PRESETS, PresetName
from epistrel.evals.progress import Printer
from epistrel.evals.runner import (
    DEFAULT_SEED_BASE,
    AdapterConfig,
    EndpointUnavailable,
    Stopped,
    calibrate_judge,
    default_run_id,
    engine_for,
    is_hosted,
    judge_layer_for,
    judge_preflight,
    planned_cases,
    preflight,
    run_suites,
    seeds_from,
)
from epistrel.evals.suites import SUITES, MalformedCase

EX_FAILED = 1
EX_USAGE = 2
EX_STOPPED = 130
DEFAULT_SEEDS = 3
CALIBRATION_NAME = "judge-calibration.json"
RECORDS_PATH = Path("src/epistrel/core/seeds/certifications")
"""Where the committed records live in the repository (``certification.SEEDS_DIR``)."""


class UsageError(Exception):
    """A usage error: exit 2 with the message."""


def parse_presets(text: str) -> list[PresetName]:
    """``--presets``' names in canonical order; ``UsageError`` on an unknown or repeated one."""
    names = [n.strip().lower() for n in text.split(",") if n.strip()]
    valid = ", ".join(PRESET_NAMES)
    unknown = [n for n in names if n not in PRESET_NAMES]
    if unknown or not names:
        raise UsageError(f"unknown preset {', '.join(unknown) or '(none)'}: choose from {valid}")
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise UsageError(f"preset named twice: {', '.join(repeated)} (choose from {valid})")
    return [n for n in PRESET_NAMES if n in names]


def repo_root(start: Path | None = None) -> Path | None:
    """The repository the command runs in: the nearest directory up from ``start`` (the
    working directory) that holds ``pyproject.toml``."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / "pyproject.toml").is_file():
            return folder
    return None


@dataclass(frozen=True, slots=True)
class Layout:
    """Where the outputs go: the reports, the records, the index and the judge calibration."""

    reports_dir: Path
    records_dir: Path
    index_dir: Path
    calibration_file: Path
    committed: bool
    """The repository's committed locations (``docs/evals``, the seeded records)."""


def layout(out: Path | None, adapter: str) -> Layout:
    if out is not None:
        reports_dir = out / "reports"
        return Layout(reports_dir, out / "records", reports_dir, out / CALIBRATION_NAME, False)
    root = repo_root()
    if root is None:
        raise UsageError("no repository here (no pyproject.toml above): pass --out <dir>")
    docs = root / "docs" / "evals"
    records = root / RECORDS_PATH
    calibration = (
        root / "build" / "evals" / CALIBRATION_NAME
        if adapter == "stub"
        else docs / CALIBRATION_NAME
    )
    return Layout(docs, records, docs, calibration, True)


def record_path(records_dir: Path, preset: str, fingerprint: str) -> Path:
    return records_dir / f"{preset}-{fingerprint.split(':', 1)[-1][:12]}.json"


def write_record(
    path: Path, record: CertificationRecord, report_file: Path, report_md: str
) -> None:
    """The record as committed: ``report_path`` relative to the record's directory, the report
    embedded (``report_md``)."""
    import os

    relative = Path(os.path.relpath(report_file.resolve(), path.parent.resolve())).as_posix()
    document = record.model_copy(update={"report_path": relative, "report_md": report_md})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", "utf-8"
    )


def _matching_record(path: Path, fingerprint: str) -> bool:
    """Whether ``path`` is a record of ``fingerprint``, whatever its date (#730)."""
    if not path.is_file():
        return False
    try:
        found = json.loads(path.read_text("utf-8"))
    except ValueError:
        return False
    return bool(found.get("fingerprint") == fingerprint)


def _bound(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def floor_table(report: reports.Report) -> list[str]:
    """The stdout table of a preset's floors: ``metric_id | verdict | n | bound``."""
    passed, failed, insufficient, _, _, _ = floors_of(report)
    lines = ["metric_id | verdict | n | bound"]
    for floor in (*passed, *failed, *insufficient):
        metric = floor.metric_id + (
            f" ({floor.intervention_type})" if floor.intervention_type else ""
        )
        lines.append(f"{metric} | {floor.verdict} | {floor.n} | {_bound(floor.upper_bound_95)}")
    return lines


@dataclass
class Certified:
    """One preset's outcome: its fingerprint, the report written and the record (``None``: no
    floor passed, or skipped by ``--resume``)."""

    preset: str
    fingerprint: str
    report: Path | None = None
    record: Path | None = None
    skipped: bool = False
    gate_failed: bool = False


def _tier(args: argparse.Namespace, adapter: str) -> str:
    if adapter == "stub":
        return TIER_OTHER
    if args.sample is not None:
        print("warning: --sample on a real model certifies tier 'other'", file=sys.stderr)
        return TIER_OTHER
    if not args.reference:
        return TIER_OTHER
    if args.model != REFERENCE_MODEL_ID:
        print(
            f"warning: --reference with {args.model!r}, not the reference model "
            f"{REFERENCE_MODEL_ID!r} (docs/model-matrix.md)",
            file=sys.stderr,
        )
    return TIER_REFERENCE


def _config(args: argparse.Namespace, adapter: str, preset: PresetName) -> AdapterConfig:
    from epistrel.evals.cli import _config as base

    return replace(base(args, "stub" if adapter == "stub" else "openai"), preset=preset)


async def certify(args: argparse.Namespace, adapter: str) -> int:
    """The command (see the module docstring); its exit code."""
    presets = parse_presets(args.presets)
    if adapter != "stub" and not (args.base_url and args.model and args.embedding_model):
        raise UsageError(
            "a real-model certification needs --base-url, --model and --embedding-model"
        )
    hosted = adapter != "stub" and is_hosted(args.base_url)
    if hosted and not (args.hardware and args.server_version):
        raise UsageError(
            "a hosted endpoint's certification needs --hardware and --server-version "
            '(e.g. "Anthropic API" and "Claude API (OpenAI compatibility)"; #757)'
        )
    where = layout(args.out, adapter)
    journals = _journal_root(args)
    tier = _tier(args, adapter)
    now = datetime.now(UTC)
    seeds = seeds_from(args.seeds, args.seed_base)
    stop = _install_stop()
    first = _config(args, adapter, presets[0])
    done: list[Certified] = []
    failed = False
    async with engine_for(first, None) as live, judge_layer_for(first, live) as judging:
        try:
            model_version = await preflight(live, first)
            judge_version = model_version
            if first.judge_model is not None:
                judge_version = await judge_preflight(judging, first)
        except EndpointUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EX_FAILED
        planned = await _plan(live, presets, where, args)
        from epistrel.evals import judging as judges

        calibration = (
            None if args.recalibrate else judges.lookup(where.calibration_file, first.judge_name)
        )
        if calibration is not None and calibration.model_version not in (None, judge_version):
            calibration = None
        if calibration is None:
            calibration = await calibrate_judge(
                live,
                first,
                sets_dir=args.sets_dir,
                model_version=judge_version,
                seed=args.seed_base,
                judge_layer=judging,
            )
            judges.save(where.calibration_file, calibration)
            reused = ""
        else:
            reused = f", stored {calibration.date}"
        print(
            f"judge {calibration.judge_model}: {calibration.status} "
            f"({calibration.pairs} pairs{reused}); shared by {', '.join(presets)}"
        )
        harness = EvalHarness(live, adapter=first)
        for preset, (request, settings, skip) in planned.items():
            print(f"{preset}: fingerprint {request.fingerprint}")
            if skip:
                print(f"{preset}: a record exists for this fingerprint (--resume): skipped")
                done.append(Certified(preset, request.fingerprint, skipped=True))
                continue
            try:
                outcome = await _certify_one(
                    live,
                    harness,
                    preset,
                    request,
                    settings,
                    args=args,
                    adapter=adapter,
                    where=where,
                    seeds=seeds,
                    tier=tier,
                    now=now,
                    probed=(model_version, judge_version),
                    calibrated=calibration.status == "calibrated",
                    journals=journals,
                    stop=stop,
                )
            except Stopped:
                print(
                    f"stopped: {preset}'s finished cases are in its journal under {journals}; "
                    "run the same command again to carry on",
                    file=sys.stderr,
                )
                return EX_STOPPED
            except (EndpointUnavailable, MalformedCase) as exc:
                print(f"error: {preset}: {exc}", file=sys.stderr)
                failed = True
                break
            done.append(outcome)
            if outcome.report is None:
                failed = True
                break
    reports.write_index(where.index_dir, where.records_dir)
    print(f"wrote {where.index_dir / reports.INDEX_NAME}")
    if failed:
        return EX_FAILED
    if args.fail_on_gate and any(c.gate_failed for c in done):
        print("a ci_gate suite failed its floors on this run", file=sys.stderr)
        return EX_FAILED
    return 0


def _journal_root(args: argparse.Namespace) -> Path:
    if args.journal_dir is not None:
        return Path(args.journal_dir)
    if args.out is not None:
        return Path(args.out) / "journal"
    root = repo_root()
    if root is None:
        raise UsageError("no repository here (no pyproject.toml above): pass --journal-dir")
    return default_root(root)


def _install_stop() -> asyncio.Event:
    """The first Ctrl-C sets the returned event (the run finishes its cases in flight and
    stops); a second cancels the run at once."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    main = asyncio.current_task()

    def interrupted() -> None:
        if not stop.is_set():
            stop.set()
            print(
                "\nstopping: finishing the cases in flight (Ctrl-C again to stop at once)",
                file=sys.stderr,
                flush=True,
            )
            return
        loop.remove_signal_handler(signal.SIGINT)
        if main is not None:
            main.cancel()

    # No signal handlers here (not the main thread, or Windows): Ctrl-C stops at once.
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGINT, interrupted)
    return stop


async def _plan(
    live: Any, presets: Sequence[PresetName], where: Layout, args: argparse.Namespace
) -> dict[PresetName, tuple[Any, Any, bool]]:
    """Each preset's configuration (fingerprint, models, settings) as this deployment computes
    it, and whether ``--resume`` skips it; ``UsageError`` before any suite runs when a record
    would be overwritten without ``--force``."""
    from epistrel.orchestrator.jobs.handlers.certification_run import profile_configuration
    from epistrel.orchestrator.models.facts import runtime_facts

    found: dict[PresetName, tuple[Any, Any, bool]] = {}
    async with live.db.begin() as conn:
        facts = await runtime_facts(conn)
        for preset in presets:
            request, settings = await profile_configuration(
                conn, PRESETS[preset].profile_id, facts=facts
            )
            path = record_path(where.records_dir, preset, request.fingerprint)
            skip = bool(args.resume) and _matching_record(path, request.fingerprint)
            if path.exists() and not skip and not args.force:
                raise UsageError(
                    f"{path} exists: pass --force to certify {preset} again (a new "
                    "certification id), or --resume to skip it"
                )
            found[preset] = (request, settings, skip)
    return found


async def _certify_one(
    live: Any,
    harness: EvalHarness,
    preset: PresetName,
    request: Any,
    settings: Any,
    *,
    args: argparse.Namespace,
    adapter: str,
    where: Layout,
    seeds: list[int],
    tier: str,
    now: datetime,
    probed: tuple[str | None, str | None],
    calibrated: bool,
    journals: Path,
    stop: asyncio.Event | None = None,
) -> Certified:
    names = [n for n in harness.applicable(settings) if n not in set(args.skip)]
    if args.suite:
        names = [n for n in names if n in set(args.suite)]
    config = _config(args, adapter, preset)
    journal = Journal.open(
        journal_path(journals, config.model_name, preset, request.fingerprint),
        model=config.model_name,
        fingerprint=request.fingerprint,
        retry_errors=args.retry_errors,
        fresh=args.fresh,
    )
    planned = len(seeds) * sum(
        planned_cases(
            SUITES[n],
            sets_dir=args.sets_dir,
            sample=args.sample,
            full=args.full,
            seed_base=args.seed_base,
        )
        for n in names
    )
    print(
        f"{preset}: {planned:,} case runs planned; {len(journal.entries):,} in the journal "
        f"{journal.path}"
    )
    taken = [*where.reports_dir.glob("*.md")] if where.reports_dir.is_dir() else []
    run_id = reports.unique_run_id(default_run_id(request.fingerprint, now, preset), taken)
    report = await run_suites(
        [SUITES[n] for n in names],
        config,
        seeds,
        engine=live,
        sets_dir=args.sets_dir,
        sample=args.sample,
        full=args.full,
        seed_base=args.seed_base,
        concurrency=args.concurrency,
        run_id=run_id,
        now=now,
        calibration_file=where.calibration_file,
        profile=PRESETS[preset],
        fingerprint_value=request.fingerprint,
        probed=probed,
        journal=journal,
        stop=stop,
        progress=Printer(preset, planned),
    )
    report_md = reports.render_markdown(report)
    report_file = where.reports_dir / f"{run_id}.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report_md, encoding="utf-8")
    print(f"{preset}: wrote {report_file}")
    for line in floor_table(report):
        print(f"  {line}")
    if report.harness_errors:
        for error in report.harness_errors:
            print(f"error: {preset}: {error}", file=sys.stderr)
        return Certified(preset, request.fingerprint)
    record = record_from(
        report,
        report_md,
        fingerprint=request.fingerprint,
        profile_id=request.profile_id,
        profile_version=request.profile_version,
        models=request.models,
        preset=preset,
        tier=tier,
    )
    outcome = Certified(
        preset,
        request.fingerprint,
        report=report_file,
        gate_failed=any(s.ci_gate and s.verdict is Verdict.failed for s in report.suites),
    )
    if record is None:
        print(f"{preset}: no floor passed: no record (the preset stays uncertified)")
        return outcome
    # The shared calibration is the record's (#158: every preset's judge rows ran under it).
    record = record.model_copy(update={"judge_calibrated": calibrated})
    path = record_path(where.records_dir, preset, request.fingerprint)
    write_record(path, record, report_file, report_md)
    print(f"{preset}: wrote {path} (certification {record.certification_id})")
    outcome.record = path
    return outcome


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """The ``certify`` subcommand's options (``evals.cli``)."""
    from epistrel.evals.cli import _embedding_options, _endpoint_options, _positive

    command: argparse.ArgumentParser = sub.add_parser(
        "certify",
        help="certify presets on one model in one run: reports, records, INDEX (#235)",
    )
    command.add_argument(
        "--presets", required=True, help="comma-separated: lite, standard, full, max"
    )
    _endpoint_options(command)
    _embedding_options(command, required=True)
    command.add_argument("--seeds", type=_positive, default=DEFAULT_SEEDS, help="seeds (3)")
    command.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE, help="the first seed")
    command.add_argument("--sample", type=_positive, help="a seeded subset of each set (CI)")
    command.add_argument(
        "--full",
        action="append",
        default=[],
        choices=sorted(SUITES),
        help="a suite that runs its full set despite --sample (repeatable)",
    )
    command.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=sorted(SUITES),
        help="a suite left out (repeatable; CI runs the zero-leak suites in a job of their own)",
    )
    command.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="run only these of the applicable suites (repeatable; tests)",
    )
    command.add_argument("--concurrency", type=_positive, default=1, help="cases at once (1)")
    command.add_argument(
        "--host",
        choices=("ci", "local", "hosted"),
        help="the host's kind (default: hosted for an endpoint off this network, else ci "
        "under CI, else local); never a machine name",
    )
    command.add_argument(
        "--reference",
        action="store_true",
        help="record host tier 'reference' (the reference model, full sets)",
    )
    command.add_argument("--out", type=Path, help="write reports/, records/ and INDEX.md here")
    command.add_argument(
        "--resume",
        action="store_true",
        help="skip a preset that already has a record for its fingerprint (any date)",
    )
    command.add_argument("--force", action="store_true", help="overwrite an existing record")
    command.add_argument(
        "--journal-dir",
        type=Path,
        help="where the results journal lives (default .evals-results/ in the repository)",
    )
    command.add_argument(
        "--fresh",
        action="store_true",
        help="set the preset's journal aside and play every case again",
    )
    command.add_argument(
        "--retry-errors",
        choices=("transient", "all", "none"),
        default="transient",
        help="journalled model errors to play again (default: transient ones)",
    )
    command.add_argument(
        "--recalibrate",
        action="store_true",
        help="calibrate the judge again even when a calibration of it is stored",
    )
    command.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="exit 1 when a suite marked ci_gate fails (CI's stub run)",
    )
    command.set_defaults(real_purposes=None)
    return command


def run(args: argparse.Namespace, adapter: str) -> int:
    try:
        return asyncio.run(certify(args, adapter))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EX_USAGE
    except KeyboardInterrupt, asyncio.CancelledError:
        print("stopped at once: the journal keeps every finished case", file=sys.stderr)
        return EX_STOPPED


__all__ = [
    "Certified",
    "UsageError",
    "add_parser",
    "certify",
    "floor_table",
    "layout",
    "parse_presets",
    "record_path",
    "repo_root",
    "run",
    "write_record",
]
