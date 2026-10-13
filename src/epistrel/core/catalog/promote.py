"""Promoting a story character to a new draft catalog entry (FR-CAT-8, FR-CAT-7, FR-API-6;
#114).

``POST /stories/{story_id}/characters/{character_id}/promote`` makes a **draft** catalog
character from one character of a story: an ordinary draft, made by the character catalog's own
create (``CharacterCatalog.create_in``), owned by the request's ``owner`` or else the requesting
principal (FR-AUTH-5), at the deployment's first listing status, never published. Any character
of the story may be promoted except a player character, which plays a persona (FR-CAT-6: 422
``validation_error``, ``reason: player_character``). Promoting a character again makes another
draft: nothing is deduplicated.

**Only definition fields are copied** (FR-CAT-8), into the paths #115 gives them:

- the name: the request's ``name``, else the character's;
- ``definition.true_age``: the character's true age as of the story's time, which in M3 (no
  world clock) is the age it was given;
- ``definition.background_flavor``: the character's free-text definition, which holds its
  appearance, personality, dispositions, likes and interaction style. Its typed fields
  (``personality`` and #115's others) are left empty for the author to fill before publishing.
  A definition longer than ``background_flavor`` holds (4,000 characters) is cut there, with the
  warning ``definition_truncated``;
- ``definition.safe_fallback_text``, when the character has one;
- the images, by reference (no bytes are copied): the first is ``public_card.images.primary``,
  the rest the gallery. An asset that is no longer live is left out, with the warning
  ``media_retired`` naming it;
- ``metadata.content_rating_id``: the request's rating, else the story's (422 ``rating_retired``
  when it cannot be assigned).

Nothing else of the story goes into the draft: no event, pinned memory, custom instruction,
summary, fact, or other character. The public description and the labels stay empty. Selecting
memories to seed into the new character's companion scenario is M4's (no ``seed_memories``
field yet).

**One transaction.** The character's row is locked ``FOR NO KEY UPDATE`` (so the story cannot be
deleted under it), the images ``FOR SHARE``; the entry is inserted with its committed,
story-less ``control`` operation, which the call's ``Idempotency-Key`` (deployment-scoped) is
bound to; and the new entry's id is appended to the character's ``promoted_to``, a plain
reference and the only change to the story character. No story event is written and the
story's revision does not move: promotion only reads the story, so it is allowed on an archived
story and while a turn is in flight. The answer carries the new ``catalog_id``, the draft's
revision, the warnings (#111's ``Entry`` shape) and the story's unchanged ``timeline_revision``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog.characters import (
    BACKGROUND_FLAVOR_MAX,
    GALLERY_MAX,
    SERVICE,
    Name,
)
from epistrel.core.catalog.report import DRAFT, Entry, EntryLocation, Outcome
from epistrel.core.errors import InvalidRequest, StoryCharacterNotFound, StoryNotFound
from epistrel.core.ids import Ulid, new_ulid
from epistrel.core.ownership import Owner
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import characters, media_assets, stories
from epistrel.core.tables.media import MediaState

DEFINITION_TRUNCATED = "definition_truncated"
"""A warning: the character's definition was longer than ``background_flavor`` holds."""
IMAGE_LEFT_OUT = "media_retired"
"""A warning: one of the character's images is no longer live, so the draft leaves it out."""
PLAYER_CHARACTER = "player_character"
"""``details.reason`` of the 422 refusing a player character."""


class PromoteRequest(BaseModel):
    """``POST /stories/{id}/characters/{cid}/promote``: optionally the new entry's ``name``
    (default: the character's), its ``owner`` (default: the requesting principal; never
    ``system``) and its ``content_rating_id`` (default: the story's rating)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{}, {"name": "Dale the courier", "owner": "p-7f3a9c"}],
        },
    )

    name: Name | None = Field(
        default=None, description="The entry's name (default: the character's)."
    )
    owner: Owner | None = Field(
        default=None,
        description="The entry's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )
    content_rating_id: Ulid | None = Field(
        default=None, description="The entry's content rating (default: the story's)."
    )


@dataclass(frozen=True, slots=True)
class Promotion:
    """A promotion's outcome: the new draft entry, its draft revision (1), the warnings, and the
    story's ``timeline_revision``, which promotion leaves as it is."""

    catalog_id: str
    draft_revision: int
    warnings: tuple[Entry, ...]
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


