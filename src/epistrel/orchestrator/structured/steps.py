"""A render's declared causal steps, verified in order (FR-TURN-12, FR-EPI-13,14 (c),
FR-STORE-31, FR-VERIFY-10,11; docs/02 §4.4; #168).

When a character's beliefs change within one render, the render declares the order it reasoned
in (``render.StepDecl``): *observe* (Jenni sees the bridge is down) → *believe* (it collapsed) →
*act* (she heads for the ferry). Each step sits at the narrative position ``(turn, variant, 0,
beat, index)`` where ``beat`` is the render's first beat (``position_of``), so its steps order
by index within the render and before the render's later beats; a beat takes the index of the
``act`` step that owns it (``core.membership.beat_steps``).

**Verification** (``StepVerifier``), in index order, interleaved with the render's beats: the
pipeline verifies the steps before an ``act`` step's beats (``run_before``), so an ``observe``
sees what the render's earlier beats established and nothing later.

- ``observe``: its proposition must be independently verifiable, deterministically only:
  ``supported`` against the canon the character may read (#159's scope, through the turn's
  same-turn overlay: committed canon, an earlier beat of the turn, or a permitted fact an
  earlier beat of this render introduced and verified, FR-VERIFY-11) and, for a located fact
  (``located_at``/``present_at`` a place), a perception path (#161: the character is at that
  place, inside it or containing it). Anything else (novel, contradicted, undecided, an
  unnormalizable sketch, no path) rejects the step (``unverified_object``,
  ``no_perception_path``): no entailment call, the safe disposition. A render that invents the
  observed fact itself is refused by its permission (#144) before the step is reached, so the
  overlay never holds it.
- ``believe``: it cites only verified earlier steps (or records: an event, a fact, an utterance,
  a belief of the character's, probed on the active timeline). A cite of a later step, of a
  step that was rejected, or no cite at all is **self-attestation** (``self_attestation``,
  FR-EPI-14): the step is rejected, and so is every step that cites it. The transition kind is
  the antecedent's: an ``observe`` step's → ``observation``, a ``believe`` step's →
  ``inference``, an utterance → ``testimony`` (its speaker the source), an event →
  ``observation``, a fact → ``discovery``, a belief → ``inference``.
- ``act``: owns the beats that name it; verified when reached (its beats are verified as
  beats).

**What a verdict does.** A verified ``believe`` joins the speaker's belief record set for the
rest of the turn (``BeliefSource.stage``, ``replay.stage_path``), so a later ``say`` of what it
concluded is ``consistent`` without a licence; the commit (``steps_committed``) writes each
verified ``observe`` as an ``observation`` transition (citing the observed fact's event, or the
``act`` step whose beat introduced it) with an episodic memory, and each verified ``believe`` as
a #160 transition with ``antecedent = render_step`` (``StepOracle`` answers the verified steps,
and a sequential turn's staged beats through its ``TurnStage``, #174).
A rejected ``believe``'s proposition, asserted in the beats of the first ``act`` citing it, is a
hallucination (#151's repair; FR-VERIFY-10), and nothing is written for it. With verification
off the steps are not verified and nothing is written; with ``character_knowledge = shared``
(partitions off) their order is still checked and nothing is written.

``turn_report.render.steps = {declared, verified, rejected: [{index, reason, cause}]}``. Log
lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.antecedents import RECORD_TABLES, RecordAntecedent, probe_record
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import transitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    EpistemicError,
    SelfAttestation,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition

log = logging.getLogger(__name__)

OBSERVE, BELIEVE, ACT = "observe", "believe", "act"
SELF_ATTESTATION = "self_attestation"
UNVERIFIED_OBJECT = "unverified_object"
NO_PERCEPTION_PATH = "no_perception_path"
ANTECEDENT_INVALID = "antecedent_invalid"
UNNORMALIZABLE = "unnormalizable"
LATER_CITE, UNVERIFIED_CITE, NO_CITE = "later_cite", "unverified_cite", "no_cite"

RECORD_KINDS: Mapping[str, str] = {
    "utterances": "testimony",
    "events": "observation",
    "facts": "discovery",
    "beliefs": "inference",
}
"""The transition kind a ``believe`` citing a record takes, by the record's table."""
STEP_KINDS: Mapping[str, str] = {OBSERVE: "observation", BELIEVE: "inference"}
"""The transition kind a ``believe`` citing a step takes, by the cited step's kind."""


