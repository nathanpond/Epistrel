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

**The quality judge** (#158; ``evals.judging``). ``CaseContext.judge``/``judge_score`` run the
``quality_judge`` purpose on a case's own scratch stories only (``NotScratch`` otherwise), each
call with its own budget, outside every turn. Before a suite with pairwise judge rows (E-12)
the run reads the stored judge calibration of its judge model (``calibration_path``), or
calibrates first (``calibrate``: ``run --all``); with none stored it raises
``CalibrationMissing`` before any case. A suite's judge rows (``Suite.judge_metrics``) are
``report_only`` unless that calibration is ``calibrated``, and each suite result names it.

**The judge model** (#475; ``evals.judge_model``). With ``AdapterConfig.judge_model`` set, the
harness's judge calls go to that model (``CaseContext.judge_models``): E-03's entailment
judgement, E-12/E-13's ``quality_judge`` and the judge calibration, which is stored and looked up
under the judge model. Every engine call (turns, the verifier, consolidation, jobs) keeps the
run model. The judge's endpoint is probed in the preflight too, and its ``model`` field is its
reported version. The run's fingerprint then also names the judge as ``quality_judge``'s model
(``run_fingerprint``). Each judged suite's result records its judge, or that the run model
judged itself (``SuiteResult.judge``). On the stub the judge is a stub of its own, scripted like
the run's.

**Per-purpose routing** (#179). ``AdapterConfig.real_purposes`` names the purposes a real-model
run sends to the endpoint (``--real-purpose``, repeatable; ``None``: every purpose): every other
purpose is answered by a stub of the run's own, with its default answers, so a run can put
only ``generation`` on the local model. A suite's **scripted** calls (``CaseContext.scripted``:
a prelude a case plays before what it measures, #179's planted thought) go to that stub on
every run, scripted by the suite (``CaseContext.scripted_stub``). The fingerprint names the
stub as the model of every purpose the endpoint does not serve, and the report lists the routed
purposes (``Report.real_purposes``).

**Per-suite configuration** (#179). A suite whose stories run a configuration other than the
seeded profile's (``Suite.dials``, a settings merge patch over it; ``Suite.profile_label`` names
it) reports its own fingerprint of that configuration in its result (``SuiteResult.fingerprint``
and ``profile``).

**Failures.** A ``ModelError`` during a case is a failed case, never a silent exclusion. A
suite with a model-error row (``Suite.model_error_metric``: NFR-QUAL-1's zero-leak suites, #179)
reports it there, and the case observes nothing else: an error is not a disclosure, a missed
control or a repair, and the suite's floors are judged on its completed cases (the observed-zero
rule's 300 cases are completed ones: a seed short of them because of model errors is
``insufficient_evidence``, ``gate.judge``'s ``lost``). The row is report-only (the owner's
decision of 2026-10-24: model-robustness floors belong to M9, ADR-164). Every other suite (M4's)
observes the error as bad on every metric (#134). Either way the failure record's ``detail``
carries the error's code and its content-free details (``ERROR_DETAILS``: the purpose, the
reason, the attempts and the structural error codes). Any other exception, or a malformed
labeled set, is a **harness error**: the suite's verdict is ``error``. Each failed case's inputs
and output go to ``failures`` (JSON lines, never committed) when a path is given.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import random
import secrets
import time
from collections import defaultdict
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import epistrel.evals.judging as judges
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts, fingerprint
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.seeds import profiles as seeded_profiles
from epistrel.core.stories import STORY_NAME_MAX, CreatedStory, StorySpec
from epistrel.engine import Engine
from epistrel.evals.gate import Rule, SeedTally, Verdict, gate, judge
from epistrel.evals.judge_model import JudgeEndpoint, JudgeModels
from epistrel.evals.report import (
    Host,
    JudgeCalibrationRef,
    JudgeModel,
    MetricResult,
    Report,
    SuiteJudge,
    SuiteResult,
)
from epistrel.evals.suites import CaseModel, Counts, MalformedCase, Outcome, Suite, Summary
from epistrel.orchestrator.embeddings.adapter import EMBEDDING
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.access import EVAL_SCOPE, ModelAccessLayer, eval_scope
from epistrel.orchestrator.models.adapter import (
    Message,
    ModelAdapter,
    ModelParams,
    ModelResult,
    ProbeResult,
)
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.facts import TEMPLATE_HASHES
from epistrel.orchestrator.models.registry import (
    DEFAULT_MODEL_KEY,
    GENERATION,
    QUALITY_JUDGE,
    STORY_PURPOSES,
    JudgeCriteria,
)
from epistrel.orchestrator.models.stub import STUB_MODEL, StubAdapter
from epistrel.tools import model_matrix
from epistrel.tools.generated import REPO_ROOT

PRINCIPAL = "system:evals"
"""The in-process principal every eval operation runs as (no credential: the library path)."""
SCRATCH_PREFIX = "scratch-eval-"
SCRATCH_DEADLINE_MS = 300_000
CALL_TIMEOUT_S = 120.0
DEFAULT_SEED_BASE = 20261008
DEFAULT_SEEDS = 2
DEFAULT_SETS_DIR = REPO_ROOT / "evals" / "sets"
BUILD_DIR = REPO_ROOT / "build" / "evals"
FINGERPRINT_PROFILE = seeded_profiles.STRUCTURED_NAME
FINGERPRINT_VERSION = seeded_profiles.STRUCTURED_VERSION
"""The fingerprint is the seeded ``Structured`` profile's, at its current version (the owner's
decision of 2026-10-23: every eval follows it; the committed M4 baselines keep their
``Structured (M4) v1`` label), with the
run's model as every story purpose's (#109's ``fingerprint()``; the stub run's model is
``stub``), and, for a run with a second judge model (#475), the judge as ``quality_judge``'s."""

type AdapterName = Literal["stub", "openai"]


class EndpointUnavailable(Exception):
    """The preflight probe failed: the endpoint is unreachable or does not speak the protocol."""


class NotScratch(Exception):
    """The quality judge was asked about a story that is not one of the eval's scratch stories."""


class CalibrationMissing(Exception):
    """A suite whose judge rows need the pairwise judge calibration was run with none stored for
    the run's judge model (#158): ``calibrate-judge`` (or ``run --all``) first."""


def calibration_path(config: AdapterConfig) -> Path:
    """Where the run's judge calibration is stored: ``docs/evals/judge-calibration.json`` for a
    real model (committed beside its reports), ``build/evals/`` for the stub."""
    if config.adapter == "stub":
        return BUILD_DIR / judges.CALIBRATION_FILE
    return REPO_ROOT / "docs" / "evals" / judges.CALIBRATION_FILE


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    """Where a run's model calls go. ``stub``: the deterministic stub adapter; ``openai``: the
    OpenAI-compatible endpoint at ``base_url`` serving ``model``."""

    adapter: AdapterName = "stub"
    base_url: str | None = None
    model: str | None = None
    api_key: str = "lm-studio"
    embedding_model: str | None = None
    """An embedding model at ``base_url`` (``model_config:embedding``): the retrieval suites
    embed their stories with it; ``None``: their vector arm does not run."""
    database_url: str | None = None
    hardware: str = ""
    server_version: str = ""
    host: Literal["ci", "local"] | None = None
    """``ci`` or ``local``; ``None``: ``ci`` under CI, else ``local``. Never the machine's
    name: reports are committed to a public repository, so a host is described only by its
    kind and #54's hardware and server-version fields."""
    judge_model: str | None = None
    """A second model for the harness's judge calls (#475); ``None``: the run model judges."""
    judge_base_url: str | None = None
    """The judge model's endpoint; ``None``: ``base_url``."""
    judge_api_key: str | None = None
    """The judge endpoint's key; ``None``: ``api_key``."""
    real_purposes: tuple[str, ...] | None = None
    """The purposes a real-model run sends to the endpoint (#179); ``None``: every purpose.
    Every other purpose is answered by the run's stub (module docstring)."""

    @property
    def model_name(self) -> str:
        return STUB_MODEL if self.adapter == "stub" else (self.model or "")

    @property
    def judge_name(self) -> str:
        """The model that judges: the judge model, else the run's own (#475)."""
        return self.judge_model or self.model_name

    @property
    def self_judged(self) -> bool:
        return self.judge_model is None

    @property
    def judge_endpoint(self) -> JudgeEndpoint | None:
        if self.judge_model is None:
            return None
        return JudgeEndpoint(self.judge_model, self.judge_base_url, self.judge_api_key)


def run_fingerprint(
    model: str,
    dials: Mapping[str, Any] | None = None,
    *,
    judge: str | None = None,
    real_purposes: Collection[str] | None = None,
) -> str:
    """The fingerprint the run reports (``FINGERPRINT_VERSION``'s docstring): the seeded
    profile over the deployment's seeded defaults, as a fresh deployment runs it; with
    ``dials`` (a settings merge patch) the variant they make (#158's ``gated`` configuration).
    With ``judge`` (#475: a second judge model) it also names the judge as ``quality_judge``'s
    model. FR-PROF-16 lists every model purpose's identifier, and ``quality_judge`` is one.
    Without a judge, the fingerprint is unchanged. With ``real_purposes`` (#179) every other
    story purpose's model is the stub."""
    settings = merged_settings(
        seeded_profiles.profile_defaults(), seeded_profiles.STRUCTURED_SETTINGS, dials
    )
    models = {
        purpose: ModelIdentity(
            id=model if real_purposes is None or purpose in real_purposes else STUB_MODEL
        )
        for purpose in STORY_PURPOSES
    }
    if judge is not None:
        models[QUALITY_JUDGE.name] = ModelIdentity(id=judge)
    facts = RuntimeFacts(models=models, templates=TEMPLATE_HASHES)
    return fingerprint(
        profile_id=seeded_profiles.STRUCTURED_PROFILE_ID,
        version=FINGERPRINT_VERSION,
        settings=settings,
        facts=facts,
    )


def default_run_id(fingerprint_value: str, when: datetime) -> str:
    """``<fingerprint[:12]>-<YYYY-MM-DD>`` (UTC); the report writer adds ``-2``… on a clash."""
    digest = fingerprint_value.split(":", 1)[-1]
    return f"{digest[:12]}-{when.astimezone(UTC).strftime('%Y-%m-%d')}"


# -- per-purpose routing (#179) ---------------------------------------------------------------

SCRIPTED: contextvars.ContextVar[bool] = contextvars.ContextVar("evals_scripted", default=False)
"""Set inside ``CaseContext.scripted``: the block's model calls go to the run's stub."""


class RoutedAdapter:
    """A real-model run's adapter (module docstring): the purposes in ``real_purposes`` (every
    purpose for ``None``) go to ``real`` unless a scripted block is open; everything else to
    ``stub``, which answers with its defaults or what a suite scripted."""

    name = "routed"

    def __init__(
        self,
        real: ModelAdapter,
        stub: StubAdapter,
        real_purposes: Collection[str] | None = None,
    ) -> None:
        self.real = real
        self.stub = stub
        self.real_purposes = None if real_purposes is None else frozenset(real_purposes)

    def routes_real(self, purpose: str) -> bool:
        if SCRIPTED.get():
            return False
        return self.real_purposes is None or purpose in self.real_purposes

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[Any] | None = None,
    ) -> ModelResult:
        target: ModelAdapter = self.real if self.routes_real(purpose) else self.stub
        return await target.complete(purpose, messages, params, response_schema)

    async def probe(self, purpose: str) -> ProbeResult:
        return await self.real.probe(purpose)

    async def aclose(self) -> None:
        await self.real.aclose()


@contextlib.contextmanager
def routed(engine: Engine, config: AdapterConfig) -> Iterator[None]:
    """A real-model run's engine on a ``RoutedAdapter`` for the run (restored afterwards); a
    stub run's untouched."""
    if config.adapter == "stub":
        yield
        return
    layer = engine.models
    before = layer.adapter
    layer.adapter = RoutedAdapter(before, StubAdapter(), config.real_purposes)
    try:
        yield
    finally:
        layer.adapter = before


def scripted_stub(engine: Engine) -> StubAdapter:
    """The stub a scripted block's calls reach: the engine's own on a stub run, the routed
    adapter's on a real one."""
    adapter = engine.models.adapter
    if isinstance(adapter, RoutedAdapter):
        return adapter.stub
    return engine.models.stub


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
    model: str = ""
    """The run's model (``AdapterConfig.model_name``): what a suite's fingerprints name."""
    judge_layer: ModelAccessLayer | None = None
    """The judge model's Model Access Layer (#475); ``None``: the run's own."""
    judge_model: str = ""
    """The model that judges (``AdapterConfig.judge_name``); empty: ``model``."""

    @property
    def models(self) -> ModelAccessLayer:
        return self.engine.models

    @property
    def judge_models(self) -> ModelAccessLayer:
        """Where the harness's judge calls go (#475): the judge model's layer, else the run's.
        Engine calls never use it."""
        return self.judge_layer or self.engine.models

    @property
    def principal(self) -> RequestContext:
        return _ctx()

    async def story(self, spec: StorySpec) -> CreatedStory:
        """A new scratch story (see the module docstring)."""
        return await self.scratch.create(spec)

    async def judge(
        self,
        story_id: str,
        a: str,
        b: str,
        criteria: JudgeCriteria = "voice_readability",
        *,
        key: str = "pair",
    ) -> judges.PairJudgement:
        """One ``quality_judge`` pairwise judgement, in both orders, of two passages from
        scratch story ``story_id`` (``judging.compare``; ``key`` names the pair within the case:
        its eval scope is ``<case>.<key>``); ``NotScratch`` for any other story (the judge never
        reads a live story)."""
        self.scratch.require(story_id)
        return await judges.compare(self.judge_models, a, b, criteria, case_id=self.scope_id(key))

    async def judge_score(
        self,
        story_id: str,
        message: str,
        reference: str,
        criteria: JudgeCriteria,
        *,
        key: str = "score",
    ) -> int | None:
        """One ``quality_judge`` score (1-5) of a message from scratch story ``story_id``
        against ``reference`` (``judging.score``); ``NotScratch`` for any other story."""
        self.scratch.require(story_id)
        return await judges.score(
            self.judge_models, message, reference, criteria, case_id=self.scope_id(key)
        )

    @contextlib.contextmanager
    def scripted(self) -> Iterator[None]:
        """A block whose model calls go to the run's stub on every run (module docstring):
        a case's scripted prelude."""
        token = SCRIPTED.set(True)
        try:
            yield
        finally:
            SCRIPTED.reset(token)

    @property
    def scripted_stub(self) -> StubAdapter:
        """The stub ``scripted`` blocks reach (``runner.scripted_stub``)."""
        return scripted_stub(self.engine)

    def scope_id(self, key: str) -> str:
        """``<case>.<key>``: the eval scope of one judge call of this case (a stub run scripts
        it by this id: ``judging.script_pair``/``script_score``)."""
        scope = EVAL_SCOPE.get()
        case = scope.case_id if scope is not None else self.run_id
        return f"{case}.{key}"


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
        if config.embedding_model:
            await model_config.write_config(
                engine.db,
                EMBEDDING,
                keybox=engine.models.keybox,
                principal=PRINCIPAL,
                base_url=config.base_url,
                model=config.embedding_model,
                api_key=config.api_key,
            )
    try:
        probe = await engine.models.probe(GENERATION.name)
    except ModelError as exc:
        reason = exc.details.get("reason") or exc.code
        raise EndpointUnavailable(f"the endpoint failed the protocol probe: {reason}") from None
    return probe.model or None


@contextlib.asynccontextmanager
async def judge_layer_for(config: AdapterConfig, engine: Engine) -> AsyncIterator[ModelAccessLayer]:
    """The Model Access Layer the harness's judge calls go to (#475): ``engine.models`` when
    the run model judges, else a ``JudgeModels`` over it (closed afterwards)."""
    endpoint = config.judge_endpoint
    if endpoint is None:
        yield engine.models
        return
    layer = JudgeModels(engine.models, endpoint)
    try:
        yield layer
    finally:
        await layer.aclose()


async def judge_preflight(layer: ModelAccessLayer, config: AdapterConfig) -> str | None:
    """Probe the judge model's endpoint (#475); the model it reported. Only for a second judge:
    the run model's own probe is ``preflight``'s."""
    try:
        probe = await layer.probe(QUALITY_JUDGE.name)
    except ModelError as exc:
        reason = exc.details.get("reason") or exc.code
        raise EndpointUnavailable(
            f"the judge endpoint ({config.judge_model}) failed the protocol probe: {reason}"
        ) from None
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
    full: frozenset[str] = frozenset()
    """Suites that run their full set whatever ``sample`` says (CI's E-10: a sample can draw
    most of its measured gap and land on the floor)."""
    seed_base: int = DEFAULT_SEED_BASE
    concurrency: int = 1
    failures: Path | None = None
    cases: Mapping[str, Sequence[CaseModel]] = field(default_factory=dict)
    """Cases given in memory by suite name, instead of its labeled set (tests)."""
    calibration: judges.Calibration | None = None
    """The judge calibration of the run's judge model (#158): what a suite's judge rows run
    under."""
    judge_layer: ModelAccessLayer | None = None
    """The judge model's layer (#475); ``None``: the run model judges."""
    judge_version: str | None = None
    """The version the judge's endpoint reported (the run model's when it judges itself)."""


def sample_for(suite_name: str, sample: int | None, full: Collection[str]) -> int | None:
    """The sample size ``suite_name`` runs at: ``None`` (its full set) when ``full`` names it."""
    return None if suite_name in full else sample


def _select(cases: Sequence[CaseModel], sample: int | None, seed_base: int) -> list[CaseModel]:
    ordered = sorted(cases, key=lambda c: c.id)
    if sample is None or sample >= len(ordered):
        return ordered
    return sorted(random.Random(seed_base).sample(ordered, sample), key=lambda c: c.id)  # noqa: S311 - a reproducible subset, not a secret


async def _play(suite: Suite, job: SuiteRun, case: CaseModel, seed: int) -> _Played:
    scratch = Scratch(job.engine, f"{job.run_id}-{case.id}-{seed}")
    ctx = CaseContext(
        job.engine,
        scratch,
        seed,
        job.run_id,
        job.config.model_name,
        judge_layer=job.judge_layer,
        judge_model=job.config.judge_name,
    )
    started = time.perf_counter()
    outcome: Outcome | None = None
    error: str | None = None
    try:
        with eval_scope(case.id, seed):
            outcome = await suite.evaluate(ctx, case)
    except ModelError as exc:
        outcome = model_error_outcome(suite, exc)
    except Exception as exc:
        error = f"case {case.id} seed {seed}: {type(exc).__name__}: {exc}"
    finally:
        try:
            await scratch.delete_all()
        except Exception as exc:
            error = error or f"case {case.id} seed {seed}: cleanup failed: {type(exc).__name__}"
    latency = round((time.perf_counter() - started) * 1000)
    errored = suite.model_error_metric
    if outcome is not None and errored is not None and errored not in outcome.observations:
        outcome = replace(outcome, observations={**outcome.observations, errored: True})
    return _Played(case, seed, outcome, error, latency)


ERROR_DETAILS = ("purpose", "reason", "provider_status", "attempts", "structural_errors")
"""The keys of a ``ModelError``'s details a failure record keeps: what failed and why, as codes
and counts, never the model's answer or the prompt (invariant 3)."""


def model_error_outcome(suite: Suite, exc: ModelError) -> Outcome:
    """A case that ended in ``exc`` (module docstring): with the suite's model-error row, that
    row observes it as bad and nothing else is observed; without one, every metric is bad
    (#134). ``detail`` names the error: its code, class and ``ERROR_DETAILS``."""
    errored = suite.model_error_metric
    observations: dict[str, bool | None]
    if errored is None:
        observations = {metric.id: False for metric in suite.metrics}
    else:
        observations = {metric.id: None for metric in suite.metrics}
        observations[errored] = False
    details = {k: exc.details[k] for k in ERROR_DETAILS if k in exc.details}
    return Outcome(
        observations,
        detail={"model_error": exc.code, "error_type": type(exc).__name__, **details},
    )


def _record_failure(job: SuiteRun, suite: Suite, played: _Played) -> None:
    if job.failures is None or played.outcome is None:
        return
    # A case failed when it missed a floored metric (a report-only cell such as #157's "found
    # first by this layer" is not a failure); a suite with no floor counts every metric.
    floored = {m.id for m in suite.metrics if m.rule is not Rule.informational}
    watched = floored or {m.id for m in suite.metrics}
    if "model_error" not in played.outcome.detail and all(
        played.outcome.observations.get(m) is not False for m in watched
    ):
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


async def _play_seeds(
    suite: Suite,
    job: SuiteRun,
    cases: Sequence[CaseModel],
    tallies: dict[str, dict[int, SeedTally]],
    errors: list[str],
    latencies: list[int],
    gate_slots: asyncio.Semaphore,
    every: list[Outcome],
    lost: dict[int, int],
) -> None:
    """Every seed's cases, in its shuffled order, tallied into ``tallies`` (and every outcome
    collected into ``every``, for ``Suite.summarize``; per seed, the cases a model error ended
    counted in ``lost``)."""

    async def bounded(case: CaseModel, seed: int) -> _Played:
        async with gate_slots:
            return await _play(suite, job, case, seed)

    for seed in job.seeds:
        order = list(cases)
        random.Random(seed).shuffle(order)  # noqa: S311 - a reproducible order, not a secret
        for metric in suite.metrics:
            tallies[metric.id][seed] = SeedTally(seed)
        outcomes: list[Outcome] = []
        for played in await asyncio.gather(*(bounded(c, seed) for c in order)):
            latencies.append(played.latency_ms)
            if played.error is not None:
                errors.append(played.error)
            if played.outcome is None:
                continue
            if "model_error" in played.outcome.detail:
                lost[seed] = lost.get(seed, 0) + 1
            outcomes.append(played.outcome)
            every.append(played.outcome)
            for metric in suite.metrics:
                seen = played.outcome.observations.get(metric.id)
                if seen is not None:
                    t = tallies[metric.id][seed]
                    tallies[metric.id][seed] = SeedTally(
                        seed, t.good + bool(seen), t.bad + (not seen)
                    )
            _record_failure(job, suite, played)
        for metric_id, found in suite.aggregate(outcomes).items():
            if isinstance(found, Counts):
                tallies[metric_id][seed] = SeedTally(seed, found.good, found.bad)
            elif found.value is None:
                tallies[metric_id][seed] = SeedTally(seed)
            else:
                tallies[metric_id][seed] = SeedTally(seed, found.n, 0, found.value)


async def run_suite(suite: Suite, job: SuiteRun) -> SuiteResult:
    """Every seed of ``suite`` (module docstring)."""
    started = time.perf_counter()
    try:
        loaded = job.cases.get(suite.name)
        cases = _select(
            loaded if loaded is not None else suite.load(job.sets_dir),
            sample_for(suite.name, job.sample, job.full),
            job.seed_base,
        )
    except MalformedCase as exc:
        return SuiteResult.errored(suite, [str(exc)], list(job.seeds))
    if job.config.adapter == "stub":
        stub = job.engine.models.stub
        # A second judge is a stub of its own (#475), scripted alike: only judge calls reach it.
        judge_stub = job.judge_layer.stub if job.judge_layer is not None else None
        for case in cases:
            suite.script_stub(case, stub)
            if judge_stub is not None and suite.judge_purposes:
                suite.script_stub(case, judge_stub)
    tallies: dict[str, dict[int, SeedTally]] = defaultdict(dict)
    errors: list[str] = []
    latencies: list[int] = []
    gate_slots = asyncio.Semaphore(max(1, job.concurrency))
    every: list[Outcome] = []
    lost: dict[int, int] = {}
    try:
        await suite.setup(job.engine, _ctx())
    except Exception as exc:
        return SuiteResult.errored(suite, [f"setup: {type(exc).__name__}: {exc}"], list(job.seeds))
    try:
        await _play_seeds(suite, job, cases, tallies, errors, latencies, gate_slots, every, lost)
    finally:
        try:
            await suite.teardown(job.engine, _ctx())
        except Exception as exc:
            errors.append(f"teardown: {type(exc).__name__}: {exc}")
    summary = Summary()
    try:
        summary = suite.summarize(every, job.config.model_name)
    except Exception as exc:
        errors.append(f"summary: {type(exc).__name__}: {exc}")
    calibration = calibration_ref(suite, job)
    metrics = []
    for metric in suite.metrics:
        ordered = sorted(tallies[metric.id].items())
        seed_tallies = [tally for _, tally in ordered]
        # A suite with a model-error row judges its floors on the completed cases (#179).
        missed = [lost.get(seed, 0) for seed, _ in ordered] if suite.model_error_metric else []
        result = MetricResult.of(metric, judge(metric, seed_tallies, lost=missed))
        added = dict(summary.metrics.get(metric.id, {}))
        if metric.id in suite.judge_metrics:
            added["judge_based"] = True
            if calibration is None or calibration.status != "calibrated":
                # An uncalibrated judge certifies nothing (#158): its rows are report-only.
                added["verdict"] = Verdict.report_only
        metrics.append(result.model_copy(update=added))
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
        notes=[*suite.reading_notes, *summary.notes],
        calibration=calibration,
        judge=suite_judge(suite, job),
        profile=suite.profile_label,
        fingerprint=suite_fingerprint(suite, job.config),
    )


def suite_fingerprint(suite: Suite, config: AdapterConfig) -> str | None:
    """The fingerprint of the configuration ``suite``'s stories ran under, when it is not the
    seeded profile's (``Suite.dials``, #179); ``None`` otherwise (the run's fingerprint)."""
    if suite.dials is None:
        return None
    return run_fingerprint(
        config.model_name,
        suite.dials,
        judge=config.judge_model,
        real_purposes=routed_purposes(config),
    )


def suite_judge(suite: Suite, job: SuiteRun) -> SuiteJudge | None:
    """The judge a judged suite's calls went to (#475); ``None`` for a suite without any."""
    if not suite.judge_purposes:
        return None
    return SuiteJudge(
        model=job.config.judge_name,
        model_version=job.judge_version,
        purposes=list(suite.judge_purposes),
        self_judged=job.config.self_judged,
    )


def calibration_ref(suite: Suite, job: SuiteRun) -> JudgeCalibrationRef | None:
    """The calibration a suite's judge rows run under (``None`` for a suite without any)."""
    if not suite.judge_metrics:
        return None
    model = job.config.judge_name
    found = job.calibration
    if suite.judge_calibration == "none":
        return JudgeCalibrationRef(status="uncalibrated", judge_model=model)
    if found is None:
        return JudgeCalibrationRef(status="not_run", judge_model=model)
    return JudgeCalibrationRef(
        status=found.status,
        judge_model=found.judge_model,
        date=found.date,
        pairs=found.pairs,
        agreement=found.agreement,
        ci95=found.ci95,
        floor=found.floor,
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
    full: Collection[str] = (),
    seed_base: int = DEFAULT_SEED_BASE,
    concurrency: int = 1,
    run_id: str | None = None,
    failures: Path | None = None,
    cases: Mapping[str, Sequence[CaseModel]] | None = None,
    on_suite: Callable[[Report], Awaitable[None] | None] | None = None,
    now: datetime | None = None,
    calibrate: bool = False,
    calibration_file: Path | None = None,
    calibration_flip: Collection[str] = (),
) -> Report:
    """One run of ``suites`` (module docstring); ``on_suite`` sees the report after each suite
    (the CLI writes the JSON incrementally).

    **The judge calibration** (#158): with ``calibrate`` the judge is calibrated first, on the
    same engine, and stored in ``calibration_file`` (default ``calibration_path``); otherwise a
    suite with pairwise judge rows reads the stored calibration of the run's judge model, and
    ``CalibrationMissing`` is raised before any case when there is none. ``calibration_flip``
    (tests) names calibration pairs the stub answers wrongly."""
    started = now or datetime.now(UTC)
    store = calibration_file or calibration_path(config)
    stored: judges.Calibration | None = None
    if not calibrate and any(s.judge_calibration == "pairwise" for s in suites):
        stored = judges.lookup(store, config.judge_name)
        if stored is None:
            raise CalibrationMissing(
                f"no judge calibration for {config.judge_name!r} in {store}: run "
                "`calibrate-judge` (or `run --all`) first"
            )
    fingerprint_value = run_fingerprint(
        config.model_name, judge=config.judge_model, real_purposes=routed_purposes(config)
    )
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
        real_purposes=routed_purposes(config),
    )
    async with (
        engine_for(config, engine) as live,
        judge_layer_for(config, live) as judging,
    ):
        with routed(live, config):
            report.model_version = await preflight(live, config)
            judge_version = report.model_version
            if config.judge_model is not None:
                judge_version = await judge_preflight(judging, config)
                report.judge = JudgeModel(
                    model=config.judge_model,
                    model_version=judge_version,
                    endpoint=(config.judge_base_url or config.base_url)
                    if config.adapter == "openai"
                    else None,
                )
            if calibrate:
                stored = await calibrate_judge(
                    live,
                    config,
                    sets_dir=sets_dir,
                    model_version=judge_version,
                    flip=calibration_flip,
                    seed=seed_base,
                    judge_layer=judging,
                )
                judges.save(store, stored)
            await sweep(live)
            before = await story_rows(live.db)
            job = SuiteRun(
                engine=live,
                config=config,
                seeds=seeds,
                run_id=report.run_id,
                sets_dir=sets_dir,
                sample=sample,
                full=frozenset(full),
                seed_base=report.seed_base,
                concurrency=concurrency,
                failures=failures,
                cases=cases or {},
                calibration=stored,
                judge_layer=None if config.self_judged else judging,
                judge_version=judge_version,
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


def routed_purposes(config: AdapterConfig) -> list[str] | None:
    """The purposes a real-model run routes to its endpoint (``None``: every one; a stub run
    names none)."""
    if config.adapter == "stub" or config.real_purposes is None:
        return None
    return sorted(config.real_purposes)


async def calibrate_judge(
    engine: Engine,
    config: AdapterConfig,
    *,
    sets_dir: Path = DEFAULT_SETS_DIR,
    model_version: str | None = None,
    flip: Collection[str] = (),
    seed: int | None = DEFAULT_SEED_BASE,
    judge_layer: ModelAccessLayer | None = None,
) -> judges.Calibration:
    """The judge calibration of the run's judge model on the labeled pairs under ``sets_dir``
    (``judging`` module docstring), through ``judge_layer`` (#475: the judge model's; default
    the engine's); on the stub, its judge is scripted to every label but the pairs in ``flip``.
    The judge's calls are outside every turn and story."""
    layer = judge_layer or engine.models
    pairs = judges.load_pairs(sets_dir)
    if config.adapter == "stub":
        judges.script_calibration(layer.stub, pairs, flip)
    results = await judges.judge_pairs(layer, pairs, seed=seed)
    return judges.summarize(
        results,
        judge_model=config.judge_name,
        model_version=model_version,
        engine_sha=model_matrix.engine_sha(),
    )


async def run_calibration(
    config: AdapterConfig,
    *,
    engine: Engine | None = None,
    sets_dir: Path = DEFAULT_SETS_DIR,
    calibration_file: Path | None = None,
    flip: Collection[str] = (),
) -> judges.Calibration:
    """``calibrate-judge``: preflight the endpoint, calibrate its judge and store the result
    (``calibration_path`` unless ``calibration_file``)."""
    async with engine_for(config, engine) as live, judge_layer_for(config, live) as judging:
        version = await preflight(live, config)
        if config.judge_model is not None:
            version = await judge_preflight(judging, config)
        found = await calibrate_judge(
            live,
            config,
            sets_dir=sets_dir,
            model_version=version,
            flip=flip,
            judge_layer=judging,
        )
    judges.save(calibration_file or calibration_path(config), found)
    return found


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
    "CalibrationMissing",
    "CaseContext",
    "EndpointUnavailable",
    "NotScratch",
    "Scratch",
    "calibrate_judge",
    "calibration_path",
    "default_run_id",
    "host_of",
    "isolation_problems",
    "judge_layer_for",
    "judge_preflight",
    "run",
    "run_calibration",
    "run_fingerprint",
    "run_suite",
    "run_suites",
    "scratch_story_ids",
    "seeds_from",
    "story_rows",
    "suite_judge",
    "sweep",
]
