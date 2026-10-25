"""Salience: content placed to be noticed triggers commitment, texture needs none (FR-COMMIT-3,7,
13; docs/02 §4.9's model-dependent, measured list; #146).

**Detection** (``detect_salience(beats, scene_context, …)``: the E-05 entry point, #157). A
render's observable beats (``say``, ``do``, narration; never ``think``, ``latent`` or the
player's input) are read once per render:

- under the profile's ``commitment.salience = rules`` no model is called: a reference is
  salient exactly when it is a detected open-slot claim on a ``do`` or narration beat
  (``rules_salient``; #148's claims), and the model verdict is empty;
- under ``rules_and_model`` the ``salience_detection`` purpose is called through the Model
  Access Layer's ``structured_call`` (hot path, parse-validate-retry), with only the beats and
  the scene (the names present and the last ``SALIENCE_LAST_BEATS`` beats). The model quotes
  each span's words and the engine locates the quote in the beat it names (#461, with the
  extraction backstop's ``locate_quote``): small models place character offsets poorly. A quote
  not found in that beat (or naming a beat it was not shown) is never mapped to a guessed span:
  it is dropped into the verdict's ``unlocated``, counted in the log.

Salience is optional work (#156 reserves nothing for it): with no model, a spent budget, an
unreachable model or an unparseable answer every beat is texture and the verdict says why in
``degradation`` (``salience_unavailable``, ``salience_skipped``, ``salience_failed``). The
stub's default answer is all texture.

**Recording** (``record_salience``, in the turn's commit step): each salient span's proposition
is normalized with one open argument (``core.commit.sketch.open_sketch``, #139/#140): a
reference with no open argument is a plain claim (#148/#140 verify it), an unnormalizable one is
texture, one canon already settles (a committed or hidden fact fills it,
``core.commit.matching.settled``) owes nothing, and every other one introduces (or finds) its
thunk and gets **one ledger entry per proposition** (``lifecycle.introduce(salient=True)``):
"someone knocked" is the entry; who knocked and why are only obligations when made salient
themselves. Salience runs before dependence within a render, so a reference made salient on
beat 2 can be settled by beat 3's ``do``.

The turn's close (``lifecycle.close_turn``) settles what is left: #150's follow-up render when
the turn can afford it, else the deterministic minimal commit. Logs carry counts only
(invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import FactOperation
from epistrel.core.commit import lifecycle, matching
from epistrel.core.commit.sketch import ThunkSketch, open_phrase, open_sketch, valid_time_of
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.context import load_context, load_time_basis
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.resolution.identity import speaker_identities
from epistrel.core.resolution.time import Position, turn_of
from epistrel.core.verify.normalize import ClaimSketch
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.config import Route
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelUnavailable
from epistrel.orchestrator.models.registry import (
    SALIENCE_DETECTION,
    SALIENCE_LAST_BEATS,
    SALIENCE_MAX_BEATS,
    SalienceBeat,
    SalienceInputs,
    SalienceQuote,
    SalienceResult,
    SalienceSketch,
    SalienceSpan,
    SalientQuote,
    SalientSpan,
    SceneContext,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.verify.extraction import locate_quote

log = logging.getLogger(__name__)

SalienceMode = Literal["rules", "rules_and_model"]
BeatKind = Literal["say", "do", "narration"]
SALIENCE_KINDS: frozenset[str] = frozenset({"say", "do", "narration"})
"""The beats salience reads (observable ones; ``think``, ``latent`` and player input never)."""
RULE_KINDS: frozenset[str] = frozenset({"do", "narration"})
"""Where the rules mode finds salience: open-slot claims on these beats."""

SALIENCE_UNAVAILABLE = "salience_unavailable"
SALIENCE_SKIPPED = "salience_skipped"
SALIENCE_FAILED = "salience_failed"


def salience_mode(settings: ProfileSettings) -> SalienceMode | None:
    """The profile's salience mode, or ``None`` when commitment does not run."""
    if not lifecycle.commitment_enabled(settings):
        return None
    return settings.commitment.salience


@dataclass(frozen=True, slots=True)
class Beat:
    """One beat of a render as salience reads it: its index in the turn, its kind, the
    speaking character's entity (``None`` for narration) and name, and its text."""

    index: int
    kind: str
    text: str
    speaker_entity: str | None = None
    speaker_name: str | None = None