@dataclass(frozen=True, slots=True)
class Step:
    """One declared step, normalized: its index and kind, the steps and records it cites, its
    proposition (``None``: an ``act``, or a sketch that did not normalize) and confidence."""

    index: int
    kind: str
    step_cites: tuple[int, ...] = ()
    record_cites: tuple[str, ...] = ()
    proposition: Proposition | None = None
    confidence: str | None = None
    unnormalizable: bool = False
    text: str = ""
    """The proposition as the render worded it (an observation's memory line)."""


@dataclass(frozen=True, slots=True)
class Observed:
    """What an ``observe`` check found: whether the object is verified, why not, and the
    observed record (a fact's source event, or the beat of this render that introduced it)."""

    verified: bool
    reason: str | None = None
    event_id: str | None = None
    beat: int | None = None
    fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class StepVerdict:
    """One step's verdict: verified or rejected (``reason``, ``cause``), and for a verified
    ``believe`` its antecedent (a cited step's index, or a probed record) and transition kind."""

    index: int
    kind: str
    verified: bool
    reason: str | None = None
    cause: str | None = None
    cited_step: int | None = None
    record: RecordAntecedent | None = None
    transition_kind: str | None = None
    observed: Observed | None = None


ObserveCheck = Callable[[Step], Awaitable[Observed]]
RecordProbe = Callable[[str], Awaitable[RecordAntecedent | None]]


def position_of(turn: int, variant: int, first_beat: int, index: int) -> Position:
    """Where step ``index`` of a render whose first beat is ``first_beat`` sits."""
    return Position(turn, variant, 0, first_beat, index)


def beat_steps(beats: Sequence[Any]) -> dict[int, int]:
    """Each beat's owning ``act`` step (beats outside declared steps left out)."""
    return {int(b.beat): int(b.step) for b in beats if getattr(b, "step", 0)}


@dataclass(slots=True)
class StepVerifier:
    """Verifies one render's steps in index order (see the module docstring)."""

    steps: Sequence[Step]
    observe: ObserveCheck
    probe: RecordProbe
    verdicts: dict[int, StepVerdict] = field(default_factory=dict)
    on_verified: Callable[[Step, StepVerdict], None] | None = None

    async def run_before(self, index: int) -> None:
        """Verify every pending step before ``index``."""
        for step in self.steps:
            if step.index >= index:
                break
            if step.index not in self.verdicts:
                await self._verify(step)

    async def run_all(self) -> dict[int, StepVerdict]:
        for step in self.steps:
            if step.index not in self.verdicts:
                await self._verify(step)
        return self.verdicts

    async def _verify(self, step: Step) -> None:
        if step.kind == ACT:
            found = StepVerdict(step.index, ACT, True)
        elif step.kind == OBSERVE:
            found = await self._observe(step)
        else:
            found = await self._believe(step)
        self.verdicts[step.index] = found
        if found.verified and self.on_verified is not None:
            self.on_verified(step, found)

    async def _observe(self, step: Step) -> StepVerdict:
        if step.proposition is None:
            return StepVerdict(step.index, OBSERVE, False, UNNORMALIZABLE)
        seen = await self.observe(step)
        if not seen.verified:
            return StepVerdict(step.index, OBSERVE, False, seen.reason or UNVERIFIED_OBJECT)
        return StepVerdict(step.index, OBSERVE, True, observed=seen)

    async def _believe(self, step: Step) -> StepVerdict:
        def rejected(reason: str, cause: str | None = None) -> StepVerdict:
            return StepVerdict(step.index, BELIEVE, False, reason, cause)

        if not step.step_cites and not step.record_cites:
            return rejected(SELF_ATTESTATION, NO_CITE)
        if any(c >= step.index for c in step.step_cites):
            return rejected(SELF_ATTESTATION, LATER_CITE)
        cited = [self.verdicts.get(c) for c in step.step_cites]
        if any(v is None or not v.verified for v in cited):
            return rejected(SELF_ATTESTATION, UNVERIFIED_CITE)
        if step.proposition is None:
            return rejected(UNNORMALIZABLE)
        records: list[RecordAntecedent] = []
        for record_id in step.record_cites:
            found = await self.probe(record_id)
            if found is None or not found.usable:
                return rejected(ANTECEDENT_INVALID)
            records.append(found)
        if step.step_cites:
            latest = max(step.step_cites)
            kind = self.verdicts[latest].kind
            return StepVerdict(
                step.index, BELIEVE, True, cited_step=latest, transition_kind=STEP_KINDS[kind]
            )
        record = records[0]
        return StepVerdict(
            step.index, BELIEVE, True, record=record, transition_kind=RECORD_KINDS[record.table]
        )

    def rejected(self) -> list[StepVerdict]:
        return [v for v in self.verdicts.values() if not v.verified]

    def report(self) -> dict[str, Any]:
        """``turn_report.render.steps``."""
        return steps_report(len(self.steps), self.verdicts)


