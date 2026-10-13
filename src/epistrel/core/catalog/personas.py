"""Player personas: saved per principal, snapshotted into stories (FR-CAT-6, FR-API-6, FR-AUTH-5;
docs/02 §4.10; #114).

A persona is what a player plays as: a ``name`` (1 to 120 characters), an optional ``gender``
(free text, up to 40), an ``age`` (0 to 150: the persona's true age, FR-CAT-12) and a short
``description`` (1 to 4,000: personality, and optionally appearance). Text fields are trimmed of
surrounding whitespace. A persona is **not** a catalog entry: it has no drafts or versions, no
listing status, and no catalog listing shows it. It lives in ``public.personas``.

- **Owner.** Each persona is owned by a principal (FR-AUTH-5): the ``owner`` its creation names
  (never ``system``), else the requesting principal. A name is unique per owner, ignoring case:
  a second one is 409 ``persona_name_taken``, on create, on a rename and on an owner transfer
  (``core.ownership``; the Engine maps it). As for every owned type, nothing gates on the owner
  or a relation (FR-AUTH-8): any principal reads, edits and deletes any persona.
- **Writes.** Create, edit and delete are each one transaction that records a committed,
  story-less ``control`` operation by the requesting principal and binds the call's
  ``Idempotency-Key`` to it (``core.control``; keys are per deployment). An edit is a JSON merge
  patch (RFC 7396) over the four fields, last write wins: a key sets that field, ``null`` removes
  it (only ``gender`` may be removed: the others are required, so their ``null`` is 422), and an
  edit that changes nothing records nothing. Delete is a hard delete: it removes the persona's
  relations with a ``deleted`` ownership event (``ownership.drop_entity``) and writes a
  ``persona_deleted`` ``audit_log`` row (identifiers only).
- **Reads.** One persona by id, or a list, newest first by ``(updated_at, id)``, a page at a
  time (1 to 200, default 50) with #110's opaque cursor bound to the filters (422
  ``cursor_invalid`` otherwise). ``owner`` and ``relation`` narrow the list only when a request
  names them: without them it lists every principal's personas (FR-AUTH-8).
- **Stories keep a snapshot.** A story created with a persona (#119's genesis) copies it into
  its player character's row (``snapshot``, ``player_character``): ``persona_snapshot``
  ``{persona_id, name, gender, age, description, snapshotted_at}`` and the plain
  ``persona_id``, and the character's ``name``, ``true_age`` and ``definition`` (the description,
  verbatim). Nothing here reads or writes a story, so editing or deleting a saved persona never
  changes one (FR-CAT-6).

The persona's text is story content: it is never logged (invariant 3), and the input gate checks
the description when a story is created with it (#119), not when it is saved.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, ValidationError
from sqlalchemy import delete as sql_delete
from sqlalchemy import insert, select, tuple_
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, control, ownership
from epistrel.core.catalog.search import Position, decode_cursor, encode_cursor
from epistrel.core.catalog.service import config_revision, merge_patch
from epistrel.core.errors import InvalidRequest, PersonaNameTaken, PersonaNotFound
from epistrel.core.ids import new_ulid
from epistrel.core.ownership import Owner, RelationFilter
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.ownership import EntityType
from epistrel.core.tables.personas import AGE_MAX, AGE_MIN, NAME_INDEX, personas

NAME_MAX = 120
GENDER_MAX = 40
DESCRIPTION_MAX = 4_000
DEFAULT_PAGE = 50
MAX_PAGE = 200
CURSOR_MAX = 256
"""The longest cursor a request may send (a list's are well under it)."""
REQUIRED = ("name", "age", "description")
"""The fields an edit cannot remove."""

PersonaName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)
]
Gender = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=GENDER_MAX)
]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=DESCRIPTION_MAX)
]

_EXAMPLE: dict[str, Any] = {
    "name": "Sam Ortiz",
    "gender": "man",
    "age": 23,
    "description": "Short-tempered and flies off the handle, but loyal to a fault. Tall, with a "
    "scar over one eyebrow.",
}


# -- requests ------------------------------------------------------------------------------------


