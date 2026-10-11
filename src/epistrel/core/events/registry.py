"""The event payload registry: every event ``type`` names one typed payload model and category.

A payload model is a frozen Pydantic model that forbids unknown fields. Writing an
unregistered type, or a payload that fails validation, raises before any row exists
(FR-STORE-2). Payloads are stored as canonical JSON (sorted keys, no whitespace).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from epistrel.core.enums import EventCategory
from epistrel.core.errors import EpistrelError, InvalidRequest


class EventPayload(BaseModel):
    """Base of every event payload model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UnregisteredEventType(EpistrelError):
    """An event ``type`` with no registered payload model."""

    code = "unsupported_event_kind"
    http_status = 422


class InvalidEventPayload(InvalidRequest):
    """A payload that does not validate against its type's model."""


@dataclass(frozen=True, slots=True)
class EventType:
    name: str
    category: EventCategory
    model: type[EventPayload]


_REGISTRY: dict[str, EventType] = {}


def register[P: EventPayload](
    name: str, *, category: EventCategory
) -> Callable[[type[P]], type[P]]:
    """Class decorator: register ``name`` → (``category``, the decorated payload model)."""

    def decorate(model: type[P]) -> type[P]:
        if name in _REGISTRY:
            raise ValueError(f"event type {name!r} is already registered")
        _REGISTRY[name] = EventType(name=name, category=category, model=model)
        return model

    return decorate


def lookup(name: str) -> EventType:
    """The registration for ``name``; raises ``UnregisteredEventType`` if there is none."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnregisteredEventType(
            f"event type {name!r} is not registered", {"type": name}
        ) from None


def registered_types() -> dict[str, EventType]:
    """A copy of the registry, by type name."""
    return dict(_REGISTRY)


def validate(name: str, payload: EventPayload | Mapping[str, Any]) -> EventPayload:
    """Validate ``payload`` as type ``name``'s model and return the model instance."""
    event_type = lookup(name)
    if isinstance(payload, EventPayload):
        if type(payload) is not event_type.model:
            raise InvalidEventPayload(
                f"payload for {name!r} must be {event_type.model.__name__}, "
                f"not {type(payload).__name__}",
                {"type": name},
            )
        return payload
    try:
        return event_type.model.model_validate(dict(payload))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise InvalidEventPayload(
            f"payload for {name!r} failed validation", {"type": name, "fields": fields}
        ) from None


def canonical_json(payload: EventPayload) -> str:
    """The stored form of a payload: JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
