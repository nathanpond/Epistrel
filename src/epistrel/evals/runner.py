"""The eval runner (#134): one run of labeled suites against one model, through the real code
paths, with nothing written to any story it did not create.

**The engine.** A run uses an in-process ``Engine`` (the library surface; no credential) on the
adapter ``AdapterConfig.adapter`` names. Without an ``engine`` passed in it builds one over
``AdapterConfig.database_url``, or over a throwaway PostgreSQL started through testcontainers
when none is given (``tools.model_matrix.database``, which also migrates it); the run writes
the endpoint into that database's ``model_config`` (``default`` row: base URL, model, key and
a 120 s per-call timeout; no adapter retries), so the URL should name a scratch database.
Before any case, a **preflight** probe (``ModelAccessLayer.probe``) checks the endpoint speaks
the protocol; an unreachable one raises ``EndpointUnavailable`` and nothing is reported. The
completing model's ``model`` field is the report's ``model_version``.

**Seeds.** Seed ``i`` of ``--seeds N`` is ``seed_base + i``. Each seed plays every case once, in
an order shuffled by the seed, inside ``eval_scope(case_id, seed)``: every model call carries
the seed (OpenAI ``seed``) and the case's ``X-Eval-Case`` line, so a stub run answers per case
(``Suite.stub_answers`` through ``StubAdapter.case``). ``sample`` keeps a subset drawn by
``seed_base``, the same on every seed. ``concurrency`` bounds the cases played at once.

**Scratch stories.** A case creates its stories through ``CaseContext.story``: owned by
``system:evals``, named ``scratch-eval-<run>-<case>-<seed>``, with the turn deadline raised to
300 s by a profile override (budgets unchanged). They are deleted through
``Engine.delete_story`` when the case ends, and a **sweep** deletes any story with that owner
and prefix before and after the run, so an interrupted run leaves nothing behind. The quality
judge (``CaseContext.judge``) refuses any story that is not one of the case's own.

**Isolation.** Before and after the run the runner counts, in every table with a ``story_id``
column, the rows of every story, and the stories themselves; once its scratch stories are gone
the counts must match. A mismatch is a run error (``Report.errors``): the run wrote to a story
it did not create. The harness never runs inside a turn: nothing in ``epistrel.core``, the
Orchestrator, the API or the ``Engine`` imports it (``[tool.importlinter]``).

**Failures.** A ``ModelError`` during a case is a failed case — every metric of the suite
observes it as bad — never an exclusion. Any other exception, or a malformed labeled set, is a
**harness error**: the suite's verdict is ``error``. Each failed case's inputs and output go to
``failures`` (JSON lines, never committed) when a path is given.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts, fingerprint
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.seeds import profiles as seeded_profiles
from epistrel.core.stories import STORY_NAME_MAX, CreatedStory, StorySpec
from epistrel.engine import Engine
from epistrel.evals.gate import SeedTally, Verdict, gate, judge
from epistrel.evals.report import Host, MetricResult, Report, SuiteResult
from epistrel.evals.suites import CaseModel, MalformedCase, Outcome, Suite
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.access import ModelAccessLayer, eval_scope
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.facts import TEMPLATE_HASHES
from epistrel.orchestrator.models.registry import (
    DEFAULT_MODEL_KEY,
    GENERATION,
    QUALITY_JUDGE,
    STORY_PURPOSES,
    QualityJudgeInputs,
    QualityJudgeVerdict,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.models.stub import STUB_MODEL
from epistrel.tools import model_matrix
from epistrel.tools.generated import REPO_ROOT

PRINCIPAL = "system:evals"
"""The in-process principal every eval operation runs as (no credential: the library path)."""
SCRATCH_PREFIX = "scratch-eval-"
SCRATCH_DEADLINE_MS = 300_000
CALL_TIMEOUT_S = 120.0
JUDGE_DEADLINE_S = 300.0
DEFAULT_SEED_BASE = 20261008
DEFAULT_SEEDS = 2
DEFAULT_SETS_DIR = REPO_ROOT / "evals" / "sets"
BUILD_DIR = REPO_ROOT / "build" / "evals"
FINGERPRINT_PROFILE = seeded_profiles.STRUCTURED_M4_NAME
FINGERPRINT_VERSION = 1
"""The fingerprint is the seeded ``Structured (M4)`` profile's, at its seeded version, with the
run's model as every story purpose's (#109's ``fingerprint()``; the stub run's model is
``stub``)."""

type AdapterName = Literal["stub", "openai"]


class EndpointUnavailable(Exception):
    """The preflight probe failed: the endpoint is unreachable or does not speak the protocol."""


class NotScratch(Exception):
    """The quality judge was asked about a story that is not one of the eval's scratch stories."""


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    """Where a run's model calls go. ``stub``: the deterministic stub adapter; ``openai``: the
    OpenAI-compatible endpoint at ``base_url`` serving ``model``."""

    adapter: AdapterName = "stub"
    base_url: str | None = None
    model: str | None = None
    api_key: str = "lm-studio"
    database_url: str | None = None
    hardware: str = ""
    server_version: str = ""
    host: Literal["ci", "local"] | None = None
    """``ci`` or ``local``; ``None``: ``ci`` under CI, else ``local``. Never the machine's
    name: reports are committed to a public repository, so a host is described only by its
    kind and #54's hardware and server-version fields."""

    @property
    def model_name(self) -> str:
        return STUB_MODEL if self.adapter == "stub" else (self.model or "")


