"""Action preconditions, verified as propositions before a transfer commits (FR-STATE-11;
docs/02 §7.1's validation order; #189). A State's bounds say what values are possible; an
action's preconditions say whether the action happened. Having the gold does not buy the sword:
the merchant has to agree and the sword has to be there.

**What is checked** (``preconditions_for``), for a ``transfer`` effect and for a ``change`` that
changes possession (one with an item):

- **The parties.** The paying side is the one whose delta is negative (a transfer's own side,
  else its counterparty); the other side is paid. The *counterparty* is a transfer's other side
  when it is a character (a character-scoped State's scope key). The *seller* is the paid side
  of a transfer, and for a ``change`` the item's current holder in canon.
- **The item** the payment is for: the effect's ``consideration`` (an entity reference, resolved
  through #139's ``EntityIndex`` to an entity of the story); else the object of the same beat's
  ``holds``/``held_by``/``owns`` proposition whose new holder is the paying side. A ``sale``
  (#188's justification label) to or from a counterparty character with neither is
  ``effect_unresolved`` (``UNRESOLVED``): #187's rung 1 asks the render to name the item. Any
  other transfer with no item (a tip, a gift) skips the item preconditions. A ``consideration``
  that resolves to no entity is ``uncertain(unresolved_entity)``, which never escalates
  (docs/02 §4.9): it takes the safe disposition at once.
- **The propositions**, in this order (the first unmet one is the reason):
  1. ``consent``: ``agreed_to(counterparty, item | payer)``. Consent is only an ``agreed_to`` a
     kept ``say``/``do`` beat of the counterparty declares, or a narrator (director/world) beat
     declares (never prose: a detected claim does not count), or the counterparty's own render
     declaring the effect (its own act). A ``theft`` needs none.
  2. ``availability``: ``holds(seller, item)``, and no reservation (#216) on the item for anyone
     but the payer. Inert until #216: there are no reservations yet.
  3. ``co_location``: ``present_at(payer, place) ∧ present_at(seller, place)``, ``place`` the
     payer's location, read through ``core.epistemic.perception.co_located_set`` over
     ``core.epistemic.scene``'s state (ADR-169: never ``placement`` directly), with the turn's
     earlier kept ``located_at`` moves laid over it.
  4. ``ownership``: ``owns(seller, item)`` (#133's canon); another party's ``owns`` with none of
     the seller's is unmet too (``owns`` is ``multi``, so ``check`` alone never contradicts it).

**How each is checked** (``evaluate``): with #140's pure ``check`` against canon (``Canon``: the
attempt's keyed reads, ``read``; each a ``KeyedLookup`` query key of the turn's read set, #136)
and then, as the pipeline's ``StagedSource`` does, against the same-turn overlay (the turn's kept
claims, and in a sequential turn #174's ``TurnStage`` beats' verified propositions) when canon
leaves it ``novel`` or ``uncertain``; a conclusive overlay verdict wins. The overlay leaves out
the effect's own beat for the item checks (its own possession change is the action's result,
not its precondition). ``consent`` must be ``supported``; ``availability``, ``co_location`` and
``ownership`` are unmet when ``contradicted`` and met when ``supported`` or ``novel`` (a world
fact the story never fixed is permitted novelty). ``uncertain`` takes the safe disposition: the
effect is refused like an unmet one, narrated as an attempt, never a silent commit.

**The verdict** (the ``PRECONDITIONS`` hook of ``core.states.authorize``, after the
justification and before the transfer balance and the caps): ``Unpermitted("precondition:
<name>")`` for an unmet or uncertain one (``state_unpermitted``, repaired in character: the
merchant refuses, the item is gone; nothing changes), ``Unpermitted("effect_unresolved")`` for a
sale with no item (``core.states.effects`` makes it the ``effect_unresolved`` finding), else
``None``. With no ``Context`` (a consumer's write, a re-check at commit) nothing is checked: the
rebase re-check sees canon changes through the read set's keys.

Pure except ``read``. Codes only; this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from epistrel.core.epistemic.perception import SceneState, co_located_set
from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    Polarity,
    Proposition,
)
from epistrel.core.propositions.registry import Registry, canonical
from epistrel.core.propositions.vocabulary import (
    AGREED_TO,
    HELD_BY,
    HOLDS,
    LOCATED_AT,
    OWNS,
    PRESENT_AT,
)
from epistrel.core.resolution.entities import EntityIndex, words_of
from epistrel.core.states import authorize as permission
from epistrel.core.states.authorize import Source, StateEffect, Unpermitted
from epistrel.core.verify.rules import Record, Verdict, VerdictKind, VerdictReason, check, uncertain

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from epistrel.core.readset import StructuredReadSet
    from epistrel.core.states.model import StateDefinition

CONSENT: Final = "consent"
AVAILABILITY: Final = "availability"
CO_LOCATION: Final = "co_location"
OWNERSHIP: Final = "ownership"
NAMES: tuple[str, ...] = (CONSENT, AVAILABILITY, CO_LOCATION, OWNERSHIP)
"""The preconditions, in the order they are checked."""
PREFIX: Final = "precondition:"
UNRESOLVED: Final = "effect_unresolved"
"""The reason of a sale whose item is named nowhere (``core.states.effects``'
``effect_unresolved``)."""
CONSIDERATION: Final = "consideration"
"""The ``effect_unresolved`` finding's constraint for a sale with no item."""
POSSESSION: frozenset[str] = frozenset({HOLDS, HELD_BY, OWNS})
"""The predicates whose same-beat change names the item a payment is for."""
SAY_DO: frozenset[str] = frozenset({"say", "do"})
KEPT_VERDICTS: frozenset[str | None] = frozenset({None, "supported", "novel"})
"""The verdicts of a claim verification kept (neither contradicted nor left uncertain)."""


