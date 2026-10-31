"""U-14: owned-value declarations (FR-STATE-2; docs/04 §3). One file per test ID and milestone:
#185's ``test_u_14__declaration`` is here, and #204 adds its part (the per-turn read,
unavailability and write policies of externally owned values) to the same file."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from epistrel.core.enums import StateChangeSource, StateOwner
from epistrel.core.errors import InvalidRequest
from epistrel.core.states import StateDefinition, model

BALANCE: dict[str, Any] = {
    "name": "balance",
    "type": "number",
    "scope": "character",
    "constraints": {"non_negative": True},
    "initial_value": 100,
    "aliases": ["funds"],
    "unit": "credits",
}
"""One declaration, held by the story or by a provider."""


def _refused(document: dict[str, Any]) -> str:
    with pytest.raises(InvalidRequest) as refused:
        model.validate_definition(document)
    return str(refused.value.details["reason"])


@pytest.mark.part("M6", "FR-STATE-2")
def test_u_14__declaration() -> None:
    """One ``StateDefinition`` validates for both owners: the same declaration (type, scope,
    bounds, initial value, aliases, unit) held by the story or by a provider, and the bounds
    judge a value the same way whoever owns it. ``owner = external`` requires ``provider_id``
    and rejects ``rules``; a story-owned value names no provider; the name ``world_clock`` is
    refused to every declaration (``reserved_state``), the built-in's too unless it is the
    built-in."""
    story = model.validate_definition(BALANCE)
    external = model.validate_definition({**BALANCE, "owner": "external", "provider_id": "bank"})
    assert (story.owner, external.owner) == (StateOwner.STORY, StateOwner.EXTERNAL)
    shared = ("type", "scope", "constraints", "initial_value", "aliases", "unit", "visibility")
    assert {m: getattr(story, m) for m in shared} == {m: getattr(external, m) for m in shared}
    assert story.initial_value == external.initial_value == Decimal(100)
    for amount, broken in ((Decimal(-1), "non_negative"), (Decimal(5), None)):
        assert model.violation(story, amount) == model.violation(external, amount) == broken
    change = model.StateChange(
        "balance", "a fee", StateChangeSource.CONSUMER, value=None, delta=Decimal(-200)
    )
    for owned in (story, external):
        refused = model.check_change(owned, change, Decimal(-100), Decimal(100))
        assert refused is not None
        assert refused.constraint == "non_negative"

    assert _refused({**BALANCE, "owner": "external"}) == "required"
    assert (
        _refused({**BALANCE, "owner": "external", "provider_id": "bank", "rules": {"per": 1}})
        == "external_rules"
    )
    assert _refused({**BALANCE, "provider_id": "bank"}) == "story_owned"
    assert _refused({**BALANCE, "name": "world_clock"}) == "reserved_state"
    built_in = model.validate_definition(model.world_clock_definition(), builtin=True)
    assert built_in.name == model.WORLD_CLOCK
    # Only the built-in world_clock is of type clock (#193).
    assert _refused({"name": "elapsed", "type": "clock", "initial_value": 0}) == "reserved_state"
    with pytest.raises(InvalidRequest):
        model.validate_definition(
            {"name": "world_clock", "type": "number", "initial_value": 0}, builtin=True
        )
    # The shape rules every declaration shares, whoever owns it.
    assert _refused({"name": "mood", "type": "enum"}) == "required"
    assert (
        _refused(
            {
                "name": "mood",
                "type": "enum",
                "constraints": {"allowed_values": ["calm", "wary"]},
                "initial_value": "angry",
            }
        )
        == "allowed_values"
    )
    assert _refused({**BALANCE, "initial_value": -5}) == "non_negative"
    assert _refused({"name": "note", "type": "text", "initial_value": "x" * 201}) == "max_length"
    assert _refused({**BALANCE, "aliases": ["funds", "Funds"]}) == "duplicate"
    assert _refused({**BALANCE, "aliases": [" "]}) == "empty"
    assert _refused({**BALANCE, "constraints": {"min": 5, "max": 1}}) == "bounds"
    assert _refused({"name": "flag", "type": "boolean", "constraints": {"max": 1}}) == "not_number"
    flag = StateDefinition.model_validate({"name": "flag", "type": "boolean"})
    assert model.validate_definition(flag) == flag
