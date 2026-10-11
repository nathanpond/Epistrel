"""The request-scoped telemetry scope (#74).

A ``Scope`` names the work in progress: an ``operation_id`` (a fresh ULID until an
``Operation`` opens, then that operation's ``operations.id``), the ``kind`` (the ``Engine``
method, route or job kind), the ``story_id`` and the requesting ``principal``. Every log line
written inside it carries those fields. The REST middleware opens one per request; each outermost
``Engine`` call and each background job or queued turn opens its own.

``epistrel.core`` imports this module (``Operation`` calls ``operation_opened``), so it imports
nothing from Epistrel but ``core.ids``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from epistrel.core.ids import new_ulid


@dataclass(slots=True)
class Scope:
    """What the current work is. ``parent`` is the scope it runs inside (a request), which an
    opened operation's id reaches too, so the request line names the operation."""

    kind: str
    operation_id: str = field(default_factory=new_ulid)
    story_id: str | None = None
    principal: str | None = None
    parent: Scope | None = None
    in_engine: bool = False
    """Inside an ``Engine`` call: nested ``Engine`` calls are part of it, not lines of their own."""
    extra: dict[str, str] = field(default_factory=dict)
    """Further identifiers every line carries (a job's ``job_id``)."""

    def fields(self) -> dict[str, str]:
        found = {**self.extra, "operation_id": self.operation_id, "kind": self.kind}
        if self.story_id is not None:
            found["story_id"] = self.story_id
        if self.principal is not None:
            found["principal"] = self.principal
        return found


SCOPE: ContextVar[Scope | None] = ContextVar("epistrel_telemetry_scope", default=None)


def current() -> Scope | None:
    return SCOPE.get()


@contextmanager
def scoped(scope: Scope) -> Iterator[Scope]:
    """Make ``scope`` current for the block."""
    token = SCOPE.set(scope)
    try:
        yield scope
    finally:
        SCOPE.reset(token)


def operation_opened(operation_id: str, story_id: str | None) -> None:
    """An ``Operation`` began: its id becomes the scope's ``operation_id`` (and its parent's)."""
    scope = SCOPE.get()
    while scope is not None:
        scope.operation_id = operation_id
        if scope.story_id is None and story_id is not None:
            scope.story_id = story_id
        scope = scope.parent


__all__ = ["SCOPE", "Scope", "current", "operation_opened", "scoped"]