def reason_of(name: str) -> str:
    """``Unpermitted.reason`` of the precondition ``name``: ``precondition:<name>``."""
    return f"{PREFIX}{name}"


REASONS: frozenset[str] = frozenset(reason_of(n) for n in NAMES)


# -- the turn as the checks read it --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """One proposition a beat asserts: whether it was declared (not detected in prose) and
    whether verification kept it."""

    proposition: Proposition
    declared: bool = True
    kept: bool = True


@dataclass(frozen=True, slots=True)
class BeatView:
    """A beat of the turn as the checks read it (a ``core.turns.beats.TurnBeat`` or a staged
    beat): its number, kind, speaker (``None``: the narrator) and its claims."""

    beat: int
    kind: str
    character_id: str | None = None
    narrator: bool = False
    attempt: bool = False
    claims: tuple[Claim, ...] = ()

    @classmethod
    def of_turn(cls, beat: Any) -> BeatView:
        """A ``core.turns.beats.TurnBeat``: its claims with their verdicts."""
        narrator = bool(beat.narrator) or beat.character_id is None
        return cls(
            beat.beat,
            beat.kind,
            None if narrator else beat.character_id,
            narrator,
            bool(beat.attempt),
            tuple(
                Claim(c.proposition, bool(c.declared), c.verdict in KEPT_VERDICTS)
                for c in beat.claims
                if c.proposition is not None
            ),
        )

    @classmethod
    def of_staged(cls, beat: Any) -> BeatView:
        """A #174 ``StagedBeat``: the propositions its claims verified (all kept; whether one
        was declared is not staged, so a staged beat's consent is its declared beat's own)."""
        return cls(
            beat.beat,
            beat.kind,
            None if beat.narrator else beat.character_id,
            bool(beat.narrator),
            False,
            tuple(Claim(p) for p in beat.verified),
        )


@dataclass(slots=True)
class Canon:
    """What the attempt read for its preconditions (``read``), each read keyed in the read set:
    the registry, the story-time of the turn, the entity index (a ``consideration``'s
    resolution), the scene at the turn's start, and per item its ``holds`` and ``owns`` facts
    and per party its ``agreed_to`` facts. A key absent here was not read: its check is
    ``uncertain`` (the safe disposition)."""

    registry: Registry
    now: int
    index: EntityIndex | None = None
    scene: SceneState | None = None
    holds: dict[str, list[Record]] = field(default_factory=dict)
    owns: dict[str, list[Record]] = field(default_factory=dict)
    agreed: dict[str, list[Record]] = field(default_factory=dict)
    reserved: dict[str, str] = field(default_factory=dict)
    """#216: an item's reservation and its beneficiary (none until reservations exist)."""


