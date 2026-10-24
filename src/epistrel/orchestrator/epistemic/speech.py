"""What a turn's first-person claims commit (FR-VERIFY-10, FR-TURN-7,8, FR-EPI-7,13,14,
FR-BELIEF-12; docs/02 §4.9; #166).

The verifier (``orchestrator.verify.verifier``) classified each character ``say`` claim at check
time; ``speech_committed`` writes, in the turn's commit (after the utterances, inside the turn's
transition tally), what those classifications license:

- **The need site** (#164): a speaker's say needs its beliefs about what it says, so the
  speaker's unresolved exposures on each claim's functional key are resolved first
  (``exposure.resolve_needed``, in their own frames, within what the turn's lazy-resolution cap
  leaves after testimony; ``lazy_resolution`` off resolves exposures in their writing turn, so
  nothing waits).
- **A licensed new belief** (``SpeechAct.writes``): the belief is written through #160's
  ``transition()`` with the licence's provenance: ``discovery`` of a canon fact the speaker may
  read, ``inference`` from a held premise, ``testimony`` from the speaker who misinformed it
  (``source``, ``outcome = accept``, at ``believed``: the assertion evidences acceptance), each
  citing the antecedent record; a claim that corrects a contrary belief by a non-testimony
  path is a ``correction``. The dependent step is the say beat ``(turn, variant, 0, beat, step)``
  (its declared ``act`` step, #168; 0 outside declared steps);
  a belief licensed by an earlier hearing holds from it (``held_from``), decided in that frame
  and included from this turn on (``resolution``: FR-BELIEF-12's anchors, as #164's lazy
  resolutions are). A refusal by the antecedent guard is counted, never raised: the claim was
  accepted on a licence the guard re-checks (a race would regenerate the turn through the read
  set).
- **A permitted own backstory** (#170, carried from #166's Discretion): a sincere claim about
  the speaker itself that canon left open and #144 permitted as its own (``Permitted(basis =
  own)``, canonized by this turn) gives the speaker an ``observation`` belief at ``known``
  citing the say's beat event (the event its canonized fact comes from), one step after the say:
  the character knows its own history from then on. A belief it already holds is strengthened in
  place.
- **A lie** writes a ``divergences`` row (``core.epistemic.divergences``): the utterance, the
  asserted proposition and the contrary belief the speaker holds (its strongest, then most
  recent; for a lie about its own true age, canon's ``age`` proposition, FR-CAT-12; none for a
  lie about something no record settles).

A hallucination writes nothing: it was repaired (or surfaced), and the belief set never changes
without an antecedent-backed cause. ``turn_report.epistemic.speech`` = ``{resolutions, licensed,
refused, divergences_written, observed}``.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import divergences, exposure, partitions, transitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    SelfAttestation,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position, step_of
from epistrel.core.propositions.registry import Registry
from epistrel.core.verify import speech_acts
from epistrel.core.verify.permission import Basis, Permitted
from epistrel.core.verify.rules import VerdictKind
from epistrel.orchestrator.verify.extraction import CheckedClaim

log = logging.getLogger(__name__)

SOURCED_KINDS = frozenset({"testimony", "observation", "discovery"})
"""The transition kinds that name a source (``belief_transitions``' CHECK)."""


@dataclass(slots=True)
class Spoken:
    """What ``speech_committed`` wrote (``turn_report.epistemic.speech``)."""

    resolutions: int = 0
    licensed: int = 0
    refused: int = 0
    divergence_ids: list[str] = field(default_factory=list)
    capped: bool = False
    observed: int = 0
    """#170: own-backstory observation beliefs written."""

    def report(self) -> dict[str, int]:
        return {
            "resolutions": self.resolutions,
            "licensed": self.licensed,
            "refused": self.refused,
            "divergences_written": len(self.divergence_ids),
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class SpokenBeat:
    """One say of the turn as the commit knows it: its beat, speaker, utterance id, its claims
    (with their verdicts) and the stored proposition id of each claim (by claim id)."""

    beat: int
    speaker: str
    utterance_id: str | None
    claims: tuple[CheckedClaim, ...]
    proposition_ids: Mapping[str, str]
    event_id: str | None = None


async def speech_committed(
    conn: AsyncConnection,
    op: Any,
    spoken: Sequence[SpokenBeat],
    *,
    settings: Any,
    registry: Registry,
    resolutions_left: int,
) -> Spoken:
    """Write what the turn's classified first-person claims license (see the module docstring)."""
    found = Spoken()
    turn = int(op.narrative_turn or 0)
    variant = int(op.turn_variant or 0)
    budget = exposure.Pass(cap=max(0, resolutions_left))
    lazy = exposure.lazy_on(settings)
    lies: list[divergences.Divergence] = []
    for say in spoken:
        classified = [
            c
            for c in say.claims
            if c.result is not None
            and c.result.speech is not None
            and c.claim.proposition is not None
        ]
        if not classified:
            continue
        position = Position(turn, variant, 0, say.beat, step_of(say.beat, str(op.operation_id)))
        if lazy:
            made = await exposure.resolve_needed(
                conn,
                op,
                say.speaker,
                [c.claim.proposition for c in classified if c.claim.proposition is not None],
                budget=budget,
                settings=settings,
                registry=registry,
            )
            found.resolutions += len(made)
        for c in classified:
            speech = None if c.result is None else c.result.speech
            proposition = c.claim.proposition
            if speech is None or proposition is None:  # pragma: no cover - classified above
                continue
            if speech.writes and not (c.result is not None and c.result.self_attested):
                # #168: a self-attested belief (a rejected render step's) is never written.
                if await _write(
                    conn, op, say.speaker, proposition, speech, position, settings, registry
                ):
                    found.licensed += 1
                else:
                    found.refused += 1
            elif say.event_id is not None and own_backstory(c):
                if await _observe_own(
                    conn, op, say, proposition, position, settings=settings, registry=registry
                ):
                    found.observed += 1
                else:
                    found.refused += 1
            if speech.classification == speech_acts.LIE and say.utterance_id is not None:
                asserted = say.proposition_ids.get(c.claim_id or "")
                if asserted is None:
                    continue
                believed = (
                    None
                    if not speech.contrary
                    else await partitions.read_belief(conn, str(op.story_id), speech.contrary[0])
                )
                lies.append(
                    divergences.Divergence(
                        utterance_id=say.utterance_id,
                        speaker=say.speaker,
                        asserted_proposition_id=asserted,
                        position=position,
                        believed_belief_id=None if believed is None else believed.id,
                        believed_proposition_id=(
                            believed.proposition_id
                            if believed is not None
                            else (speech.known[0] if speech.known else None)
                        ),
                        source_event_id=say.event_id,
                    )
                )
    found.capped = budget.capped
    found.divergence_ids = await divergences.record_divergences(conn, op, lies)
    log.debug(
        "speech: %d licensed, %d refused, %d divergences",
        found.licensed,
        found.refused,
        len(found.divergence_ids),
    )
    return found


def own_backstory(c: CheckedClaim) -> bool:
    """Whether ``c`` is a sincere claim about the speaker itself that canon left open and #144
    permitted as the speaker's own (see the module docstring)."""
    result = c.result
    if result is None or result.speech is None or result.self_attested:
        return False
    speech = result.speech
    return (
        speech.classification == speech_acts.SINCERE
        and not speech.writes
        and result.kind is VerdictKind.NOVEL
        and isinstance(result.permission, Permitted)
        and result.permission.basis is Basis.OWN
    )


async def _observe_own(
    conn: AsyncConnection,
    op: Any,
    say: SpokenBeat,
    proposition: Any,
    position: Position,
    *,
    settings: Any,
    registry: Registry,
) -> bool:
    """The speaker's observation of its own permitted backstory (see the module docstring);
    ``False`` when the guard refused it."""
    assert say.event_id is not None  # noqa: S101 - checked by the caller
    at = Position(
        position.turn, position.variant, position.slot_k, position.beat, position.step + 1
    )
    try:
        await transitions.transition(
            conn,
            op,
            holder=say.speaker,
            kind=transitions.Kind.OBSERVATION,
            proposition=proposition,
            antecedent=transitions.Antecedent.record("events", say.event_id),
            position=at,
            confidence=transitions.Confidence(BeliefConfidenceLevel.KNOWN),
            settings=settings,
            registry=registry,
        )
    except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
        return False
    except InvalidRequest:
        transitions.refused()
        return False
    return True


async def _write(
    conn: AsyncConnection,
    op: Any,
    speaker: str,
    proposition: Any,
    speech: Any,
    position: Position,
    settings: Any,
    registry: Registry,
) -> bool:
    """Write one licensed belief (see the module docstring); ``False`` when the guard refused."""
    licence = speech.licence
    kind = str(licence.kind)
    if speech.decision.write == "correct" and kind != "testimony":
        kind = "correction"
    held_from = licence.held_from
    if held_from is not None and not held_from.before(position):
        held_from = None
    backdated = held_from is not None and held_from.turn < position.turn
    try:
        await transitions.transition(
            conn,
            op,
            holder=speaker,
            kind=kind,
            proposition=proposition,
            antecedent=transitions.Antecedent.record(str(licence.table), str(licence.record_id)),
            position=position,
            held_from=held_from,
            confidence=transitions.Confidence(BeliefConfidenceLevel.BELIEVED),
            source=licence.source if kind in SOURCED_KINDS else None,
            outcome="accept" if kind == "testimony" else None,
            settings=settings,
            registry=registry,
            frame=held_from if backdated else None,
            resolution=backdated,
        )
    except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
        return False  # counted as refused by transition() itself
    except InvalidRequest:
        transitions.refused()
        return False
    return True


__all__ = ["Spoken", "SpokenBeat", "own_backstory", "speech_committed"]
