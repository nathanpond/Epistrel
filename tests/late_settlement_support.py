"""Helpers for the late-settlement tests (#217): a structured story whose gold an asynchronous,
write-capable in-process provider owns (``bank``) and whose ``sword`` is story-owned inventory;
a purchase played through ``respond`` (a conditional sword grant waiting on the payment's outbox
row, #216), or written directly as a turn's intent (``intent``: the outbox row and its
contingent rows, for the variants that need a shape a render cannot give cheaply); plain turns;
the dispatcher's answer (the outcome record); and the settlement run in the test's own loop
(``settle``) rather than by a worker."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import outbox as core_outbox
from epistrel.core import states, timeline
from epistrel.core.enums import (
    OperationKind,
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
)
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.propositions import store
from epistrel.core.states import StateDefinition
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.event_support import execute_in_tx, rows
from tests.projection_support import CONTROL_PRINCIPAL
from tests.respond_support import body, respond
from tests.structured_turn_support import beat

PRICE = 500
WRAPS = "Mira wraps the sword in oilcloth."


def sword_state(entity: str, **fields: Any) -> StateDefinition:
    """The per-character ``sword`` State: tracks the sword entity, transferable against
    ``gold``, never negative."""
    return StateDefinition.model_validate(
        {
            "name": "sword",
            "type": "number",
            "scope": "character",
            "constraints": {"non_negative": True},
            "initial_value": 0,
            "aliases": ("blade",),
            "entity_id": entity,
            "transfer": {"counterparties": ["gold", "sword"]},
            "authorization": {"sources": ["any_character", "director", "consumer"]},
            **fields,
        }
    )


async def bank(
    engine: AsyncEngine, epistrel: Any, story: str, name: str | None = None
) -> ps.ScriptedProvider:
    """``story``'s gold, owned by a fresh write-capable asynchronous in-process provider
    (``name``: a fixed name, on a database of the caller's own)."""
    name = name or ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name))
    fake = ps.ScriptedProvider(name, default=1000).attach(epistrel)
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return fake


async def swords(engine: AsyncEngine, story: str, *holders: str, **fields: Any) -> str:
    """The sword entity and the ``sword`` State; each of ``holders`` holds one; answers the
    entity."""
    async with engine.begin() as conn:
        sword = await store.add_entity(conn, story, "item", "sword")
    await ss.define_all(engine, story, sword_state(sword, **fields))
    for holder in holders:
        await ss.change(
            engine,
            story,
            ss.set_to(
                "sword", 1, scope_key=holder, reason="stock", source=StateChangeSource.CONSUMER
            ),
        )
    return sword


def trade(owner: str, delta: int, **extra: Any) -> dict[str, Any]:
    """``owner``'s sword by ``delta`` against ``owner``'s external gold."""
    return {
        "kind": "transfer",
        "state": "sword",
        "scope_key": owner,
        "delta": delta,
        "counterparty": {"state": "gold", "scope_key": owner, "delta": -delta * PRICE},
        "reason": "the sword",
        **extra,
    }