@dataclass(frozen=True, slots=True)
class Context:
    """The checks' inputs for one dry-run: the attempt's ``Canon`` and the turn's beats as they
    stand (repairs included), by number."""

    canon: Canon
    beats: Mapping[int, BeatView] = field(default_factory=dict)

    def at(self, beats: Iterable[BeatView]) -> Context:
        return replace(self, beats={b.beat: b for b in beats})


def context_of(canon: Canon, beats: Sequence[Any], stage: Any = None) -> Context:
    """The ``Context`` of ``beats`` (``TurnBeat``s), ``stage``'s beats (#174) where ``beats``
    has no beat of that number."""
    found: dict[int, BeatView] = {}
    if stage is not None:
        for s in stage.beats:
            found[s.beat] = BeatView.of_staged(s)
    for b in beats:
        found[b.beat] = BeatView.of_turn(b)
    return Context(canon, found)


# -- the parties and the item --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Parties:
    """Who pays, who is paid (the seller), the counterparty whose consent is needed, and the
    item (``unresolved``: a ``consideration`` that names no entity)."""

    payer: str | None
    seller: str | None
    counterparty: str | None
    item: str | None = None
    unresolved: bool = False


def resolve(name: str, index: EntityIndex | None) -> str | None:
    """The entity of the story ``name`` (an id, or a name or alias that names one entity)
    resolves to through #139's index (``None``: none, or more than one)."""
    if index is None:
        return None
    if index.get(name) is not None:
        return name
    found = index.lookup(tuple(words_of(name)))
    if found is None:
        return None
    entities, _ = found
    return next(iter(entities)) if len(entities) == 1 else None


def _holder_and_item(p: Proposition) -> tuple[str, str] | None:
    """A possession proposition's new holder and its item."""
    if p.polarity is not Polarity.AFFIRMED or p.object_id is None:
        return None
    if p.predicate_id == HELD_BY:
        return p.object_id, p.subject_id
    if p.predicate_id in (HOLDS, OWNS):
        return p.subject_id, p.object_id
    return None


def possession_item(beat: BeatView | None, payer: str | None) -> str | None:
    """The object of ``beat``'s kept ``holds``/``held_by``/``owns`` change whose new holder is
    ``payer``."""
    if beat is None or payer is None:
        return None
    for c in beat.claims:
        found = _holder_and_item(c.proposition)
        if c.kept and found is not None and found[0] == payer:
            return found[1]
    return None


def _sides(effect: StateEffect) -> tuple[str | None, str | None]:
    """The paying and the paid side's characters (``None``: the story, or untracked)."""
    other = effect.counterparty
    own = effect.scope_key
    theirs = None if other is None else other.scope_key
    delta = effect.delta
    if delta is None or delta == 0:
        return None, None
    if effect.kind == permission.TRANSFER:
        return (own, theirs) if delta < 0 else (theirs, own)
    return (own, None) if delta < 0 else (None, own)


def _holder(canon: Canon, item: str, *, besides: str | None) -> str | None:
    """The item's current holder in canon (a ``change``'s seller), other than ``besides``."""
    for r in canon.holds.get(item, ()):
        p = r.proposition
        if p.polarity is Polarity.AFFIRMED and p.subject_id != besides:
            return p.subject_id
    return None


def parties_of(effect: StateEffect, context: Context) -> Parties | None:
    """``effect``'s parties and item (see the module docstring), ``None`` for an effect with no
    precondition: a ``change`` that changes no possession, or no amount."""
    payer, paid = _sides(effect)
    if payer is None and paid is None:
        return None
    beat = None if effect.beat is None else context.beats.get(effect.beat)
    item: str | None = None
    unresolved = False
    if effect.consideration is not None:
        item = resolve(effect.consideration, context.canon.index)
        unresolved = item is None
    else:
        item = possession_item(beat, payer)
    transfer = effect.kind == permission.TRANSFER
    if not transfer and item is None and not unresolved:
        return None
    counterparty = None
    if transfer and effect.counterparty is not None:
        counterparty = effect.counterparty.scope_key
    seller = paid
    if not transfer and payer is not None and item is not None:
        seller = _holder(context.canon, item, besides=payer)
        counterparty = seller
    return Parties(payer, seller, counterparty, item, unresolved)


# -- the propositions ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Required:
    """One precondition: its name and the proposition that must hold."""

    name: str
    proposition: Proposition


