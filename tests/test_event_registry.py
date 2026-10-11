"""The event payload registry (no database)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from epistrel.core.enums import EventCategory
from epistrel.core.events import registry
from epistrel.core.events.registry import (
    EventPayload,
    InvalidEventPayload,
    UnregisteredEventType,
    canonical_json,
    lookup,
    register,
    validate,
)
from epistrel.core.events.types import MessageRendered, PlayerInput
from epistrel.core.ids import CROCKFORD, ULID_LENGTH, new_ulid

EXPECTED_M2_TYPES = {
    "story_created": "control",
    "story_archived": "control",
    "story_unarchived": "control",
    "story_deleted": "audit",
    "character_defined": "control",
    "player_input": "input",
    "message_rendered": "narrative_state",
    "truncate": "control",
    "untruncate": "control",
    "swap": "control",
    "fork": "control",
    "pinned_memories_set": "control",
    "custom_instructions_set": "control",
    "config_changed": "control",
    "guardrail_output_rejected": "audit",
    "lite_summary_segment": "derived",
    "think": "narrative_state",
    "latent": "narrative_state",
}


@pytest.fixture
def scratch_type() -> Iterator[str]:
    name = "test_scratch_type"
    yield name
    registry._REGISTRY.pop(name, None)


def test_every_m2_type_is_registered_with_its_category() -> None:
    found = {name: t.category.value for name, t in registry.registered_types().items()}
    assert {name: found.get(name) for name in EXPECTED_M2_TYPES} == EXPECTED_M2_TYPES


def test_duplicate_registration_raises(scratch_type: str) -> None:
    @register(scratch_type, category=EventCategory.AUDIT)
    class First(EventPayload):
        value: int

    with pytest.raises(ValueError, match="already registered"):

        @register(scratch_type, category=EventCategory.AUDIT)
        class Second(EventPayload):
            value: int

    assert lookup(scratch_type).model is First


def test_unknown_type_raises() -> None:
    with pytest.raises(UnregisteredEventType) as caught:
        lookup("nope")
    assert caught.value.details == {"type": "nope"}


def test_payloads_are_frozen_and_closed() -> None:
    payload = validate("player_input", {"text": "hi"})
    assert payload == PlayerInput(text="hi")
    with pytest.raises(ValidationError):
        payload.text = "changed"  # type: ignore[misc]
    with pytest.raises(InvalidEventPayload) as caught:
        validate("player_input", {"text": "hi", "extra": 1})
    assert caught.value.details == {"type": "player_input", "fields": ["extra"]}
    assert caught.value.code == "validation_error"


def test_model_instances_must_match_their_type() -> None:
    assert validate("player_input", PlayerInput(text="a")) == PlayerInput(text="a")
    with pytest.raises(InvalidEventPayload, match="must be PlayerInput"):
        validate("player_input", MessageRendered(text="a"))


def test_canonical_json_sorts_keys() -> None:
    payload = validate("config_changed", {"setting": "s", "new": {"b": 1, "a": 2}, "old": None})
    assert canonical_json(payload) == '{"new":{"a":2,"b":1},"old":null,"setting":"s"}'


def test_ulids_are_26_crockford_chars_and_time_ordered() -> None:
    first, second = new_ulid(), new_ulid()
    assert len(first) == ULID_LENGTH
    assert set(first) <= set(CROCKFORD)
    assert first[:10] <= second[:10]
