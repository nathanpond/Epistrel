"""Writing a story's settings: ``PATCH /stories/{id}/settings`` (FR-CONC-5,15; #63).

The body is a JSON merge patch (RFC 7396) over ``StorySettings``: a key present changes that
setting, ``null`` resets it to its default, a nested object (``generation_settings``) is merged
the same way, and a key ``StorySettings`` does not define is 422 ``validation_error``. The
default output viewpoint is checked against the cast (``validate_default_viewpoint``: never
``omniscient``, 422 ``viewpoint_unresolved`` for anyone outside the cast) and stored as
``character:<id>``.

A write that changes anything moves the story's ``config_revision`` by one and records one
``config_changes`` row per changed setting (scope ``story``, with the acting principal) through
#48's ``record_config_change``, in one transaction under the story row lock; a write that
changes nothing writes nothing. Every setting is prospective here (#64 tags each one): it
applies to requests that start after the write, never to a turn already running. An archived
story's settings are read-only (409 ``story_archived``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import ChangeClass, ConfigScope, record_config_change
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidRequest, StoryArchived, StoryNotFound
from epistrel.core.settings_schema import COLUMN_SETTINGS, StorySettings
from epistrel.core.stories import settings_of, split_settings
from epistrel.core.tables import characters, stories
from epistrel.core.viewpoint import validate_default_viewpoint

VIEWPOINT = "default_output_viewpoint"


@dataclass(frozen=True, slots=True)
class StorySettingsWrite:
    """The story's settings after the write (defaults filled in), its configuration and
    timeline revisions, and whether anything changed."""

    settings: dict[str, Any]
    config_revision: int
    timeline_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


def merge_patch(target: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """RFC 7396: ``null`` removes a key, an object merges into an object, anything else
    replaces."""
    merged = dict(target)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping):
            current = merged.get(key)
            merged[key] = merge_patch(current if isinstance(current, Mapping) else {}, value)
        else:
            merged[key] = value
    return merged


def check_keys(patch: Mapping[str, Any]) -> None:
    """422 for a key ``StorySettings`` does not define (the patch would be silently kept)."""
    for key in patch:
        if key not in StorySettings.model_fields:
            raise InvalidRequest(
                f"{key!r} is not a story setting", {"field": f"body.{key}", "reason": "unknown"}
            )


def _validated(document: Mapping[str, Any]) -> StorySettings:
    try:
        return StorySettings.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise InvalidRequest(
            str(first["msg"]), {"field": f"body.{location}", "errors": exc.error_count()}
        ) from None


def _dump(settings: StorySettings) -> dict[str, Any]:
    return {
        name: value
        for name, value in settings.model_dump(mode="json").items()
        if name in StorySettings.model_fields
    }


async def update_story_settings(
    db: AsyncEngine, story_id: str, patch: Mapping[str, Any], *, principal: str
) -> StorySettingsWrite:
    """Apply ``patch`` to the story's settings; see the module docstring."""
    check_keys(patch)
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(
                    stories.c.settings,
                    stories.c.turn_concurrency,
                    stories.c.turn_queue_max,
                    stories.c.config_revision,
                    stories.c.timeline_revision,
                    stories.c.status,
                )
                .where(stories.c.id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        current_document = settings_of(row)
        current = _dump(_validated(current_document))
        wanted_document = merge_patch(current_document, patch)
        if patch.get(VIEWPOINT) is not None:
            cast = await conn.execute(
                select(characters.c.id).where(characters.c.story_id == story_id)
            )
            wanted_document[VIEWPOINT] = validate_default_viewpoint(
                patch[VIEWPOINT], {r.id for r in cast}, field=f"body.{VIEWPOINT}"
            )
        wanted = _dump(_validated(wanted_document))
        changed = {k for k in wanted if wanted[k] != current.get(k)}
        config_revision = int(row.config_revision)
        if not changed:
            return StorySettingsWrite(
                current, config_revision, int(row.timeline_revision), changed=False
            )
        if row.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        config_revision += 1
        kept = {k: v for k, v in dict(row.settings or {}).items() if k not in wanted}
        document, columns = split_settings(wanted)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(
                settings={**kept, **document},
                config_revision=config_revision,
                **{name: columns.get(name) for name in COLUMN_SETTINGS},
            )
        )
        for name in sorted(changed):
            await record_config_change(
                conn,
                config_revision=config_revision,
                setting=name,
                old=current.get(name),
                new=wanted[name],
                actor=principal,
                change_class=ChangeClass.PROSPECTIVE,
                scope=ConfigScope.STORY,
                scope_id=story_id,
            )
    return StorySettingsWrite(wanted, config_revision, int(row.timeline_revision), changed=True)


__all__ = ["StorySettingsWrite", "check_keys", "merge_patch", "update_story_settings"]
