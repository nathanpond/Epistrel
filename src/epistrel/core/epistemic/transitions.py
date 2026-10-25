"""Dated, typed belief transitions with mandatory provenance (FR-EPI-7,13,14, FR-BELIEF-1,2,4,10;
docs/02 §4.4; #160).

**Every belief has a cause.** A character's belief set changes only through a transition: one
of the acquisition operations FR-EPI-7 names (``observation``, ``testimony``, ``inference``,
``discovery``, ``correction``, ``forgetting``, ``revision``), author direction (``author``) or a
thunk resolution's knowledge obligation (``resolution``). ``transition()`` is the only writer
of ``beliefs`` (``partitions._insert_belief`` is module-private, and ``tools.gates
check-epistemic-separation`` refuses any other caller); each call writes one
``belief_transitions`` row naming the belief it produced or changed, and a belief row's
``provenance`` is the transition that produced it (NOT NULL, a deferred foreign key, and a
BEFORE INSERT trigger: BELIEF-PROVENANCE). Hearing is not believing (FR-BELIEF-1): an exposure
(#164) records what a character perceived; believing it is a separate ``testimony`` or
``observation`` transition the caller decides on.

**The antecedent guard** (FR-EPI-13,14). Every transition cites one antecedent, validated
before anything is written:

- ``record``: a committed record (``core.antecedents``: a fact, an event, an utterance or a
  belief) on the active timeline, a fact ``committed``; a position-less record (a seed at
  position 0, an author act) precedes everything;
- ``staged_beat`` / ``render_step``: a verified staged beat earlier in the same turn (#174) or a
  verified earlier step of the same render (#168), reached through an ``AntecedentOracle``
  (the default refuses both: nothing is verified outside a turn's staging); an oracle with a
  ``refusal(ref)`` may refuse a staged beat outright with its reason (#175: a ``simultaneous``
  turn's stage answers ``antecedent_unavailable_in_mode``, FR-MULTI-11);
- ``resolution``: a thunk this operation resolved (``thunks.state = resolved`` and
  ``resolution_operation_id`` the writing operation), exempt from the order rule (FR-EPI-14 (d)).

The antecedent must precede the dependent step (``position``) in (turn, slot, beat, step) order;
equal or later is ``SelfAttestation`` (422 ``self_attestation``) and an antecedent in another
variant of the same turn is ``AntecedentInvalid`` (``other_variant``). Which antecedents a kind
admits is ``KIND_ANTECEDENTS`` (testimony from an utterance or a staged beat, inference from
the holder's own belief, forgetting from the belief itself, author from an author act, …);
``source_character_id`` is required for testimony and admitted for observation and discovery.

**Dual confidence** (FR-BELIEF-4). ``Confidence(level, scalar)``: the scalar, when present, is the
truth and projects onto the level by fixed bands (``project``: ``[0, 0.2)`` unknown, ``[0.2,
0.5)`` suspected, ``[0.5, 0.9)`` believed, ``[0.9, 1]`` known); a level that disagrees with its
scalar is ``ConfidenceInconsistent`` (422 ``confidence_inconsistent``); a scalar with more than
three decimals or outside ``[0, 1]`` is 422 ``validation_error``; a level alone is valid; neither
takes the kind's default (``observation`` and ``author``: known; the rest: believed).

**What a transition does to the partition.**

- The holder already holds the proposition (same subject, predicate, object, qualifiers and
  polarity: #159's ``normalized_key`` match, because #131's store has no shape upsert): the
  belief changes **in place**. A ``revision`` sets the confidence; any other kind raises it to
  at least the new level (strengthening testimony). A decayed belief is held again with full
  accessibility. The transition records the state it replaced (``prior``). A ``resolution``
  held from before that row starts (#165's knowledge obligation, held from the thunk's time;
  #547) also writes the **backdated hold**: a second ``resolution`` transition citing the same
  thunk produces a row of its own, held from the obligation's start and closed where the held
  row starts (``held_from`` is immutable and one row is open per proposition), so the holder
  holds it from then with no gap. It is skipped when another row of the key, or one
  contradicting it, is held in that span.
- Otherwise a new belief row is written, produced by the transition.
- A held belief the new one contradicts (the same key with the other polarity, or, for a
  functional predicate, the same subject and qualifiers with another object over overlapping
  valid-time) is **closed** whole: ``held_to`` at the new belief's ``held_from``, ``tx_to``
  now, ``state = corrected``, ``corrected_by`` the new belief (bitemporal history: the holder
  can be shown to have been wrong, then corrected; FR-BELIEF-10, U-12). A ``correction`` needs
  such a prior.
- ``forgetting`` (FR-BELIEF-10) cites the belief itself: the scalar halves (or, with none, the
  level drops one band), the accessibility drops by 0.3 and the belief is marked ``decayed``
  (#143's consolidation reads ``decayed_beliefs``); it never contradicts, and at ``unknown``
  the belief leaves ``belief_state``.
- A testimony's ``outcome``: ``reject`` believes the negation, ``suspend`` and
  ``speech_act_only`` hold it at ``unknown``.
- ``belief_kind = speech_act`` (#162; testimony only, a source required) writes the witness's
  belief that the source *said* the proposition (``believes(H, said(S, X))``): a row of its own
  kind, never contradicting or closing a first-order belief and never in ``belief_state``; the
  same claim from the same source changes that row in place. ``core.epistemic.testimony`` gives
  every witness one, whatever it made of the claim.
- ``belief_kind = nested`` / ``attributed`` (#167, ``core.epistemic.nested``'s writers):
  ``shape`` carries the nested columns (``partitions.BELIEF_SHAPE_COLUMNS``). An ``attributed``
  row is the immutable inner content of a nested belief: always a new row, never matched, closed
  or projected. A ``nested`` row matches and contradicts only the holder's open nested rows with
  the same attribution (attributed holder and inner chain, ``signature``): the same content is
  changed in place (strengthened, or set by a revision), a contradicting one closed whole as a
  first-order prior is. An ``inference`` of either may cite what the holder perceived (an
  event, an utterance or a staged beat: the overheard speech act, ``NESTED_ANTECEDENTS``), not
  only a belief of its own.
- Citing the same antecedent twice for the same belief and kind answers the first transition
  (idempotent).

Every transition appends a ``derived`` ``belief_transition`` event, is a derived record of its
operation (#130's registry: truncation, swap and fork move it), indexes its holder and source as
declared subjects for erasure, and counts in ``epistrel_belief_transitions_total{kind}``
(refusals in ``epistrel_belief_transitions_refused_total``). A membership flip re-settles the
beliefs its transitions touched (``follow_operations``): an in-place change reverts to the
latest active transition's state, and a prior closed by a correction whose operation left the
timeline is reopened (and re-closed when it returns).

``transition()`` raises ``ModuleOff`` when the story's configuration has ``partitions`` off;
callers check the module first. A belief never writes a fact: nothing here commits canon
(``tools.gates``, X-02). Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import readset
from epistrel.core.antecedents import RecordAntecedent, is_author_act, probe_record
from epistrel.core.canon.errors import UnknownPredicate
from epistrel.core.enums import (
    AntecedentKind,
    BeliefConfidenceLevel,
    BeliefHoldState,
    BeliefKind,
    BeliefTransitionKind,
    Membership,
    ThunkState,
)
from epistrel.core.epistemic import partitions
from epistrel.core.epistemic.errors import (
    AntecedentInvalid,
    ConfidenceInconsistent,
    SelfAttestation,
)
from epistrel.core.epistemic.partitions import Belief, BeliefOperation
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import belief_transition_record
from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.events.types import BeliefTransitioned
from epistrel.core.ids import new_ulid
from epistrel.core.membership import (
    Position,
    active_only,
    position_key,
    position_literal,
    register_follower,
)
from epistrel.core.propositions.model import Cardinality, Polarity, Proposition
from epistrel.core.propositions.registry import Registry, canonical, load
from epistrel.core.tables import characters, events
from epistrel.core.tables.base import ulid_type
from epistrel.core.tables.beliefs import TRANSITION_SOURCE_KINDS, belief_transitions, beliefs
from epistrel.core.tables.commitments import thunks
from epistrel.observability.metrics import BELIEF_TRANSITIONS, BELIEF_TRANSITIONS_REFUSED

BELIEF_TRANSITION = "belief_transition"
PARTITIONS_MODULE = "partitions"

Kind = BeliefTransitionKind
Level = BeliefConfidenceLevel

# -- confidence --------------------------------------------------------------------------------

BANDS: tuple[tuple[float, Level], ...] = (
    (0.2, Level.UNKNOWN),
    (0.5, Level.SUSPECTED),
    (0.9, Level.BELIEVED),
)
"""The scalar bands below each threshold; ``[0.9, 1]`` is ``known`` (FR-BELIEF-4)."""

FLOORS: Mapping[Level, float] = {
    Level.UNKNOWN: 0.0,
    Level.SUSPECTED: 0.2,
    Level.BELIEVED: 0.5,
    Level.KNOWN: 0.9,
}
"""Each level's lowest scalar: what an in-place level change sets a belief's scalar to."""

DEFAULT_LEVELS: Mapping[Kind, Level] = {Kind.OBSERVATION: Level.KNOWN, Kind.AUTHOR: Level.KNOWN}
"""A kind's level when the caller gives no confidence (the others: ``believed``)."""


