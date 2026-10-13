"""The player half of a story's creation from a scenario (FR-CAT-6, FR-SCN-6; #114, for #119).

A story created from a scenario names, for each player, the role they fill and the persona they
play as: ``players: {role_id: PlayerAssignment}``, where an assignment is the player's
``principal`` and their ``persona``, either a saved one (``{id}``) or an ad-hoc one
(``{ad_hoc: {name, gender?, age, description}}``, exactly a persona's create body without its
owner). A player plays a persona, never a catalog character, and the scenario fixes which role
the player fills (FR-CAT-6). #119's genesis applies two checks, each answering the creation
report's entries (``core.catalog.report``: outcome ``rejected``, so any one refuses the
creation with nothing written):

- ``player_role_entries`` (pure): ``player_role_fixed`` for a ``players`` key that names a role
  of kind ``npc``, and for a role of kind ``player`` that ``slots`` also fills (a catalog
  character or an ad-hoc definition on the player's role). A ``players`` key or ``slots`` key
  that names no role of the scenario is 422 ``validation_error`` at it.
- ``resolve_personas`` (reads, writes nothing): ``persona_required`` for a saved persona's
  ``id`` that is a catalog character's id; an id that is neither a persona's nor a catalog
  character's is 422 ``validation_error`` at ``body.players.<role>.persona.id``. It answers the
  saved personas it found, by role, for the snapshot (``core.catalog.personas.snapshot``).

Neither check consults an owner or a relation (FR-AUTH-8): any principal's persona may be
played, and a persona's owner need not be the player. The player's persona is never held to the
role's eligibility filters (FR-SCN-3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.personas import Persona, PersonaFields, persona_of
from epistrel.core.catalog.report import Entry, EntryLocation, Outcome, VersionLabel
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import Ulid
from epistrel.core.ownership import Principal
from epistrel.core.tables import character_catalog, personas

PERSONA_REQUIRED = "persona_required"
"""A player's persona names a catalog character: a player plays a persona (FR-CAT-6)."""
PLAYER_ROLE_FIXED = "player_role_fixed"
"""A player put in a role the scenario did not make the player's, or the player's role filled
as a character slot (FR-CAT-6: the scenario fixes which role the player fills)."""

PERSONA_RULE = "a player plays a persona, never a catalog character (FR-CAT-6)"
ROLE_RULE = "the scenario fixes which role the player fills (FR-CAT-6)"
PLAYER = "player"
"""The role kind a player fills (``ScenarioRole.kind``)."""


class PersonaRef(BaseModel):
    """A saved persona, by id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Ulid = Field(description="The saved persona's id.")


class AdHocPersona(BaseModel):
    """A persona given for this story only: a persona's fields, saved nowhere."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ad_hoc: PersonaFields


class PlayerAssignment(BaseModel):
    """One player of a new story: the player's ``principal`` (recorded as a ``player`` relation
    on the story, #119) and the ``persona`` they play as, saved or ad hoc."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"principal": "p-7f3a9c", "persona": {"id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"}},
                {
                    "principal": "p-7f3a9c",
                    "persona": {
                        "ad_hoc": {"name": "Sam", "age": 23, "description": "Quick to anger."}
                    },
                },
            ]
        },
    )

    principal: Principal = Field(description="The player's principal (opaque; never `system`).")
    persona: PersonaRef | AdHocPersona


def _rejected(
    code: str,
    scenario_id: str,
    version: VersionLabel,
    role: str,
    field: str,
    rule: str,
    *,
    source: str | None = None,
) -> Entry:
    return Entry(
        code=code,
        location=EntryLocation(entry=scenario_id, version=version, role=role, field=field),
        outcome=Outcome.rejected,
        source=source,
        rule=rule,
    )


def player_role_entries(
    scenario_id: str,
    version: VersionLabel,
    roles: Mapping[str, str],
    players: Iterable[str],
    slots: Iterable[str],
) -> list[Entry]:
    """The ``player_role_fixed`` entries of a creation that fills ``roles`` (role id → kind,
    ``npc`` or ``player``, in the scenario's order) with ``players`` and ``slots`` (the role ids
    each names): a player in an ``npc`` role, and a ``player`` role filled as a slot. 422
    ``validation_error`` for a key that names no role of the scenario."""
    named = list(players)
    filled = list(slots)
    for where, keys in (("players", named), ("slots", filled)):
        for key in keys:
            if key not in roles:
                raise InvalidRequest(
                    f"the scenario has no role {key!r}", {"field": f"body.{where}.{key}"}
                )
    entries: list[Entry] = []
    for role_id in named:
        if roles[role_id] != PLAYER:
            entries.append(
                _rejected(
                    PLAYER_ROLE_FIXED,
                    scenario_id,
                    version,
                    role_id,
                    f"players.{role_id}",
                    ROLE_RULE,
                )
            )
    for role_id in filled:
        if roles[role_id] == PLAYER:
            entries.append(
                _rejected(
                    PLAYER_ROLE_FIXED,
                    scenario_id,
                    version,
                    role_id,
                    f"slots.{role_id}",
                    ROLE_RULE,
                )
            )
    return entries


async def resolve_personas(
    conn: AsyncConnection,
    scenario_id: str,
    version: VersionLabel,
    players: Mapping[str, PlayerAssignment],
) -> tuple[dict[str, Persona], list[Entry]]:
    """The saved personas ``players`` name, by role, and a ``persona_required`` entry for each
    id that is a catalog character's; 422 ``validation_error`` at
    ``body.players.<role>.persona.id`` for an id that is neither. Ad-hoc personas need no
    lookup. Reads only, in ``conn``'s transaction."""
    wanted = {
        role_id: assignment.persona.id
        for role_id, assignment in players.items()
        if isinstance(assignment.persona, PersonaRef)
    }
    if not wanted:
        return {}, []
    ids = sorted(set(wanted.values()))
    saved = {
        str(row.id): row
        for row in await conn.execute(select(personas).where(personas.c.id.in_(ids)))
    }
    catalog = set(
        await conn.scalars(
            select(character_catalog.c.id).where(
                character_catalog.c.id.in_([i for i in ids if i not in saved])
            )
        )
    )
    found: dict[str, Persona] = {}
    entries: list[Entry] = []
    for role_id, persona_id in wanted.items():
        if persona_id in saved:
            found[role_id] = persona_of(saved[persona_id])
        elif persona_id in catalog:
            entries.append(
                _rejected(
                    PERSONA_REQUIRED,
                    scenario_id,
                    version,
                    role_id,
                    f"players.{role_id}.persona.id",
                    PERSONA_RULE,
                    source=persona_id,
                )
            )
        else:
            raise InvalidRequest(
                f"no persona has the id {persona_id}",
                {"field": f"body.players.{role_id}.persona.id"},
            )
    return found, entries


__all__ = [
    "PERSONA_REQUIRED",
    "PLAYER_ROLE_FIXED",
    "AdHocPersona",
    "PersonaRef",
    "PlayerAssignment",
    "player_role_entries",
    "resolve_personas",
]
