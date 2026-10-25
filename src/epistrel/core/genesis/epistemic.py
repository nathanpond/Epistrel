"""Epistemic seeding: who starts out believing what (FR-SCN-8, FR-EPI-5; docs/02 §4.10; #173).

When a structured story's effective configuration keeps per-character partitions
(``character_knowledge.partitions``, #159), #149's materializer (``core.genesis.structured``)
takes the branch this module serves: the ``genesis_deferred`` rows of capability ``partitions``
are consumed from the scenario snapshot (each item's original JSON is ``payload.value``, its
resolved characters ``payload.characters``) and get ``materialized_at``. Without partitions they
stay unmaterialized, as under M4.

**Audiences** (``resolve``). A seed's tokens name roles: ``self`` the holder, a role id that
role, a named group (``cast.groups``) its members, ``acquaintances`` the roles a relationship
links to the holder either way plus the holder's group-mates; ``public`` and ``perceivable`` make
it shared. The holder is always in its own audience. A token naming an unfilled role or the
player's (the player has no partition) is dropped and reported ``seed_audience_unfilled``; a
group or an acquaintance that is the player is left out silently.

**Beliefs.** Every seeded belief is an ``author`` transition (#160) with provenance ``seed``,
citing its item's ``seed_materialized`` event (a ``control`` author act, one per item, with the
audience tokens and the disclosure) as its ``record`` antecedent, at genesis position
``(0, 0, 0, 0, k)`` (one step per belief, in plan order: #160's order rule holds, the author
act being position-less) and story minute -1, "before the story". It keeps its seed's
``disclosure`` (``beliefs.disclosure``; an audience member's is ``open``), which #166's lie rule
reads.

- A ``canon`` seed whose audience is limited (or secret) is a canon fact visible to its audience
  (#149's visibility, named groups added here) **and** a ``known`` belief in every member's
  partition, plus one semantic memory per member (``characters: [member]``). A shared seed
  (``public``/``perceivable``, not secret) writes no beliefs: visible canon suffices (#170).
- A ``belief`` seed writes only beliefs: the holder's (``believed`` unless the seed states its
  ``confidence``), copied first-order into each further member's partition; no canon, so a false
  belief survives genesis unchanged. One that does not normalize refuses the creation (422
  ``seed_unnormalizable``).
- A ``nested`` seed (``nested: {attributed_holder, proposition, attributed_confidence?}``) is
  #167's nested record (``nested.write_nested``, cause ``author``), attributed confidence
  ``known`` by default; it needs ``nested_beliefs`` too, and stays deferred without it.
- A relationship is ``public`` canon (#149) plus a ``known`` belief for each role that knows it
  (``audience``, default its two roles); its ``trust`` is the fact's qualifier (#162's
  ``seed_trust`` reads it).
- The player persona: its acquaintances (the roles a relationship links to the player, and the
  player's group-mates) believe its name, exact age (#150's ``age``) and each trait of its
  description (an auto ``trait`` predicate, multi: observation can revise it); a stranger holds
  none of it and perceives only what #170's context computes (gender, an age band). No canon is
  created from the persona's description.

**The per-holder merge** (``merge_beliefs``). A holder's beliefs from its seeds are merged like
canon (#140's ``merge_layers``), world layer first: a contradicting belief from a lower layer
is dropped and its item reported ``merge_conflict``; within one layer document order wins and
the loser is reported ``belief_conflict``. A belief that contradicts canon is kept (false
beliefs are allowed).

**Duplicate facts** (``dedupe_facts``). Two seeds stating one fact are one fact: both limited →
the union of their audiences (both holders); one shared and one limited → the higher source's
audience wins and the other is reported ``visibility_conflict``.

**The opening** (``write_opening``): turn 1's narration writes one #164 exposure per detected
proposition per present non-player role (``source = null``, ``kind = content``), at the
narration beat's witness position.

Pure but for ``write`` and ``write_opening``. Log lines carry identifiers and counts only
(invariant 3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import Visibility
from epistrel.core.enums import BeliefConfidenceLevel, MemoryKind
from epistrel.core.epistemic import exposure, nested, transitions
from epistrel.core.epistemic.transitions import Antecedent, Confidence
from epistrel.core.events.operation import Operation
from epistrel.core.membership import Position
from epistrel.core.memory.records import SeedMemory, write_seed
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.propositions.registry import Registry, auto_register
from epistrel.core.propositions.store import write_proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.tables import events
from epistrel.core.verify.merge import SAME_LAYER_ORDER, SeedFact, SeedLayer, merge_layers
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

PARTITIONS_MODULE: Final = "partitions"
NESTED_MODULE: Final = "nested_beliefs"
SEED_MATERIALIZED: Final = "seed_materialized"
SELF: Final = "self"
ACQUAINTANCES: Final = "acquaintances"
SHARED: Final = frozenset({"public", "perceivable"})
BEFORE_STORY: Final = -1
"""The story minute a seeded belief is held from: before the story began."""
TRAIT: Final = "trait"
"""The auto predicate a persona's traits are believed under (multi, weakest semantics)."""
UNFILLED: Final = "seed_audience_unfilled"
BELIEF_CONFLICT: Final = "belief_conflict"
VISIBILITY_CONFLICT: Final = "visibility_conflict"
Level = BeliefConfidenceLevel


