"""A story's structured current state over REST (FR-STORE-3, FR-RET-6; #137).

- ``GET /stories/{id}/scene?as_of=&viewpoint=`` → the scene as of a turn: the player
  character's location and the entities present there.
- ``GET /stories/{id}/entities/{entity_id}/profile?as_of=&viewpoint=`` → an entity's standing
  profile: its relationships in both directions, where it is, and what it holds.

Both read the structured projections (``epistrel.core.projections.structured``) for one
viewpoint: ``viewpoint`` when named, else the story's default output viewpoint (422
``viewpoint_unresolved`` when none resolves). A character viewpoint sees only what the facts
behind the rows let it read; ``source_fact_id`` is returned to ``omniscient`` only. Thin
handlers over ``Engine``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.projections.structured import (
    EntityRef,
    InventoryItem,
    PlacementView,
    ProfileView,
    RelationshipView,
    SceneView,
)

router = APIRouter(prefix="/stories", tags=["stories"])

EntityId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The entity's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"],
    ),
]

READ_RULES = (
    " `as_of` is a turn from 0 to the head (default the head; 422 otherwise); `viewpoint` is "
    "`omniscient` or `character:<id>` of the cast (default the story's default output "
    "viewpoint; 422 `viewpoint_unresolved` when none resolves). In M4 the story-time of every "
    "turn is the story's start (the world clock arrives in M6)."
)


class EntityBody(BaseModel):
    """An entity of the story: its id, kind and canonical name."""

    entity_id: str
    kind: str
    name: str

    @classmethod
    def of(cls, ref: EntityRef) -> EntityBody:
        return cls(entity_id=ref.entity_id, kind=ref.kind, name=ref.name)


class SceneBody(BaseModel):
    """The scene as of a turn for one viewpoint: the turn and its active variant (``null`` at
    turn 0), the player character's location (``null`` when unknown to the viewpoint), and the
    entities present there, characters and objects, by id."""

    turn: int
    variant: int | None
    location: EntityBody | None
    present: list[EntityBody]
    viewpoint: str

    @classmethod
    def of(cls, found: SceneView) -> SceneBody:
        return cls(
            turn=found.turn,
            variant=found.variant,
            location=None if found.location is None else EntityBody.of(found.location),
            present=[EntityBody.of(e) for e in found.present],
            viewpoint=found.viewpoint,
        )


class RelationshipBody(BaseModel):
    """One relationship of the entity: the predicate, ``direction`` (``out`` when the entity is
    the subject, or the predicate is symmetric; ``in`` otherwise), the other entity, the key
    qualifiers, the valid-time bounds (story-minutes, ``null`` open) and, for ``omniscient``,
    the canon fact behind it."""

    predicate_id: str
    predicate: str
    direction: str
    other: EntityBody
    qualifiers: dict[str, Any]
    valid_from: int | None
    valid_to: int | None
    source_fact_id: str | None

    @classmethod
    def of(cls, found: RelationshipView) -> RelationshipBody:
        return cls(
            predicate_id=found.predicate_id,
            predicate=found.predicate,
            direction=found.direction,
            other=EntityBody.of(found.other),
            qualifiers=dict(found.qualifiers),
            valid_from=found.valid_from,
            valid_to=found.valid_to,
            source_fact_id=found.source_fact_id,
        )


class PlacementBody(BaseModel):
    """Where the entity is: at a ``location`` (with the places that contain it, nearest
    first, ``within``) or in a ``container`` (the entity holding it); the valid-time bounds and,
    for ``omniscient``, the canon fact behind it."""

    location: EntityBody | None
    container: EntityBody | None
    within: list[EntityBody]
    valid_from: int | None
    valid_to: int | None
    source_fact_id: str | None

    @classmethod
    def of(cls, found: PlacementView) -> PlacementBody:
        return cls(
            location=None if found.location is None else EntityBody.of(found.location),
            container=None if found.container is None else EntityBody.of(found.container),
            within=[EntityBody.of(e) for e in found.within],
            valid_from=found.valid_from,
            valid_to=found.valid_to,
            source_fact_id=found.source_fact_id,
        )


class InventoryBody(BaseModel):
    """An item the entity holds and, for ``omniscient``, the canon fact behind it."""

    item: EntityBody
    source_fact_id: str | None

    @classmethod
    def of(cls, found: InventoryItem) -> InventoryBody:
        return cls(item=EntityBody.of(found.item), source_fact_id=found.source_fact_id)


class EntityProfileBody(BaseModel):
    """An entity's standing profile as of a turn for one viewpoint (FR-RET-6): the entity,
    its relationships in both directions, its placement (``null`` when it has none the
    viewpoint can read) and its inventory."""

    entity: EntityBody
    as_of: int
    relationships: list[RelationshipBody]
    placement: PlacementBody | None
    inventory: list[InventoryBody]
    viewpoint: str

    @classmethod
    def of(cls, found: ProfileView) -> EntityProfileBody:
        return cls(
            entity=EntityBody.of(found.entity),
            as_of=found.as_of,
            relationships=[RelationshipBody.of(r) for r in found.relationships],
            placement=None if found.placement is None else PlacementBody.of(found.placement),
            inventory=[InventoryBody.of(i) for i in found.inventory],
            viewpoint=found.viewpoint,
        )


@router.get(
    "/{story_id}/scene",
    response_model=SceneBody,
    operation_id="scene",
    summary="Read the story's scene as of a turn",
    description=(
        "The player character's location at the end of the turn and the entities placed (or "
        "present) there, from the structured projections over canon (FR-STORE-3). A character "
        "viewpoint is not shown what a fact it may not read placed there." + READ_RULES
    ),
)
async def get_scene(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> SceneBody:
    return SceneBody.of(await engine.scene(ctx, story_id, as_of=as_of, viewpoint=viewpoint))


@router.get(
    "/{story_id}/entities/{entity_id}/profile",
    response_model=EntityProfileBody,
    operation_id="entity_profile",
    summary="Read an entity's standing profile",
    description=(
        "The entity's relationships in both directions, where it is and what it holds, as of a "
        "turn, from the structured projections over canon (FR-RET-6). 404 `not_found` when the "
        "entity is not one of the story's." + READ_RULES
    ),
)
async def get_entity_profile(
    story_id: StoryId,
    entity_id: EntityId,
    ctx: ContextDep,
    engine: EngineDep,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> EntityProfileBody:
    return EntityProfileBody.of(
        await engine.entity_profile(ctx, story_id, entity_id, as_of=as_of, viewpoint=viewpoint)
    )
