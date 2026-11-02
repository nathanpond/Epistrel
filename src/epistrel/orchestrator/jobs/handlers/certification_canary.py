"""``certification_canary`` and ``certification_recertify``: the opt-in re-checks of in-use
certifications (FR-PROF-16,17; ADR-107; #234).

Hosted models can change behind an unchanged name. Two recurring tasks re-check the
certifications in use; both rows exist from the workers' first reconcile, and both return at once
while the deployment leaves them off (the owner's decision 3: **off by default**, since every run
costs real model calls), so a deployment that never opts in enqueues no run and makes no call.

- **Canary** (``certification_canary``, every ``certification_canary_interval_s``, while
  ``certification_canary_enabled``): one ``certification_run`` job of ``kind = canary`` per
  ``valid`` certification in use, on the suites behind its passed floors
  (``Harness.suites_of``), ``certification_canary_sample_size`` cases a suite over
  ``certification_canary_seeds`` seeds. The run's outcome (``certification_run``): ``held``,
  ``inconclusive`` (only a measured miss invalidates), or ``missed``, which marks the
  certification ``stale``.
- **Re-certification** (``certification_recertify``, every
  ``certification_recertify_interval_s``; ``null``: off): one full ``certification_run``
  (#233's defaults: the capability's suites, 3 seeds, no sample) per in-use certified
  fingerprint. The previous certification is left as it is.

**In use**: the current effective fingerprint of a story that is not archived, or a reference
certification (``model_tier = reference``). A certification no profile version backs (an import
without one) is skipped, and so is a fingerprint with a run already queued or running. Each
due time fans out once (``max_attempts = 1``; a late fan-out still runs once, never three times);
its result holds ids and counts only (invariant 3).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.config_changes import read_settings
from epistrel.core.enums import StoryStatus
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import certification, effective
from epistrel.core.profiles.certification import VALID
from epistrel.core.settings_schema import (
    CERTIFICATION_CANARY_TASK,
    CERTIFICATION_RECERTIFY_TASK,
    DEFAULT_CANARY_INTERVAL_S,
    DeploymentSettings,
)
from epistrel.core.tables import certification_runs, profile_certifications, stories
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers.certification_run import (
    ACTIVE,
    CANARY,
    CERTIFICATION_RUN,
    DEFAULT_SEEDS,
    RunPayload,
    _target,
)
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.facts import runtime_facts

log = get_logger(__name__)

CANARY_PRINCIPAL = "system:canary"
RECERTIFY_PRINCIPAL = "system:recertify"
REFERENCE_TIER = "reference"


async def in_use(conn: AsyncConnection) -> list[Any]:
    """The ``valid`` certifications in use (module docstring), newest per fingerprint."""
    facts = await runtime_facts(conn)
    s = stories.c
    ids = await conn.scalars(
        select(s.id).where(s.archived_at.is_(None), s.status != StoryStatus.ARCHIVED.value)
    )
    fingerprints: set[str] = set()
    for story_id in ids:
        current = await effective.effective_configuration(conn, str(story_id), validate=False)
        fingerprints.add(current.configuration().stamp(facts).fingerprint)
    t = profile_certifications.c
    rows = await conn.execute(
        select(t.id, t.fingerprint, t.profile_id, t.profile_version, t.floors_passed, t.model_tier)
        .where(t.status == VALID)
        .order_by(t.fingerprint, t.run_at.desc(), t.id.desc())
    )
    found: dict[str, Any] = {}
    for row in rows:
        used = row.fingerprint in fingerprints or row.model_tier == REFERENCE_TIER
        if used and row.floors_passed and str(row.fingerprint) not in found:
            found[str(row.fingerprint)] = row
    return list(found.values())


async def _busy(conn: AsyncConnection, fingerprint: str) -> bool:
    t = certification_runs.c
    found = await conn.scalar(
        select(t.id).where(t.fingerprint == fingerprint, t.status.in_(ACTIVE)).limit(1)
    )
    return found is not None


async def _queue(conn: AsyncConnection, **values: Any) -> str:
    run_id = new_ulid()
    job_id = await queue.enqueue(conn, CERTIFICATION_RUN, RunPayload(run_id=run_id))
    await conn.execute(certification_runs.insert().values(id=run_id, job_id=job_id, **values))
    return run_id


async def _fan_out(ctx: JobContext, *, recertify: bool) -> JobOutput:
    harness = certification.harness_or_none()
    async with ctx.db.begin() as conn:
        settings: DeploymentSettings = (await read_settings(conn)).as_settings()
        enabled = (
            settings.certification_recertify_interval_s is not None
            if recertify
            else settings.certification_canary_enabled
        )
        if not enabled:
            return JobOutput(result={"enabled": False})
        if harness is None:
            return JobOutput(result={"enabled": True, "harness": False})
        queued: list[str] = []
        skipped = 0
        facts = await runtime_facts(conn)
        for row in await in_use(conn):
            if row.profile_id is None or row.profile_version is None:
                skipped += 1
                continue
            if await _busy(conn, str(row.fingerprint)):
                skipped += 1
                continue
            common = {
                "fingerprint": row.fingerprint,
                "profile_id": row.profile_id,
                "profile_version": row.profile_version,
                "estimate": {},
            }
            if recertify:
                _, profile_settings = await _target(
                    conn,
                    profile_id=str(row.profile_id),
                    story_id=None,
                    version=int(row.profile_version),
                    facts=facts,
                )
                suites = harness.applicable(profile_settings)
                if not suites:
                    skipped += 1
                    continue
                run_id = await _queue(
                    conn,
                    **common,
                    suites=suites,
                    seeds=DEFAULT_SEEDS,
                    requested_by=RECERTIFY_PRINCIPAL,
                )
            else:
                suites = harness.suites_of([str(f["metric_id"]) for f in row.floors_passed])
                if not suites:
                    skipped += 1
                    continue
                run_id = await _queue(
                    conn,
                    **common,
                    kind=CANARY,
                    checks_certification_id=row.id,
                    suites=suites,
                    seeds=settings.certification_canary_seeds,
                    sample=settings.certification_canary_sample_size,
                    requested_by=CANARY_PRINCIPAL,
                )
            queued.append(run_id)
    log.info(
        "certification re-checks queued",
        task=ctx.job.kind,
        runs=len(queued),
        skipped=skipped,
    )
    return JobOutput(result={"enabled": True, "runs": queued, "skipped": skipped})


async def _canary(ctx: JobContext) -> JobOutput:
    return await _fan_out(ctx, recertify=False)


async def _recertify(ctx: JobContext) -> JobOutput:
    return await _fan_out(ctx, recertify=True)


CERTIFICATION_CANARY_HANDLER = JobHandler(
    kind=CERTIFICATION_CANARY_TASK,
    payload=NoPayload,
    run=recording(_canary),
    max_attempts=1,
    records_operation=False,
)
CERTIFICATION_RECERTIFY_HANDLER = JobHandler(
    kind=CERTIFICATION_RECERTIFY_TASK,
    payload=NoPayload,
    run=recording(_recertify),
    max_attempts=1,
    records_operation=False,
)
CERTIFICATION_CANARY_TASK_DEFINITION = RecurringTask(
    CERTIFICATION_CANARY_TASK,
    timedelta(seconds=DEFAULT_CANARY_INTERVAL_S),
    interval_setting="certification_canary_interval_s",
)
CERTIFICATION_RECERTIFY_TASK_DEFINITION = RecurringTask(
    CERTIFICATION_RECERTIFY_TASK,
    timedelta(seconds=DEFAULT_CANARY_INTERVAL_S),
    interval_setting="recertify_interval_s",
)

__all__ = [
    "CANARY_PRINCIPAL",
    "CERTIFICATION_CANARY_HANDLER",
    "CERTIFICATION_CANARY_TASK_DEFINITION",
    "CERTIFICATION_RECERTIFY_HANDLER",
    "CERTIFICATION_RECERTIFY_TASK_DEFINITION",
    "RECERTIFY_PRINCIPAL",
    "in_use",
]