def partitions_on(settings: Any) -> bool:
    """Whether the story keeps per-character partitions (the branch's switch)."""
    return settings is not None and module_on(settings, PARTITIONS_MODULE)


def nested_on(settings: Any) -> bool:
    """Whether the story keeps nested beliefs (a ``nested`` seed's second switch)."""
    return settings is not None and module_on(settings, NESTED_MODULE)


# -- audiences -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """The roles as audience resolution sees them: each filled role's character, the player's
    roles, the named groups and the relationship links (undirected)."""

    characters: Mapping[str, str]
    players: frozenset[str] = frozenset()
    groups: Mapping[str, Sequence[str]] = field(default_factory=dict)
    links: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        characters: Mapping[str, str],
        *,
        players: Iterable[str] = (),
        groups: Mapping[str, Sequence[str]] | None = None,
        relationships: Iterable[tuple[str, str]] = (),
    ) -> Cast:
        links: dict[str, set[str]] = {}
        for a, b in relationships:
            links.setdefault(a, set()).add(b)
            links.setdefault(b, set()).add(a)
        return cls(
            dict(characters),
            frozenset(players),
            {k: tuple(v) for k, v in (groups or {}).items()},
            {k: frozenset(v) for k, v in links.items()},
        )

    def acquaintances(self, role: str) -> set[str]:
        """The roles a relationship links to ``role``, and its group-mates."""
        found = set(self.links.get(role, ()))
        for members in self.groups.values():
            if role in members:
                found.update(members)
        found.discard(role)
        return found


@dataclass(frozen=True, slots=True)
class Audience:
    """A resolved audience: the roles in it (filled, not the player's; the holder included), the
    characters filling them, whether it is shared, and the tokens or roles dropped."""

    roles: tuple[str, ...]
    characters: tuple[str, ...]
    shared: bool
    dropped: tuple[str, ...] = ()


def resolve(tokens: Iterable[str], holder_role: str | None, cast: Cast) -> Audience:
    """``tokens`` (a seed's audience) for a seed held by ``holder_role`` (``None``: the world's)
    against ``cast`` (see the module docstring)."""
    wanted: list[str] = []
    named: set[str] = set()
    shared = False
    for token in tokens:
        if token in SHARED:
            shared = True
        elif token == SELF:
            if holder_role is not None:
                wanted.append(holder_role)
        elif token == ACQUAINTANCES:
            if holder_role is not None:
                wanted.extend(sorted(cast.acquaintances(holder_role)))
        elif token in cast.groups:
            wanted.extend(cast.groups[token])
        else:
            wanted.append(token)
            named.add(token)
    if holder_role is not None:
        wanted.insert(0, holder_role)
    roles: list[str] = []
    dropped: list[str] = []
    for role in dict.fromkeys(wanted):
        if role in cast.players or role not in cast.characters:
            if role in named:  # a group or an acquaintance that is the player is no news
                dropped.append(role)
            continue
        roles.append(role)
    return Audience(
        roles=tuple(roles),
        characters=tuple(cast.characters[r] for r in roles),
        shared=shared,
        dropped=tuple(dropped),
    )


def level_of(value: Any, default: Level) -> Level:
    """A seed's ``confidence`` (or ``default``)."""
    return Level(value) if isinstance(value, str) and value in Level else default


# -- persona traits --------------------------------------------------------------------------

_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\.)\s*", re.IGNORECASE)


