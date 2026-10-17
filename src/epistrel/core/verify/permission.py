"""Novelty is not permission: who may introduce a ``novel`` claim (FR-VERIFY-11,12; docs/02
§4.9; #144).

A ``novel`` verdict (#140, settled by #142's verifier) does not by itself authorize
canonization. ``permission(claim, asserter, subject_owner, facts)`` decides it from who asserts
the claim and who owns what it is about, reading structured data only (entity kinds, character
ids, the lock flag, the predicate's owner, the story's arrival policy); nothing here calls a
model or imports the Orchestrator.

**Asserters** (``Asserter``): a character's render (``character``), a director/world pass
(``director``), player input (``player``, whose actor is the player's own character; input for
a character the platform plays is that character's render, FR-AUTH-3) and the author
(``author``). M5's exceptions are not delivered: attributed reactive narration (``reactive``,
FR-MULTI-9) is judged as its character's render and ``co_author`` player input (FR-RESP-11) as
plain player input, so a claim either exception would admit is ``Denied``.

**Ownership.** ``owner_of`` derives the subject's owner from ``entities.character_id`` and
``characters.is_player``: ``player_character``, ``modeled_character`` (an entity with a
character row), ``external`` (the predicate is owned by an external provider) or ``world``
(anything else: places, items, an unmodeled person, who is scene texture). Every
character-typed argument is inspected, not only the subject: Bobby asserting he is Jenni's
sibling introduces Jenni's backstory too.

**The matrix (M4).** In precedence order, one reason per denial:

- ``external_value``: the predicate is externally owned (FR-EXT-8): denied to everyone, the
  author included;
- the author may introduce anything else (``author``);
- ``locked_canon``: the claim's (subject, predicate) has a current ``author_locked`` fact, or
  it sets a character's definition-locked field (``age``, ``name``, ``appearance_of``,
  ``visual_identity_of``: the definition's true age, name, appearance and visual identity);
- ``state_change``: a change to a story State outside a validated effect (FR-STATE-5): never
  fires in M4 (States arrive in M6);
- ``player_character``: a render or a director pass asserting the player character's actions,
  speech, thoughts or backstory;
- ``other_character_secret``: a render asserting another modeled character's secrets,
  backstory, actions or thoughts, or a director pass asserting anything about a modeled
  character beyond an arrival;
- ``player_authority``: player input about another character (``player_authority =
  own_character``): the player's text stands and is not repaired, but it is never canonized;
- ``policy``: an arrival while the story's ``allow_new_arrivals`` is off (a director pass's
  arrival, or a render bringing in a new unmodeled person);
- ``protected_content``: the claim's text matched protected content (#148's detector, passed
  in): repaired.

A permitted claim carries its basis: ``own`` (the asserter's own actions, thoughts and minor
backstory within its constraints), ``texture`` (incidental scene texture, an unmodeled person
included), ``arrival``, ``world`` (a director's world fact) or ``author``. An **arrival** is a
``located_at``/``present_at`` claim on a person with no prior placement (no current fact of
either predicate).

**Reads** (``check``): the predicate registry, the claim's entities and their characters, the
story's ``allow_new_arrivals`` setting, and through ``KeyedLookup`` (this module is a
``KEYED_READERS`` member) the claim's own (subject, predicate) facts for a lock and the
subject's placement for an arrival, so a concurrent lock or placement on those keys
regenerates the turn. The arrival policy and the set of locks are also configuration members
of a structured turn (``core.config_dependencies``: ``allow_new_arrivals``, ``author_locks``),
so an edit of either between a turn's start and its commit re-checks it: ``recheck`` (a
``REBASE_CHECKS`` entry) re-runs every answer the turn's attempt recorded and regenerates when
one changed. Verification off (a Lite story) is inert: ``None``, nothing read.

Log lines carry counts and codes only (invariant 3).
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import RecordProvenance
from epistrel.core.propositions.model import Predicate, PredicateOwner, Proposition
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import KeyedLookup, StructuredReadSet, register_rebase_check
from epistrel.core.settings_schema import OMNISCIENT, StorySettings
from epistrel.core.tables import characters, entities, stories
from epistrel.core.verify.rules import options_for

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation

log = logging.getLogger(__name__)

AsserterKind = Literal["character", "director", "player", "author", "reactive", "co_author"]
OwnerKind = Literal["modeled_character", "player_character", "world", "external"]

PLACEMENT_PREDICATES: tuple[str, ...] = ("located_at", "present_at")
"""An arrival's predicates (by name; both core)."""
PERSON = "person"

LOCKED_FIELDS: frozenset[str] = frozenset({"age", "name", "appearance_of", "visual_identity_of"})
"""The predicates a character definition fixes (true age, name, appearance, visual identity):
no render, director or player may introduce them for a modeled character (``locked_canon``)."""


