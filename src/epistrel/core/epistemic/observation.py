"""Observation transitions from perception routing: seeing is believing (FR-EPI-7, FR-TURN-2,
FR-BELIEF-5; docs/02 §4.4; #164, carried from #160 and #161).

A ``PERCEPTION_SINKS`` entry (``observation_sink``), run in the turn's commit after routing, under
per-character partitions only:

- a ``do`` or a narration a character with a partition perceives at ``content`` fidelity is an
  **observation** of every proposition the beat states (``RouteBeat.claims``): one ``observation``
  transition per (perceiver, proposition), ``known``, citing the beat's event, at the beat's step 1
  (#162's witness rule), its source the acting character when there is one;
- a ``say`` a character perceives only as an **envelope** (a whisper overheard as a whisper, a
  language not understood, a phone call, an aside) is an observation that the speaker spoke:
  ``spoke_to(speaker, target)`` when the envelope names a target, else ``spoke(speaker)``, at the
  beat's story-time. A heard ``say``'s content is testimony (#162) or an exposure (#164), never an
  observation.

A reactive character's narrated reaction (#176) is observed by everyone who perceives it but
its reactor: reactive narration never writes the reactor's beliefs (FR-MULTI-9).

A ``minor``-weight perceiver observes like any other (FR-EPI-10's sparsity filters only its
memory rows, docs/02 §4.4: exposures and observation transitions are unfiltered). The player's
character has no partition and observes nothing. A transition the guards refuse (a
self-attestation, a contradiction already held from that position) is skipped and counted
(``turn_report.epistemic.transitions.refused``). Log lines carry identifiers and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.errors import UnknownPredicate
from epistrel.core.epistemic import partitions, perception, testimony, transitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    EpistemicError,
    SelfAttestation,
)
from epistrel.core.epistemic.partitions import BeliefOperation
from epistrel.core.errors import InvalidRequest
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.registry import load
from epistrel.core.propositions.vocabulary import SPOKE, SPOKE_TO

log = logging.getLogger(__name__)

OBSERVED_KINDS: frozenset[str] = frozenset({"do", "narration"})


def envelope_proposition(envelope: perception.Envelope, at: int) -> Proposition | None:
    """What an envelope perceiver observes: that the speaker spoke (to the target), at
    story-minute ``at``; ``None`` for a sound or an envelope without a speaker."""
    if envelope.kind == "sound" or envelope.speaker is None:
        return None
    when = Instant.of(at, Granularity.MINUTE)
    if envelope.target:
        return Proposition(
            subject_id=envelope.speaker,
            predicate_id=SPOKE_TO,
            object_id=envelope.target[0],
            valid_time=when,
        )
    return Proposition(subject_id=envelope.speaker, predicate_id=SPOKE, valid_time=when)


def _event_id(result: perception.PerceptionResult) -> str | None:
    found = getattr(result.beat.source, "event_id", None)
    return None if found is None else str(found)


async def observe(
    conn: AsyncConnection,
    op: BeliefOperation,
    results: Sequence[perception.PerceptionResult],
    *,
    settings: Any = None,
) -> int:
    """Write the observations of ``results`` (see the module docstring); answers the transitions
    written."""
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    story_id = op.story_id
    if story_id is None or not results:
        return 0
    current = settings
    if current is None:
        current = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(current, transitions.PARTITIONS_MODULE):
        return 0
    registry = await load(conn, story_id)
    holders: dict[str, bool] = {}
    when: int | None = None
    written = 0

    async def holds(character_id: str) -> bool:
        if character_id not in holders:
            holders[character_id] = not await partitions.is_player(conn, story_id, character_id)
        return holders[character_id]

    for result in results:
        event_id = _event_id(result)
        kind = result.beat.kind
        if event_id is None or kind not in (*OBSERVED_KINDS, "say"):
            continue
        position = testimony.witness_position(
            op.narrative_turn or 0, op.turn_variant or 0, result.beat.beat
        )
        antecedent = transitions.Antecedent.record("events", event_id)
        narrated = bool(getattr(result.beat.source, "narrated", False))
        for seen in result.perceived:
            if not await holds(seen.character_id):
                continue
            if narrated and seen.character_id == result.beat.actor:
                # #176 (FR-MULTI-9): a narrated reaction never writes its reactor's beliefs.
                continue
            observed: list[Proposition] = []
            source = result.beat.actor
            if kind in OBSERVED_KINDS and seen.fidelity == perception.CONTENT:
                observed = [p for p in result.beat.claims if isinstance(p, Proposition)]
            elif (
                kind == "say" and seen.fidelity == perception.ENVELOPE and seen.envelope is not None
            ):
                if when is None:
                    from epistrel.core.resolution.context import load_time_basis
                    from epistrel.core.resolution.time import story_time_at

                    when = story_time_at(position.turn, await load_time_basis(conn, story_id))
                found = envelope_proposition(seen.envelope, when)
                observed = [] if found is None else [found]
            for proposition in observed:
                try:
                    await transitions.transition(
                        conn,
                        op,
                        holder=seen.character_id,
                        kind=transitions.Kind.OBSERVATION,
                        proposition=proposition,
                        antecedent=antecedent,
                        position=position,
                        source=source if source != seen.character_id else None,
                        settings=current,
                        registry=registry,
                    )
                    written += 1
                except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
                    continue  # counted by transition() itself
                except EpistemicError, InvalidRequest, UnknownPredicate:
                    transitions.refused()  # a held_already contradiction, an unknown predicate
    log.debug("observations: %d written", written)
    return written


async def observation_sink(
    conn: AsyncConnection,
    operation: perception.SinkOperation,
    results: Sequence[perception.PerceptionResult],
) -> None:
    """The observation sink of ``core.epistemic.perception.PERCEPTION_SINKS`` (``observe``)."""
    await observe(conn, cast(BeliefOperation, operation), results)


perception.register_perception_sink(observation_sink)


__all__ = ["OBSERVED_KINDS", "envelope_proposition", "observation_sink", "observe"]
