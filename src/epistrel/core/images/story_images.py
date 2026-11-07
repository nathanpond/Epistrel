"""Story images: pictures placed at a narrative position for one audience viewpoint
(FR-IMG-5, FR-IMG-10; #261).

An image in a story sits at a **narrative position**: after a beat of a turn (``{turn, beat}``,
#57/#150's numbering over the turn's active variant, the player input being beat 1; any beat
the variant recorded, the reply included), or in an inter-turn slot (``{slot_after_turn: N}``).
A slot follows only the story's last completed turn N (owner decision 14, 2026-11-01:
``core.world_clock.slots.open_inter_turn`` never inserts a slot into the past), so an earlier
N is 422 ``position_invalid``, as is an inactive or uncommitted turn or a beat the variant does
not have.

It has an **audience viewpoint** (``character:<id>`` of a story character, departed allowed,
presence not required, or ``omniscient``; else 422 ``viewpoint_invalid``): the only output
viewpoint that receives it. Its **camera POV** (``third_person`` by default, ``establishing``,
or ``over_the_shoulder``/``first_person`` naming a character) is composition only and grants no
access: a character camera names a character authoritatively present at the position
(``core.images.safety.present_at``: #161's routing presence, every character with perception
``none``; else 422 ``camera_pov_not_present``), and a malformed one is 422
``camera_pov_invalid``.

**Upload** (``POST /stories/{id}/images``; ``upload``): the checks run in the order
``position_invalid`` → ``viewpoint_invalid`` → ``camera_pov_invalid`` →
``camera_pov_not_present`` → ``subjects_required`` (``depicted_subjects`` declared, ``[]``
declaring none), before the bytes are checked; then #258's upload path checks the staged image
(``passed``, or ``unchecked`` without a checker) outside the story lock, and one operation
stores the asset (``source = uploaded``, ``viewpoint_basis = none``, owned by the story's owner,
its rating the request's or the story's; a looser one answers a ``rating_looser`` warning,
FR-RATE-5) and places the image. At a beat that operation is a ``story_image`` operation
anchored at the turn's active variant (it follows the turn), at a slot an inter-turn slot
operation (``slot_position(op)`` is ``N⁺k``; truncating to N discards it, FR-STORE-31). Under
the story lock the commit re-checks the position: a turn whose active variant changed, or a
slot whose last completed turn moved, is 422 ``position_invalid`` and nothing is written.

**Visibility.** A row is visible when its membership is ``active`` (it follows the operation
that placed it: truncate, untruncate, regenerate, swap and fork need no image-specific code,
#130) and it has no deletion, or its deletion's operation is not on the timeline: a deletion
follows its own operation's membership. ``delete`` records ``story_image_deleted`` in an
operation of the same kind as an upload's at that position (a slot image's in a new slot);
the asset stays until the media sweep finds no reference (FR-MEDIA-2), and a deleted row still
counts as a reference until pruned or erased.

**Reads.** ``images_for_history`` gives a history page the visible images whose
``audience_viewpoint`` equals the read's viewpoint (under either ``player_view``: it widens
beats, never image audiences), and ``merge_into_history`` puts them in place: after the last
returned item of their turn at or before their beat (an image after a beat the viewpoint does
not receive follows the last preceding beat it does), a slot image after its turn's items, in
position, then operation, then image id order. ``depicted_subjects`` is only on ``omniscient``
reads (it can name someone a character viewpoint does not know). ``as_recorded`` lists the
``omniscient`` images whose ``story_image_added`` the as-recorded checkpoint holds and whose
``story_image_deleted`` it does not (owner 2026-11-06, FR-STORE-23).

Log lines carry identifiers only (invariant 3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import and_, exists, func, literal, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from epistrel.core import clock
from epistrel.core.enums import Membership, OperationKind, OperationStatus, Provenance
from epistrel.core.erasure.index import index_records
from epistrel.core.errors import (
    CameraPovInvalid,
    CameraPovNotPresent,
    InvalidRequest,
    PositionInvalid,
    StoryImageNotFound,
    StoryNotFound,
    SubjectsRequired,
    ViewpointInvalid,
    refuse_nul,
)
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import STORY_IMAGE_ADDED, STORY_IMAGE_DELETED
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.media import assets as media
from epistrel.core.media.checks import CHECK_STATEMENTS, ImageCheck, image_check_of
from epistrel.core.membership import (
    Position,
    active_only,
    insert_derived,
)
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.tables import characters, events, media_assets, operations, stories
from epistrel.core.tables.media import CheckStatus, MediaKind, MediaSource
from epistrel.core.tables.story_images import (
    CHARACTER_CAMERAS,
    CameraKind,
    ViewpointBasis,
    story_images,
)
from epistrel.core.world_clock.slots import open_inter_turn, slot_position
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

Ulid = Annotated[str, Field(pattern=ULID_PATTERN)]
METADATA_FIELD = "body.metadata"
DEFAULT_CAMERA: dict[str, Any] = {"kind": CameraKind.THIRD_PERSON.value}
RATING_LOOSER = "rating_looser"
_ULID = re.compile(ULID_PATTERN)

# -- the request -------------------------------------------------------------------------------


class BeatPosition(BaseModel):
    """After beat ``beat`` of turn ``turn`` (the player input is beat 1), on the turn's active
    variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: StrictInt = Field(ge=1)
    beat: StrictInt = Field(ge=1)


