"""``story_images``: images placed in a story at a narrative position for one audience viewpoint
(FR-IMG-5, FR-IMG-10; #261, migration 0136).

One row per story image: the story, its narrative position (the five ``at_*`` columns: a beat
image after beat ``at_beat ≥ 1`` of turn ``at_turn``'s variant ``at_variant`` with ``at_slot_k =
0``, or a slot image in the inter-turn slot ``at_turn⁺at_slot_k`` with ``at_beat = 0``), the
output viewpoint that may receive it (``audience_viewpoint``: ``character:<id>`` or
``omniscient``), its camera POV (``camera_pov`` ``{kind, character_id?}``: composition only, it
grants no access), its asset (``asset_id``, a soft reference to ``media_assets`` that
``core.media.assets``' reference discovery counts), the stored prompt of a generated image
(``NULL`` for an upload), its ``source`` and ``viewpoint_basis`` (copies of the asset's, written
at insert), the image job that made it (#263) and, once deleted, the operation that deleted it
(``deleted_operation_id``).

A derived record of the operation that placed it (#130's registry: truncation, swap, regenerate
and fork move it, ``source_event_ids`` its ``story_image_added`` event). ``deleted_operation_id``
is not one of its operation columns: a deletion follows its own operation's membership, so an
image is visible when its row is active and its deletion (if any) is not
(``core.images.story_images.visible``).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    position_group,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type
from epistrel.core.tables.media import MediaSource


class CameraKind(StrEnum):
    """A story image's camera POV (FR-IMG-5): composition only, it grants no access."""

    THIRD_PERSON = "third_person"
    ESTABLISHING = "establishing"
    OVER_THE_SHOULDER = "over_the_shoulder"
    FIRST_PERSON = "first_person"


CHARACTER_CAMERAS: frozenset[CameraKind] = frozenset(
    {CameraKind.OVER_THE_SHOULDER, CameraKind.FIRST_PERSON}
)
"""The camera kinds that name a character (``camera_pov.character_id``)."""


class ViewpointBasis(StrEnum):
    """What an image's perceivable inputs were built from (FR-IMG-10): ``filtered_inputs`` (the
    audience viewpoint's perceivable inputs, #263), ``protected_term_match`` (#263) or ``none``
    (an upload: Epistrel built no inputs)."""

    FILTERED_INPUTS = "filtered_inputs"
    PROTECTED_TERM_MATCH = "protected_term_match"
    NONE = "none"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v.value}'" for v in values) + ")"


POSITION_SHAPE_CHECK = "(at_slot_k = 0 AND at_beat >= 1) OR (at_slot_k >= 1 AND at_beat = 0)"
"""A beat image sits after a beat of the turn itself; a slot image at the start of its slot."""
CAMERA_CHECK = (
    "jsonb_typeof(camera_pov) = 'object' AND "
    + _in("camera_pov->>'kind'", CameraKind)
    + " AND ((camera_pov->>'kind') IN ('over_the_shoulder', 'first_person')) "
    "= (camera_pov ? 'character_id')"
)
"""The camera kind set; a character camera names its character and no other kind does."""
AUDIENCE_CHECK = (
    "audience_viewpoint = 'omniscient' OR audience_viewpoint ~ '^character:[0-9A-HJKMNP-TV-Z]{26}$'"
)
SOURCE_CHECK = _in("source", MediaSource)
VIEWPOINT_BASIS_CHECK = _in("viewpoint_basis", ViewpointBasis)

story_images = Table(
    "story_images",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    *derived_columns(),
    *position_group("at", nullable=False),
    Column("audience_viewpoint", Text, nullable=False),
    Column("camera_pov", JSONB, nullable=False),
    Column("asset_id", ulid_type(), nullable=False),
    Column("prompt", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("viewpoint_basis", Text, nullable=False),
    Column("job_id", ulid_type(), nullable=True),
    Column(
        "deleted_operation_id",
        ulid_type(),
        ForeignKey("operations.operation_id"),
        nullable=True,
    ),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("created_by", Text, nullable=False),
    *derived_indexes("story_images"),
    CheckConstraint(POSITION_SHAPE_CHECK, name="position_shape"),
    CheckConstraint(CAMERA_CHECK, name="camera_pov"),
    CheckConstraint(AUDIENCE_CHECK, name="audience_viewpoint"),
    CheckConstraint(SOURCE_CHECK, name="source"),
    CheckConstraint(VIEWPOINT_BASIS_CHECK, name="viewpoint_basis"),
)
Index("ix_story_images_story_at_turn", story_images.c.story_id, story_images.c.at_turn)
Index("ix_story_images_asset_id", story_images.c.asset_id)

STORY_IMAGES = register_derived(DerivedTable(story_images))
"""``story_images`` in ``core.membership.DERIVED_TABLES``: an image follows the operation that
placed it."""

__all__ = [
    "CHARACTER_CAMERAS",
    "STORY_IMAGES",
    "CameraKind",
    "ViewpointBasis",
    "story_images",
]
