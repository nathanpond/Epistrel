"""``POST /subject-images``: safety-checked images of a subject outside any story or catalog
(FR-IMG-14; #830).

A consumer describes a subject that exists in no Epistrel story or catalog (its appearance, the
true age it declares and a label naming who it is) and gets ``n`` images of it through the
catalog image path's safety checks. The answer is 202 ``{job_id, status: queued}`` with
``Location: /image-jobs/{id}``; ``GET /image-jobs/{id}`` reports the job and, once it is
``completed``, its ``asset_ids`` and ``failures``. Each stored image is a live media asset owned by
the caller, kept until it is deleted (``DELETE /media/{id}``) or its owner is offboarded, and
usable as a story character's image. The route needs only the service credential: ownership
gates nothing (FR-AUTH-8). One ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi import status as http_status

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.orchestrator.images.targets.subject import (
    SUBJECT_N_MAX,
    SubjectImageRequest,
    SubjectImagesQueued,
)

router = APIRouter(prefix="/subject-images", tags=["images"])


class SubjectImagesAccepted(DeploymentMutationResponse):
    """A subject image request's answer (202): the job to follow at ``Location``."""

    job_id: str
    status: str

    @classmethod
    def of(cls, queued: SubjectImagesQueued) -> SubjectImagesAccepted:
        return cls(
            job_id=queued.job_id, status=queued.status, config_revision=queued.config_revision
        )


GENERATE = (
    "Generates `n` images (1 to "
    f"{SUBJECT_N_MAX}, default 1) of a **subject that is in no story or catalog** (FR-IMG-14): "
    "`subject.description` is its appearance (at most 4,000 characters), `subject.true_age` the "
    "age the consumer declares (0 to 150) and `subject.label` who it is (at most 120 printable "
    "characters, not unique); the label and true age are the depicted-subject declaration. "
    "`style` (at most 1,000 characters) and `content_rating` (default: the deployment's "
    "default rating, read now) are optional; no seed or size is taken. The prompt is written "
    "by the `image_prompt` purpose from the description, the true age's apparent age band, "
    "the style and the rating's image guidance (the image model never receives the true age), "
    "then checked **before any generation**, together with the description and style, against "
    "the global and the rating's image-prompt policies, the seeded minors policy included, "
    "judged by the declared true age (FR-IMG-3, FR-GUARD-11). Every output is classified for "
    "figures and passes the image-output check in private staging; one that fails is never "
    "stored. 202 `{job_id, status: queued}` with `Location: /image-jobs/{id}`: the job ends "
    "`completed` with `asset_ids` and `failures` (`[{index, reason}]`, codes only) when at "
    "least one image passed, or `failed` (`guardrail_violation`) when none did. Each stored "
    "image is a **live** media asset owned by the caller with `origin: subject`, `source: "
    "generated`, the rating's current version and `depicted_subjects` `[{kind: subject, id, "
    "label, true_age}]`; the media sweep never purges it while it is live (ADR-186): it stays "
    "until `DELETE /media/{id}` or its owner's offboarding (whose `media` action applies), "
    "and it can be named in a story character's `images`.\n\n"
    "Refused before any job, reservation or provider call: 422 `validation_error` for a body "
    "outside these bounds or an unknown or retired `content_rating`; 409 `not_configured` "
    "without an image adapter; 409 `image_check_unconfigured` while an image-output policy "
    "applies and `image_check` is unset; 503 `image_prompt_unavailable`; 422 "
    "`guardrail_violation` when the prompt, description or style violates an image-prompt "
    "policy (its audit row names the subject's id, never its label), 503 `model_unavailable` "
    "when the check cannot reach its model; 409 `image_limit_reached` or 422 "
    "`images_disabled` from the deployment's daily image limit. Idempotent under "
    "`Idempotency-Key` (deployment-scoped). The description, label and style never reach an "
    "operational log, metric or trace."
)


@router.post(
    "",
    response_model=SubjectImagesAccepted,
    status_code=http_status.HTTP_202_ACCEPTED,
    operation_id="request_subject_images",
    summary="Generate images of a subject outside any story or catalog",
    description=GENERATE,
)
async def request_subject_images(
    body: SubjectImageRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> SubjectImagesAccepted:
    queued = await engine.request_subject_images(ctx, body)
    response.headers["Location"] = queued.location
    return answered(SubjectImagesAccepted.of(queued), ctx, response, replayed=queued.replayed)
