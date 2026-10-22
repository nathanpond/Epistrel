"""``transcript_messages``: one row per beat, following its operation's membership.

A beat is a ``player_input``, ``message_rendered``, ``think`` or ``latent`` event, or a structured
turn's ``beat_say``/``beat_think``/``beat_do``/``beat_latent``/``beat_narration`` (#150), whose
channel is its kind. Each becomes
one row keyed by the event id (``message_id``); #57's ``say`` and ``do`` stay one row. The row
carries its operation's membership, and ``truncate``, ``untruncate`` and ``swap`` flip the rows
of the operations they name, so no row is re-read from the log. A regenerated turn (variant
> 1, #47) makes the rows of every other active variant at its index ``dormant``, as its commit
does to their operations, and every turn makes the still-``discarded`` rows at or after its index
``dormant`` (a rewound tail becomes a dormant continuation under the first new turn, #66).

``role`` comes from (provenance, character): ``player`` for a ``player_input`` (respond's or an
ingested one, #58), ``narration`` for a consumer or system beat with no character (an ingested
narration; a scenario's opening narration, turn 1 of a story created from a scenario, #119),
``character`` otherwise (``actor_character_id`` is null for a combined reply). ``channel`` is
``think``/``latent`` for those beats, ``narration`` for narration, and ``message`` otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership, OperationKind, Provenance
from epistrel.core.events.types import (
    BEAT_EVENT_PREFIX,
    Latent,
    MessageRendered,
    PlayerInput,
    StructuredBeat,
    Swap,
    Think,
    Truncate,
    Untruncate,
)
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables import transcript_messages

Beat = PlayerInput | MessageRendered | Think | Latent | StructuredBeat

PRIVATE_CHANNELS: dict[type[Beat], str] = {Think: "think", Latent: "latent"}


def channel_of(event: LogEvent, beat: Beat, role: str) -> str:
    """A row's channel: a structured beat's kind (``beat_say`` → ``say``, #150), ``think`` or
    ``latent`` for those shells, ``narration`` for narration, ``message`` otherwise."""
    if isinstance(beat, StructuredBeat):
        return event.type.removeprefix(BEAT_EVENT_PREFIX)
    return PRIVATE_CHANNELS.get(type(beat)) or ("narration" if role == "narration" else "message")


def role_of(event: LogEvent) -> str:
    if event.provenance is Provenance.PLAYER or isinstance(event.payload, PlayerInput):
        return "player"
    if event.provenance in (Provenance.CONSUMER, Provenance.SYSTEM) and event.character_id is None:
        return "narration"
    return "character"


def message_row(event: LogEvent, beat: Beat) -> Row:
    role = role_of(event)
    channel = channel_of(event, beat, role)
    return {
        "message_id": event.id,
        "story_id": event.story_id,
        "seq": event.seq,
        "operation_id": event.operation_id,
        "narrative_turn": event.narrative_turn,
        "turn_variant": event.turn_variant,
        "beat": event.beat,
        "kind": event.type,
        "channel": channel,
        "role": role,
        "actor_character_id": event.character_id,
        "render_character_ids": (
            list(beat.render_character_ids) if isinstance(beat, MessageRendered) else None
        ),
        "text": beat.text,
        "membership": event.membership.value,
        "redacted": event.redacted_at is not None,
    }


def membership_changes(event: LogEvent) -> list[tuple[list[str], Membership]]:
    """(operation ids, new membership) pairs a control event records; empty for anything else."""
    payload = event.payload
    if isinstance(payload, Truncate):
        return [(payload.operation_ids, Membership.DISCARDED)]
    if isinstance(payload, Untruncate):
        return [(payload.operation_ids, Membership.ACTIVE)]
    if isinstance(payload, Swap):
        return [
            (payload.dormant_operation_ids, Membership.DORMANT),
            (payload.activated_operation_ids, Membership.ACTIVE),
        ]
    return []


def regenerated_turn(event: LogEvent) -> int | None:
    """The turn index a regenerated variant replaces, or ``None`` (variant 1 is never one)."""
    if (
        event.kind is OperationKind.TURN
        and event.narrative_turn is not None
        and event.turn_variant is not None
        and event.turn_variant > 1
    ):
        return event.narrative_turn
    return None


def shelved_from(event: LogEvent) -> int | None:
    """The turn index from which a turn's commit makes ``discarded`` rows ``dormant``."""
    if event.kind is OperationKind.TURN and event.narrative_turn is not None:
        return event.narrative_turn
    return None


class TranscriptProjection(Projection):
    table = transcript_messages

    def apply(self, rows: Rows, event: LogEvent) -> None:
        turn = regenerated_turn(event)
        if turn is not None:
            _retire(rows, turn, event.turn_variant)
        shelved = shelved_from(event)
        if shelved is not None:
            _shelve(rows, shelved)
        if isinstance(
            event.payload, PlayerInput | MessageRendered | Think | Latent | StructuredBeat
        ):
            rows.put(message_row(event, event.payload))
            return
        for operation_ids, membership in membership_changes(event):
            _flip(rows, operation_ids, membership)

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        named = {op for e in events for ids, _ in membership_changes(e) for op in ids}
        turns = {turn for e in events if (turn := regenerated_turn(e)) is not None}
        shelved = [turn for e in events if (turn := shelved_from(e)) is not None]
        if not named and not turns and not shelved:
            return Rows(self.table)
        t = transcript_messages
        wanted: list[Any] = [
            t.c.operation_id.in_(sorted(named)),
            t.c.narrative_turn.in_(sorted(turns)),
        ]
        if shelved:
            wanted.append(
                and_(
                    t.c.narrative_turn >= min(shelved),
                    t.c.membership == Membership.DISCARDED.value,
                )
            )
        found = await conn.execute(select(t).where(t.c.story_id == story_id, or_(*wanted)))
        return Rows(self.table, (dict(row._mapping) for row in found))


def _retire(rows: Rows, turn: int, variant: int | None) -> None:
    for row in rows:
        if (
            row["narrative_turn"] == turn
            and row["turn_variant"] != variant
            and row["membership"] == Membership.ACTIVE.value
        ):
            rows.put({**row, "membership": Membership.DORMANT.value})


def _shelve(rows: Rows, turn: int) -> None:
    for row in rows:
        if (
            row["narrative_turn"] is not None
            and row["narrative_turn"] >= turn
            and row["membership"] == Membership.DISCARDED.value
        ):
            rows.put({**row, "membership": Membership.DORMANT.value})


def _flip(rows: Rows, operation_ids: Iterable[str], membership: Membership) -> None:
    targets = set(operation_ids)
    for row in rows:
        if row["operation_id"] in targets and row["membership"] != membership.value:
            rows.put({**row, "membership": membership.value})
