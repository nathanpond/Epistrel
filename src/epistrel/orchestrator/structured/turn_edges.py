"""A structured turn's dependency edges, derived in its commit (FR-DIRECT-7; #220).

Deterministic, no model call; written through ``core.edges.record_edges`` in the turn's
transaction, after canon, the utterances, routing and the dependences:

- **asserts**: each ``say`` utterance to each proposition it carries (a lie included: its
  target is the proposition, never a fact);
- **causes**: a ``think``/``do`` beat whose claim ``depend()`` consumed (#146) to the fact it
  resolved; a ``think`` beat to each fact it ``cites`` (#166); a ``state_changes`` row the turn
  wrote to each canon fact its effect's #189 preconditions read
  (``core.states.preconditions.reads_at``);
- **presupposes**: a narrator or ``do`` claim #142 verified ``supported`` without depending on
  it, to the facts its verdict rests on, or to the State an owner lookup read (#191's
  ``state_value``, #195/#200's clock predicates: ``target_kind = state``);
- **mentions**: an entity a beat names (#139's resolved mentions) that none of the beat's claims
  takes as an argument (subject, object or qualifier); the mention's own ``entity_mention``
  reading is not such a claim.

A claim both depended on and verified writes ``causes`` only. A Lite turn never comes here.
``derives`` edges are written where beliefs and consolidation outputs are
(``core.epistemic.transitions``, the consolidation job).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import edges
from epistrel.core.enums import DependencyEdgeType, DependencyTarget
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.states import preconditions
from epistrel.core.tables.events import events
from epistrel.core.tables.states import state_changes
from epistrel.core.verify.rules import VerdictKind

SAY, DO, THINK, NARRATION = "say", "do", "think", "narration"
ENTITY_MENTION = "entity_mention"
STATE_REF = "state:"


def _arguments(proposition: Proposition | None) -> set[str]:
    if proposition is None:
        return set()
    found = {str(proposition.subject_id)}
    if proposition.object_id is not None:
        found.add(str(proposition.object_id))
    found.update(str(x) for x in proposition.object_set or ())
    found.update(v for v in proposition.qualifiers.values() if isinstance(v, str))
    return found


def _key(proposition: Proposition) -> tuple[str, str]:
    return (str(proposition.subject_id), str(proposition.predicate_id))


async def turn_edges(
    conn: AsyncConnection,
    op: Any,
    *,
    turn: int,
    beats: Sequence[Any],
    checked: Sequence[Any],
    event_ids: Mapping[int, str],
    utterances: Sequence[tuple[str, int, Sequence[tuple[str, Proposition]]]],
    depended: Sequence[tuple[Any, Any]] = (),
    mentions: Mapping[int, Sequence[str]] | None = None,
) -> int:
    """Derive and write the turn's edges (see the module docstring); answers how many were
    offered. ``utterances``: each ``say`` utterance's id, beat and stored propositions;
    ``depended``: each (checked claim, its ``Dependence``); ``mentions``: each beat's resolved
    entity mentions (``beat_mentions``; else the beats' own)."""
    story_id = str(op.story_id)
    variant = int(op.turn_variant or 0)

    def at(beat: int | None) -> Position:
        return Position(turn, variant, 0, int(beat or 0), 0)

    found: list[edges.EdgeSpec] = []
    # asserts: a say utterance's propositions.
    for utterance_id, beat, props in utterances:
        event = event_ids.get(beat)
        for proposition_id, proposition in props:
            subject, predicate = _key(proposition)
            found.append(
                edges.EdgeSpec(
                    DependencyEdgeType.ASSERTS,
                    "utterances",
                    utterance_id,
                    DependencyTarget.PROPOSITION,
                    proposition_id,
                    target_subject_id=subject,
                    target_predicate_id=predicate,
                    position=at(beat),
                    source_event_ids=() if event is None else (event,),
                )
            )
    # causes: a depended claim's resolution.
    caused: set[tuple[int | None, str]] = set()
    for claim, dependence in depended:
        fact = getattr(dependence, "fact", None)
        beat = claim.claim.beat
        event = event_ids.get(beat) if beat is not None else None
        if fact is None or event is None or claim.claim.channel not in (THINK, DO):
            continue
        caused.add((beat, str(fact.id)))
        found += await edges.fact_edges(
            conn,
            story_id,
            DependencyEdgeType.CAUSES,
            "events",
            event,
            [str(fact.id)],
            position=at(beat),
            source_event_ids=(event,),
        )
    # causes: a think beat's citations of canon facts.
    for beat in beats:
        if beat.kind != THINK or not beat.cites:
            continue
        event = event_ids.get(beat.beat)
        if event is None:
            continue
        for edge in await edges.fact_edges(
            conn,
            story_id,
            DependencyEdgeType.CAUSES,
            "events",
            event,
            [str(c) for c in beat.cites],
            position=at(beat.beat),
            source_event_ids=(event,),
        ):
            caused.add((beat.beat, edge.target_id))
            found.append(edge)
    # presupposes: a supported narrator or do claim it did not depend on.
    for claim in checked:
        result = claim.result
        detected = claim.claim
        if result is None or result.kind is not VerdictKind.SUPPORTED:
            continue
        if detected.channel not in (NARRATION, DO) and detected.speaker != NARRATOR:
            continue
        event = event_ids.get(detected.beat) if detected.beat is not None else None
        if event is None:
            continue
        evidence = tuple(getattr(result.verdict, "evidence", ()) or ())
        facts = [
            str(e.fact_id)
            for e in evidence
            if e.fact_id is not None and (detected.beat, str(e.fact_id)) not in caused
        ]
        found += await edges.fact_edges(
            conn,
            story_id,
            DependencyEdgeType.PRESUPPOSES,
            "events",
            event,
            facts,
            position=at(detected.beat),
            source_event_ids=(event,),
        )
        for e in evidence:
            owner = getattr(e, "owner_ref", None)
            if isinstance(owner, str) and owner.startswith(STATE_REF):
                state_id = owner.removeprefix(STATE_REF).split(":", 1)[0]
                proposition = detected.proposition
                found.append(
                    edges.EdgeSpec(
                        DependencyEdgeType.PRESUPPOSES,
                        "events",
                        event,
                        DependencyTarget.STATE,
                        state_id,
                        target_subject_id=None
                        if proposition is None
                        else str(proposition.subject_id),
                        position=at(detected.beat),
                        source_event_ids=(event,),
                    )
                )
    # mentions: an entity named in a beat but in none of its claims.
    claimed: dict[int, set[str]] = {}
    for claim in checked:
        # An ``entity_mention`` claim is the mention itself (#139's presence reading), not a
        # claim about the entity.
        if claim.claim.beat is not None and claim.claim.category != ENTITY_MENTION:
            claimed.setdefault(int(claim.claim.beat), set()).update(
                _arguments(claim.claim.proposition)
            )
    named_by: dict[int, list[str]] = {
        int(beat.beat): [
            str(m.get("entity_id"))
            for m in beat.mentions or ()
            if isinstance(m, Mapping) and m.get("entity_id")
        ]
        for beat in beats
    }
    if mentions is not None:
        named_by = {int(b): list(m) for b, m in mentions.items()}
    for number, named in sorted(named_by.items()):
        event = event_ids.get(number)
        if event is None or number <= 1:
            continue  # the player's input (beat 1) is not a beat the turn narrated
        for entity in dict.fromkeys(named):
            if entity in claimed.get(number, set()):
                continue
            found.append(
                edges.EdgeSpec(
                    DependencyEdgeType.MENTIONS,
                    "events",
                    event,
                    DependencyTarget.ENTITY,
                    entity,
                    position=at(number),
                    source_event_ids=(event,),
                )
            )
    # causes: a State change and the canon facts its preconditions read.
    s = state_changes.c
    for row in await conn.execute(
        select(s.id, s.beat).where(s.story_id == story_id, s.operation_id == op.operation_id)
    ):
        read = preconditions.reads_at(None if row.beat is None else int(row.beat))
        if read:
            found += await edges.fact_edges(
                conn,
                story_id,
                DependencyEdgeType.CAUSES,
                "state_changes",
                str(row.id),
                sorted(read),
                position=at(row.beat),
            )
    return await edges.record_edges(conn, op, found)


async def beat_mentions(conn: AsyncConnection, operation_id: str) -> dict[int, list[str]]:
    """Each beat event's resolved mentions (#139, persisted by #150), by beat."""
    e = events.c
    found: dict[int, list[str]] = {}
    for row in await conn.execute(
        select(e.beat, e.payload).where(e.operation_id == operation_id, e.type.like("beat_%"))
    ):
        payload = dict(row.payload or {})
        found[int(row.beat or 0)] = [
            str(m.get("entity_id"))
            for m in payload.get("mentions") or ()
            if isinstance(m, Mapping) and m.get("entity_id")
        ]
    return found


def stored_props(
    claims: Iterable[Any], proposition_ids: Sequence[str]
) -> list[tuple[str, Proposition]]:
    """An utterance's (stored proposition id, proposition) pairs, in claim order."""
    asserted = [c.proposition for c in claims if getattr(c, "proposition", None) is not None]
    return list(zip(proposition_ids, asserted, strict=False))


__all__ = ["beat_mentions", "stored_props", "turn_edges"]
