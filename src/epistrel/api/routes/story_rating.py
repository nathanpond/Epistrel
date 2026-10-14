"""A story's content rating (FR-RATE-6,7; FR-API-6; #118).

- ``PUT /stories/{id}/content-rating``: change the story's rating. Relaxing applies at once
  (200); tightening pauses the story and starts its history check (202 with ``Location``).
- ``GET /stories/{id}/content-rating/checks/{job_id}``: a check's status, progress and, once
  refused, its violations.

The story's current rating is read with ``GET /stories/{id}`` (``content_rating``). Every
handler makes one ``Engine`` call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, JobId, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ratings.change import (
    RatingApplied,
    RatingChangeRequest,
    RatingCheckStarted,
    RatingCheckView,
)

router = APIRouter(tags=["settings"])

CHECK_PATH = "/stories/{}/content-rating/checks/{}"


class RatingAppliedBody(MutationResponse):
    """The change applied (a relaxing one, or the rating the story already has: then
    ``changed`` is false and nothing was written): the story's rating and its version, its
    ``config_revision`` and its unchanged ``timeline_revision``."""

    applied: bool
    changed: bool
    content_rating_id: str
    rating_version: str
    config_revision: int

    @classmethod
    def of(cls, result: RatingApplied) -> RatingAppliedBody:
        return cls(
            applied=result.applied,
            changed=result.changed,
            content_rating_id=result.content_rating_id,
            rating_version=result.rating_version,
            config_revision=result.config_revision,
            timeline_revision=result.timeline_revision,
        )


class RatingCheckStartedBody(MutationResponse):
    """A tightening's check started (202): the story is paused until it ends. ``job_id``
    names the check (read it at the ``Location``), ``operation_id`` the ``control`` operation
    that paused the story."""

    job_id: str
    operation_id: str
    status: str
    config_revision: int

    @classmethod
    def of(cls, result: RatingCheckStarted) -> RatingCheckStartedBody:
        return cls(
            job_id=result.job_id,
            operation_id=result.operation_id,
            status=result.status,
            config_revision=result.config_revision,
            timeline_revision=result.timeline_revision,
        )


class CheckProgressBody(BaseModel):
    """How many of the history's records the check has checked, of how many."""

    done: int
    total: int


class CheckTargetBody(BaseModel):
    """The rating the check evaluates and the version id it evaluates."""

    rating_id: str
    version: str


class CheckViolationBody(BaseModel):
    """One record breaking one policy: its ``position`` (``{turn, variant, beat}``, or ``{kind,
    id}`` for a seed, pinned memory, the custom instructions or an image), the policy's id and
    name, and a ``code`` for a record that could not be checked. Never the text."""

    position: dict[str, Any]
    policy_id: str | None
    policy_name: str | None
    code: str | None = None


class RatingCheckBody(BaseModel):
    """A rating check: see ``GET /stories/{id}/content-rating/checks/{job_id}``."""

    job_id: str
    story_id: str
    status: str
    progress: CheckProgressBody
    started_at: datetime
    started_by: str
    finished_at: datetime | None
    target: CheckTargetBody
    from_rating_id: str
    operation_id: str | None
    rating_version: str | None
    config_revision: int | None
    violations: list[CheckViolationBody]
    error: dict[str, Any] | None

    @classmethod
    def of(cls, view: RatingCheckView) -> RatingCheckBody:
        return cls(
            job_id=view.job_id,
            story_id=view.story_id,
            status=view.status,
            progress=CheckProgressBody(**view.progress),
            started_at=view.started_at,
            started_by=view.started_by,
            finished_at=view.finished_at,
            target=CheckTargetBody(**view.target),
            from_rating_id=view.from_rating_id,
            operation_id=view.operation_id,
            rating_version=view.rating_version,
            config_revision=view.config_revision,
            violations=[CheckViolationBody(**v) for v in view.violations],
            error=view.error,
        )


