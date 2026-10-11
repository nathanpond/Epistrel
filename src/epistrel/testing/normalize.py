"""Id-free comparison of two stories' contents (#67 creates it; #75 reuses it).

A fork copies a story under new ids, so "the fork equals the source" means equal once every id
is replaced by its position among the ids seen so far. ``Normalizer`` does that: each ULID
(alone, or inside a string such as ``character:<id>``) becomes ``<id:n>``, numbered in the
order first seen, so two normalisers fed corresponding values in the same order produce equal
output exactly when the values correspond id for id. Pydantic models, dataclasses, mappings and
sequences are walked; other values are kept.

``history`` turns a ``views`` event list into comparable rows: no event id, story or ``seq``
(a fork renumbers ``seq`` densely), everything else kept, ids normalised.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from epistrel.core.ids import CROCKFORD, ULID_LENGTH

ID_PATTERN = re.compile(rf"(?<![{CROCKFORD}])[{CROCKFORD}]{{{ULID_LENGTH}}}(?![{CROCKFORD}])")

EVENT_FIELDS_DROPPED = frozenset({"id", "story_id", "seq"})
"""Event fields that differ between a story and its fork by construction."""


class Normalizer:
    """Replaces ids by first-seen placeholders (see the module docstring)."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def placeholder(self, value: str) -> str:
        return self.ids.setdefault(value, f"<id:{len(self.ids)}>")

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return ID_PATTERN.sub(lambda m: self.placeholder(m.group(0)), value)
        if isinstance(value, BaseModel):
            return self(value.model_dump(mode="json"))
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
        if isinstance(value, Mapping):
            return {self(k): self(v) for k, v in value.items()}
        if isinstance(value, Sequence | set | frozenset) and not isinstance(value, bytes):
            items = sorted(value, key=repr) if isinstance(value, set | frozenset) else value
            return [self(v) for v in items]
        return value


def history(events: Iterable[Any], normalizer: Normalizer | None = None) -> list[Any]:
    """``views.Event`` rows without their per-story fields, ids normalised."""
    normalize = normalizer or Normalizer()
    return [
        normalize(
            {
                f.name: getattr(event, f.name)
                for f in dataclasses.fields(event)
                if f.name not in EVENT_FIELDS_DROPPED
            }
        )
        for event in events
    ]


__all__ = ["EVENT_FIELDS_DROPPED", "ID_PATTERN", "Normalizer", "history"]
