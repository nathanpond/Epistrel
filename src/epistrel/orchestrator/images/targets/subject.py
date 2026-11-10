"""The ``subject`` image job target: images of a consumer-described subject that is in no story
or catalog (FR-IMG-14; #830).

``POST /subject-images`` (``Engine.request_subject_images``) describes the subject (its
appearance, the true age the consumer declares and a label naming who it is), an optional style,
an optional content rating (the deployment default when omitted) and ``n`` (1 to 4). Each
request mints a subject id (a ULID). A subject job's ``target`` is ``{subject_id,
content_rating_id, content_rating_version_id}`` (the rating's version current at the request)
and its ``origin`` ``{subject_id, label, true_age, description, style}``: the declaration the
safety path resolves inline (``core.images.safety.InlineSubject``), with the consumer's
description and style checked beside the built prompt.

**The prompt** is the catalog target's: ``image_prompt`` writes it from ``subject_view`` (the
description with authored ages redacted, the true age's apparent age band, the style and the
rating's image guidance), never from the true age. **The safety path** is the catalog path
(FR-IMG-3, FR-GUARD-11): #257's prompt check before any generation with the declared true age
and the global and the rating's policies, figure classification (the subject is a resolved label
with its true age), and #258's output check on every output. A failing output is never stored.

- ``validate_enqueue``: nothing beyond the request's own validation.
- ``revalidate`` (at claim and before each provider call): the owner being offboarded (an
  offboarding of the requester asked since the job, whose media action is not ``keep``) ends
  the job ``target_gone`` with no provider call.
- ``promote`` (inside the promotion's transaction): per passing output a **live**
  ``media_assets`` row owned by the requester (``source = generated``, ``origin = subject``,
  the rating's version, ``depicted_subjects`` ``[{kind: subject, id, label, true_age}]``), and
  the job's ``result`` ``{asset_ids, failures}``: each output index of ``n`` not stored, with
  ``reason`` ``guardrail_violation`` (its output check failed) or ``provider_empty_result`` (the
  provider returned no usable image for it). The job ends ``completed``; with no passing output
  after the retry budget it ends ``failed`` (``guardrail_violation``), as a catalog job does.
- ``result_ids``: ``asset_ids``.

The assets are kept until they are deleted or their owner is offboarded: the media sweep never
purges a live subject image (ADR-186). The label and description are consumer content: they
never reach a log line, a metric or an audit row (invariant 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.epistemic.context import age_band
from epistrel.core.errors import InvalidRequest
from epistrel.core.images.limits import SUBJECT_ENTRY_KIND
from epistrel.core.images.perceived import redact_ages
from epistrel.core.images.store import ImageJob, set_result
from epistrel.core.tables import content_rating_versions, content_ratings, deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.core.tables.media import MediaOrigin, MediaState
from epistrel.core.tables.offboarding import OffboardingStatus, offboarding_jobs
from epistrel.orchestrator.images import jobs
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.prompt_builder import (
    PerceivedSubject,
    PerceivedView,
    resolve_dimensions,
)

JOB_LOCATION = jobs.JOB_LOCATION
DESCRIPTION_MAX = 4_000
LABEL_MAX = 120
STYLE_MAX = 1_000
TRUE_AGE_MAX = 150
SUBJECT_N_MAX = 4
PURPOSE = "subject"
"""The prompt builder's purpose for a subject image (``prompt_builder.PURPOSE_TEXT``)."""
SCENE = "single-subject portrait, plain background"
SUBJECT_KIND = "subject"
"""``depicted_subjects[].kind`` of a subject image's declaration."""
FAILED_CHECK = "guardrail_violation"
NO_OUTPUT = "provider_empty_result"