class Reason(StrEnum):
    """Why a novel claim is not permitted (one code per denial; see the module docstring)."""

    EXTERNAL_VALUE = "external_value"
    LOCKED_CANON = "locked_canon"
    STATE_CHANGE = "state_change"
    PLAYER_CHARACTER = "player_character"
    OTHER_CHARACTER_SECRET = "other_character_secret"  # noqa: S105 - a reason code
    PLAYER_AUTHORITY = "player_authority"
    POLICY = "policy"
    PROTECTED_CONTENT = "protected_content"


PRECEDENCE: tuple[Reason, ...] = tuple(Reason)
"""Reasons in precedence order: the first that applies is the denial's one code."""


class Basis(StrEnum):
    """Why a novel claim is permitted."""

    OWN = "own"
    TEXTURE = "texture"
    ARRIVAL = "arrival"
    WORLD = "world"
    AUTHOR = "author"


@dataclass(frozen=True, slots=True)
class Asserter:
    """Who asserts a claim: its kind and, for a character's render, reactive narration or
    player input, the character (the player's own character for ``player``)."""

    kind: AsserterKind
    character_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind in ("character", "reactive", "player", "co_author") and not self.character_id:
            raise ValueError(f"a {self.kind} asserter names its character")

    @property
    def provenance(self) -> RecordProvenance:
        """The provenance a canonized claim of this asserter records (#133's shared enum)."""
        if self.kind == "director":
            return RecordProvenance.DIRECTOR
        if self.kind in ("player", "co_author"):
            return RecordProvenance.PLAYER
        if self.kind == "author":
            return RecordProvenance.AUTHOR
        return RecordProvenance.RENDER

    def key(self) -> str:
        """``<kind>`` or ``<kind>:<character id>``, as the turn report lists it."""
        return self.kind if self.character_id is None else f"{self.kind}:{self.character_id}"


@dataclass(frozen=True, slots=True)
class SubjectOwner:
    """Who owns a claim's subject (see ``owner_of``)."""

    kind: OwnerKind
    character_id: str | None = None


@dataclass(frozen=True, slots=True)
class Permitted:
    basis: Basis
    ok: Literal[True] = True

    def code(self) -> str:
        return f"permitted:{self.basis.value}"


@dataclass(frozen=True, slots=True)
class Denied:
    reason: Reason
    ok: Literal[False] = False

    @property
    def repaired(self) -> bool:
        """Whether #151 repairs the claim (tagged ``unpermitted``): every denial but
        ``player_authority``, whose player text stands (never canonized either way)."""
        return self.reason is not Reason.PLAYER_AUTHORITY

    def code(self) -> str:
        return f"denied:{self.reason.value}"


type Permission = Permitted | Denied


@dataclass(frozen=True, slots=True)
class EntityInfo:
    """What the matrix needs of one entity: its kind and, for a modeled character, its
    character id and whether that character is the player's."""

    id: str
    kind: str
    character_id: str | None = None
    is_player: bool = False

    @property
    def modeled(self) -> bool:
        return self.character_id is not None


@dataclass(frozen=True, slots=True)
class PermissionFacts:
    """The structured data one claim's permission is decided over (``check`` reads it; a unit
    test passes it in)."""

    entities: Mapping[str, EntityInfo] = field(default_factory=dict)
    predicate: Predicate | None = None
    locked: bool = False
    """A current ``author_locked`` fact has the claim's (subject, predicate)."""
    placed: bool = True
    """The subject has a prior placement (a current ``located_at``/``present_at`` fact)."""
    allow_new_arrivals: bool = True
    protected: bool = False
    """The claim's text matched protected content (#148's detector)."""


# -- the matrix ---------------------------------------------------------------------------------


def _argument_ids(claim: Proposition) -> list[str]:
    found = [claim.subject_id]
    if claim.object_id is not None:
        found.append(claim.object_id)
    found += list(claim.object_set or ())
    found += [v for v in claim.qualifiers.values() if isinstance(v, str)]
    return list(dict.fromkeys(found))


def owner_of(claim: Proposition, facts: PermissionFacts) -> SubjectOwner:
    """The claim subject's owner: the player character, a modeled character, an external
    provider (an externally owned predicate) or the world."""
    if facts.predicate is not None and facts.predicate.owner is PredicateOwner.EXTERNAL:
        return SubjectOwner("external")
    found = facts.entities.get(claim.subject_id)
    if found is not None and found.modeled:
        kind: OwnerKind = "player_character" if found.is_player else "modeled_character"
        return SubjectOwner(kind, found.character_id)
    return SubjectOwner("world")


