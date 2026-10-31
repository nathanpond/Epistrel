"""Narrated skips: declared, estimated and bounded (FR-CLOCK-7,8,12; NFR-PERF-7; #199).

A structured turn's renders move the clock by their pacing, by the advances they declare
(#194) and, here, by the elapsed time they narrate without declaring. Each render's beats are
read before they are timed (``candidates``):

- **Declared skips**: a beat's ``time_advance`` (``minutes``, or ``to`` placed by the anchor).
  An engine target (#196's cutoff) and a narrated explicit time ("at 15:10", an anchor
  statement #194 places) are not skips.
- **Undeclared skips**: a ``do`` or narration beat whose prose narrates elapsed time (the
  ``time`` detector's ``elapsed`` hit) with no advance declared (``core.states.completeness
  .undeclared_skips``, #190's diff).

Each is classified and authorized (``core.world_clock.skips``): the player's input (or an
author directive) states or invites it, or a render started it on its own. An undeclared one
the player's input gave an amount for takes that amount (``source = player``); any other
undeclared one is estimated by the ``time_estimation`` purpose (``estimate``: one call per
skip, optional work under #156's budget, pre-allocated so an earlier entailment call cannot
starve it), becoming an advance with ``source = estimate`` and its ``estimate_meta``
(``{reason, confidence, model, prompt_hash, initiated_by}``). A model-initiated skip that could
never stand (the cap is 0, or no established activity) is refused without the call.

What a plan decides (``Decision``): **accept** (the advance stays, or joins the beat, with
``initiated_by``); **refuse** (FR-CLOCK-8: the beat is repaired to stay in the present, repair
reason ``unprompted_skip``); **unestimated** (no call fit, none answered, or its confidence was
under ``MIN_CONFIDENCE``: the span is removed deterministically, no model repair and no guessed
duration, degradation ``unestimated_skip_removed``, NFR-PERF-7 (5)). Accepted skips are then
timed (#194) and checked against the due events (#196's ``check_advance``).

Replay folds the recorded advances and never re-estimates (FR-CLOCK-12): nothing here runs
outside a live render; a regenerated turn renders and estimates again.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, Literal

from epistrel.core.enums import StateChangeSource
from epistrel.core.propositions.model import TemporalKind
from epistrel.core.propositions.registry import Registry
from epistrel.core.readset import KeyedLookup
from epistrel.core.states import completeness
from epistrel.core.states.effects import UNESTIMATED_SKIP, UNPROMPTED_SKIP, Finding
from epistrel.core.states.model import WORLD_CLOCK
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.world_clock import skips
from epistrel.core.world_clock.timing import next_offset
from epistrel.orchestrator.budget import TIME_ESTIMATION as TIME_ESTIMATION_KIND
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.prompt import prompt_hash
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.config import Route
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    TIME_ESTIMATION,
    TIME_ESTIMATION_MAX_TIMES,
    TIME_ESTIMATION_TEXT_MAX,
    TimeEstimate,
    TimeEstimationInputs,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.structured import planner
from epistrel.orchestrator.structured.clocking import ClockRead

log = logging.getLogger(__name__)

UNESTIMATED_SKIP_REMOVED = planner.UNESTIMATED_SKIP_REMOVED
"""The degradation step of an undeclared skip removed for want of an estimate (NFR-PERF-7 (5))."""
TIME_ESTIMATION_PURPOSE = TIME_ESTIMATION.name
"""The purpose's name (a story's route for it, ``models.purposes.time_estimation``)."""
UNPROMPTED = UNPROMPTED_SKIP
"""The repair reason (``turn_report.repairs[].tag``) of a refused model-initiated skip."""


@dataclass(frozen=True, slots=True)
class Estimate:
    """One ``time_estimation`` answer the engine took: the minutes, the model's reason and
    confidence, the model that answered and the prompt's hash."""

    minutes: int
    reason: str
    confidence: float
    model: str
    prompt_hash: str

    def meta(self, initiated_by: str) -> dict[str, Any]:
        """The change row's ``estimate_meta``."""
        return {
            "reason": self.reason,
            "confidence": self.confidence,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "initiated_by": initiated_by,
        }