def _text(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _label(value: str) -> str:
    if not value.strip() or not value.isprintable():
        raise ValueError("must be printable text, not blank")
    return value


class SubjectSpec(BaseModel):
    """The subject an image depicts: its appearance (``description``, at most 4,000
    characters), the ``true_age`` the consumer declares (0 to 150; the safety path judges by
    it, the image model never sees it) and a ``label`` naming who it is (at most 120 printable
    characters, not unique). ``label`` and ``true_age`` are the depicted-subject declaration."""

    model_config = ConfigDict(extra="forbid")

    description: Annotated[
        str, Field(min_length=1, max_length=DESCRIPTION_MAX), AfterValidator(_text)
    ]
    true_age: Annotated[StrictInt, Field(ge=0, le=TRUE_AGE_MAX)]
    label: Annotated[str, Field(min_length=1, max_length=LABEL_MAX), AfterValidator(_label)]


class SubjectImageRequest(BaseModel):
    """``POST /subject-images`` (FR-IMG-14): the ``subject``, an optional ``style`` (free text,
    at most 1,000 characters, checked with the prompt), an optional ``content_rating`` (the
    deployment default when omitted) and ``n`` (1 to 4, default 1). No seed or size."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "subject": {
                        "description": "Weathered fisherman, grey beard, yellow oilskin coat.",
                        "true_age": 52,
                        "label": "Contestant 7",
                    },
                    "style": "oil painting, warm light",
                    "n": 2,
                }
            ]
        },
    )

    subject: SubjectSpec
    style: Annotated[str, Field(min_length=1, max_length=STYLE_MAX)] | None = None
    content_rating: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    n: Annotated[StrictInt, Field(ge=1, le=SUBJECT_N_MAX)] = 1


@dataclass(frozen=True, slots=True)
class SubjectImagesQueued:
    """A subject image request's answer: 202 ``{job_id, status: queued}`` with ``Location:
    /image-jobs/{id}``, and the deployment's unchanged ``config_revision``. A replay of the same
    key answers the same job (``replayed``)."""

    job_id: str
    config_revision: int
    status: str = ImageJobStatus.QUEUED.value
    replayed: bool = False

    @property
    def location(self) -> str:
        return JOB_LOCATION.format(self.job_id)


@dataclass(frozen=True, slots=True)
class SubjectRating:
    """The rating a subject image is made under: its id, its version current at the request
    and that version's image guidance."""

    rating_id: str
    version_id: str
    guidance: str


async def subject_rating(conn: AsyncConnection, rating_id: str | None) -> SubjectRating:
    """The named rating, else the deployment default read now, at its current version; 422
    ``validation_error`` at ``body.content_rating`` when it is unknown or retired."""
    if rating_id is None:
        rating_id = str(
            (
                await conn.execute(
                    select(deployment_state.c.default_content_rating_id).where(
                        deployment_state.c.id == DEPLOYMENT_ROW_ID
                    )
                )
            ).scalar_one()
        )
    row = (
        await conn.execute(
            select(
                content_ratings.c.retired_at,
                content_ratings.c.current_version_id,
                content_rating_versions.c.image_guidance,
            )
            .join(
                content_rating_versions,
                content_rating_versions.c.version_id == content_ratings.c.current_version_id,
            )
            .where(content_ratings.c.id == rating_id)
        )
    ).one_or_none()
    if row is None or row.retired_at is not None:
        reason = "unknown" if row is None else "retired"
        raise InvalidRequest(
            f"content rating {rating_id} cannot be used ({reason})",
            {"field": "body.content_rating", "reason": reason, "rating_id": rating_id},
        )
    return SubjectRating(rating_id, str(row.current_version_id), str(row.image_guidance or ""))


def subject_view(
    request: SubjectImageRequest,
    guidance: str,
    deployment: ImageGenerationParams | None = None,
) -> PerceivedView:
    """What ``image_prompt`` writes a subject image's prompt from: the description with
    authored ages redacted, the true age's apparent age band (never the age), the style and the
    rating's image guidance. 422 at ``body.subject.description`` when redaction leaves no
    appearance."""
    subject = request.subject
    description = redact_ages(subject.description, subject.true_age).strip()
    if not description:
        raise InvalidRequest(
            "the subject's description states no appearance once its ages are left out",
            {"field": "body.subject.description"},
        )
    band = age_band(subject.true_age)
    assert band is not None  # noqa: S101 - a true age has a band
    size, ratio = resolve_dimensions(PURPOSE, None, None, deployment)
    return PerceivedView(
        subjects=(
            PerceivedSubject(
                description=description,
                age_band=band,
                base_appearance_prompt=None,
                reference_image_ids=(),
                seed=None,
            ),
        ),
        scene=SCENE,
        visual_style=request.style,
        rating_image_guidance=guidance,
        purpose=PURPOSE,
        size=size,
        aspect_ratio=ratio,
    )


def job_target(subject_id: str, rating_id: str, version_id: str) -> dict[str, Any]:
    return {
        "subject_id": subject_id,
        "content_rating_id": rating_id,
        "content_rating_version_id": version_id,
    }


def job_origin(subject_id: str, request: SubjectImageRequest) -> dict[str, Any]:
    return {
        "subject_id": subject_id,
        "label": request.subject.label,
        "true_age": request.subject.true_age,
        "description": request.subject.description,
        "style": request.style,
    }


def declaration(job: ImageJob) -> dict[str, Any]:
    """The asset's ``depicted_subjects`` entry: ``{kind: subject, id, label, true_age}``."""
    return {
        "kind": SUBJECT_KIND,
        "id": str(job.origin["subject_id"]),
        "label": str(job.origin["label"]),
        "true_age": int(job.origin["true_age"]),
    }


def failures(job: ImageJob, stored: Sequence[int]) -> list[dict[str, Any]]:
    """Each output index of the job's ``n`` that was not stored, with its reason code."""
    violated = {
        r.get("output")
        for r in job.policy_results
        if r.get("attempt_token") == job.attempt_token and r.get("result") == "violation"
    }
    kept = set(stored)
    return [
        {"index": index, "reason": FAILED_CHECK if index in violated else NO_OUTPUT}
        for index in range(jobs.requested_outputs(job))
        if index not in kept
    ]


async def owner_leaving(conn: AsyncConnection, job: ImageJob) -> bool:
    """Whether an offboarding of the job's requester, asked since the job, takes or retires its
    media (its media action is not ``keep``)."""
    found = await conn.scalar(
        select(offboarding_jobs.c.id)
        .where(
            offboarding_jobs.c.principal == job.principal,
            offboarding_jobs.c.created_at >= job.created_at,
            offboarding_jobs.c.status != OffboardingStatus.FAILED.value,
            offboarding_jobs.c.actions["media"].astext != "keep",
        )
        .limit(1)
    )
    return found is not None


class SubjectTarget:
    """The subject target (module docstring)."""

    success_status = ImageJobStatus.COMPLETED

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None:
        return None

    async def revalidate(self, conn: AsyncConnection, job: ImageJob) -> None:
        if await owner_leaving(conn, job):
            raise jobs.TargetGone(f"{SUBJECT_ENTRY_KIND} {job.target.get('subject_id')}")

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[jobs.PromotableOutput]
    ) -> list[str]:
        if await owner_leaving(conn, job):
            raise jobs.TargetGone(f"{SUBJECT_ENTRY_KIND} {job.target.get('subject_id')}")
        depicted = [declaration(job)]
        asset_ids = [
            await jobs.insert_output_asset(
                conn,
                job,
                output,
                state=MediaState.LIVE,
                owner=job.principal,
                content_rating_version_id=str(job.target["content_rating_version_id"]),
                depicted_subjects=depicted,
                origin=MediaOrigin.SUBJECT,
            )
            for output in outputs
        ]
        await set_result(
            conn,
            job.id,
            {"asset_ids": asset_ids, "failures": failures(job, [o.index for o in outputs])},
        )
        return asset_ids

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None:
        return None

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]:
        found = (job.result or {}).get("asset_ids") or job.asset_ids or []
        return "asset_ids", [str(a) for a in found]


TARGET = SubjectTarget()
jobs.register_target(ImageJobKind.SUBJECT, TARGET)
"""Registered on import (the Engine imports this module)."""


__all__ = [
    "JOB_LOCATION",
    "SUBJECT_N_MAX",
    "TARGET",
    "SubjectImageRequest",
    "SubjectImagesQueued",
    "SubjectRating",
    "SubjectSpec",
    "SubjectTarget",
    "declaration",
    "failures",
    "job_origin",
    "job_target",
    "owner_leaving",
    "subject_rating",
    "subject_view",
]
