"""The rules every scenario draft keeps (FR-SCN-3; #108), and dotted field paths.

``rule_errors`` is pure over a validated ``ScenarioDraft``: a save checks it on every create and
save (``core.catalog.scenarios``, 422 ``validation_error``), and the validation's document check
(``core.catalog.checks.document``, #111) checks it again on what is published. ``dotted`` turns a
location (``("cast", "roles", 0, "id")``) into the field path reports use
(``cast.roles[0].id``).

- Role ids are unique and none is reserved (``world`` and the audience levels).
- At most one role is the player's (zero is allowed), and it is ``open``.
- A ``locked`` or ``default`` role names its ``character`` and an ``open`` one names none; a
  role's ``character.version`` is a version or omitted (the latest published at story
  creation), never ``null`` — except on a companion (#116), whose locked role names ``null``
  before its character's first publish (the version it is pinned to: none yet).
- A relationship names another declared role, never its own; every starting location names a
  declared location, and every role a seed's holder, a reveal or the opening names is declared
  (a holder may also be ``world``); location, entity and seed ids are unique.
- A scenario holds at most 200 seeds.
"""

from __future__ import annotations

from typing import Any

from epistrel.core.catalog.scenario_models import (
    RESERVED_ROLE_IDS,
    SEEDS_MAX,
    WORLD_HOLDER,
    ScenarioDraft,
)

Loc = tuple[str | int, ...]


def dotted(loc: Loc) -> str:
    """``loc`` as a dotted field path, list indexes in brackets: ``cast.roles[0].id``."""
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path


def _error(loc: Loc, kind: str, message: str) -> dict[str, Any]:
    """One finding, in the shape of a pydantic error (``InvalidRequest.from_errors``)."""
    return {"loc": loc, "type": kind, "msg": message}


def _unknown_role(loc: Loc, role_id: str) -> dict[str, Any]:
    return _error(loc, "unknown_role", f"no role of this scenario is `{role_id}`")


def _unknown_location(loc: Loc, location_id: str) -> dict[str, Any]:
    return _error(loc, "unknown_location", f"no location of this scenario is `{location_id}`")


def rule_errors(draft: ScenarioDraft, *, companion: bool = False) -> list[dict[str, Any]]:
    """Every rule ``draft`` breaks (see the module docstring), each at its location in the
    document; ``[]`` when it keeps them all. A ``companion`` draft (#116) may name its locked
    character's ``version`` as ``null``: its pin before the character's first publish."""
    errors: list[dict[str, Any]] = []
    roles = draft.cast.roles
    role_ids = {role.id for role in roles}
    location_ids: set[str] = set()
    for index, location in enumerate(draft.world.locations):
        if location.id in location_ids:
            errors.append(
                _error(
                    ("world", "locations", index, "id"),
                    "location_id_duplicate",
                    f"another location is `{location.id}`; location ids are unique",
                )
            )
        location_ids.add(location.id)
    entity_ids: set[str] = set()
    for index, entity in enumerate(draft.world.entities):
        if entity.id in entity_ids:
            errors.append(
                _error(
                    ("world", "entities", index, "id"),
                    "entity_id_duplicate",
                    f"another entity is `{entity.id}`; entity ids are unique",
                )
            )
        entity_ids.add(entity.id)

    seen: set[str] = set()
    players = 0
    for index, role in enumerate(roles):
        at: Loc = ("cast", "roles", index)
        if role.id in RESERVED_ROLE_IDS:
            errors.append(
                _error(
                    (*at, "id"),
                    "role_id_reserved",
                    f"`{role.id}` is reserved: a seed's holder or audience uses it",
                )
            )
        elif role.id in seen:
            errors.append(
                _error(
                    (*at, "id"),
                    "role_id_duplicate",
                    f"another role is `{role.id}`; role ids are unique",
                )
            )
        seen.add(role.id)
        if role.kind == "player":
            players += 1
            if players > 1:
                errors.append(
                    _error((*at, "kind"), "player_role_limit", "a scenario has one player role")
                )
            if role.fill != "open":
                errors.append(
                    _error(
                        (*at, "fill"),
                        "player_role_fill",
                        "the player role is filled by the player's persona: its fill mode is "
                        "`open`",
                    )
                )
        if role.fill in ("locked", "default") and role.character is None:
            errors.append(
                _error(
                    (*at, "character"),
                    "role_character_required",
                    f"a `{role.fill}` role names the catalog character that fills it",
                )
            )
        if role.fill == "open" and role.character is not None:
            errors.append(
                _error(
                    (*at, "character"),
                    "role_character_not_allowed",
                    "an `open` role names no character: the story creator picks one",
                )
            )
        character = role.character
        if (
            not companion
            and character is not None
            and character.version is None
            and "version" in character.model_fields_set
        ):
            errors.append(
                _error(
                    (*at, "character", "version"),
                    "role_version_null",
                    "omit `version` to use the latest published version at story creation",
                )
            )
        for rel_index, relationship in enumerate(role.relationships):
            where: Loc = (*at, "relationships", rel_index, "to_role")
            if relationship.to_role == role.id:
                errors.append(
                    _error(where, "relationship_self", "a role has no relationship to itself")
                )
            elif relationship.to_role not in role_ids:
                errors.append(_unknown_role(where, relationship.to_role))
        if role.starting_location is not None and role.starting_location not in location_ids:
            errors.append(_unknown_location((*at, "starting_location"), role.starting_location))

    opening = draft.starting_state.opening
    if opening.starting_location is not None and opening.starting_location not in location_ids:
        errors.append(
            _unknown_location(
                ("starting_state", "opening", "starting_location"), opening.starting_location
            )
        )
    for index, role_id in enumerate(opening.roles_present):
        if role_id not in role_ids:
            errors.append(
                _unknown_role(("starting_state", "opening", "roles_present", index), role_id)
            )
    for index, reveal in enumerate(draft.drivers.planned_reveals):
        for role_index, role_id in enumerate(reveal.allowed_roles):
            if role_id not in role_ids:
                errors.append(
                    _unknown_role(
                        ("drivers", "planned_reveals", index, "allowed_roles", role_index), role_id
                    )
                )
    viewpoint = draft.story_settings_defaults.default_output_viewpoint
    if viewpoint is not None and viewpoint not in role_ids:
        errors.append(
            _unknown_role(("story_settings_defaults", "default_output_viewpoint"), viewpoint)
        )

    seed_ids: set[str] = set()
    for count, (loc, seed) in enumerate(draft.seeds(), start=1):
        if count == SEEDS_MAX + 1:
            errors.append(
                _error(
                    loc,
                    "too_many_seeds",
                    f"a scenario holds at most {SEEDS_MAX} seeds, all lists together",
                )
            )
        if seed.holder is not None and seed.holder != WORLD_HOLDER and seed.holder not in role_ids:
            errors.append(_unknown_role((*loc, "holder"), seed.holder))
        if seed.id is not None:
            if seed.id in seed_ids:
                errors.append(
                    _error(
                        (*loc, "id"),
                        "seed_id_duplicate",
                        f"another seed is `{seed.id}`; seed ids are unique",
                    )
                )
            seed_ids.add(seed.id)
    return errors


__all__ = ["Loc", "dotted", "rule_errors"]
