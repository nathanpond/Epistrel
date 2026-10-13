"""Helpers for the persona tests (#114): giving a player character a persona's snapshot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.catalog import personas


async def copy_into(engine: AsyncEngine, character_id: str, persona: dict[str, Any]) -> None:
    """Give a player character the persona ``persona`` (a read's body) the way #119's genesis
    will: its snapshot and id, and the character's name, true age and definition
    (``personas.player_character``). A direct update, until genesis exists (#103's FR-CAT-9
    pattern)."""
    copy = personas.snapshot(personas.Persona.model_validate(persona), at=datetime.now(UTC))
    values = personas.player_character(copy)
    async with engine.begin() as conn:
        updated = await conn.execute(
            text(
                "UPDATE characters SET name = :name, true_age = :true_age, "
                "definition = :definition, persona_id = :persona_id, "
                "persona_snapshot = CAST(:snapshot AS jsonb) WHERE id = :c AND is_player"
            ),
            {
                "c": character_id,
                "name": values["name"],
                "true_age": values["true_age"],
                "definition": values["definition"],
                "persona_id": values["persona_id"],
                "snapshot": json.dumps(values["persona_snapshot"]),
            },
        )
    assert updated.rowcount == 1, "the character must be a player character"


__all__ = ["copy_into"]
