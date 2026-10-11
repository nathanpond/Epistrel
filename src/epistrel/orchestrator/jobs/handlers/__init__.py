"""Job handlers, registered by kind (#70).

M2's kinds: ``rebuild_projections`` (#46, here), ``lite_summary`` (#72), and the sweeps
``retention_sweep`` / ``idempotency_purge`` (#71). A kind no handler is registered for is
refused at enqueue and ends ``failed`` if a worker claims one (a row a newer version wrote).
Tests register their own handlers through ``register`` / ``unregister``.
"""

from __future__ import annotations

from epistrel.orchestrator.jobs.handlers.rebuild import REBUILD_HANDLER
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


register(REBUILD_HANDLER)

__all__ = ["lookup", "register", "registered", "unregister"]
