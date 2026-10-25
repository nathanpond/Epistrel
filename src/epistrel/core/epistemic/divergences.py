"""Divergence records: a lie's asserted proposition against the speaker's contrary belief
(FR-TURN-7, FR-VERIFY-10; docs/02 §4.9; #166).

``record_divergences`` writes one ``divergences`` row per lie claim in the turn's commit
transaction (a commit step): the utterance, the speaker, the asserted proposition, the contrary
belief and its proposition (``None`` for a lie about something no record settles), the say
beat's position, and, for a lie that attributes a belief to someone, the attribution's objective
relation (a label, FR-BELIEF-15; #545). The erasure guard runs over the speaker first, and
each row is indexed with its declared subjects (the speaker and both propositions' subject and
object characters). A row is a derived record of the turn (it follows truncation, swap,
regenerate and fork) and persists: it is queryable for as long as the turn is in the timeline
(``divergences_of``). #169's reveal sets ``revealed_by_event_id``.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.epistemic.partitions import BeliefOperation
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import divergence_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, active_only, insert_derived
from epistrel.core.tables import entities, propositions
from epistrel.core.tables.divergences import divergences

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Divergence:
    """One lie claim to record: the utterance and speaker, the asserted proposition's id, the
    contrary belief (and its proposition's id) when the speaker holds one, and the position."""

    utterance_id: str
    speaker: str
    asserted_proposition_id: str
    position: Position
    believed_belief_id: str | None = None
    believed_proposition_id: str | None = None
    source_event_id: str | None = None
    attribution_objective_relation: str | None = None
    """#545: for an attribution lie, whether the attributed holder actually believes it (a
    label, FR-BELIEF-15); ``None`` for any other lie."""


@dataclass(frozen=True, slots=True)
class DivergenceRow:
    """A stored divergence (``divergences_of``)."""

    id: str
    utterance_id: str
    speaker: str
    asserted_proposition_id: str
    believed_proposition_id: str | None
    believed_belief_id: str | None
    position: Position
    revealed_by_event_id: str | None
    operation_id: str
    attribution_objective_relation: str | None = None


async def _characters_of(
    conn: AsyncConnection, story_id: str, proposition_id: str | None
) -> tuple[str | None, str | None]:
    """The characters a proposition's subject and object are (their entities'), if any."""
    if proposition_id is None:
        return None, None
    p = propositions.c
    subject = entities.alias("subject_entity")
    obj = entities.alias("object_entity")
    row = (
        await conn.execute(
            select(subject.c.character_id, obj.c.character_id.label("object_character_id"))
            .select_from(
                propositions.outerjoin(subject, subject.c.id == p.subject_id).outerjoin(
                    obj, obj.c.id == p.object_id
                )
            )
            .where(p.story_id == story_id, p.id == proposition_id)
        )
    ).one_or_none()
    if row is None:
        return None, None
    return (
        None if row.character_id is None else str(row.character_id),
        None if row.object_character_id is None else str(row.object_character_id),
    )


async def record_divergences(
    conn: AsyncConnection, op: BeliefOperation, items: Sequence[Divergence]
) -> list[str]:
    """Write ``items`` in ``op``'s commit transaction (see the module docstring); answers the
    ids."""
    story_id = op.story_id
    if story_id is None:
        raise InvalidRequest("a divergence belongs to a story", {"field": "operation"})
    if not items:
        return []
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((), frozenset({d.speaker})) for d in items],
        base_revision=None,
        path=__name__,
    )
    rows: list[dict[str, Any]] = []
    records = []
    for item in items:
        divergence_id = new_ulid()
        rows.append(
            {
                "id": divergence_id,
                "story_id": story_id,
                "utterance_id": item.utterance_id,
                "speaker_character_id": item.speaker,
                "asserted_proposition_id": item.asserted_proposition_id,
                "believed_proposition_id": item.believed_proposition_id,
                "believed_belief_id": item.believed_belief_id,
                "attribution_objective_relation": item.attribution_objective_relation,
                **item.position.columns("position"),
                "operation_id": op.operation_id,
                "source_event_ids": [] if item.source_event_id is None else [item.source_event_id],
                "narrative_turn": op.narrative_turn,
                "turn_variant": op.turn_variant,
            }
        )
        asserted = await _characters_of(conn, story_id, item.asserted_proposition_id)
        believed = await _characters_of(conn, story_id, item.believed_proposition_id)
        records.append(
            divergence_record(
                SimpleNamespace(
                    id=divergence_id,
                    story_id=story_id,
                    speaker_character_id=item.speaker,
                    asserted_subject_character_id=asserted[0],
                    asserted_object_character_id=asserted[1],
                    believed_subject_character_id=believed[0],
                    believed_object_character_id=believed[1],
                )
            )
        )
    await insert_derived(conn, divergences, rows)
    await index_records(conn, records)
    log.debug("divergences written: %d", len(rows))
    return [str(r["id"]) for r in rows]


async def divergences_of(
    conn: AsyncConnection, story_id: str, *, speaker: str | None = None
) -> list[DivergenceRow]:
    """The story's divergences on the active timeline (``speaker``'s only when given), oldest
    first."""
    d = divergences.c
    query = select(divergences).where(d.story_id == story_id, active_only(divergences))
    if speaker is not None:
        query = query.where(d.speaker_character_id == speaker)
    found = await conn.execute(query.order_by(d.id))
    return [
        DivergenceRow(
            id=str(r.id),
            utterance_id=str(r.utterance_id),
            speaker=str(r.speaker_character_id),
            asserted_proposition_id=str(r.asserted_proposition_id),
            believed_proposition_id=None
            if r.believed_proposition_id is None
            else str(r.believed_proposition_id),
            believed_belief_id=None if r.believed_belief_id is None else str(r.believed_belief_id),
            position=Position.of_columns(dict(r._mapping), "position") or Position(0),
            revealed_by_event_id=None
            if r.revealed_by_event_id is None
            else str(r.revealed_by_event_id),
            operation_id=str(r.operation_id),
            attribution_objective_relation=r.attribution_objective_relation,
        )
        for r in found
    ]


__all__ = ["Divergence", "DivergenceRow", "divergences_of", "record_divergences"]