def _now(context: Context) -> Instant:
    return Instant.of(context.canon.now, Granularity.MINUTE)


def _consent_records(context: Context, party: str) -> list[Record]:
    """The turn's declared ``agreed_to`` of ``party``: on its own kept ``say``/``do`` beat, or a
    narrator beat (FR-STATE-11: a beat or decision by the counterparty or the world)."""
    found = []
    for b in context.beats.values():
        if b.attempt:
            continue
        own = b.kind in SAY_DO and b.character_id == party
        if not (own or b.narrator):
            continue
        for c in b.claims:
            p = c.proposition
            if c.declared and c.kept and p.predicate_id == AGREED_TO and p.subject_id == party:
                found.append(Record(p, f"beat:{b.beat}"))
    return found


def _consent(context: Context, parties: Parties) -> Proposition:
    party = parties.counterparty
    assert party is not None  # noqa: S101 - consent is asked of a counterparty only
    wanted = {o for o in (parties.item, parties.payer) if o is not None}
    for r in _consent_records(context, party):
        if r.proposition.object_id in wanted:
            return r.proposition.model_copy(update={"polarity": Polarity.AFFIRMED})
    return Proposition(
        subject_id=party,
        predicate_id=AGREED_TO,
        object_id=parties.item or parties.payer,
        valid_time=_now(context),
    )


def preconditions_for(
    effect: StateEffect, beat: BeatView | None, context: Context, *, asserter: Source | None = None
) -> list[Required]:
    """The propositions ``effect`` (declared on ``beat``) depends on (see the module docstring),
    in checking order. An unresolved ``consideration`` names no item: its item checks are
    ``uncertain`` (``evaluate``)."""
    within = context if beat is None else replace(context, beats={**context.beats, beat.beat: beat})
    parties = parties_of(effect, within)
    if parties is None:
        return []
    now = _now(within)
    found: list[Required] = []
    own = asserter is not None and asserter.kind == "character"
    by_self = own and asserter is not None and asserter.character_id == parties.counterparty
    if (
        parties.counterparty is not None
        and effect.justification != permission.THEFT
        and not by_self
    ):
        found.append(Required(CONSENT, _consent(within, parties)))
    seller, item = parties.seller, parties.item
    if seller is not None and item is not None:
        found.append(
            Required(
                AVAILABILITY,
                Proposition(subject_id=seller, predicate_id=HOLDS, object_id=item, valid_time=now),
            )
        )
    place = _place(within, effect.beat, parties.payer)
    if place is not None and seller is not None and parties.payer is not None:
        for who in (parties.payer, seller):
            found.append(
                Required(
                    CO_LOCATION,
                    Proposition(
                        subject_id=who, predicate_id=PRESENT_AT, object_id=place, valid_time=now
                    ),
                )
            )
    if seller is not None and item is not None:
        found.append(
            Required(
                OWNERSHIP,
                Proposition(subject_id=seller, predicate_id=OWNS, object_id=item, valid_time=now),
            )
        )
    return found


# -- the checks ----------------------------------------------------------------------------------


def _scene(context: Context, before: int | None) -> SceneState | None:
    """The scene at the turn's start with the turn's kept ``located_at`` moves of the beats
    before ``before`` laid over it."""
    scene = context.canon.scene
    if scene is None:
        return None
    moves = [
        (c.proposition.subject_id, c.proposition.object_id)
        for b in sorted(context.beats.values(), key=lambda b: b.beat)
        if before is None or b.beat < before
        for c in b.claims
        if c.kept
        and not b.attempt
        and c.proposition.predicate_id == LOCATED_AT
        and c.proposition.polarity is Polarity.AFFIRMED
        and c.proposition.subject_id in scene.characters
    ]
    return scene.moved(moves) if moves else scene


def _place(context: Context, before: int | None, payer: str | None) -> str | None:
    scene = _scene(context, before)
    return None if scene is None or payer is None else scene.at(payer)