async def buy(client: Any, stub: Any, story: str, cal: str) -> dict[str, Any]:
    """Cal buys the sword through ``respond`` (Mira's render): the payment's outbox row and the
    pending grant. Answers the response."""
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(beat("do", WRAPS, effects=[trade(cal, 1)])))
    es.narrator(stub, es.render(beat("narration", "The forge glows.")))
    answer = await respond(client, story, body(cal, None, do="buys the sword"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def payment_of(played: dict[str, Any]) -> str:
    (payment,) = [e for e in played["state_effects"] if e["state"] == "gold"]
    return str(payment["outbox_id"])


async def plain(
    client: Any, stub: Any, story: str, cal: str, text: str = "Rain taps on the shutters."
) -> dict[str, Any]:
    """A turn through ``respond`` that changes no State."""
    stub.reset()
    es.narrator(stub, es.render(beat("narration", text)))
    ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
    answer = await respond(client, story, body(cal, None, do="waits"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


@dataclass(frozen=True, slots=True)
class Side:
    """One contingent row an ``intent`` turn writes: ``scope_key``'s sword by ``delta``."""

    scope_key: str
    delta: int
    contingency: StateContingency
    kind: StateChangeKind = StateChangeKind.DELTA


async def intent(
    engine: AsyncEngine, story: str, provider: str, payer: str, sides: Sequence[Side]
) -> tuple[str, Operation]:
    """A turn's intent written directly (#210's outbox row for ``payer``'s gold -500, #216's
    contingent ``sides`` waiting on it): answers the outbox row's id and the turn's
    operation."""
    outbox_id = new_ulid()

    async def write(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await core_outbox.write_intended(
            conn,
            op,
            [
                core_outbox.Intended(
                    id=outbox_id,
                    provider_name=provider,
                    value_name="gold",
                    scope_key=payer,
                    effect={"kind": "change", "state": "gold", "delta": -PRICE, "reason": "pay"},
                    beat=2,
                )
            ],
        )
        for side in sides:
            status = (
                StateChangeStatus.HELD
                if side.contingency is StateContingency.RESERVATION
                else StateChangeStatus.PENDING
            )
            found = await states.apply_change(
                conn,
                op,
                states.StateChange(
                    state="sword",
                    reason="the sword",
                    source=StateChangeSource.TURN,
                    kind=side.kind,
                    delta=Decimal(side.delta),
                    scope_key=side.scope_key,
                    position=Position(op.narrative_turn, op.turn_variant or 0, 0, 2),
                    contingency=side.contingency,
                    status=status,
                    prerequisite_effect_ids=(outbox_id,),
                ),
            )
            assert isinstance(found, states.Applied), found

    op, _ = await c.run(engine, story, write, said="buy the sword")
    return outbox_id, op


async def turn(engine: AsyncEngine, story: str, said: str = "wait") -> Operation:
    """A plain turn written directly."""

    async def write(conn: AsyncConnection, op: Operation) -> None:
        return None

    op, _ = await c.run(engine, story, write, said=said)
    return op


async def sell(engine: AsyncEngine, story: str, holder: str) -> None:
    """A turn in which ``holder``'s sword is sold off (``-1``, committed): "no sword left"."""
    await ss.change(
        engine,
        story,
        ss.delta("sword", -1, scope_key=holder, reason="sold to another buyer"),
    )


async def answer(engine: AsyncEngine, epistrel: Any, outbox_id: str) -> str:
    """The dispatcher claims and sends the row (the fake answers as scripted); answers the
    outcome record's id."""
    await outbox_job.dispatch(engine, outbox_id, registry=epistrel.in_process_providers)
    return await last_outcome(engine, outbox_id)


async def last_outcome(engine: AsyncEngine, outbox_id: str) -> str:
    (row,) = await rows(
        engine,
        "SELECT id FROM outbox_outcomes WHERE outbox_id = :i ORDER BY received_at DESC, id DESC "
        "LIMIT 1",
        i=outbox_id,
    )
    return str(row.id)


async def settle(engine: AsyncEngine, epistrel: Any, outcome_id: str) -> settlement.Settlement:
    return await settlement.settle(engine, outcome_id, registry=epistrel.in_process_providers)


async def sword_of(
    engine: AsyncEngine, story: str, holder: str, turn: int | None = None, slot: int = 0
) -> Any:
    """``holder``'s swords through checkpoint ``turn`` (``N⁺slot``), or now."""
    if turn is None:
        return await ss.value(engine, story, "sword", holder)
    return await ss.value(engine, story, "sword", holder, through=ss.at(turn, slot))


async def listed(engine: AsyncEngine, story: str) -> set[tuple[str, str]]:
    return {(e.outbox_id, e.reason) for e in (await core_outbox.listings(engine, story)).listings}


async def settle_jobs(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``outcome_settlement`` jobs."""
    return await rows(
        engine,
        "SELECT id, status, payload, due_at FROM jobs WHERE story_id = :s "
        "AND kind = :k ORDER BY created_at, id",
        s=story,
        k=settlement.SETTLE,
    )


async def settlement_rows(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``state_changes`` rows a settlement wrote, with their positions."""
    return await rows(
        engine,
        "SELECT sc.*, sc.membership::text AS member FROM state_changes sc "
        "WHERE sc.story_id = :s AND sc.source = 'settlement' ORDER BY sc.id",
        s=story,
    )


async def drop_jobs(engine: AsyncEngine, story: str) -> None:
    """Delete the story's outbox and settlement jobs (the database is shared)."""
    await ps.drop_outbox_jobs(engine, story)
    await execute_in_tx(
        engine,
        (
            "DELETE FROM jobs WHERE story_id = :s AND kind = :k",
            {"s": story, "k": settlement.SETTLE},
        ),
    )


async def set_stage(engine: AsyncEngine, operation_id: str, stage: str) -> None:
    """Move a turn's ``turn_stage`` (#212): an incomplete staged turn, or its completion."""
    await execute_in_tx(
        engine,
        (
            "UPDATE operations SET turn_stage = CAST(:t AS turn_stage) WHERE operation_id = :o",
            {"t": stage, "o": operation_id},
        ),
    )


__all__ = [
    "PRICE",
    "WRAPS",
    "OperationKind",
    "Side",
    "answer",
    "bank",
    "buy",
    "drop_jobs",
    "intent",
    "last_outcome",
    "listed",
    "payment_of",
    "plain",
    "sell",
    "set_stage",
    "settle",
    "settle_jobs",
    "settlement_rows",
    "sword_of",
    "sword_state",
    "swords",
    "trade",
    "turn",
]


async def discarded(
    engine: AsyncEngine, epistrel: Any, story: str, provider: str, payer: str, seller: str
) -> str:
    """S-LATE-SETTLEMENT's discarded origin: ``payer`` buys ``seller``'s sword (claimed), the
    player rewinds before the purchase and plays a new turn, then the confirmation arrives →
    listed ``confirmed_discarded_origin``, nothing granted. Answers the outbox row's id."""
    sale = [
        Side(seller, -1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_OUT),
        Side(payer, 1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_IN),
    ]
    found, origin = await intent(engine, story, provider, payer, sale)
    assert origin.narrative_turn is not None
    claimed = await outbox_job.claim(engine, found, registry=epistrel.in_process_providers)
    assert isinstance(claimed, outbox_job.Claimed)
    await timeline.truncate(
        engine,
        story,
        timeline.TruncateRequest(to_turn=origin.narrative_turn - 1),
        principal=CONTROL_PRINCIPAL,
    )
    await turn(engine, story, "do something else")
    assert await outbox_job.send(engine, claimed) == "confirmed"
    settled = await settle(engine, epistrel, await last_outcome(engine, found))
    assert settled.result == settlement.DISCARDED
    assert (found, "confirmed_discarded_origin") in await listed(engine, story)
    return found
