"""Conflicting actions in one ``simultaneous`` turn, detected deterministically (FR-MULTI-8;
docs/02 §8; #175).

In ``simultaneous`` mode every primary renders against the turn's starting state and none sees
another's reaction, so two of them can both act on one object. ``detect_conflicts`` finds those
clashes over the turn's **effects** (``Effect``): the normalized propositions of each staged
``do`` beat's claims (declared, or found by #148's possession and placement detectors), of each
``latent`` beat's, and of the player's input ``do``, each with the acting character and its
rank in request order. Speech never conflicts (two people talking at once is fine), and neither
does a ``think``. No model is involved: the answer is a pure function of the effects and the
story's ``Registry``.

**Incompatible outcomes.** Two effects of two different characters (a character's clash with
itself is #166's to repair) conflict when:

- #140's deterministic rules (``core.verify.rules.check``, each effect read as the other's one
  record) answer ``contradicted``: the functional-key rule (the same uniqueness key, a
  different object, overlapping time; through a registered inverse with cardinality, so
  ``holds(Bobby, key)`` and ``holds(Jenni, key)`` are two holders of one key under the
  functional ``held_by``), a closed set the caller passes, max cardinality, a declared exclusion
  or a registered implication. An ``uncertain`` answer is not a conflict by this rule;
- or both place one object (``located_at``) in two different places that no containment fact
  the caller passes relates (the same place, or one contains the other through ``located_in``,
  either way, up to ``Options.containment_max_depth``): the location fluent is single-valued,
  so the key cannot be in the study and in the hall at once (#549). This is the ``placement``
  rule. ADR-160's ``uncertain(world_knowledge)`` still governs a claim checked against canon
  (#140, unchanged); it does not decide whether two same-turn effects can both happen, and a
  false conflict costs only one effect losing the tie-break, where a miss commits both. Only
  ``detect_conflicts`` applies it (``clash(placement=True)``): #177's reconcile, where a later
  placement may move the object on, keeps #140's answer;
- or one possesses an item (``holds``/``held_by``) and the other places that same item
  (``located_at``): the core possession/placement pair, always incompatible inside one moment
  (Bobby pockets the key while Jenni locks it in the drawer). Other cross-predicate pairs
  conflict only through a registered implication (the first rule).

Only affirmed effects take part: a negation ("she leaves") changes no object two ways.

**Grouping.** Each conflict is keyed by the contested **object**: the shared subject (a place for
one item, a state for one door), else the shared object (two holders of one item), else the item
of the possession/placement pair. Effects clashing over one object form one group (``Conflict``,
its effects in request order), adjudicated with one winner (``orchestrator.structured
.simultaneous``); an effect may sit in several groups. Effects are sorted by rank, beat and claim
id first, so the answer never depends on the order they arrive in.

Pure: no I/O, no story text in any log (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import HELD_BY, HOLDS, LOCATED_AT
from epistrel.core.verify.rules import ClosedSetRecord, Options, Record, VerdictKind, check

POSSESSION_PLACEMENT = "possession_placement"
"""The rule of the core possession/placement pair."""
PLACEMENT = "placement"
"""The rule of one object placed in two places no containment fact relates (#549)."""
POSSESSION = frozenset({HOLDS, HELD_BY})
_DEFAULT_OPTIONS = Options()


@dataclass(frozen=True, slots=True)
class Effect:
    """One effect of the turn: the beat that declared it, the acting character, its rank in
    request order (the player's input is 0), its normalized proposition and its claim id."""

    beat: int
    character_id: str
    rank: int
    proposition: Proposition
    claim_id: str | None = None
    player: bool = False

    def key(self) -> tuple[int, int, str]:
        return (self.rank, self.beat, self.claim_id or "")


@dataclass(frozen=True, slots=True)
class Conflict:
    """The effects that clash over one object, in request order, and the rules that found it."""

    object_id: str
    effects: tuple[Effect, ...]
    rules: tuple[str, ...]

    @property
    def beats(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(e.beat for e in self.effects))

    @property
    def characters(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(e.character_id for e in self.effects))


def _item_of_possession(p: Proposition) -> str | None:
    if p.predicate_id == HOLDS:
        return p.object_id
    if p.predicate_id == HELD_BY:
        return p.subject_id
    return None


def _possession_placement(a: Proposition, b: Proposition) -> str | None:
    """The item when one of ``a``/``b`` possesses it and the other places it."""
    for held, placed in ((a, b), (b, a)):
        item = _item_of_possession(held)
        if item is not None and placed.predicate_id == LOCATED_AT and placed.subject_id == item:
            return item
    return None


def _outer(containment: Sequence[Record], predicate_id: str, inner: str, depth: int) -> set[str]:
    """What contains ``inner`` through ``predicate_id``'s affirmed facts, up to ``depth`` hops."""
    edges: dict[str, set[str]] = {}
    for r in containment:
        p = r.proposition
        if (
            p.predicate_id == predicate_id
            and p.polarity is Polarity.AFFIRMED
            and p.object_id is not None
        ):
            edges.setdefault(p.subject_id, set()).add(p.object_id)
    found: set[str] = set()
    frontier = {inner}
    for _ in range(depth):
        step = set().union(*(edges.get(x, set()) for x in frontier)) - found - {inner}
        if not step:
            break
        found |= step
        frontier = step
    return found


def placement_pair(a: Proposition, b: Proposition) -> bool:
    """Whether ``a`` and ``b`` both place one object (affirmed ``located_at``) in two different
    places: the pair whose answer needs the containment facts (#549)."""
    return (
        a.predicate_id == LOCATED_AT
        and b.predicate_id == LOCATED_AT
        and a.polarity is Polarity.AFFIRMED
        and b.polarity is Polarity.AFFIRMED
        and a.subject_id == b.subject_id
        and a.object_id is not None
        and b.object_id is not None
        and a.object_id != b.object_id
    )


def _two_places(
    a: Proposition,
    b: Proposition,
    registry: Registry,
    containment: Sequence[Record],
    options: Options,
) -> bool:
    """Whether ``a`` and ``b`` place one object in two places no containment fact relates."""
    if not placement_pair(a, b):
        return False
    predicate = registry.get(LOCATED_AT)
    via = None if predicate is None else predicate.containment_of
    if via is None:
        return True
    pa, pb = str(a.object_id), str(b.object_id)
    depth = options.containment_max_depth
    return pb not in _outer(containment, via, pa, depth) and pa not in _outer(
        containment, via, pb, depth
    )


def _contested(a: Proposition, b: Proposition) -> str:
    """The object two clashing propositions contest (see the module docstring)."""
    if a.subject_id == b.subject_id:
        return a.subject_id
    if a.object_id is not None and a.object_id == b.object_id:
        return a.object_id
    item_a, item_b = _item_of_possession(a), _item_of_possession(b)
    if item_a is not None and item_a == item_b:
        return item_a
    return b.subject_id


def _contradicts(
    a: Effect, b: Effect, registry: Registry, closed_sets: Sequence[ClosedSetRecord]
) -> bool:
    for claim, other in ((b, a), (a, b)):
        verdict = check(
            claim.proposition,
            [Record(other.proposition, other.claim_id or f"effect:{other.beat}")],
            registry=registry,
            closed_sets=closed_sets,
        )
        if verdict.kind is VerdictKind.CONTRADICTED:
            return True
    return False


def clash(
    a: Effect,
    b: Effect,
    registry: Registry,
    closed_sets: Sequence[ClosedSetRecord] = (),
    containment: Sequence[Record] = (),
    options: Options = _DEFAULT_OPTIONS,
    *,
    placement: bool = False,
) -> tuple[str, str] | None:
    """``(object, rule)`` when ``a`` and ``b`` are incompatible outcomes (see the module
    docstring), else ``None``. ``placement`` applies the placement rule (``detect_conflicts``:
    a simultaneous turn's one moment; #177's reconcile does not), ``containment`` the
    containment facts (``located_in``) that relate the places of a placement pair."""
    if a.character_id == b.character_id:
        return None
    pa, pb = a.proposition, b.proposition
    if pa.polarity is not Polarity.AFFIRMED or pb.polarity is not Polarity.AFFIRMED:
        return None
    item = _possession_placement(pa, pb)
    if item is not None:
        return item, POSSESSION_PLACEMENT
    if placement and placement_pair(pa, pb):
        if _two_places(pa, pb, registry, containment, options):
            return pa.subject_id, PLACEMENT
        return None
    if _contradicts(a, b, registry, closed_sets):
        return _contested(pa, pb), "rules"
    return None


def detect_conflicts(
    effects: Sequence[Effect],
    *,
    registry: Registry,
    closed_sets: Sequence[ClosedSetRecord] = (),
    containment: Sequence[Record] = (),
    options: Options = _DEFAULT_OPTIONS,
) -> list[Conflict]:
    """The turn's conflicts, one per contested object, in the order of their first effect (see
    the module docstring); ``containment`` the containment facts a placement pair reads
    (``needs_containment``). Pure."""
    ordered = sorted(effects, key=Effect.key)
    groups: dict[str, list[Effect]] = {}
    rules: dict[str, list[str]] = {}
    for n, a in enumerate(ordered):
        for b in ordered[n + 1 :]:
            found = clash(a, b, registry, closed_sets, containment, options, placement=True)
            if found is None:
                continue
            obj, rule = found
            group = groups.setdefault(obj, [])
            for effect in (a, b):
                if effect not in group:
                    group.append(effect)
            if rule not in rules.setdefault(obj, []):
                rules[obj].append(rule)
    conflicts = [
        Conflict(obj, tuple(sorted(group, key=Effect.key)), tuple(rules[obj]))
        for obj, group in groups.items()
    ]
    return sorted(conflicts, key=lambda c: (c.effects[0].key(), c.object_id))


def needs_containment(effects: Sequence[Effect]) -> bool:
    """Whether two characters' effects place one object in two different places, so the
    caller must read the containment facts (keyed, #136) before ``detect_conflicts``."""
    for n, a in enumerate(effects):
        for b in effects[n + 1 :]:
            if a.character_id != b.character_id and placement_pair(a.proposition, b.proposition):
                return True
    return False


__all__ = [
    "PLACEMENT",
    "POSSESSION_PLACEMENT",
    "Conflict",
    "Effect",
    "clash",
    "detect_conflicts",
    "needs_containment",
    "placement_pair",
]