def _co_location_records(context: Context, before: int | None, payer: str) -> list[Record]:
    """``present_at(c, place)`` of every placed character, affirmed when it is co-located with
    the payer (``co_located_set``) and negated when it is placed elsewhere."""
    scene = _scene(context, before)
    if scene is None:
        return []
    place = scene.at(payer)
    if place is None:
        return []
    together = co_located_set(scene, payer)
    found = []
    for c in sorted(scene.characters):
        if scene.at(c) is None:
            continue
        p = Proposition(
            subject_id=c,
            predicate_id=PRESENT_AT,
            object_id=place,
            polarity=Polarity.AFFIRMED if c in together else Polarity.NEGATED,
            valid_time=Interval.of(None, None),
        )
        found.append(Record(p, f"scene:{c}"))
    return found


def _overlay(
    context: Context, predicates: frozenset[str], item: str, *, skip: int | None, buyer: str | None
) -> list[Record]:
    """The turn's kept claims on ``item`` of ``predicates``, canonical, but the effect's own
    beat's and the buyer's own possession (the action's result, not its precondition)."""
    registry = context.canon.registry
    found = []
    for b in context.beats.values():
        if b.beat == skip or b.attempt:
            continue
        for c in b.claims:
            p = c.proposition
            if not c.kept or p.predicate_id not in predicates:
                continue
            held = _holder_and_item(p) if p.polarity is Polarity.AFFIRMED else None
            names = held[1] if held is not None else (p.object_id or p.subject_id)
            if names != item or (held is not None and held[0] == buyer):
                continue
            found.append(Record(canonical(p, registry), f"beat:{b.beat}"))
    return found


def _staged_check(
    claim: Proposition, canon: Sequence[Record], overlay: Sequence[Record], *, registry: Registry
) -> Verdict:
    """``check`` over canon, then over the overlay when canon leaves it open (the pipeline's
    ``StagedSource``)."""
    found = check(claim, canon, registry=registry)
    if found.kind not in (VerdictKind.NOVEL, VerdictKind.UNCERTAIN) or not overlay:
        return found
    staged = check(claim, overlay, registry=registry)
    return staged if staged.kind in (VerdictKind.SUPPORTED, VerdictKind.CONTRADICTED) else found


def verdict_of(
    required: Required, effect: StateEffect, parties: Parties, context: Context
) -> Verdict:
    """``required``'s verdict over canon and the same-turn overlay (see the module docstring)."""
    canon, registry = context.canon, context.canon.registry
    p = required.proposition
    if required.name == CONSENT:
        if p.subject_id not in canon.agreed:
            return uncertain(VerdictReason.UNRESOLVED_ENTITY)
        return _staged_check(
            p,
            canon.agreed[p.subject_id],
            _consent_records(context, p.subject_id),
            registry=registry,
        )
    if required.name == CO_LOCATION:
        assert parties.payer is not None  # noqa: S101 - co-location has a payer
        return check(
            p, _co_location_records(context, effect.beat, parties.payer), registry=registry
        )
    item = p.object_id
    assert item is not None  # noqa: S101 - the item checks name an item
    facts = canon.holds if required.name == AVAILABILITY else canon.owns
    if item not in facts:
        return uncertain(VerdictReason.UNRESOLVED_ENTITY)
    predicates = frozenset({HOLDS, HELD_BY}) if required.name == AVAILABILITY else frozenset({OWNS})
    overlay = _overlay(context, predicates, item, skip=effect.beat, buyer=parties.payer)
    found = _staged_check(p, facts[item], overlay, registry=registry)
    if required.name == OWNERSHIP and found.kind is VerdictKind.NOVEL:
        others = [
            r
            for r in [*facts[item], *overlay]
            if r.proposition.polarity is Polarity.AFFIRMED
            and r.proposition.subject_id not in (p.subject_id, parties.payer)
        ]
        if others:
            return Verdict(VerdictKind.CONTRADICTED)
    return found


def _met(name: str, verdict: Verdict) -> bool | None:
    """Whether a precondition holds (``None``: uncertain, the safe disposition)."""
    if verdict.kind is VerdictKind.UNCERTAIN:
        return None
    if name == CONSENT:
        return verdict.kind is VerdictKind.SUPPORTED
    return verdict.kind is not VerdictKind.CONTRADICTED