def traits_of(description: str | None) -> tuple[str, ...]:
    """A persona description's traits: its comma-, semicolon- or ``and``-separated phrases,
    lower-cased (``"Short-tempered, flies off the handle"`` → ``("short-tempered", "flies off
    the handle")``)."""
    if not description:
        return ()
    found = [p.strip().lower() for p in _SPLIT.split(description)]
    return tuple(dict.fromkeys(p for p in found if p))


# -- the plan --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthorAct:
    """One ``seed_materialized`` event: the item whose beliefs cite it."""

    field_ref: str
    seed_id: str | None
    holder: str | None
    audience: tuple[str, ...]
    disclosure: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "field_ref": self.field_ref,
            "seed_id": self.seed_id,
            "holder_character_id": self.holder,
            "audience": list(self.audience),
            "disclosure": self.disclosure,
        }


@dataclass(frozen=True, slots=True)
class SeededBelief:
    """One seeded belief to write: its item (``act``, a field ref), its holder, its proposition
    (``None`` with ``trait`` set: a persona trait, built at the write once ``trait`` is
    registered), its confidence and disclosure, the merge key of the canon fact it copies
    (``None``: a belief only), its layer, and for a nested seed the attributed holder and its
    confidence."""

    act: str
    holder: str
    proposition: Proposition | None
    level: Level
    disclosure: str
    layer: SeedLayer = SeedLayer.ROLE
    fact_key: str | None = None
    attributed: str | None = None
    attributed_level: Level | None = None
    trait: str | None = None
    subject: str | None = None
    """A trait's subject (the persona's character)."""
    key: str = ""
    """The merge key (``<field_ref>@<holder>#<n>``)."""


@dataclass(frozen=True, slots=True)
class MemberMemory:
    """A seeded memory in one member's partition: its text, the member, the beliefs (keys) or
    canon facts (merge keys) it states, and its item."""

    act: str
    member: str
    text: str
    beliefs: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()


@dataclass(slots=True)
class EpistemicPlan:
    """What the partitions branch writes: the author acts, the beliefs, the members' memories
    and turn 1's exposures (its propositions and the witnesses present)."""

    acts: list[AuthorAct] = field(default_factory=list)
    beliefs: list[SeededBelief] = field(default_factory=list)
    memories: list[MemberMemory] = field(default_factory=list)
    opening: tuple[Proposition, ...] = ()
    witnesses: tuple[str, ...] = ()

    def act(self, found: AuthorAct) -> None:
        if all(a.field_ref != found.field_ref for a in self.acts):
            self.acts.append(found)


@dataclass(frozen=True, slots=True)
class BeliefLoss:
    """A seeded belief the per-holder merge dropped: the losing and winning beliefs' keys and
    the deciding rule (``belief_conflict`` when the same layer decided)."""

    loser: SeededBelief
    winner: SeededBelief
    rule: str
    code: str


def merge_beliefs(
    beliefs: Sequence[SeededBelief], *, registry: Registry
) -> tuple[list[SeededBelief], list[BeliefLoss]]:
    """The per-holder merge (see the module docstring): the beliefs kept, in order, and the ones
    dropped. A persona trait and a nested belief are never in conflict."""
    by_key = {b.key: b for b in beliefs}
    dropped: dict[str, BeliefLoss] = {}
    holders = dict.fromkeys(b.holder for b in beliefs)
    for holder in holders:
        layers: dict[SeedLayer, list[SeedFact]] = {layer: [] for layer in SeedLayer}
        for b in beliefs:
            if b.holder == holder and b.proposition is not None and b.attributed is None:
                layers[b.layer].append(SeedFact(b.key, b.layer, proposition=b.proposition))
        result = merge_layers(layers, registry=registry)
        for conflict in result.dropped:
            loser = by_key[conflict.loser.seed_id]
            winner = by_key[conflict.winner.seed_id]
            same = conflict.rule == SAME_LAYER_ORDER
            dropped[loser.key] = BeliefLoss(
                loser, winner, conflict.rule, BELIEF_CONFLICT if same else "merge_conflict"
            )
    return [b for b in beliefs if b.key not in dropped], list(dropped.values())


