"""Job handlers, registered by kind (#70).

M2's kinds: ``rebuild_projections`` (#46, here), ``lite_summary`` (#72, with its 10-minute
recurring safety net), and the recurring
sweeps ``retention_sweep`` / ``idempotency_purge`` (``retention``) and ``turn_queue_drain``
(``turn_queue``), registered with their recurring tasks (``recurring``; #71). M3 adds the
recurring ``media_sweep`` (#105), ``offboarding`` (#117), ``rating_check`` (#118) and the
recurring ``erasure_reconcile_retry`` (#125). M4 adds ``importance_scoring`` (#135, with its daily
recurring safety net), the embedding port's ``embed`` and ``reembed`` (#138) and
``consolidation`` (#143), and the hot set's ``hot_set_retirement`` and its daily
``hot_counts_reconcile`` (#147), and claim detection's post-commit ``extraction_shadow``
(#148, ``orchestrator.verify.extraction``). M6 adds the on-demand State proposal analysis
``state_proposal_analysis`` (#192), and the outbox dispatcher's ``outbox_dispatch``,
its ``outbox_lease`` timer kind and the ``outbox_recovery`` sweep every 60 s (#210), and the
``reservation_expiry`` timer kind that releases an expired State hold (#216), and the late
settlement of an outcome at the next inter-turn slot, ``outcome_settlement`` (#217). M7 adds
the on-demand ``certification_run`` (#233) and ``backfill`` (#228). M8 adds ``image_job`` and
the ``image_staging_sweep`` every 10 minutes (#256), and the hourly ``image_candidate_expiry``
(#260). A kind
no handler is registered for is refused at enqueue and ends ``failed`` if a worker claims one
(a row a newer version wrote). Tests register their own handlers through ``register`` /
``unregister``.
"""

from __future__ import annotations

from epistrel.orchestrator.jobs.handlers.backfill import BACKFILL_HANDLER
from epistrel.orchestrator.jobs.handlers.certification_canary import (
    CERTIFICATION_CANARY_HANDLER,
    CERTIFICATION_CANARY_TASK_DEFINITION,
    CERTIFICATION_RECERTIFY_HANDLER,
    CERTIFICATION_RECERTIFY_TASK_DEFINITION,
)
from epistrel.orchestrator.jobs.handlers.certification_run import CERTIFICATION_RUN_HANDLER
from epistrel.orchestrator.jobs.handlers.consolidation import CONSOLIDATION_HANDLER
from epistrel.orchestrator.jobs.handlers.erasure_reconcile import (
    ERASURE_RECONCILE_HANDLER,
    ERASURE_RECONCILE_TASK,
)
from epistrel.orchestrator.jobs.handlers.image_candidate_expiry import (
    IMAGE_CANDIDATE_EXPIRY_HANDLER,
    IMAGE_CANDIDATE_EXPIRY_TASK_DEFINITION,
)
from epistrel.orchestrator.jobs.handlers.image_job import (
    IMAGE_JOB_HANDLER,
    IMAGE_STAGING_SWEEP_HANDLER,
    IMAGE_STAGING_SWEEP_TASK_DEFINITION,
)
from epistrel.orchestrator.jobs.handlers.importance import IMPORTANCE_HANDLER, IMPORTANCE_TASK
from epistrel.orchestrator.jobs.handlers.lite_summary import (
    LITE_SUMMARY_HANDLER,
    LITE_SUMMARY_TASK,
)
from epistrel.orchestrator.jobs.handlers.media_sweep import (
    MEDIA_SWEEP_HANDLER,
    MEDIA_SWEEP_TASK_DEFINITION,
)
from epistrel.orchestrator.jobs.handlers.offboarding import OFFBOARDING_HANDLER
from epistrel.orchestrator.jobs.handlers.outbox import (
    OUTBOX_DISPATCH_HANDLER,
    OUTBOX_LEASE_HANDLER,
    OUTBOX_RECOVERY_HANDLER,
    OUTBOX_RECOVERY_TASK,
)
from epistrel.orchestrator.jobs.handlers.prune_policy import PRUNE_RUN_HANDLER, PRUNE_RUN_TASK
from epistrel.orchestrator.jobs.handlers.rating_check import RATING_CHECK_HANDLER
from epistrel.orchestrator.jobs.handlers.rebuild import REBUILD_HANDLER
from epistrel.orchestrator.jobs.handlers.reembed import EMBED_HANDLER, REEMBED_HANDLER
from epistrel.orchestrator.jobs.handlers.reservations import RESERVATION_EXPIRY_HANDLER
from epistrel.orchestrator.jobs.handlers.retention import (
    IDEMPOTENCY_HANDLER,
    IDEMPOTENCY_TASK,
    RETENTION_HANDLER,
    RETENTION_TASK,
)
from epistrel.orchestrator.jobs.handlers.retirement import (
    RECONCILE_HANDLER,
    RECONCILE_TASK,
    RETIREMENT_HANDLER,
)
from epistrel.orchestrator.jobs.handlers.rewrite_expiry import (
    REWRITE_EXPIRY_HANDLER,
    REWRITE_EXPIRY_TASK,
)
from epistrel.orchestrator.jobs.handlers.state_proposals import STATE_PROPOSAL_HANDLER
from epistrel.orchestrator.jobs.handlers.story_export import STORY_EXPORT_HANDLER
from epistrel.orchestrator.jobs.handlers.story_import import STORY_IMPORT_HANDLER
from epistrel.orchestrator.jobs.handlers.turn_queue import TURN_QUEUE_HANDLER, TURN_QUEUE_TASK
from epistrel.orchestrator.jobs.recurring import register_recurring
from epistrel.orchestrator.jobs.settlement import SETTLE_HANDLER
from epistrel.orchestrator.jobs.types import JobHandler
from epistrel.orchestrator.verify.extraction import SHADOW_HANDLER