def evaluate(
    effect: StateEffect,
    asserter: Source,
    definition: StateDefinition,
    context: Context | None,
) -> Unpermitted | None:
    """The ``PRECONDITIONS`` hook (see the module docstring): the first unmet or uncertain
    precondition's ``Unpermitted``, ``effect_unresolved`` for a sale with no item, else
    ``None``."""
    del definition
    if not isinstance(context, Context):
        return None
    beat = None if effect.beat is None else context.beats.get(effect.beat)
    parties = parties_of(effect, context)
    if parties is None:
        return None
    if (
        parties.item is None
        and not parties.unresolved
        and effect.kind == permission.TRANSFER
        and effect.justification == permission.SALE
        and parties.counterparty is not None
    ):
        return Unpermitted(UNRESOLVED)
    for r in preconditions_for(effect, beat, context, asserter=asserter):
        if r.name != CONSENT and parties.unresolved:
            break
        if not _met(r.name, verdict_of(r, effect, parties, context)):
            return Unpermitted(reason_of(r.name))
    if parties.unresolved:
        # An item named nowhere in the story: uncertain(unresolved_entity), which never
        # escalates (docs/02 §4.9), so the safe disposition at once.
        return Unpermitted(reason_of(AVAILABILITY))
    return None


# -- the reads -----------------------------------------------------------------------------------


def wanted(effects: Iterable[StateEffect], context: Context) -> tuple[set[str], set[str]]:
    """The items and the parties whose canon ``effects``' preconditions read."""
    items: set[str] = set()
    parties: set[str] = set()
    for effect in effects:
        found = parties_of(effect, context)
        if found is None:
            continue
        if found.item is not None:
            items.add(found.item)
        for who in (found.counterparty, found.seller):
            if who is not None:
                parties.add(who)
    return items, parties


async def read(
    conn: AsyncConnection,
    read_set: StructuredReadSet,
    story_id: str,
    turn: int,
    effects: Sequence[StateEffect],
    beats: Sequence[Any],
    stage: Any = None,
) -> Canon:
    """The attempt's ``Canon`` for the preconditions of ``effects`` (each with its beat),
    declared on ``beats`` (``TurnBeat``s) and ``stage``'s (see the module docstring): every
    read through ``KeyedLookup`` (a query key of ``read_set``), the scene through
    ``core.epistemic.scene``."""
    from epistrel.core import world_clock
    from epistrel.core.epistemic.scene import load_scene
    from epistrel.core.membership import Position
    from epistrel.core.propositions.registry import load
    from epistrel.core.readset import KeyedLookup
    from epistrel.core.resolution.context import load_index
    from epistrel.core.verify.rules import EVIDENCE_STATES

    registry = await load(conn, story_id)
    now = await world_clock.story_time_at(conn, story_id, Position(turn))
    canon = Canon(registry, now, await load_index(conn, story_id))
    canon.scene, _ = await load_scene(conn, story_id, turn, read_set=read_set)
    lookup = KeyedLookup(conn, read_set)

    async def facts(predicate: str, subjects: str | None, object_id: str | None) -> list[Record]:
        found = await lookup.canon_at(
            story_id,
            predicate=predicate,
            subjects=subjects,
            object=object_id,
            story_time=now,
            registry=registry,
        )
        return [Record.of_fact(f) for f in found if f.commit_state in EVIDENCE_STATES]

    context = context_of(canon, beats, stage)
    items, _ = wanted(effects, context)
    for item in sorted(items):
        canon.holds[item] = await facts(HOLDS, None, item)
        canon.owns[item] = await facts(OWNS, None, item)
    # A change's seller is the item's holder, known only now.
    _, parties = wanted(effects, context)
    for party in sorted(parties):
        canon.agreed[party] = await facts(AGREED_TO, party, None)
    return canon


def install() -> None:
    """Register ``evaluate`` as a ``core.states.authorize.PRECONDITIONS`` hook (once)."""
    if evaluate not in permission.PRECONDITIONS:
        permission.PRECONDITIONS.append(evaluate)


install()


__all__ = [
    "AVAILABILITY",
    "CONSENT",
    "CONSIDERATION",
    "CO_LOCATION",
    "NAMES",
    "OWNERSHIP",
    "PREFIX",
    "REASONS",
    "UNRESOLVED",
    "BeatView",
    "Canon",
    "Claim",
    "Context",
    "Parties",
    "Required",
    "context_of",
    "evaluate",
    "parties_of",
    "possession_item",
    "preconditions_for",
    "read",
    "reason_of",
    "resolve",
    "verdict_of",
    "wanted",
]