def inputs_of(
    narrated_text: str, activity_context: str = "", established_times: Sequence[str] = ()
) -> TimeEstimationInputs:
    """A ``time_estimation`` request, each field cut to its bound."""
    return TimeEstimationInputs(
        narrated_text=narrated_text[:TIME_ESTIMATION_TEXT_MAX],
        activity_context=activity_context[:TIME_ESTIMATION_TEXT_MAX],
        established_times=[t[:TIME_ESTIMATION_TEXT_MAX] for t in established_times][
            -TIME_ESTIMATION_MAX_TIMES:
        ],
    )


async def estimate(
    models: ModelAccessLayer,
    *,
    narrated_text: str,
    activity_context: str = "",
    established_times: Sequence[str] = (),
    budget: TurnBudget,
    route: Route | None = None,
) -> Estimate | None:
    """One ``time_estimation`` call for an undeclared skip (FR-CLOCK-7): the estimate, or
    ``None`` when the model gave none the engine takes (an error, a refused budget, an
    unparsable answer, 0 minutes, or a confidence under ``skips.MIN_CONFIDENCE``). The caller
    has already asked the budget (``TurnBudget.try_optional``); #201's E-09 calls it directly
    with an unbounded budget."""
    inputs = inputs_of(narrated_text, activity_context, established_times)
    _, _, messages = models.prepare(TIME_ESTIMATION.name, inputs)
    hashed = prompt_hash(messages)
    try:
        scope = nullcontext() if route is None else routing({TIME_ESTIMATION.name: route})
        with scope:
            found = await models.structured_call(
                TIME_ESTIMATION.name,
                TimeEstimate,
                inputs,
                params=None if route is None else route.params,
                deterministic_first=lambda: None,
                budget=budget,
            )
            _, model, _ = await models.endpoint(TIME_ESTIMATION.name)
    except ModelError:
        return None
    if isinstance(found, Uncertain):
        return None
    if found.minutes <= 0 or found.confidence < skips.MIN_CONFIDENCE:
        return None
    # The configured model; with none configured, the adapter that answered (the stub's name).
    model = model or models.adapter.name
    return Estimate(found.minutes, found.reason, found.confidence, model, hashed)


# -- the plan ------------------------------------------------------------------------------------

