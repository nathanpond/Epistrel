"""The image limit counters' reconciler and the image accounting retention (FR-IMG-11,
NFR-REL-5; #262).

``image_counter_reconcile`` runs daily (``register_recurring``, #71; the recovery pass creates
its row due now). It compares every ``image_limit_counters`` row with the sums of the
reservation records that name it, in one REPEATABLE READ snapshot. It also finds ``held``
records whose job is terminal or missing (``core.images.limits.reconcile``). It never rewrites
anything. Each drifted counter adds 1 to ``epistrel_image_counter_drift_total{scope}``
(``deployment`` or ``entry``), and each stuck record adds 1 under ``scope="stuck_held"``. Each
finding is logged with its scope, key, period and the expected and stored counts, or with the
record's ids, and never with content. The run's ``jobs.result`` is ``{status: ok|drift, rows,
drifted, stuck_held}``. ``/health`` reads ``checks.image_counters`` from the latest finished run
(``latest_check``), which a clean run clears. The top-level status is unchanged, and
``recurring_tasks.last_status`` stays ``ok`` for a run that found drift.

``fix`` (``epistrel images reconcile --fix``) releases the stuck records through
``limits.release``. It then rewrites the drifted counters to their records' sums and creates
missing rows in lock order, all in one transaction with an ``image_counters_reconciled`` audit
row (actor ``cli:images-reconcile``, a count per scope). It never touches ``last_record_at``.

``image_accounting_retention`` runs daily too. It deletes the records, and then the counters, of
periods closed for ``image_accounting_retention_days``, in batches of 1000, and never while a
``held`` record names them (``limits.retain``), then the cost rows (#269) whose ``period_date``
is before the same cutoff.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit
from epistrel.core.images import limits
from epistrel.core.settings_schema import (
    IMAGE_ACCOUNTING_RETENTION_TASK,
    IMAGE_COUNTER_RECONCILE_TASK,
)
from epistrel.core.tables import jobs as jobs_table
from epistrel.core.tables.audit_log import AuditKind
from epistrel.observability.metrics import IMAGE_COUNTER_DRIFT
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

logger = logging.getLogger(__name__)

INTERVAL = timedelta(hours=24)
FIX_ACTOR = "cli:images-reconcile"


async def reconcile(db: AsyncEngine) -> limits.ReconcileReport:
    """One reconcile run: compare, count each finding on the drift metric and log it (ids,
    keys and counts only). It never writes."""
    report = await limits.reconcile(db)
    for drifted in report.drift:
        IMAGE_COUNTER_DRIFT.labels(scope=drifted.key.scope.value).inc()
        logger.warning(
            "image counter drift: scope=%s scope_key=%s period=%s expected=%s stored=%s",
            drifted.key.scope.value,
            drifted.key.scope_key,
            drifted.key.period.isoformat(),
            drifted.expected,
            drifted.stored,
        )
    for stuck in report.stuck:
        IMAGE_COUNTER_DRIFT.labels(scope=limits.STUCK_HELD).inc()
        logger.warning(
            "image reservation stuck held: job=%s attempt=%s job_status=%s",
            stuck.job_id,
            stuck.attempt_token,
            stuck.job_status or "missing",
        )
    return report


def summary(report: limits.ReconcileReport) -> dict[str, Any]:
    """The run's ``jobs.result``: counts only."""
    return {
        "status": "ok" if report.clean else "drift",
        "rows": report.rows,
        "drifted": len(report.drift),
        "stuck_held": len(report.stuck),
    }


async def fix(db: AsyncEngine) -> dict[str, int]:
    """``reconcile --fix`` (module docstring): the counts per scope it fixed."""
    async with db.begin() as conn:
        counts = await limits.fix_counters(conn)
        await audit.record(
            conn,
            AuditKind.IMAGE_COUNTERS_RECONCILED,
            principal=FIX_ACTOR,
            details={"counts": counts},
        )
    logger.info("image counters reconciled with --fix: %s", counts)
    return counts


async def latest_check(conn: AsyncConnection) -> Literal["ok", "drift"] | None:
    """What the latest finished reconcile run found (``None``: none has run yet)."""
    result = await conn.scalar(
        select(jobs_table.c.result)
        .where(
            jobs_table.c.kind == IMAGE_COUNTER_RECONCILE_TASK,
            jobs_table.c.status == "done",
        )
        .order_by(jobs_table.c.updated_at.desc())
        .limit(1)
    )
    if not isinstance(result, dict):
        return None
    return "drift" if result.get("status") == "drift" else "ok"


async def _reconcile(ctx: JobContext) -> JobOutput:
    return JobOutput(result=summary(await reconcile(ctx.db)))


async def _retain(ctx: JobContext) -> JobOutput:
    return JobOutput(result=await limits.retain(ctx.db))


IMAGE_COUNTER_RECONCILE_HANDLER = JobHandler(
    kind=IMAGE_COUNTER_RECONCILE_TASK,
    payload=NoPayload,
    run=recording(_reconcile),
    max_attempts=1,
    records_operation=False,
)
IMAGE_COUNTER_RECONCILE_TASK_DEFINITION = RecurringTask(IMAGE_COUNTER_RECONCILE_TASK, INTERVAL)
IMAGE_ACCOUNTING_RETENTION_HANDLER = JobHandler(
    kind=IMAGE_ACCOUNTING_RETENTION_TASK,
    payload=NoPayload,
    run=recording(_retain),
    max_attempts=1,
    records_operation=False,
)
IMAGE_ACCOUNTING_RETENTION_TASK_DEFINITION = RecurringTask(
    IMAGE_ACCOUNTING_RETENTION_TASK, INTERVAL
)

__all__ = [
    "FIX_ACTOR",
    "IMAGE_ACCOUNTING_RETENTION_HANDLER",
    "IMAGE_ACCOUNTING_RETENTION_TASK_DEFINITION",
    "IMAGE_COUNTER_RECONCILE_HANDLER",
    "IMAGE_COUNTER_RECONCILE_TASK_DEFINITION",
    "INTERVAL",
    "fix",
    "latest_check",
    "reconcile",
    "summary",
]