def _is_arrival(claim: Proposition, facts: PermissionFacts) -> bool:
    if facts.predicate is None or facts.predicate.name not in PLACEMENT_PREDICATES:
        return False
    subject = facts.entities.get(claim.subject_id)
    return subject is not None and subject.kind == PERSON and not facts.placed


def permission(
    claim: Proposition, asserter: Asserter, subject_owner: SubjectOwner, facts: PermissionFacts
) -> Permission:
    """Whether ``asserter`` may introduce the novel ``claim`` (see the module docstring)."""
    if subject_owner.kind == "external":
        return Denied(Reason.EXTERNAL_VALUE)
    if asserter.kind == "author":
        return Permitted(Basis.AUTHOR)
    name = None if facts.predicate is None else facts.predicate.name
    if facts.locked or (
        name in LOCKED_FIELDS and subject_owner.kind in ("modeled_character", "player_character")
    ):
        return Denied(Reason.LOCKED_CANON)
    modeled = [
        e for i in _argument_ids(claim) if (e := facts.entities.get(i)) is not None and e.modeled
    ]
    arrival = _is_arrival(claim, facts)
    if asserter.kind == "director":
        if arrival:
            return Permitted(Basis.ARRIVAL) if facts.allow_new_arrivals else Denied(Reason.POLICY)
        if any(e.is_player for e in modeled):
            return Denied(Reason.PLAYER_CHARACTER)
        if modeled:
            return Denied(Reason.OTHER_CHARACTER_SECRET)
        return _texture(facts, Basis.WORLD)
    own = asserter.character_id
    others = [e for e in modeled if e.character_id != own]
    mine = next((e for e in facts.entities.values() if e.character_id == own), None)
    if asserter.kind in ("player", "co_author") and (mine is None or mine.is_player):
        # Player input for the player's own character (FR-AUTH-3: input for a character the
        # platform plays is that character's render, below).
        if others:
            return Denied(Reason.PLAYER_AUTHORITY)
    elif any(e.is_player for e in others):
        return Denied(Reason.PLAYER_CHARACTER)
    elif others:
        return Denied(Reason.OTHER_CHARACTER_SECRET)
    if arrival and subject_owner.character_id != own and not facts.allow_new_arrivals:
        return Denied(Reason.POLICY)
    return _texture(facts, Basis.OWN if subject_owner.character_id == own else Basis.TEXTURE)


def _texture(facts: PermissionFacts, basis: Basis) -> Permission:
    if facts.protected:
        return Denied(Reason.PROTECTED_CONTENT)
    return Permitted(basis)


# -- reading the facts ----------------------------------------------------------------------


async def entity_infos(
    conn: AsyncConnection, story_id: str, ids: Sequence[str]
) -> dict[str, EntityInfo]:
    """The story's entities among ``ids`` with their kind, character and whether that is the
    player's character (an id that is no entity, e.g. a literal's, is absent)."""
    if not ids:
        return {}
    rows = await conn.execute(
        select(entities.c.id, entities.c.kind, entities.c.character_id, characters.c.is_player)
        .select_from(
            entities.outerjoin(
                characters,
                (characters.c.id == entities.c.character_id)
                & (characters.c.story_id == entities.c.story_id),
            )
        )
        .where(entities.c.story_id == story_id, entities.c.id.in_(list(ids)))
    )
    return {
        str(r.id): EntityInfo(
            str(r.id),
            str(r.kind),
            None if r.character_id is None else str(r.character_id),
            bool(r.is_player),
        )
        for r in rows
    }


async def character_infos(
    conn: AsyncConnection,
    story_id: str,
    character_ids: Sequence[str],
    known: Mapping[str, EntityInfo],
) -> dict[str, EntityInfo]:
    """The characters among ``character_ids`` that ``known`` holds no entity for (a character
    whose entity was never resolved), as person entities keyed by character id."""
    wanted = [c for c in character_ids if not any(e.character_id == c for e in known.values())]
    if not wanted:
        return {}
    rows = await conn.execute(
        select(characters.c.id, characters.c.is_player).where(
            characters.c.story_id == story_id, characters.c.id.in_(wanted)
        )
    )
    return {str(r.id): EntityInfo(str(r.id), PERSON, str(r.id), bool(r.is_player)) for r in rows}


async def arrival_policy(conn: AsyncConnection, story_id: str) -> bool:
    """The story's ``allow_new_arrivals`` (true for a story without it)."""
    document = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    return StorySettings.from_row(document).allow_new_arrivals


