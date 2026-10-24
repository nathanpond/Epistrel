"""Identity as a holder-scoped proposition (FR-PROP-7; docs/02 §4.9; #171).

``same_as(e1, e2)`` is an ordinary proposition (#131's core predicate, symmetric and
permanent): canon holds it as a fact with a visibility (#133), and a character holds it as a
belief in its partition (#159). Knowing that "the stranger" is Bobby is therefore something a
character can know or not know: canon and Bobby may hold it while Jenni does not, so "the
stranger" resolves to Bobby for them and stays the stranger for her.

**The one helper.** ``identities_for`` reads what one holder holds, through #136's
``KeyedLookup`` (so a ``same_as`` change touches the turn's read set):

- **canon** (the narrator, the director, an omniscient read): the ``same_as`` facts visible to
  ``omniscient``;
- **a character** C: the ``same_as`` facts visible to ``character:C`` and C's own ``same_as``
  beliefs held at ``suspected`` or above (its partition);
- **the player's character**: the facts visible to it (it holds no partition, FR-EPI-12).

``holder_of`` names the holder a speaker resolves as (narrator → canon, ``player`` → the
player character's view, a character → its beliefs and view). With
``character_knowledge.partitions`` off a character holds no beliefs, and every scope it reads is
the shared one, so identities are holder-agnostic there.

**Expansion** is depth 1 (#140): an entity's class is itself and its direct ``same_as``
partners. ``representatives`` is the pure rule the verifier's record set uses (the least id of
the class); ``Identities.rep`` prefers a modeled character in the class (the stranger's
representative is Bobby, whatever their ids), and else the least id. Registered aliases stay
holder-agnostic: only entity-to-entity ``same_as`` is holder-scoped.

Nothing here writes or calls a model; logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT

if TYPE_CHECKING:
    from epistrel.core.membership import Through
    from epistrel.core.readset import KeyedLookup

CANON = "canon"
"""The canon holder (``identities_for(holder=CANON)``): omniscient canon."""


def pair_of(p: Proposition) -> tuple[str, str] | None:
    """The two entities an affirmed entity-to-entity ``same_as`` proposition links, in id
    order (``None``: not one)."""
    if (
        p.predicate_id != SAME_AS
        or p.polarity is not Polarity.AFFIRMED
        or p.object_id is None
        or p.object_id == p.subject_id
    ):
        return None
    a, b = sorted((p.subject_id, p.object_id))
    return a, b


def representatives(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Each linked entity's representative at depth 1: the least id among itself and its
    direct partners (#140's rule)."""
    reps: dict[str, str] = {}
    for a, b in pairs:
        for x, y in ((a, b), (b, a)):
            reps[x] = min(reps.get(x, x), y)
    return reps


@dataclass(frozen=True, slots=True)
class Identity:
    """One held ``same_as``: the pair (id order), and the canon fact or the belief it is."""

    entities: tuple[str, str]
    source: str
    """``canon`` (a fact the holder may read) or ``belief`` (one of the holder's own)."""
    record_id: str


@dataclass(frozen=True, slots=True)
class Identities:
    """The ``same_as`` a holder holds, and the classes they make (see the module docstring).
    ``characters`` are the modeled characters' ids: a class's representative is one of them
    when it has one."""

    holder: str = CANON
    held: tuple[Identity, ...] = ()
    characters: frozenset[str] = frozenset()
    _partners: dict[str, frozenset[str]] = field(default_factory=dict, compare=False)

    @classmethod
    def of(
        cls,
        held: Iterable[Identity],
        *,
        holder: str = CANON,
        characters: Iterable[str] = (),
    ) -> Identities:
        kept = tuple({(i.entities, i.source, i.record_id): i for i in held}.values())
        partners: dict[str, set[str]] = {}
        for identity in kept:
            a, b = identity.entities
            partners.setdefault(a, set()).add(b)
            partners.setdefault(b, set()).add(a)
        return cls(
            holder,
            kept,
            frozenset(characters),
            {k: frozenset(v) for k, v in partners.items()},
        )

    def __bool__(self) -> bool:
        return bool(self.held)

    def members(self, entity_id: str) -> tuple[str, ...]:
        """``entity_id`` and its direct ``same_as`` partners for this holder (depth 1), in id
        order."""
        return tuple(sorted({entity_id, *self._partners.get(entity_id, ())}))

    def rep(self, entity_id: str) -> str:
        """The representative of ``entity_id``'s class for this holder: a modeled character of
        the class (the least id when several), else the least id."""
        found = self.members(entity_id)
        if len(found) == 1:
            return entity_id
        modeled = [e for e in found if e in self.characters]
        return modeled[0] if modeled else found[0]

    def expand(self, entity_ids: Iterable[str]) -> tuple[str, ...]:
        """Every member of the classes of ``entity_ids``, in first-seen order."""
        out: dict[str, None] = {}
        for entity_id in entity_ids:
            out[entity_id] = None
            for member in self.members(entity_id):
                out.setdefault(member, None)
        return tuple(out)

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({i.entities for i in self.held}))

    def document(self) -> list[dict[str, Any]]:
        """The JSON form (``/inspect/beliefs``' ``identities``)."""
        return [
            {"entities": list(i.entities), "source": i.source, "record_id": i.record_id}
            for i in sorted(self.held, key=lambda i: (i.entities, i.source, i.record_id))
        ]