def dedupe_facts(
    facts: Sequence[tuple[str, Proposition, Visibility]],
) -> tuple[dict[str, tuple[str, Visibility]], list[tuple[str, str]]]:
    """Facts stating one proposition (``(merge key, proposition, visibility)``, higher source
    first): ``{key: (kept key, visibility)}`` for every key (the kept one maps to itself with
    the merged visibility), and the ``(loser, winner)`` keys whose audience lost
    (``visibility_conflict``; see the module docstring)."""
    kept: dict[tuple[Any, ...], tuple[str, Visibility]] = {}
    found: dict[str, tuple[str, Visibility]] = {}
    lost: list[tuple[str, str]] = []
    for key, proposition, visibility in facts:
        signature = (proposition.normalized_key(), proposition.polarity)
        first = kept.get(signature)
        if first is None:
            kept[signature] = (key, visibility)
            found[key] = (key, visibility)
            continue
        winner, held = first
        if held.scope == "characters" and visibility.scope == "characters":
            merged = Visibility(
                scope="characters",
                characters=tuple({*(held.characters or ()), *(visibility.characters or ())}),
            )
            kept[signature] = (winner, merged)
            found[winner] = (winner, merged)
        elif held != visibility:
            lost.append((key, winner))
        found[key] = (winner, kept[signature][1])
    for key, (winner, _) in list(found.items()):
        found[key] = (winner, found[winner][1])
    return found, lost


def numbered(beliefs: Sequence[SeededBelief]) -> list[SeededBelief]:
    """``beliefs`` deduplicated per (holder, proposition, attribution): one write each."""
    seen: set[tuple[Any, ...]] = set()
    found: list[SeededBelief] = []
    for b in beliefs:
        signature = (
            b.holder,
            None if b.proposition is None else b.proposition.normalized_key(),
            None if b.proposition is None else b.proposition.polarity,
            b.attributed,
            b.trait,
        )
        if signature in seen:
            continue
        seen.add(signature)
        found.append(b)
    return found


# -- the write -------------------------------------------------------------------------------


async def _acts(conn: AsyncConnection, operation_id: str) -> dict[str, str]:
    rows = await conn.execute(
        select(events.c.id, events.c.payload["field_ref"].astext.label("field_ref")).where(
            events.c.operation_id == operation_id, events.c.type == SEED_MATERIALIZED
        )
    )
    return {str(r.field_ref): str(r.id) for r in rows}


async def write(
    conn: AsyncConnection,
    story_id: str,
    operation: Operation,
    found: EpistemicPlan,
    *,
    facts: Mapping[str, Any],
    settings: Any,
    registry: Registry,
) -> dict[str, int]:
    """Record ``found``'s beliefs and member memories in the genesis operation's commit (after
    #149's facts: ``facts`` maps a merge key to its committed fact). Answers counts by kind."""
    acts = await _acts(conn, operation.operation_id)
    trait_predicate: str | None = None
    written: dict[str, str] = {}
    """A belief's key → its proposition id."""
    counts = {"beliefs": 0, "nested": 0, "memories": 0}
    for step, belief in enumerate(found.beliefs, start=1):
        act = acts.get(belief.act)
        if act is None:  # pragma: no cover - every planned item has its event
            continue
        proposition = belief.proposition
        if proposition is None:
            if trait_predicate is None:
                trait_predicate = (await auto_register(conn, story_id, TRAIT)).id
            proposition = Proposition(
                subject_id=str(belief.subject),
                predicate_id=trait_predicate,
                literal=TypedLiteral(kind=LiteralKind.STR, value=str(belief.trait)),
                valid_time=Interval(start=None, end=None),
            )
        position = Position(0, 0, 0, 0, step)
        cause = Antecedent.record("events", act)
        if belief.attributed is not None:
            made = await nested.write_nested(
                conn,
                operation,
                holder=belief.holder,
                attributed_holder=belief.attributed,
                proposition=proposition,
                cause=nested.Cause(transitions.Kind.AUTHOR, cause),
                position=position,
                confidence=Confidence(belief.level),
                attributed_confidence=Confidence(belief.attributed_level or Level.KNOWN),
                times=nested.Times(attributed_from=BEFORE_STORY),
                story_time=BEFORE_STORY,
                settings=settings,
            )
            written[belief.key] = made.belief.proposition_id
            counts["nested"] += 1
            continue
        result = await transitions.transition(
            conn,
            operation,
            holder=belief.holder,
            kind=transitions.Kind.AUTHOR,
            proposition=proposition,
            antecedent=cause,
            position=position,
            confidence=Confidence(belief.level),
            story_time=BEFORE_STORY,
            settings=settings,
            registry=None if trait_predicate is not None else registry,
            shape={"disclosure": belief.disclosure},
        )
        written[belief.key] = result.belief.proposition_id
        counts["beliefs"] += 1
    memories: list[SeedMemory] = []
    for memory in found.memories:
        act = acts.get(memory.act)
        stated = [facts[k] for k in memory.facts if k in facts]
        ids = [written[k] for k in memory.beliefs if k in written]
        ids += [f.proposition_id for f in stated]
        memories.append(
            SeedMemory(
                kind=MemoryKind.SEMANTIC if ids else MemoryKind.EPISODIC,
                text=memory.text,
                visibility=Visibility(scope="characters", characters=(memory.member,)),
                source_event_ids=() if act is None else (act,),
                holder_character_id=memory.member,
                proposition_ids=tuple(dict.fromkeys(ids)),
            )
        )
    if memories:
        counts["memories"] = len(await write_seed(conn, operation, memories))
    log.info("epistemic genesis", story_id=story_id, acts=len(found.acts), **counts)
    return counts


