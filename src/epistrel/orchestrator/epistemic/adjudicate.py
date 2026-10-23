"""Testimony in a structured turn: who witnesses each say, which witnesses are salient, the
``belief_adjudication`` purpose for them under the optional pool, and the commit-time writes
(FR-BELIEF-1,3,9, FR-TURN-7, NFR-PERF-7 step 3; #162).

**Witnesses.** A say's witnesses are the characters #161's routing gives its *content*
(``fidelity = content``): the speaker is no witness of its own words, the player's character has
no partition (FR-EPI-12) and an envelope (a whisper overheard as a whisper, a language not
understood) carries no claim. A render-limited eavesdropper hears only the claims whose words
fall inside its ``partial_text``.

**Salience** (FR-BELIEF-9: belief formation proportionate to salience). A witness is salient
when the say's beat carries a salient span (#146's salience step) and the witness is a primary
or reactive character (never silent, #176's turn roles; never a minor), or when a claim's
subject touches the witness's partition (it already holds something about it). Salient and
addressed witnesses (the say's ``addressee`` or ``whisper_to``) are resolved now, by the default
or, when adjudicated, by the model; every other witness is left to #164's exposure record and lazy
resolver (``deferred`` in the report).

**Adjudication** (``adjudicate_turn``, before composition). Only under ``planner.adjudicates``
(partitions, ``character_knowledge.testimony = model``, model salience): for salient witnesses,
primaries before reactive and in beat order, at most ``MAX_ADJUDICATIONS_PER_TURN`` calls per
turn (``character_knowledge.max_adjudications_per_turn`` once #181 adds the leaf), one per
(witness, beat) batching the beat's claims. A call is optional work: the pool must afford it
(``TurnBudget.try_optional("testimony", 1)``, after follow-up and before repair; nothing is
reserved for it). A witness the cap or the budget leaves out is decided by the default and the
turn lists ``testimony_defaulted`` (NFR-PERF-7 step 3); a failed call lists
``adjudication_unavailable``. Nothing is listed under ``testimony = defaulted``. A call that
could change nothing is not made: a joke, a say with no claim, or a claim the witness's records
contradict (the model never overrides a ``contradicted`` verdict, nor accepts a joke).

**At commit** (``testify_committed``, after routing): the default is recomputed over the
committed state for each resolved witness (``core.epistemic.testimony``), the adjudicated
decision applied where the guards allow (``apply_adjudication``: confidence 0.6 or more), and the
outcomes written. ``turn_report.epistemic.testimony`` = ``{witnesses, adjudicated, defaulted,
deferred, outcomes_by_kind}``; ``epistrel_adjudications_total{result}`` counts the calls.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import BeliefKind, ExposureFidelity
from epistrel.core.epistemic import exposure, modeling, partitions, perception, testimony
from epistrel.core.epistemic import scene as scene_loader
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry, canonical, load
from epistrel.core.propositions.store import write_proposition
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables import characters, entities, events
from epistrel.core.verify.rules import VerdictKind
from epistrel.observability.metrics import ADJUDICATIONS
from epistrel.orchestrator.budget import TESTIMONY, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer, routing
from epistrel.orchestrator.models.config import routes_of
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelUnavailable
from epistrel.orchestrator.models.registry import (
    ADJUDICATION_MAX_CLAIMS,
    ADJUDICATION_MAX_RECORDS,
    BELIEF_ADJUDICATION,
    AdjudicationBelief,
    AdjudicationPriors,
    AdjudicationUtterance,
    BeliefAdjudicationInputs,
    BeliefAdjudicationResult,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.structured import planner
from epistrel.orchestrator.verify.verifier import render_proposition

log = logging.getLogger(__name__)

MAX_ADJUDICATIONS_PER_TURN = 2
"""The most ``belief_adjudication`` calls one turn makes (#181 adds the profile leaf
``character_knowledge.max_adjudications_per_turn``)."""

TEXT_MAX = 1_000


def max_adjudications(settings: ProfileSettings) -> int:
    found = getattr(settings.character_knowledge, "max_adjudications_per_turn", None)
    return MAX_ADJUDICATIONS_PER_TURN if not isinstance(found, int) else max(0, found)


# -- the says and their witnesses --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """One proposition a say carries, with the words that say it (``None``: not located) and,
    for an attribution (#167), the character it is attributed to (``attributed_to``) and its
    mode."""

    proposition: Proposition
    words: str | None = None
    attributed_to: str | None = None
    mode: str = "believes"
    factive: bool = False

    @property
    def asserted(self) -> bool:
        """Whether the speaker asserts the proposition itself: a plain claim, a factive
        attribution, or a self-report of belief ("I figured …": trust-gated, #167)."""
        return self.attributed_to is None or self.factive


@dataclass(frozen=True, slots=True)
class Heard:
    """One say as its witnesses take it: the speaker, the beat, the words, the declared intent,
    the claims and the perception qualifiers."""

    speaker: str
    beat: int
    text: str
    intent: str
    claims: tuple[Claim, ...]
    qualifiers: perception.Qualifiers = field(default_factory=perception.Qualifiers)

    @property
    def addressed(self) -> frozenset[str]:
        q = self.qualifiers
        return frozenset({*q.whisper_to, *((q.addressee,) if q.addressee else ())})

    def heard_claims(self, partial: str | None) -> tuple[Claim, ...]:
        """The claims a witness caught (``partial``: only those whose words fall in it)."""
        if partial is None:
            return self.claims
        caught = partial.casefold()
        return tuple(c for c in self.claims if c.words and c.words.casefold() in caught)

    def attributions(self, partial: str | None) -> list[tuple[Proposition, str]]:
        """The belief attributions a witness caught (#167): ``(proposition, attributed holder)``
        for each ``believes`` attribution claim with a resolved holder."""
        return [
            (c.proposition, c.attributed_to)
            for c in self.heard_claims(partial)
            if c.attributed_to is not None and c.mode == "believes"
        ]

    def route_beat(self) -> perception.RouteBeat:
        return perception.RouteBeat(
            kind="say", beat=self.beat, actor=self.speaker, qualifiers=self.qualifiers
        )

    def speech_act(self, event_id: str | None, partial: str | None) -> testimony.SpeechAct:
        """The speech act a witness perceived (``partial``: only the claims whose words fall in
        what it caught)."""
        claims = self.heard_claims(partial)
        claims = tuple(
            c
            for c in claims
            if c.asserted or (c.attributed_to == self.speaker and c.mode == "believes")
        )
        return testimony.SpeechAct(
            speaker=self.speaker,
            beat=self.beat,
            propositions=tuple(c.proposition for c in claims),
            event_id=event_id,
            text=self.text if partial is None else partial,
            manner=self.qualifiers.manner,
        )


def _words(text: str, span: tuple[int, int] | None) -> str | None:
    if span is None:
        return None
    start, end = span
    found = text[start:end].strip()
    return found or None


def heard_of(checked: Any, actor: str, say: str | None) -> list[Heard]:
    """The turn's says (the player's beat 1 and every rendered ``say`` with a speaker)."""
    found: list[Heard] = []
    if say:
        found.append(
            Heard(
                speaker=actor,
                beat=1,
                text=say,
                intent="unspecified",
                claims=tuple(
                    Claim(c.proposition, _words(say, c.span))
                    for c in checked.input_claims
                    if c.proposition is not None
                ),
            )
        )
    for beat in checked.beats:
        if beat.kind != "say" or beat.character_id is None:
            continue
        found.append(
            Heard(
                speaker=beat.character_id,
                beat=beat.beat,
                text=beat.text,
                intent=beat.intent,
                claims=tuple(
                    Claim(
                        c.proposition,
                        _words(beat.text, c.span),
                        None if c.attribution is None else c.attribution.holder_id,
                        "believes" if c.attribution is None else c.attribution.mode,
                        c.attribution is not None and c.attribution.factive,
                    )
                    for c in beat.claims
                    if c.proposition is not None
                    and (c.attribution is None or c.attribution.holder_id is not None)
                ),
                qualifiers=beat.qualifiers,
            )
        )
    return found


def witnesses_of(
    perceived: Sequence[perception.Perceived], speaker: str, holders: frozenset[str]
) -> list[tuple[str, str | None]]:
    """The content perceivers of a routed say who hold a partition, the speaker excluded, each
    with the part it caught (``None``: all of it)."""
    return [
        (p.character_id, p.partial_text)
        for p in perceived
        if p.fidelity == perception.CONTENT
        and p.character_id != speaker
        and p.character_id in holders
    ]


async def _holders(conn: AsyncConnection, story_id: str, cast: Sequence[str]) -> frozenset[str]:
    """The cast members with a partition (everyone but the player's character)."""
    found = []
    for character_id in cast:
        if not await partitions.is_player(conn, story_id, character_id):
            found.append(character_id)
    return frozenset(found)


def salient_beats(checked: Any) -> frozenset[int]:
    """The beats #146's salience step marked (a salient span on them)."""
    return frozenset(int(s[0].index) for s in getattr(checked, "salient", ()))


def silent_of(checked: Any) -> frozenset[str]:
    """#176 (carried from #162): the turn's ``silent`` characters, never salient witnesses (the
    turn's resolved roles, ``Checked.silent``)."""
    return frozenset(getattr(checked, "silent", ()) or ())


async def is_salient(
    conn: AsyncConnection,
    story_id: str,
    heard: Heard,
    witness: str,
    *,
    marked: frozenset[int],
    minors: frozenset[str],
) -> bool:
    """See the module docstring: a marked beat, or a claim whose subject touches the witness's
    partition; never a minor (an addressed minor still gets the default)."""
    if witness in minors:
        return False
    if heard.beat in marked:
        return True
    for claim in heard.claims:
        if await testimony.touches(conn, story_id, witness, claim.proposition.subject_id):
            return True
    return False


# -- adjudication (before composition) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Adjudication:
    """One salient witness's adjudication of one say: the model's decision and confidence, or
    why the default decides (``result``: ``adjudicated``, ``low_confidence``, ``defaulted`` (the
    cap or the budget), ``unavailable``)."""

    result: str
    decision: testimony.Decision | None = None
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class Adjudications:
    """The turn's adjudications by ``(beat, witness)`` and the degradation they caused."""

    by_witness: dict[tuple[int, str], Adjudication] = field(default_factory=dict)
    degradation: list[tuple[str, str]] = field(default_factory=list)

    def report(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for item in self.by_witness.values():
            found[item.result] = found.get(item.result, 0) + 1
        return found


async def _names(conn: AsyncConnection, story_id: str, ids: Sequence[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.id.in_(list(ids))
        )
    )
    return {str(r.id): str(r.canonical_name) for r in rows}


def _ids(props: Sequence[Proposition]) -> list[str]:
    found: list[str] = []
    for p in props:
        found += [p.subject_id, *((p.object_id,) if p.object_id else ()), *(p.object_set or ())]
    return list(dict.fromkeys(found))


async def _prior_outcomes(
    conn: AsyncConnection, story_id: str, witness: str, speaker: str
) -> list[testimony.Decision]:
    said = await partitions.speech_acts(conn, story_id, witness, speaker=speaker, held=False)
    found: list[testimony.Decision] = []
    for act in said:
        for decision in testimony.DECISIONS:
            if act.outcome == decision:
                found.append(decision)
    return found[-20:]


async def _inputs(
    conn: AsyncConnection,
    story_id: str,
    heard: Heard,
    witness: str,
    defaults: Sequence[testimony.TestimonyOutcome],
    consistency: Sequence[testimony.Consistency],
    trust: float,
    *,
    registry: Registry,
    names: Mapping[str, str],
) -> BeliefAdjudicationInputs:
    records = list({r.proposition_id: r for c in consistency for r in c.records}.values())
    props = [c.proposition for c in heard.claims] + [r.proposition for r in records]
    found = {**await _names(conn, story_id, _ids(props)), **names}
    beliefs: list[AdjudicationBelief] = []
    for record in records[:ADJUDICATION_MAX_RECORDS]:
        level = None
        if record.belief_id is not None:
            held = await partitions.read_belief(conn, story_id, record.belief_id)
            level = None if held is None else held.confidence_level.value
        beliefs.append(
            AdjudicationBelief(
                statement=render_proposition(record.proposition, registry, found)[:TEXT_MAX],
                source="belief" if record.belief_id is not None else "canon",
                confidence=level,
            )
        )
    existing = next((c.prior_level.value for c in consistency if c.prior_level is not None), None)
    rule_default = defaults[0].decision if defaults else testimony.SPEECH_ACT_ONLY
    return BeliefAdjudicationInputs(
        witness=found.get(witness, witness)[:200],
        witness_beliefs=beliefs,
        trust=trust,
        priors=AdjudicationPriors(
            outcomes=await _prior_outcomes(conn, story_id, witness, heard.speaker),
            existing_confidence=existing,
            rule_default=rule_default,
        ),
        utterance=AdjudicationUtterance(
            speaker=found.get(heard.speaker, heard.speaker)[:200],
            text=heard.text[:4_000],
            manner=None if heard.qualifiers.manner is None else heard.qualifiers.manner[:200],
            claims=[
                render_proposition(c.proposition, registry, found)[:TEXT_MAX]
                for c in heard.claims[:ADJUDICATION_MAX_CLAIMS]
            ],
        ),
        perceived_intent=testimony.perceived(heard.intent),
    )


async def adjudicate_turn(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    *,
    turn: int,
    actor: str,
    primary: str,
    cast: Sequence[str],
    names: Mapping[str, str],
    checked: Any,
    say: str | None,
    settings: ProfileSettings,
    budget: TurnBudget,
    read_set: StructuredReadSet,
) -> Adjudications:
    """The turn's adjudications (see the module docstring); empty unless ``planner.adjudicates``
    and perception routes."""
    found = Adjudications()
    level = scene_loader.level_of(settings)
    if not planner.adjudicates(settings) or level == perception.NONE:
        return found
    says = [h for h in heard_of(checked, actor, say) if h.claims]
    if not says:
        budget.settle_optional(TESTIMONY)
        return found
    cap = max_adjudications(settings)
    marked = salient_beats(checked)
    routes = routes_of(settings)
    async with db.connect() as conn:
        state = await scene_loader.load_scene_state(conn, story_id, turn)
        holders = await _holders(conn, story_id, cast)
        minors = await testimony.minors(conn, story_id) | silent_of(checked)
        # #163: a ``minor``-weight (lazily modeled) witness never takes the model adjudication;
        # it gets the default at commit.
        lazy = await modeling.minor_ids(conn, story_id)
        registry = await load(conn, story_id)
        lookup = KeyedLookup(conn, read_set)
        queue: list[tuple[int, int, Heard, str]] = []
        for heard in says:
            routed = perception.route_perception(heard.route_beat(), state, level=level)
            for witness, partial in witnesses_of(routed, heard.speaker, holders):
                if partial is not None or witness in minors or witness in lazy:
                    continue
                if not await is_salient(
                    conn, story_id, heard, witness, marked=marked, minors=minors
                ):
                    continue
                queue.append((0 if witness == primary else 1, heard.beat, heard, witness))
        queue.sort(key=lambda item: (item[0], item[1]))
        calls = 0
        for _, _, heard, witness in queue:
            intent = testimony.perceived(heard.intent)
            if intent == "joking":
                continue
            props = [c.proposition for c in heard.claims]
            consistency = await testimony.consistency_of(
                lookup, story_id, witness, props, registry=registry
            )
            if all(c.verdict is VerdictKind.CONTRADICTED for c in consistency):
                continue  # nothing the model may change
            trust = await testimony.trust(conn, story_id, witness, heard.speaker)
            defaults = testimony.testimony(
                witness,
                heard.speech_act(None, None),
                intent,
                consistency=consistency,
                trust=trust,
            )
            key = (heard.beat, witness)
            if calls >= cap:
                found.by_witness[key] = Adjudication("defaulted")
                found.degradation.append((planner.TESTIMONY_DEFAULTED, "cap"))
                ADJUDICATIONS.labels(result="defaulted").inc()
                continue
            held = budget.held
            if not budget.try_optional(TESTIMONY, 1):
                found.by_witness[key] = Adjudication("defaulted")
                found.degradation.append((planner.TESTIMONY_DEFAULTED, "budget"))
                ADJUDICATIONS.labels(result="defaulted").inc()
                continue
            calls += 1
            inputs = await _inputs(
                conn,
                story_id,
                heard,
                witness,
                defaults,
                consistency,
                trust,
                registry=registry,
                names=names,
            )
            route = routes.get(BELIEF_ADJUDICATION.name)
            answer: BeliefAdjudicationResult | Uncertain | None = None
            try:
                with routing(routes):
                    answer = await models.structured_call(
                        BELIEF_ADJUDICATION.name,
                        BeliefAdjudicationResult,
                        inputs,
                        params=route.params if route is not None else None,
                        deterministic_first=lambda: None,
                        budget=budget,
                    )
            except BudgetExhausted:
                answer = Uncertain("budget_exhausted", 0)
            except ModelUnavailable:
                answer = None
            finally:
                budget.release_to(min(budget.held, held))
            if answer is None or isinstance(answer, Uncertain):
                if answer is not None and answer.reason == "budget_exhausted":
                    found.by_witness[key] = Adjudication("defaulted")
                    found.degradation.append((planner.TESTIMONY_DEFAULTED, "budget"))
                    ADJUDICATIONS.labels(result="defaulted").inc()
                else:
                    found.by_witness[key] = Adjudication("unavailable")
                    found.degradation.append((planner.ADJUDICATION_UNAVAILABLE, "model"))
                    ADJUDICATIONS.labels(result="unavailable").inc()
                continue
            if answer.confidence < testimony.ADJUDICATION_MIN_CONFIDENCE:
                found.by_witness[key] = Adjudication(
                    "low_confidence", answer.decision, answer.confidence, tuple(answer.reasons)
                )
                found.degradation.append((planner.TESTIMONY_DEFAULTED, "low_confidence"))
                ADJUDICATIONS.labels(result="kept").inc()
                continue
            found.by_witness[key] = Adjudication(
                "adjudicated", answer.decision, answer.confidence, tuple(answer.reasons)
            )
            ADJUDICATIONS.labels(result="adjudicated").inc()
    budget.settle_optional(TESTIMONY)
    log.debug(
        "testimony: %d adjudicated, %d defaulted",
        sum(1 for a in found.by_witness.values() if a.result == "adjudicated"),
        sum(1 for a in found.by_witness.values() if a.result != "adjudicated"),
    )
    return found


# -- a needed exposure (#170) ------------------------------------------------------------------


class ExposureAdjudicator:
    """``core.epistemic.context``'s adjudication hook (#170, completing #164's "adjudication only
    when the needing turn's budget affords it"): the rendering character is the turn's primary
    witness, so an exposure its context needs is adjudicated by ``belief_adjudication`` under
    ``planner.adjudicates`` when the pool affords the call (``try_optional("testimony", 1)``), at
    most ``max_adjudications`` a turn; otherwise, and for a joke or a claim the witness's records
    contradict, the default decides (``None``), with ``testimony_defaulted`` (``cap``,
    ``budget``) or ``adjudication_unavailable`` in ``degradation``."""

    def __init__(
        self,
        models: ModelAccessLayer,
        settings: ProfileSettings,
        budget: TurnBudget,
        degradation: list[tuple[str, str]],
    ) -> None:
        self.models = models
        self.settings = settings
        self.budget = budget
        self.degradation = degradation
        self.calls = 0

    async def __call__(
        self,
        conn: AsyncConnection,
        item: exposure.Exposure,
        outcome: testimony.TestimonyOutcome,
    ) -> tuple[testimony.Decision, float] | None:
        if not planner.adjudicates(self.settings) or item.source is None:
            return None
        story_id = item.story_id
        source = await conn.execute(
            select(events.c.payload).where(events.c.id == item.source_event_id)
        )
        payload = source.scalar_one_or_none() or {}
        intent = str(payload.get("intent") or "unspecified")
        if testimony.perceived(intent) == "joking":
            return None
        registry = await load(conn, story_id)
        frame = item.position
        consistency = await testimony.consistency_of(
            KeyedLookup(conn, StructuredReadSet()),
            story_id,
            item.witness,
            [item.proposition],
            registry=registry,
            through=frame,
        )
        if all(c.verdict is VerdictKind.CONTRADICTED for c in consistency):
            return None
        if self.calls >= max_adjudications(self.settings):
            self.degradation.append((planner.TESTIMONY_DEFAULTED, "cap"))
            ADJUDICATIONS.labels(result="defaulted").inc()
            return None
        held = self.budget.held
        if not self.budget.try_optional(TESTIMONY, 1):
            self.degradation.append((planner.TESTIMONY_DEFAULTED, "budget"))
            ADJUDICATIONS.labels(result="defaulted").inc()
            return None
        self.calls += 1
        heard = Heard(
            speaker=item.source,
            beat=frame.beat,
            text=str(payload.get("text") or ""),
            intent=intent,
            claims=(Claim(item.proposition),),
        )
        cast = {
            str(r.id): str(r.name)
            for r in await conn.execute(
                select(characters.c.id, characters.c.name).where(
                    characters.c.story_id == story_id,
                    characters.c.id.in_([item.witness, item.source]),
                )
            )
        }
        inputs = await _inputs(
            conn,
            story_id,
            heard,
            item.witness,
            [outcome],
            consistency,
            outcome.trust,
            registry=registry,
            names=cast,
        )
        routes = routes_of(self.settings)
        route = routes.get(BELIEF_ADJUDICATION.name)
        answer: BeliefAdjudicationResult | Uncertain | None = None
        try:
            with routing(routes):
                answer = await self.models.structured_call(
                    BELIEF_ADJUDICATION.name,
                    BeliefAdjudicationResult,
                    inputs,
                    params=route.params if route is not None else None,
                    deterministic_first=lambda: None,
                    budget=self.budget,
                )
        except BudgetExhausted:
            answer = Uncertain("budget_exhausted", 0)
        except ModelUnavailable:
            answer = None
        finally:
            self.budget.release_to(min(self.budget.held, held))
        if answer is None or isinstance(answer, Uncertain):
            if answer is not None and answer.reason == "budget_exhausted":
                self.degradation.append((planner.TESTIMONY_DEFAULTED, "budget"))
                ADJUDICATIONS.labels(result="defaulted").inc()
            else:
                self.degradation.append((planner.ADJUDICATION_UNAVAILABLE, "model"))
                ADJUDICATIONS.labels(result="unavailable").inc()
            return None
        if answer.confidence < testimony.ADJUDICATION_MIN_CONFIDENCE:
            self.degradation.append((planner.TESTIMONY_DEFAULTED, "low_confidence"))
            ADJUDICATIONS.labels(result="kept").inc()
            return None
        ADJUDICATIONS.labels(result="adjudicated").inc()
        return answer.decision, answer.confidence


# -- the commit ----------------------------------------------------------------------------------


async def testify_committed(
    conn: AsyncConnection,
    op: Any,
    results: Sequence[perception.PerceptionResult],
    *,
    checked: Any,
    actor: str,
    say: str | None,
    cast: Sequence[str],
    ids: Mapping[int, str],
    adjudications: Adjudications | None,
    read_set: StructuredReadSet,
    utterances: Mapping[int, str] | None,
    stage: Any,
) -> Testified | None:
    """Write the turn's testimony in its commit step, after routing (see the module
    docstring); answers ``turn_report.epistemic``'s testimony and exposure parts (``None`` when
    partitions are off under the configuration now). ``stage`` (#174, a sequential turn's
    ``core.epistemic.stage.TurnStage``): a later primary's testimony from an earlier primary's
    staged beat cites that beat (``antecedent = staged_beat``, FR-EPI-14 (b)), the stage
    answering as the oracle."""
    from epistrel.core.profiles.effective import effective_configuration

    story_id = str(op.story_id)
    current = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(current, "partitions"):
        return None
    says = {h.beat: h for h in heard_of(checked, actor, say)}
    holders = await _holders(conn, story_id, cast)
    minors = await testimony.minors(conn, story_id) | silent_of(checked)
    marked = salient_beats(checked)
    registry = await load(conn, story_id)
    lookup = KeyedLookup(conn, read_set)
    lazy = exposure.lazy_on(current)
    budget = exposure.Pass(cap=exposure.max_lazy_resolutions(current))
    stored: dict[tuple[int, Any], str] = {}
    exposed = 0
    now: list[str] = []
    by_kind: dict[str, int] = {}
    witnesses = adjudicated = defaulted = deferred = nested_written = 0

    async def stored_id(beat: int, proposition: Proposition) -> str:
        key = (beat, proposition.normalized_key())
        if key not in stored:
            stored[key] = await write_proposition(
                conn, story_id, proposition, registry, base_revision=op.base_revision
            )
        return stored[key]

    for result in results:
        heard = says.get(result.beat.beat)
        if result.beat.kind != "say" or heard is None or result.beat.actor != heard.speaker:
            continue
        if not heard.claims:
            continue  # a say that claims nothing: its memory row is the whole speech act
        event_id = ids.get(heard.beat)
        position = exposure.exposure_position(
            op.narrative_turn or 0, op.turn_variant or 0, heard.beat
        )
        for witness, partial in witnesses_of(result.perceived, heard.speaker, holders):
            witnesses += 1
            eager = witness in heard.addressed or await is_salient(
                conn, story_id, heard, witness, marked=marked, minors=minors
            )
            act = heard.speech_act(event_id, partial)
            fidelity = ExposureFidelity.FULL if partial is None else ExposureFidelity.PARTIAL
            if not eager:
                # #164: an exposure record, not a belief; resolved when a later turn needs it
                # (at once, by the default, with lazy resolution off).
                deferred += 1
                if event_id is None or not act.propositions:
                    continue
                written_ids = await exposure.record_exposures(
                    conn,
                    op,
                    [
                        exposure.Exposed(
                            witness=witness,
                            proposition_id=await stored_id(heard.beat, p),
                            proposition=p,
                            source=heard.speaker,
                            source_event_id=event_id,
                            position=position,
                            fidelity=fidelity,
                        )
                        for p in act.propositions
                    ],
                )
                exposed += len(written_ids)
                if not lazy:
                    now += written_ids
                continue
            # #164: this turn needs the witness's belief: its waiting exposures on the claims'
            # keys resolve first, each in its own frame.
            await exposure.resolve_needed(
                conn,
                op,
                witness,
                list(act.propositions),
                budget=budget,
                settings=current,
                registry=registry,
            )
            intent = testimony.perceived(heard.intent)
            consistency = await testimony.consistency_of(
                lookup, story_id, witness, list(act.propositions), registry=registry
            )
            trust = await testimony.trust(conn, story_id, witness, heard.speaker)
            outcomes = list(
                testimony.testimony(witness, act, intent, consistency=consistency, trust=trust)
            )
            judged = (adjudications or Adjudications()).by_witness.get((heard.beat, witness))
            if judged is not None and judged.result != "adjudicated":
                defaulted += 1
            if judged is not None and judged.result == "adjudicated" and judged.decision:
                taken = [
                    testimony.apply_adjudication(o, judged.decision, judged.confidence, intent)
                    for o in outcomes
                ]
                outcomes = [t if t is not None else o for t, o in zip(taken, outcomes, strict=True)]
                if any(t is not None for t in taken):
                    adjudicated += 1
            staged = stage is not None and stage.is_antecedent_for(witness, heard.beat)
            made = await testimony.commit_testimony(
                conn,
                op,
                act,
                outcomes,
                settings=current,
                registry=registry,
                utterance_id=(utterances or {}).get(heard.beat),
                intent=heard.intent,
                staged=stage if staged else None,
            )
            # #167: what the witness now thinks the speaker (and whoever it named) believes.
            nested_written += await testimony.nested_committed(
                conn,
                op,
                act,
                [o for o in outcomes if o.proposition is not None and _plain(heard, o)],
                witness=witness,
                intent=heard.intent,
                attributions=heard.attributions(partial),
                trust_value=trust,
                settings=current,
                registry=registry,
            )
            for outcome in outcomes:
                by_kind[outcome.decision] = by_kind.get(outcome.decision, 0) + 1
            if event_id is not None and act.propositions:
                # The salient witness's exposures, resolved now, for audit.
                exposed += len(
                    await exposure.record_exposures(
                        conn,
                        op,
                        [
                            exposure.Exposed(
                                witness=witness,
                                proposition_id=await stored_id(heard.beat, p),
                                proposition=p,
                                source=heard.speaker,
                                source_event_id=event_id,
                                position=position,
                                fidelity=fidelity,
                                resolved_by_transition=_resolving(made, p, registry),
                            )
                            for p in act.propositions
                        ],
                    )
                )
    if now:
        await exposure.resolve_written(
            conn, op, now, budget=budget, settings=current, registry=registry
        )
    return Testified(
        testimony={
            "witnesses": witnesses,
            "adjudicated": adjudicated,
            "defaulted": defaulted,
            "deferred": deferred,
            "outcomes_by_kind": dict(sorted(by_kind.items())),
            "nested_written": nested_written,
        },
        exposures=exposure.report(exposed, budget),
        capped=budget.capped,
    )


def _plain(heard: Heard, outcome: testimony.TestimonyOutcome) -> bool:
    """Whether ``outcome`` is about a proposition the speaker asserted outright (not a
    self-report of belief, whose nested belief its attribution writes, #167)."""
    if outcome.proposition is None:
        return False
    key = outcome.proposition.normalized_key()
    return not any(
        c.attributed_to is not None and not c.factive and c.proposition.normalized_key() == key
        for c in heard.claims
    )


@dataclass(frozen=True, slots=True)
class Testified:
    """What the commit's testimony step wrote: ``turn_report.epistemic.testimony``, the exposure
    counts (``exposures_written``, ``resolutions``) and whether the lazy-resolution cap left
    exposures waiting (``lazy_resolution_capped``)."""

    testimony: dict[str, Any]
    exposures: dict[str, Any]
    capped: bool = False


def _resolving(made: Sequence[Any], proposition: Proposition, registry: Registry) -> str | None:
    """The transition that resolved a salient witness's claim: its first-order transition when
    it wrote one, else its ``said`` belief's."""
    key = canonical(proposition, registry).normalized_key()
    mine = [
        m
        for m in made
        if canonical(m.belief.proposition, registry).normalized_key()
        in (key, canonical(proposition.negation(), registry).normalized_key())
    ]
    first = next((m for m in mine if m.belief.kind is BeliefKind.FIRST_ORDER), None)
    said = next((m for m in mine if m.belief.kind is BeliefKind.SPEECH_ACT), None)
    chosen = first or said or (made[0] if made else None)
    return None if chosen is None else str(chosen.transition.id)


__all__ = [
    "MAX_ADJUDICATIONS_PER_TURN",
    "Adjudication",
    "Adjudications",
    "Claim",
    "ExposureAdjudicator",
    "Heard",
    "Testified",
    "adjudicate_turn",
    "heard_of",
    "is_salient",
    "max_adjudications",
    "salient_beats",
    "testify_committed",
    "witnesses_of",
]
