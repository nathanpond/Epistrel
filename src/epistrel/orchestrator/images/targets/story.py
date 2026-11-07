"""The ``story`` image job target: a generated story image (FR-IMG-5,8,10; #263).

A story job's ``target`` is ``{story_id, audience_viewpoint, camera_pov, content_rating_id}``
and its ``origin`` ``{story_id, operation_id, turn, variant, position, viewpoint,
timeline_revision, request_position}`` (docs/02 §4.13): ``operation_id`` is the operation the
image will be placed in, a ULID minted in the enqueue transaction; ``position`` is the
five-column position the safety path reads (``core.images.safety.image_position``); and
``request_position`` is the request's own (``{turn, beat}`` or ``{slot_after_turn}``). The job's
``params`` also fix, at enqueue, the image's ``source`` (``generated`` or
``generated_from_consumer_prompt``), its ``viewpoint_basis`` (``filtered_inputs`` or
``protected_term_match``) and its final ``depicted_subjects``; promotion copies them verbatim and
never recomputes them. The worker sends exactly the stored prompt.

- ``validate_enqueue``: the placement's checks again (``core.images.story_images.place_at``: an
  active position, the audience, the camera), 422 as at the request.
- ``revalidate`` (at claim and before each provider call): the story still exists, else
  ``TargetGone``. The rest of the story's revalidation (an inactive turn, a pause, an amendment,
  the policies, the erasure guard) is #264's.
- ``promote`` (inside the promotion's transaction): the story row ``FOR SHARE`` (gone:
  ``TargetGone``), then the output's ``media_assets`` row (``live``, owned by the story's owner,
  under its rating's current version, with the job's source, viewpoint basis and depicted
  subjects) and the ``story_images`` row, under a ``story_image_added`` event in the job's own
  operation (anchored as an upload's: a beat position's ``story_image`` operation at its turn, a
  slot's inter-turn operation after the last completed turn). A timeline that moved past the
  position ends the job ``target_gone`` here (interim until #264's ``stale``). The job's
  ``result`` is ``{asset_ids, image_ids}``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import PositionInvalid
from epistrel.core.images import story_images
from epistrel.core.images.store import ImageJob, set_result
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.tables import content_ratings, stories
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.core.tables.media import MediaSource, MediaState
from epistrel.core.tables.story_images import ViewpointBasis
from epistrel.orchestrator.images import jobs

FIELD = "body"
"""Where a generation request's fields are reported (its JSON body)."""
JOB_LOCATION = jobs.JOB_LOCATION


@dataclass(frozen=True, slots=True)
class StoryImageQueued:
    """A story image generation request's answer: 202 ``{job_id, status: queued}`` with
    ``Location: /image-jobs/{id}``, and the story's unchanged ``timeline_revision`` (the image is
    placed, in its own operation, when the job promotes it). A replay of the same key answers
    the same job (``replayed``)."""

    job_id: str
    timeline_revision: int
    status: str = ImageJobStatus.QUEUED.value
    replayed: bool = False

    @property
    def location(self) -> str:
        return JOB_LOCATION.format(self.job_id)


def request_position(
    origin: Mapping[str, Any],
) -> story_images.BeatPosition | story_images.SlotPosition:
    """The request's position from a job's origin."""
    found = origin.get("request_position") or {}
    if "slot_after_turn" in found:
        return story_images.SlotPosition(slot_after_turn=int(found["slot_after_turn"]))
    return story_images.BeatPosition(turn=int(found["turn"]), beat=int(found["beat"]))


def origin_of(
    placement: story_images.Placement,
    *,
    operation_id: str,
    timeline_revision: int,
    position: story_images.BeatPosition | story_images.SlotPosition,
) -> dict[str, Any]:
    """A story job's origin (module docstring)."""
    return {
        "story_id": placement.story_id,
        "operation_id": operation_id,
        "turn": placement.turn,
        "variant": placement.variant,
        "position": {
            "turn": placement.turn,
            "variant": placement.variant,
            "slot_k": 1 if placement.slot else 0,
            "beat": 0 if placement.slot else placement.beat,
            "step": 0,
        },
        "viewpoint": placement.audience_viewpoint,
        "timeline_revision": timeline_revision,
        "request_position": position.model_dump(mode="json"),
    }