def run_fingerprint(model: str) -> str:
    """The fingerprint the run reports (``FINGERPRINT_VERSION``'s docstring): the seeded
    profile over the deployment's seeded defaults, as a fresh deployment runs it."""
    settings = merged_settings(
        seeded_profiles.profile_defaults(), seeded_profiles.STRUCTURED_M4_SETTINGS
    )
    facts = RuntimeFacts(
        models={purpose: ModelIdentity(id=model) for purpose in STORY_PURPOSES},
        templates=TEMPLATE_HASHES,
    )
    return fingerprint(
        profile_id=seeded_profiles.STRUCTURED_M4_PROFILE_ID,
        version=FINGERPRINT_VERSION,
        settings=settings,
        facts=facts,
    )


def default_run_id(fingerprint_value: str, when: datetime) -> str:
    """``<fingerprint[:12]>-<YYYY-MM-DD>`` (UTC); the report writer adds ``-2``… on a clash."""
    digest = fingerprint_value.split(":", 1)[-1]
    return f"{digest[:12]}-{when.astimezone(UTC).strftime('%Y-%m-%d')}"


# -- scratch stories --------------------------------------------------------------------------


def _ctx() -> RequestContext:
    return RequestContext(principal=PRINCIPAL)


async def scratch_story_ids(db: AsyncEngine) -> list[str]:
    """Every story a sweep deletes: owned by ``system:evals`` and named ``scratch-eval-…``."""
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id FROM stories WHERE owner_principal = :owner "
                "AND name LIKE :prefix ORDER BY id"
            ),
            {"owner": PRINCIPAL, "prefix": f"{SCRATCH_PREFIX}%"},
        )
        return [str(row[0]) for row in rows]


async def sweep(engine: Engine) -> int:
    """Delete every scratch story (an interrupted run's included); the number deleted."""
    found = await scratch_story_ids(engine.db)
    for story_id in found:
        await engine.delete_story(_ctx(), story_id)
    return len(found)


class Scratch:
    """The scratch stories of one case: created, checked and deleted through the ``Engine``."""

    def __init__(self, engine: Engine, label: str) -> None:
        self.engine = engine
        self.label = label
        self.ids: list[str] = []

    async def create(self, spec: StorySpec) -> CreatedStory:
        name = f"{SCRATCH_PREFIX}{self.label}-{len(self.ids) + 1}"[:STORY_NAME_MAX]
        created = await self.engine.create_story(_ctx(), spec.model_copy(update={"name": name}))
        self.ids.append(created.story_id)
        override = ProfileOverridesPatch.model_validate(
            {"settings": {"budgets": {"deadline_ms": SCRATCH_DEADLINE_MS}}, "ignore_warnings": True}
        )
        await self.engine.set_profile_overrides(_ctx(), created.story_id, override)
        return created

    def require(self, story_id: str) -> None:
        if story_id not in self.ids:
            raise NotScratch(f"story {story_id} is not one of this eval case's scratch stories")

    async def delete_all(self) -> None:
        while self.ids:
            await self.engine.delete_story(_ctx(), self.ids.pop())


