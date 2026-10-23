"""Nested beliefs and beliefs about speech acts (FR-BELIEF-15, FR-EPI-2, NFR-PRIV-1; docs/02
§4.4; #167).

**What a nested belief is.** ``believes(H, believes(B, P))``: holder H attributes to B a belief in
P. It lives in **H's** partition and is H's record, not B's: it is independent of what B actually
believes and of what is true (all three may differ). Two rows carry it (``core.tables.beliefs``):

- an immutable **inner** ``attributed`` row (holder H, attributed holder B, proposition P): the
  content ``believes(B, P)``, never read as a first-order belief (``partitions.partition`` and
  every ``check(holder = belief(H))`` record set see first-order rows only), never matched,
  closed or projected; a deeper nesting chains further ``attributed`` rows (``believes(B,
  believes(C, P))``), and the innermost may be a ``speech_act`` row (``believes(B, said(S, P))``);
- the **outer** ``nested`` row: H's own belief in that content, with ``inner_belief_id``, the
  innermost ``proposition_id`` P (duplicated, so reads by subject and predicate stay keyed),
  H's confidence (``confidence_level``/``_scalar``, FR-BELIEF-4), how sure H thinks B is
  (``attributed_confidence_*``, optional), and three times: when H holds it (``held_from``/
  ``held_to``, the position of its transition by default), when H thinks B holds P
  (``attributed_held_from``/``_to``, story-minutes; by default from the antecedent's
  story-time, open) and P's valid-time (``valid_from``/``valid_to``, from P's bounds).

Its provenance is a #160 transition with an antecedent like any belief's (FR-EPI-13): H observed
B witness something (``observation``), H inferred it from B's behaviour or a speech act H
perceived (``inference``, citing the overheard beat: ``transitions.NESTED_ANTECEDENTS``), H was
told (``testimony``, the informant its source), a ``correction``, a ``revision``, a
``forgetting``. A new attribution of the same content (same attributed holder, chain and P)
changes the outer row in place; a contradicting one (another value of a functional predicate,
the other polarity) closes it whole and writes a fresh outer and inner (inner rows are immutable):
the history is kept (FR-BELIEF-10).

**Depth** (``character_knowledge.nested_beliefs.depth``: 2 to 5, default 2; #106). First-order
counts 1; ``believes(H, believes(B, P))`` and ``believes(H, said(S, P))`` 2; each further
attribution or ``said`` wrapper one more. A write deeper than the dial is 422
``nesting_too_deep`` for an API writer (``api=True``) and is **truncated** for an internal one:
the innermost layers are dropped until it fits (a ``said`` wrapper first, then the deepest
attributions) and ``NestedWrite.truncated`` reports it (``nesting_truncated``). Lowering the
dial keeps every row readable. The ``nested_beliefs`` module gates the feature: with it off no
nested row is written (``ModuleOff``) and no claim gets a nested-belief check.

**Who.** The holder is a modeled character with a partition (never the player's,
FR-EPI-12); the attributed holder is a character of the story (the player's included, whose
objective relation is ``unresolved``: the player has no partition) and never the holder itself
(422 ``validation_error``, ``attributed_self``). An unmodeled person is attributable once #163
models it.

**A belief about a speech act** (``write_speech_act``): ``believes(H, said(S, X))``, a
``speech_act`` row of its own kind with the speaker, the utterance heard (``NULL`` for one
learned by testimony), a staged beat's reference and the perceived intent. It is not a belief
about S's beliefs: ``said`` is stored distinctly from ``believes(believes)``.

**Verification** (#166's verifier asks here; FR-BELIEF-15, NFR-PRIV-1):

- ``attribution_belief_relation(H, B, P, position)``: the **belief relation** of H's claim that B
  believes P, decided by #140's ``check`` over **H's** nested rows attributed to B at that
  position only (``unknown`` when there are none; H's own first-order belief in P is never
  evidence). A ``said`` claim ("Bobby *said* it was Marcus") is checked over H's ``speech_act``
  rows of speaker B. Never B's partition.
- ``attribution_objective_relation(B, P, position)``: the **objective relation** (does B actually
  believe P?): ``check(P, holder = belief(B))`` as of the position (supported → ``true``,
  contradicted → ``false``, else ``unresolved``; a ``said`` claim against the ``utterances``
  table). A label for the utterance and the divergence records only: it is never placed in H's
  context, and ``tools.gates check-epistemic-separation`` lets only this module and the verifier
  name it.

**Replay** (``could_have_attributed``): #165's ``replay`` over H's nested rows attributed to B
with their transitions, then the path "H perceived B sincerely assert P" (H's ``said`` row of
speaker B, not joking).

**Context** (#170 consumes ``describe``): a nested belief reads "you think B believes …", a
speech act "you heard B say …" (never the perceived intent). Neither is ever a fact or a memory.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import BeliefConfidenceLevel, BeliefKind
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.epistemic.errors import EpistemicError
from epistrel.core.epistemic.partitions import Belief, BeliefOperation, NestedBelief
from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.membership import Position, active_only
from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry, canonical, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables import characters
from epistrel.core.verify.rules import Holder, Record, Verdict, VerdictKind, check, check_with

log = logging.getLogger(__name__)

NESTED_MODULE = "nested_beliefs"
DEFAULT_DEPTH = 2
NESTING_TRUNCATED = "nesting_truncated"

Level = BeliefConfidenceLevel
Mode = Literal["believes", "said"]
ObjectiveRelation = Literal["true", "false", "unresolved"]
PerceivedIntent = Literal["sincere", "joking", "deceptive"]


class NestingTooDeep(EpistemicError):
    """A nested belief deeper than the story's ``character_knowledge.nested_beliefs.depth``
    (#167). ``details``: ``depth``, ``limit``. Nothing is written."""

    code = "nesting_too_deep"
    http_status = 422


# -- the dial ------------------------------------------------------------------------------------


def nested_on(settings: Any) -> bool:
    """Whether the story's configuration keeps nested beliefs (the ``nested_beliefs`` module)."""
    from epistrel.core.profiles.capabilities import module_on

    return module_on(settings, NESTED_MODULE)


def depth_limit(settings: Any) -> int:
    """The configured nesting depth (``character_knowledge.nested_beliefs.depth``)."""
    found = getattr(getattr(settings, "character_knowledge", None), "nested_beliefs", None)
    value = getattr(found, "depth", DEFAULT_DEPTH)
    return int(value) if isinstance(value, int) else DEFAULT_DEPTH


async def _settings(conn: AsyncConnection, story_id: str, settings: Any) -> Any:
    if settings is not None:
        return settings
    from epistrel.core.profiles.effective import effective_configuration

    return (await effective_configuration(conn, story_id, validate=False)).settings


# -- writing -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cause:
    """A nested write's typed cause (#160): the transition kind, its antecedent, the informant
    (``source``, a testimony's) and the oracle for staged antecedents."""

    kind: transitions.Kind
    antecedent: transitions.Antecedent
    source: str | None = None
    oracle: transitions.AntecedentOracle = transitions.DEFAULT_ORACLE


@dataclass(frozen=True, slots=True)
class Times:
    """A nested belief's times beyond P's own valid-time: when the holder holds it
    (``held_from``, a position; the transition's by default) and when the holder thinks the
    attributed holder holds it (``attributed_from``/``attributed_to``, story-minutes; the
    antecedent's story-time and open by default)."""

    held_from: Position | None = None
    attributed_from: int | None = None
    attributed_to: int | None = None


@dataclass(frozen=True, slots=True)
class NestedWrite:
    """What ``write_nested`` did: the outer transition's result, the inner rows it wrote
    (innermost last; none when the content was held already) and whether it was truncated to
    the configured depth (``nesting_truncated``)."""

    result: transitions.TransitionResult
    inner: tuple[Belief, ...] = ()
    truncated: bool = False
    depth: int = 2

    @property
    def belief(self) -> Belief:
        return self.result.belief


def depth_of(deeper: Sequence[str], said_by: str | None) -> int:
    """The depth of ``believes(H, believes(B, believes(deeper…, [said(S,)] P)))``."""
    return 2 + len(deeper) + (1 if said_by is not None else 0)


def fitted(
    deeper: Sequence[str], said_by: str | None, limit: int
) -> tuple[tuple[str, ...], str | None, bool]:
    """``deeper`` and ``said_by`` cut to ``limit`` (the innermost layers first: the ``said``
    wrapper, then the deepest attributions), and whether anything was cut."""
    layers, said = list(deeper), said_by
    cut = False
    while depth_of(layers, said) > limit:
        cut = True
        if said is not None:
            said = None
        elif layers:
            layers.pop()
        else:
            break
    return tuple(layers), said, cut


async def _attributable(conn: AsyncConnection, story_id: str, holder: str, other: str) -> None:
    if other == holder:
        raise InvalidRequest(
            "a nested belief attributes a belief to someone other than its holder",
            {"field": "attributed_holder", "reason": "attributed_self"},
        )
    found = await conn.scalar(
        select(characters.c.id).where(characters.c.story_id == story_id, characters.c.id == other)
    )
    if found is None:
        raise InvalidRequest(
            "an attributed holder is a character of the story",
            {"field": "attributed_holder", "reason": "unknown_character"},
        )


def _confidence_columns(prefix: str, confidence: transitions.Confidence | None) -> dict[str, Any]:
    if confidence is None or (confidence.level is None and confidence.scalar is None):
        return {}
    level, scalar = transitions.resolve_confidence(transitions.Kind.TESTIMONY, confidence)
    return {f"{prefix}_level": level.value, f"{prefix}_scalar": scalar}


async def write_nested(
    conn: AsyncConnection,
    op: BeliefOperation,
    *,
    holder: str,
    attributed_holder: str,
    proposition: Proposition,
    cause: Cause,
    position: Position,
    polarity: Polarity | str | None = None,
    confidence: transitions.Confidence | None = None,
    attributed_confidence: transitions.Confidence | None = None,
    times: Times | None = None,
    deeper: Sequence[str] = (),
    said_by: str | None = None,
    api: bool = False,
    settings: Any = None,
    registry: Registry | None = None,
    frame: Position | None = None,
    resolution: bool = False,
) -> NestedWrite:
    """``holder`` comes to believe that ``attributed_holder`` believes ``proposition`` (through
    ``deeper``'s further attributions, and a speech act of ``said_by`` innermost when given), in
    ``op``'s commit transaction: see the module docstring. 409 ``module_off`` unless the story
    keeps nested beliefs; 422 ``nesting_too_deep`` for an API writer past the dial. ``frame`` and
    ``resolution``: #164's lazy resolution (one exposure yields a ``said`` and a nested row)."""
    story_id = op.story_id
    if story_id is None:
        raise InvalidRequest("a belief belongs to a story", {"field": "operation"})
    current = await _settings(conn, story_id, settings)
    if not nested_on(current):
        raise ModuleOff(
            "the story's configuration keeps no nested beliefs",
            {"story_id": story_id, "module": NESTED_MODULE},
        )
    limit = depth_limit(current)
    wanted = depth_of(deeper, said_by)
    if wanted > limit and api:
        raise NestingTooDeep(
            "the nested belief is deeper than the story's configured depth (FR-BELIEF-15)",
            {"depth": wanted, "limit": limit, "field": "attributed_to"},
        )
    layers, said, truncated = fitted(deeper, said_by, limit)
    await _attributable(conn, story_id, holder, attributed_holder)
    for other in layers:
        await _attributable(conn, story_id, "", other)
    found = registry if registry is not None else await load(conn, story_id)
    if polarity is not None:
        proposition = proposition.model_copy(update={"polarity": Polarity(polarity)})
    stored = canonical(proposition, found)
    given = times or Times()
    when = given.attributed_from
    if when is None:
        when = await transitions._story_time(conn, story_id, given.held_from or position)
    common: dict[str, Any] = {
        "holder": holder,
        "kind": cause.kind,
        "antecedent": cause.antecedent,
        "position": position,
        "held_from": given.held_from,
        "source": cause.source,
        "oracle": cause.oracle,
        "settings": current,
        "registry": found,
        "frame": frame,
        "resolution": resolution,
    }
    chain_holders = (attributed_holder, *layers)
    signature: transitions.Signature = (chain_holders, said)
    held = [
        n
        for n in await partitions.nested_of(
            conn,
            story_id,
            holder,
            attributed_holder=attributed_holder,
            subjects=[stored.subject_id],
            predicate=stored.predicate_id,
        )
        if transitions.signature(n) == signature
    ]
    same = next(
        (
            n
            for n in held
            if n.belief.proposition.normalized_key() == stored.normalized_key()
            and n.belief.polarity == stored.polarity.value
        ),
        None,
    )
    inner: list[Belief] = []
    shared: dict[str, Any] = {}
    if same is not None:
        inner_id = same.belief.inner_belief_id
    else:
        inner_id = None
        # Innermost first: the said content, then each attribution outward.
        if said is not None:
            made = await transitions.transition(
                conn,
                op,
                proposition=stored,
                belief_kind=BeliefKind.SPEECH_ACT,
                confidence=confidence,
                shape={
                    "attributed_holder_id": chain_holders[-1],
                    "speaker_character_id": said,
                    "depth": 2,
                },
                **common,
            )
            inner.append(made.belief)
            inner_id = made.belief.id
            shared = {"proposition_id": made.belief.proposition_id}
        for n, attributed in enumerate(reversed(chain_holders)):
            made = await transitions.transition(
                conn,
                op,
                proposition=stored,
                belief_kind=BeliefKind.ATTRIBUTED,
                confidence=confidence,
                shape={
                    "attributed_holder_id": attributed,
                    "inner_belief_id": inner_id,
                    "depth": 2 + n + (1 if said is not None else 0),
                    **shared,
                },
                **common,
            )
            inner.append(made.belief)
            inner_id = made.belief.id
            shared = {"proposition_id": made.belief.proposition_id}
    result = await transitions.transition(
        conn,
        op,
        proposition=stored,
        belief_kind=BeliefKind.NESTED,
        confidence=confidence,
        shape={
            "attributed_holder_id": attributed_holder,
            "inner_belief_id": inner_id,
            "depth": depth_of(layers, said),
            "attributed_held_from": when,
            "attributed_held_to": given.attributed_to,
            **_confidence_columns("attributed_confidence", attributed_confidence),
            **shared,
        },
        **common,
    )
    log.debug(
        "nested belief %s: depth %d, truncated=%s",
        result.belief.id,
        depth_of(layers, said),
        truncated,
    )
    return NestedWrite(result, tuple(reversed(inner)), truncated, depth_of(layers, said))


async def write_speech_act(
    conn: AsyncConnection,
    op: BeliefOperation,
    *,
    holder: str,
    speaker: str,
    proposition: Proposition,
    antecedent: transitions.Antecedent,
    position: Position,
    outcome: str | None = None,
    utterance_id: str | None = None,
    staged_ref: Mapping[str, Any] | None = None,
    perceived_intent: PerceivedIntent | None = None,
    confidence: transitions.Confidence | None = None,
    oracle: transitions.AntecedentOracle = transitions.DEFAULT_ORACLE,
    settings: Any = None,
    registry: Registry | None = None,
) -> transitions.TransitionResult:
    """``holder`` believes ``speaker`` said ``proposition`` (``believes(H, said(S, X))``): a
    ``speech_act`` row with its full shape, by a testimony transition from the speaker (held
    ``known`` by default: the holder heard it)."""
    return await transitions.transition(
        conn,
        op,
        holder=holder,
        kind=transitions.Kind.TESTIMONY,
        proposition=proposition,
        antecedent=antecedent,
        position=position,
        confidence=confidence or transitions.Confidence(Level.KNOWN),
        source=speaker,
        outcome=outcome,
        belief_kind=BeliefKind.SPEECH_ACT,
        oracle=oracle,
        settings=settings,
        registry=registry,
        shape={
            "speaker_character_id": speaker,
            "utterance_id": utterance_id,
            "staged_ref": None if staged_ref is None else dict(staged_ref),
            "perceived_intent": perceived_intent,
            "depth": 2,
        },
    )


INTENT_LEVELS: Mapping[str, Level | None] = {
    "sincere": Level.BELIEVED,
    "joking": Level.SUSPECTED,
    "deceptive": None,
}
"""The confidence of the nested belief a perceived speech act yields, by perceived intent: a
sincere assertion evidences the speaker believes it, a joke suggests it, a detected deception
yields none (#167)."""


# -- verification --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributionRelation:
    """An attribution claim's belief relation for its speaker: the verdict over the speaker's own
    nested (or ``said``) rows and the rows it rests on."""

    verdict: VerdictKind
    belief_ids: tuple[str, ...] = ()
    contrary: tuple[str, ...] = ()


def _held(belief: Belief, at: Position | None) -> bool:
    return at is None or belief.held_at(at)


async def attribution_records(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    attributed_holder: str,
    proposition: Proposition,
    at: Position | None,
    *,
    mode: Mode = "believes",
) -> list[Belief]:
    """The holder's rows an attribution claim is checked over: its depth-2 nested beliefs that
    ``attributed_holder`` believes something about P's subject and predicate (``mode =
    believes``), or its ``said`` rows of speaker ``attributed_holder`` (``mode = said``), held at
    ``at``."""
    if mode == "said":
        return [
            a.belief
            for a in await partitions.speech_acts(conn, story_id, holder, speaker=attributed_holder)
            if a.belief.proposition.subject_id == proposition.subject_id
            and a.belief.proposition.predicate_id == proposition.predicate_id
            and _held(a.belief, at)
        ]
    return [
        n.belief
        for n in await partitions.nested_of(
            conn,
            story_id,
            holder,
            attributed_holder=attributed_holder,
            through=at,
            subjects=[proposition.subject_id],
            predicate=proposition.predicate_id,
        )
        if transitions.signature(n) == ((attributed_holder,), None)
    ]


async def attribution_belief_relation(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    attributed_holder: str,
    proposition: Proposition,
    at: Position | None,
    *,
    mode: Mode = "believes",
    registry: Registry | None = None,
) -> AttributionRelation:
    """The belief relation of ``holder``'s claim that ``attributed_holder`` believes (or said)
    ``proposition``: ``check`` over the holder's own rows only (see the module docstring);
    ``novel`` (``unknown``) when it holds none."""
    found = registry if registry is not None else await load(conn, story_id)
    target = canonical(proposition, found)
    rows = await attribution_records(
        conn, story_id, holder, attributed_holder, target, at, mode=mode
    )
    if not rows:
        return AttributionRelation(VerdictKind.NOVEL)
    verdict = check(target, [Record.of_belief(b) for b in rows], registry=found)
    cited = tuple(e.belief_id for e in verdict.evidence if e.belief_id is not None)
    contrary = cited if verdict.kind is VerdictKind.CONTRADICTED else ()
    return AttributionRelation(verdict.kind, cited, contrary)


def objective_of(verdict: VerdictKind | None) -> ObjectiveRelation:
    if verdict is VerdictKind.SUPPORTED:
        return "true"
    if verdict is VerdictKind.CONTRADICTED:
        return "false"
    return "unresolved"


async def attribution_objective_relation(
    conn: AsyncConnection,
    story_id: str,
    attributed_holder: str,
    proposition: Proposition,
    at: Position | None,
    *,
    mode: Mode = "believes",
    registry: Registry | None = None,
) -> ObjectiveRelation:
    """Whether ``attributed_holder`` actually believes ``proposition`` as of ``at``
    (``check(P, holder = belief(B))``), or, for ``mode = said``, said it by then (the
    ``utterances`` table): the objective relation of an attribution, a core-only label (see the
    module docstring). The player's character holds no partition: ``unresolved``."""
    found = registry if registry is not None else await load(conn, story_id)
    target = canonical(proposition, found)
    if mode == "said":
        return await _said_relation(conn, story_id, attributed_holder, target, at, found)
    if await partitions.is_player(conn, story_id, attributed_holder):
        return "unresolved"
    lookup = KeyedLookup(conn, StructuredReadSet())
    verdict: Verdict = await check_with(
        lookup,
        story_id,
        target,
        holder=Holder.belief(attributed_holder),
        position=at,
        through_position=at,
        registry=found,
    )
    return objective_of(verdict.kind)


async def _said_relation(
    conn: AsyncConnection,
    story_id: str,
    speaker: str,
    target: Proposition,
    at: Position | None,
    registry: Registry,
) -> ObjectiveRelation:
    from epistrel.core.propositions.codec import proposition_from_row
    from epistrel.core.tables import propositions, utterances

    u = utterances.c
    p = propositions.c
    where = [
        u.story_id == story_id,
        u.speaker_character_id == speaker,
        active_only(utterances),
        p.subject_id == target.subject_id,
        p.predicate_id == target.predicate_id,
    ]
    if at is not None:
        where.append(u.narrative_turn <= at.turn)
    found = await conn.execute(
        select(propositions)
        .select_from(utterances.join(propositions, p.id == func.any(u.proposition_ids)))
        .where(*where)
    )
    said = [canonical(proposition_from_row(r), registry) for r in found]
    if not said:
        return "unresolved"
    verdict = check(
        target,
        [Record(s, f"said:{n}") for n, s in enumerate(said)],
        registry=registry,
    )
    return objective_of(verdict.kind)


def aggregate_objective(relations: Sequence[ObjectiveRelation]) -> ObjectiveRelation | None:
    """An utterance's attribution objective relation over its attribution claims: any ``false``
    → ``false``, else any ``unresolved`` → ``unresolved``, else ``true`` (``None``: none)."""
    if not relations:
        return None
    if "false" in relations:
        return "false"
    if "unresolved" in relations:
        return "unresolved"
    return "true"


# -- replay ---------------------------------------------------------------------------------------


async def could_have_attributed(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    attributed_holder: str,
    proposition: Proposition,
    at: Position,
    *,
    min_level: Level = Level.SUSPECTED,
    registry: Registry | None = None,
) -> Any:
    """#165's replay for a nested proposition: whether ``holder`` could, at ``at``, hold that
    ``attributed_holder`` believes ``proposition``: its nested rows attributed to B replayed with
    their transitions, then the path "H perceived B sincerely assert P" (an earlier ``said`` row
    of speaker B, not perceived as a joke). Answers ``replay.Known`` or ``replay.NoPath``."""
    from epistrel.core.epistemic import replay

    found = registry if registry is not None else await load(conn, story_id)
    target = canonical(proposition, found)
    key = target.normalized_key()
    rows = [
        b
        for b in await attribution_records(
            conn, story_id, holder, attributed_holder, target, None, mode="believes"
        )
        if canonical(b.proposition, found).normalized_key() == key
    ]
    records: list[replay.Acquisition] = []
    if rows:
        for t in await transitions.transitions_of(
            conn, story_id, holder=holder, belief_ids=[b.id for b in rows]
        ):
            belief = next(b for b in rows if b.id == t.belief_id)
            records.append(
                replay.Acquisition(
                    key=key,
                    polarity=Polarity(belief.polarity),
                    level=t.confidence_level,
                    since=t.position,
                    until=belief.held_to,
                    lineage=belief.id,
                    ref=replay.PathRef("transition", t.id, t.position, t.kind.value),
                )
            )
    answer = replay.replay(records, key, target.polarity, at, min_level=min_level)
    if isinstance(answer, replay.Known):
        return answer
    for said in await partitions.speech_acts(conn, story_id, holder, speaker=attributed_holder):
        b = said.belief
        if b.perceived_intent in ("joking", "deceptive"):
            continue
        stored = canonical(b.proposition, found)
        if stored.normalized_key() != key or stored.polarity is not target.polarity:
            continue
        if b.held_from.before(at):
            return replay.Known(
                replay.PathRef("transition", b.provenance, b.held_from, "testimony")
            )
    return answer


# -- context --------------------------------------------------------------------------------------


def describe(
    found: NestedBelief | Belief,
    names: Mapping[str, str],
    statement: str,
) -> str:
    """A nested belief or a speech act as #170's context reads it: "you think Bobby believes
    <statement>", "you heard Bobby say <statement>" (``statement``: the rendered inner
    proposition; ``names``: display names by character id). The perceived intent is never
    named."""
    if isinstance(found, Belief):
        speaker = found.speaker_character_id or ""
        return f"you heard {names.get(speaker, 'someone')} say {statement}"
    phrase = statement
    if found.said_by is not None:
        phrase = f"{names.get(found.said_by, 'someone')} said {phrase}"
    for other in reversed(found.holders[1:]):
        phrase = f"{names.get(other, 'someone')} believes {phrase}"
    return f"you think {names.get(found.attributed_holder_id, 'someone')} believes {phrase}"


async def held_nested(
    conn: AsyncConnection, story_id: str, holder: str, *, through: Position | None = None
) -> list[NestedBelief]:
    """``holder``'s nested beliefs held at ``through`` (now when ``None``): what #170's context
    lists as "you think …" lines, H-private (never ``memories`` rows)."""
    return await partitions.nested_of(conn, story_id, holder, through=through)


__all__ = [
    "DEFAULT_DEPTH",
    "INTENT_LEVELS",
    "NESTED_MODULE",
    "NESTING_TRUNCATED",
    "AttributionRelation",
    "Cause",
    "Mode",
    "NestedWrite",
    "NestingTooDeep",
    "ObjectiveRelation",
    "Times",
    "aggregate_objective",
    "attribution_belief_relation",
    "attribution_objective_relation",
    "attribution_records",
    "could_have_attributed",
    "depth_limit",
    "depth_of",
    "describe",
    "fitted",
    "held_nested",
    "nested_on",
    "objective_of",
    "write_nested",
    "write_speech_act",
]