class SlotPosition(BaseModel):
    """In a new inter-turn slot after turn ``slot_after_turn``, which must be the story's last
    completed turn (0 before the first)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_after_turn: StrictInt = Field(ge=0)


def _deduplicated(
    value: list[media.DepictedSubject] | None,
) -> list[media.DepictedSubject] | None:
    return None if value is None else list(dict.fromkeys(value))


def _document_upload(schema: dict[str, Any]) -> None:
    schema.setdefault("required", []).append("depicted_subjects")
    prop = schema["properties"]["depicted_subjects"]
    options = [o for o in prop.pop("anyOf", []) if o.get("type") != "null"]
    if options:
        prop.update(options[0])
    prop.pop("default", None)
    schema["examples"] = [dict(EXAMPLE_UPLOAD)]


EXAMPLE_UPLOAD: dict[str, Any] = {
    "position": {"turn": 3, "beat": 2},
    "audience_viewpoint": "character:01J9Z8Q4N5V6W7X8Y9Z0A1B2C5",
    "camera_pov": {"kind": "third_person"},
    "depicted_subjects": [{"kind": "character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"}],
    "alt_text": "Jenni at the window",
}


class StoryImageUpload(BaseModel):
    """``POST /stories/{id}/images``' ``metadata`` part (FR-IMG-5): where the image goes
    (``position``: ``{turn, beat}`` or ``{slot_after_turn}``), who may receive it
    (``audience_viewpoint``: ``character:<id>`` or ``omniscient``), its camera POV (default
    ``{kind: third_person}``; ``over_the_shoulder``/``first_person`` name a ``character_id``
    present there), the subjects it depicts (required: ``[]`` declares none), and optional
    caption, alt text and content rating (default the story's)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_document_upload)

    position: BeatPosition | SlotPosition
    audience_viewpoint: StrictStr = Field(min_length=1, max_length=64)
    camera_pov: dict[str, Any] | None = Field(
        default=None,
        description="`{kind}`: `third_person` (the default), `establishing`, or "
        "`over_the_shoulder`/`first_person` with the `character_id` of a character present at "
        "the position. Composition only: it grants no viewpoint access.",
    )
    depicted_subjects: Annotated[
        list[media.DepictedSubject] | None, AfterValidator(_deduplicated)
    ] = Field(
        default=None,
        max_length=media.SUBJECTS_MAX,
        description="Every subject the image depicts (required; `[]` declares none). Omitted: "
        "422 `subjects_required`.",
    )
    caption: str | None = Field(default=None, max_length=media.CAPTION_MAX)
    alt_text: str | None = Field(default=None, max_length=media.ALT_TEXT_MAX)
    content_rating_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="The image's content rating (default the story's). A looser one is accepted "
        "with a `rating_looser` warning; the story's rating still governs (FR-RATE-3,5).",
    )


_UPLOAD: TypeAdapter[StoryImageUpload] = TypeAdapter(StoryImageUpload)


