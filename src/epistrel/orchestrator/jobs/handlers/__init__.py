"""Job handlers, registered by kind (#70).

M2's kinds: ``rebuild_projections`` (#46, here), ``lite_summary`` (#72, with its 10-minute
recurring safety net), and the recurring
sweeps ``retention_sweep`` / ``idempotency_purge`` (``retention``) and ``turn_queue_drain``
(``turn_queue``), registered with their recurring tasks (``recurring``; #71). M3 adds the
recurring ``media_sweep`` (#105), ``offboarding`` (#117), ``rating_check`` (#118) and the
recurring ``erasure_reconcile_retry`` (#125). A kind
no handler is registered for is refused at enqueue and ends ``failed`` if a worker claims one
(a row a newer version wrote). Tests register their own handlers through ``register`` /
``unregister``.
"""

from __future__ import annotations

from epistrel.orchestrator.jobs.handlers.erasure_reconcile import (
    ERASURE_RECONCILE_HANDLER,
    ERASURE_RECONCILE_TASK,
)
from epistrel.orchestrator.jobs.handlers.lite_summary import (
    LITE_SUMMARY_HANDLER,
    LITE_SUMMARY_TASK,
)
from epistrel.orchestrator.jobs.handlers.media_sweep import (
    MEDIA_SWEEP_HANDLER,
    MEDIA_SWEEP_TASK_DEFINITION,
)
from epistrel.orchestrator.jobs.handlers.offboarding import OFFBOARDING_HANDLER
from epistrel.orchestrator.jobs.handlers.rating_check import RATING_CHECK_HANDLER
from epistrel.orchestrator.jobs.handlers.rebuild import REBUILD_HANDLER
from epistrel.orchestrator.jobs.handlers.retention import (
    IDEMPOTENCY_HANDLER,
    IDEMPOTENCY_TASK,
    RETENTION_HANDLER,
    RETENTION_TASK,
)
from epistrel.orchestrator.jobs.handlers.turn_queue import TURN_QUEUE_HANDLER, TURN_QUEUE_TASK
from epistrel.orchestrator.jobs.recurring import register_recurring
from epistrel.orchestrator.jobs.types import JobHandler

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
):
    register(_handler)
for _task in (
    IDEMPOTENCY_TASK,
    RETENTION_TASK,
    TURN_QUEUE_TASK,
    LITE_SUMMARY_TASK,
    MEDIA_SWEEP_TASK_DEFINITION,
    ERASURE_RECONCILE_TASK,
):
    register_recurring(_task)

__all__ = ["lookup", "register", "registered", "unregister"]
