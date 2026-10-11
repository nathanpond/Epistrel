"""The event store's write side: payload registry, M2 event types, and ``Operation``.

Importing this package registers every M2 event type (``epistrel.core.events.types``).
"""

from epistrel.core.events import registry, types
from epistrel.core.events.operation import Operation, StagedEvent, StoryNotFound
from epistrel.core.events.registry import (
    EventPayload,
    InvalidEventPayload,
    UnregisteredEventType,
    register,
)

__all__ = [
    "EventPayload",
    "InvalidEventPayload",
    "Operation",
    "StagedEvent",
    "StoryNotFound",
    "UnregisteredEventType",
    "register",
    "registry",
    "types",
]