def project(scalar: float) -> Level:
    """The categorical level a scalar confidence projects onto (FR-BELIEF-4's bands)."""
    for below, level in BANDS:
        if scalar < below:
            return level
    return Level.KNOWN


@dataclass(frozen=True, slots=True)
class Confidence:
    """A belief's dual confidence as a caller gives it: a level, a scalar, both or neither."""

    level: Level | None = None
    scalar: float | None = None


def resolve_confidence(kind: Kind, confidence: Confidence | None) -> tuple[Level, float | None]:
    """The ``(level, scalar)`` a transition writes (see the module docstring): 422
    ``validation_error`` for a scalar outside ``[0, 1]`` or with more than three decimals, 422
    ``confidence_inconsistent`` for a level its scalar does not project onto."""
    given = confidence or Confidence()
    scalar = given.scalar
    if scalar is not None:
        if isinstance(scalar, bool) or not math.isfinite(float(scalar)) or not 0 <= scalar <= 1:
            raise InvalidRequest(
                "a confidence scalar is between 0 and 1",
                {"field": "confidence_scalar", "reason": "out_of_range"},
            )
        exponent = Decimal(str(scalar)).normalize().as_tuple().exponent
        if isinstance(exponent, int) and exponent < -3:
            raise InvalidRequest(
                "a confidence scalar has at most three decimals",
                {"field": "confidence_scalar", "reason": "precision"},
            )
        projected = project(float(scalar))
        if given.level is not None and Level(given.level) is not projected:
            raise ConfidenceInconsistent(
                "a belief's level and scalar contradict each other (FR-BELIEF-4)",
                {
                    "confidence_level": Level(given.level).value,
                    "confidence_scalar": scalar,
                    "projected": projected.value,
                },
            )
        return projected, float(scalar)
    if given.level is not None:
        return Level(given.level), None
    return DEFAULT_LEVELS.get(kind, Level.BELIEVED), None


def _rank(level: Level) -> int:
    return partitions.LEVELS.index(Level(level))


# -- antecedents ---------------------------------------------------------------------------------


class Antecedent(BaseModel):
    """A transition's cause: its ``kind`` and ``ref`` (``{table, id}`` for a record;
    ``{operation_id, variant, beat}`` for a staged beat, plus ``step`` for a render step;
    ``{thunk_id}`` for a resolution)."""

    model_config = ConfigDict(frozen=True)

    kind: AntecedentKind
    ref: dict[str, Any]

    @classmethod
    def record(cls, table: str, record_id: str) -> Antecedent:
        return cls(kind=AntecedentKind.RECORD, ref={"table": table, "id": record_id})

    @classmethod
    def staged_beat(cls, operation_id: str, variant: int, beat: int) -> Antecedent:
        return cls(
            kind=AntecedentKind.STAGED_BEAT,
            ref={"operation_id": operation_id, "variant": variant, "beat": beat},
        )

    @classmethod
    def render_step(cls, operation_id: str, variant: int, beat: int, step: int) -> Antecedent:
        return cls(
            kind=AntecedentKind.RENDER_STEP,
            ref={"operation_id": operation_id, "variant": variant, "beat": beat, "step": step},
        )

    @classmethod
    def resolution(cls, thunk_id: str) -> Antecedent:
        return cls(kind=AntecedentKind.RESOLUTION, ref={"thunk_id": thunk_id})

    def document(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "ref": dict(self.ref)}


@dataclass(frozen=True, slots=True)
class StagedRecord:
    """What an oracle answers for a verified staged beat or render step: its position and the
    proposition keys it verified (empty: not reported)."""

    position: Position
    verified_props: frozenset[str] = frozenset()


class AntecedentOracle(Protocol):
    """Reaches the antecedents that are verified but not yet committed: #174's ``TurnStage``
    (staged beats) and #168's step verifier (render steps)."""

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None: ...

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None: ...


class RefusingOracle:
    """The default oracle: nothing staged is verified outside a turn's staging."""

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return None

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return None


DEFAULT_ORACLE: AntecedentOracle = RefusingOracle()

_ALL_RECORDS = frozenset({"facts", "events", "beliefs", "utterances"})
_A = AntecedentKind

KIND_ANTECEDENTS: Mapping[Kind, Mapping[AntecedentKind, frozenset[str]]] = {
    Kind.TESTIMONY: {_A.RECORD: frozenset({"utterances", "events"}), _A.STAGED_BEAT: frozenset()},
    Kind.OBSERVATION: {
        _A.RECORD: frozenset({"events"}),
        _A.STAGED_BEAT: frozenset(),
        _A.RENDER_STEP: frozenset(),
    },
    Kind.INFERENCE: {_A.RECORD: frozenset({"beliefs"}), _A.RENDER_STEP: frozenset()},
    Kind.DISCOVERY: {_A.RECORD: frozenset({"facts", "events"}), _A.STAGED_BEAT: frozenset()},
    Kind.CORRECTION: {
        _A.RECORD: _ALL_RECORDS,
        _A.STAGED_BEAT: frozenset(),
        _A.RENDER_STEP: frozenset(),
    },
    Kind.REVISION: {
        _A.RECORD: _ALL_RECORDS,
        _A.STAGED_BEAT: frozenset(),
        _A.RENDER_STEP: frozenset(),
    },
    Kind.FORGETTING: {_A.RECORD: frozenset({"beliefs"})},
    Kind.AUTHOR: {_A.RECORD: frozenset({"events"})},
    Kind.RESOLUTION: {_A.RESOLUTION: frozenset()},
}
"""Which antecedents each kind admits (for a ``record``, which tables). #168: an ``inference``
may rest on a verified earlier ``believe`` step of the same render (a chain of reasoning)."""