def _warning(code: str, catalog_id: str, field: str, rule: str, source: str | None) -> Entry:
    return Entry(
        code=code,
        location=EntryLocation(entry=catalog_id, version=DRAFT, field=field),
        outcome=Outcome.dropped if code == IMAGE_LEFT_OUT else Outcome.kept,
        source=source,
        rule=rule,
    )


async def _live_images(conn: AsyncConnection, images: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(live, left out)``: the character's images that are ``live`` (held ``FOR SHARE`` until
    the promotion commits) and those that are not, each in order."""
    wanted = [str(media_id) for media_id in images]
    if not wanted:
        return [], []
    rows = await conn.execute(
        select(media_assets.c.id, media_assets.c.state)
        .where(media_assets.c.id.in_(wanted))
        .with_for_update(read=True)
    )
    states = {str(row.id): str(row.state) for row in rows}
    live = [m for m in wanted if states.get(m) == MediaState.LIVE.value]
    return live, [m for m in wanted if states.get(m) != MediaState.LIVE.value]


def draft_of(
    row: Any, request: PromoteRequest, rating_id: str, images: Sequence[str]
) -> tuple[dict[str, Any], bool]:
    """The draft document a promotion of the character ``row`` makes, and whether its
    definition had to be cut (see the module docstring)."""
    text = str(row.definition)
    definition: dict[str, Any] = {
        "true_age": int(row.true_age),
        "background_flavor": text[:BACKGROUND_FLAVOR_MAX],
    }
    if row.safe_fallback_text:
        definition["safe_fallback_text"] = row.safe_fallback_text
    document: dict[str, Any] = {
        "name": request.name or str(row.name),
        "definition": definition,
        "metadata": {"content_rating_id": rating_id},
    }
    if images:
        document["public_card"] = {
            "images": {"primary": images[0], "gallery": list(images[1 : 1 + GALLERY_MAX])}
        }
    return document, len(text) > BACKGROUND_FLAVOR_MAX


async def promote(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    request: PromoteRequest,
    *,
    principal: str,
) -> Promotion:
    """Make a new draft catalog entry from the story's character (see the module docstring):
    404 for an unknown story or a character not in it, 422 for a player character or a rating
    that cannot be assigned."""
    catalog_id = new_ulid()
    async with db.begin() as conn:
        story = (
            await conn.execute(
                select(stories.c.content_rating_id, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one_or_none()
        if story is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        row = (
            await conn.execute(
                select(characters)
                .where(characters.c.id == character_id, characters.c.story_id == story_id)
                .with_for_update(key_share=True)
            )
        ).one_or_none()
        if row is None:
            raise StoryCharacterNotFound(
                f"story {story_id} has no character {character_id}",
                {"story_id": story_id, "character_id": character_id},
            )
        if row.is_player:
            raise InvalidRequest(
                "a player character plays a persona and cannot be promoted to the catalog",
                {"field": "path.character_id", "reason": PLAYER_CHARACTER},
            )
        rating_id = request.content_rating_id or str(story.content_rating_id)
        await require_assignable(conn, rating_id, field="body.content_rating_id")
        live, left_out = await _live_images(conn, row.images or [])
        document, cut = draft_of(row, request, rating_id, live)
        shown, _ = await SERVICE.create_in(
            conn,
            document,
            owner=request.owner or principal,
            principal=principal,
            entry_id=catalog_id,
        )
        await conn.execute(
            update(characters)
            .where(characters.c.id == character_id)
            .values(
                promoted_to=characters.c.promoted_to.op("||", return_type=JSONB)(
                    func.jsonb_build_array(cast(literal(catalog_id), Text))
                )
            )
        )
    warnings: list[Entry] = []
    if cut:
        warnings.append(
            _warning(
                DEFINITION_TRUNCATED,
                catalog_id,
                "definition.background_flavor",
                f"background_flavor holds at most {BACKGROUND_FLAVOR_MAX} characters; the rest of "
                "the definition was left out",
                None,
            )
        )
    warnings.extend(
        _warning(
            IMAGE_LEFT_OUT,
            catalog_id,
            "public_card.images",
            "an image the draft shows must be live; a retired one is left out (FR-MEDIA-2)",
            media_id,
        )
        for media_id in left_out
    )
    return Promotion(
        catalog_id=catalog_id,
        draft_revision=int(shown.draft_revision),
        warnings=tuple(warnings),
        timeline_revision=int(story.timeline_revision),
    )


__all__ = [
    "DEFINITION_TRUNCATED",
    "IMAGE_LEFT_OUT",
    "PLAYER_CHARACTER",
    "PromoteRequest",
    "Promotion",
    "draft_of",
    "promote",
]