SET_DESCRIPTION = (
    "Change the story's content rating to `content_rating_id` (FR-RATE-6,7). The two ratings "
    "are compared by their effective guardrails (live policies, inheritance included), never "
    "by their order. **Relaxing**, a target whose guardrails are a subset of the current "
    "rating's (an equal set included), applies at once with no content check: 200 `{applied: "
    "true, changed, content_rating_id, rating_version}`, recorded as the interpretive "
    "configuration change `content_rating_id` with the acting principal "
    "(`GET /stories/{id}/config-changes`). It moves the story's `config_revision`, and every "
    "operation that commits after it runs under the new rating: a turn still generating is "
    "re-checked under it before it commits. Naming the story's current rating changes nothing "
    "(`changed: false`). **Tightening**, a target that adds any policy (a sideways move "
    "included), pauses the story and checks its whole active history against the target's "
    "effective policies in the background: the player inputs and rendered beats of every "
    "active turn, generated or ingested, the pinned memories and the custom instructions. "
    "Deterministic policies run first, then model-assisted ones, outside any turn budget, "
    "within the deployment's `rating_check_model_calls_per_record`. It answers 202 `{job_id, "
    "operation_id, status: checking}` with `Location` (the check). While it runs, every call "
    "that would change the story's truth answers 409 `story_paused` with the check's "
    "progress, and reads are served. A turn already in flight commits first. If every record "
    "passes, the rating changes in one transaction and every active turn records the new "
    "guardrail set; otherwise nothing changes and the check lists each violation by position "
    "and policy. The story resumes either way. 409 `story_archived`, `story_paused` (a check "
    "already runs) or `revision_conflict`; 422 `rating_retired` for a rating that cannot be "
    "assigned."
)
CHECK_DESCRIPTION = (
    "A tightening's check (FR-RATE-7): `status` (`checking` while the story is paused, then "
    "`applied`, `refused` or `failed`), `progress` (`{done, total}` records), when and by "
    "whom it was started, and the `target` (`{rating_id, version}`). Applied: the "
    "`rating_version` and the story's `config_revision` it left. Refused: `violations`, each "
    "`{position, policy_id, policy_name}` in position order (turns by `{turn, variant, beat}` "
    "first, then `{kind, id}` for seeds, pinned memories, the custom instructions and images), "
    "never the text; the rating did not change. Failed: `error {class, message}` (a model that "
    "could not answer on the last attempt); the rating did not change. A check is kept "
    "`rating_check_retention_days` (30) after it ends. A job that is not one of the story's "
    "checks is 404."
)


@router.put(
    "/stories/{story_id}/content-rating",
    response_model=RatingAppliedBody,
    responses={
        202: {
            "model": RatingCheckStartedBody,
            "description": "Tightening: the story is paused while its history is checked "
            "(read the check at the `Location`).",
        }
    },
    operation_id="set_content_rating",
    summary="Change a story's content rating",
    description=SET_DESCRIPTION,
)
async def set_content_rating(
    story_id: StoryId,
    body: RatingChangeRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> RatingAppliedBody | JSONResponse:
    result = await engine.set_content_rating(ctx, story_id, body)
    if isinstance(result, RatingCheckStarted):
        started = answered(
            RatingCheckStartedBody.of(result), ctx, response, replayed=result.replayed
        )
        headers: dict[str, Any] = dict(response.headers)
        headers["Location"] = CHECK_PATH.format(story_id, result.job_id)
        return JSONResponse(started.model_dump(mode="json"), status_code=202, headers=headers)
    return answered(RatingAppliedBody.of(result), ctx, response, replayed=result.replayed)


@router.get(
    "/stories/{story_id}/content-rating/checks/{job_id}",
    response_model=RatingCheckBody,
    operation_id="get_rating_check",
    summary="Read a rating check",
    description=CHECK_DESCRIPTION,
)
async def get_rating_check(
    story_id: StoryId, job_id: JobId, ctx: ContextDep, engine: EngineDep
) -> RatingCheckBody:
    return RatingCheckBody.of(await engine.get_rating_check(ctx, story_id, job_id))


__all__ = [
    "RatingAppliedBody",
    "RatingCheckBody",
    "RatingCheckStartedBody",
    "router",
]