NESTED_ANTECEDENTS: Mapping[Kind, Mapping[AntecedentKind, frozenset[str]]] = {
    Kind.INFERENCE: {
        _A.RECORD: frozenset({"beliefs", "events", "utterances"}),
        _A.STAGED_BEAT: frozenset(),
    },
}
"""#167: what a nested or attributed belief's kind admits beyond ``KIND_ANTECEDENTS``: an
inference about what someone believes may rest on the speech act the holder perceived."""

BACKDATING_KINDS = frozenset({Kind.RESOLUTION, Kind.AUTHOR})
"""The kinds whose ``story_time`` may differ from their position's instant."""


@dataclass(frozen=True, slots=True)
class _Checked:
    """A validated antecedent: its position (``None``: position-less or order-exempt) and the
    record it named, if any."""

    position: Position | None
    record: RecordAntecedent | None = None


def _invalid(reason: str, antecedent: Antecedent, **more: Any) -> AntecedentInvalid:
    return AntecedentInvalid(
        f"the antecedent is not a legitimate cause ({reason})",
        {"reason": reason, "antecedent": antecedent.document(), **more},
    )


async def _check_antecedent(
    conn: AsyncConnection,
    op: BeliefOperation,
    story_id: str,
    *,
    holder: str,
    kind: Kind,
    antecedent: Antecedent,
    position: Position,
    source: str | None,
    oracle: AntecedentOracle,
    belief_kind: BeliefKind = BeliefKind.FIRST_ORDER,
) -> _Checked:
    allowed = KIND_ANTECEDENTS[kind]
    if belief_kind in (BeliefKind.NESTED, BeliefKind.ATTRIBUTED) and kind in NESTED_ANTECEDENTS:
        allowed = NESTED_ANTECEDENTS[kind]
    if antecedent.kind not in allowed:
        raise _invalid("kind_mismatch", antecedent, kind=kind.value)
    ref = antecedent.ref
    if antecedent.kind is AntecedentKind.RECORD:
        table, record_id = str(ref.get("table", "")), str(ref.get("id", ""))
        if table not in allowed[AntecedentKind.RECORD]:
            raise _invalid("kind_mismatch", antecedent, kind=kind.value)
        found = await probe_record(conn, story_id, table, record_id, operation_id=op.operation_id)
        if found is None:
            raise _invalid("not_found", antecedent)
        if not found.usable:
            raise _invalid(found.reason or "unusable", antecedent)
        if kind is Kind.AUTHOR and not is_author_act(found):
            raise _invalid("not_an_author_act", antecedent)
        if table == "beliefs" and found.holder_id != holder:
            raise _invalid("not_the_holders_own", antecedent)
        if table == "utterances" and source is not None and found.speaker_id != source:
            raise _invalid("source_mismatch", antecedent)
        if kind is Kind.INFERENCE and table == "beliefs":
            premise = await partitions.read_belief(conn, story_id, record_id)
            if premise is None or not _holds(premise, position):
                raise _invalid("premise_not_held", antecedent)
        # An author act (a control event: a directive, a seed) is position-less: it precedes
        # every step, its own operation's included.
        position_of = None if is_author_act(found) else found.position
        return _Checked(position_of, found)
    if antecedent.kind in (AntecedentKind.STAGED_BEAT, AntecedentKind.RENDER_STEP):
        if str(ref.get("operation_id")) != op.operation_id:
            raise _invalid("other_operation", antecedent)
        if antecedent.kind is AntecedentKind.STAGED_BEAT:
            # #175: an oracle may refuse a staged beat outright (a simultaneous turn's stage).
            refusal = getattr(oracle, "refusal", None)
            reason = None if refusal is None else refusal(ref)
            if reason is not None:
                raise _invalid(str(reason), antecedent)
        staged = (
            oracle.staged_beat(ref)
            if antecedent.kind is AntecedentKind.STAGED_BEAT
            else oracle.render_step(ref)
        )
        if staged is None:
            raise _invalid("unverified", antecedent)
        return _Checked(staged.position)
    # resolution: the thunk this operation resolved; exempt from the order rule (FR-EPI-14 (d))
    row = (
        await conn.execute(
            select(thunks.c.state, thunks.c.resolution_operation_id).where(
                thunks.c.story_id == story_id, thunks.c.id == str(ref.get("thunk_id", ""))
            )
        )
    ).one_or_none()
    if row is None:
        raise _invalid("not_found", antecedent)
    if (
        str(row.state) != ThunkState.RESOLVED.value
        or str(row.resolution_operation_id) != op.operation_id
    ):
        raise _invalid("not_reconciled", antecedent)
    return _Checked(None)


def _holds(belief: Belief, position: Position) -> bool:
    return (
        belief.membership is Membership.ACTIVE
        and belief.state is not BeliefHoldState.CORRECTED
        and belief.held_from.before(position)
        and (belief.held_to is None or position.before(belief.held_to))
    )


def check_order(antecedent: Antecedent, found: Position | None, position: Position) -> None:
    """FR-EPI-14's order rule: the antecedent strictly precedes the dependent step."""
    if found is None:
        return
    same_turn = (found.turn, found.slot_k) == (position.turn, position.slot_k)
    if same_turn and found.variant != position.variant:
        raise _invalid("other_variant", antecedent)
    if not found.before(position):
        raise SelfAttestation(
            "a belief's antecedent must precede the step that depends on it (FR-EPI-14)",
            {
                "antecedent": antecedent.document(),
                "antecedent_position": found.document(),
                "position": position.document(),
            },
        )


# -- results ---------------------------------------------------------------------------------------


class Transition(BaseModel):
    """One ``belief_transitions`` row (see ``core.tables.beliefs``)."""

    model_config = ConfigDict(frozen=True)

    id: str
    story_id: str
    holder_character_id: str
    belief_id: str
    kind: Kind
    antecedent: Antecedent
    source_character_id: str | None
    valid_at: int | None
    confidence_level: Level
    confidence_scalar: float | None
    accessibility: float
    state: BeliefHoldState
    outcome: str | None
    prior_belief_ids: tuple[str, ...]
    prior: dict[str, Any] | None
    position: Position
    tx_from: datetime
    operation_id: str
    membership: Membership


