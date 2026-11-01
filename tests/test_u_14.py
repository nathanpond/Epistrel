"""U-14: owned-value declarations (FR-STATE-2; docs/04 §3). One file per test ID and milestone:
#185's ``test_u_14__declaration`` is here, and #204 adds its part (the per-turn read,
unavailability and write policies of externally owned values) to the same file."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import providers, states
from epistrel.core.enums import OperationKind, StateChangeSource, StateOwner
from epistrel.core.errors import InvalidRequest, ProviderUnbound
from epistrel.core.states import StateDefinition, model
from epistrel.orchestrator.providers.bindings import require_bound
from tests import canon_support as c
from tests import provider_support as ps
from tests import state_support as ss

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


@pytest.mark.integration
@pytest.mark.part("M6", "FR-STATE-2")
async def test_u_14__owner(engine: AsyncEngine, states_delivered: None) -> None:
    """Story-owned and externally owned values share the one declaration path
    (``define_state``): an external one names a registered provider whose manifest owns the
    value (its ``external_value_name``, by default the State's name) with the same type. The
    per-turn read, unavailability and write policies hang on ``owner = external`` only: the
    providers a turn needs bound are those its external States name.

    Complement asserted: a story-owned State is declared, changed and read with no provider
    call, and a story with only story-owned States needs no binding; an unregistered provider,
    a value the manifest lacks and a type mismatch are refused, and nothing is declared."""
    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await ss.set_level(engine, story, "prompt_only")
    bank = ps.unique_name("bank")
    await ps.register(engine, ps.in_process_spec(bank, {"balance": "number", "vip": "boolean"}))
    fake = ps.ScriptedProvider(bank)

    # Story-owned: declared, changed and read, never a provider call; no binding needed.
    await ss.define(engine, story, ss.gold())
    await ss.change(
        engine,
        story,
        ss.delta("gold", 7, scope_key=cal, source=StateChangeSource.CONSUMER),
        kind=OperationKind.CONTROL,
    )
    assert await ss.value(engine, story, "gold", cal) == 7
    async with engine.connect() as conn:
        assert await providers.needed(conn, story) == []
    await require_bound(engine, story)
    assert fake.calls == 0

    # Externally owned through the same path: the State's name, or another provider value.
    balance = await ss.define(
        engine, story, ss.gold(name="balance", owner="external", provider_id=bank)
    )
    member = await ss.define(
        engine,
        story,
        StateDefinition.model_validate(
            {
                "name": "member",
                "type": "boolean",
                "owner": "external",
                "provider_id": bank,
                "external_value_name": "vip",
            }
        ),
    )
    with pytest.raises(InvalidRequest) as no_value:
        await ss.define(engine, story, ss.gold(name="gold2", owner="external", provider_id=bank))
    assert no_value.value.details["reason"] == "unknown_value"
    async with engine.connect() as conn:
        stored = {d.name: d for d in await states.definitions(conn, story)}
        assert await providers.needed(conn, story) == [bank]
    assert (balance.external_value_name, member.external_value_name) == ("balance", "vip")
    assert stored["balance"].provider_id == bank
    assert stored["gold"].external_value_name is None
    with pytest.raises(ProviderUnbound):
        await require_bound(engine, story)
    await ps.bind(engine, story, bank, {})
    await require_bound(engine, story)

    refusals = {
        "unknown_provider": ss.gold(
            name="debt", owner="external", provider_id=ps.unique_name("none")
        ),
        "type_mismatch": StateDefinition.model_validate(
            {
                "name": "rank",
                "type": "text",
                "owner": "external",
                "provider_id": bank,
                "external_value_name": "balance",
            }
        ),
    }
    for reason, definition in refusals.items():
        with pytest.raises(InvalidRequest) as refused:
            await ss.define(engine, story, definition)
        assert refused.value.details["reason"] == reason
    assert (
        _refused({**BALANCE, "external_value_name": "gold"}) == "story_owned"
    )  # a story-owned value names no provider value
    async with engine.connect() as conn:
        names = [d.name for d in await states.definitions(conn, story) if d.name != "world_clock"]
    assert names == ["balance", "gold", "member"]
    assert fake.calls == 0
    await ss.set_level(engine, story, None)