@dataclass
class CaseContext:
    """What a suite's ``evaluate`` plays a case with: the engine, its scratch stories, and the
    seed and run it belongs to."""

    engine: Engine
    scratch: Scratch
    seed: int
    run_id: str

    @property
    def models(self) -> ModelAccessLayer:
        return self.engine.models

    @property
    def principal(self) -> RequestContext:
        return _ctx()

    async def story(self, spec: StorySpec) -> CreatedStory:
        """A new scratch story (see the module docstring)."""
        return await self.scratch.create(spec)

    async def judge(
        self, story_id: str, first: str, second: str, criteria: str | None = None
    ) -> QualityJudgeVerdict | Uncertain:
        """One ``quality_judge`` comparison of two passages from scratch story ``story_id``;
        ``NotScratch`` for any other story (the judge never reads a live story)."""
        self.scratch.require(story_id)
        extra = {} if criteria is None else {"criteria": criteria}
        inputs = QualityJudgeInputs(first=first, second=second, **extra)

        def identical() -> QualityJudgeVerdict | None:
            # FR-MODEL-5's deterministic step: the same passage twice is a tie, no call needed.
            return QualityJudgeVerdict(preferred="tie") if first.strip() == second.strip() else None

        return await self.models.structured_call(
            QUALITY_JUDGE.name,
            QualityJudgeVerdict,
            inputs,
            deterministic_first=identical,
            budget=TurnBudget(deadline_s=JUDGE_DEADLINE_S),
        )


# -- isolation --------------------------------------------------------------------------------


async def story_rows(db: AsyncEngine) -> dict[str, int]:
    """Per table with a ``story_id`` column, its rows that belong to a story; and the stories."""
    async with db.connect() as conn:
        tables = [
            str(row[0])
            for row in await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'story_id' "
                    "ORDER BY table_name"
                )
            )
        ]
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = int(
                (
                    await conn.execute(
                        text(f'SELECT count(*) FROM "{table}" WHERE story_id IS NOT NULL')  # noqa: S608 - names from information_schema
                    )
                ).scalar_one()
            )
        counts["stories"] = int(
            (await conn.execute(text("SELECT count(*) FROM stories"))).scalar_one()
        )
        return counts


