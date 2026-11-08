"""A story's images (FR-IMG-5, FR-IMG-8, FR-IMG-10; #261, #263).

- ``POST /stories/{id}/images``: one operation, two bodies, told apart by ``Content-Type``:
  - ``application/json`` (``StoryImageRequest``, #263): generate the image at a position for an
    audience viewpoint from what that viewpoint perceived (or from the consumer's ``prompt``).
    202 ``{job_id, status: queued}`` with ``Location: /image-jobs/{id}``; the job places the
    image when it promotes it;
  - ``multipart/form-data`` (#261): an upload, the ``metadata`` part (``StoryImageUpload``)
    first, then the ``file`` part (an image). 201 with the image as an ``omniscient`` read
    returns it, its ``operation_id``, ``depicted_subjects`` and any ``warnings``. Any other
    body is the upload path's 422.
  The image sits at its position for its audience viewpoint only.
- ``GET /stories/{id}/images/{image_id}?viewpoint=``: one image, for its audience viewpoint
  only (404 for any other).
- ``DELETE /stories/{id}/images/{image_id}``: delete it (204, idempotent).

Every handler makes one ``Engine`` call (the generation request reads its body after the
story's gates, as the upload does); reads in history (``GET /stories/{id}/history``) and
respond's ``beats`` hold the viewpoint's images in place.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from python_multipart.multipart import parse_options_header

from epistrel.api import multipart
from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.images.story_images import (
    StoryImageItem,
    StoryImagePlaced,
    parse_request,
)
from epistrel.core.media.checks import CHECK_STATEMENTS, IMAGE_VIEWPOINT_GUARANTEE
from epistrel.orchestrator.images.targets.story import StoryImageQueued

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


class StoryImageRequestedBody(MutationResponse):
    """A story image generation request's answer (202): the job to follow at ``Location``, and
    the story's unchanged revision (the job places the image in its own operation)."""

    job_id: str
    status: str

    @classmethod
    def of(cls, queued: StoryImageQueued) -> StoryImageRequestedBody:
        return cls(
            job_id=queued.job_id,
            status=queued.status,
            timeline_revision=queued.timeline_revision,
        )


GENERATE = (
    "An `application/json` body (`StoryImageRequest`) asks Epistrel to **generate** the image "
    "(FR-IMG-8): `position`, `audience_viewpoint` and `camera_pov` as for an upload (the camera's "
    "character must also be perceived there by the audience viewpoint), and optionally the "
    "consumer's own `prompt` (1 to 4000 characters) with its `depicted_subjects` (then required; "
    "`[]` declares none), `caption` and `alt_text` (stored as given, never generated). Without "
    "`prompt`, Epistrel builds the prompt and the reference images from what the audience "
    "viewpoint perceived at the position, and nothing else: its filtered beats of that turn, its "
    "own location as it holds it, and the characters it perceives as present, each by perceived "
    "appearance and apparent age band; a character whose identity the viewpoint does not know "
    "contributes no visual identity, seed or reference image, and `restricted` elements only "
    "reach `omniscient` images. Under Lite the prompt is built from the message the player was "
    "shown. Every story image prompt, a consumer's included, carries the scenario's visual style "
    "and the story rating's image guidance (a consumer's as sections after its text). The "
    "prompt, negative prompt, caption and alt text then pass the protected-content match for the "
    "audience viewpoint (no name, alias, protected term or unperceived beat's words it does not "
    "hold; a built prompt is rewritten once), and the image-prompt policies. One image per "
    "request. 202 `{job_id, status: queued}` with `Location: /image-jobs/{id}`; the job stores "
    "the prompt it was enqueued with and sends exactly that. Refusals, in order, with nothing "
    "written: 404; 409 `not_configured` (no image adapter), `image_check_unconfigured`; 422 "
    "`subjects_required` (a `prompt` without `depicted_subjects`), `subjects_not_allowed` "
    "(`depicted_subjects` without a `prompt`), `position_invalid`, `viewpoint_invalid`, "
    "`camera_pov_invalid`, `camera_pov_not_present`, `subject_invalid` (a declared subject "
    "that is no character of the story; 409 `erased_subject_referenced` for an erased one), "
    "`viewpoint_perceived_nothing`, "
    "`viewpoint_protected_content` (`details: {field}`, never the term), `guardrail_violation`; "
    "409 `image_limit_reached`, 422 `images_disabled`; 503 `image_prompt_unavailable`. The "
    "image is stored `source: generated` with `viewpoint_basis: filtered_inputs`, or "
    "`generated_from_consumer_prompt` with `protected_term_match`."
)


@router.post(
    "/{story_id}/images",
    status_code=http_status.HTTP_201_CREATED,
    response_model=StoryImageUploadedBody,
    responses={
        202: {
            "model": StoryImageRequestedBody,
            "description": "A generation request (`application/json`) was queued: follow the "
            "job at the `Location`.",
        }
    },
    operation_id="upload_story_image",
    summary="Generate or upload a story image at a narrative position for one audience viewpoint",
    description=(
        f"{IMAGE_VIEWPOINT_GUARANTEE}\n\n{GENERATE}\n\n"
        "A `multipart/form-data` body **uploads** the image: the `metadata` part (JSON: "
        "`position` `{turn, beat}` after a beat of an active turn, the player input being beat 1,"
        " or `{slot_after_turn}` in a new inter-turn slot after the story's last completed turn; "
        "`audience_viewpoint` `character:<id>` or `omniscient`; `camera_pov` `{kind}` (default "
        "`third_person`; `over_the_shoulder`/`first_person` name a `character_id` present there; "
        "composition only, it grants no access); `depicted_subjects` (required, `[]` declares "
        "none; the story's `character` ids only); optional `caption`, `alt_text`, "
        "`content_rating_id`, default the story's), then the `file` part (an image). "
        "Refusals, in order: 422 `position_invalid` (an inactive turn, a beat it lacks, or a "
        "slot after any turn but the last completed one), "
        "`viewpoint_invalid`, `camera_pov_invalid`, `camera_pov_not_present`, "
        "`subjects_required`, `subject_invalid` (a declared subject that is no character of "
        "the story), 409 `erased_subject_referenced`; then the image goes through `POST /media`'s "
        "upload path (415, 413, `rating_retired`, with an `image_check` model 422 "
        "`guardrail_violation` or 503 `image_check_unavailable`, the moderation hook's "
        "refusals), and nothing is stored on any of them. The asset is stored "
        "`source: uploaded`, `viewpoint_basis: none`, owned by the "
        "story's owner, and the image is placed in one operation; a looser rating than the "
        "story's answers a `rating_looser` warning. One image per request. Only reads for "
        f"its audience viewpoint receive it, in place.{_GATE} {CHECK_STATEMENTS}"
    ),
)
async def upload_story_image(
    story_id: StoryId, request: Request, response: Response, ctx: ContextDep, engine: EngineDep
) -> StoryImageUploadedBody | JSONResponse:
    # The story's gates (importing, erasure, the expected revision) answer before the body is
    # read, as every other story route's do.
    await engine.check_expected_revision(ctx, story_id)
    kind, _ = parse_options_header(request.headers.get("content-type"))
    if kind == b"application/json":
        body = parse_request(await request.body())
        queued = await engine.request_story_image(ctx, story_id, body)
        answer = answered(
            StoryImageRequestedBody.of(queued), ctx, response, replayed=queued.replayed
        )
        headers: dict[str, Any] = dict(response.headers)
        headers["Location"] = queued.location
        return JSONResponse(answer.model_dump(mode="json"), status_code=202, headers=headers)
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
