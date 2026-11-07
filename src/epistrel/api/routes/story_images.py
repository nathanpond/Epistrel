"""A story's images (FR-IMG-5, FR-IMG-10; #261).

- ``POST /stories/{id}/images``: a ``multipart/form-data`` upload, the ``metadata`` part
  (``StoryImageUpload``) first, then the ``file`` part (an image). 201 with the image as an
  ``omniscient`` read returns it, its ``operation_id``, ``depicted_subjects`` and any
  ``warnings``. The image sits at its position for its audience viewpoint only.
- ``GET /stories/{id}/images/{image_id}?viewpoint=``: one image, for its audience viewpoint
  only (404 for any other).
- ``DELETE /stories/{id}/images/{image_id}``: delete it (204, idempotent).

Every handler makes one ``Engine`` call; reads in history (``GET /stories/{id}/history``) and
respond's ``beats`` hold the viewpoint's images in place.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api import multipart
from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.images.story_images import StoryImageItem, StoryImagePlaced
from epistrel.core.media.checks import CHECK_STATEMENTS

router = APIRouter(prefix="/stories", tags=["images"])

ImageId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The story image's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"],
    ),
]

_GATE = " 409 `story_archived`, `story_paused` or `revision_conflict`."


class StoryImageWarning(BaseModel):
    """What the upload did that the consumer should know: ``rating_looser`` (the image's
    rating lacks a guardrail the story's has; the story's rating still governs, FR-RATE-3,5)."""

    code: str
    media_id: str


class StoryImageUploadedBody(StoryImageItem, MutationResponse):
    """The image an upload placed, as an ``omniscient`` read returns it, with the operation
    that placed it, the subjects its asset depicts, the story's revision and any warnings."""

    model_config = ConfigDict(frozen=False)

    audience_viewpoint: str = Field(
        description="The only output viewpoint that receives the image (`character:<id>` or "
        "`omniscient`); the camera POV is a separate field and grants no access."
    )
    warnings: list[StoryImageWarning] = Field(default_factory=list)

    @classmethod
    def of(cls, placed: StoryImagePlaced) -> StoryImageUploadedBody:
        found: dict[str, Any] = {
            name: getattr(placed.image, name) for name in StoryImageItem.model_fields
        }
        return cls(
            **found,
            timeline_revision=placed.timeline_revision,
            audience_viewpoint=placed.audience_viewpoint,
            warnings=[StoryImageWarning.model_validate(w) for w in placed.warnings],
        )


@router.post(
    "/{story_id}/images",
    status_code=http_status.HTTP_201_CREATED,
    response_model=StoryImageUploadedBody,
    operation_id="upload_story_image",
    summary="Upload a story image at a narrative position for one audience viewpoint",
    description=(
        "A `multipart/form-data` body: the `metadata` part (JSON: `position` `{turn, beat}` "
        "after a beat of an active turn, the player input being beat 1, or `{slot_after_turn}` "
        "in a new inter-turn slot after the story's last completed turn; `audience_viewpoint` "
        "`character:<id>` or `omniscient`; `camera_pov` `{kind}` (default `third_person`; "
        "`over_the_shoulder`/`first_person` name a `character_id` present there; composition "
        "only, it grants no access); `depicted_subjects` (required, `[]` declares none); "
        "optional `caption`, `alt_text`, `content_rating_id`, default the story's), then the "
        "`file` part (an image). Refusals, in order: 422 `position_invalid` (an inactive turn, "
        "a beat it lacks, or a slot after any turn but the last completed one), "
        "`viewpoint_invalid`, `camera_pov_invalid`, `camera_pov_not_present`, "
        "`subjects_required`; then the image goes through `POST /media`'s upload path (415, 413, "
        "`rating_retired`, with an `image_check` model 422 `guardrail_violation` or 503 "
        "`image_check_unavailable`, the moderation hook's refusals), and nothing is stored on "
        "any of them. The asset is stored `source: uploaded`, `viewpoint_basis: none`, owned "
        "by the story's owner, and the image is placed in one operation; a looser rating than "
        "the story's answers a `rating_looser` warning. One image per request. Only reads for "
        f"its audience viewpoint receive it, in place.{_GATE} {CHECK_STATEMENTS}"
    ),
)
async def upload_story_image(
    story_id: StoryId, request: Request, response: Response, ctx: ContextDep, engine: EngineDep
) -> StoryImageUploadedBody:
    # The story's gates (importing, erasure, the expected revision) answer before the body is
    # read, as every other story route's do.
    await engine.check_expected_revision(ctx, story_id)
    form = await multipart.read_story_image(request)
    placed = await engine.upload_story_image(
        ctx,
        story_id,
        form.metadata,
        form.chunks(),
        content_type=form.content_type,
        body_size=multipart.declared_length(request),
    )
    return answered(StoryImageUploadedBody.of(placed), ctx, response, replayed=placed.replayed)


@router.get(
    "/{story_id}/images/{image_id}",
    response_model=StoryImageItem,
    operation_id="get_story_image",
    summary="Read one story image for its audience viewpoint",
    description=(
        "The image for the read's viewpoint (`viewpoint`, else the story's default; 422 "
        "`viewpoint_unresolved`): 404 for another audience, another story, a deleted image or "
        "one discarded with its turn. `depicted_subjects` only for `omniscient`; "
        "`include_audit` (`omniscient` only, 403 `omniscient_required`) adds `operation_id`, "
        f"`job_id` and `prompt`. {CHECK_STATEMENTS}"
    ),
)
async def get_story_image(
    story_id: StoryId,
    image_id: ImageId,
    ctx: ContextDep,
    engine: EngineDep,
    include_audit: bool = False,
) -> StoryImageItem:
    return await engine.get_story_image(ctx, story_id, image_id, include_audit=include_audit)


@router.delete(
    "/{story_id}/images/{image_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_story_image",
    summary="Delete a story image",
    description=(
        "The image leaves every read while the deletion is on the timeline (truncating it "
        "back brings the image back); its asset stays until the media sweep finds no "
        "reference. Idempotent: a deleted image answers 204 again. 404 for another story's "
        f"image or one discarded with its turn.{_GATE}"
    ),
)
async def delete_story_image(
    story_id: StoryId, image_id: ImageId, ctx: ContextDep, engine: EngineDep
) -> Response:
    deleted = await engine.delete_story_image(ctx, story_id, image_id)
    return no_content(ctx, replayed=deleted.replayed)