def _transition_of(row: Any) -> Transition:
    m = row._mapping
    position = Position.of_columns(m, "position")
    assert position is not None  # noqa: S101 - NOT NULL
    return Transition(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        holder_character_id=str(m["holder_character_id"]),
        belief_id=str(m["belief_id"]),
        kind=Kind(m["kind"]),
        antecedent=Antecedent(kind=AntecedentKind(m["antecedent_kind"]), ref=m["antecedent_ref"]),
        source_character_id=None
        if m["source_character_id"] is None
        else str(m["source_character_id"]),
        valid_at=m["valid_at"],
        confidence_level=Level(m["confidence_level"]),
        confidence_scalar=None if m["confidence_scalar"] is None else float(m["confidence_scalar"]),
        accessibility=float(m["accessibility"]),
        state=BeliefHoldState(m["state"]),
        outcome=m["outcome"],
        prior_belief_ids=tuple(str(x) for x in m["prior_belief_ids"] or ()),
        prior=m["prior"],
        position=position,
        tx_from=m["tx_from"],
        operation_id=str(m["operation_id"]),
        membership=Membership(m["membership"]),
    )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What ``transition()`` did: the transition, the belief after it, whether a new belief row
    was written (``created``), the beliefs it closed and whether it answered an earlier
    identical transition (``replayed``)."""

    transition: Transition
    belief: Belief
    created: bool = False
    closed: tuple[str, ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Forgetting:
    """FR-BELIEF-10's decay step: the scalar's factor, the bands a scalar-less level drops, and
    the accessibility change."""

    scalar_factor: float = 0.5
    level_steps: int = 1
    accessibility_delta: float = -0.3


FORGETTING = Forgetting()


@dataclass(frozen=True, slots=True)
class HeldState:
    """A belief's in-place state: its dual confidence, accessibility and hold state."""

    level: Level
    scalar: float | None
    accessibility: float
    state: BeliefHoldState

    def document(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "scalar": self.scalar,
            "accessibility": self.accessibility,
            "state": self.state.value,
        }

    @classmethod
    def of(cls, belief: Belief) -> HeldState:
        return cls(
            belief.confidence_level, belief.confidence_scalar, belief.accessibility, belief.state
        )


