"""``certification_run``: certify one configuration with the eval harness (FR-PROF-17; #233).

**The request** (``request_run``: ``POST /engine-profiles/{id}/certification-runs`` and
``POST /stories/{id}/certification-runs``) names a profile version (the latest by default), or
a story whose effective configuration is certified (its profile version and its overrides), and
what to run: ``suites`` (default: every suite the configuration enables; an unknown or
not-enabled one is 422 ``validation_error``), ``seeds`` (1-5, default 3) and ``sample`` (a
per-suite case cap, at least 10). Without ``confirm`` it answers the estimate
(``Harness.estimate``), the fingerprint and the suites, and writes nothing. With ``confirm`` it
refuses a second run for a fingerprint already ``queued`` or ``running`` (409
``certification_run_in_progress``), then writes the ``certification_runs`` row and enqueues
this job in one transaction (202 ``{run_id, job_id}``, the estimate repeated). The
fingerprint is captured then: a later configuration change does not affect the run.

**The job** (one attempt: a run is expensive) marks the run ``running``, hands it to the
installed harness (``core.profiles.certification.harness``: ``epistrel.evals.certify``), and
records what it returns: the report summary on the run row and, when a floor passed, the
certification record (``recorded_via = job``) linked from the run. A run that fails keeps its
partial summary and its error, and records no certification. It changes no configuration:
the profile's versions and the story rows are left as they are. The job writes no story state,
so it records no operation; its result holds ids and counts only (invariant 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.errors import (
    CertificationRunInProgress,
    CertificationRunNotFound,
    InvalidRequest,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.profiles import certification, effective, tiers
from epistrel.core.profiles.certification import HarnessRequest, RunEstimate
from epistrel.core.profiles.effective import Configuration
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.tables import certification_runs, deployment_state, engine_profiles
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

log = get_logger(__name__)

CERTIFICATION_RUN = "certification_run"
DEFAULT_SEEDS = 3
SAMPLE_MIN = 10
ACTIVE = ("queued", "running")


class CertificationRunRequest(BaseModel):
    """A certification run's request (FR-PROF-17): the profile version (latest by default; a
    story's run uses the story's), the suites (default: every one the configuration enables),
    the seed count, a per-suite case cap, and ``confirm`` (without it: the estimate only)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"suites": ["smoke"], "seeds": 2, "confirm": True}]},
    )

    version: int | None = Field(default=None, ge=1)
    suites: list[str] | None = Field(default=None, min_length=1, max_length=50)
    seeds: int = Field(default=DEFAULT_SEEDS, ge=1, le=5)
    sample: int | None = Field(default=None, ge=SAMPLE_MIN)
    confirm: StrictBool = False


@dataclass(frozen=True, slots=True)
class CertificationRunAnswer:
    """A run request's answer: what the run would take, the fingerprint it certifies and the
    suites it runs; with ``confirm`` also the run and its job (REST 202), else nothing is
    written (200). ``config_revision`` is the deployment's, unchanged."""

    estimate: RunEstimate
    fingerprint: str
    suites: list[str]
    config_revision: int
    run_id: str | None = None
    job_id: str | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


