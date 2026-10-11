"""``transcript_messages``: one row per beat, following its operation's membership.

A beat is a ``player_input``, ``message_rendered``, ``think`` or ``latent`` event. Each becomes
one row keyed by the event id (``message_id``); #57's ``say`` and ``do`` stay one row. The row
carries its operation's membership, and ``truncate``, ``untruncate`` and ``swap`` flip the rows
of the operations they name, so no row is re-read from the log.

``role`` comes from (provenance, character): ``player`` for the player's input, ``narration``
for a consumer beat with no character, ``character`` otherwise (``actor_character_id`` is null
for a combined reply). ``channel`` is ``think``/``latent`` for those beats, ``narration`` for
narration, and ``message`` otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership, Provenance
from epistrel.core.events.types import (
    Latent,
    MessageRendered,
    PlayerInput,
    Swap,
    Think,
    Truncate,
    Untruncate,
)
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables import transcript_messages

Beat = PlayerInput | MessageRendered | Think | Latent

PRIVATE_CHANNELS: dict[type[Beat], str] = {Think: "think", Latent: "latent"}


def role_of(event: LogEvent) -> str:
    if event.provenance is Provenance.PLAYER:
        return "player"
    if event.provenance is Provenance.CONSUMER and event.character_id is None:
        return "narration"
    return "character"


def message_row(event: LogEvent, beat: Beat) -> Row:
    role = role_of(event)
    channel = PRIVATE_CHANNELS.get(type(beat)) or (
        "narration" if role == "narration" else "message"
    )
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
        "text": beat.text,
        "membership": event.membership.value,
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


class TranscriptProjection(Projection):
    table = transcript_messages

    def apply(self, rows: Rows, event: LogEvent) -> None:
        if isinstance(event.payload, PlayerInput | MessageRendered | Think | Latent):
            rows.put(message_row(event, event.payload))
            return
        for operation_ids, membership in membership_changes(event):
            _flip(rows, operation_ids, membership)

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        named = {op for e in events for ids, _ in membership_changes(e) for op in ids}
        if not named:
            return Rows(self.table)
        found = await conn.execute(
            select(transcript_messages).where(
                transcript_messages.c.story_id == story_id,
                transcript_messages.c.operation_id.in_(sorted(named)),
            )
        )
        return Rows(self.table, (dict(row._mapping) for row in found))


def _flip(rows: Rows, operation_ids: Iterable[str], membership: Membership) -> None:
    targets = set(operation_ids)
    for row in rows:
        if row["operation_id"] in targets and row["membership"] != membership.value:
            rows.put({**row, "membership": membership.value})