async def facts_for(
    lookup: KeyedLookup,
    story_id: str,
    claim: Proposition,
    *,
    registry: Registry,
    asserter: Asserter | None = None,
    through_position: tuple[int, int] | None = None,
    protected: bool = False,
) -> PermissionFacts:
    """The structured data ``claim``'s permission is decided over (the claim's entities and
    the asserter's own), its canon reads keyed."""
    conn = lookup.conn
    predicate = registry.get(claim.predicate_id)
    ids = _argument_ids(claim)
    if asserter is not None and asserter.character_id is not None:
        ids.append(asserter.character_id)
    known = await entity_infos(conn, story_id, ids)
    if asserter is not None and asserter.character_id is not None:
        known.update(await character_infos(conn, story_id, [asserter.character_id], known))
    locks = await lookup.canon_at(
        story_id,
        predicate=claim.predicate_id,
        subjects=claim.subject_id,
        viewpoint=OMNISCIENT,
        through_position=through_position,
    )
    placed = True
    subject = known.get(claim.subject_id)
    if (
        predicate is not None
        and predicate.name in PLACEMENT_PREDICATES
        and subject is not None
        and subject.kind == PERSON
    ):
        placed = False
        for name in PLACEMENT_PREDICATES:
            found = await lookup.canon_at(
                story_id,
                predicate=CORE_BY_NAME[name].id,
                subjects=claim.subject_id,
                viewpoint=OMNISCIENT,
                through_position=through_position,
            )
            placed = placed or bool(found)
    return PermissionFacts(
        entities=known,
        predicate=predicate,
        locked=any(f.author_locked for f in locks),
        placed=placed,
        allow_new_arrivals=await arrival_policy(conn, story_id),
        protected=protected,
    )


# -- check and the rebase re-check ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Call:
    """A permission answer a turn's attempt rests on, to re-check at its rebase."""

    attempt: int
    story_id: str
    claim: Proposition
    asserter: Asserter
    through_position: tuple[int, int] | None
    protected: bool
    code: str


_CALLS: dict[int, tuple[weakref.ref[StructuredReadSet], list[_Call]]] = {}


def _remember(read_set: StructuredReadSet, call: _Call) -> None:
    key = id(read_set)
    entry = _CALLS.get(key)
    if entry is None or entry[0]() is not read_set:
        entry = (weakref.ref(read_set, lambda _: _CALLS.pop(key, None)), [])
        _CALLS[key] = entry
    entry[1].append(call)


def recorded(read_set: StructuredReadSet) -> list[_Call]:
    """The permission answers the read set's current attempt recorded."""
    entry = _CALLS.get(id(read_set))
    if entry is None or entry[0]() is not read_set:
        return []
    return [c for c in entry[1] if c.attempt == read_set.attempt]


async def check(
    conn: AsyncConnection,
    read_set: StructuredReadSet,
    story_id: str,
    claim: Proposition,
    asserter: Asserter,
    *,
    through_position: tuple[int, int] | None = None,
    protected: bool = False,
    registry: Registry | None = None,
    remember: bool = True,
) -> Permission | None:
    """``claim``'s permission for ``asserter`` over the story's structured data, its canon
    reads keyed in ``read_set``; ``None`` (inert, nothing read) when verification is off. The
    answer is recorded for the turn's rebase re-check unless ``remember`` is off."""
    if await options_for(conn, story_id) is None:
        return None
    facts = await facts_for(
        KeyedLookup(conn, read_set),
        story_id,
        claim,
        registry=registry or await load(conn, story_id),
        asserter=asserter,
        through_position=through_position,
        protected=protected,
    )
    found = permission(claim, asserter, owner_of(claim, facts), facts)
    if remember:
        _remember(
            read_set,
            _Call(
                read_set.attempt,
                story_id,
                claim,
                asserter,
                through_position,
                protected,
                found.code(),
            ),
        )
    return found


async def recheck(conn: AsyncConnection, op: Operation) -> bool:
    """The rebase re-check (``core.readset.REBASE_CHECKS``): every permission answer the
    turn's current attempt rests on, re-run against current state and configuration (the
    arrival policy, the locks) on a scratch read set; ``False`` (regenerate) when one changed
    or verification no longer runs."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    calls = recorded(source)
    for call in calls:
        again = await check(
            conn,
            StructuredReadSet(),
            call.story_id,
            call.claim,
            call.asserter,
            through_position=call.through_position,
            protected=call.protected,
            remember=False,
        )
        if again is None or again.code() != call.code:
            log.debug("rebase re-check: a permission changed (%d checked)", len(calls))
            return False
    return True


register_rebase_check(recheck)


__all__ = [
    "LOCKED_FIELDS",
    "PLACEMENT_PREDICATES",
    "PRECEDENCE",
    "Asserter",
    "AsserterKind",
    "Basis",
    "Denied",
    "EntityInfo",
    "OwnerKind",
    "Permission",
    "PermissionFacts",
    "Permitted",
    "Reason",
    "SubjectOwner",
    "arrival_policy",
    "character_infos",
    "check",
    "entity_infos",
    "facts_for",
    "owner_of",
    "permission",
    "recheck",
    "recorded",
]