def isolation_problems(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """The tables whose story rows changed across the run (empty when none did)."""
    return [
        f"{table}: {before.get(table, 0)} story rows before the run, {after.get(table, 0)} after"
        for table in sorted(set(before) | set(after))
        if before.get(table, 0) != after.get(table, 0)
    ]


# -- the run ----------------------------------------------------------------------------------


def host_of(config: AdapterConfig) -> Host:
    kind = config.host or ("ci" if os.environ.get("CI") else "local")
    return Host(
        kind=kind,
        hardware=config.hardware,
        server_version=config.server_version,
        endpoint=config.base_url if config.adapter == "openai" else None,
    )


def make_settings(dsn: str, adapter: AdapterName, ledger_dir: Path) -> Settings:
    return Settings.model_validate(
        {
            "database_url": dsn,
            "model_adapter": adapter,
            "config_key": Fernet.generate_key().decode(),
            # The harness calls the library in process; no REST call needs this credential.
            "service_credentials": [secrets.token_urlsafe(32)],
            # A scratch database has its own erasure ledger, never the deployment's.
            "erasure_ledger_dir": str(ledger_dir),
        }
    )


@contextlib.asynccontextmanager
async def engine_for(config: AdapterConfig, engine: Engine | None) -> AsyncIterator[Engine]:
    """``engine``, or an in-process ``Engine`` over the run's database (module docstring)."""
    if engine is not None:
        yield engine
        return
    import tempfile

    async with model_matrix.database(config.database_url) as dsn:
        with tempfile.TemporaryDirectory(prefix="epistrel-evals-ledger-") as ledger:
            settings = make_settings(dsn, config.adapter, Path(ledger))
            db = make_engine(settings)
            built = Engine(settings, db)
            try:
                yield built
            finally:
                await built.models.aclose()
                await db.dispose()


async def preflight(engine: Engine, config: AdapterConfig) -> str | None:
    """Point the engine at the endpoint and probe it; the completing model's reported name."""
    if config.adapter == "openai":
        if not config.base_url or not config.model:
            raise EndpointUnavailable("an openai run needs --base-url and --model")
        await model_config.write_config(
            engine.db,
            DEFAULT_MODEL_KEY,
            keybox=engine.models.keybox,
            principal=PRINCIPAL,
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            params=ModelParams(timeout=CALL_TIMEOUT_S),
        )
    try:
        probe = await engine.models.probe(GENERATION.name)
    except ModelError as exc:
        reason = exc.details.get("reason") or exc.code
        raise EndpointUnavailable(f"the endpoint failed the protocol probe: {reason}") from None
    return probe.model or None


@dataclass
class _Played:
    case: CaseModel
    seed: int
    outcome: Outcome | None
    error: str | None
    latency_ms: int


@dataclass
class SuiteRun:
    """What ``run_suite`` needs besides the suite."""

    engine: Engine
    config: AdapterConfig
    seeds: Sequence[int]
    run_id: str
    sets_dir: Path = DEFAULT_SETS_DIR
    sample: int | None = None
    seed_base: int = DEFAULT_SEED_BASE
    concurrency: int = 1
    failures: Path | None = None
    cases: Mapping[str, Sequence[CaseModel]] = field(default_factory=dict)
    """Cases given in memory by suite name, instead of its labeled set (tests)."""


def _select(cases: Sequence[CaseModel], sample: int | None, seed_base: int) -> list[CaseModel]:
    ordered = sorted(cases, key=lambda c: c.id)
    if sample is None or sample >= len(ordered):
        return ordered
    return sorted(random.Random(seed_base).sample(ordered, sample), key=lambda c: c.id)  # noqa: S311 - a reproducible subset, not a secret


async def _play(suite: Suite, job: SuiteRun, case: CaseModel, seed: int) -> _Played:
    scratch = Scratch(job.engine, f"{job.run_id}-{case.id}-{seed}")
    ctx = CaseContext(job.engine, scratch, seed, job.run_id)
    started = time.perf_counter()
    outcome: Outcome | None = None
    error: str | None = None
    try:
        with eval_scope(case.id, seed):
            outcome = await suite.evaluate(ctx, case)
    except ModelError as exc:
        # A model error is a failed case, never an exclusion (#134).
        outcome = Outcome(
            {metric.id: False for metric in suite.metrics}, detail={"model_error": exc.code}
        )
    except Exception as exc:
        error = f"case {case.id} seed {seed}: {type(exc).__name__}: {exc}"
    finally:
        try:
            await scratch.delete_all()
        except Exception as exc:
            error = error or f"case {case.id} seed {seed}: cleanup failed: {type(exc).__name__}"
    latency = round((time.perf_counter() - started) * 1000)
    return _Played(case, seed, outcome, error, latency)


def _record_failure(job: SuiteRun, suite: Suite, played: _Played) -> None:
    if job.failures is None or played.outcome is None:
        return
    if all(v is not False for v in played.outcome.observations.values()):
        return
    job.failures.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "run_id": job.run_id,
        "suite": suite.name,
        "case": played.case.model_dump(mode="json"),
        "seed": played.seed,
        "observations": dict(played.outcome.observations),
        "output": played.outcome.output,
        "detail": dict(played.outcome.detail),
    }
    with job.failures.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")


async def run_suite(suite: Suite, job: SuiteRun) -> SuiteResult:
    """Every seed of ``suite`` (module docstring)."""
    started = time.perf_counter()
    try:
        loaded = job.cases.get(suite.name)
        cases = _select(
            loaded if loaded is not None else suite.load(job.sets_dir), job.sample, job.seed_base
        )
    except MalformedCase as exc:
        return SuiteResult.errored(suite, [str(exc)], list(job.seeds))
    if job.config.adapter == "stub":
        stub = job.engine.models.stub
        for case in cases:
            for purpose, answer in suite.stub_answers(case).items():
                stub.case(case.id, answer, purpose=purpose)
    tallies: dict[str, dict[int, list[int]]] = defaultdict(dict)
    errors: list[str] = []
    latencies: list[int] = []
    gate_slots = asyncio.Semaphore(max(1, job.concurrency))

    async def bounded(case: CaseModel, seed: int) -> _Played:
        async with gate_slots:
            return await _play(suite, job, case, seed)

    for seed in job.seeds:
        order = list(cases)
        random.Random(seed).shuffle(order)  # noqa: S311 - a reproducible order, not a secret
        for metric in suite.metrics:
            tallies[metric.id][seed] = [0, 0]
        for played in await asyncio.gather(*(bounded(c, seed) for c in order)):
            latencies.append(played.latency_ms)
            if played.error is not None:
                errors.append(played.error)
            if played.outcome is None:
                continue
            for metric in suite.metrics:
                seen = played.outcome.observations.get(metric.id)
                if seen is not None:
                    tallies[metric.id][seed][0 if seen else 1] += 1
            _record_failure(job, suite, played)
    metrics = []
    for metric in suite.metrics:
        seed_tallies = [
            SeedTally(seed, good, bad) for seed, (good, bad) in sorted(tallies[metric.id].items())
        ]
        metrics.append(MetricResult.of(metric, judge(metric, seed_tallies)))
    verdict = Verdict.error if errors else gate(m.verdict for m in metrics)
    return SuiteResult(
        name=suite.name,
        title=suite.title,
        ci_gate=suite.ci_gate,
        guarantees=list(suite.guarantees),
        cases=len(cases),
        seeds=list(job.seeds),
        metrics=metrics,
        errors=errors,
        verdict=verdict,
        duration_s=round(time.perf_counter() - started, 1),
        mean_case_ms=round(sum(latencies) / len(latencies)) if latencies else None,
    )


