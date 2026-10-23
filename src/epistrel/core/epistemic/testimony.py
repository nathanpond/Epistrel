"""Testimony: hearing is not believing (FR-BELIEF-1,2,3,9, FR-TURN-7's hearer clause; docs/02
§4.4; #162).

An assertion heard is a **speech act** for every witness who perceived its content (#161's
routing decides who that is; the speaker is no witness of its own words, and the player's
character has no partition). What each witness makes of it is a separate, typed testimony
transition (#160's ``transition()``), gated by three things:

- **consistency** with what the witness already knows: #140's ``check()`` over the witness's
  partition as the record set (#159's ``belief(witness)`` holder: the partition, then the canon
  the witness may read). ``contradicted`` → ``reject`` (or, from a witness who trusts the speaker
  at ``TRUST_CORRECTS`` or more against a belief it holds only at ``suspected``, a
  ``correction``); ``supported`` → ``accept``, raising the witness's own belief one level
  (``revision``; never past ``believed``: ``known`` is observation's) or, supported by visible
  canon alone, a ``testimony`` belief at ``believed``; ``novel`` → ``accept`` at ``believed``
  when the witness trusts the speaker at ``BELIEVE_AT`` or more, else at ``suspected``;
  ``uncertain`` (an unresolved entity included) → ``suspend``;
- **trust** toward the speaker (``trust``): seeded from #137's ``relationships`` projection as
  the witness may see it (kin (``parent_of``, ``child_of``, ``sibling_of``, ``married_to``) and
  ``friend_of`` → ``TRUST_HIGH``, ``enemy_of`` → ``TRUST_LOW``, otherwise ``TRUST_NEUTRAL``;
  several relationships → the highest; a relationship's ``trust`` qualifier, 0 to 1, overrides its
  default), then nudged by ``NUDGE`` per prior outcome with that speaker recorded in the
  witness's own transitions: -0.1 per claim of the speaker's the witness rejected, -0.1 per
  testimony belief from the speaker later overturned (corrected), +0.1 per testimony belief
  from the speaker a later observation confirmed; clamped to [0, 1]. Trust is derived, never
  stored: it follows the timeline (truncation, swap, fork) and erasure with the transitions;
- **perceived intent**: a ``joking`` say is ``speech_act_only``; every other declared intent
  reads as sincere (a hearer never reads ``deceptive``: an undetected deception is accepted on
  the normal rules, and a detected one, the witness's knowledge contradicting it, is a
  ``reject``); the player's ``unspecified`` is sincere.

``testimony()`` is the deterministic default, pure: one ``TestimonyOutcome`` per proposition the
``say`` carries (declared plus detected; none normalizable → one ``speech_act_only``). The
orchestrator's ``belief_adjudication`` purpose (#162's ``orchestrator.epistemic.adjudicate``)
may replace a salient witness's decision (``apply_adjudication``), never overriding a
``contradicted`` verdict or accepting a joke.

**Writing** (``commit_testimony``, a commit step of the turn): every witness gets, per
proposition, a ``said`` belief (``belief_kind = speech_act``: ``believes(H, said(S, X))``, held
``known``, its transition's ``outcome`` the witness's decision), then the first-order transition
the decision calls for, if any. Every transition cites the say beat's event as its antecedent and
sits at the beat's step 1 (the witness rule), so #160's self-attestation guard applies.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import (
    BeliefConfidenceLevel,
    BeliefHoldState,
    BeliefKind,
    BeliefTransitionKind,
)
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.epistemic.partitions import BeliefOperation
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position, active_only
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.readset import KeyedLookup
from epistrel.core.tables import characters
from epistrel.core.tables.beliefs import belief_transitions, beliefs
from epistrel.core.verify.rules import Holder, Record, Verdict, VerdictKind, check_with
from epistrel.observability.metrics import TESTIMONY_OUTCOMES

Decision = Literal["accept", "reject", "suspend", "speech_act_only"]
ACCEPT: Decision = "accept"
REJECT: Decision = "reject"
SUSPEND: Decision = "suspend"
SPEECH_ACT_ONLY: Decision = "speech_act_only"
DECISIONS: tuple[Decision, ...] = (ACCEPT, REJECT, SUSPEND, SPEECH_ACT_ONLY)

PerceivedIntent = Literal["sincere", "joking"]

TRUST_HIGH = 0.8
TRUST_NEUTRAL = 0.5
TRUST_LOW = 0.2
BELIEVE_AT = 0.65
"""A novel claim from a speaker trusted at least this much is ``believed``, else ``suspected``."""
TRUST_CORRECTS = 0.8
"""A contradicted claim from a speaker trusted at least this much corrects a ``suspected``
belief."""
NUDGE = 0.1
"""What one prior outcome with a speaker moves the witness's trust in them."""