@dataclass(frozen=True, slots=True)
class SalienceVerdict:
    """What one render's salience detection found: the located salient spans (with the
    proposition each raises), the located texture spans, the model's quotes the engine could
    not find in the beat they name (``unlocated``: dropped, never guessed; #461), whether the
    model was called, and why not."""

    salient: tuple[SalientSpan, ...] = ()
    texture: tuple[SalienceSpan, ...] = ()
    unlocated: tuple[SalientQuote | SalienceQuote, ...] = ()
    called: bool = False
    degradation: tuple[str, ...] = ()
    characters: tuple[str, ...] = ()
    """The figures the model singled out as characters to model (#163's salience trigger), as
    the words naming them; ``core.epistemic.modeling.candidates`` finds each in its beat."""


def _inputs(beats: Sequence[Beat], scene_context: SceneContext) -> SalienceInputs:
    read = [b for b in beats if b.kind in SALIENCE_KINDS][:SALIENCE_MAX_BEATS]
    return SalienceInputs(
        beats=[
            SalienceBeat(
                index=b.index,
                kind=cast(BeatKind, b.kind),
                speaker=b.speaker_name,
                text=b.text[:4_000],
            )
            for b in read
        ],
        scene_context=SceneContext(
            present_entities=list(scene_context.present_entities),
            last_beats=list(scene_context.last_beats)[-SALIENCE_LAST_BEATS:],
        ),
    )


_QUOTE_MARKS = "\"'\u201c\u201d\u2018\u2019"


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """``quote`` located in ``text``; failing that, the quote without the surrounding spaces
    or quotation marks a model may add."""
    found = locate_quote(text, quote)
    if found is None:
        bare = quote.strip().strip(_QUOTE_MARKS).strip()
        if bare and bare != quote:
            found = locate_quote(text, bare)
    return found


def _located(result: SalienceResult, beats: Sequence[Beat]) -> SalienceVerdict:
    """The model's quotes located in the beats it was shown; a quote not found in the beat it
    names is ``unlocated``."""
    texts = {b.index: b.text for b in beats if b.kind in SALIENCE_KINDS}

    def where(quote: SalientQuote | SalienceQuote) -> SalienceSpan | None:
        text = texts.get(quote.beat)
        found = None if text is None else _locate(text, quote.span)
        return (
            None if found is None else SalienceSpan(beat=quote.beat, start=found[0], end=found[1])
        )

    salient: list[SalientSpan] = []
    texture: list[SalienceSpan] = []
    unlocated: list[SalientQuote | SalienceQuote] = []
    for item in result.salient:
        span = where(item)
        if span is None:
            unlocated.append(item)
        else:
            salient.append(SalientSpan(span=span, proposition_sketch=item.proposition_sketch))
    for quote in result.texture:
        span = where(quote)
        if span is None:
            unlocated.append(quote)
        else:
            texture.append(span)
    return SalienceVerdict(
        salient=tuple(salient),
        texture=tuple(texture),
        unlocated=tuple(unlocated),
        called=True,
        characters=tuple(dict.fromkeys(c.strip() for c in result.characters if c.strip())),
    )


async def detect_salience(
    beats: Sequence[Beat],
    scene_context: SceneContext,
    *,
    mode: SalienceMode = "rules_and_model",
    models: ModelAccessLayer | None = None,
    budget: TurnBudget | None = None,
    routes: Mapping[str, Route] | None = None,
) -> SalienceVerdict:
    """One render's salience (see the module docstring)."""
    inputs = _inputs(beats, scene_context)
    if mode == "rules" or not inputs.beats:
        return SalienceVerdict()
    if models is None:
        return SalienceVerdict(degradation=(SALIENCE_UNAVAILABLE,))
    route = (routes or {}).get(SALIENCE_DETECTION.name)
    try:
        with routing(routes or {}):
            answer = await models.structured_call(
                SALIENCE_DETECTION.name,
                SalienceResult,
                inputs,
                params=route.params if route is not None else None,
                deterministic_first=lambda: None,
                budget=budget or TurnBudget(),
            )
    except BudgetExhausted:
        return SalienceVerdict(degradation=(SALIENCE_SKIPPED,))
    except ModelUnavailable:
        return SalienceVerdict(called=True, degradation=(SALIENCE_FAILED,))
    if isinstance(answer, Uncertain):
        reason = SALIENCE_SKIPPED if answer.reason == "budget_exhausted" else SALIENCE_FAILED
        return SalienceVerdict(called=answer.attempts > 0, degradation=(reason,))
    found = _located(answer, beats)
    log.debug(
        "salience: %d salient, %d texture, %d unlocated",
        len(found.salient),
        len(found.texture),
        len(found.unlocated),
    )
    return found