def target_of(placement: story_images.Placement) -> dict[str, Any]:
    """A story job's target (module docstring)."""
    return {
        "story_id": placement.story_id,
        "audience_viewpoint": placement.audience_viewpoint,
        "camera_pov": dict(placement.camera_pov),
        "content_rating_id": placement.content_rating_id,
    }


async def _owner(conn: AsyncConnection, story_id: str, *, share: bool = False) -> str | None:
    query = select(stories.c.owner_principal).where(stories.c.id == story_id)
    if share:
        query = query.with_for_update(read=True)
    found = await conn.scalar(query)
    return None if found is None else str(found)


async def _rating_version(conn: AsyncConnection, rating_id: Any) -> str:
    """The current version of the image's rating (a retired rating still has one), else
    Everyone's."""
    found = None
    if isinstance(rating_id, str):
        found = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == rating_id)
        )
    if found is None:
        found = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == EVERYONE_ID)
        )
    return str(found)


def _placement(job: ImageJob, owner: str) -> story_images.Placement:
    origin, target = job.origin, job.target
    position = request_position(origin)
    slot = isinstance(position, story_images.SlotPosition)
    return story_images.Placement(
        story_id=str(origin["story_id"]),
        turn=int(origin["turn"]),
        variant=int(origin.get("variant") or 0),
        beat=0 if slot else int(origin["position"]["beat"]),
        slot=slot,
        audience_viewpoint=str(target["audience_viewpoint"]),
        camera_pov=dict(target.get("camera_pov") or story_images.DEFAULT_CAMERA),
        content_rating_id=str(target.get("content_rating_id") or EVERYONE_ID),
        owner=owner,
    )


class StoryTarget:
    """The story target (module docstring)."""

    success_status = ImageJobStatus.PROMOTED

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None:
        await story_images.place_at(
            conn,
            str(origin["story_id"]),
            request_position(origin),
            str(target["audience_viewpoint"]),
            target.get("camera_pov"),
            at=FIELD,
            content_rating_id=target.get("content_rating_id"),
        )

    async def revalidate(self, conn: AsyncConnection, job: ImageJob) -> None:
        story_id = str(job.origin["story_id"])
        if await _owner(conn, story_id) is None:
            raise jobs.TargetGone(f"story {story_id}")

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[jobs.PromotableOutput]
    ) -> list[str]:
        story_id = str(job.origin["story_id"])
        owner = await _owner(conn, story_id, share=True)
        if owner is None:
            raise jobs.TargetGone(f"story {story_id}")
        placement = _placement(job, owner)
        source = MediaSource(str(job.params.get("source") or MediaSource.GENERATED.value))
        basis = ViewpointBasis(
            str(job.params.get("viewpoint_basis") or ViewpointBasis.FILTERED_INPUTS.value)
        )
        version = await _rating_version(conn, placement.content_rating_id)
        output = outputs[0]  # a story job generates one image (``n = 1``)
        asset_id = await jobs.insert_output_asset(
            conn,
            job,
            output,
            state=MediaState.LIVE,
            owner=owner,
            content_rating_version_id=version,
            depicted_subjects=list(job.params.get("depicted_subjects") or ()),
            source=source,
            viewpoint_basis=basis.value,
        )
        try:
            image_id = await story_images.place_generated(
                conn,
                placement,
                asset_id,
                operation_id=str(job.origin["operation_id"]),
                principal=job.principal,
                prompt=job.prompt,
                job_id=job.id,
                source=source,
                viewpoint_basis=basis,
            )
        except PositionInvalid:
            # Interim until #264's revalidation ends such a job ``stale``: the position the
            # image was made for is no longer on the timeline, so nothing is placed.
            raise jobs.TargetGone(f"story {story_id} position") from None
        await set_result(conn, job.id, {"asset_ids": [asset_id], "image_ids": [image_id]})
        return [asset_id]

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None:
        return None

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]:
        return "asset_ids", [str(a) for a in job.asset_ids]


TARGET = StoryTarget()
jobs.register_target(ImageJobKind.STORY, TARGET)
"""Registered on import (the Engine imports this module): tests swap it with
``testing.images.registered_target``, which restores it."""


__all__ = [
    "FIELD",
    "JOB_LOCATION",
    "TARGET",
    "StoryImageQueued",
    "StoryTarget",
    "origin_of",
    "request_position",
    "target_of",
]