async def write_opening(
    conn: AsyncConnection,
    operation: Operation,
    propositions: Sequence[Proposition],
    witnesses: Sequence[str],
    *,
    beat: int,
    registry: Registry,
) -> int:
    """Turn 1's exposures (see the module docstring), in the opening turn's commit: answers how
    many it wrote."""
    story_id = operation.story_id
    if story_id is None or not propositions or not witnesses:
        return 0
    event_id = await conn.scalar(
        select(events.c.id).where(
            events.c.operation_id == operation.operation_id,
            events.c.type == "message_rendered",
        )
    )
    if event_id is None:  # pragma: no cover - the opening turn renders its narration
        return 0
    position = exposure.exposure_position(
        operation.narrative_turn or 1, operation.turn_variant or 0, beat
    )
    stored = [
        (p, await write_proposition(conn, story_id, p, registry, base_revision=None))
        for p in propositions
    ]
    written = await exposure.record_exposures(
        conn,
        operation,
        [
            exposure.Exposed(
                witness=witness,
                proposition_id=proposition_id,
                proposition=proposition,
                source=None,
                source_event_id=str(event_id),
                position=position,
            )
            for witness in witnesses
            for proposition, proposition_id in stored
        ],
    )
    log.info("opening exposures", story_id=story_id, exposures=len(written))
    return len(written)


def identity_beliefs(
    act: str, holder: str, subject: str, name: str, age: int, traits: Sequence[str]
) -> list[SeededBelief]:
    """A persona's name, exact age and traits, as ``holder``'s ``known``/``believed`` beliefs."""
    found = [
        SeededBelief(
            act=act,
            holder=holder,
            proposition=Proposition(
                subject_id=subject,
                predicate_id=CORE_BY_NAME["name"].id,
                literal=TypedLiteral(kind=LiteralKind.STR, value=name),
                valid_time=Interval.of(0, None),
            ),
            level=Level.KNOWN,
            disclosure="open",
            key=f"{act}@{holder}#name",
        ),
        SeededBelief(
            act=act,
            holder=holder,
            proposition=Proposition(
                subject_id=subject,
                predicate_id=CORE_BY_NAME["age"].id,
                literal=TypedLiteral(kind=LiteralKind.INT, value=age),
                valid_time=Interval.of(0, None),
            ),
            level=Level.KNOWN,
            disclosure="open",
            key=f"{act}@{holder}#age",
        ),
    ]
    found += [
        SeededBelief(
            act=act,
            holder=holder,
            proposition=None,
            level=Level.BELIEVED,
            disclosure="open",
            trait=trait,
            subject=subject,
            key=f"{act}@{holder}#trait{n}",
        )
        for n, trait in enumerate(traits)
    ]
    return found


__all__ = [
    "ACQUAINTANCES",
    "BEFORE_STORY",
    "BELIEF_CONFLICT",
    "SEED_MATERIALIZED",
    "TRAIT",
    "UNFILLED",
    "VISIBILITY_CONFLICT",
    "Audience",
    "AuthorAct",
    "BeliefLoss",
    "Cast",
    "EpistemicPlan",
    "MemberMemory",
    "SeededBelief",
    "dedupe_facts",
    "identity_beliefs",
    "level_of",
    "merge_beliefs",
    "nested_on",
    "numbered",
    "partitions_on",
    "resolve",
    "traits_of",
    "write",
    "write_opening",
]