def rules_salient(claims: Sequence[tuple[Beat, ClaimSketch]]) -> list[tuple[Beat, ClaimSketch]]:
    """The rules mode's salient references: the detected claims (#148's sketches, with their
    beats) on ``do`` and narration beats that leave an argument open."""
    return [
        (beat, claim)
        for beat, claim in claims
        if beat.kind in RULE_KINDS
        and (
            (claim.subject_id is None and open_phrase(claim.subject_text))
            or (
                claim.object_id is None and claim.literal is None and open_phrase(claim.object_text)
            )
        )
    ]


Outcome = Literal["entry", "owed", "settled", "determined", "texture", "inert"]


@dataclass(frozen=True, slots=True)
class Recorded:
    """What one salient reference became: ``entry`` (a new ledger entry on ``thunk_id``),
    ``owed`` (its thunk already has an undischarged entry), ``settled`` (canon already holds a
    value), ``determined`` (no open argument: a plain claim), ``texture`` (not normalizable) or
    ``inert`` (commitment off)."""

    beat: int
    outcome: Outcome
    thunk_id: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class SalienceReport:
    """``turn_report.commit.salience``: one line per salient reference, and the verdict's
    degradation."""

    recorded: list[Recorded] = field(default_factory=list)
    degradation: list[str] = field(default_factory=list)

    @property
    def entries(self) -> int:
        return sum(1 for r in self.recorded if r.outcome == "entry")


def _claim_of(sketch: SalienceSketch | ClaimSketch) -> ClaimSketch:
    if isinstance(sketch, ClaimSketch):
        return sketch
    return ClaimSketch(
        subject_text=sketch.subject,
        relation_text=sketch.relation,
        object_text=sketch.object,
        time_text=sketch.time,
    )


async def record_salience(
    conn: AsyncConnection,
    operation: FactOperation,
    references: Sequence[tuple[Beat, SalienceSketch | ClaimSketch, tuple[int, int] | None]],
    *,
    read_set: StructuredReadSet,
    position: Position,
    registry: Registry | None = None,
    on: bool | None = None,
) -> SalienceReport:
    """Record a render's salient references in ``operation``'s commit step (see the module
    docstring): each ``(beat, proposition, span)`` from ``detect_salience`` (the model's
    sketch) or ``rules_salient`` (#148's claim)."""
    report = SalienceReport()
    story_id = operation.story_id or ""
    if not await lifecycle.resolve_on(conn, story_id, on):
        report.recorded = [Recorded(b.index, "inert") for b, _, _ in references]
        return report
    registry = registry or await load(conn, story_id)
    basis = await load_time_basis(conn, story_id)
    lookup = KeyedLookup(conn, read_set)
    for beat, proposition, span in references:
        speaker = beat.speaker_entity or NARRATOR
        context = await load_context(
            conn,
            story_id,
            position=position,
            speaker=speaker,
            lookup=lookup,
            # #546: holder-aware mentions (the speaker's ``same_as``).
            identities=await speaker_identities(
                lookup, story_id, speaker, through_position=(turn_of(position), 0)
            ),
        )
        claim = _claim_of(proposition)
        found = open_sketch(
            claim,
            speaker,
            registry=registry,
            context=context,
            span=None if span is None else (beat.index, span[0], span[1]),
        )
        if found is None:
            report.recorded.append(Recorded(beat.index, "determined"))
            continue
        if not isinstance(found, ThunkSketch):
            report.recorded.append(Recorded(beat.index, "texture", reason=found.detail))
            continue
        if await matching.settled(lookup, story_id, found):
            report.recorded.append(Recorded(beat.index, "settled"))
            continue
        when = valid_time_of(claim, position, basis)
        valid_time = when if isinstance(when, int) else None
        existing = await lifecycle.find_open(conn, story_id, found)
        owed = existing is not None and bool(
            await lifecycle.entries_of(conn, story_id, existing.id, undischarged=True)
        )
        thunk = await lifecycle.introduce(
            conn,
            operation,
            found,
            valid_time=valid_time,
            beat=beat.index,
            salient=True,
            on=True,
        )
        if thunk is None:  # pragma: no cover - on=True
            continue
        report.recorded.append(Recorded(beat.index, "owed" if owed else "entry", thunk.id))
    log.debug(
        "salience recorded: %d references, %d new entries", len(report.recorded), report.entries
    )
    return report


__all__ = [
    "RULE_KINDS",
    "SALIENCE_FAILED",
    "SALIENCE_KINDS",
    "SALIENCE_SKIPPED",
    "SALIENCE_UNAVAILABLE",
    "Beat",
    "Recorded",
    "SalienceMode",
    "SalienceReport",
    "SalienceVerdict",
    "detect_salience",
    "record_salience",
    "rules_salient",
    "salience_mode",
]