class CertificationRun(BaseModel):
    """A certification run: what it certifies and runs, its status and estimate, its report
    summary (per suite and floor) and the certification it recorded (``null`` when no floor
    passed, or the run failed)."""

    id: str
    kind: str
    fingerprint: str
    profile_id: str
    profile_version: int
    story_id: str | None
    suites: list[str]
    seeds: int
    sample: int | None
    estimate: dict[str, Any]
    status: str
    job_id: str | None
    certification_id: str | None
    report: dict[str, Any] | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class RunPayload(BaseModel):
    """The run a ``certification_run`` job plays."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=ULID_PATTERN)


async def _profile_name(conn: AsyncConnection, profile_id: str) -> str:
    found = await conn.scalar(
        select(engine_profiles.c.name).where(engine_profiles.c.id == profile_id)
    )
    return str(found or profile_id)


async def _target(
    conn: AsyncConnection,
    *,
    profile_id: str | None,
    story_id: str | None,
    version: int | None,
    facts: RuntimeFacts,
) -> tuple[HarnessRequest, Any]:
    """The configuration a run certifies, as the harness will play it, and its settings."""
    defaults = await effective.profile_defaults(conn)
    deployment = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    strongest = tiers.strongest_of(deployment)
    overrides: dict[str, Any] = {}
    if story_id is not None:
        current = await effective.effective_configuration(conn, story_id)
        profile_id, version = current.profile_id, current.version
        document, overrides = dict(current.document), dict(current.overrides)
        settings = current.settings
    else:
        from epistrel.core.profiles import store

        assert profile_id is not None  # noqa: S101 - one of the two is given
        found = await store.read_profile(conn, profile_id, version)
        version, document = found.version, dict(found.settings)
        settings = tiers.resolved(effective.merged_settings(defaults, document), strongest)
    stamped = Configuration(profile_id, version, settings).stamp(facts)
    request = HarnessRequest(
        run_id="",
        fingerprint=stamped.fingerprint,
        profile_id=profile_id,
        profile_name=await _profile_name(conn, profile_id),
        profile_version=version,
        document=document,
        overrides=overrides,
        models={
            purpose: model.model_dump(mode="json") for purpose, model in stamped.models.items()
        },
        suites=[],
        seeds=1,
    )
    return request, settings


async def request_run(
    db: AsyncEngine,
    body: CertificationRunRequest,
    *,
    principal: str,
    facts: RuntimeFacts,
    profile_id: str | None = None,
    story_id: str | None = None,
) -> CertificationRunAnswer:
    """Estimate, or with ``confirm`` queue, a certification run (see the module docstring)."""
    harness = certification.harness()
    async with db.begin() as conn:
        target, settings = await _target(
            conn, profile_id=profile_id, story_id=story_id, version=body.version, facts=facts
        )
        enabled = harness.applicable(settings)
        suites = list(body.suites) if body.suites is not None else enabled
        unknown = sorted(set(suites) - set(harness.suites()))
        disabled = sorted(set(suites) - set(enabled) - set(unknown))
        if unknown or disabled or not suites:
            raise InvalidRequest(
                "a certification run names suites the configuration enables",
                {
                    "field": "body.suites",
                    "unknown": unknown,
                    "not_enabled": disabled,
                    "enabled": enabled,
                },
            )
        estimate = harness.estimate(suites, body.seeds, body.sample)
        revision = int(
            await conn.scalar(
                select(deployment_state.c.config_revision).where(
                    deployment_state.c.id == DEPLOYMENT_ROW_ID
                )
            )
            or 0
        )
        if not body.confirm:
            return CertificationRunAnswer(
                estimate=estimate,
                fingerprint=target.fingerprint,
                suites=suites,
                config_revision=revision,
            )
        t = certification_runs
        running = await conn.scalar(
            select(t.c.id)
            .where(t.c.fingerprint == target.fingerprint, t.c.status.in_(ACTIVE))
            .limit(1)
        )
        if running is not None:
            raise CertificationRunInProgress(
                "a certification run for this configuration is already queued or running",
                {"run_id": str(running), "fingerprint": target.fingerprint},
            )
        run_id = new_ulid()
        await conn.execute(
            t.insert().values(
                id=run_id,
                fingerprint=target.fingerprint,
                profile_id=target.profile_id,
                profile_version=target.profile_version,
                story_id=story_id,
                suites=suites,
                seeds=body.seeds,
                sample=body.sample,
                estimate=estimate.model_dump(mode="json"),
                requested_by=principal,
            )
        )
        job_id = await queue.enqueue(conn, CERTIFICATION_RUN, RunPayload(run_id=run_id))
        await conn.execute(update(t).where(t.c.id == run_id).values(job_id=job_id))
    log.info("certification run queued", run_id=run_id, suites=len(suites), seeds=body.seeds)
    return CertificationRunAnswer(
        estimate=estimate,
        fingerprint=target.fingerprint,
        suites=suites,
        config_revision=revision,
        run_id=run_id,
        job_id=job_id,
    )


async def get_run(db: AsyncEngine, run_id: str) -> CertificationRun:
    """The run ``run_id``; 404 ``certification_run_not_found``."""
    t = certification_runs
    async with db.connect() as conn:
        row = (await conn.execute(select(t).where(t.c.id == run_id))).one_or_none()
    if row is None:
        raise CertificationRunNotFound("no such certification run", {"run_id": run_id})
    return CertificationRun.model_validate(
        {key: getattr(row, key) for key in CertificationRun.model_fields}
    )


async def _finish(db: AsyncEngine, run_id: str, **values: Any) -> None:
    async with db.begin() as conn:
        await conn.execute(
            update(certification_runs)
            .where(certification_runs.c.id == run_id)
            .values(finished_at=datetime.now(UTC), **values)
        )


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(RunPayload, ctx.payload)
    t = certification_runs
    async with ctx.db.begin() as conn:
        row = (await conn.execute(select(t).where(t.c.id == payload.run_id))).one_or_none()
        if row is None or row.status != "queued":
            return JobOutput(result={"run_id": payload.run_id, "skipped": "not_queued"})
        await conn.execute(
            update(t)
            .where(t.c.id == payload.run_id)
            .values(status="running", started_at=datetime.now(UTC))
        )
        document: dict[str, Any] = {}
        overrides: dict[str, Any] = {}
        if row.story_id is not None:
            current = await effective.effective_configuration(conn, str(row.story_id))
            if current.profile_id == str(row.profile_id) and current.version == row.profile_version:
                document, overrides = dict(current.document), dict(current.overrides)
        if not document:
            from epistrel.core.profiles import store

            found = await store.read_profile(conn, str(row.profile_id), int(row.profile_version))
            document = dict(found.settings)
        name = await _profile_name(conn, str(row.profile_id))
    request = HarnessRequest(
        run_id=payload.run_id,
        fingerprint=str(row.fingerprint),
        profile_id=str(row.profile_id),
        profile_name=name,
        profile_version=int(row.profile_version),
        document=document,
        overrides=overrides,
        suites=list(row.suites),
        seeds=int(row.seeds),
        sample=row.sample,
        reference=bool(row.reference),
    )
    try:
        outcome = await certification.harness().run(request)
    except Exception as exc:
        await _finish(ctx.db, payload.run_id, status="failed", error=type(exc).__name__)
        log.warning("certification run failed", run_id=payload.run_id, error=type(exc).__name__)
        return JobOutput(result={"run_id": payload.run_id, "status": "failed"})
    certification_id: str | None = None
    async with ctx.db.begin() as conn:
        if outcome.record is not None:
            certification_id, _ = await certification.store_record(conn, outcome.record, via="job")
        await conn.execute(
            update(t)
            .where(t.c.id == payload.run_id)
            .values(
                status="completed",
                report=outcome.summary,
                certification_id=certification_id,
                finished_at=datetime.now(UTC),
            )
        )
    return JobOutput(
        result={
            "run_id": payload.run_id,
            "status": "completed",
            "certification_id": certification_id,
        }
    )


CERTIFICATION_RUN_HANDLER = JobHandler(
    kind=CERTIFICATION_RUN,
    payload=RunPayload,
    run=_run,
    max_attempts=1,
    records_operation=False,
)

__all__ = [
    "CERTIFICATION_RUN",
    "CERTIFICATION_RUN_HANDLER",
    "CertificationRun",
    "CertificationRunAnswer",
    "CertificationRunRequest",
    "RunPayload",
    "get_run",
    "request_run",
]