KIN: frozenset[str] = frozenset({"parent_of", "child_of", "sibling_of", "married_to"})
FRIENDS: frozenset[str] = frozenset({"friend_of"})
ENEMIES: frozenset[str] = frozenset({"enemy_of"})

Level = BeliefConfidenceLevel
Kind = BeliefTransitionKind


def perceived(intent: str | None) -> PerceivedIntent:
    """How a hearer reads a say's declared intent (FR-TURN-7): a joke as a joke, anything else
    (``deceptive``, ``unspecified`` included) as sincere."""
    return "joking" if intent == "joking" else "sincere"


# -- trust ---------------------------------------------------------------------------------------


def seed_trust(relations: Iterable[tuple[str, Mapping[str, Any]]]) -> float:
    """The trust a witness's relationships to a speaker seed (``(predicate name, qualifiers)``
    pairs; see the module docstring)."""
    found: list[float] = []
    for name, qualifiers in relations:
        given = qualifiers.get("trust")
        if isinstance(given, int | float) and not isinstance(given, bool) and 0 <= given <= 1:
            found.append(float(given))
        elif name in KIN or name in FRIENDS:
            found.append(TRUST_HIGH)
        elif name in ENEMIES:
            found.append(TRUST_LOW)
    return max(found) if found else TRUST_NEUTRAL


def nudged(seed: float, confirmed: int = 0, rejected: int = 0, overturned: int = 0) -> float:
    """``seed`` moved ``NUDGE`` per prior outcome, clamped to [0, 1] (three decimals)."""
    value = seed + NUDGE * (confirmed - rejected - overturned)
    return round(min(1.0, max(0.0, value)), 3)


async def trust(conn: AsyncConnection, story_id: str, witness: str, speaker: str) -> float:
    """``witness``'s trust in ``speaker`` now (see the module docstring)."""
    from epistrel.core.projections.structured import relationships_views
    from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX

    _, views = await relationships_views(
        conn, story_id, [witness], viewpoint=f"{CHARACTER_VIEWPOINT_PREFIX}{witness}"
    )
    seed = seed_trust(
        (v.predicate, v.qualifiers)
        for v in views.get(witness, ())
        if v.other.entity_id == speaker and (v.direction == "out" or "trust" not in v.qualifiers)
    )
    t = belief_transitions.c
    b = beliefs.c
    mine = [
        t.story_id == story_id,
        t.holder_character_id == witness,
        t.source_character_id == speaker,
        t.kind == Kind.TESTIMONY.value,
        active_only(belief_transitions),
    ]
    rejected = await conn.scalar(
        select(func.count()).select_from(belief_transitions).where(*mine, t.outcome == REJECT)
    )
    produced = (
        select(b.id)
        .select_from(beliefs.join(belief_transitions, t.id == b.provenance))
        .where(*mine, b.kind == BeliefKind.FIRST_ORDER.value)
    )
    overturned = await conn.scalar(
        select(func.count())
        .select_from(beliefs)
        .where(
            b.id.in_(produced),
            b.state == BeliefHoldState.CORRECTED.value,
            active_only(beliefs),
        )
    )
    o = belief_transitions.alias("observed")
    confirmed = await conn.scalar(
        select(func.count(func.distinct(o.c.belief_id))).where(
            o.c.story_id == story_id,
            o.c.kind == Kind.OBSERVATION.value,
            o.c.belief_id.in_(produced),
            active_only(o),
        )
    )
    return nudged(seed, int(confirmed or 0), int(rejected or 0), int(overturned or 0))


# -- the default ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpeechAct:
    """One ``say`` as its witnesses take it: the speaker, its beat (and the beat's event, the
    transitions' antecedent), the propositions it carries, its text and visible manner."""

    speaker: str
    beat: int
    propositions: tuple[Proposition, ...]
    event_id: str | None = None
    text: str = ""
    manner: str | None = None


@dataclass(frozen=True, slots=True)
class Consistency:
    """One proposition's verdict over the witness's records, with the witness's own belief it
    rests on (``prior_belief_id``, at ``prior_level``), if any."""

    proposition: Proposition
    verdict: VerdictKind
    prior_belief_id: str | None = None
    prior_level: Level | None = None
    records: tuple[Record, ...] = ()


@dataclass(frozen=True, slots=True)
class TestimonyOutcome:
    """What one witness makes of one proposition (``None``: the say carried none): the
    ``decision``, the first-order transition it writes (``transition``: ``testimony``,
    ``revision`` or ``correction``; ``None`` for the speech act alone) and its level
    (``confidence``), the consistency verdict it rests on, the trust it was read under and why
    (``rule``)."""

    witness: str
    proposition: Proposition | None
    decision: Decision
    confidence: Level | None = None
    transition: Kind | None = None
    verdict: str | None = None
    trust: float = TRUST_NEUTRAL
    rule: str = "default"
    prior_belief_id: str | None = None
    adjudicated: bool = False

    def report(self) -> dict[str, Any]:
        return {
            "witness": self.witness,
            "decision": self.decision,
            "transition": None if self.transition is None else self.transition.value,
            "confidence": None if self.confidence is None else self.confidence.value,
            "verdict": self.verdict,
            "rule": self.rule,
            "adjudicated": self.adjudicated,
        }


