"""A backfill's ordered reconciliation (FR-PROF-9; #228).

The extraction stage stored, per chunk, each turn's messages with what was read from them
(``Extracted``: the propositions the claims normalized to, whether a claim narrated a change,
and the characters present). Reconciliation walks the turns in narrative order and writes,
through the live writers and under the backfill operation at each turn's own position
(``Position``), with provenance ``backfill``:

- **facts** (the structured pipeline or verification gained): ``canon.commit_fact`` per
  proposition, citing its message's event. A functional key's value that differs from the one
  the backfill last wrote for it, with no narrated change (the claim's ``time``), is a
  **contradiction**, settled by the story's ``backfill_conflict_policy`` (#230, read at the
  backfill's start into ``report.policy``): ``last_mention`` (the default) makes the later
  statement canon for the overlap (``commit_fact`` closes the earlier version), and
  ``first_mention`` keeps the earlier one (the later is not written); either way a
  ``backfill_conflicts`` row records the statements, ``resolved``, ``{policy}``, and the losing
  statement, when a character spoke it, is recorded as an in-world claim (an ``utterances`` row,
  ``objective_relation = false``, ``sincere_error``). Under ``ask`` the later statement is not
  written: the row is ``open`` (a further contradicting statement of the key joins it) and
  ``core.backfill.conflicts`` settles it;
- **episodic memories** (the structured pipeline gained, with ``episodic`` on in the target
  configuration): ``memory.records.write_episodic``, one per observable message, trust tier
  ``backfill`` (the lowest);
- **exposures** (``partitions`` gained): each character present at a ``say`` other than its
  speaker is exposed to its propositions (``epistemic.exposure.record_exposures``), flagged
  ``inferred``; unresolved, so a later turn resolves them lazily as it does live ones;
- **observations** (``partitions`` gained, #709): the claims of a narrated message are observed
  at ``content`` fidelity by the characters its turn names (presence inferred from the text):
  ``epistemic.observation.observe``, one ``observation`` transition per perceiver and claim, so
  each holds its own belief;
- **the clock** (``clock`` gained, #709): each turn's advance, planned in narrative order before
  the transaction (``ClockStep``: #194's pacing over the Lite turn's combined render, or a
  narrated skip in its place, with ``time_estimation`` given the reconciled clock), written by
  ``world_clock.advance`` with ``source = backfill``; a turn that already holds recorded
  advances (a ``prompt_only`` era) keeps them and gets none;
- **scheduled times** (#709): a ``scheduled`` claim's time of day ("at 5 pm") is placed through
  the reconciled clock's anchor at the claim's position, as a live turn's normalization places
  it, so its canon fact plans the event (``scheduled_events``) at that time.

The live writers read the held target configuration (``profiles.effective.deriving``), so the
clock's gate and time basis are the target's while the stored configuration is unchanged.

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
from epistrel.core.membership import Position, insert_derived
from epistrel.core.memory import records as memories
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Cardinality, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.tables import backfill_conflicts, facts, transcript_messages, utterances

FACT_CAPABILITIES = frozenset({"pipeline", "verification"})
"""Gained capabilities whose data is canon facts."""
MEMORY_CAPABILITIES = frozenset({"pipeline"})
"""The structured pipeline gained: episodic memories, when the target configuration has the
``episodic`` module on (a behavioral module, so never in coverage itself; the writer checks)."""
EXPOSURE_CAPABILITIES = frozenset({"partitions"})
LAST_MENTION = "last_mention"
FIRST_MENTION = "first_mention"
ASK = "ask"
POLICIES = (LAST_MENTION, FIRST_MENTION, ASK)


class ReadProposition(BaseModel):
    """One proposition a claim normalized to, and whether its claim narrated a change.
    ``time_text`` (#709): the claim's time expression, placed at reconciliation against the
    reconciled clock (a ``scheduled`` claim's time of day: "at five")."""

    proposition: dict[str, Any]
    changed: bool = False
    time_text: str | None = None


class ReadMessage(BaseModel):
    """One transcript message as extraction read it. Its ``text`` is never stored in the
    checkpoint: reconciliation reads it from the transcript (so an erasure's redaction holds)."""

    event_id: str
    kind: str
    speaker: str | None = None
    text: str = ""
    present: list[str] = Field(default_factory=list)
    propositions: list[ReadProposition] = Field(default_factory=list)


class ClockStep(BaseModel):
    """#709: a reconciled turn's clock advance, planned before the reconciliation's transaction
    (its ``time_estimation`` call made in order, with the reconciled clock): ``minutes`` (0:
    none), how it was ``derived`` (``pacing``, ``declared_skip``, ``estimate``), and a narrated
    skip #199's authority refused (``refused``: reported, not repaired; the turn paces)."""

    minutes: int = 0
    derived: str = "pacing"
    reason: str = ""
    refused: str | None = None
    meta: dict[str, Any] | None = None


class ReadTurn(BaseModel):
    turn: int
    variant: int = 0
    messages: list[ReadMessage] = Field(default_factory=list)
    clock: ClockStep | None = None
    """#709: the turn's planned advance (``None``: the clock is not gained, or the turn already
    holds the advances a ``prompt_only`` clock recorded)."""


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
    open: int = 0
    skipped_erased: int = 0
    clock_minutes: int = 0
    skipped_skips: int = 0
    observations: int = 0
    by_turn: dict[int, int] = field(default_factory=dict)


def _shim(op_id: str, story_id: str, principal: str, turn: ReadTurn) -> Any:
    return SimpleNamespace(
        operation_id=op_id,
        story_id=story_id,
        principal=principal,
        narrative_turn=turn.turn,
        turn_variant=turn.variant,
        base_revision=None,
        # #709: what a State change (the clock's advance) reads of its operation.
        provenance=None,
        slot=False,
        slot_k=None,
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
            select(f.id, f.proposition_id, f.narrative_turn, f.source_event_ids, f.provenance)
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


def value_of(proposition: Proposition) -> Any:
    """A statement's value as the conflict listing shows it."""
    if proposition.literal is not None:
        return proposition.literal.value
    if proposition.object_set is not None:
        return list(proposition.object_set)
    return proposition.object_id


def statement(
    proposition: Proposition,
    *,
    turn: int,
    variant: int,
    beat: int,
    event_id: str | None,
    speaker: str | None,
    fact_id: str | None = None,
    source: str = "transcript",
) -> dict[str, Any]:
    """One statement of a conflict (``statement_id = <event_id>:<start>-<end>``; extraction keeps
    no span here, so ``0-0``). No text: the listing quotes the transcript (an erasure's
    redaction holds). ``source``: ``transcript``, or ``seed`` for a materialized genesis seed
    (#698: the backfill's earliest statement, at turn 0)."""
    return {
        "statement_id": f"{event_id}:0-0",
        "event_id": event_id,
        "turn": turn,
        "variant": variant,
        "beat": beat,
        "position": turn,
        "speaker": speaker,
        "source": source,
        "value": value_of(proposition),
        "proposition": proposition.model_dump(mode="json"),
        "fact_id": fact_id,
    }


async def _earlier(
    conn: AsyncConnection, story_id: str, earlier: Any, stored: Proposition
) -> dict[str, Any]:
    event_id = (list(earlier.source_event_ids or []) or [None])[0]
    speaker = None
    if event_id is not None:
        speaker = await conn.scalar(
            select(transcript_messages.c.actor_character_id).where(
                transcript_messages.c.story_id == story_id,
                transcript_messages.c.message_id == event_id,
            )
        )
    return statement(
        stored,
        turn=int(earlier.narrative_turn or 0),
        variant=0,
        beat=1,
        event_id=event_id,
        speaker=None if speaker is None else str(speaker),
        fact_id=str(earlier.id),
        source="seed" if str(getattr(earlier, "provenance", "")) == "seed" else "transcript",
    )


async def in_world_claim(
    conn: AsyncConnection, story_id: str, operation_id: str, loser: Mapping[str, Any]
) -> None:
    """Record a losing statement a character spoke as an in-world claim (#166's labels: what it
    said is false; a ``sincere_error``, the speaker's belief not read here)."""
    if not loser.get("speaker") or not loser.get("event_id"):
        return
    await insert_derived(
        conn,
        utterances,
        [
            {
                "id": new_ulid(),
                "story_id": story_id,
                "operation_id": operation_id,
                "source_event_ids": [loser["event_id"]],
                "narrative_turn": loser.get("turn"),
                "turn_variant": loser.get("variant", 0),
                "event_id": loser["event_id"],
                "speaker_character_id": loser["speaker"],
                "objective_relation": "false",
                "classification": "sincere_error",
            }
        ],
    )


async def _open_conflict(
    conn: AsyncConnection,
    backfill_id: str,
    story_id: str,
    proposition: Proposition,
    statements: Sequence[Mapping[str, Any]],
) -> bool:
    """Open (or join) the key's conflict under ``ask``; whether a new row was opened."""
    subject = {"entity_id": proposition.subject_id, "predicate": proposition.predicate_id}
    t = backfill_conflicts.c
    found = (
        await conn.execute(
            select(t.id, t.statements).where(
                t.backfill_id == backfill_id, t.status == "open", t.subject == subject
            )
        )
    ).one_or_none()
    if found is not None:
        known = {s.get("statement_id") for s in found.statements or []}
        joined = [*found.statements, *(s for s in statements if s["statement_id"] not in known)]
        await conn.execute(
            backfill_conflicts.update().where(t.id == found.id).values(statements=joined)
        )
        return False
    await conn.execute(
        insert(backfill_conflicts).values(
            id=new_ulid(),
            backfill_id=backfill_id,
            story_id=story_id,
            subject=subject,
            statements=list(statements),
            status="open",
        )
    )
    return True


async def _conflict(
    conn: AsyncConnection,
    backfill_id: str,
    story_id: str,
    proposition: Proposition,
    statements: Sequence[Mapping[str, Any]],
    policy: str,
) -> None:
    await conn.execute(
        insert(backfill_conflicts).values(
            id=new_ulid(),
            backfill_id=backfill_id,
            story_id=story_id,
            subject={"entity_id": proposition.subject_id, "predicate": proposition.predicate_id},
            statements=list(statements),
            status="resolved",
            resolution={
                "policy": policy,
                "canon": "latest" if policy == LAST_MENTION else "earliest",
            },
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
    policy = str((row.report or {}).get("policy") or LAST_MENTION)
    beats: list[memories.Beat] = []
    exposed: list[Exposed] = []
    observed: list[Any] = []
    if turn.clock is not None:
        await _advance(conn, op, story_id, turn, tally)
    for index, message in enumerate(turn.messages):
        written: list[tuple[str, Proposition]] = []
        if capabilities & FACT_CAPABILITIES:
            for read in message.propositions:
                proposition = await _placed(
                    conn,
                    story_id,
                    Proposition.model_validate(read.proposition),
                    read.time_text,
                    Position(turn.turn, turn.variant, index + 1),
                )
                earlier = await _previous(conn, story_id, op_id, proposition)
                if (
                    earlier is not None
                    and not read.changed
                    and _functional(registry, proposition.predicate_id)
                ):
                    stored, _ = await canon._load(conn, story_id, str(earlier.id))
                    if stored.proposition.object_key() != proposition.object_key():
                        old = await _earlier(conn, story_id, earlier, stored.proposition)
                        new = statement(
                            proposition,
                            turn=turn.turn,
                            variant=turn.variant,
                            beat=index + 1,
                            event_id=message.event_id,
                            speaker=message.speaker,
                        )
                        if policy == ASK:
                            if await _open_conflict(
                                conn, str(row.id), story_id, proposition, [old, new]
                            ):
                                tally.conflicts += 1
                                tally.open += 1
                            continue
                        await _conflict(
                            conn, str(row.id), story_id, proposition, [old, new], policy
                        )
                        tally.conflicts += 1
                        if policy == FIRST_MENTION:
                            await in_world_claim(conn, story_id, op_id, new)
                            continue
                        await in_world_claim(conn, story_id, op_id, old)
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
        if (
            capabilities & EXPOSURE_CAPABILITIES
            and message.kind in OBSERVED_KINDS
            and written
            and message.present
        ):
            observed.append(_observed(message, index + 1, [p for _, p in written]))
    if capabilities & MEMORY_CAPABILITIES and beats:
        made = await memories.write_episodic(
            conn, op, beats, settings=settings, foreign_events=True
        )
        tally.memories += len(made)
    if exposed:
        ids = await record_exposures(conn, op, exposed, inferred=True)
        tally.exposures += len(ids)
    if observed:
        from epistrel.core.epistemic import observation

        tally.observations += await observation.observe(conn, op, observed, settings=settings)


CLOCK_REASON = "backfill: re-derived from the transcript"
OBSERVED_KINDS = frozenset({"do", "narration"})
"""The messages whose claims the characters present observe (#164's observed kinds)."""


def _observed(message: ReadMessage, beat: int, claims: Sequence[Proposition]) -> Any:
    """#709 (FR-PROF-9, FR-EPI-7): a narrated message as #161's routing result for the
    observation sink: its claims observed at ``content`` fidelity by the characters the text
    names (presence inferred from the transcript, the only placement a Lite era has; the
    narrator's lines have no actor). ``core.epistemic.observation.observe`` writes one
    ``observation`` transition per perceiver and claim, skipping the player's character."""
    from epistrel.core.epistemic import perception

    return perception.PerceptionResult(
        beat=perception.RouteBeat(
            kind="narration",
            beat=beat,
            actor=message.speaker,
            source=SimpleNamespace(event_id=message.event_id, narrated=False),
            claims=tuple(claims),
        ),
        qualifiers=perception.Qualifiers(),
        level=perception.CO_LOCATION,
        location=None,
        perceived=tuple(
            perception.Perceived(character_id=c, fidelity=perception.CONTENT, channel="room")
            for c in message.present
        ),
    )


async def _advance(
    conn: AsyncConnection, op: Any, story_id: str, turn: ReadTurn, tally: Tally
) -> None:
    """#709: the turn's planned clock advance, at the Lite reply's position (#203: one
    combined-render increment, or the accepted narrated skip in its place), ``source =
    backfill``; a refused skip is counted (``skipped_skips``), never repaired."""
    from epistrel.core import world_clock
    from epistrel.core.enums import StateChangeSource
    from epistrel.core.membership import POSITION_MAX
    from epistrel.core.turns import REPLY_BEAT

    step = turn.clock
    assert step is not None  # noqa: S101 - the caller checked
    if step.refused is not None:
        tally.skipped_skips += 1
    if step.minutes <= 0:
        return
    await world_clock.advance(
        conn,
        op,
        story_id,
        minutes=step.minutes,
        source=StateChangeSource.BACKFILL,
        position=Position(turn.turn, turn.variant, 0, REPLY_BEAT, POSITION_MAX),
        reason=(step.reason or CLOCK_REASON)[:200],
        estimate_meta={"derived": step.derived, **(step.meta or {})},
    )
    tally.clock_minutes += step.minutes


async def _placed(
    conn: AsyncConnection,
    story_id: str,
    proposition: Proposition,
    time_text: str | None,
    position: Position,
) -> Proposition:
    """#709 (FR-CLOCK-9): a ``scheduled`` claim's time of day ("at five") placed through the
    reconciled clock's anchor at the claim's position (``schedule_statements.place_time``, as a
    live turn's normalization places it), so the canon fact plans its event at that time; any
    other proposition, or a time the clock cannot place, as extracted."""
    from epistrel.core.propositions.model import Granularity, Instant
    from epistrel.core.propositions.vocabulary import SCHEDULED

    if not time_text or proposition.predicate_id != SCHEDULED:
        return proposition
    from epistrel.core import world_clock
    from epistrel.core.world_clock.schedule_statements import place_time

    if await world_clock.clock_level(conn, story_id) is None:
        return proposition
    basis = await world_clock.time_basis(conn, story_id)
    placed = place_time(time_text, basis, position)
    if placed is None:
        return proposition
    return proposition.model_copy(update={"valid_time": Instant.of(placed, Granularity.MINUTE)})


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
    for key in (
        "facts",
        "memories",
        "exposures",
        "skipped_erased",
        "clock_minutes",
        "skipped_skips",
    ):
        found[key] = int(found.get(key, 0)) + int(getattr(tally, key))
    conflicts = dict(found.get("conflicts") or {"total": 0, "by_resolution": {}})
    conflicts["total"] = int(conflicts.get("total", 0)) + tally.conflicts
    by = dict(conflicts.get("by_resolution") or {})
    policy = str(found.get("policy") or LAST_MENTION)
    settled = tally.conflicts - tally.open
    if settled:
        by[policy] = int(by.get(policy, 0)) + settled
    if tally.open:
        by["open"] = int(by.get("open", 0)) + tally.open
    conflicts["by_resolution"] = by
    found["conflicts"] = conflicts
    return found


__all__ = [
    "ASK",
    "CLOCK_REASON",
    "EXPOSURE_CAPABILITIES",
    "FACT_CAPABILITIES",
    "FIRST_MENTION",
    "LAST_MENTION",
    "MEMORY_CAPABILITIES",
    "POLICIES",
    "ClockStep",
    "Extracted",
    "ReadMessage",
    "ReadProposition",
    "ReadTurn",
    "Tally",
    "in_world_claim",
    "merge_report",
    "reconcile_turns",
    "statement",
    "value_of",
]