def parse_metadata(raw: bytes) -> StoryImageUpload:
    """The ``metadata`` part, validated as JSON's own types (strict, FR-API-4): 422
    ``validation_error`` at ``body.metadata.<field>``. The checks of the module docstring run
    later, in their order."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise InvalidRequest(
            "the metadata part is not valid JSON", {"field": METADATA_FIELD}
        ) from None
    refuse_nul(document, ("body", "metadata"))
    try:
        return _UPLOAD.validate_json(raw, strict=True)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "metadata")) from None


# -- the placement -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Placement:
    """An upload's checked position, audience, camera and rating: ``turn``/``variant``/``beat``
    for a beat image, ``turn`` (the slot's ``N``) for a slot image (``slot``)."""

    story_id: str
    turn: int
    variant: int
    beat: int
    slot: bool
    audience_viewpoint: str
    camera_pov: dict[str, Any]
    content_rating_id: str
    owner: str
    warnings: tuple[dict[str, Any], ...] = ()


async def last_completed_turn(conn: AsyncConnection, story_id: str) -> int:
    """The story's last completed turn (its active head; 0 before the first)."""
    found = await conn.scalar(
        select(func.max(operations.c.narrative_turn)).where(
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            operations.c.membership == Membership.ACTIVE.value,
        )
    )
    return int(found or 0)


async def _active_turn(conn: AsyncConnection, story_id: str, turn: int) -> tuple[str, int] | None:
    row = (
        await conn.execute(
            select(operations.c.operation_id, operations.c.turn_variant).where(
                operations.c.story_id == story_id,
                operations.c.narrative_turn == turn,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                operations.c.membership == Membership.ACTIVE.value,
            )
        )
    ).first()
    return None if row is None else (str(row.operation_id), int(row.turn_variant or 0))


async def _beat_count(conn: AsyncConnection, operation_id: str) -> int:
    found = await conn.scalar(
        select(func.max(events.c.beat)).where(
            events.c.operation_id == operation_id, events.c.beat.is_not(None)
        )
    )
    return int(found or 0)


def _position_invalid(message: str, **details: Any) -> PositionInvalid:
    return PositionInvalid(message, {"field": f"{METADATA_FIELD}.position", **details})


async def _position(
    conn: AsyncConnection, story_id: str, position: BeatPosition | SlotPosition
) -> tuple[int, int, int, bool]:
    """``(turn, variant, beat, slot)`` of an active position; 422 ``position_invalid``."""
    if isinstance(position, SlotPosition):
        last = await last_completed_turn(conn, story_id)
        if position.slot_after_turn != last:
            raise _position_invalid(
                "a slot follows only the story's last completed turn",
                reason="not_last_completed_turn",
                last_completed_turn=last,
            )
        variant = 0
        if last >= 1:
            found = await _active_turn(conn, story_id, last)
            variant = found[1] if found is not None else 0
        return last, variant, 0, True
    found = await _active_turn(conn, story_id, position.turn)
    if found is None:
        raise _position_invalid(
            f"turn {position.turn} is not an active committed turn", reason="turn_inactive"
        )
    operation_id, variant = found
    count = await _beat_count(conn, operation_id)
    if position.beat > count:
        raise _position_invalid(
            f"turn {position.turn} has no beat {position.beat}",
            reason="beat_out_of_range",
            beat_count=count,
        )
    return position.turn, variant, position.beat, False


async def _cast(conn: AsyncConnection, story_id: str) -> list[str]:
    return [
        str(i)
        for i in await conn.scalars(
            select(characters.c.id)
            .where(characters.c.story_id == story_id)
            .order_by(characters.c.ordinal)
        )
    ]


def audience_character(viewpoint: str) -> str | None:
    """The character of a ``character:<id>`` viewpoint (``None`` for ``omniscient``)."""
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    return None


def _audience(viewpoint: str, cast: Collection[str]) -> str:
    named = viewpoint.strip()
    if named == OMNISCIENT:
        return OMNISCIENT
    member = audience_character(named)
    if member is not None and member in cast:
        return named
    raise ViewpointInvalid(
        "the audience viewpoint must be omniscient or character:<id> of a story character",
        {"field": f"{METADATA_FIELD}.audience_viewpoint"},
    )


def camera_of(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """The stored camera POV of a request's ``camera_pov`` (the default when omitted); 422
    ``camera_pov_invalid`` for an unknown kind or a bad shape."""
    at = f"{METADATA_FIELD}.camera_pov"
    if value is None:
        return dict(DEFAULT_CAMERA)
    kind = value.get("kind", CameraKind.THIRD_PERSON.value)
    if not isinstance(kind, str) or kind not in {k.value for k in CameraKind}:
        raise CameraPovInvalid(
            "camera_pov.kind is third_person, establishing, over_the_shoulder or first_person",
            {"field": f"{at}.kind"},
        )
    unknown = sorted(set(value) - {"kind", "character_id"})
    if unknown:
        raise CameraPovInvalid(
            "camera_pov holds only kind and character_id", {"field": f"{at}.{unknown[0]}"}
        )
    character = value.get("character_id")
    if CameraKind(kind) in CHARACTER_CAMERAS:
        if not isinstance(character, str) or not _ULID.fullmatch(character):
            raise CameraPovInvalid(
                f"a {kind} camera names its character (character_id)",
                {"field": f"{at}.character_id"},
            )
        return {"kind": kind, "character_id": character}
    if "character_id" in value:
        raise CameraPovInvalid(
            f"a {kind} camera names no character", {"field": f"{at}.character_id"}
        )
    return {"kind": kind}


async def _rating_warning(
    conn: AsyncConnection, story_rating: str | None, image_rating: str
) -> tuple[dict[str, Any], ...]:
    """A ``rating_looser`` warning when the image's rating lacks a guardrail the story's has
    (FR-RATE-5: it warns, never blocks; the story's rating still governs)."""
    if story_rating is None or image_rating == story_rating:
        return ()
    from epistrel.core.ratings import effective_guardrails, load_graph
    from epistrel.core.tables import policies

    graph = await load_graph(conn)
    live = frozenset(
        str(i)
        for i in await conn.scalars(select(policies.c.id).where(policies.c.retired_at.is_(None)))
    )

    def guarded(rating_id: str) -> frozenset[str]:
        if rating_id not in graph:
            return frozenset()
        return frozenset(g.policy_id for g in effective_guardrails(rating_id, graph)) & live

    if guarded(story_rating) <= guarded(image_rating):
        return ()
    return ({"code": RATING_LOOSER},)


async def place(conn: AsyncConnection, story_id: str, request: StoryImageUpload) -> Placement:
    """The upload's checks, in the module docstring's order, before any byte is checked."""
    from epistrel.core.images.safety import present_at

    story = (
        await conn.execute(
            select(stories.c.content_rating_id, stories.c.owner_principal).where(
                stories.c.id == story_id
            )
        )
    ).one_or_none()
    if story is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    turn, variant, beat, slot = await _position(conn, story_id, request.position)
    cast = await _cast(conn, story_id)
    audience = _audience(request.audience_viewpoint, cast)
    camera = camera_of(request.camera_pov)
    character = camera.get("character_id")
    if character is not None:
        at = Position(turn, variant, 1, 0, 0) if slot else Position(turn, variant, 0, beat, 0)
        # The audience's character is the actor (#541's rule): the camera's is what is tested.
        actors = [a for a in (audience_character(audience),) if a is not None]
        present = await present_at(conn, story_id, at, cast, actors) if character in cast else ()
        if character not in present:
            raise CameraPovNotPresent(
                "the camera's character is not present at the position",
                {"field": f"{METADATA_FIELD}.camera_pov.character_id"},
            )
    if request.depicted_subjects is None:
        raise SubjectsRequired(
            "a story image must declare the subjects it depicts: `depicted_subjects` "
            "(an empty list declares none)",
            {"field": f"{METADATA_FIELD}.depicted_subjects"},
        )
    story_rating = None if story.content_rating_id is None else str(story.content_rating_id)
    rating = request.content_rating_id or story_rating
    if rating is None:
        raise InvalidRequest(
            "the story has no rating: name the image's content_rating_id",
            {"field": f"{METADATA_FIELD}.content_rating_id"},
        )
    return Placement(
        story_id=story_id,
        turn=turn,
        variant=variant,
        beat=beat,
        slot=slot,
        audience_viewpoint=audience,
        camera_pov=camera,
        content_rating_id=rating,
        owner=str(story.owner_principal),
        warnings=await _rating_warning(conn, story_rating, rating),
    )


def media_request(request: StoryImageUpload, placement: Placement) -> media.MediaUpload:
    """The asset half of an upload, as #258's upload path takes it: an image, the placement's
    rating, the story's owner."""
    return media.MediaUpload(
        kind=MediaKind.IMAGE,
        content_rating_id=placement.content_rating_id,
        alt_text=request.alt_text,
        caption=request.caption,
        depicted_subjects=list(request.depicted_subjects or []),
        owner=placement.owner,
    )


# -- results -----------------------------------------------------------------------------------


class StoryImageItem(BaseModel):
    """A story image as a read returns it (FR-IMG-5, FR-IMG-10): ``kind: image``, its id and its
    asset's (``media_id``), its position (``turn`` with ``beat``, or ``slot_after_turn``), its
    camera POV, the asset's alt text, caption, ``source``, ``check_status``, ``check`` and
    ``viewpoint_basis``, and when it was placed. ``depicted_subjects`` only on an
    ``omniscient`` read; ``operation_id``, ``job_id`` and ``prompt`` only with
    ``include_audit``."""

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={"description": f"A story image in place. {CHECK_STATEMENTS}"},
    )

    kind: Literal["image"] = "image"
    image_id: str
    media_id: str
    turn: int
    variant: int = Field(default=0, exclude=True)
    slot_k: int = Field(default=0, exclude=True)
    beat: int | None = Field(default=None, exclude_if=lambda v: v is None)
    slot_after_turn: int | None = Field(default=None, exclude_if=lambda v: v is None)
    camera_pov: dict[str, Any]
    alt_text: str | None
    caption: str | None
    source: MediaSource
    check_status: CheckStatus
    check: ImageCheck | None
    viewpoint_basis: ViewpointBasis
    created_at: datetime
    depicted_subjects: list[media.DepictedSubject] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    operation_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    job_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    prompt: str | None = Field(default=None, exclude_if=lambda v: v is None)
    seq: int = Field(default=0, exclude=True)
    """The placing operation's first event ``seq``: the order of images at one position."""

    def key(self) -> tuple[int, int, int, int, str]:
        """The image's order: position, then operation, then id."""
        return (self.turn, self.slot_k, self.beat or 0, self.seq, self.image_id)


@dataclass(frozen=True, slots=True)
class StoryImagePlaced:
    """An upload's answer (201): the image as an ``omniscient`` read returns it, with
    ``operation_id``, ``depicted_subjects`` and any ``warnings`` (``rating_looser``)."""

    image: StoryImageItem
    operation_id: str
    timeline_revision: int
    audience_viewpoint: str
    warnings: list[dict[str, Any]] = field(default_factory=list)
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class StoryImageDeletion:
    """A delete (204): the image, the operation that deleted it (``None`` when an active
    deletion already held) and the story's revision."""

    image_id: str
    operation_id: str | None
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- the reads ---------------------------------------------------------------------------------

_DELETION = aliased(operations, name="deletion")


def _not_deleted(chain: Any | None = None) -> Any:
    """No deletion, or one whose operation is not on the timeline (or the chain read)."""
    d = _DELETION.c
    on = d.membership == Membership.ACTIVE.value
    if chain is not None:
        on = chain.visible(d.membership, d.narrative_turn, d.operation_id)
    return or_(
        story_images.c.deleted_operation_id.is_(None),
        not_(exists().where(d.operation_id == story_images.c.deleted_operation_id, on)),
    )


def visible(chain: Any | None = None) -> Any:
    """SQL: a visible story image (see the module docstring); ``chain`` a variant read's
    ``core.variants.ChainSelection``."""
    s = story_images.c
    on_timeline = (
        active_only(story_images)
        if chain is None
        else chain.visible(s.membership, s.at_turn, s.operation_id)
    )
    return and_(on_timeline, _not_deleted(chain))


def _query() -> Any:
    s, m = story_images.c, media_assets.c
    first_seq = (
        select(func.min(events.c.seq))
        .where(events.c.operation_id == s.operation_id)
        .scalar_subquery()
    )
    return select(
        story_images,
        m.alt_text,
        m.caption,
        m.check_status,
        m.check_model,
        m.check_endpoint,
        m.policy_results,
        m.depicted_subjects,
        first_seq.label("seq"),
    ).join(media_assets, m.id == s.asset_id)


def item_of(row: Any, *, omniscient: bool, audit: bool = False) -> StoryImageItem:
    """One image row (``_query``'s) as ``StoryImageItem``."""
    slot = int(row.at_slot_k) > 0
    return StoryImageItem(
        kind="image",  # set, so a read that leaves out unset fields keeps it
        image_id=str(row.id),
        media_id=str(row.asset_id),
        turn=int(row.at_turn),
        variant=int(row.at_variant),
        slot_k=int(row.at_slot_k),
        beat=None if slot else int(row.at_beat),
        slot_after_turn=int(row.at_turn) if slot else None,
        camera_pov=dict(row.camera_pov),
        alt_text=row.alt_text,
        caption=row.caption,
        source=MediaSource(row.source),
        check_status=CheckStatus(row.check_status),
        check=image_check_of(
            row.check_status, row.check_model, row.check_endpoint, row.policy_results or ()
        ),
        viewpoint_basis=ViewpointBasis(row.viewpoint_basis),
        created_at=row.created_at,
        depicted_subjects=(
            [media.DepictedSubject.model_validate(d) for d in row.depicted_subjects or ()]
            if omniscient
            else None
        ),
        operation_id=str(row.operation_id) if audit else None,
        job_id=None if not audit or row.job_id is None else str(row.job_id),
        prompt=row.prompt if audit else None,
        seq=int(row.seq or 0),
    )


async def images_for_history(
    conn: AsyncConnection,
    story_id: str,
    viewpoint: str,
    *,
    lower: int,
    upper: int,
    slots_through: int,
    chain: Any | None = None,
    audit: bool = False,
) -> list[StoryImageItem]:
    """The visible images of turns ``lower`` to ``upper`` whose audience is ``viewpoint``, in
    order; slot images only after turns up to ``slots_through`` (``as_of=N`` leaves out the
    slots after N, #65's rule)."""
    s = story_images.c
    query = _query().where(
        s.story_id == story_id,
        s.audience_viewpoint == viewpoint,
        s.at_turn.between(lower, upper),
        or_(s.at_slot_k == 0, s.at_turn <= slots_through),
        visible(chain),
    )
    omniscient = viewpoint == OMNISCIENT
    found = [item_of(r, omniscient=omniscient, audit=audit) for r in await conn.execute(query)]
    return sorted(found, key=StoryImageItem.key)


async def images_for_operation(
    db: AsyncEngine | AsyncConnection, story_id: str, turn: int, variant: int, viewpoint: str
) -> list[StoryImageItem]:
    """Respond's: the visible images positioned within turn ``turn``'s variant ``variant``
    (beat images only; slots follow the turn) whose audience is ``viewpoint``."""
    s = story_images.c
    query = _query().where(
        s.story_id == story_id,
        s.audience_viewpoint == viewpoint,
        s.at_turn == turn,
        s.at_variant == variant,
        s.at_slot_k == 0,
        visible(),
    )
    if isinstance(db, AsyncConnection):
        rows = list(await db.execute(query))
    else:
        async with db.connect() as conn:
            rows = list(await conn.execute(query))
    omniscient = viewpoint == OMNISCIENT
    return sorted((item_of(r, omniscient=omniscient) for r in rows), key=StoryImageItem.key)


async def as_recorded(
    conn: AsyncConnection, story_id: str, event_types_ids: Iterable[tuple[str, Any]]
) -> list[StoryImageItem]:
    """An as-recorded read's images (#222, owner 2026-11-06): the ``omniscient`` images whose
    ``story_image_added`` is among the checkpoint's events (``(type, payload)`` pairs) and whose
    ``story_image_deleted`` is not."""
    added: list[str] = []
    deleted: set[str] = set()
    for kind, payload in event_types_ids:
        image_id = getattr(payload, "image_id", None)
        if image_id is None:
            continue
        if kind == STORY_IMAGE_ADDED:
            added.append(str(image_id))
        elif kind == STORY_IMAGE_DELETED:
            deleted.add(str(image_id))
    wanted = [i for i in added if i not in deleted]
    if not wanted:
        return []
    s = story_images.c
    rows = await conn.execute(
        _query().where(s.story_id == story_id, s.id.in_(wanted), s.audience_viewpoint == OMNISCIENT)
    )
    return sorted((item_of(r, omniscient=True) for r in rows), key=StoryImageItem.key)


async def get_image(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    image_id: str,
    viewpoint: str,
    *,
    audit: bool = False,
) -> StoryImageItem:
    """One visible image for an already resolved ``viewpoint``: 404 ``not_found`` when it is
    another story's, inactive, deleted, or for another audience."""
    s = story_images.c
    query = _query().where(
        s.story_id == story_id,
        s.id == image_id,
        s.audience_viewpoint == viewpoint,
        visible(),
    )
    if isinstance(db, AsyncConnection):
        row = (await db.execute(query)).one_or_none()
    else:
        async with db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _not_found(story_id, image_id)
    return item_of(row, omniscient=viewpoint == OMNISCIENT, audit=audit)


def _not_found(story_id: str, image_id: str) -> StoryImageNotFound:
    return StoryImageNotFound(
        f"story {story_id} has no image {image_id}", {"story_id": story_id, "image_id": image_id}
    )


class _Positioned(Protocol):
    @property
    def turn(self) -> int | None: ...

    @property
    def beat(self) -> int | None: ...


def merge_into_history[T: _Positioned](
    items: Sequence[T], images: Sequence[StoryImageItem]
) -> list[T | StoryImageItem]:
    """``items`` (ascending, as a history page or respond's beats hold them) with ``images``
    in place (see the module docstring); positionless items count as turn 0."""
    merged: list[T | StoryImageItem] = list(items)
    for image in sorted(images, key=StoryImageItem.key):
        at = _slot_for(merged, image)
        merged.insert(at, image)
    return merged


def _slot_for(merged: Sequence[Any], image: StoryImageItem) -> int:
    """Where ``image`` goes: after the last entry at or before its position."""
    image_key = (image.turn, image.slot_k, image.beat or 0)
    at = 0
    for index, entry in enumerate(merged):
        if isinstance(entry, StoryImageItem):
            if (entry.turn, entry.slot_k, entry.beat or 0) <= image_key:
                at = index + 1
            continue
        turn = int(entry.turn or 0)
        beat = int(entry.beat or 0)
        if turn < image.turn or (turn == image.turn and (image.slot_k > 0 or beat <= image_key[2])):
            at = index + 1
    return at


# -- the writes --------------------------------------------------------------------------------


class AssetCommit(Protocol):
    """Stores the prepared asset in the operation's commit (``core.media.assets.commit``)."""

    async def __call__(self, conn: AsyncConnection) -> str: ...


def _operation(
    db: AsyncEngine,
    story_id: str,
    *,
    slot: bool,
    turn: int,
    principal: str,
    idempotency_key: str | None,
) -> Operation:
    """A beat position's ``story_image`` operation anchored at ``turn`` (it follows the turn),
    or a slot's inter-turn operation after the last completed turn."""
    if slot:
        return open_inter_turn(
            db,
            story_id,
            source=Provenance.CONSUMER,
            principal=principal,
            idempotency_key=idempotency_key,
        )
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.STORY_IMAGE,
        principal=principal,
        turn=turn,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )


def _committed_position(op: Operation, placement: Placement) -> Position:
    """The position ``op``'s commit gave the image; 422 ``position_invalid`` when the timeline
    moved since the checks (another active variant, or a later last completed turn)."""
    if placement.slot:
        position = slot_position(op)
        if position.turn != placement.turn:
            raise _position_invalid(
                "a slot follows only the story's last completed turn",
                reason="not_last_completed_turn",
                last_completed_turn=position.turn,
            )
        return position
    if op.narrative_turn != placement.turn or (op.turn_variant or 0) != placement.variant:
        raise _position_invalid(
            f"turn {placement.turn}'s active variant changed", reason="turn_inactive"
        )
    return Position(placement.turn, placement.variant, 0, placement.beat, 0)


async def _event_id(conn: AsyncConnection, operation_id: str, kind: str) -> str:
    found = await conn.scalar(
        select(events.c.id)
        .where(events.c.operation_id == operation_id, events.c.type == kind)
        .order_by(events.c.seq)
        .limit(1)
    )
    assert found is not None  # noqa: S101 - the commit wrote the event before its steps
    return str(found)


async def _index(conn: AsyncConnection, image_id: str) -> None:
    from epistrel.core.erasure.records import story_image_record

    row = (
        await conn.execute(
            select(story_images, media_assets.c.depicted_subjects.label("asset_subjects"))
            .join(media_assets, media_assets.c.id == story_images.c.asset_id)
            .where(story_images.c.id == image_id)
        )
    ).one()
    await index_records(conn, [story_image_record(row)])