def _round(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.001")))


def decayed(current: HeldState, step: Forgetting = FORGETTING) -> HeldState:
    """The state a forgetting transition leaves: see the module docstring."""
    if current.scalar is not None:
        scalar: float | None = _round(current.scalar * step.scalar_factor)
        level = project(scalar or 0.0)
    else:
        scalar = None
        level = partitions.LEVELS[max(0, _rank(current.level) - step.level_steps)]
    access = max(0.0, _round(current.accessibility + step.accessibility_delta))
    return HeldState(level, scalar, access, BeliefHoldState.DECAYED)


def _revised(kind: Kind, current: HeldState, level: Level, scalar: float | None) -> HeldState:
    """The in-place state after a non-forgetting transition on a held belief."""
    if kind is not Kind.REVISION and _rank(level) < _rank(current.level):
        level, scalar = current.level, current.scalar  # never weakens: strengthening only
    elif scalar is None and current.scalar is not None:
        scalar = current.scalar if level is current.level else FLOORS[level]
    access = 1.0 if current.state is BeliefHoldState.DECAYED else current.accessibility
    return HeldState(level, scalar, access, BeliefHoldState.HELD)


# -- the write -------------------------------------------------------------------------------------


async def _module_guard(conn: AsyncConnection, story_id: str, settings: Any) -> None:
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    found = settings
    if found is None:
        found = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(found, PARTITIONS_MODULE):
        raise ModuleOff(
            "the story's configuration keeps no character partitions",
            {"story_id": story_id, "module": PARTITIONS_MODULE},
        )


async def _story_time(conn: AsyncConnection, story_id: str, position: Position) -> int:
    from epistrel.core.resolution.context import load_time_basis
    from epistrel.core.resolution.time import story_time_at

    return story_time_at(position.turn, await load_time_basis(conn, story_id))


def _conflicts(stored: Proposition, belief: Belief, registry: Registry) -> bool:
    """Whether ``belief`` contradicts ``stored`` (see the module docstring)."""
    held = belief.proposition
    if held.normalized_key() == stored.normalized_key():
        return held.polarity is not stored.polarity
    predicate = registry.get(stored.predicate_id)
    if (
        predicate is None
        or predicate.cardinality is not Cardinality.FUNCTIONAL
        or held.subject_id != stored.subject_id
        or held.predicate_id != stored.predicate_id
        or held.qualifier_key() != stored.qualifier_key()
        or held.polarity is not Polarity.AFFIRMED
        or stored.polarity is not Polarity.AFFIRMED
    ):
        return False
    lo, hi = stored.valid_time.possible()
    lo_b, hi_b = belief.valid_from, belief.valid_to
    return (hi is None or lo_b is None or lo_b < hi) and (hi_b is None or lo is None or lo < hi_b)


conflicts = _conflicts
"""Whether a belief contradicts a stored proposition (#164's fold-forward reads it)."""


async def _replay(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    kind: Kind,
    antecedent: Antecedent,
    belief_id: str,
) -> Transition | None:
    t = belief_transitions.c
    row = (
        await conn.execute(
            select(belief_transitions)
            .where(
                t.story_id == story_id,
                t.holder_character_id == holder,
                t.belief_id == belief_id,
                t.kind == kind.value,
                t.antecedent_kind == antecedent.kind.value,
                t.antecedent_ref.contains(antecedent.ref),
                t.antecedent_ref.contained_by(antecedent.ref),
                active_only(belief_transitions),
            )
            .order_by(t.id)
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else _transition_of(row)


async def _check_source(
    conn: AsyncConnection, story_id: str, kind: Kind, source: str | None
) -> None:
    if kind is Kind.TESTIMONY and source is None:
        raise InvalidRequest(
            "a testimony names who asserted it", {"field": "source", "reason": "required"}
        )
    if source is None:
        return
    if kind.value not in TRANSITION_SOURCE_KINDS:
        raise InvalidRequest(
            f"a {kind.value} transition names no source",
            {"field": "source", "reason": "not_admitted"},
        )
    found = await conn.scalar(
        select(characters.c.id).where(characters.c.story_id == story_id, characters.c.id == source)
    )
    if found is None:
        raise InvalidRequest(
            "a transition's source is a character of the story",
            {"field": "source", "reason": "unknown_character"},
        )


async def _set_state(conn: AsyncConnection, belief: Belief, after: HeldState) -> None:
    await conn.execute(
        update(beliefs)
        .where(beliefs.c.id == belief.id)
        .values(
            confidence_level=after.level.value,
            confidence_scalar=after.scalar,
            accessibility=after.accessibility,
            state=after.state.value,
        )
    )


async def _record_changes(
    conn: AsyncConnection,
    story_id: str,
    operation_id: str,
    changed: Iterable[tuple[Belief, readset.ChangeKind]],
) -> None:
    rows = [
        readset.Change(
            proposition=b.proposition,
            proposition_id=b.proposition_id,
            change=kind,
            valid_window=(b.valid_from, b.valid_to),
            visibility=b.visibility.document(),
            speaker_id=b.holder_character_id,
            holder=readset.BELIEF,
        )
        for b, kind in changed
    ]
    if rows:
        await readset.record_change(conn, story_id, operation_id, rows)


async def _close(conn: AsyncConnection, priors: Sequence[Belief], *, at: Position, by: str) -> None:
    """Close ``priors`` whole at ``at`` (held), now (transaction-time), corrected by ``by``."""
    for prior in priors:
        await conn.execute(
            update(beliefs)
            .where(beliefs.c.id == prior.id)
            .values(
                **at.columns("held_to"),
                tx_to=func.clock_timestamp(),
                state=BeliefHoldState.CORRECTED.value,
                corrected_by=by,
            )
        )


async def transition(
    conn: AsyncConnection,
    op: BeliefOperation,
    *,
    holder: str,
    kind: Kind | str,
    proposition: Proposition,
    antecedent: Antecedent,
    position: Position,
    polarity: Polarity | str | None = None,
    held_from: Position | None = None,
    confidence: Confidence | None = None,
    story_time: int | None = None,
    source: str | None = None,
    outcome: str | None = None,
    oracle: AntecedentOracle = DEFAULT_ORACLE,
    settings: Any = None,
    registry: Registry | None = None,
    belief_kind: BeliefKind = BeliefKind.FIRST_ORDER,
    frame: Position | None = None,
    resolution: bool = False,
    shape: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Record one belief transition of ``holder`` in ``op``'s commit transaction (a commit
    step) and apply it to the partition; see the module docstring. ``position`` is the
    dependent step (callers apply the witness ``step + 1`` rule); ``held_from`` defaults to it.
    ``settings`` is the story's effective configuration (read when ``None``).

    ``frame`` (#164's lazy resolution): the partition the transition is decided against is the
    holder's as of that position (beliefs held there and still open now), not now's, so a
    belief written at an exposure position neither matches nor closes a belief held only from a
    later position (the resolver folds those forward itself). ``resolution`` marks the belief
    written as a lazy resolution (``beliefs.resolution_operation_id`` = ``op``). ``shape``: a
    nested, attributed or speech-act row's columns (#167)."""
    tally = _TALLY.get()
    try:
        result = await _transition(
            conn,
            op,
            holder=holder,
            kind=Kind(kind),
            proposition=proposition,
            antecedent=antecedent,
            position=position,
            polarity=polarity,
            held_from=held_from,
            confidence=confidence,
            story_time=story_time,
            source=source,
            outcome=outcome,
            oracle=oracle,
            settings=settings,
            registry=registry,
            belief_kind=BeliefKind(belief_kind),
            frame=frame,
            resolution=resolution,
            shape=shape,
        )
    except SelfAttestation, AntecedentInvalid, ConfidenceInconsistent:
        BELIEF_TRANSITIONS_REFUSED.inc()
        if tally is not None:
            tally.refused += 1
        raise
    written = [result]
    start = held_from or position
    if (
        Kind(kind) is Kind.RESOLUTION
        and not result.replayed
        and not result.created
        and BeliefKind(belief_kind) is BeliefKind.FIRST_ORDER
        and start.before(result.belief.held_from)
        and await _backdatable(conn, result.belief, start, registry)
    ):
        # #165 (FR-COMMIT-14): the binding obliges the holder to have held the act from the
        # thunk's time, but it already holds it from a later position (raised in place above,
        # the replay anchor). Its ``held_from`` is immutable and one row is open per
        # proposition, so the earlier span is a row of its own, held from ``start`` and closed
        # where the held row starts, by a second ``resolution`` transition citing the same thunk.
        written.append(
            await _transition(
                conn,
                op,
                holder=holder,
                kind=Kind.RESOLUTION,
                proposition=proposition,
                antecedent=antecedent,
                position=position,
                polarity=polarity,
                held_from=start,
                confidence=confidence,
                story_time=story_time,
                source=source,
                outcome=outcome,
                oracle=oracle,
                settings=settings,
                registry=registry,
                backdate=result.belief,
            )
        )
    for each in written:
        if each.replayed:
            continue
        BELIEF_TRANSITIONS.labels(kind=each.transition.kind.value).inc()
        if tally is not None:
            kind_name = each.transition.kind.value
            tally.by_kind[kind_name] = tally.by_kind.get(kind_name, 0) + 1
    return result


async def _backdatable(
    conn: AsyncConnection, held: Belief, start: Position, registry: Registry | None
) -> bool:
    """Whether ``held``'s holder may be shown holding it from ``start`` (before ``held`` starts):
    no other row of its key, and none contradicting it, is held anywhere in ``[start, held)``."""
    from epistrel.core.tables.propositions import propositions

    found = registry if registry is not None else await load(conn, held.story_id)
    b = beliefs.c
    p = propositions.c
    rows = await conn.execute(
        partitions._select().where(
            b.story_id == held.story_id,
            b.holder_character_id == held.holder_character_id,
            b.id != held.id,
            p.subject_id == held.proposition.subject_id,
            p.predicate_id == held.proposition.predicate_id,
            *partitions._held_then(),
            position_key(beliefs, "held_from") < position_literal(held.held_from),
        )
    )
    for row in rows:
        other = partitions._belief_of(row)
        if other.held_to is not None and other.held_to.key() <= start.key():
            continue
        same = other.proposition.normalized_key() == held.proposition.normalized_key()
        if same or _conflicts(held.proposition, other, found):
            return False
    return True


@dataclass(slots=True)
class Tally:
    """The transitions one commit wrote, by kind, and the ones refused
    (``turn_report.epistemic.transitions``)."""

    by_kind: dict[str, int] = field(default_factory=dict)
    refused: int = 0

    def report(self) -> dict[str, Any]:
        return {"by_kind": dict(sorted(self.by_kind.items())), "refused": self.refused}


_TALLY: ContextVar[Tally | None] = ContextVar("epistrel_transition_tally", default=None)


@contextmanager
def tallying() -> Iterator[Tally]:
    """Within the block, every ``transition()`` call is counted in the yielded ``Tally``."""
    found = Tally()
    token = _TALLY.set(found)
    try:
        yield found
    finally:
        _TALLY.reset(token)


def refused() -> None:
    """Count a transition a caller refused before calling ``transition()`` (a sink's own guard)."""
    tally = _TALLY.get()
    if tally is not None:
        tally.refused += 1


async def _transition(
    conn: AsyncConnection,
    op: BeliefOperation,
    *,
    holder: str,
    kind: Kind,
    proposition: Proposition,
    antecedent: Antecedent,
    position: Position,
    polarity: Polarity | str | None,
    held_from: Position | None,
    confidence: Confidence | None,
    story_time: int | None,
    source: str | None,
    outcome: str | None,
    oracle: AntecedentOracle,
    settings: Any,
    registry: Registry | None,
    belief_kind: BeliefKind = BeliefKind.FIRST_ORDER,
    frame: Position | None = None,
    resolution: bool = False,
    shape: Mapping[str, Any] | None = None,
    backdate: Belief | None = None,
) -> TransitionResult:
    story_id = op.story_id
    if story_id is None:
        raise InvalidRequest("a belief belongs to a story", {"field": "operation"})
    await _module_guard(conn, story_id, settings)
    await partitions._holder_ok(conn, story_id, holder)
    await _check_source(conn, story_id, kind, source)
    if outcome is not None and (kind is not Kind.TESTIMONY or outcome not in _OUTCOMES):
        raise InvalidRequest(
            "an outcome is a testimony's accept, reject, suspend or speech_act_only",
            {"field": "outcome", "reason": "not_admitted"},
        )
    if story_time is not None and kind not in BACKDATING_KINDS:
        raise InvalidRequest(
            f"a {kind.value} transition happens at its position's story-time",
            {"field": "story_time", "reason": "backdating"},
        )
    said = belief_kind is BeliefKind.SPEECH_ACT
    columns = dict(shape or {})
    content = belief_kind is BeliefKind.ATTRIBUTED or (
        said and columns.get("attributed_holder_id") is not None
    )
    nested = belief_kind is BeliefKind.NESTED
    if said and not content and kind is not Kind.TESTIMONY:
        raise InvalidRequest(
            "a speech-act belief is a testimony's: someone said it",
            {"field": "belief_kind", "reason": "not_admitted"},
        )
    if said:
        columns.setdefault("speaker_character_id", source)
        columns.setdefault("depth", 2)
        if columns["speaker_character_id"] is None:
            raise InvalidRequest(
                "a speech-act belief names who said it", {"field": "source", "reason": "required"}
            )
    if (nested or belief_kind is BeliefKind.ATTRIBUTED) and columns.get(
        "attributed_holder_id"
    ) is None:
        raise InvalidRequest(
            "a nested belief names whom it attributes the belief to",
            {"field": "attributed_holder", "reason": "required"},
        )
    if nested and columns.get("inner_belief_id") is None:
        raise InvalidRequest(
            "a nested belief names its inner belief", {"field": "inner", "reason": "required"}
        )
    level, scalar = resolve_confidence(kind, confidence)
    if polarity is not None:
        proposition = proposition.model_copy(update={"polarity": Polarity(polarity)})
    if said or content or nested:
        pass  # the speech act or the attribution itself, whatever the witness made of it
    elif outcome == "reject":
        proposition = proposition.negation()
    elif outcome in ("suspend", "speech_act_only"):
        level, scalar = Level.UNKNOWN, None
    found = registry if registry is not None else await load(conn, story_id)
    if found.get(proposition.predicate_id) is None:
        raise UnknownPredicate(
            f"predicate {proposition.predicate_id} is not registered for the story",
            {"field": "proposition.predicate_id", "predicate_id": proposition.predicate_id},
        )
    stored = canonical(proposition, found)
    checked = await _check_antecedent(
        conn,
        op,
        story_id,
        holder=holder,
        kind=kind,
        antecedent=antecedent,
        position=position,
        source=source,
        oracle=oracle,
        belief_kind=belief_kind,
    )
    if antecedent.kind is not AntecedentKind.RESOLUTION:
        check_order(antecedent, checked.position, position)
    named = (
        holder,
        source,
        columns.get("attributed_holder_id"),
        columns.get("speaker_character_id"),
    )
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((), frozenset(str(c) for c in named if c is not None))],
        base_revision=None,
        path=__name__,
    )
    when = story_time if story_time is not None else await _story_time(conn, story_id, position)

    key = stored.normalized_key()
    if backdate is not None:
        held: list[Belief] = []  # #165: the backdated hold, closed where ``backdate`` starts
    elif content:
        held = []  # an inner content row is immutable: always its own row (#167)
    elif said:
        # A speech act never contradicts a belief: the same claim from the same speaker is the
        # same ``said`` row (strengthened in place), any other is a new one (#162).
        held = [
            a.belief
            for a in await partitions.speech_acts(
                conn, story_id, holder, speaker=columns["speaker_character_id"]
            )
            if a.belief.proposition.subject_id == stored.subject_id
            and a.belief.proposition.predicate_id == stored.predicate_id
        ]
    elif nested:
        # #167: only the holder's open nested rows with the same attribution are its prior.
        wanted = await _signature_of(conn, story_id, columns)
        held = [
            n.belief
            for n in await partitions.nested_of(
                conn,
                story_id,
                holder,
                attributed_holder=str(columns["attributed_holder_id"]),
                subjects=[stored.subject_id],
                predicate=stored.predicate_id,
            )
            if signature(n) == wanted
        ]
    elif frame is not None:
        # #164: the holder's partition as of the exposure frame, as far as it is still open.
        held = [
            b
            for b in await partitions.partition(
                conn,
                story_id,
                holder,
                through=frame,
                subjects=[stored.subject_id],
                predicate=stored.predicate_id,
            )
            if b.tx_to is None and b.held_to is None
        ]
    else:
        held = await partitions.partition(
            conn,
            story_id,
            holder,
            subjects=[stored.subject_id],
            predicate=stored.predicate_id,
        )
    existing = next(
        (
            b
            for b in held
            if b.proposition.normalized_key() == key and b.polarity == stored.polarity.value
        ),
        None,
    )
    if said or content:
        priors: list[Belief] = []
    elif kind is Kind.FORGETTING:
        if existing is None or antecedent.ref.get("id") != existing.id:
            raise _invalid("forgetting_cites_its_belief", antecedent)
        priors = []
    else:
        priors = [b for b in held if b is not existing and _conflicts(stored, b, found)]
    if kind is Kind.CORRECTION and not priors:
        raise InvalidRequest(
            "a correction replaces a held belief it contradicts; the holder holds none",
            {"field": "proposition", "reason": "nothing_to_correct"},
        )
    if existing is not None and kind is not Kind.FORGETTING:
        replayed = await _replay(conn, story_id, holder, kind, antecedent, existing.id)
        if replayed is not None:
            return TransitionResult(replayed, existing, replayed=True)

    start = held_from or position
    for prior in priors:
        if not prior.held_from.before(start):
            raise InvalidRequest(
                "a belief is closed by a later one only",
                {"field": "held_from", "reason": "held_already", "belief_id": prior.id},
            )
    if existing is None and frame is not None and belief_kind is BeliefKind.FIRST_ORDER:
        # #182 (Rule 1): a frame's partition leaves out a belief held only from after the
        # frame, which ``_insert_belief`` would find open and refuse (``held_already``). Refuse
        # it here, before anything is written: a caller that lets the said belief stand (a lazy
        # resolution) must not leave this transition's row without its belief.
        later = next(
            (
                b
                for b in await partitions.partition(
                    conn,
                    story_id,
                    holder,
                    subjects=[stored.subject_id],
                    predicate=stored.predicate_id,
                )
                if b.proposition.normalized_key() == key
                and b.polarity == stored.polarity.value
                and b.held_to is None
                and not b.held_from.before(start)
            ),
            None,
        )
        if later is not None:
            raise InvalidRequest(
                "the holder already holds this proposition from this position or a later one",
                {"field": "held_from", "reason": "held_already", "belief_id": later.id},
            )
    transition_id = new_ulid()
    belief_id = existing.id if existing is not None else new_ulid()
    prior_ids = tuple(p.id for p in priors)
    if existing is not None:
        before = HeldState.of(existing)
        after = (
            decayed(before) if kind is Kind.FORGETTING else _revised(kind, before, level, scalar)
        )
        prior_state: dict[str, Any] | None = before.document()
    else:
        after = HeldState(level, scalar, 1.0, BeliefHoldState.HELD)
        prior_state = None

    event_id = await partitions._append_event(
        conn,
        story_id,
        op,
        BeliefTransitioned(
            transition_id=transition_id,
            belief_id=belief_id,
            kind=kind.value,
            antecedent=antecedent.document(),
            prior_belief_ids=prior_ids,
        ),
        position.beat or None,
        BELIEF_TRANSITION,
    )
    now = (await conn.execute(select(func.transaction_timestamp()))).scalar_one()
    await conn.execute(
        insert(belief_transitions).values(
            id=transition_id,
            story_id=story_id,
            holder_character_id=holder,
            belief_id=belief_id,
            kind=kind.value,
            antecedent_kind=antecedent.kind.value,
            antecedent_ref=dict(antecedent.ref),
            source_character_id=source,
            valid_at=when,
            confidence_level=after.level.value,
            confidence_scalar=after.scalar,
            accessibility=after.accessibility,
            state=after.state.value,
            outcome=outcome,
            prior_belief_ids=list(prior_ids),
            prior=prior_state,
            **position.columns("position"),
            tx_from=now,
            operation_id=op.operation_id,
            source_event_ids=[event_id],
            narrative_turn=op.narrative_turn,
            turn_variant=op.turn_variant,
            membership=Membership.ACTIVE.value,
            membership_tx=func.transaction_timestamp(),
        )
    )
    await index_records(
        conn,
        [
            belief_transition_record(
                SimpleNamespace(
                    id=transition_id,
                    story_id=story_id,
                    holder_character_id=holder,
                    source_character_id=source,
                )
            )
        ],
    )
    changed: list[tuple[Belief, readset.ChangeKind]] = []
    if existing is not None:
        await _set_state(conn, existing, after)
        if priors:
            await _close(conn, priors, at=start, by=existing.id)
        belief = await partitions.read_belief(conn, story_id, existing.id)
        assert belief is not None  # noqa: S101 - it exists
        changed.append((belief, "supersede"))
        created = False
    else:
        belief = await partitions._insert_belief(
            conn,
            story_id,
            holder,
            stored,
            operation=op,
            level=after.level,
            scalar=after.scalar,
            held_from=start,
            beat=position.beat or None,
            step=position.step,
            provenance=transition_id,
            registry=found,
            belief_id=belief_id,
            lineage_id=(
                backdate.lineage_id
                if backdate is not None
                else priors[0].lineage_id
                if priors
                else None
            ),
            kind=belief_kind,
            resolution=resolution,
            shape=columns,
            held_to=None if backdate is None else backdate.held_from,
        )
        if priors:
            await _close(conn, priors, at=start, by=belief_id)
        created = True
    if priors:
        for prior in priors:
            closed = await partitions.read_belief(conn, story_id, prior.id)
            if closed is not None:
                changed.append((closed, "close"))
    # A new belief's row is projected by its write's hook (``BELIEF_HOOKS``); a change in
    # place and a closed prior are this module's own writes.
    await partitions.refresh(
        conn,
        story_id,
        {
            *(() if created else ((holder, belief.proposition_id),)),
            *((holder, p.proposition_id) for p in priors),
        },
    )
    await _record_changes(conn, story_id, op.operation_id, changed)
    row = (
        await conn.execute(
            select(belief_transitions).where(belief_transitions.c.id == transition_id)
        )
    ).one()
    return TransitionResult(_transition_of(row), belief, created=created, closed=prior_ids)


_OUTCOMES = frozenset({"accept", "reject", "suspend", "speech_act_only"})


Signature = tuple[tuple[str, ...], str | None]
"""A nested belief's attribution (#167): its attributed holders, outermost first, and the
speaker of an innermost speech act (``None``: the content is a belief)."""


def signature(found: partitions.NestedBelief) -> Signature:
    return partitions.chain_signature(found.attributed_holder_id, found.chain)


async def _signature_of(
    conn: AsyncConnection, story_id: str, columns: Mapping[str, Any]
) -> Signature:
    outer = str(columns["attributed_holder_id"])
    inner_id = columns.get("inner_belief_id")
    inner = None if inner_id is None else await partitions.read_belief(conn, story_id, inner_id)
    chain = () if inner is None else (inner, *await partitions._chain(conn, story_id, inner))
    return partitions.chain_signature(outer, chain)


# -- reads -----------------------------------------------------------------------------------------


async def transitions_of(
    conn: AsyncConnection,
    story_id: str,
    *,
    holder: str | None = None,
    belief_ids: Sequence[str] | None = None,
    active: bool = True,
) -> list[Transition]:
    """The story's transitions (of ``holder``, on ``belief_ids``), by position then id."""
    t = belief_transitions.c
    where: list[Any] = [t.story_id == story_id]
    if holder is not None:
        where.append(t.holder_character_id == holder)
    if belief_ids is not None:
        where.append(t.belief_id.in_(list(belief_ids)))
    if active:
        where.append(active_only(belief_transitions))
    rows = await conn.execute(
        select(belief_transitions)
        .where(*where)
        .order_by(*position_key(belief_transitions, "position").clauses, t.tx_from, t.id)
    )
    return [_transition_of(r) for r in rows]


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One belief of a lineage with the transitions that produced and changed it."""

    belief: Belief
    transitions: tuple[Transition, ...] = field(default=())


async def history(conn: AsyncConnection, story_id: str, belief_id: str) -> list[HistoryEntry]:
    """The lineage of ``belief_id``: every active belief a correction chain links to it, oldest
    first, each with its active transitions (FR-BELIEF-10: belief, then correction)."""
    b = beliefs.c
    lineage = await conn.scalar(
        select(b.lineage_id).where(b.story_id == story_id, b.id == belief_id)
    )
    if lineage is None:
        return []
    ids = [
        str(i)
        for i in await conn.scalars(
            select(b.id)
            .where(b.story_id == story_id, b.lineage_id == lineage, active_only(beliefs))
            .order_by(*position_key(beliefs, "held_from").clauses, b.id)
        )
    ]
    found = await transitions_of(conn, story_id, belief_ids=ids)
    entries = []
    for i in ids:
        belief = await partitions.read_belief(conn, story_id, i)
        if belief is not None:
            entries.append(HistoryEntry(belief, tuple(t for t in found if t.belief_id == i)))
    return entries


async def decayed_beliefs(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    window: tuple[int, int] | None = None,
) -> list[Belief]:
    """``holder``'s held beliefs a forgetting marked ``decayed`` (#143's consolidation input),
    optionally only those held from a turn in ``window`` (inclusive)."""
    found = [
        x
        for x in await partitions.partition(conn, story_id, holder)
        if x.state is BeliefHoldState.DECAYED
    ]
    if window is not None:
        lo, hi = window
        found = [x for x in found if lo <= x.held_from.turn <= hi]
    return found


async def forget_covered(
    conn: AsyncConnection,
    op: BeliefOperation,
    *,
    holders: Sequence[str],
    from_turn: int,
    to_turn: int,
    settings: Any = None,
) -> int:
    """#143's consolidation of a character scope over turns ``from_turn..to_turn``: each held
    belief of ``holders`` whose source events lie in the window decays by one forgetting
    transition (citing itself), positioned at the end of ``to_turn``. Nothing when the story
    keeps no partitions. Answers the transitions written."""
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    story_id = op.story_id
    if story_id is None or not holders:
        return 0
    effective = settings
    if effective is None:
        effective = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(effective, PARTITIONS_MODULE):
        return 0
    in_window = [
        str(i)
        for i in await conn.scalars(
            select(events.c.id).where(
                events.c.story_id == story_id,
                events.c.narrative_turn >= from_turn,
                events.c.narrative_turn <= to_turn,
            )
        )
    ]
    if not in_window:
        return 0
    b = beliefs.c
    covered = [
        str(i)
        for i in await conn.scalars(
            select(b.id)
            .where(
                b.story_id == story_id,
                b.holder_character_id.in_(list(holders)),
                active_only(beliefs),
                b.tx_to.is_(None),
                b.held_to_turn.is_(None),
                b.state == BeliefHoldState.HELD.value,
                # #167: a said row and an attributed content row do not decay; a nested belief
                # decays as a first-order one does.
                b.kind.in_((BeliefKind.FIRST_ORDER.value, BeliefKind.NESTED.value)),
                b.source_event_ids.overlap(literal(in_window, type_=ARRAY(ulid_type()))),
            )
            .order_by(b.id)
        )
    ]
    position = Position(to_turn, op.turn_variant or 0, 0, 32_767, 32_767)
    written = 0
    for belief_id in covered:
        belief = await partitions.read_belief(conn, story_id, belief_id)
        if belief is None or not belief.held_from.before(position):
            continue
        await transition(
            conn,
            op,
            holder=belief.holder_character_id,
            kind=Kind.FORGETTING,
            proposition=belief.proposition,
            antecedent=Antecedent.record("beliefs", belief.id),
            position=position,
            settings=effective,
            belief_kind=belief.kind,
            shape=None
            if belief.kind is not BeliefKind.NESTED
            else {
                "attributed_holder_id": belief.attributed_holder_id,
                "inner_belief_id": belief.inner_belief_id,
            },
        )
        written += 1
    return written


# -- membership --------------------------------------------------------------------------------


async def settle(
    conn: AsyncConnection,
    story_id: str,
    belief_ids: Iterable[str] | None,
    *,
    project: bool = True,
) -> int:
    """Re-derive the in-place state and the correction closure of ``belief_ids`` (``None``:
    every belief of the story) from their active transitions (after a membership flip, and a
    fork's copy; see the module docstring), then their ``belief_state`` rows unless
    ``project`` is false (a fork folds them itself). Answers the beliefs changed."""
    if belief_ids is None:
        belief_ids = [
            str(i)
            for i in await conn.scalars(select(beliefs.c.id).where(beliefs.c.story_id == story_id))
        ]
    ids = sorted(set(belief_ids))
    if not ids:
        return 0
    b = beliefs.c
    t = belief_transitions.c
    rows = [
        partitions._belief_of(r)
        for r in await conn.execute(
            partitions._select().where(b.story_id == story_id, b.id.in_(ids))
        )
    ]
    on = await transitions_of(conn, story_id, belief_ids=ids)
    closers = [
        _transition_of(r)
        for r in await conn.execute(
            select(belief_transitions)
            .where(
                t.story_id == story_id,
                active_only(belief_transitions),
                t.prior_belief_ids.overlap(literal(ids, type_=ARRAY(ulid_type()))),
            )
            .order_by(*position_key(belief_transitions, "position").clauses, t.tx_from, t.id)
        )
    ]
    changed = 0
    keys: set[tuple[str, str]] = set()
    for belief in rows:
        if belief.membership is not Membership.ACTIVE:
            continue
        mine = [x for x in on if x.belief_id == belief.id]
        values: dict[str, Any] = {}
        if mine:
            last = mine[-1]
            values.update(
                confidence_level=last.confidence_level.value,
                confidence_scalar=last.confidence_scalar,
                accessibility=last.accessibility,
                state=last.state.value,
            )
        closer = next((c for c in closers if belief.id in c.prior_belief_ids), None)
        if closer is not None:
            by = await partitions.read_belief(conn, story_id, closer.belief_id)
            at = by.held_from if by is not None else closer.position
            values.update(
                state=BeliefHoldState.CORRECTED.value,
                corrected_by=closer.belief_id,
                **at.columns("held_to"),
            )
            if belief.tx_to is None:
                values["tx_to"] = func.clock_timestamp()
        elif belief.corrected_by is not None:
            values.update(
                corrected_by=None,
                tx_to=None,
                **{f"held_to_{n}": None for n in ("turn", "variant", "slot_k", "beat", "step")},
            )
            if values.get("state") == BeliefHoldState.CORRECTED.value or "state" not in values:
                values["state"] = BeliefHoldState.HELD.value
        current = {
            "confidence_level": belief.confidence_level.value,
            "confidence_scalar": belief.confidence_scalar,
            "accessibility": belief.accessibility,
            "state": belief.state.value,
            "corrected_by": belief.corrected_by,
            "tx_to": belief.tx_to,
            **(belief.held_to.columns("held_to") if belief.held_to is not None else {}),
        }
        differs = {
            k: v
            for k, v in values.items()
            if ((v is None) != (current.get(k) is None) if k == "tx_to" else current.get(k) != v)
        }
        if not differs:
            continue
        reopening = belief.tx_to is not None and "tx_to" in differs
        reopening = reopening or (belief.held_to is not None and "held_to_turn" in differs)
        if reopening:
            await conn.execute(text("SELECT set_config('epistrel.belief_reopen', 'on', true)"))
        await conn.execute(update(beliefs).where(b.id == belief.id).values(**differs))
        if reopening:
            await conn.execute(text("SELECT set_config('epistrel.belief_reopen', 'off', true)"))
        changed += 1
        keys.add((belief.holder_character_id, belief.proposition_id))
    if keys and project:
        await partitions.refresh(conn, story_id, keys)
    return changed


async def follow_operations(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str], to: Membership
) -> None:
    """A membership flip of ``operation_ids`` (``core.membership``'s follower): the beliefs
    their transitions changed or closed are re-settled in the flip's transaction."""
    del to
    ids = sorted(set(operation_ids))
    if not ids:
        return
    t = belief_transitions.c
    where: list[Any] = [t.operation_id.in_(ids)]
    if story_id is not None:
        where.append(t.story_id == story_id)
    touched: dict[str, set[str]] = {}
    for r in await conn.execute(select(t.story_id, t.belief_id, t.prior_belief_ids).where(*where)):
        found = touched.setdefault(str(r.story_id), set())
        found.add(str(r.belief_id))
        found.update(str(x) for x in r.prior_belief_ids or ())
    for story, beliefs_ in sorted(touched.items()):
        await settle(conn, story, beliefs_)


register_follower(follow_operations)


__all__ = [
    "BACKDATING_KINDS",
    "BANDS",
    "BELIEF_TRANSITION",
    "DEFAULT_LEVELS",
    "DEFAULT_ORACLE",
    "FLOORS",
    "FORGETTING",
    "KIND_ANTECEDENTS",
    "NESTED_ANTECEDENTS",
    "Antecedent",
    "AntecedentOracle",
    "Confidence",
    "Forgetting",
    "HeldState",
    "HistoryEntry",
    "RefusingOracle",
    "Signature",
    "StagedRecord",
    "Tally",
    "Transition",
    "TransitionResult",
    "check_order",
    "conflicts",
    "decayed",
    "decayed_beliefs",
    "follow_operations",
    "forget_covered",
    "history",
    "project",
    "refused",
    "resolve_confidence",
    "settle",
    "signature",
    "tallying",
    "transition",
    "transitions_of",
]