def steps_report(declared: int, verdicts: Mapping[int, StepVerdict]) -> dict[str, Any]:
    """``turn_report.render.steps``: counts and the rejected steps' indices and reasons."""
    return {
        "declared": declared,
        "verified": sum(1 for v in verdicts.values() if v.verified),
        "rejected": [
            {"index": v.index, "reason": v.reason, "cause": v.cause}
            for v in sorted(verdicts.values(), key=lambda v: v.index)
            if not v.verified
        ],
    }


def first_citing_act(steps: Sequence[Step], index: int) -> int | None:
    """The first ``act`` step citing step ``index`` (its beats carry the rejected belief)."""
    for step in steps:
        if step.kind == ACT and index in step.step_cites:
            return step.index
    return None


async def probe_any(
    conn: AsyncConnection, story_id: str, record_id: str, *, operation_id: str
) -> RecordAntecedent | None:
    """The record ``record_id`` names in any antecedent table (ids are ULIDs, unique)."""
    for table in RECORD_TABLES:
        found = await probe_record(conn, story_id, table, record_id, operation_id=operation_id)
        if found is not None:
            return found
    return None


@dataclass(frozen=True, slots=True)
class StepOracle:
    """The ``AntecedentOracle`` of one render's verified steps (#160's protocol): a
    ``render_step`` ref of this operation and variant names a verified step; a staged beat is
    the turn's ``TurnStage``'s (#174, ``stage``; ``None`` without one)."""

    operation_id: str
    variant: int
    positions: Mapping[int, Position]
    verified: frozenset[int]
    stage: transitions.AntecedentOracle | None = None

    def refusal(self, ref: Mapping[str, Any]) -> str | None:
        """The stage's refusal of a staged beat (#175: ``simultaneous``), if any."""
        found = getattr(self.stage, "refusal", None)
        reason = None if found is None else found(ref)
        return None if reason is None else str(reason)

    def staged_beat(self, ref: Mapping[str, Any]) -> transitions.StagedRecord | None:
        return None if self.stage is None else self.stage.staged_beat(ref)

    def render_step(self, ref: Mapping[str, Any]) -> transitions.StagedRecord | None:
        if str(ref.get("operation_id")) != self.operation_id:
            return None
        if int(ref.get("variant", -1)) != self.variant:
            return None
        index = int(ref.get("step", -1))
        position = self.positions.get(index)
        if (
            position is None
            or index not in self.verified
            or int(ref.get("beat", -1)) != (position.beat)
        ):
            return None
        return transitions.StagedRecord(position)


@dataclass(slots=True)
class Written:
    """What ``steps_committed`` wrote: the transitions, the memories and the refusals."""

    transitions: int = 0
    memories: int = 0
    refused: int = 0


