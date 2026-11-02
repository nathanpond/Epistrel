"""A backfill's ordered reconciliation (FR-PROF-9; #228).

The extraction stage stored, per chunk, each turn's messages with what was read from them
(``Extracted``: the propositions the claims normalized to, whether a claim narrated a change,
and the characters present). Reconciliation walks the turns in narrative order and writes,
through the live writers and under the backfill operation at each turn's own position
(``Position``), with provenance ``backfill``:

- **facts** (the structured pipeline or verification gained): ``canon.commit_fact`` per
  proposition, citing its message's event. A functional key's value that differs from the one
  the backfill last wrote for it, with no narrated change (the claim's ``time``), is a
  **contradiction**: under the default policy ``last_mention`` the later statement is canon for
  the overlap (``commit_fact`` closes the earlier version), and a ``backfill_conflicts`` row
  records both statements, ``resolved``, ``{policy: last_mention}`` (#230 adds the others);
- **episodic memories** (the structured pipeline gained, with ``episodic`` on in the target
  configuration): ``memory.records.write_episodic``, one per observable message, trust tier
  ``backfill`` (the lowest);
- **exposures** (``partitions`` gained): each character present at a ``say`` other than its
  speaker is exposed to its propositions (``epistemic.exposure.record_exposures``), flagged
  ``inferred``; unresolved, so a later turn resolves them lazily as it does live ones.

Every write of one turn runs in a savepoint: a record naming a subject erased from the story is
refused by the erasure guard, the turn's records are skipped, and the skip is counted
(``report.skipped_erased``). Nothing is regenerated, and no private thought is invented: only
what the transcript says is read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.canon import facts as canon
from epistrel.core.enums import ExposureFidelity, RecordProvenance
from epistrel.core.epistemic.exposure import Exposed, record_exposures
from epistrel.core.errors import ErasedSubjectReferenced
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.memory import records as memories
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Cardinality, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.tables import backfill_conflicts, facts, transcript_messages

FACT_CAPABILITIES = frozenset({"pipeline", "verification"})
"""Gained capabilities whose data is canon facts."""
MEMORY_CAPABILITIES = frozenset({"pipeline"})
"""The structured pipeline gained: episodic memories, when the target configuration has the
``episodic`` module on (a behavioral module, so never in coverage itself; the writer checks)."""
EXPOSURE_CAPABILITIES = frozenset({"partitions"})
LAST_MENTION = "last_mention"


class ReadProposition(BaseModel):
    """One proposition a claim normalized to, and whether its claim narrated a change."""

    proposition: dict[str, Any]
    changed: bool = False


class ReadMessage(BaseModel):
    """One transcript message as extraction read it. Its ``text`` is never stored in the
    checkpoint: reconciliation reads it from the transcript (so an erasure's redaction holds)."""

    event_id: str
    kind: str
    speaker: str | None = None
    text: str = ""
    present: list[str] = Field(default_factory=list)
    propositions: list[ReadProposition] = Field(default_factory=list)


class ReadTurn(BaseModel):
    turn: int
    variant: int = 0
    messages: list[ReadMessage] = Field(default_factory=list)


class Extracted(BaseModel):
    """A chunk's extraction checkpoint (``backfill_chunks.extraction``)."""

    turns: list[ReadTurn] = Field(default_factory=list)


@dataclass
class Tally:
    """What one reconciliation span wrote, for the backfill's report."""

    facts: int = 0
    memories: int = 0
    exposures: int = 0
    conflicts: int = 0
    skipped_erased: int = 0
    by_turn: dict[int, int] = field(default_factory=dict)


def _shim(op_id: str, story_id: str, principal: str, turn: ReadTurn) -> Any:
    return SimpleNamespace(
        operation_id=op_id,
        story_id=story_id,
        principal=principal,
        narrative_turn=turn.turn,
        turn_variant=turn.variant,
        base_revision=None,
    )


def _functional(registry: Registry, predicate_id: str) -> bool:
    found = registry.get(predicate_id)
    return found is not None and found.cardinality is Cardinality.FUNCTIONAL


async def _previous(
    conn: AsyncConnection, story_id: str, op_id: str, proposition: Proposition
) -> Any:
    """The version the backfill last wrote for ``proposition``'s key, still current."""
    f = facts.c
    return (
        await conn.execute(
            select(f.id, f.proposition_id, f.narrative_turn, f.source_event_ids)
            .where(
                f.story_id == story_id,
                f.operation_id == op_id,
                f.subject_id == proposition.subject_id,
                f.predicate_id == proposition.predicate_id,
                f.tx_to.is_(None),
                f.valid_to.is_(None),  # the value that holds now, not an earlier remainder
            )
            .order_by(f.narrative_turn.desc(), f.id.desc())
            .limit(1)
        )
    ).one_or_none()


async def _conflict(
    conn: AsyncConnection,
    backfill_id: str,
    story_id: str,
    proposition: Proposition,
    earlier: Any,
    later: tuple[int, str],
) -> None:
    turn, event_id = later
    statements = [
        {
            "fact_id": str(earlier.id),
            "position": earlier.narrative_turn,
            "event_id": (list(earlier.source_event_ids or []) or [None])[0],
            "source": "transcript",
        },
        {"position": turn, "event_id": event_id, "source": "transcript"},
    ]
    await conn.execute(
        insert(backfill_conflicts).values(
            id=new_ulid(),
            backfill_id=backfill_id,
            story_id=story_id,
            subject={"entity_id": proposition.subject_id, "predicate": proposition.predicate_id},
            statements=statements,
            status="resolved",
            resolution={"policy": LAST_MENTION, "canon": "latest"},
            resolved_by="system:backfill",
            resolved_at=clock.now(),
        )
    )


async def _turn(
    conn: AsyncConnection,
    row: Any,
    turn: ReadTurn,
    *,
    capabilities: frozenset[str],
    registry: Registry,
    settings: ProfileSettings,
    tally: Tally,
) -> None:
    story_id, op_id = str(row.story_id), str(row.operation_id)
    op = _shim(op_id, story_id, str(row.requested_by), turn)
    beats: list[memories.Beat] = []
    exposed: list[Exposed] = []
    for index, message in enumerate(turn.messages):
        written: list[tuple[str, Proposition]] = []
        if capabilities & FACT_CAPABILITIES:
            for read in message.propositions:
                proposition = Proposition.model_validate(read.proposition)
                earlier = await _previous(conn, story_id, op_id, proposition)
                if (
                    earlier is not None
                    and not read.changed
                    and _functional(registry, proposition.predicate_id)
                ):
                    stored, _ = await canon._load(conn, story_id, str(earlier.id))
                    if stored.proposition.object_key() != proposition.object_key():
                        await _conflict(
                            conn,
                            str(row.id),
                            story_id,
                            proposition,
                            earlier,
                            (turn.turn, message.event_id),
                        )
                        tally.conflicts += 1
                fact = await canon.commit_fact(
                    conn,
                    story_id,
                    proposition,
                    provenance=RecordProvenance.BACKFILL,
                    operation=op,
                    source_event_ids=[message.event_id],
                    beat=index + 1,
                    registry=registry,
                )
                written.append((fact.proposition_id, proposition))
                tally.facts += 1
        if message.kind in ("say", "narration", "do") and message.text.strip():
            beats.append(
                memories.Beat(
                    kind=message.kind,  # type: ignore[arg-type]
                    text=message.text,
                    event_id=message.event_id,
                    beat=index + 1,
                    character_id=message.speaker,
                    provenance=RecordProvenance.BACKFILL,
                    proposition_ids=tuple(pid for pid, _ in written),
                )
            )
        if capabilities & EXPOSURE_CAPABILITIES and message.kind == "say" and written:
            for witness in message.present:
                if witness == message.speaker:
                    continue
                for proposition_id, proposition in written:
                    exposed.append(
                        Exposed(
                            witness=witness,
                            proposition_id=proposition_id,
                            proposition=proposition,
                            source=message.speaker,
                            source_event_id=message.event_id,
                            position=Position(turn.turn, turn.variant, index + 1),
                            fidelity=ExposureFidelity.FULL,
                        )
                    )
    if capabilities & MEMORY_CAPABILITIES and beats:
        made = await memories.write_episodic(
            conn, op, beats, settings=settings, foreign_events=True
        )
        tally.memories += len(made)
    if exposed:
        ids = await record_exposures(conn, op, exposed, inferred=True)
        tally.exposures += len(ids)


async def reconcile_turns(
    conn: AsyncConnection,
    row: Any,
    turns: Sequence[ReadTurn],
    *,
    capabilities: Sequence[str],
    registry: Registry,
    settings: ProfileSettings,
) -> Tally:
    """Write ``turns`` (in order) in ``conn``'s transaction (see the module docstring)."""
    tally = Tally()
    wanted = frozenset(capabilities)
    ids = [m.event_id for t in turns for m in t.messages]
    texts = {
        str(r.message_id): str(r.text)
        for r in await conn.execute(
            select(transcript_messages.c.message_id, transcript_messages.c.text).where(
                transcript_messages.c.story_id == row.story_id,
                transcript_messages.c.message_id.in_(ids),
            )
        )
    }
    for turn in turns:
        for message in turn.messages:
            message.text = texts.get(message.event_id, "")
    for turn in sorted(turns, key=lambda t: t.turn):
        try:
            async with conn.begin_nested():
                await _turn(
                    conn,
                    row,
                    turn,
                    capabilities=wanted,
                    registry=registry,
                    settings=settings,
                    tally=tally,
                )
        except ErasedSubjectReferenced:
            tally.skipped_erased += 1
    return tally


def merge_report(report: Mapping[str, Any], tally: Tally) -> dict[str, Any]:
    """The backfill's report with ``tally`` added."""
    found = dict(report)
    for key in ("facts", "memories", "exposures", "skipped_erased"):
        found[key] = int(found.get(key, 0)) + int(getattr(tally, key))
    conflicts = dict(found.get("conflicts") or {"total": 0, "by_resolution": {}})
    conflicts["total"] = int(conflicts.get("total", 0)) + tally.conflicts
    by = dict(conflicts.get("by_resolution") or {})
    if tally.conflicts:
        by[LAST_MENTION] = int(by.get(LAST_MENTION, 0)) + tally.conflicts
    conflicts["by_resolution"] = by
    found["conflicts"] = conflicts
    return found


__all__ = [
    "EXPOSURE_CAPABILITIES",
    "FACT_CAPABILITIES",
    "LAST_MENTION",
    "MEMORY_CAPABILITIES",
    "Extracted",
    "ReadMessage",
    "ReadProposition",
    "ReadTurn",
    "Tally",
    "merge_report",
    "reconcile_turns",
]
