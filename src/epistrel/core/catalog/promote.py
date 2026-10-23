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

- ``public_card.name``: the request's ``name``, else the character's;
- ``definition.true_age``: the character's true age as of the story's time, which in M3 (no
  world clock) is the age it was given;
- ``definition.background_flavor``: the character's free-text definition, which holds its
  appearance, personality, dispositions, likes and interaction style. Its typed fields
  (``personality`` and #115's others) are left empty for the author to fill before publishing.
  A definition longer than ``background_flavor`` holds (4,000 characters) is cut there, with the
  warning ``definition_truncated``. A character the story was created with from the catalog
  (#119) is promoted from its genesis snapshot instead: the snapshot's typed ``definition``
  (with the story's true age) and its public card's images;
- ``definition.safe_fallback_text`` and ``definition.deflection_style`` (FR-GUARD-7), when the
  character has them;
- the images, by reference (no bytes are copied): the first is ``public_card.images.primary``,
  the rest the gallery. An asset that is no longer live is left out, with the warning
  ``media_retired`` naming it;
- ``metadata.content_rating_id``: the request's rating, else the story's (422 ``rating_retired``
  when it cannot be assigned).

Nothing else of the story goes into the draft: no event, pinned memory, custom instruction,
summary, fact, or other character. The public description and the labels stay empty.

**Seeded memories (FR-CAT-8's second half, M4; #498).** ``seed_memories`` names memories of the
story (at most ``SEED_MEMORIES_MAX``, each once) whose text is seeded into the new character's
**companion scenario** draft, never into the character: each becomes one
``starting_state.seeded_memories`` item, in the order named, held by the companion's locked
``npc`` role with audience ``self`` and disclosure ``open`` (FR-SCN-8). Its ``valid_time`` is
left out, which FR-SCN-8 reads as before the story began, and genesis records it with provenance
``seed`` like any scenario seed. Each id must be an **active** memory of this story that is not
quarantined and that the character holds or can know: its ``holder_character_id`` is the
character, or it has no holder and its visibility is ``objective``, ``public`` or ``characters``
naming the character. Anything else is 422 ``seed_memory_invalid`` (``details {field, memory_id,
reason}``; ``SeedMemoryInvalid``), and nothing is written. The memory rows are read ``FOR
SHARE``, so an erasure that rewrites one waits for the promotion, or the promotion reads what
the erasure left. The copied texts pass the resurrection guard (``check_content``, a ``DIRECT``
write path) against the story's erasures. The companion draft keeps its save rules
(``scenarios.stored_document``) and size limit, stays at draft revision 1, and is re-indexed in
the subject index (catalog scope), so a catalog-scope erasure reaches what it holds. Publishing
or validating the companion runs the full scenario validation over the seeds, input policies
included (FR-SCN-5, FR-GUARD-10). Log lines carry counts and ids only (invariant 3).

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Text, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.catalog import scenarios
from epistrel.core.catalog.characters import (
    BACKGROUND_FLAVOR_MAX,
    GALLERY_MAX,
    SERVICE,
    Name,
)
from epistrel.core.catalog.checks.character_documents import images_of
from epistrel.core.catalog.companion import companion_of
from epistrel.core.catalog.companion_slots import NPC_ROLE
from epistrel.core.catalog.report import DRAFT, Entry, EntryLocation, Outcome
from epistrel.core.erasure import guard as erasure_guard
from epistrel.core.erasure.index import index_records
from epistrel.core.errors import (
    InvalidRequest,
    SeedMemoryInvalid,
    StoryCharacterNotFound,
    StoryNotFound,
)
from epistrel.core.ids import Ulid, new_ulid
from epistrel.core.membership import active_only
from epistrel.core.ownership import Owner
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import character_snapshots, characters, media_assets, stories
from epistrel.core.tables.catalog import scenario_catalog
from epistrel.core.tables.media import MediaState
from epistrel.core.tables.memories import memories
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

DEFINITION_TRUNCATED = "definition_truncated"
"""A warning: the character's definition was longer than ``background_flavor`` holds."""
IMAGE_LEFT_OUT = "media_retired"
"""A warning: one of the character's images is no longer live, so the draft leaves it out."""
PLAYER_CHARACTER = "player_character"
"""``details.reason`` of the 422 refusing a player character."""

SEED_MEMORIES_MAX = 50
"""The most memories one promotion seeds (#498): well under a scenario's ``SEEDS_MAX`` (200),
so an author can still add seeds of their own to the companion."""
SEED_AUDIENCE = ("self",)
"""A seeded memory's audience: the holder alone (FR-SCN-8's default for a memory)."""
SEED_DISCLOSURE = "open"
"""How the holder treats a seeded memory: shares it when relevant (FR-SCN-8)."""
SHARED_SCOPES = ("objective", "public")
"""The visibility scopes every character can know (``core.canon.facts.SHARED_SCOPES``)."""
NOT_IN_STORY = "not_in_story"
INACTIVE = "inactive"
QUARANTINED = "quarantined"
NOT_KNOWN = "not_known"
"""``details.reason`` of a 422 ``seed_memory_invalid``."""


class PromoteRequest(BaseModel):
    """``POST /stories/{id}/characters/{cid}/promote``: optionally the new entry's ``name``
    (default: the character's), its ``owner`` (default: the requesting principal; never
    ``system``), its ``content_rating_id`` (default: the story's rating) and the
    ``seed_memories`` to seed into its companion scenario (default: none)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {},
                {"name": "Dale the courier", "owner": "p-7f3a9c"},
                {"seed_memories": ["01J9Z3X4M8Q2V6R1T5Y7W0K3N9"]},
            ],
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
    seed_memories: list[Ulid] = Field(
        default_factory=list,
        max_length=SEED_MEMORIES_MAX,
        description="Memories of the story to seed into the new character's companion "
        "scenario draft (never into the character), in this order: each an active, "
        "unquarantined memory the character holds or can know. Each is seeded as "
        "`starting_state.seeded_memories` held by the `npc` role, audience `self`, disclosure "
        "`open`. Any other id is 422 `seed_memory_invalid` and nothing is written.",
    )

    @field_validator("seed_memories")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("names a memory more than once")
        return value


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


async def seeds_of(
    conn: AsyncConnection, story_id: str, character_id: str, memory_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """The companion seeds the memories ``memory_ids`` of the story make for the character
    ``character_id`` (see the module docstring), in order, with the rows held ``FOR SHARE``:
    422 ``seed_memory_invalid`` for the first id that cannot be seeded. The copied texts pass
    the resurrection guard against the story's erasures."""
    if not memory_ids:
        return []
    m = memories.c
    found = await conn.execute(
        select(
            m.id,
            m.text,
            m.holder_character_id,
            m.character_id,
            m.visibility,
            m.quarantined_at,
            active_only(memories).label("active"),
        )
        .where(m.story_id == story_id, m.id.in_(list(memory_ids)))
        .with_for_update(read=True)
    )
    rows = {str(r.id): r for r in found}
    texts: list[str] = []
    references: set[str] = set()
    for index, memory_id in enumerate(memory_ids):
        row = rows.get(memory_id)
        reason = None if row is None else _unseedable(row, character_id)
        if row is None or reason is not None:
            raise SeedMemoryInvalid(
                f"memory {memory_id} cannot be seeded into the companion of story character "
                f"{character_id}",
                {
                    "field": f"body.seed_memories[{index}]",
                    "memory_id": memory_id,
                    "reason": reason or NOT_IN_STORY,
                },
            )
        texts.append(str(row.text))
        references.update(str(i) for i in (row.holder_character_id, row.character_id) if i)
    await erasure_guard.check_content(
        conn,
        story_id,
        [erasure_guard.Content(tuple(texts), frozenset(references))],
        base_revision=None,
        path=__name__,
    )
    return [
        {
            "text": text,
            "holder": NPC_ROLE,
            "audience": list(SEED_AUDIENCE),
            "disclosure": SEED_DISCLOSURE,
        }
        for text in texts
    ]


def _unseedable(row: Any, character_id: str) -> str | None:
    """Why the memory ``row`` cannot be seeded for ``character_id`` (``None``: it can)."""
    if not row.active:
        return INACTIVE
    if row.quarantined_at is not None:
        return QUARANTINED
    if row.holder_character_id is not None:
        return None if str(row.holder_character_id) == character_id else NOT_KNOWN
    visibility = row.visibility if isinstance(row.visibility, Mapping) else {}
    scope = visibility.get("scope")
    if scope in SHARED_SCOPES:
        return None
    if scope == "characters" and character_id in (visibility.get("characters") or ()):
        return None
    return NOT_KNOWN


async def seed_companion(
    conn: AsyncConnection, catalog_id: str, seeds: Sequence[Mapping[str, Any]]
) -> None:
    """Seed ``seeds`` into the companion draft of the new catalog character ``catalog_id``
    (created with it in this transaction), as ``starting_state.seeded_memories``: kept by the
    companion's save rules and size limit, at its draft revision, and re-indexed."""
    if not seeds:
        return
    row = await companion_of(conn, catalog_id, lock=True)
    if row is None:  # pragma: no cover - create_in makes every character's companion
        raise RuntimeError(f"catalog character {catalog_id} was created without its companion")
    document = dict(row.draft)
    starting = dict(document.get("starting_state") or {})
    starting["seeded_memories"] = [*(starting.get("seeded_memories") or ()), *seeds]
    document["starting_state"] = starting
    draft = scenarios.stored_document(document, companion=True)
    scenarios.SERVICE.check_size(draft)
    await conn.execute(
        update(scenario_catalog)
        .where(scenario_catalog.c.id == row.id)
        .values(draft=draft, updated_at=clock.now())
    )
    await index_records(conn, [scenarios.draft_record(str(row.id), draft)])
    log.debug("companion seeded", catalog_id=catalog_id, scenario_id=str(row.id), seeds=len(seeds))


def draft_of(
    row: Any,
    request: PromoteRequest,
    rating_id: str,
    images: Sequence[str],
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """The draft document a promotion of the character ``row`` makes, and whether its
    definition had to be cut (see the module docstring). A character a story was created with
    from the catalog (``snapshot``: its ``character_snapshots.content``, #119) is promoted from
    its snapshot's typed definition, not from the plain text the story renders."""
    text = str(row.definition)
    typed = snapshot.get("definition") if snapshot is not None else None
    definition: dict[str, Any]
    if isinstance(typed, Mapping):
        definition = {k: v for k, v in typed.items() if v is not None}
        definition["true_age"] = int(row.true_age)
        text = ""
    else:
        definition = {
            "true_age": int(row.true_age),
            "background_flavor": text[:BACKGROUND_FLAVOR_MAX],
        }
    if row.safe_fallback_text:
        definition["safe_fallback_text"] = row.safe_fallback_text
    if row.deflection_style:
        definition["deflection_style"] = row.deflection_style
    public_card: dict[str, Any] = {"name": request.name or str(row.name)}
    if images:
        public_card["images"] = {
            "primary": images[0],
            "gallery": list(images[1 : 1 + GALLERY_MAX]),
        }
    document: dict[str, Any] = {
        "public_card": public_card,
        "definition": definition,
        "metadata": {"content_rating_id": rating_id},
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
        seeds = await seeds_of(conn, story_id, character_id, request.seed_memories)
        snapshot = await conn.scalar(
            select(character_snapshots.c.content).where(
                character_snapshots.c.story_id == story_id,
                character_snapshots.c.character_id == character_id,
            )
        )
        shown_images = list(row.images or [])
        if isinstance(snapshot, Mapping):
            shown_images = [media_id for _, media_id in images_of(snapshot)]
        live, left_out = await _live_images(conn, shown_images)
        document, cut = draft_of(
            row, request, rating_id, live, snapshot if isinstance(snapshot, Mapping) else None
        )
        shown, _ = await SERVICE.create_in(
            conn,
            document,
            owner=request.owner or principal,
            principal=principal,
            entry_id=catalog_id,
        )
        await seed_companion(conn, catalog_id, seeds)
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
    "SEED_MEMORIES_MAX",
    "PromoteRequest",
    "Promotion",
    "draft_of",
    "promote",
    "seed_companion",
    "seeds_of",
]