NONE = Identities()


def holder_of(speaker: str, player_id: str | None = None) -> str:
    """The holder a speaker resolves identities as: ``canon`` for the narrator (and an
    omniscient read), the player character's id for ``player``, else the speaker's id."""
    from epistrel.core.resolution.entities import NARRATOR, PLAYER

    if speaker in (NARRATOR, OMNISCIENT, CANON, "narrator"):
        return CANON
    if speaker == PLAYER:
        return player_id or CANON
    return speaker


def holder_of_viewpoint(viewpoint: str) -> str:
    """The holder a read viewpoint resolves identities as: ``character:<id>`` → that
    character; any other (``omniscient``, ``narrator``, a shared reader) → canon."""
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    return CANON


async def identities_for(
    lookup: KeyedLookup,
    story_id: str,
    holder: str,
    *,
    beliefs: bool = True,
    through_position: Through | None = None,
    through: Any = None,
    characters: Iterable[str] | None = None,
    viewpoint: str | None = None,
) -> Identities:
    """What ``holder`` (``canon`` or a character id) holds of ``same_as`` (see the module
    docstring), read through ``lookup`` (each read a query key of its read set). ``beliefs``:
    whether the character's partition is read (false for the player's character, or with
    partitions off); ``through`` the beliefs' held position (``None``: now). ``characters``
    are the modeled characters (read from the story when ``None``). ``viewpoint`` overrides the
    canon viewpoint read (``omniscient`` for canon, ``character:<holder>`` otherwise): a
    ``narrator`` read holds only the shared scopes."""
    from epistrel.core.epistemic.partitions import HELD_LEVELS

    key = ("identities", story_id, holder, beliefs, through_position, through, viewpoint)
    if key in lookup.memo:
        cached: Identities = lookup.memo[key]
        return cached
    if viewpoint is None:
        viewpoint = OMNISCIENT if holder == CANON else f"{CHARACTER_VIEWPOINT_PREFIX}{holder}"
    held: list[Identity] = []
    for fact in await lookup.canon_at(
        story_id,
        predicate=SAME_AS,
        subjects=None,
        viewpoint=viewpoint,
        through_position=through_position,
    ):
        found = pair_of(fact.proposition)
        if found is not None:
            held.append(Identity(found, "canon", fact.id))
    if holder != CANON and beliefs:
        for belief in await lookup.beliefs(
            story_id,
            holder=holder,
            predicate=SAME_AS,
            subjects=None,
            through=through,
            levels=sorted(HELD_LEVELS),
            viewpoint=viewpoint,
        ):
            found = pair_of(belief.proposition)
            if found is not None:
                held.append(Identity(found, "belief", belief.id))
    if characters is None:
        characters = await _modeled(lookup, story_id) if held else ()
    made = Identities.of(held, holder=holder, characters=characters)
    lookup.memo[key] = made
    return made


async def _modeled(lookup: KeyedLookup, story_id: str) -> frozenset[str]:
    from sqlalchemy import select

    from epistrel.core.tables import characters

    rows = await lookup.conn.scalars(
        select(characters.c.id).where(characters.c.story_id == story_id)
    )
    return frozenset(str(r) for r in rows)


def linked_characters(identities: Identities, entity_ids: Iterable[str]) -> Mapping[str, str]:
    """Each of ``entity_ids`` that is not a modeled character but whose class holds one: the
    character it is (#163's modeling is suppressed for it)."""
    found: dict[str, str] = {}
    for entity_id in entity_ids:
        if entity_id in identities.characters:
            continue
        rep = identities.rep(entity_id)
        if rep != entity_id and rep in identities.characters:
            found[entity_id] = rep
    return found


__all__ = [
    "CANON",
    "NONE",
    "Identities",
    "Identity",
    "holder_of",
    "holder_of_viewpoint",
    "identities_for",
    "linked_characters",
    "pair_of",
    "representatives",
]
