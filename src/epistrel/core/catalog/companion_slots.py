"""A companion scenario's two slots and its defaults (FR-CAT-5, FR-CAT-10; #116). Pure: no
database, no service. ``core.catalog.companion`` creates and publishes companions with them, the
scenario save keeps the slots fixed with them (``core.catalog.scenarios``), and the validation's
companion check (``core.catalog.checks.companion``) checks them again.

**The slots.** A companion's cast is exactly two roles: ``npc``, locked to the character it is
the companion of, and ``player``, the player's open slot. Their slot fields — ``id``, ``kind``,
``fill`` and ``character`` — never change; every other role field (labels, setup descriptions,
seeds, relationships, participation, …) is editable like any scenario's. ``slot_change`` finds
the first change a draft makes to them, matching roles by id (reordering the roles is not a
change), on raw documents, so a save refuses it before any other rule is checked.

**The pin.** The ``npc`` role's ``character.version`` is the character version the companion is
pinned to: the character's latest published version, moved by every character publish
(``repinned``); before the character's first publish it has none (``pin_of`` is ``None``, a
``character`` without a ``version``). A companion version records it twice: in its content and
in ``scenario_versions.pinned_character_id``/``pinned_character_version`` (a foreign key).

**The defaults** (``defaults``): the character's name (``public_card.name`` and the ``npc``
role's label), no public description or labels, the character's content rating, an empty world,
no opening, ``max_characters_at_start`` 2 and no story settings defaults; the ``npc`` role
required, ``participation`` ``primary``, and the ``player`` role labelled "You". A companion is
created with them, and a character's first publish copies them into the companion's first
version when it publishes the character alone (FR-CAT-10: "its two slots and an empty world").
The setup descriptions are short texts, since every role needs one (#108).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final

from epistrel.core.catalog.scenario_models import ScenarioDraft

NPC_ROLE: Final = "npc"
"""The companion's role locked to its character."""
PLAYER_ROLE: Final = "player"
"""The companion's player slot."""
SLOT_FIELDS: Final = ("kind", "fill", "character")
"""What a slot is, besides its ``id``: frozen on a companion."""
DEFAULT_CAST_LIMIT: Final = 2
"""A companion's ``max_characters_at_start``: its two slots."""
UNNAMED_LABEL: Final = "Companion"
"""The ``npc`` role's label when the character has no name yet (a draft may lack one)."""
PLAYER_LABEL: Final = "You"
PLAYER_SETUP: Final = "You."


def _npc_setup(name: str | None) -> str:
    return f"Just talk with {name}." if name else "Just talk."


def defaults(
    character_id: str, *, name: str | None, rating_id: str | None, pin: int | None
) -> dict[str, Any]:
    """The companion of ``character_id``'s default document, stored form (see the module
    docstring): named ``name``, rated ``rating_id``, pinned to ``pin`` (``None``: no version
    yet)."""
    character: dict[str, Any] = {"catalog_id": character_id}
    if pin is not None:
        character["version"] = pin
    document: dict[str, Any] = {
        "public_card": {"name": name} if name else {},
        "metadata": {"content_rating_id": rating_id} if rating_id else {},
        "cast": {
            "roles": [
                {
                    "id": NPC_ROLE,
                    "public_label": name or UNNAMED_LABEL,
                    "setup_description": _npc_setup(name),
                    "kind": "npc",
                    "required": True,
                    "fill": "locked",
                    "character": character,
                    "participation": "primary",
                },
                {
                    "id": PLAYER_ROLE,
                    "public_label": PLAYER_LABEL,
                    "setup_description": PLAYER_SETUP,
                    "kind": "player",
                    "required": True,
                    "fill": "open",
                },
            ],
            "max_characters_at_start": DEFAULT_CAST_LIMIT,
        },
    }
    return ScenarioDraft.model_validate(document).document()


def _roles(document: Any) -> list[Any]:
    """The roles of a raw document (``[]`` when it has none, or they are not a list)."""
    cast = document.get("cast") if isinstance(document, Mapping) else None
    roles = cast.get("roles") if isinstance(cast, Mapping) else None
    return roles if isinstance(roles, list) else []


def _slot(role: Mapping[str, Any], name: str) -> Any:
    """A slot field of a raw role, as stored: ``fill`` defaults to ``open``; a ``character`` is
    its catalog id and version (a missing version and ``null`` are the same: none yet)."""
    value = role.get(name)
    if name == "fill" and value is None:
        return "open"
    if name == "character" and isinstance(value, Mapping):
        return value.get("catalog_id"), value.get("version")
    return value


def slot_change(stored: Mapping[str, Any], merged: Any) -> tuple[str | None, str] | None:
    """The first change ``merged`` makes to the slots of the companion draft ``stored``, as
    ``(role id, field path)`` (``cast.roles[1].fill``; ``cast.roles`` for a role removed;
    ``cast.roles[2].id`` for one added, renamed or repeated); ``None`` when the slots are kept.
    Roles are matched by id, so their order is free."""
    before = {
        role["id"]: role
        for role in _roles(stored)
        if isinstance(role, Mapping) and isinstance(role.get("id"), str)
    }
    seen: set[str] = set()
    for index, role in enumerate(_roles(merged)):
        role_id = role.get("id") if isinstance(role, Mapping) else None
        if not isinstance(role, Mapping) or not isinstance(role_id, str):
            return None, f"cast.roles[{index}].id"
        if role_id not in before or role_id in seen:
            return role_id, f"cast.roles[{index}].id"
        seen.add(role_id)
        for name in SLOT_FIELDS:
            if _slot(role, name) != _slot(before[role_id], name):
                return role_id, f"cast.roles[{index}].{name}"
    for role_id in before:
        if role_id not in seen:
            return role_id, "cast.roles"
    return None


def pin_of(document: Mapping[str, Any]) -> int | None:
    """The character version a companion document is pinned to (``None``: none yet)."""
    for role in _roles(document):
        if isinstance(role, Mapping) and role.get("id") == NPC_ROLE:
            character = role.get("character")
            version = character.get("version") if isinstance(character, Mapping) else None
            return version if isinstance(version, int) else None
    return None


def pinned_columns(character_id: str, pin: int | None) -> dict[str, Any]:
    """A companion version's pin columns: ``pinned_character_id`` and
    ``pinned_character_version``, both set or both null (``ck_scenario_versions_pin_complete``)."""
    return {
        "pinned_character_id": None if pin is None else character_id,
        "pinned_character_version": pin,
    }


def repinned(document: Mapping[str, Any], pin: int | None) -> dict[str, Any]:
    """A copy of the companion ``document`` pinned to ``pin`` (``None``: no version); the
    document itself is not changed."""
    copied: dict[str, Any] = copy.deepcopy(dict(document))
    for role in _roles(copied):
        if isinstance(role, dict) and role.get("id") == NPC_ROLE:
            character = role.get("character")
            if isinstance(character, dict):
                if pin is None:
                    character.pop("version", None)
                else:
                    character["version"] = pin
    return copied


__all__ = [
    "DEFAULT_CAST_LIMIT",
    "NPC_ROLE",
    "PLAYER_LABEL",
    "PLAYER_ROLE",
    "PLAYER_SETUP",
    "SLOT_FIELDS",
    "UNNAMED_LABEL",
    "defaults",
    "pin_of",
    "pinned_columns",
    "repinned",
    "slot_change",
]