Outcome = Literal["accept", "refuse", "unestimated", "estimate"]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A skip one beat narrates: the beat, its advance's index among the beat's advances
    (``None``: undeclared), the sentence that narrates it (``None``: the whole beat), the text
    authority reads, its reason and its minutes (``None``: not known yet)."""

    beat: int
    index: int | None
    span: tuple[int, int] | None
    text: str
    reason: str = ""
    minutes: int | None = None

    @property
    def declared(self) -> bool:
        return self.index is not None


@dataclass(frozen=True, slots=True)
class Decision:
    """What the plan decided for a candidate (see the module docstring): ``accept`` with the
    advance the beat keeps or gains, ``refuse`` with its reason, ``unestimated``, or
    ``estimate`` (an undeclared skip waiting for its ``time_estimation`` call)."""

    candidate: Candidate
    outcome: Outcome
    classified: skips.Classified
    advance: DeclaredAdvance | None = None
    reason: str = ""


def _declared_minutes(advance: DeclaredAdvance, clock: ClockRead) -> int | None:
    if advance.minutes is not None:
        return int(advance.minutes)
    if advance.to is not None and clock.minute is not None:
        wanted = int(advance.to[:2]) * 60 + int(advance.to[3:])
        target = next_offset(
            wanted,
            at_offset=clock.start,
            at_minute=clock.minute,
            day_minutes=clock.clockwork.day_minutes,
        )
        return target - clock.start
    return None


def candidates(beats: Sequence[TurnBeat], clock: ClockRead) -> list[Candidate]:
    """The skips ``beats`` narrate (see the module docstring), in beat order."""
    found: list[Candidate] = []
    views = []
    for b in sorted(beats, key=lambda b: b.beat):
        if b.beat <= 1 or b.flashback or b.attempt:
            continue
        hits = completeness.undeclared_skips(
            [completeness.Beat(b.beat, b.kind, b.text, flashback=b.flashback)]
        )
        sentence = hits[0].span if hits else None
        for n, advance in enumerate(b.advances):
            if advance.to_offset is not None or advance.source is not None:
                continue  # an engine target (#196) or an advance the engine supplied
            minutes = _declared_minutes(advance, clock)
            if minutes is None:
                continue
            found.append(Candidate(b.beat, n, sentence, b.text, advance.reason, minutes))
        views.append(
            completeness.Beat(
                b.beat, b.kind, b.text, advances=len(b.advances), flashback=b.flashback
            )
        )
    for skip in completeness.undeclared_skips(views):
        beat = next(b for b in beats if b.beat == skip.beat)
        start, end = skip.span
        found.append(Candidate(skip.beat, None, skip.span, beat.text[start:end]))
    return sorted(found, key=lambda c: (c.beat, c.index is None))


def undeclared(beats: Sequence[TurnBeat]) -> int:
    """How many undeclared skips ``beats`` narrate: the ``time_estimation`` calls a render may
    need, pre-allocated before its verification (``TurnBudget.pending``)."""
    return len(
        completeness.undeclared_skips(
            [
                completeness.Beat(
                    b.beat, b.kind, b.text, advances=len(b.advances), flashback=b.flashback
                )
                for b in beats
                if b.beat > 1 and not b.attempt
            ]
        )
    )


def decide(
    candidate: Candidate,
    *,
    player_input: str,
    directives: Sequence[str] = (),
    cap: int,
    established: frozenset[str],
    estimated: Estimate | None = None,
) -> Decision:
    """The plan for one candidate (``estimated``: its estimate, once made)."""
    classified = skips.classify_skip(
        skips.Skip(candidate.text, candidate.reason, candidate.minutes), player_input, directives
    )
    by = classified.initiated_by
    if candidate.declared:
        assert candidate.minutes is not None  # noqa: S101 - a declared skip's minutes are known
        verdict = skips.authorize(
            classified, minutes=candidate.minutes, cap=cap, established=established
        )
        if isinstance(verdict, skips.Refused):
            return Decision(candidate, "refuse", classified, reason=verdict.reason)
        return Decision(candidate, "accept", classified)
    if isinstance(classified, skips.PlayerInitiated) and classified.minutes is not None:
        # The player's stated amount (FR-CLOCK-8's ``source = player``): no estimate needed.
        advance = DeclaredAdvance(
            "the player's stated time",
            minutes=classified.minutes,
            source=StateChangeSource.PLAYER.value,
            meta={"initiated_by": by},
        )
        return Decision(candidate, "accept", classified, advance)
    if isinstance(classified, skips.ModelInitiated):
        # A skip no duration could make acceptable is refused without its call.
        never = skips.authorize(classified, minutes=0, cap=cap, established=established)
        if isinstance(never, skips.Refused):
            return Decision(candidate, "refuse", classified, reason=never.reason)
    if estimated is None:
        return Decision(candidate, "estimate", classified)
    verdict = skips.authorize(
        classified, minutes=estimated.minutes, cap=cap, established=established
    )
    if isinstance(verdict, skips.Refused):
        return Decision(candidate, "refuse", classified, reason=verdict.reason)
    advance = DeclaredAdvance(
        estimated.reason[:200] or "estimated",
        minutes=estimated.minutes,
        source=StateChangeSource.ESTIMATE.value,
        meta=estimated.meta(by),
    )
    return Decision(candidate, "accept", classified, advance)


def unestimated(candidate: Candidate, classified: skips.Classified) -> Decision:
    """An undeclared skip the turn could not estimate."""
    return Decision(candidate, "unestimated", classified)


def applied(beat: TurnBeat, decisions: Sequence[Decision]) -> TurnBeat:
    """``beat`` with its accepted decisions applied: a declared skip stamped with who
    initiated it, an undeclared one gaining its advance (the beat's own step)."""
    advances = list(beat.advances)
    for d in decisions:
        if d.outcome != "accept" or d.candidate.beat != beat.beat:
            continue
        if d.candidate.index is not None:
            kept = advances[d.candidate.index]
            advances[d.candidate.index] = replace(
                kept, meta={**dict(kept.meta or {}), "initiated_by": d.classified.initiated_by}
            )
        elif d.advance is not None:
            advances.append(d.advance)
    return replace(beat, advances=tuple(advances))


def finding_of(decision: Decision) -> Finding:
    """The repair ladder's finding for a refused (``unprompted_skip``) or unestimated
    (``unestimated_skip``) skip: its sentence (the whole beat when it has none) and the line
    the regeneration is constrained by."""
    candidate = decision.candidate
    if decision.outcome == "unestimated":
        return Finding(UNESTIMATED_SKIP, candidate.beat, WORLD_CLOCK, span=candidate.span)
    return Finding(
        UNPROMPTED_SKIP,
        candidate.beat,
        WORLD_CLOCK,
        constraint=decision.reason,
        span=candidate.span,
        line=(
            "Stay in the present: do not skip time here. Nothing established in the scene "
            "lets this beat move the clock on its own; narrate only what happens now."
        ),
    )


def stripped(beat: TurnBeat) -> TurnBeat:
    """``beat`` with no skip of its own: only an engine target (#196's cutoff) is kept."""
    kept = tuple(a for a in beat.advances if a.to_offset is not None)
    return beat if kept == beat.advances else replace(beat, advances=kept)


# -- what the scene established ------------------------------------------------------------------


async def fluent_texts(
    lookup: KeyedLookup,
    story_id: str,
    registry: Registry,
    *,
    subjects: Sequence[str],
    story_time: int,
) -> list[str]:
    """The current ``fluent`` canon facts about ``subjects`` whose predicate names an activity
    (``skips.ACTIVITY_LEXICON``), each as its predicate's name: what the scene established as
    under way ("watching", "driving"). A keyed read per such predicate (none: no read)."""
    found: list[str] = []
    if not subjects:
        return found
    for predicate in registry.predicates.values():
        if predicate.temporal_kind is not TemporalKind.FLUENT:
            continue
        name = predicate.name.replace("_", " ")
        if not skips.activities(name):
            continue
        facts = await lookup.canon_at(
            story_id,
            predicate=predicate.id,
            subjects=tuple(subjects),
            story_time=story_time,
            registry=registry,
        )
        if facts:
            found.append(name)
    return found


def established_activities(earlier: Iterable[str], fluents: Iterable[str] = ()) -> frozenset[str]:
    """The scene's established activities: its earlier ``do`` beats' and fluents' texts."""
    return skips.established([*earlier, *fluents])


def report(decisions: Sequence[Decision]) -> dict[str, int]:
    """``turn_report.clock.skips``: counts only (invariant 3)."""
    return {
        "accepted": sum(1 for d in decisions if d.outcome == "accept"),
        "estimated": sum(
            1
            for d in decisions
            if d.outcome == "accept"
            and d.advance is not None
            and d.advance.source == StateChangeSource.ESTIMATE.value
        ),
        "refused": sum(1 for d in decisions if d.outcome == "refuse"),
        "unestimated": sum(1 for d in decisions if d.outcome == "unestimated"),
    }


def mapping(decisions: Sequence[Decision]) -> Mapping[int, list[Decision]]:
    """The decisions by beat."""
    found: dict[int, list[Decision]] = {}
    for d in decisions:
        found.setdefault(d.candidate.beat, []).append(d)
    return found


__all__ = [
    "TIME_ESTIMATION_KIND",
    "TIME_ESTIMATION_PURPOSE",
    "UNESTIMATED_SKIP_REMOVED",
    "UNPROMPTED",
    "Candidate",
    "Decision",
    "Estimate",
    "applied",
    "candidates",
    "decide",
    "established_activities",
    "estimate",
    "finding_of",
    "fluent_texts",
    "inputs_of",
    "mapping",
    "report",
    "stripped",
    "undeclared",
    "unestimated",
]