def _rank(level: Level) -> int:
    return partitions.LEVELS.index(Level(level))


def _by_trust(value: float) -> Level:
    return Level.BELIEVED if value >= BELIEVE_AT else Level.SUSPECTED


def decide(
    witness: str, consistency: Consistency, trust_value: float, intent: PerceivedIntent
) -> TestimonyOutcome:
    """The deterministic default for one proposition (see the module docstring). Pure."""
    found = TestimonyOutcome(
        witness,
        consistency.proposition,
        SPEECH_ACT_ONLY,
        verdict=consistency.verdict.value,
        trust=trust_value,
        prior_belief_id=consistency.prior_belief_id,
    )
    if intent == "joking":
        return replace(found, rule="joking")
    verdict = consistency.verdict
    prior = consistency.prior_level
    if verdict is VerdictKind.CONTRADICTED:
        if (
            trust_value >= TRUST_CORRECTS
            and consistency.prior_belief_id is not None
            and prior is Level.SUSPECTED
        ):
            return replace(
                found,
                decision=ACCEPT,
                transition=Kind.CORRECTION,
                confidence=_by_trust(trust_value),
                rule="trusted_correction",
            )
        return replace(found, decision=REJECT, rule="contradicted")
    if verdict is VerdictKind.SUPPORTED:
        if consistency.prior_belief_id is None or prior is None:
            return replace(
                found,
                decision=ACCEPT,
                transition=Kind.TESTIMONY,
                confidence=Level.BELIEVED,
                rule="supported",
            )
        raised = partitions.LEVELS[min(_rank(Level.BELIEVED), _rank(prior) + 1)]
        if _rank(raised) <= _rank(prior):
            return replace(found, decision=ACCEPT, confidence=prior, rule="supported_held")
        return replace(
            found, decision=ACCEPT, transition=Kind.REVISION, confidence=raised, rule="supported"
        )
    if verdict is VerdictKind.NOVEL:
        return replace(
            found,
            decision=ACCEPT,
            transition=Kind.TESTIMONY,
            confidence=_by_trust(trust_value),
            rule="novel",
        )
    return replace(found, decision=SUSPEND, rule="uncertain")


def testimony(
    witness: str,
    speech_act: SpeechAct,
    perceived_intent: PerceivedIntent,
    *,
    consistency: Sequence[Consistency],
    trust: float,
) -> tuple[TestimonyOutcome, ...]:
    """``witness``'s default outcome for each proposition of ``speech_act`` (``consistency``:
    one verdict per proposition, in order); a say carrying none is one ``speech_act_only``."""
    if not speech_act.propositions:
        return (TestimonyOutcome(witness, None, SPEECH_ACT_ONLY, trust=trust, rule="no_claim"),)
    return tuple(decide(witness, c, trust, perceived_intent) for c in consistency)


ADJUDICATION_MIN_CONFIDENCE = 0.6
"""A model decision below this confidence is not taken: the default stands."""


def apply_adjudication(
    default: TestimonyOutcome,
    decision: Decision,
    confidence: float,
    intent: PerceivedIntent,
) -> TestimonyOutcome | None:
    """The model's ``decision`` on ``default`` (a salient witness's adjudication), or ``None``
    when the default stands: below ``ADJUDICATION_MIN_CONFIDENCE``, against a deterministic
    ``contradicted`` verdict, accepting a joke, or about a say that carried no proposition."""
    if (
        confidence < ADJUDICATION_MIN_CONFIDENCE
        or default.proposition is None
        or default.verdict == VerdictKind.CONTRADICTED.value
        or (intent == "joking" and decision == ACCEPT)
    ):
        return None
    if decision == default.decision:
        return replace(default, adjudicated=True, rule="adjudicated")
    if decision == ACCEPT:
        return replace(
            default,
            decision=ACCEPT,
            transition=Kind.TESTIMONY,
            confidence=_by_trust(default.trust),
            rule="adjudicated",
            adjudicated=True,
        )
    return replace(
        default,
        decision=decision,
        transition=None,
        confidence=None,
        rule="adjudicated",
        adjudicated=True,
    )


# -- reading ---------------------------------------------------------------------------------------