async def steps_committed(
    conn: AsyncConnection,
    op: Any,
    *,
    holder: str,
    steps: Sequence[Step],
    verdicts: Mapping[int, StepVerdict],
    first_beat: int,
    beat_owner: Mapping[int, int],
    settings: Any,
    registry: Any,
    remember: Callable[[AsyncConnection, Step, Observed, Position], Awaitable[bool]],
    stage: transitions.AntecedentOracle | None = None,
) -> Written:
    """Write the verified steps (see the module docstring) in the turn's commit, inside its
    transition tally. ``beat_owner`` maps a beat of this render to its ``act`` step (an observed
    fact a beat introduced cites that step); ``remember`` writes an observation's episodic
    memory. A refusal by the antecedent guard is counted, never raised."""
    found = Written()
    turn = int(op.narrative_turn or 0)
    variant = int(op.turn_variant or 0)
    positions = {s.index: position_of(turn, variant, first_beat, s.index) for s in steps}
    oracle = StepOracle(
        str(op.operation_id),
        variant,
        positions,
        frozenset(i for i, v in verdicts.items() if v.verified),
        stage,
    )
    for step in steps:
        verdict = verdicts.get(step.index)
        if verdict is None or not verdict.verified or step.proposition is None:
            continue
        position = positions[step.index]
        source: str | None = None
        if step.kind == OBSERVE:
            seen = verdict.observed
            if seen is None:
                continue
            owner = None if seen.beat is None else beat_owner.get(seen.beat)
            if seen.event_id is not None:
                kind, antecedent = (
                    "observation",
                    transitions.Antecedent.record("events", seen.event_id),
                )
            elif owner is not None:
                kind = "observation"
                antecedent = transitions.Antecedent.render_step(
                    str(op.operation_id), variant, first_beat, owner
                )
            elif seen.fact_id is not None:
                kind, antecedent = "discovery", transitions.Antecedent.record("facts", seen.fact_id)
            else:
                continue  # an earlier beat of the turn outside this render's steps (#174)
            level = BeliefConfidenceLevel.KNOWN
        elif step.kind == BELIEVE:
            kind = str(verdict.transition_kind)
            if verdict.cited_step is not None:
                antecedent = transitions.Antecedent.render_step(
                    str(op.operation_id), variant, first_beat, verdict.cited_step
                )
            elif verdict.record is not None:
                antecedent = transitions.Antecedent.record(verdict.record.table, verdict.record.id)
                if kind == "testimony":
                    source = verdict.record.speaker_id
            else:  # pragma: no cover - a verified believe has an antecedent
                continue
            level = BeliefConfidenceLevel(step.confidence or BeliefConfidenceLevel.BELIEVED.value)
        else:
            continue
        try:
            await transitions.transition(
                conn,
                op,
                holder=holder,
                kind=kind,
                proposition=step.proposition,
                antecedent=antecedent,
                position=position,
                confidence=transitions.Confidence(level),
                source=source if source != holder else None,
                outcome="accept" if kind == "testimony" else None,
                oracle=oracle,
                settings=settings,
                registry=registry,
            )
            found.transitions += 1
        except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
            found.refused += 1  # counted by transition() itself
            continue
        except EpistemicError, InvalidRequest:
            transitions.refused()
            found.refused += 1
            continue
        observed = verdict.observed
        if (
            step.kind == OBSERVE
            and observed is not None
            and await remember(conn, step, observed, position)
        ):
            found.memories += 1
    log.debug(
        "steps: %d transitions, %d memories, %d refused",
        found.transitions,
        found.memories,
        found.refused,
    )
    return found


__all__ = [
    "ACT",
    "BELIEVE",
    "OBSERVE",
    "SELF_ATTESTATION",
    "Observed",
    "Step",
    "StepOracle",
    "StepVerdict",
    "StepVerifier",
    "Written",
    "beat_steps",
    "first_citing_act",
    "position_of",
    "probe_any",
    "steps_committed",
    "steps_report",
]