_REGISTRY: dict[str, JobHandler] = {}


def register(handler: JobHandler) -> None:
    """Register ``handler`` for its kind (replacing any handler of that kind)."""
    _REGISTRY[handler.kind] = handler


def unregister(kind: str) -> None:
    _REGISTRY.pop(kind, None)


def lookup(kind: str) -> JobHandler | None:
    return _REGISTRY.get(kind)


def registered() -> dict[str, JobHandler]:
    """A copy of the registry, by kind."""
    return dict(_REGISTRY)


for _handler in (
    REBUILD_HANDLER,
    RETENTION_HANDLER,
    IDEMPOTENCY_HANDLER,
    TURN_QUEUE_HANDLER,
    LITE_SUMMARY_HANDLER,
    MEDIA_SWEEP_HANDLER,
    OFFBOARDING_HANDLER,
    RATING_CHECK_HANDLER,
    ERASURE_RECONCILE_HANDLER,
    IMPORTANCE_HANDLER,
    EMBED_HANDLER,
    REEMBED_HANDLER,
    CONSOLIDATION_HANDLER,
    RETIREMENT_HANDLER,
    RECONCILE_HANDLER,
    SHADOW_HANDLER,
    STATE_PROPOSAL_HANDLER,
    OUTBOX_DISPATCH_HANDLER,
    OUTBOX_LEASE_HANDLER,
    OUTBOX_RECOVERY_HANDLER,
    RESERVATION_EXPIRY_HANDLER,
    SETTLE_HANDLER,
    CERTIFICATION_RUN_HANDLER,
    CERTIFICATION_CANARY_HANDLER,
    CERTIFICATION_RECERTIFY_HANDLER,
    BACKFILL_HANDLER,
    REWRITE_EXPIRY_HANDLER,
    PRUNE_RUN_HANDLER,
    STORY_EXPORT_HANDLER,
    STORY_IMPORT_HANDLER,
    IMAGE_JOB_HANDLER,
    IMAGE_STAGING_SWEEP_HANDLER,
    IMAGE_CANDIDATE_EXPIRY_HANDLER,
):
    register(_handler)
for _task in (
    PRUNE_RUN_TASK,
    IDEMPOTENCY_TASK,
    RETENTION_TASK,
    TURN_QUEUE_TASK,
    LITE_SUMMARY_TASK,
    MEDIA_SWEEP_TASK_DEFINITION,
    ERASURE_RECONCILE_TASK,
    IMPORTANCE_TASK,
    RECONCILE_TASK,
    OUTBOX_RECOVERY_TASK,
    CERTIFICATION_CANARY_TASK_DEFINITION,
    CERTIFICATION_RECERTIFY_TASK_DEFINITION,
    REWRITE_EXPIRY_TASK,
    IMAGE_STAGING_SWEEP_TASK_DEFINITION,
    IMAGE_CANDIDATE_EXPIRY_TASK_DEFINITION,
):
    register_recurring(_task)

__all__ = ["lookup", "register", "registered", "unregister"]