async def upload(
    db: AsyncEngine,
    placement: Placement,
    asset_id: str,
    store_asset: AssetCommit,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> StoryImagePlaced:
    """Place an uploaded image (see the module docstring): one operation stores the asset
    (``store_asset``, the prepared upload's commit) and the image row."""
    image_id = new_ulid()
    op = _operation(
        db,
        placement.story_id,
        slot=placement.slot,
        turn=placement.turn,
        principal=principal,
        idempotency_key=idempotency_key,
    )

    async def step(conn: AsyncConnection) -> None:
        position = _committed_position(op, placement)
        stored = await store_asset(conn)
        assert stored == asset_id  # noqa: S101 - the staged asset's id
        await insert_derived(
            conn,
            story_images,
            [
                {
                    "id": image_id,
                    "story_id": placement.story_id,
                    "operation_id": op.operation_id,
                    "source_event_ids": [await _event_id(conn, op.operation_id, STORY_IMAGE_ADDED)],
                    "narrative_turn": position.turn,
                    "turn_variant": position.variant,
                    **position.columns("at"),
                    "audience_viewpoint": placement.audience_viewpoint,
                    "camera_pov": placement.camera_pov,
                    "asset_id": asset_id,
                    "prompt": None,
                    "source": MediaSource.UPLOADED.value,
                    "viewpoint_basis": ViewpointBasis.NONE.value,
                    "job_id": None,
                    "created_at": clock.now(),
                    "created_by": principal,
                }
            ],
        )
        await _index(conn, image_id)

    async with op:
        op.append(
            STORY_IMAGE_ADDED,
            {"image_id": image_id, "asset_id": asset_id},
            beat=None if placement.slot else placement.beat,
        )
        op.add_commit_step(step)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - a committed story operation's
    async with db.connect() as conn:
        rows = await conn.execute(_query().where(story_images.c.id == image_id))
        image = item_of(rows.one(), omniscient=True)
    log.debug("story image placed", story_id=placement.story_id, image_id=image_id)
    return StoryImagePlaced(
        image=replace_item(image, operation_id=op.operation_id),
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
        audience_viewpoint=placement.audience_viewpoint,
        warnings=[{**w, "media_id": asset_id} for w in placement.warnings],
    )


def replace_item(item: StoryImageItem, **changes: Any) -> StoryImageItem:
    return item.model_copy(update=changes)


async def delete(
    db: AsyncEngine,
    story_id: str,
    image_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> StoryImageDeletion:
    """Delete a visible image (see the module docstring): 404 for another story's, an unknown
    or an inactive (discarded or dormant) one; an image an active deletion already holds
    answers unchanged (idempotent)."""
    from epistrel.core.stories import require_writable

    await require_writable(db, story_id)
    s = story_images.c
    async with db.connect() as conn:
        row = (
            await conn.execute(select(story_images).where(s.story_id == story_id, s.id == image_id))
        ).one_or_none()
        if row is None or row.membership != Membership.ACTIVE.value:
            raise _not_found(story_id, image_id)
        deleted = (
            await conn.execute(
                select(literal(1))
                .select_from(story_images)
                .where(s.id == image_id, not_(_not_deleted()))
            )
        ).first()
        if deleted is not None:
            revision = await conn.scalar(
                select(stories.c.timeline_revision).where(stories.c.id == story_id)
            )
            return StoryImageDeletion(
                image_id=image_id, operation_id=None, timeline_revision=int(revision or 0)
            )
    slot = int(row.at_slot_k) > 0
    op = _operation(
        db,
        story_id,
        slot=slot,
        turn=int(row.at_turn),
        principal=principal,
        idempotency_key=idempotency_key,
    )

    async def step(conn: AsyncConnection) -> None:
        changed = await conn.execute(
            update(story_images)
            .where(s.id == image_id, active_only(story_images), _not_deleted())
            .values(deleted_operation_id=op.operation_id)
        )
        if not changed.rowcount:
            raise _not_found(story_id, image_id)

    async with op:
        op.append(
            STORY_IMAGE_DELETED,
            {"image_id": image_id},
            beat=None if slot else int(row.at_beat),
        )
        op.add_commit_step(step)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - a committed story operation's
    log.debug("story image deleted", story_id=story_id, image_id=image_id)
    return StoryImageDeletion(
        image_id=image_id, operation_id=op.operation_id, timeline_revision=op.timeline_revision
    )


# -- registrations -----------------------------------------------------------------------------


TABLE = "public.story_images"
media.register_reference_source(media.ReferenceSource(TABLE, "asset_id", None))
media.REFERENCE_HOLDERS.setdefault(
    TABLE,
    (
        "story",
        "t.story_id",
        "(SELECT s.owner_principal FROM public.stories AS s WHERE s.id = t.story_id)",
    ),
)
"""FR-MEDIA-2: an image's asset is referenced by its row, active or not, deleted or not, until
the row is pruned or erased; the row belongs to its story (#117's offboarding)."""


__all__ = [
    "BeatPosition",
    "Placement",
    "SlotPosition",
    "StoryImageDeletion",
    "StoryImageItem",
    "StoryImagePlaced",
    "StoryImageUpload",
    "as_recorded",
    "audience_character",
    "camera_of",
    "delete",
    "get_image",
    "images_for_history",
    "images_for_operation",
    "item_of",
    "last_completed_turn",
    "media_request",
    "merge_into_history",
    "parse_metadata",
    "place",
    "upload",
    "visible",
]
