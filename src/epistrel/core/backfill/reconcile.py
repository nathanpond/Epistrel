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
  it, so its canon fact plans the event (``scheduled_events``) at that time;
- **placements** (the pipeline, ``partitions`` or ``perception`` gained): a turn whose text names
  a place places every character present there (``located_at``, provenance ``backfill``: the
  record of an inference), before its messages are routed; a character canon already places
  there is left as it is, and a turn naming no place places no one;
- **testimony** (``partitions`` gained, #162): a ``say``'s witness whose partition already holds
  something about a claim's subject (the partition-touch salience rule; never a minor or the
  player's character) takes it through the live testimony writers, each transition
  ``inferred``, and its exposure is recorded resolved; every other witness gets the unresolved
  inferred exposure above;
- **commitments** (``commitment`` gained, #146): each salient reference extraction kept
  (``ReadReference``: an open-argument claim on narration, or a ``salience_detection`` span)
  introduces its thunk with a ledger entry (``lifecycle.introduce``) before the message's
  claims are written, and a claim that fills an open thunk settles it (``lifecycle.depend``,
  provenance ``backfill``) instead of being written as a plain fact;
- **State changes** (``states`` gained, #187/#188/#190): see ``_narrated_states``; over a turn
  the story ran with States at ``prompt_only`` (no ``states`` gap) nothing is derived.

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
from epistrel.core.epistemic.exposure import Exposed, exposure_position, record_exposures
from epistrel.core.errors import ErasedSubjectReferenced
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, insert_derived
from epistrel.core.memory import records as memories
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Cardinality, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.tables import backfill_conflicts, facts, transcript_messages, utterances

FACT_CAPABILITIES = frozenset({"pipeline", "verification"})
COMMITMENT_CAPABILITIES = frozenset({"commitment"})
"""#228 (#146): gained commitment: the transcript's salient references become thunks, and a
later claim that fills one settles it."""
"""Gained capabilities whose data is canon facts."""
MEMORY_CAPABILITIES = frozenset({"pipeline"})
"""The structured pipeline gained: episodic memories, when the target configuration has the
``episodic`` module on (a behavioral module, so never in coverage itself; the writer checks)."""
EXPOSURE_CAPABILITIES = frozenset({"partitions"})
PLACEMENT_CAPABILITIES = frozenset({"pipeline", "partitions", "perception"})
"""#228: gained capabilities whose data includes the cast's placements (the scene projection
and #161's routing read ``located_at``)."""
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


Span = tuple[int, int]


class ReadReference(BaseModel):
    """#228 (#146): a salient reference read from a message, a thunk's sketch to be: the
    resolved predicate and the offsets of its argument words in the message's text (never the
    words: the checkpoint keeps no transcript text; reconciliation reads them from the
    transcript, so an erasure's redaction holds). ``source``: ``rules`` (an extraction claim on
    narration that leaves an argument open, the rules mode's reference) or ``model``
    (``salience_detection``)."""

    predicate_id: str
    span: Span | None = None
    subject: Span | None = None
    object: Span | None = None
    time: Span | None = None
    negated: bool = False
    source: str = "rules"


class ReadMessage(BaseModel):
    """One transcript message as extraction read it. Its ``text`` is never stored in the
    checkpoint: reconciliation reads it from the transcript (so an erasure's redaction holds)."""

    event_id: str
    kind: str
    speaker: str | None = None
    text: str = ""
    present: list[str] = Field(default_factory=list)
    propositions: list[ReadProposition] = Field(default_factory=list)
    references: list[ReadReference] = Field(default_factory=list)


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
    placements: list[ReadProposition] = Field(default_factory=list)
    """#228: the turn's inferred placements, ``located_at(<present character>, <place>)`` for
    each character present when the turn's text names a place (the last one named: where the
    turn ends), written before the turn's messages are routed."""
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
    placements: int = 0
    testimonies: int = 0
    thunks: int = 0
    settled: int = 0
    state_changes: int = 0
    skipped_state_changes: list[dict[str, Any]] = field(default_factory=list)
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
    state_turns: frozenset[int] = frozenset(),
) -> None:
    story_id, op_id = str(row.story_id), str(row.operation_id)
    op = _shim(op_id, story_id, str(row.requested_by), turn)
    policy = str((row.report or {}).get("policy") or LAST_MENTION)
    beats: list[memories.Beat] = []
    exposed: list[Exposed] = []
    observed: list[Any] = []
    minors: frozenset[str] = frozenset()
    if capabilities & EXPOSURE_CAPABILITIES:
        from epistrel.core.epistemic import testimony

        minors = await testimony.minors(conn, story_id)
    if turn.clock is not None:
        await _advance(conn, op, story_id, turn, tally)
    if turn.placements and capabilities & PLACEMENT_CAPABILITIES:
        tally.placements += await _place(conn, story_id, op, turn, registry)
    committing = bool(capabilities & COMMITMENT_CAPABILITIES)
    for index, message in enumerate(turn.messages):
        written: list[tuple[str, Proposition]] = []
        if committing and message.references:
            # #146: salience runs before dependence within a render.
            tally.thunks += await _introduce(conn, op, message, index + 1, registry)
        if capabilities & FACT_CAPABILITIES or committing:
            for read in message.propositions:
                proposition = await _placed(
                    conn,
                    story_id,
                    Proposition.model_validate(read.proposition),
                    read.time_text,
                    Position(turn.turn, turn.variant, 0, index + 1),
                )
                if committing and message.kind in DEPENDENCE_KINDS:
                    settled = await _depend(conn, op, message, index + 1, proposition, registry)
                    if settled is not None:
                        written.append((settled, proposition))
                        tally.settled += 1
                        tally.facts += 1
                        continue
                if not capabilities & FACT_CAPABILITIES:
                    continue
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
                if message.speaker is not None and await _salient(
                    conn, story_id, witness, written, minors
                ):
                    # #162: a salient witness takes the say as testimony, as a live turn does.
                    tally.exposures += await _testify(
                        conn,
                        op,
                        message,
                        index + 1,
                        witness,
                        written,
                        settings=settings,
                        registry=registry,
                        tally=tally,
                    )
                    continue
                for proposition_id, proposition in written:
                    exposed.append(
                        Exposed(
                            witness=witness,
                            proposition_id=proposition_id,
                            proposition=proposition,
                            source=message.speaker,
                            source_event_id=message.event_id,
                            position=exposure_position(turn.turn, turn.variant, index + 1),
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
    if "states" in capabilities and turn.turn in state_turns:
        await _narrated_states(conn, op, turn, tally)
    if capabilities & MEMORY_CAPABILITIES and beats:
        made = await memories.write_episodic(
            conn, op, beats, settings=settings, foreign_events=True
        )
        tally.memories += len(made)
    if exposed:
        ids = await record_exposures(conn, op, exposed, inferred=True)
        tally.exposures += len(ids)
    if observed:
        from epistrel.core.epistemic import observation, transitions

        with transitions.inferring():
            tally.observations += await observation.observe(conn, op, observed, settings=settings)


async def _placed_at(conn: AsyncConnection, story_id: str, character: str) -> str | None:
    """Where canon places ``character`` now (its open ``located_at``), as this transaction
    reads it (the backfill's own rows active)."""
    from epistrel.core.enums import Membership
    from epistrel.core.propositions.vocabulary import LOCATED_AT
    from epistrel.core.tables import propositions

    f, pr = facts.c, propositions.c
    found = await conn.scalar(
        select(pr.object_id)
        .select_from(facts.join(propositions, pr.id == f.proposition_id))
        .where(
            f.story_id == story_id,
            f.subject_id == character,
            f.predicate_id == LOCATED_AT,
            pr.polarity == "affirmed",
            f.tx_to.is_(None),
            f.valid_to.is_(None),
            f.membership == Membership.ACTIVE.value,
        )
        .order_by(f.narrative_turn.desc(), f.id.desc())
        .limit(1)
    )
    return None if found is None else str(found)


async def _place(
    conn: AsyncConnection, story_id: str, op: Any, turn: ReadTurn, registry: Registry
) -> int:
    """#228: the turn's inferred placements (``ReadTurn.placements``), written before its
    messages are routed (#579: an unplaced character perceives nothing), through
    ``commit_fact`` with provenance ``backfill`` (the record of an inference from the text);
    a character canon already places there is left as it is. Answers how many were written."""
    written = 0
    for read in turn.placements:
        proposition = Proposition.model_validate(read.proposition)
        if await _placed_at(conn, story_id, proposition.subject_id) == proposition.object_id:
            continue
        await canon.commit_fact(
            conn,
            story_id,
            proposition,
            provenance=RecordProvenance.BACKFILL,
            operation=op,
            source_event_ids=[m.event_id for m in turn.messages[:1]],
            beat=0,
            registry=registry,
        )
        written += 1
    return written


async def _salient(
    conn: AsyncConnection,
    story_id: str,
    witness: str,
    written: Sequence[tuple[str, Proposition]],
    minors: frozenset[str],
) -> bool:
    """#162's salience rule for a backfilled ``say``: the witness's partition already holds
    something about a claim's subject (the partition-touch rule; the turn has no salience
    marks, since a Lite render declared none), never a minor nor the player's character (who
    holds no partition)."""
    from epistrel.core.epistemic import partitions, testimony

    if witness in minors or await partitions.is_player(conn, story_id, witness):
        return False
    for _, proposition in written:
        if await testimony.touches(conn, story_id, witness, proposition.subject_id):
            return True
    return False


async def _testify(
    conn: AsyncConnection,
    op: Any,
    message: ReadMessage,
    beat: int,
    witness: str,
    written: Sequence[tuple[str, Proposition]],
    *,
    settings: ProfileSettings,
    registry: Registry,
    tally: Tally,
) -> int:
    """#162 (FR-PROF-9): a salient witness's testimony of a backfilled ``say``, through the live
    writers (``testimony.consistency_of``, ``trust``, the deterministic ``testimony`` default,
    ``commit_testimony``; #167's nested beliefs when the target keeps them), every transition
    flagged ``inferred``; then its exposures, recorded resolved by those transitions, for audit
    (as a live turn records a salient witness's). No ``belief_adjudication`` call: a backfill
    makes no model call per witness. Answers the exposures written."""
    from epistrel.core.epistemic import testimony, transitions
    from epistrel.core.readset import KeyedLookup, StructuredReadSet

    assert message.speaker is not None  # noqa: S101 - the caller checked
    story_id = str(op.story_id)
    propositions = [p for _, p in written]
    act = testimony.SpeechAct(
        speaker=message.speaker,
        beat=beat,
        propositions=tuple(propositions),
        event_id=message.event_id,
        text=message.text,
    )
    lookup = KeyedLookup(conn, StructuredReadSet())
    consistency = await testimony.consistency_of(
        lookup, story_id, witness, propositions, registry=registry
    )
    trust = await testimony.trust(conn, story_id, witness, message.speaker)
    outcomes = list(
        testimony.testimony(
            witness, act, testimony.perceived("unspecified"), consistency=consistency, trust=trust
        )
    )
    with transitions.inferring():
        made = await testimony.commit_testimony(
            conn, op, act, outcomes, settings=settings, registry=registry, intent="unspecified"
        )
        # Nothing unless the target keeps nested beliefs (the writer reads it).
        await testimony.nested_committed(
            conn,
            op,
            act,
            [o for o in outcomes if o.proposition is not None],
            witness=witness,
            intent="unspecified",
            trust_value=trust,
            settings=settings,
            registry=registry,
        )
    tally.testimonies += 1
    position = exposure_position(int(op.narrative_turn or 0), int(op.turn_variant or 0), beat)
    ids = await record_exposures(
        conn,
        op,
        [
            Exposed(
                witness=witness,
                proposition_id=proposition_id,
                proposition=proposition,
                source=message.speaker,
                source_event_id=message.event_id,
                position=position,
                fidelity=ExposureFidelity.FULL,
                resolved_by_transition=_resolving(made, proposition, registry),
            )
            for proposition_id, proposition in written
        ],
        inferred=True,
    )
    return len(ids)


def _resolving(made: Sequence[Any], proposition: Proposition, registry: Registry) -> str | None:
    """The transition that resolved a salient witness's claim: its first-order transition
    when it wrote one, else its ``said`` belief's (the live turn's rule,
    ``orchestrator.epistemic.adjudicate``)."""
    from epistrel.core.enums import BeliefKind
    from epistrel.core.propositions.registry import canonical

    keys = (
        canonical(proposition, registry).normalized_key(),
        canonical(proposition.negation(), registry).normalized_key(),
    )
    mine = [m for m in made if canonical(m.belief.proposition, registry).normalized_key() in keys]
    first = next((m for m in mine if m.belief.kind is BeliefKind.FIRST_ORDER), None)
    said = next((m for m in mine if m.belief.kind is BeliefKind.SPEECH_ACT), None)
    chosen = first or said or (made[0] if made else None)
    return None if chosen is None else str(chosen.transition.id)


DEPENDENCE_KINDS = frozenset({"narration", "do", "say"})
"""#146: the backfilled messages whose claims may settle an open thunk (a narration, or the
player's input, which a live turn reads on the ``player`` channel)."""


def _channel(message: ReadMessage) -> Any:
    return "player" if message.kind == "say" else message.kind


def _words(text: str, at: Span | None) -> str | None:
    return None if at is None else text[at[0] : at[1]] or None


async def _introduce(
    conn: AsyncConnection, op: Any, message: ReadMessage, beat: int, registry: Registry
) -> int:
    """#228 (#146's ``record_salience``, through the live writers): each salient reference of
    ``message`` (its words read again from the transcript) normalized with one open argument
    (``open_sketch``); one canon already settles owes nothing; every other one introduces (or
    finds) its thunk with a ledger entry (``lifecycle.introduce(salient=True)``) at the
    message's position. Answers how many references gave a thunk."""
    from epistrel.core.commit import lifecycle, matching
    from epistrel.core.commit.sketch import ThunkSketch, open_sketch
    from epistrel.core.propositions.model import Polarity
    from epistrel.core.readset import KeyedLookup, StructuredReadSet
    from epistrel.core.resolution.context import load_context
    from epistrel.core.resolution.entities import NARRATOR
    from epistrel.core.resolution.identity import speaker_identities
    from epistrel.core.verify.normalize import ClaimSketch

    story_id = str(op.story_id)
    lookup = KeyedLookup(conn, StructuredReadSet())
    speaker = message.speaker or NARRATOR
    position = Position(int(op.narrative_turn or 0), int(op.turn_variant or 0), 0, beat)
    context = await load_context(
        conn,
        story_id,
        position=position,
        speaker=speaker,
        lookup=lookup,
        identities=await speaker_identities(
            lookup, story_id, speaker, through_position=(position.turn, 0)
        ),
    )
    made = 0
    for reference in message.references:
        predicate = registry.get(reference.predicate_id)
        if predicate is None:
            continue
        claim = ClaimSketch(
            subject_text=_words(message.text, reference.subject) or "",
            relation_text=predicate.name,
            object_text=_words(message.text, reference.object),
            time_text=_words(message.text, reference.time),
            polarity=Polarity.NEGATED if reference.negated else Polarity.AFFIRMED,
        )
        span = None if reference.span is None else (beat, *reference.span)
        found = open_sketch(claim, speaker, registry=registry, context=context, span=span)
        if not isinstance(found, ThunkSketch) or await matching.settled(lookup, story_id, found):
            continue
        thunk = await lifecycle.introduce(
            conn,
            op,
            found,
            beat=beat,
            salient=True,
            source_event_ids=[message.event_id],
            on=True,
        )
        if thunk is not None:
            made += 1
    return made


async def _depend(
    conn: AsyncConnection,
    op: Any,
    message: ReadMessage,
    beat: int,
    proposition: Proposition,
    registry: Registry,
) -> str | None:
    """#228 (#146): a backfilled claim that fills an open thunk settles it, in narrative order,
    through the live check (``matching.dependence_target``, ``lifecycle.depend``, its
    resolution fact ``provenance = backfill``); answers the resolution fact's proposition id,
    or ``None`` when no thunk takes it (the claim is then an ordinary fact)."""
    from epistrel.core.commit import lifecycle, matching
    from epistrel.core.readset import KeyedLookup, StructuredReadSet

    story_id = str(op.story_id)
    target = await matching.dependence_target(
        KeyedLookup(conn, StructuredReadSet()), story_id, proposition
    )
    if target is None:
        return None
    done = await lifecycle.depend(
        conn,
        op,
        target.id,
        proposition,
        beat=beat,
        channel=_channel(message),
        provenance=RecordProvenance.BACKFILL,
        source_event_ids=[message.event_id],
        registry=registry,
        on=True,
    )
    if done.kind != "committed" or done.fact is None:
        return None
    return str(done.fact.proposition_id)


STATE_REASON = "backfill: a change the transcript narrates"


async def _narrated_states(conn: AsyncConnection, op: Any, turn: ReadTurn, tally: Tally) -> None:
    """#228 (FR-PROF-9, FR-STATE-13): the changes the turn's narration makes to story-owned
    States, read by #190's completeness detector (``completeness.diff`` with no declared
    effect: every narrated change is ``undeclared``), authorized by #188 for the narrator's
    source (``director``), and written by ``states.apply_change`` (#187's bounds) with
    ``source = backfill``, flagged ``inferred``. Game-owned (external) values, the world clock
    and derived States are never inferred. A change with no stated amount, of a
    character-scoped State (a narration names no actor), or of a non-number State is not
    inferred; one the authorization or the bounds refuse is skipped. Each skip is listed in
    the report (``skipped_state_changes``: ``{turn, state_id, reason_code}``); nothing is
    repaired."""
    from decimal import Decimal

    from epistrel.core import states
    from epistrel.core.enums import StateChangeKind, StateChangeSource, StateOwner, StateScope
    from epistrel.core.enums import StateType as Type
    from epistrel.core.states import authorize as authorization
    from epistrel.core.states import completeness
    from epistrel.core.states.model import WORLD_CLOCK, derived_rule
    from epistrel.core.tables import entities

    story_id = str(op.story_id)
    if await states.state_level(conn, story_id) is None:
        return
    defined = {
        d.name: d
        for d in await states.definitions(conn, story_id)
        if d.owner is StateOwner.STORY and d.name != WORLD_CLOCK and derived_rule(d) is None
    }
    if not defined:
        return
    beats = [
        completeness.Beat(beat=n + 1, kind="narration", text=m.text, narrator=True)
        for n, m in enumerate(turn.messages)
        if m.kind in OBSERVED_KINDS and m.text.strip()
    ]
    if not beats:
        return
    tracked = [d.entity_id for d in defined.values() if d.entity_id]
    names = {
        str(r.id): str(r.canonical_name)
        for r in await conn.execute(
            select(entities.c.id, entities.c.canonical_name).where(entities.c.id.in_(tracked))
        )
    }
    found = completeness.diff([], [], defined, beats=beats, entity_names=names)
    so_far: list[authorization.Authorized] = []

    def skip(definition: Any, reason: str) -> None:
        tally.skipped_state_changes.append(
            {"turn": turn.turn, "state_id": definition.id, "reason_code": reason}
        )

    for undeclared in found.undeclared:
        ref = undeclared.ref
        definition = defined[ref.state]
        if definition.type is not Type.NUMBER:
            skip(definition, "not_a_number")
            continue
        if definition.scope is not StateScope.STORY:
            skip(definition, "scope_unknown")
            continue
        if ref.amount is None or ref.direction is None:
            skip(definition, "amount_unknown")
            continue
        delta = Decimal(ref.amount) * ref.direction
        effect = authorization.StateEffect(state=definition.name, delta=delta, beat=ref.beat)
        decision = authorization.authorize(
            effect,
            authorization.Source("director"),
            so_far,
            definition=definition,
            definitions=defined,
        )
        if isinstance(decision, authorization.Unpermitted):
            skip(definition, decision.reason)
            continue
        if isinstance(decision, authorization.Capped):
            skip(definition, authorization.CAP)  # a cap is a repair; a backfill never repairs
            continue
        with states.inferring():
            applied = await states.apply_change(
                conn,
                op,
                states.StateChange(
                    state=definition.name,
                    reason=STATE_REASON,
                    source=StateChangeSource.BACKFILL,
                    kind=StateChangeKind.DELTA,
                    delta=delta,
                    position=Position(int(turn.turn), int(turn.variant), 0, ref.beat, 0),
                ),
            )
        if isinstance(applied, states.Violation):
            skip(definition, applied.constraint)
            continue
        so_far += authorization.authorized(effect, delta, defined)
        tally.state_changes += 1


CLOCK_REASON = "backfill: re-derived from the transcript"
OBSERVED_KINDS = frozenset({"do", "narration"})
REFERENCE_KINDS = frozenset({"do", "narration"})
"""#228: the messages whose open references are salient (``rules_salient``'s ``RULE_KINDS``)."""
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
    state_turns: frozenset[int] = frozenset()
    if "states" in wanted:
        from epistrel.core.backfill import coverage

        # Owner decision 2: a turn the story ran with States at ``prompt_only`` keeps its
        # recorded changes (it is covered, so no gap) and gets nothing more.
        state_turns = frozenset(
            t
            for low, high in await coverage.gaps(conn, str(row.story_id), "states")
            for t in range(low, high + 1)
        )
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
                    state_turns=state_turns,
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
        "observations",
        "placements",
        "testimonies",
        "thunks",
        "settled",
        "state_changes",
    ):
        found[key] = int(found.get(key, 0)) + int(getattr(tally, key))
    if tally.skipped_state_changes:
        found["skipped_state_changes"] = [
            *list(found.get("skipped_state_changes") or []),
            *tally.skipped_state_changes,
        ]
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
    "COMMITMENT_CAPABILITIES",
    "EXPOSURE_CAPABILITIES",
    "FACT_CAPABILITIES",
    "FIRST_MENTION",
    "LAST_MENTION",
    "MEMORY_CAPABILITIES",
    "PLACEMENT_CAPABILITIES",
    "POLICIES",
    "ClockStep",
    "Extracted",
    "ReadMessage",
    "ReadProposition",
    "ReadReference",
    "ReadTurn",
    "Tally",
    "in_world_claim",
    "merge_report",
    "reconcile_turns",
    "statement",
    "value_of",
]