async def consistency_of(
    lookup: KeyedLookup,
    story_id: str,
    witness: str,
    propositions: Sequence[Proposition],
    *,
    registry: Registry | None = None,
) -> list[Consistency]:
    """Each proposition's verdict over ``witness``'s records now (#140's ``check`` through
    ``check_with``'s ``belief(witness)`` holder), with the witness's own belief it rests on."""
    found: list[Consistency] = []
    for proposition in propositions:
        fetched: list[Record] = []
        verdict: Verdict = await check_with(
            lookup,
            story_id,
            proposition,
            holder=Holder.belief(witness),
            registry=registry,
            fetched=fetched,
        )
        cited = next((e.belief_id for e in verdict.evidence if e.belief_id is not None), None)
        level = None
        if cited is not None:
            held = await partitions.read_belief(lookup.conn, story_id, cited)
            level = None if held is None else held.confidence_level
        found.append(Consistency(proposition, verdict.kind, cited, level, tuple(fetched)))
    return found


async def touches(conn: AsyncConnection, story_id: str, witness: str, subject_id: str) -> bool:
    """Whether ``witness``'s partition holds anything about ``subject_id`` (the partition-touch
    rule of salience)."""
    held = await partitions.partition(conn, story_id, witness, entities_=[subject_id], limit=1)
    return bool(held)


async def minors(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """The story's characters under 18 (never salient witnesses)."""
    found = await conn.scalars(
        select(characters.c.id).where(characters.c.story_id == story_id, characters.c.true_age < 18)
    )
    return frozenset(str(c) for c in found)


# -- writing ---------------------------------------------------------------------------------------


def witness_position(turn: int, variant: int, beat: int) -> Position:
    """Where a witness's transitions sit: the say beat's step 1 (#160's witness rule)."""
    return Position(turn, variant, 0, beat, 1)


async def commit_testimony(
    conn: AsyncConnection,
    op: BeliefOperation,
    speech_act: SpeechAct,
    outcomes: Sequence[TestimonyOutcome],
    *,
    settings: Any = None,
    registry: Registry | None = None,
) -> list[transitions.TransitionResult]:
    """Write one witness's outcomes of ``speech_act`` in ``op``'s commit transaction (see the
    module docstring); answers the transitions written."""
    if speech_act.event_id is None:
        raise InvalidRequest("a testimony cites the say beat's event", {"field": "event_id"})
    antecedent = transitions.Antecedent.record("events", speech_act.event_id)
    position = witness_position(op.narrative_turn or 0, op.turn_variant or 0, speech_act.beat)
    written: list[transitions.TransitionResult] = []
    for outcome in outcomes:
        TESTIMONY_OUTCOMES.labels(outcome=outcome.decision).inc()
        if outcome.proposition is None:
            continue
        common: dict[str, Any] = {
            "holder": outcome.witness,
            "antecedent": antecedent,
            "position": position,
            "settings": settings,
            "registry": registry,
        }
        written.append(
            await transitions.transition(
                conn,
                op,
                kind=Kind.TESTIMONY,
                proposition=outcome.proposition,
                confidence=transitions.Confidence(Level.KNOWN),
                source=speech_act.speaker,
                outcome=outcome.decision,
                belief_kind=BeliefKind.SPEECH_ACT,
                **common,
            )
        )
        if outcome.transition is None:
            continue
        confidence = transitions.Confidence(outcome.confidence)
        if outcome.transition is Kind.TESTIMONY:
            written.append(
                await transitions.transition(
                    conn,
                    op,
                    kind=Kind.TESTIMONY,
                    proposition=outcome.proposition,
                    confidence=confidence,
                    source=speech_act.speaker,
                    outcome=ACCEPT,
                    **common,
                )
            )
            continue
        try:
            written.append(
                await transitions.transition(
                    conn,
                    op,
                    kind=outcome.transition,
                    proposition=outcome.proposition,
                    confidence=confidence,
                    **common,
                )
            )
        except InvalidRequest as refused:
            # A correction whose contradiction is not one the partition closes (another rule
            # decided it), or a revision with nothing left to raise: the said belief stands.
            if (refused.details or {}).get("reason") not in ("nothing_to_correct",):
                raise
    return written


__all__ = [
    "ACCEPT",
    "ADJUDICATION_MIN_CONFIDENCE",
    "BELIEVE_AT",
    "DECISIONS",
    "ENEMIES",
    "FRIENDS",
    "KIN",
    "NUDGE",
    "REJECT",
    "SPEECH_ACT_ONLY",
    "SUSPEND",
    "TRUST_CORRECTS",
    "TRUST_HIGH",
    "TRUST_LOW",
    "TRUST_NEUTRAL",
    "Consistency",
    "Decision",
    "PerceivedIntent",
    "SpeechAct",
    "TestimonyOutcome",
    "apply_adjudication",
    "commit_testimony",
    "consistency_of",
    "decide",
    "minors",
    "nudged",
    "perceived",
    "seed_trust",
    "testimony",
    "touches",
    "trust",
    "witness_position",
]