class PersonaFields(BaseModel):
    """A persona's own fields (FR-CAT-6): the ``name``, an optional ``gender``, the ``age`` (its
    true age, FR-CAT-12) and a short ``description`` the player asserts they will play as. The
    body of an ad-hoc persona at story creation (#119) is exactly these fields."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [_EXAMPLE]}
    )

    name: PersonaName = Field(description="The player character's canonical name.")
    gender: Gender | None = Field(default=None, description="Free text (up to 40 characters).")
    age: StrictInt = Field(
        ge=AGE_MIN, le=AGE_MAX, description="The persona's true age (FR-CAT-12): 0 to 150."
    )
    description: Description = Field(
        description="Personality, and optionally appearance (up to 4,000 characters): what the "
        "player asserts they will play as. Never objective canon."
    )

    def fields(self) -> dict[str, Any]:
        """The four fields as stored (``gender`` ``None`` when not given)."""
        return {
            "name": self.name,
            "gender": self.gender,
            "age": self.age,
            "description": self.description,
        }


class PersonaCreate(PersonaFields):
    """``POST /personas``: a persona's fields and, optionally, its ``owner``: an opaque
    principal, by default the requesting one, never ``system`` (FR-AUTH-5). Ownership confers no
    permission (FR-AUTH-8)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{**_EXAMPLE, "owner": "p-7f3a9c"}]})

    owner: Owner | None = Field(
        default=None,
        description="The persona's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )


class PersonaPatch(BaseModel):
    """``PATCH /personas/{id}``: a JSON merge patch (RFC 7396) over the persona, last write wins.
    A key sets that field, and ``null`` removes it: only ``gender`` may be removed, so ``null``
    for another field is 422 at it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"age": 24, "description": "Calmer since the wedding."}]},
    )

    name: PersonaName | None = Field(default=None, description="Required: `null` is 422.")
    gender: Gender | None = Field(default=None, description="`null` removes it.")
    age: StrictInt | None = Field(
        default=None, ge=AGE_MIN, le=AGE_MAX, description="Required: `null` is 422."
    )
    description: Description | None = Field(default=None, description="Required: `null` is 422.")

    def document(self) -> dict[str, Any]:
        """The merge patch itself: the fields the request set, ``null`` included."""
        patch: dict[str, Any] = self.model_dump(mode="json", exclude_unset=True)
        return patch


# -- results -------------------------------------------------------------------------------------


class Persona(BaseModel):
    """A saved persona (FR-CAT-6): its id, its owner (FR-AUTH-5, recorded for listing,
    attribution and offboarding only), its fields, and when it was created and last changed (an
    edit or an owner transfer)."""

    id: str
    owner_principal: str
    name: str
    gender: str | None
    age: int
    description: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaWrite:
    """A created or edited persona and the deployment's ``config_revision``, which persona
    writes leave as it is. ``changed`` is false for an edit that changed nothing."""

    persona: Persona
    config_revision: int
    changed: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class PersonaPage:
    """A page of personas, newest first, and the cursor of the next page (``None``: the last)."""

    items: list[Persona] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class DeletedPersona:
    """A deleted persona."""

    id: str
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


class PersonaSnapshot(BaseModel):
    """A persona as a story keeps it (``characters.persona_snapshot``): the saved persona's id
    (``None`` for an ad-hoc one), its fields, and when the story took the copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_id: str | None
    name: str
    gender: str | None
    age: int
    description: str
    snapshotted_at: datetime


# -- rows ----------------------------------------------------------------------------------------


def persona_of(row: Any) -> Persona:
    """A ``personas`` row as a ``Persona``."""
    return Persona(
        id=str(row.id),
        owner_principal=str(row.owner_principal),
        name=str(row.name),
        gender=row.gender,
        age=int(row.age),
        description=str(row.description),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _not_found(persona_id: str) -> PersonaNotFound:
    return PersonaNotFound(f"persona {persona_id} does not exist", {"persona_id": persona_id})


async def _row(conn: AsyncConnection, persona_id: str, *, lock: bool = False) -> Any:
    query = select(personas).where(personas.c.id == persona_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _not_found(persona_id)
    return row


def is_name_conflict(exc: IntegrityError) -> bool:
    """Whether ``exc`` is the unique index on ``(owner_principal, lower(name))`` refusing a
    second persona of one name for one owner."""
    diagnostics = getattr(exc.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None) == NAME_INDEX


def name_taken(owner: str, *, field: str, persona_id: str | None = None) -> PersonaNameTaken:
    """409 ``persona_name_taken``: ``owner`` already has a persona of the name at ``field``."""
    details: dict[str, Any] = {"field": field, "owner_principal": owner}
    if persona_id is not None:
        details["persona_id"] = persona_id
    return PersonaNameTaken(
        "the owner already has a persona with this name (names are unique per owner, ignoring "
        "case)",
        details,
    )


@contextlib.contextmanager
def name_conflicts(owner: str, *, field: str, persona_id: str | None = None) -> Iterator[None]:
    """Turn the unique name index's refusal inside the block into 409 ``persona_name_taken``
    (at ``field``, for ``owner``); any other integrity error is raised as it is."""
    try:
        yield
    except IntegrityError as exc:
        if not is_name_conflict(exc):
            raise
        raise name_taken(owner, field=field, persona_id=persona_id) from None


# -- writes --------------------------------------------------------------------------------------


async def create(db: AsyncEngine, request: PersonaCreate, *, principal: str) -> PersonaWrite:
    """A new persona with ``request``'s fields, owned by ``request.owner`` or else
    ``principal``; 409 ``persona_name_taken`` when the owner has one of that name."""
    persona_id = new_ulid()
    owner = request.owner or principal
    now = clock.now()
    with name_conflicts(owner, field="body.name"):
        async with db.begin() as conn:
            await conn.execute(
                insert(personas).values(
                    id=persona_id,
                    owner_principal=owner,
                    **request.fields(),
                    created_at=now,
                    updated_at=now,
                )
            )
            await control.record_control_operation(conn, actor=principal)
            persona = persona_of(await _row(conn, persona_id))
            revision = await config_revision(conn)
    return PersonaWrite(persona=persona, config_revision=revision)


def _required_kept(patch: dict[str, Any]) -> None:
    """422 at ``body.<field>`` for a ``null`` that would remove a required field."""
    for name in REQUIRED:
        if name in patch and patch[name] is None:
            raise InvalidRequest(
                f"{name} is required: it can be changed, not removed",
                {"field": f"body.{name}"},
            )


def _merged(row: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """The persona's fields with ``patch`` applied, validated as ``PersonaFields``."""
    current = {key: value for key, value in persona_of(row).model_dump().items() if key in REQUIRED}
    if row.gender is not None:
        current["gender"] = row.gender
    try:
        return PersonaFields.model_validate(merge_patch(current, patch)).fields()
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None


async def update(
    db: AsyncEngine, persona_id: str, patch: PersonaPatch, *, principal: str
) -> PersonaWrite:
    """Apply ``patch`` (a JSON merge patch; last write wins): 404 for an unknown persona, 422
    for a required field's ``null``, 409 ``persona_name_taken`` for a name the owner already
    uses. An edit that changes nothing records nothing (``changed`` false)."""
    changes = patch.document()
    _required_kept(changes)
    owner: list[str] = []
    try:
        async with db.begin() as conn:
            row = await _row(conn, persona_id, lock=True)
            owner.append(str(row.owner_principal))
            fields = _merged(row, changes)
            if fields == {
                "name": row.name,
                "gender": row.gender,
                "age": int(row.age),
                "description": row.description,
            }:
                return PersonaWrite(
                    persona=persona_of(row),
                    config_revision=await config_revision(conn),
                    changed=False,
                )
            await conn.execute(
                sql_update(personas)
                .where(personas.c.id == persona_id)
                .values(**fields, updated_at=clock.now())
            )
            await control.record_control_operation(conn, actor=principal)
            persona = persona_of(await _row(conn, persona_id))
            revision = await config_revision(conn)
    except IntegrityError as exc:
        if not is_name_conflict(exc):
            raise
        raise name_taken(owner[0], field="body.name", persona_id=persona_id) from None
    return PersonaWrite(persona=persona, config_revision=revision)


async def delete(db: AsyncEngine, persona_id: str, *, principal: str) -> DeletedPersona:
    """Remove the persona (a hard delete) with its relations, recorded as a ``deleted``
    ownership event and a ``persona_deleted`` audit row; 404 for an unknown persona. Stories
    created with it keep their snapshot (FR-CAT-6)."""
    async with db.begin() as conn:
        await _row(conn, persona_id, lock=True)
        await ownership.drop_entity(conn, EntityType.PERSONA, persona_id, principal=principal)
        await conn.execute(sql_delete(personas).where(personas.c.id == persona_id))
        operation_id = await control.record_control_operation(conn, actor=principal)
        await audit.record(
            conn,
            AuditKind.PERSONA_DELETED,
            principal=principal,
            details={
                "entity_type": EntityType.PERSONA.value,
                "entity_id": persona_id,
                "operation_id": operation_id,
            },
        )
    return DeletedPersona(id=persona_id)


# -- reads ---------------------------------------------------------------------------------------


async def get(db: AsyncEngine, persona_id: str) -> Persona:
    """The persona; 404 for an unknown one."""
    async with db.connect() as conn:
        return persona_of(await _row(conn, persona_id))


def fingerprint(owner: str | None, relations: Sequence[RelationFilter]) -> str:
    """What a list's cursor binds: a hash of its filters (``limit`` aside)."""
    bound = {
        "entity_type": EntityType.PERSONA.value,
        "owner": owner,
        "relations": sorted([r.relation, r.principal or ""] for r in relations),
    }
    canonical = json.dumps(bound, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def list_personas(
    db: AsyncEngine,
    *,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> PersonaPage:
    """A page of personas, newest first: every principal's, unless ``owner`` or ``relations``
    narrow it (FR-AUTH-8). ``cursor`` (a page's ``next_cursor``) continues with the same
    filters: 422 ``cursor_invalid`` for one given for other filters."""
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidRequest(f"limit must be 1 to {MAX_PAGE}", {"field": "query.limit"})
    if cursor is not None and len(cursor) > CURSOR_MAX:
        raise InvalidRequest(
            f"cursor is at most {CURSOR_MAX} characters", {"field": "query.cursor"}
        )
    bound = fingerprint(owner, relations)
    where = ownership.list_filters(
        EntityType.PERSONA,
        personas.c.id,
        personas.c.owner_principal,
        owner=owner,
        relations=relations,
    )
    if cursor is not None:
        position = decode_cursor(cursor, bound, ranked=False)
        where.append(
            tuple_(personas.c.updated_at, personas.c.id) < tuple_(position.updated_at, position.id)
        )
    query = (
        select(personas)
        .where(*where)
        .order_by(personas.c.updated_at.desc(), personas.c.id.desc())
        .limit(limit + 1)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
    items = [persona_of(row) for row in rows[:limit]]
    following = None
    if len(rows) > limit and items:
        last = items[-1]
        following = encode_cursor(Position(None, last.updated_at, last.id), bound)
    return PersonaPage(items=items, next_cursor=following)


# -- what a story keeps (#119's genesis) ---------------------------------------------------------


def snapshot(
    persona: Persona | PersonaFields, *, at: datetime, persona_id: str | None = None
) -> PersonaSnapshot:
    """The copy of ``persona`` a story keeps: a saved persona with its id, an ad-hoc one
    (``PersonaFields``) with ``persona_id`` (``None`` unless given)."""
    found = persona.id if isinstance(persona, Persona) else persona_id
    return PersonaSnapshot(
        persona_id=found,
        name=persona.name,
        gender=persona.gender,
        age=persona.age,
        description=persona.description,
        snapshotted_at=at,
    )


def player_character(copy: PersonaSnapshot) -> dict[str, Any]:
    """The ``characters`` columns of a player character created with the persona ``copy``: its
    name, its true age (the persona's age), its definition (the description, verbatim), and the
    persona's id and snapshot."""
    return {
        "name": copy.name,
        "true_age": copy.age,
        "definition": copy.description,
        "is_player": True,
        "persona_id": copy.persona_id,
        "persona_snapshot": copy.model_dump(mode="json"),
    }


__all__ = [
    "CURSOR_MAX",
    "DEFAULT_PAGE",
    "DESCRIPTION_MAX",
    "GENDER_MAX",
    "MAX_PAGE",
    "NAME_MAX",
    "DeletedPersona",
    "Persona",
    "PersonaCreate",
    "PersonaFields",
    "PersonaPage",
    "PersonaPatch",
    "PersonaSnapshot",
    "PersonaWrite",
    "create",
    "delete",
    "fingerprint",
    "get",
    "is_name_conflict",
    "list_personas",
    "name_conflicts",
    "name_taken",
    "persona_of",
    "player_character",
    "snapshot",
    "update",
]
