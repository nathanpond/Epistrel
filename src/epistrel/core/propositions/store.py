"""Writing entities, aliases, propositions and closed sets (FR-PROP-1,2,6,8; #131).

The one writer of ``entities``, ``entity_aliases``, ``propositions`` and ``closed_sets``. Each
write runs in the caller's transaction, passes the resurrection guard first (``guard.
check_content``: a text or an entity naming an erased subject is refused, 409
``erased_subject_referenced``) and indexes what it wrote (``index_records``), so erasure finds
it (FR-ADMIN-6,9).

- ``add_entity`` records an entity with its canonical name and aliases (#139's resolver is the
  caller).
- ``write_proposition`` stores a proposition in its predicate's canonical direction
  (``registry.canonical``) after checking it against the registry: a predicate the registry
  holds, declared qualifiers of the declared kinds, a set-valued object only where the
  predicate allows one. A negation is stored as such (``polarity = negated``) and takes part in
  #140's rules like any other proposition. Propositions are append-only.
- ``declare_closed_set`` stores an author's or canon's closed set (FR-PROP-8): the key is the
  subject plus exactly the predicate's key qualifiers. Only an assertion writes one (the
  author's at structured genesis, ``registry.register_author``; canon through #145's facts
  route): there is no write path from observation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure import guard
from epistrel.core.erasure.index import Record, index_records
from epistrel.core.erasure.records import (
    alias_record,
    closed_set_record,
    entity_record,
    proposition_record,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.codec import proposition_row
from epistrel.core.propositions.model import ENTITY_KINDS, NONE, Proposition
from epistrel.core.propositions.registry import Registry, canonical
from epistrel.core.tables import closed_sets, entities, entity_aliases, propositions

AssertedBy = Literal["author", "canon"]


async def _guarded(
    conn: AsyncConnection,
    story_id: str,
    records: Sequence[Record],
    *,
    references: Iterable[str] = (),
    base_revision: int | None,
    consumer: bool,
) -> None:
    contents = [
        guard.Content(
            tuple(v for v in r.fields.values() if v), frozenset(references), consumer=consumer
        )
        for r in records
    ]
    await guard.check_content(conn, story_id, contents, base_revision=base_revision, path=__name__)


async def add_entity(
    conn: AsyncConnection,
    story_id: str,
    kind: str,
    canonical_name: str,
    *,
    aliases: Sequence[str] = (),
    character_id: str | None = None,
    base_revision: int | None = None,
) -> str:
    """Record an entity of the story (its kind, canonical name, the character it is, if any)
    and its aliases; answers its id."""
    if kind not in ENTITY_KINDS:
        raise InvalidRequest(f"{kind!r} is not an entity kind", {"field": "kind"})
    entity_id = new_ulid()
    entity = SimpleNamespace(
        id=entity_id,
        story_id=story_id,
        kind=kind,
        canonical_name=canonical_name,
        character_id=character_id,
    )
    alias_rows = [
        SimpleNamespace(id=new_ulid(), story_id=story_id, entity_id=entity_id, alias=alias)
        for alias in dict.fromkeys(aliases)
    ]
    records = [entity_record(entity), *(alias_record(a) for a in alias_rows)]
    await _guarded(
        conn,
        story_id,
        records,
        references=() if character_id is None else (character_id,),
        base_revision=base_revision,
        consumer=False,
    )
    await conn.execute(insert(entities).values(**vars(entity)))
    if alias_rows:
        await conn.execute(insert(entity_aliases), [vars(a) for a in alias_rows])
    await index_records(conn, records)
    return entity_id


async def write_proposition(
    conn: AsyncConnection,
    story_id: str,
    proposition: Proposition,
    registry: Registry,
    *,
    base_revision: int | None = None,
    consumer: bool = False,
) -> str:
    """Store ``proposition`` (canonical direction, checked against ``registry``; see the
    module docstring); answers its id."""
    predicate = registry.get(proposition.predicate_id)
    if predicate is None:
        raise InvalidRequest(
            f"predicate {proposition.predicate_id} is not registered for the story",
            {"field": "predicate_id", "reason": "unregistered_predicate"},
        )
    problems = predicate.qualifier_errors(proposition)
    if problems:
        raise InvalidRequest(problems[0], {"field": "qualifiers", "reason": "qualifier"})
    has_object = proposition.object_key()[0] != "none"
    if (predicate.object_type == NONE) == has_object:
        raise InvalidRequest(
            f"{predicate.name} takes "
            + ("no object" if predicate.object_type == NONE else "an object"),
            {"field": "object", "reason": "object"},
        )
    if proposition.object_set is not None and not predicate.set_object:
        raise InvalidRequest(
            f"{predicate.name} takes no set-valued object",
            {"field": "object_set", "reason": "set_object"},
        )
    stored = canonical(proposition, registry)
    proposition_id = new_ulid()
    row = {"id": proposition_id, "story_id": story_id, **proposition_row(stored)}
    record = proposition_record(SimpleNamespace(**row))
    await _guarded(conn, story_id, [record], base_revision=base_revision, consumer=consumer)
    await conn.execute(insert(propositions).values(**row))
    await index_records(conn, [record])
    return proposition_id


@dataclass(frozen=True, slots=True)
class ClosedSet:
    """A closed set to declare: ``members`` are all the objects of ``predicate_id`` for the
    key (``subject_id`` and ``key``'s qualifier values) over ``[valid_from, valid_to)``."""

    predicate_id: str
    subject_id: str
    members: tuple[str, ...]
    key: Mapping[str, Any] = field(default_factory=dict)
    valid_from: int | None = None
    valid_to: int | None = None


async def declare_closed_set(
    conn: AsyncConnection,
    story_id: str,
    closed: ClosedSet,
    registry: Registry,
    *,
    asserted_by: AssertedBy,
    field_ref: str = "body",
) -> str:
    """Store an asserted closed set (see the module docstring); answers its id. 422 when its
    key's qualifiers are not exactly the predicate's key qualifiers, it lists no member, or its
    interval is empty."""
    predicate = registry.get(closed.predicate_id)
    if predicate is None:
        raise InvalidRequest(
            f"predicate {closed.predicate_id} is not registered for the story",
            {"field": f"{field_ref}.predicate", "reason": "unknown_predicate"},
        )
    if set(closed.key) != set(predicate.key_qualifiers):
        raise InvalidRequest(
            f"a closed set of {predicate.name} is keyed by its key qualifiers "
            f"{list(predicate.key_qualifiers)}",
            {"field": f"{field_ref}.key", "reason": "closed_set_key"},
        )
    if not closed.members:
        raise InvalidRequest("a closed set lists its members", {"field": f"{field_ref}.members"})
    if (
        closed.valid_from is not None
        and closed.valid_to is not None
        and closed.valid_from >= closed.valid_to
    ):
        raise InvalidRequest(
            "a closed set's interval needs valid_from < valid_to",
            {"field": f"{field_ref}.valid_from", "reason": "invalid_valid_time"},
        )
    closed_id = new_ulid()
    row = {
        "id": closed_id,
        "story_id": story_id,
        "predicate_id": closed.predicate_id,
        "key": {"subject_id": closed.subject_id, **dict(closed.key)},
        "members": sorted(set(closed.members)),
        "valid_from": closed.valid_from,
        "valid_to": closed.valid_to,
        "asserted_by": asserted_by,
    }
    record = closed_set_record(SimpleNamespace(**row))
    await _guarded(conn, story_id, [record], base_revision=None, consumer=False)
    await conn.execute(insert(closed_sets).values(**row))
    await index_records(conn, [record])
    return closed_id


__all__ = ["AssertedBy", "ClosedSet", "add_entity", "declare_closed_set", "write_proposition"]