def seeds_from(count: int, base: int = DEFAULT_SEED_BASE) -> list[int]:
    """``base + 0 .. base + count - 1``."""
    return [base + i for i in range(count)]


async def run_suites(
    suites: Sequence[Suite],
    config: AdapterConfig,
    seeds: Sequence[int],
    *,
    engine: Engine | None = None,
    sets_dir: Path = DEFAULT_SETS_DIR,
    sample: int | None = None,
    seed_base: int = DEFAULT_SEED_BASE,
    concurrency: int = 1,
    run_id: str | None = None,
    failures: Path | None = None,
    cases: Mapping[str, Sequence[CaseModel]] | None = None,
    on_suite: Callable[[Report], Awaitable[None] | None] | None = None,
    now: datetime | None = None,
) -> Report:
    """One run of ``suites`` (module docstring); ``on_suite`` sees the report after each suite
    (the CLI writes the JSON incrementally)."""
    started = now or datetime.now(UTC)
    fingerprint_value = run_fingerprint(config.model_name)
    report = Report(
        run_id=run_id or default_run_id(fingerprint_value, started),
        started_at=started.isoformat(timespec="seconds"),
        model=config.model_name,
        fingerprint=fingerprint_value,
        fingerprint_profile=f"{FINGERPRINT_PROFILE} v{FINGERPRINT_VERSION}",
        adapter=config.adapter,
        engine_sha=model_matrix.engine_sha(),
        host=host_of(config),
        seed_base=seed_base,
        seeds=list(seeds),
    )
    async with engine_for(config, engine) as live:
        report.model_version = await preflight(live, config)
        await sweep(live)
        before = await story_rows(live.db)
        job = SuiteRun(
            engine=live,
            config=config,
            seeds=seeds,
            run_id=report.run_id,
            sets_dir=sets_dir,
            sample=sample,
            seed_base=report.seed_base,
            concurrency=concurrency,
            failures=failures,
            cases=cases or {},
        )
        try:
            for suite in suites:
                report.merge(await run_suite(suite, job))
                if on_suite is not None:
                    pending = on_suite(report)
                    if pending is not None:
                        await pending
        finally:
            await sweep(live)
        problems = isolation_problems(before, await story_rows(live.db))
    report.errors.extend(f"isolation: {p}" for p in problems)
    report.finish(datetime.now(UTC))
    return report


async def run(
    suite: Suite,
    adapter_config: AdapterConfig,
    seeds: Sequence[int],
    **options: Any,
) -> Report:
    """One suite's run: ``run_suites([suite], …)``."""
    return await run_suites([suite], adapter_config, seeds, **options)


__all__ = [
    "BUILD_DIR",
    "DEFAULT_SEEDS",
    "DEFAULT_SEED_BASE",
    "DEFAULT_SETS_DIR",
    "PRINCIPAL",
    "SCRATCH_PREFIX",
    "AdapterConfig",
    "CaseContext",
    "EndpointUnavailable",
    "NotScratch",
    "Scratch",
    "default_run_id",
    "host_of",
    "isolation_problems",
    "run",
    "run_fingerprint",
    "run_suite",
    "run_suites",
    "scratch_story_ids",
    "seeds_from",
    "story_rows",
    "sweep",
]
