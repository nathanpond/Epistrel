"""Image generation as checked background jobs that promote only from private staging
(FR-IMG-3,4; docs/02 §4.13; #256).

**Enqueue.** ``enqueue_image_job`` is ``prepare_image_job`` then ``insert_image_job``:

- ``prepare_image_job`` (no transaction) validates the request, calls the target's
  ``validate_enqueue``, ``require_image_checker`` (409 ``not_configured`` without an adapter,
  then 409 ``image_check_unconfigured`` while an image-output policy applies and ``image_check``
  is unset) and the checks' ``check_prompt`` (#257's ``safety_checks``: ``422
  guardrail_violation``, its audit row aside), which answers the safety context the job keeps
  (``image_jobs.safety_context``). Nothing else is written on any refusal.
- ``insert_image_job`` joins the caller's transaction (#263's reserve-and-enqueue, #265's turn
  commit): the ``image_jobs`` row, its first ``image_reservations`` row (``prepared``), the
  ``jobs`` row (kind ``image_job``, payload ``{image_job_id}``), then ``PRE_COMMIT_HOOKS`` (empty
  here; #262 registers ``reserve``). #61's key lookup and store stay in the route or library
  call that wraps it (``IMAGE_JOB_CODEC``); the job only records the key.

**Run** (``run_image_job``, the ``image_job`` handler). Each step commits on its own, and no
transaction stays open across an adapter or checker call (#70's heartbeat keeps the lease):
``queued → generating`` (the attempt's reservation ``sending``, then the adapter's ``submit``,
or ``submit`` and ``collect`` for an ``async`` adapter), each output written to private
staging ``tmp/images/<job_id>/<attempt_token>/<k>``, ``→ checking``, the checks'
``check_output`` per output in order, a violating output deleted from staging and audited,
then the passing outputs promoted. Promotion copies each passing output to its content-addressed
key first, then runs one transaction that compares the job's attempt token (a fenced or
cancelled worker never promotes) and calls the target's ``promote``, which inserts the
``media_assets`` rows (``insert_output_asset``) and attaches them. A content-addressed blob
whose transaction did not commit is left to the media sweep's orphan step.

**Retries.** Only when no output of an attempt passed: under a new attempt token and
reservation, inside the same claimed run, while ``image_retry_budget`` allows (rejected
attempts so far against the budget). A requested seed is reused; an unseeded request gets a
fresh engine-minted seed each attempt. With no passing output after the budget the job ends
``failed`` with ``status_reason: guardrail_violation``. A retryable adapter error puts the job
back to ``queued`` and fails the ``jobs`` attempt (#70's backoff), until ``job_max_attempts``,
then ``failed``/``provider_unavailable``; it uses no retry budget. A non-retryable one ends
``failed`` naming its code (``adapter_auth``, ``adapter_credits``, ``request_invalid``). No
usable output (none returned, or none that decodes as an image) ends ``failed`` with
``provider_empty_result``, unretried. A checker that cannot answer is retried on the same staged
bytes with #70's backoff, then ``failed``/``image_check_unavailable``. ``image_check`` unset
while an image-output policy applies fails closed: ``failed``/``image_check_unconfigured``.

**Late returns** (#267, ``attempts``). A lease expiry advances the job's token. A return for a
token that is no longer current goes through the settlement path only, and its bytes are
deleted unpromoted. A run that finds no record on the current token waits until the stalled
attempt's horizon, then releases its record, and either asks a confirming adapter (adopt, retry
or poll) or begins a new attempt.

**Revalidation** (#264). A target with ``refresh`` and an outcome-answering ``revalidate`` (the
story target) is revalidated against its source: before its outputs are checked (a pause, a gone
target or an erasure ends or defers the work before any model call) and inside the promotion's
transaction, before the job row is locked (the target takes its own row first, #47's lock
order). ``revalidate`` answers ``Ok``, ``Wait`` (the job stays ``checking`` with its staged
bytes and reservation, ``status_detail {waiting_on, next_check_at}``, and its ``jobs`` row is
due again then: ``requeue_in``), ``NeedsEvaluation`` (a policy in force has no result: the
transaction ends, the prompt and output checks run again outside any lock and the promotion
starts over, at most ``MAX_EVALUATIONS`` times before #70 backs off), ``Stale`` or ``Gone``.
``refresh`` re-runs the prompt check with a fresh safety context and records the policy set it
was checked under. An output that fails a check after it had passed is ``stale`` with
``policy_changed``: no regeneration.

**Endings.** ``failed``, ``cancelled``, ``stale``, ``target_gone`` and ``cap_reached`` delete
the job's staging directory once their transaction commits (a failed delete is logged and left
to ``image_staging_sweep``); so does a successful ending. Rejected outputs, fail-closed,
cancelled and stale endings write an ``audit_log`` row (job id, attempt token, policy ids,
outcome), never the image or its prompt.

**Seams.** Targets (``ImageTarget``, registered by kind: ``catalog`` #260, ``story``
#263/#264) and checks (``ImageChecks``: ``check_prompt`` #257, ``check_output`` #258;
``set_image_checks``). The default prompt check is #257's and the default output check #258's
(``output_check``): no applicable policy → the output stays ``unchecked``; ``image_check``
unset → ``ImageCheckUnconfigured``; a checker that cannot answer → ``ImageCheckUnavailable``.
A promoted output's ``media_assets`` row carries the verdict's check fields
(``insert_output_asset``).

**Read and cancel.** ``read_view`` and ``cancel_job`` (``Engine.get_image_job`` /
``cancel_image_job``); ``ImageJobWaiters`` is the long poll's one-per-process ``LISTEN``
connection with a one-second polling fallback.

Logs, metrics and exception messages carry ids, codes and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import psycopg
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock
from epistrel.core.config_changes import read_section
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.errors import (
    EpistrelError,
    GuardrailViolation,
    ImageLimitReached,
    ImagesDisabled,
    InvalidRequest,
)
from epistrel.core.idempotency import ResultCodec
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.images import costs, limits, store
from epistrel.core.images.errors import FencedToken
from epistrel.core.images.store import ImageJob
from epistrel.core.media import probe
from epistrel.core.media.assets import lock_checksum
from epistrel.core.media.store import BlobStore
from epistrel.core.policies.model import Scope
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables import jobs as jobs_table
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.images import (
    TERMINAL_STATUSES,
    AttemptOutcome,
    ImageJobKind,
    ImageJobStatus,
    ReservationState,
    SendState,
    SettlementBasis,
    image_jobs,
)
from epistrel.core.tables.media import CheckStatus, MediaOrigin, MediaSource, MediaState
from epistrel.observability.metrics import count_revalidation
from epistrel.orchestrator.images import attempts
from epistrel.orchestrator.images import errors as adapter_errors
from epistrel.orchestrator.images.params import ImageGenerationParams
from epistrel.orchestrator.images.port import (
    MAX_IMAGES_PER_REQUEST,
    MAX_PROMPT_CHARS,
    MAX_REFERENCE_IMAGES,
    AppliedParameters,
    AspectRatio,
    AttemptCost,
    Cost,
    ImageOutput,
    ImageUsage,
    Quality,
    Size,
)
from epistrel.orchestrator.images.registry import (
    ImageCheckUnconfigured,
    ImageGeneratorHandle,
    ImageNotConfigured,
    TargetedPolicy,
    ensure_image_check_configured,
    require_image_adapter,
)
from epistrel.orchestrator.images.request import (
    RequestInputs,
    build_image_request,
    resend_request,
)
from epistrel.testing import crash

if TYPE_CHECKING:
    from epistrel.core.images.safety import SafetyContext
    from epistrel.orchestrator.models.access import ModelAccessLayer

logger = logging.getLogger(__name__)

IMAGE_JOB_KIND = "image_job"
"""The ``jobs`` kind that runs an image job (``orchestrator.jobs.handlers.image_job``)."""
JOB_LOCATION = "/image-jobs/{}"
"""Where a consumer follows a job (``Location`` of the 202 a generation route answers)."""
MINORS_POLICY_ID = "minors_no_sexual_content"
"""The seeded minors policy (``orchestrator.policies.seeds``): it always applies to image
outputs while it exists, whatever an administrator set its ``applies_to`` to (#257's migration
``0133`` added the image targets)."""
SEED_LIMIT = 2**31
WAIT_MIN_S = 1
WAIT_MAX_S = 30
POLL_INTERVAL_S = 1.0
"""The long poll's fallback: every waited-on job's status is read once a second."""
MAX_WAITERS = 200
"""Long-polling reads one process holds at once; the next is 503 ``too_many_waiters``."""
LISTEN_RECONNECT_S = 1.0


# -- errors ---------------------------------------------------------------------------------


class ImageJobNotFound(EpistrelError):
    code = "image_job_not_found"
    http_status = 404


class TooManyWaiters(EpistrelError):
    """This process already holds ``MAX_WAITERS`` long-polling reads."""

    code = "too_many_waiters"
    http_status = 503


class ImageCheckRequired(EpistrelError):
    """409 ``image_check_unconfigured``: an image-output policy applies and ``image_check`` has
    no model, so a generated image could never pass. Raised at enqueue, before any job,
    reservation or provider call (``require_image_checker``)."""

    code = "image_check_unconfigured"
    http_status = 409


class TargetGone(Exception):
    """The job's target no longer exists (a catalog entry deleted, #260; a story, #263): raised
    by a target's ``revalidate`` (at claim and before each provider call) or its ``promote``,
    it ends the job ``target_gone`` with nothing promoted and its staging deleted."""


class TargetStale(Exception):
    """The job's source changed under it (#264): raised by a target's ``promote`` when the
    placement fails in its transaction, it ends the job ``stale`` with ``reason`` and
    ``detail``, nothing attached."""

    def __init__(self, reason: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class ImageCheckUnavailable(Exception):
    """The output check gave no verdict (unavailable, timed out after its retries, or not
    delivered): the check is retried on the same bytes, then the job fails
    ``image_check_unavailable``."""

    code = "image_check_unavailable"


# -- the request ----------------------------------------------------------------------------


class DepictedSubject(BaseModel):
    """A subject an image depicts (FR-IMG-9): a story character's id (or the persona's or catalog
    character's it was made from; a catalog image: the catalog character's), with the visual
    descriptors the prompt uses for it. The prompt builder marks its own ``depicted`` (#259); a
    consumer's declaration may be the bare id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    character_id: str = Field(min_length=1, max_length=64)
    visual_descriptors: tuple[str, ...] = Field(default=(), max_length=20)
    depicted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _bare_id(cls, value: Any) -> Any:
        return {"character_id": value} if isinstance(value, str) else value


class ImageJobRequest(BaseModel):
    """What a generation request asks the adapter for (``image_jobs.params`` plus the prompt):
    ``n`` images (1 for a story job), an optional ``seed``, ``size`` or ``aspect_ratio`` (at most
    one), ``quality``, and up to four reference images by media id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    negative_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    n: int = Field(default=1, ge=1, le=MAX_IMAGES_PER_REQUEST)
    seed: int | None = Field(default=None, ge=0)
    size: Size | None = None
    aspect_ratio: AspectRatio | None = None
    quality: Quality | None = None
    reference_image_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REFERENCE_IMAGES)
    caption: str | None = None
    alt_text: str | None = None
    depicted_subjects: tuple[DepictedSubject, ...] = Field(default=(), max_length=64)
    """Who the image depicts (FR-IMG-9): the request's declaration or the prompt builder's
    list. The prompt check resolves more (#257): never only this list (FR-IMG-12)."""

    @model_validator(mode="after")
    def _one_dimension(self) -> ImageJobRequest:
        if self.size is not None and self.aspect_ratio is not None:
            raise ValueError("give size or aspect_ratio, not both")
        return self

    def params(self) -> dict[str, Any]:
        """``image_jobs.params``: ``{n, seed, size, aspect_ratio, quality,
        reference_image_ids}``."""
        return {
            "n": self.n,
            "seed": self.seed,
            "size": self.size,
            "aspect_ratio": self.aspect_ratio,
            "quality": self.quality,
            "reference_image_ids": list(self.reference_image_ids),
        }


@dataclass(frozen=True, slots=True)
class PreparedImageJob:
    """A request that passed every enqueue check, ready for ``insert_image_job``."""

    kind: ImageJobKind
    target: dict[str, Any]
    origin: dict[str, Any]
    request: ImageJobRequest
    principal: str
    viewpoint: str | None = None
    safety_context: Mapping[str, Any] | None = None
    """The prompt check's safety context (#257), stored with the job for the output check."""
    extra_params: Mapping[str, Any] = field(default_factory=dict)
    """What a target fixes at enqueue beside the request (#263's story image: ``source``,
    ``viewpoint_basis``, the final ``depicted_subjects``), kept in ``image_jobs.params`` and
    copied verbatim at promotion."""

    @property
    def story_id(self) -> str | None:
        if self.kind is not ImageJobKind.STORY:
            return None
        return str(self.origin["story_id"])


@dataclass(frozen=True, slots=True)
class ImageJobQueued:
    """A request's first answer: 202 ``{job_id, status: queued}`` with ``Location:
    /image-jobs/{id}``. A replay of the same key answers the same job (``replayed``)."""

    job_id: str
    status: str = ImageJobStatus.QUEUED.value
    replayed: bool = False

    @property
    def location(self) -> str:
        return JOB_LOCATION.format(self.job_id)


IMAGE_JOB_CODEC: ResultCodec[ImageJobQueued] = ResultCodec.of(ImageJobQueued, status=202)
"""How a generation route keeps its answer under its ``Idempotency-Key`` (#61; scope ``story``
for a story job, ``deployment`` for a catalog job)."""


# -- the seams ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedOutput:
    """One output of an attempt in private staging."""

    index: int
    path: Path
    media_type: str
    size: int
    sha256: str
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """One applicable policy's result for one output: ``pass`` or ``violation``, whether the
    checker was uncertain (an ``allow`` policy's uncertain result is a pass), whether the checker
    is certified for the policy (NFR-QUAL-12; #258), and the policy's version and name as they
    were at check time."""

    policy_id: str
    result: Literal["pass", "violation"]
    uncertain: bool = False
    certified: bool = False
    policy_version: int | None = None
    policy_name: str | None = None

    def stored(self) -> dict[str, Any]:
        """The result as ``media_assets.policy_results`` holds it."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_name": self.policy_name,
            "result": self.result,
            "uncertain": self.uncertain,
            "certified": self.certified,
        }


@dataclass(frozen=True, slots=True)
class OutputVerdict:
    """``check_output``'s answer (#258, ``orchestrator.images.output_check``): every applicable
    policy's result, and the checker model, its endpoint's host and its fingerprint; no policy
    and no model means the image stays ``unchecked``."""

    per_policy: tuple[PolicyVerdict, ...] = ()
    model: str | None = None
    endpoint: str | None = None
    checker_fingerprint: str | None = None

    def policy_results(self) -> list[dict[str, Any]]:
        """``media_assets.policy_results``: the passes (a violating image is never stored)."""
        return [v.stored() for v in self.per_policy if v.result == "pass"]

    @property
    def violations(self) -> list[str]:
        return [v.policy_id for v in self.per_policy if v.result == "violation"]

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def check_status(self) -> Literal["passed", "unchecked"]:
        return "passed" if self.model is not None and self.per_policy else "unchecked"


@dataclass(frozen=True, slots=True)
class PromotableOutput:
    """A passing output, its bytes at their content-addressed key, ready for ``promote``."""

    index: int
    sha256: str
    media_type: str
    size: int
    width: int | None
    height: int | None
    verdict: OutputVerdict
    generation_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PromptCheck:
    """What ``check_prompt`` judges: the request, its target and origin (#257 builds the safety
    context from them), who asked and the image's viewpoint."""

    kind: ImageJobKind
    target: Mapping[str, Any]
    origin: Mapping[str, Any]
    request: ImageJobRequest
    principal: str = SYSTEM_PRINCIPAL
    viewpoint: str | None = None


@runtime_checkable
class ImageTarget(Protocol):
    """Where a job's images go (registered by kind: ``catalog`` #260, ``story`` #263/#264,
    ``subject`` #830)."""

    @property
    def success_status(self) -> ImageJobStatus: ...

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None: ...

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[PromotableOutput]
    ) -> list[str]: ...

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None: ...

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]: ...


@dataclass(frozen=True, slots=True)
class Ok:
    """``revalidate``: the target still holds; promotion proceeds (#264)."""

    @property
    def outcome(self) -> str:
        return "ok"


@dataclass(frozen=True, slots=True)
class Wait:
    """``revalidate``: the target is paused; look again at ``next_check_at`` (``recheck_s``
    from now), keeping the staged bytes and the reservation (#264)."""

    next_check_at: datetime
    recheck_s: int

    @property
    def outcome(self) -> str:
        return "wait"


@dataclass(frozen=True, slots=True)
class NeedsEvaluation:
    """``revalidate``: policies now in force have no result for the job (#264); they are
    evaluated outside any lock, then revalidation starts over."""

    policy_ids: tuple[str, ...]

    @property
    def outcome(self) -> str:
        return "needs_evaluation"


@dataclass(frozen=True, slots=True)
class Stale:
    """``revalidate``: the source changed (#264); the job ends ``stale`` with ``reason``
    (``target_inactive``, ``amended``, ``erased``, ``policy_changed``, ``paused_too_long``) and
    ``detail`` (``{cause, policy_ids, amendment_id}``, ids only)."""

    reason: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return "stale"


@dataclass(frozen=True, slots=True)
class Gone:
    """``revalidate``: the target no longer exists; the job ends ``target_gone``."""

    @property
    def outcome(self) -> str:
        return "target_gone"


type Revalidation = Ok | Wait | NeedsEvaluation | Stale | Gone
"""What a target's outcome-answering ``revalidate(conn, job)`` returns (#264)."""
OK = Ok()
MAX_EVALUATIONS = 3
"""How many times one run re-evaluates policies that came into force during a promotion before
it backs off (#70) and tries again later."""
EARLY_STALE = frozenset({"erased", "paused_too_long"})
"""The stale reasons acted on before the outputs are checked: no later state undoes them. The
others are decided at promotion, where only the state then counts (FR-IMG-4)."""


async def revalidate(target: ImageTarget, conn: AsyncConnection, job: ImageJob) -> Revalidation:
    """The target's optional ``revalidate(conn, job)`` (#260): raises ``TargetGone`` when the
    target no longer exists (a catalog target raises it; a story target answers ``Gone``,
    #264). A target without one is never gone before promotion: ``Ok``."""
    check = getattr(target, "revalidate", None)
    if check is None:
        return OK
    found = await check(conn, job)
    if isinstance(found, Gone):
        raise TargetGone(f"job {job.id}")
    return found if found is not None else OK


def revalidates(target: ImageTarget) -> bool:
    """Whether ``target`` revalidates against its source before checks and at promotion
    (#264): it has ``refresh``, the story target."""
    return getattr(target, "refresh", None) is not None


class ImageChecks(Protocol):
    """The checks every request and output passes (#257, #258)."""

    async def check_prompt(
        self, runtime: ImageRuntime, check: PromptCheck
    ) -> SafetyContext | None: ...

    async def check_output(
        self, runtime: ImageRuntime, job: ImageJob, output: StagedOutput
    ) -> OutputVerdict: ...


_TARGETS: dict[ImageJobKind, ImageTarget] = {}
PRE_COMMIT_HOOKS: list[Callable[[AsyncConnection, ImageJob], Awaitable[None]]] = []
"""Run by ``insert_image_job`` after the rows are written and the first attempt reserved, in the
caller's transaction. #262 reserves directly (``core.images.limits.reserve``), so it is empty
here."""


def register_target(kind: ImageJobKind, target: ImageTarget) -> None:
    """``target`` handles jobs of ``kind`` from now on (tests swap ``story`` for a test target;
    the ``kind`` CHECK stays ``catalog|story|subject``)."""
    _TARGETS[kind] = target


def unregister_target(kind: ImageJobKind) -> None:
    _TARGETS.pop(kind, None)


def target_for(kind: ImageJobKind) -> ImageTarget | None:
    return _TARGETS.get(kind)


async def image_output_policy_set(db: AsyncEngine) -> list[TargetedPolicy]:
    """The live global policies that apply to image outputs. The seeded minors policy always
    does, while it exists (FR-GUARD-11)."""
    from epistrel.orchestrator.policies.store import list_policies  # here: it imports models

    found: list[TargetedPolicy] = []
    for policy in await list_policies(db, scope=Scope.GLOBAL):
        if policy.id == MINORS_POLICY_ID:
            found.append(_Applies(policy.id, ("image_output",)))
        elif "image_output" in policy.applies_to:
            found.append(policy)
    return found


@dataclass(frozen=True, slots=True)
class _Applies:
    id: str
    applies_to: tuple[str, ...]


class FailClosedChecks:
    """The default checks: #257's prompt check (``safety_checks.check_image_prompt``) and #258's
    output check (``output_check.OutputChecks``), which fails closed: ``image_check`` unset while
    a policy applies is ``ImageCheckUnconfigured``, a checker that cannot answer is
    ``ImageCheckUnavailable``."""

    def __init__(self) -> None:
        self._output: ImageChecks | None = None

    def _checks(self) -> ImageChecks:
        if self._output is None:
            from epistrel.orchestrator.images.output_check import OutputChecks  # it imports us

            self._output = OutputChecks()
        return self._output

    async def check_prompt(self, runtime: ImageRuntime, check: PromptCheck) -> SafetyContext:
        from epistrel.orchestrator.images.safety_checks import check_image_prompt  # it imports us

        return await check_image_prompt(runtime, check)

    async def check_output(
        self, runtime: ImageRuntime, job: ImageJob, output: StagedOutput
    ) -> OutputVerdict:
        return await self._checks().check_output(runtime, job, output)


_CHECKS: list[ImageChecks] = [FailClosedChecks()]


def set_image_checks(checks: ImageChecks | None) -> None:
    """Use ``checks`` from now on (``None``: the fail-closed default)."""
    _CHECKS[0] = checks if checks is not None else FailClosedChecks()


def image_checks() -> ImageChecks:
    return _CHECKS[0]


# -- the runtime ----------------------------------------------------------------------------


@dataclass
class ImageRuntime:
    """What the pipeline runs on: the database, the Model Access Layer (the image adapter and
    ``image_check``), the blob store (staging and the content-addressed keys) and #70's
    ``job_max_attempts``."""

    db: AsyncEngine
    models: ModelAccessLayer
    media: BlobStore
    max_attempts: int = 5


_BOUND: list[ImageRuntime] = []


def bind(runtime: ImageRuntime | None) -> None:
    """Make ``runtime`` what the ``image_job`` handler runs on (``Engine.start_workers``)."""
    _BOUND.clear()
    if runtime is not None:
        _BOUND.append(runtime)


def bound() -> ImageRuntime | None:
    return _BOUND[0] if _BOUND else None


# -- enqueue --------------------------------------------------------------------------------


async def require_image_checker(models: ModelAccessLayer, db: AsyncEngine) -> None:
    """Refuse a generation early (FR-IMG-3): 409 ``not_configured`` without an image adapter
    (checked first), then ``ImageCheckUnconfigured`` naming the policies while an image-output
    policy applies (the seeded minors policy always does) and ``image_check`` is unset."""
    await require_image_adapter(models)
    await ensure_image_check_configured(models, await image_output_policy_set(db))


async def require_generation(models: ModelAccessLayer, db: AsyncEngine) -> None:
    """``require_image_checker`` as a request refuses: 409 ``not_configured``, then 409
    ``image_check_unconfigured`` (``ImageCheckRequired``, naming the policies)."""
    try:
        await require_image_checker(models, db)
    except ImageCheckUnconfigured as exc:
        raise ImageCheckRequired(
            "image generation needs an image check model", {"policy_ids": list(exc.policy_ids)}
        ) from None


def check_request(
    kind: ImageJobKind, request: ImageJobRequest | Mapping[str, Any]
) -> ImageJobRequest:
    """``request`` validated (422 ``validation_error`` naming the field); a story job is
    ``n = 1``."""
    try:
        found = (
            request
            if isinstance(request, ImageJobRequest)
            else ImageJobRequest.model_validate(dict(request))
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise InvalidRequest(
            f"the image request is invalid at {location}", {"field": f"body.{location}"}
        ) from None
    if kind is ImageJobKind.STORY and found.n != 1:
        raise InvalidRequest("a story image job generates one image", {"field": "body.n"})
    return found


async def prepare_image_job(
    runtime: ImageRuntime,
    kind: ImageJobKind,
    target: Mapping[str, Any],
    origin: Mapping[str, Any],
    request: ImageJobRequest | Mapping[str, Any],
    principal: str,
    *,
    viewpoint: str | None = None,
    extra_params: Mapping[str, Any] | None = None,
) -> PreparedImageJob:
    """Every enqueue check, with no transaction open and nothing written (module docstring)."""
    found = check_request(kind, request)
    handler = target_for(kind)
    if handler is None:
        # The route serving a kind registers its target (#260, #263): a programming error.
        raise LookupError(f"no image target is registered for {kind.value} jobs")
    if kind is ImageJobKind.STORY and not origin.get("story_id"):
        raise InvalidRequest("a story image job names its story", {"field": "origin.story_id"})
    async with runtime.db.connect() as conn:
        await handler.validate_enqueue(conn, target, origin)
    await require_generation(runtime.models, runtime.db)
    context = await image_checks().check_prompt(
        runtime, PromptCheck(kind, target, origin, found, principal, viewpoint)
    )
    return PreparedImageJob(
        kind,
        dict(target),
        dict(origin),
        found,
        principal,
        viewpoint,
        None if context is None else context.as_json(),
        dict(extra_params or {}),
    )


async def insert_image_job(
    conn: AsyncConnection, prepared: PreparedImageJob, idempotency_key: str | None
) -> ImageJobQueued:
    """Write the prepared job in ``conn``'s transaction: its row, its first reservation, its
    ``jobs`` row, then ``PRE_COMMIT_HOOKS`` (module docstring)."""
    from epistrel.orchestrator.jobs import queue  # here: the queue imports the handlers

    request = prepared.request
    new = store.NewJob(
        kind=prepared.kind,
        story_id=prepared.story_id,
        target=prepared.target,
        origin=prepared.origin,
        viewpoint=prepared.viewpoint,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        params={
            **request.params(),
            "caption": request.caption,
            "alt_text": request.alt_text,
            # #264: the subjects the prompt check was given, so revalidation re-runs it as is.
            "prompt_subjects": [s.model_dump(mode="json") for s in request.depicted_subjects],
            **prepared.extra_params,
        },
        principal=prepared.principal,
        idempotency_key=idempotency_key,
        safety_context=prepared.safety_context,
    )
    job_id, token = await store.insert_job(conn, new)
    job = await store.read_job(conn, job_id)
    assert job is not None  # noqa: S101 - written above
    # #262: the first attempt is reserved at enqueue, in the request's transaction; a refusal
    # (409 ``image_limit_reached``, 422 ``images_disabled``) rolls the job back with it.
    await limits.reserve(conn, job, token, requested_outputs(job))
    await queue.enqueue(conn, IMAGE_JOB_KIND, {"image_job_id": job_id}, story_id=prepared.story_id)
    for hook in PRE_COMMIT_HOOKS:
        await hook(conn, job)
    return ImageJobQueued(job_id)


def requested_outputs(job: ImageJob) -> int:
    """The outputs each attempt of the job reserves (``k``): its ``n`` (a story job's is 1)."""
    return max(1, int(job.params.get("n") or 1))


async def enqueue_image_job(
    runtime: ImageRuntime,
    kind: ImageJobKind,
    target: Mapping[str, Any],
    origin: Mapping[str, Any],
    request: ImageJobRequest | Mapping[str, Any],
    idempotency_key: str | None,
    principal: str,
    *,
    viewpoint: str | None = None,
) -> ImageJobQueued:
    """``prepare_image_job`` then ``insert_image_job`` in a transaction of its own."""
    prepared = await prepare_image_job(
        runtime, kind, target, origin, request, principal, viewpoint=viewpoint
    )
    async with runtime.db.begin() as conn:
        return await insert_image_job(conn, prepared, idempotency_key)


# -- endings --------------------------------------------------------------------------------

AUDITED_ENDINGS: frozenset[ImageJobStatus] = frozenset(
    {
        ImageJobStatus.CANCELLED,
        ImageJobStatus.STALE,
        ImageJobStatus.TARGET_GONE,
    }
)
"""Endings that write an ``image_job_ended`` audit row (a fail-closed ``failed`` does too)."""
FAIL_CLOSED = "image_check_unconfigured"
WAITING_ON_PAUSE = "pause"
"""``status_detail.waiting_on`` of a story job waiting on its paused story (#264)."""
REQUEUE_IN = "requeue_in"
"""The run result's key asking #70 to run the job again in that many seconds (a wait, #264);
the handler moves it to ``JobOutput.requeue_in``."""


async def _delete_staging(media: BlobStore, job_id: str) -> None:
    """Remove the job's staging directory after its ending committed (logged, never raised)."""
    try:
        await asyncio.to_thread(media.remove_image_staging, job_id)
    except Exception as exc:
        logger.warning("image job %s: staging not removed: %s", job_id, type(exc).__name__)


async def end_job(
    runtime: ImageRuntime,
    job_id: str,
    status: ImageJobStatus,
    *,
    reason: str | None = None,
    token: str | None = None,
    policy_ids: Sequence[str] = (),
    outcome: AttemptOutcome | None = None,
    asset_ids: Sequence[str] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    """End the job ``status`` in one transaction (while it still carries ``token``, when
    given): the reason and its ``status_detail``, the policy results of a fail-closed ending, the
    current attempt's
    outcome, every reservation it still holds released (#262), the target's ``on_terminal``,
    the audit row and the notification; then, once committed, its staging is deleted. ``False``
    when the job had already ended or was fenced."""
    async with runtime.db.begin() as conn:
        ended = await _end_in(
            conn,
            job_id,
            status,
            reason=reason,
            token=token,
            policy_ids=policy_ids,
            outcome=outcome,
            asset_ids=asset_ids,
            detail=detail,
        )
    if not ended:
        return False
    await _delete_staging(runtime.media, job_id)
    logger.info("image job %s ended %s (%s)", job_id, status.value, reason or "-")
    return True


async def _end_in(
    conn: AsyncConnection,
    job_id: str,
    status: ImageJobStatus,
    *,
    reason: str | None = None,
    token: str | None = None,
    policy_ids: Sequence[str] = (),
    outcome: AttemptOutcome | None = None,
    asset_ids: Sequence[str] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    """``end_job``'s transaction, in ``conn``'s (the caller deletes the staging once it
    commits); ``False`` when the job had already ended or was fenced."""
    job = await store.read_job(conn, job_id, lock=True)
    if job is None or job.terminal or (token is not None and job.attempt_token != token):
        return False
    values: dict[str, Any] = {"status_reason": reason}
    if asset_ids is not None:
        values["asset_ids"] = list(asset_ids)
    if detail is not None:
        values["status_detail"] = dict(detail)
    await store.set_status(conn, job_id, status, **values)
    if reason == FAIL_CLOSED and policy_ids:
        await store.append_policy_results(
            conn,
            job_id,
            [
                {
                    "attempt_token": job.attempt_token,
                    "output": None,
                    "policy_id": p,
                    "result": "unevaluated",
                }
                for p in policy_ids
            ],
        )
    if outcome is not None:
        await store.update_reservation(conn, job_id, job.attempt_token, outcome=outcome.value)
    # #267: an attempt a lease expiry fenced stays held until its horizon (its timer releases it).
    await limits.release_all(conn, job_id, current_token=job.attempt_token)
    handler = target_for(job.kind)
    if handler is not None:
        ended = await store.read_job(conn, job_id)
        assert ended is not None  # noqa: S101 - locked above
        await handler.on_terminal(conn, ended)
    if status in AUDITED_ENDINGS or reason == FAIL_CLOSED:
        details: dict[str, Any] = {
            "job_id": job_id,
            "attempt_token": job.attempt_token,
            "outcome": status.value,
            "reason": reason,
        }
        if status is ImageJobStatus.STALE and detail:
            details["detail"] = dict(detail)
        await audit.record(
            conn,
            AuditKind.IMAGE_JOB_ENDED,
            principal=SYSTEM_PRINCIPAL,
            story_id=job.story_id,
            policy_ids=list(policy_ids),
            details=details,
        )
    return True


async def cancel_job(runtime: ImageRuntime, job_id: str) -> ImageJob:
    """Cancel a queued or running job (``cancelled``, a new attempt token fencing any worker at
    promote, a queued ``jobs`` row finished, staged bytes deleted); a terminal job is answered
    unchanged. 404 ``image_job_not_found`` (also for a ``job_id`` that is no ULID, so no text
    such as a NUL reaches the database)."""
    if not re.fullmatch(ULID_PATTERN, job_id):
        raise _not_found(job_id.replace("\x00", ""))
    async with runtime.db.begin() as conn:
        job = await store.read_job(conn, job_id, lock=True)
        if job is None:
            raise _not_found(job_id)
        if job.terminal:
            return job
        await store.set_status(
            conn,
            job_id,
            ImageJobStatus.CANCELLED,
            status_reason=None,
            attempt_token=store.new_token(),
        )
        await store.update_reservation(
            conn, job_id, job.attempt_token, outcome=AttemptOutcome.DISCARDED.value
        )
        # #267: polling stops, but a fenced attempt stays held until its horizon timer.
        await limits.release_all(conn, job_id, current_token=job.attempt_token)
        await conn.execute(
            update(jobs_table)
            .where(
                jobs_table.c.kind == IMAGE_JOB_KIND,
                jobs_table.c.status == "queued",
                jobs_table.c.payload["image_job_id"].astext == job_id,
            )
            .values(
                status="done",
                result={"image_job": ImageJobStatus.CANCELLED.value},
                updated_at=clock.now(),
            )
        )
        handler = target_for(job.kind)
        ended = await store.read_job(conn, job_id)
        assert ended is not None  # noqa: S101 - locked above
        if handler is not None:
            await handler.on_terminal(conn, ended)
        await audit.record(
            conn,
            AuditKind.IMAGE_JOB_ENDED,
            principal=SYSTEM_PRINCIPAL,
            story_id=job.story_id,
            details={
                "job_id": job_id,
                "attempt_token": job.attempt_token,
                "outcome": ImageJobStatus.CANCELLED.value,
                "reason": None,
            },
        )
    await _delete_staging(runtime.media, job_id)
    logger.info("image job %s cancelled", job_id)
    return ended


def _not_found(job_id: str) -> ImageJobNotFound:
    return ImageJobNotFound(f"image job {job_id} does not exist", {"job_id": job_id})


# -- the worker -----------------------------------------------------------------------------

PROVIDER_REASONS: Mapping[type[adapter_errors.ImageAdapterError], str] = {
    adapter_errors.ImageAdapterAuth: "adapter_auth",
    adapter_errors.ImageAdapterCredits: "adapter_credits",
    adapter_errors.ImageRequestInvalid: "request_invalid",
}


class _Requeue(Exception):
    """Put the job back for #70's backoff: the ``jobs`` attempt fails with this class name."""


class ImageJobRetry(_Requeue):
    """A retryable adapter error or an unavailable checker: #70 backs off and runs it again."""


def _reason_of(exc: adapter_errors.ImageAdapterError) -> str:
    for kind, reason in PROVIDER_REASONS.items():
        if isinstance(exc, kind):
            return reason
    return "adapter_error"


def _decoded(output: ImageOutput) -> tuple[str, int | None, int | None] | None:
    """The output's sniffed media type and decoded dimensions, or ``None`` when its bytes are
    not an image (discarded unchecked, never a violation)."""
    found = probe.sniff(output.data[: probe.SNIFF_BYTES])
    if found is None or not found.startswith("image/"):
        return None
    try:
        with Image.open(io.BytesIO(output.data)) as image:
            image.verify()
        with Image.open(io.BytesIO(output.data)) as image:
            width, height = image.size
    except UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError:
        return None
    return found, width, height


def _cost(cost: AttemptCost | None) -> Any:
    if isinstance(cost, Cost):
        return {"amount": str(cost.amount), "currency": cost.currency}
    return cost


def _usage(usage: ImageUsage | None) -> Any:
    return None if usage is None else {"images": usage.images, **dict(usage.raw)}


@dataclass
class _Attempt:
    token: str
    seed: int
    seed_source: Literal["requested", "engine"]
    outputs: list[StagedOutput] = field(default_factory=list)
    applied: dict[str, Any] = field(default_factory=dict)
    usage: Any = None
    cost: Any = None
    attempt_ref: str | None = None
    resend_of: str | None = None
    """#268: an idempotent re-send's stalled attempt (whose record its result settles)."""
    request: dict[str, Any] | None = None
    """#268: the request a re-send sends again (the stalled attempt's ``request_snapshot``)."""
    key: str | None = None
    """#268: the idempotency key a re-send carries (the stalled attempt's)."""


SNAPSHOT_EXCLUDED: set[str] = {"prompt", "negative_prompt"}
"""What ``image_reservations.request_snapshot`` leaves out (#268): the prompt text stays on the
job (where erasure and story deletion reach it); the records are deployment accounting."""


def _request_inputs(job: ImageJob) -> RequestInputs:
    """#797: the narrowed value the adapter's request is built from (``request``): the job's
    prompt and request-shaped parameters, never its ``safety_context``."""
    return RequestInputs.of(job.prompt, job.negative_prompt, job.params)


class ImageJobRunner:
    """One claimed run of one image job (``run_image_job``)."""

    def __init__(self, runtime: ImageRuntime, job_id: str, failures: int) -> None:
        self.rt = runtime
        self.job_id = job_id
        self.failures = failures
        self.settings = ImageJobSettings()
        self.evaluations = 0
        """How many times this run re-evaluated policies at promotion (#264)."""

    @property
    def last_try(self) -> bool:
        """Whether a requeue now would be the ``jobs`` row's last failed attempt."""
        return self.failures + 1 >= self.rt.max_attempts

    async def read(self) -> ImageJob | None:
        async with self.rt.db.connect() as conn:
            return await store.read_job(conn, self.job_id)

    async def run(self) -> dict[str, Any]:
        job = await self.read()
        if job is None or job.terminal:
            return {"image_job": None if job is None else job.status.value}
        target = target_for(job.kind)
        if target is None:
            await end_job(self.rt, job.id, ImageJobStatus.FAILED, reason="target_unavailable")
            return {"image_job": ImageJobStatus.FAILED.value}
        try:
            handle = await require_image_adapter(self.rt.models)
        except ImageNotConfigured:
            await end_job(self.rt, job.id, ImageJobStatus.FAILED, reason="not_configured")
            return {"image_job": ImageJobStatus.FAILED.value}
        settings = await read_section(self.rt.db, ImageJobSettings)
        self.settings = settings
        config = await self.rt.models.images.config()
        params = config.image_params or ImageGenerationParams()
        if _needs_preparing(job):
            # #265: an automatic job builds its prompt, is checked and reserves here.
            stopped = await self._prepare(job, target)
            if stopped is not None:
                return stopped
            job = await self.read()
            if job is None or job.terminal:
                return {"image_job": None if job is None else job.status.value}
        begun = await self._resume_or_begin(job, handle)
        if isinstance(begun, dict):
            return begun
        attempt: _Attempt | None = begun
        while attempt is not None:
            job = await self.read()
            if job is None or job.terminal or job.attempt_token != attempt.token:
                return {"image_job": None if job is None else job.status.value}
            if not attempt.outputs:
                if await self._gone(job, target, attempt.token):
                    return {"image_job": ImageJobStatus.TARGET_GONE.value}
                sent = await self._generate(job, attempt, handle, params)
                if sent is not None:
                    return sent
            ended = await self._check_and_promote(job, attempt, target, settings, handle)
            if isinstance(ended, dict):
                return ended
            attempt = ended
        return {"image_job": "fenced"}

    async def _prepare(self, job: ImageJob, target: ImageTarget) -> dict[str, Any] | None:
        """#265: an automatic job's first step (``auto.queue_points`` queued it with no prompt
        and no reservation). Before any prompt building, reservation or provider call,
        ``image_check`` unset while an image-output policy applies ends it
        ``failed``/``image_check_unconfigured``. Its target's revalidation then ends it
        ``target_gone`` or ``stale`` or defers it (a paused story), and the target's ``prepare``
        builds and checks its prompt: a refused prompt ends it ``failed``/``guardrail_violation``
        naming the policies, another refusal ``failed`` with its code, an unavailable model is
        retried with #70's backoff. Last, one transaction stores the prompt, the params and the
        safety context, and reserves the first attempt (#262): a refusal ends it ``cap_reached``
        (``image_limit_reached`` or ``images_disabled``). The run's result when it stops here,
        else ``None``."""
        try:
            await ensure_image_check_configured(
                self.rt.models, await image_output_policy_set(self.rt.db)
            )
        except ImageCheckUnconfigured as exc:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason=FAIL_CLOSED,
                policy_ids=list(exc.policy_ids),
            )
            return {"image_job": ImageJobStatus.FAILED.value, "reason": FAIL_CLOSED}
        prepare = getattr(target, "prepare", None)
        if prepare is None:
            await end_job(self.rt, job.id, ImageJobStatus.FAILED, reason="target_unavailable")
            return {"image_job": ImageJobStatus.FAILED.value, "reason": "target_unavailable"}
        async with self.rt.db.begin() as conn:
            try:
                found = await revalidate(target, conn, job)
            except TargetGone:
                found = Gone()
        if isinstance(found, Gone):
            await end_job(self.rt, job.id, ImageJobStatus.TARGET_GONE)
            return {"image_job": ImageJobStatus.TARGET_GONE.value}
        if isinstance(found, Stale):
            # No prompt can be built for a position the timeline left: the job ends now.
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.STALE,
                reason=found.reason,
                policy_ids=list(found.detail.get("policy_ids") or ()),
                detail=found.detail,
            )
            return {"image_job": ImageJobStatus.STALE.value, "reason": found.reason}
        if isinstance(found, Wait):
            return {
                "image_job": job.status.value,
                "waiting_on": WAITING_ON_PAUSE,
                REQUEUE_IN: found.recheck_s,
            }
        try:
            prepared: PreparedImageJob = await prepare(self.rt, job)
        except GuardrailViolation as exc:
            violations = exc.details.get("violations") or ()
            ids = [str(v["policy_id"]) for v in violations if v.get("policy_id")]
            async with self.rt.db.begin() as conn:
                await store.append_policy_results(
                    conn,
                    job.id,
                    [
                        {
                            "attempt_token": job.attempt_token,
                            "output": None,
                            "policy_id": p,
                            "result": "violation",
                        }
                        for p in ids
                    ],
                )
                await _end_in(
                    conn,
                    job.id,
                    ImageJobStatus.FAILED,
                    reason=GuardrailViolation.code,
                    policy_ids=ids,
                )
            logger.info("image job %s: its prompt was refused (%d policies)", job.id, len(ids))
            return {"image_job": ImageJobStatus.FAILED.value, "reason": GuardrailViolation.code}
        except EpistrelError as exc:
            if exc.http_status == 503 and not self.last_try:
                raise ImageJobRetry(exc.code) from None
            await end_job(self.rt, job.id, ImageJobStatus.FAILED, reason=exc.code)
            return {"image_job": ImageJobStatus.FAILED.value, "reason": exc.code}
        return await self._store_prepared(job, prepared)

    async def _store_prepared(
        self, job: ImageJob, prepared: PreparedImageJob
    ) -> dict[str, Any] | None:
        """The prepared prompt stored on the job and its first attempt reserved, in one
        transaction (``_prepare``)."""
        request = prepared.request
        capped: str | None = None
        async with self.rt.db.begin() as conn:
            current = await store.read_job(conn, job.id, lock=True)
            if (
                current is None
                or current.terminal
                or current.attempt_token != job.attempt_token
                or not _needs_preparing(current)
            ):
                return {"image_job": "fenced"}
            auto = dict(current.params.get("auto") or {})
            await store.update_job(
                conn,
                job.id,
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                params={
                    **current.params,
                    **request.params(),
                    "caption": request.caption,
                    "alt_text": request.alt_text,
                    "prompt_subjects": [
                        s.model_dump(mode="json") for s in request.depicted_subjects
                    ],
                    **prepared.extra_params,
                    "auto": {**auto, "prepared": True},
                },
                safety_context=None
                if prepared.safety_context is None
                else dict(prepared.safety_context),
            )
            ready = await store.read_job(conn, job.id)
            assert ready is not None  # noqa: S101 - locked above
            try:
                async with conn.begin_nested():
                    await limits.reserve(conn, ready, ready.attempt_token, requested_outputs(ready))
            except (ImageLimitReached, ImagesDisabled) as exc:
                period = exc.details.get("period") or await limits.current_period(conn)
                await _end_in(
                    conn,
                    job.id,
                    ImageJobStatus.CAP_REACHED,
                    reason=exc.code,
                    detail={"scope": exc.details.get("scope"), "period": period},
                )
                capped = exc.code
        if capped is not None:
            logger.info("image job %s ended cap_reached (%s)", job.id, capped)
            return {"image_job": ImageJobStatus.CAP_REACHED.value, "reason": capped}
        return None

    async def _gone(self, job: ImageJob, target: ImageTarget, token: str) -> bool:
        """Whether the target is gone (``revalidate``): then the job ends ``target_gone`` at
        once, with no provider call."""
        try:
            async with self.rt.db.connect() as conn:
                await revalidate(target, conn, job)
        except TargetGone:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.TARGET_GONE,
                token=token,
                outcome=AttemptOutcome.DISCARDED,
            )
            return True
        return False

    # -- attempts ---------------------------------------------------------------------------

    def _seed(self, job: ImageJob) -> tuple[int, Literal["requested", "engine"]]:
        requested = job.params.get("seed")
        if requested is not None:
            return int(requested), "requested"
        return secrets.randbelow(SEED_LIMIT), "engine"

    async def _resume_or_begin(
        self, job: ImageJob, handle: ImageGeneratorHandle
    ) -> _Attempt | dict[str, Any] | None:
        """The attempt this run starts with: the current one when it was never sent; the
        current one's staged outputs when a previous run staged them and stopped before its
        checks finished (a checker retry); with no record on the current token (a lease expiry
        fenced the last one, #267), what ``_after_fence`` decides. A current attempt still in
        flight (``sending`` or ``accepted``: the previous run raised mid-call and #70 requeued
        it without a lease expiry) is fenced as a lease expiry would (``attempts.fence_in``,
        #268: an ambiguous send or an idempotent re-send), and ``_after_fence`` decides.
        Otherwise the attempt is discarded with its staged bytes and a new one begins, reserved
        before its call (#262), and any record it still holds is released. The run's result
        when it stops here (a horizon or confirmation wait, or ``cap_reached``); ``None`` when
        fenced."""
        async with self.rt.db.connect() as conn:
            found = await store.read_reservation(conn, job.id, job.attempt_token)
        if found is None:
            return await self._after_fence(job, handle)
        if found.send_state in attempts.IN_FLIGHT and found.outcome is None:
            async with self.rt.db.begin() as conn:
                locked = await store.read_job(conn, job.id, lock=True)
                if locked is None or locked.terminal or locked.attempt_token != job.attempt_token:
                    return None
                await attempts.fence_in(conn, locked)
            fresh = await self.read()
            if fresh is None or fresh.terminal:
                return {"image_job": None if fresh is None else fresh.status.value}
            return await self._after_fence(fresh, handle)
        async with self.rt.db.begin() as conn:
            current = await store.read_reservation(conn, job.id, job.attempt_token)
            if current is None:
                return None
            if current.send_state is SendState.PREPARED:
                seed, source = self._seed(job)
                return _Attempt(job.attempt_token, seed, source)
            if current.outcome is None and job.status is ImageJobStatus.CHECKING:
                staged = await asyncio.to_thread(
                    self._staged, job.id, job.attempt_token, requested_outputs(job)
                )
                if staged:
                    applied = dict(current.applied_parameters or {})
                    return _Attempt(
                        job.attempt_token,
                        int(applied.get("seed", 0)),
                        applied.get("seed_source", "engine"),
                        staged,
                        applied,
                        attempt_ref=current.attempt_ref,
                    )
            if current.outcome is None:
                await store.update_reservation(
                    conn, job.id, job.attempt_token, outcome=AttemptOutcome.DISCARDED.value
                )
            record = await limits.read_record(conn, job.id, job.attempt_token, lock=True)
            if record is not None and record.state is ReservationState.HELD:
                await limits.release(conn, record)
            begun = await self._next_attempt(conn, job, job.attempt_token)
        await asyncio.to_thread(self._remove_attempt, job.id, job.attempt_token)
        if not isinstance(begun, str):
            if begun is not None:
                await _delete_staging(self.rt.media, job.id)
            return begun
        seed, source = self._seed(job)
        return _Attempt(begun, seed, source)

    # -- after a lease expiry (#267) ---------------------------------------------------------

    async def _after_fence(
        self, job: ImageJob, handle: ImageGeneratorHandle
    ) -> _Attempt | dict[str, Any] | None:
        """The job's current token has no record: a lease expiry fenced its last attempt
        (``attempts.on_lease_expired``). A record fenced before it was sent is released. A
        stalled attempt in flight holds the job until its horizon (``waiting_for_horizon``; the
        run asks #70 to come back then). At the horizon its record is released if it is still
        held, and then a ``confirms_completion`` adapter is asked (``attempts.confirm``): adopt,
        retry, or poll again later. Otherwise, and on a retry, a new attempt begins under the
        current token (``_begin_current``).

        #268: a stalled attempt still ``sending`` is recovered first (``recover_attempt``): an
        idempotent one is re-sent at once (``_resend_in``), any other is an ambiguous send held
        to its horizon and presumed there. A job whose ``jobs`` row has used up #70's
        ``max_attempts`` (each presumption and re-send counts) ends
        ``failed``/``provider_unavailable`` instead of sending again."""
        now = clock.now()
        exhausted = self.failures >= self.rt.max_attempts
        resend: _Attempt | None = None
        async with self.rt.db.begin() as conn:
            locked = await store.read_job(conn, job.id, lock=True)
            if locked is None or locked.terminal:
                return {"image_job": None if locked is None else locked.status.value}
            if locked.attempt_token != job.attempt_token:
                return None
            if await store.read_reservation(conn, job.id, job.attempt_token) is not None:
                return None  # another run began the attempt meanwhile
            await attempts.release_unsent(conn, locked)
            stalled = await attempts.stalled_record(conn, locked)
            if stalled is not None and stalled.send_state is SendState.SENDING:
                recovered, original = await attempts.recover_attempt(
                    conn, locked, stalled, resend=not exhausted
                )
                if recovered == attempts.RESEND and original is not None:
                    resend = await self._resend_in(conn, locked, original, stalled)
                else:
                    stalled = original
            if resend is None and stalled is not None:
                horizon = stalled.horizon_at or now
                if now < horizon:
                    await attempts.set_waiting(conn, job.id, True)
                    left = max(1, int((horizon - now).total_seconds()) + 1)
                    logger.info("image job %s waits for its stalled attempt's horizon", job.id)
                    return {
                        "image_job": locked.status.value,
                        "waiting_for_horizon": True,
                        REQUEUE_IN: left,
                    }
                await attempts.set_waiting(conn, job.id, False)
                await attempts.release_at_horizon_in(conn, job.id, stalled.attempt_token)
        if resend is not None:
            return resend
        if exhausted:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason="provider_unavailable",
                token=job.attempt_token,
            )
            return {"image_job": ImageJobStatus.FAILED.value, "reason": "provider_unavailable"}
        if (
            stalled is not None
            and stalled.attempt_ref is not None
            and handle.capabilities().confirms_completion
        ):
            fresh = await self.read()
            if fresh is None or fresh.terminal or fresh.attempt_token != job.attempt_token:
                return {"image_job": "fenced"}
            decided = await attempts.confirm(self.rt.db, fresh, stalled, handle, self.settings)
            if isinstance(decided, attempts.Poll):
                return {
                    "image_job": fresh.status.value,
                    "waiting_on_confirmation": True,
                    "next_poll_at": decided.next_at.isoformat(),
                }
            if isinstance(decided, attempts.Adopt):
                return await self._adopt(fresh, stalled, decided.status)
        return await self._begin_current(job)

    async def _resend_in(
        self,
        conn: AsyncConnection,
        job: ImageJob,
        original: store.Reservation,
        stalled: store.Reservation,
    ) -> _Attempt:
        """#268: the idempotent re-send of ``original`` under the job's current token, in
        ``conn``'s transaction: a row that reserves nothing (``limits.insert_unreserved_record``)
        naming ``resend_of``, with the original's key and request snapshot; its result settles
        the original's record (``_generate``). A stalled earlier re-send (``stalled``) is
        superseded."""
        await attempts.supersede(conn, job.id, stalled)
        token = job.attempt_token
        await limits.insert_unreserved_record(conn, job, token)
        await store.update_reservation(
            conn,
            job.id,
            token,
            resend_of=original.attempt_token,
            idempotency_key=original.idempotency_key,
            request_snapshot=original.request_snapshot,
        )
        applied = dict(original.applied_parameters or {})
        return _Attempt(
            token,
            int(applied.get("seed", 0)),
            applied.get("seed_source", "engine"),
            resend_of=original.attempt_token,
            request=original.request_snapshot,
            key=original.idempotency_key,
        )

    async def _adopt(
        self, job: ImageJob, stalled: store.Reservation, status: Any
    ) -> _Attempt | dict[str, Any] | None:
        """A confirming adapter's ``succeeded`` after the horizon: the stalled attempt's record
        is settled through the settlement path (``adopted``: all its outputs count as used and
        overage), and its first ``n`` outputs are staged under the job's current token, on an
        attempt row that reserves nothing (``limits.insert_unreserved_record``), for the checks,
        revalidation and promotion. Outputs beyond ``n`` are ``extra_outputs``, never staged.
        When the record was settled already (a late return got there first), the job retries
        instead."""
        outputs = list(status.outputs or ())
        n = requested_outputs(job)
        token = job.attempt_token
        written = await asyncio.to_thread(self._stage, job.id, token, outputs[:n])
        applied = dict(stalled.applied_parameters or {})
        cost, usage = attempts.cost_json(status.cost), attempts.usage_json(status.usage)
        adopted = False
        async with self.rt.db.begin() as conn:
            locked = await store.read_job(conn, job.id, lock=True)
            if locked is not None and not locked.terminal and locked.attempt_token == token:
                outcome = await attempts.settle_attempt_in(
                    conn,
                    job.id,
                    stalled.attempt_token,
                    len(outputs),
                    cost=status.cost,
                    usage=status.usage,
                    released=AttemptOutcome.ADOPTED,
                )
                if outcome is AttemptOutcome.ADOPTED and written:
                    await limits.insert_unreserved_record(conn, locked, token)
                    await store.update_reservation(
                        conn,
                        job.id,
                        token,
                        send_state=SendState.RETURNED.value,
                        attempt_ref=stalled.attempt_ref,
                        applied_parameters={**applied, "usage": usage, "cost": cost},
                        outputs_returned=len(outputs),
                    )
                    adopted = await store.set_status(
                        conn, job.id, ImageJobStatus.CHECKING, token=token
                    )
        if not adopted:
            await asyncio.to_thread(self._remove_attempt, job.id, token)
            fresh = await self.read()
            if fresh is None or fresh.terminal or fresh.attempt_token != token:
                return {"image_job": "fenced"}
            return await self._begin_current(fresh)
        logger.info("image job %s adopted %d output(s) after the horizon", job.id, len(written))
        return _Attempt(
            token,
            int(applied.get("seed", 0)),
            applied.get("seed_source", "engine"),
            written,
            applied,
            usage=usage,
            cost=cost,
            attempt_ref=stalled.attempt_ref,
        )

    async def _begin_current(self, job: ImageJob) -> _Attempt | dict[str, Any] | None:
        """A new attempt under the job's current token (which has no record yet), reserved
        before its call in a SAVEPOINT (#262): the attempt, or the run's result when it could
        not reserve and the job ended ``cap_reached``; ``None`` when fenced."""
        capped: dict[str, Any] | None = None
        async with self.rt.db.begin() as conn:
            locked = await store.read_job(conn, job.id, lock=True)
            if (
                locked is None
                or locked.terminal
                or locked.attempt_token != job.attempt_token
                or await store.read_reservation(conn, job.id, job.attempt_token) is not None
            ):
                return None
            try:
                async with conn.begin_nested():
                    await limits.reserve(
                        conn, locked, locked.attempt_token, requested_outputs(locked)
                    )
            except (ImageLimitReached, ImagesDisabled) as exc:
                period = exc.details.get("period") or await limits.current_period(conn)
                await _end_in(
                    conn,
                    job.id,
                    ImageJobStatus.CAP_REACHED,
                    reason=exc.code,
                    token=locked.attempt_token,
                    detail={"scope": exc.details.get("scope"), "period": period},
                )
                capped = {"image_job": ImageJobStatus.CAP_REACHED.value, "reason": exc.code}
        if capped is not None:
            await _delete_staging(self.rt.media, job.id)
            logger.info("image job %s ended cap_reached (%s)", job.id, capped["reason"])
            return capped
        seed, source = self._seed(job)
        return _Attempt(job.attempt_token, seed, source)

    async def _next_attempt(
        self, conn: AsyncConnection, job: ImageJob, old_token: str
    ) -> str | dict[str, Any] | None:
        """A new attempt under a new token, reserved before its call inside a SAVEPOINT (#262),
        in ``conn``'s transaction: its token. When the reservation fails, the savepoint is
        rolled back (undoing the counter upserts) and the job ends ``cap_reached`` in the same
        transaction (``status_reason`` ``image_limit_reached`` or ``images_disabled``,
        ``status_detail {scope, period}``); the run's result then, and the caller deletes the
        staging once committed. ``None`` when ``old_token`` was fenced."""
        token = await store.next_attempt(conn, job.id, old_token)
        if token is None:
            return None
        try:
            async with conn.begin_nested():
                await limits.reserve(conn, job, token, requested_outputs(job))
        except (ImageLimitReached, ImagesDisabled) as exc:
            period = exc.details.get("period") or await limits.current_period(conn)
            await _end_in(
                conn,
                job.id,
                ImageJobStatus.CAP_REACHED,
                reason=exc.code,
                token=token,
                detail={"scope": exc.details.get("scope"), "period": period},
            )
            logger.info("image job %s ended cap_reached (%s)", job.id, exc.code)
            return {"image_job": ImageJobStatus.CAP_REACHED.value, "reason": exc.code}
        return token

    def _remove_attempt(self, job_id: str, token: str) -> None:
        with contextlib.suppress(FileNotFoundError, ValueError):
            self.rt.media.remove_image_staging(job_id, token)

    def _staged(self, job_id: str, token: str, n: int) -> list[StagedOutput]:
        """The attempt's staged outputs below index ``n`` (one beyond ``n`` a crash left before
        its delete is never checked)."""
        directory = self.rt.media.image_staging_dir(job_id, token)
        if not directory.is_dir():
            return []
        found: list[StagedOutput] = []
        for path in sorted(
            directory.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1
        ):
            if not path.name.isdigit() or int(path.name) >= n:
                continue
            data = path.read_bytes()
            decoded = _decoded(ImageOutput(data, "image/png"))
            if decoded is None:
                path.unlink(missing_ok=True)
                continue
            media_type, width, height = decoded
            found.append(
                StagedOutput(
                    int(path.name),
                    path,
                    media_type,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    width,
                    height,
                )
            )
        return found

    def _stage(self, job_id: str, token: str, outputs: Sequence[ImageOutput]) -> list[StagedOutput]:
        """Write each usable output to ``tmp/images/<job>/<token>/<k>`` (fsynced)."""
        directory = self.rt.media.image_staging_dir(job_id, token)
        directory.mkdir(parents=True, exist_ok=True)
        staged: list[StagedOutput] = []
        for index, output in enumerate(outputs):
            decoded = _decoded(output)
            if decoded is None:
                continue
            media_type, width, height = decoded
            path = directory / str(index)
            with path.open("wb") as handle:
                handle.write(output.data)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append(
                StagedOutput(
                    index,
                    path,
                    media_type,
                    len(output.data),
                    hashlib.sha256(output.data).hexdigest(),
                    width,
                    height,
                )
            )
        return staged

    async def _generate(
        self,
        job: ImageJob,
        attempt: _Attempt,
        handle: ImageGeneratorHandle,
        params: ImageGenerationParams,
    ) -> dict[str, Any] | None:
        """Send the attempt and stage its outputs; the run's result when the job ended (or
        was fenced), else ``None`` with ``attempt.outputs`` filled. The ``sending`` commit fixes
        the attempt's horizon (#267: ``started_at + request_timeout_s +
        image_attempt_grace_s``) and its seed. A return for a token a lease expiry, a retry or a
        cancel replaced goes through the settlement path only (``attempts``), and its bytes are
        deleted unpromoted."""
        from epistrel.orchestrator.jobs import timers  # here: the jobs package imports us

        capabilities = handle.capabilities()
        started = clock.now()
        request = (
            # A re-send's snapshot holds no prompt text (accounting rows outlive their story):
            # the job's prompt is sent with it again.
            resend_request(attempt.request, _request_inputs(job))
            if attempt.request is not None
            else build_image_request(_request_inputs(job), attempt.seed, params)
        )
        key = attempt.key or store.idempotency_key_of(job.id, attempt.token)
        horizon = attempts.horizon_of(
            started, attempts.request_timeout_s(params, capabilities), self.settings
        )
        async with self.rt.db.begin() as conn:
            moved = await store.set_status(
                conn,
                job.id,
                ImageJobStatus.GENERATING,
                token=attempt.token,
                attempts=image_jobs.c.attempts + 1,
            )
            if not moved:
                return {"image_job": "fenced"}
            # #268: the key, the request and the adapter's capabilities commit before submit.
            await store.update_reservation(
                conn,
                job.id,
                attempt.token,
                send_state=SendState.SENDING.value,
                started_at=started,
                horizon_at=horizon,
                applied_parameters={"seed": attempt.seed, "seed_source": attempt.seed_source},
                idempotency_key=key,
                request_snapshot=request.model_dump(mode="json", exclude=SNAPSHOT_EXCLUDED),
                capabilities_snapshot={
                    "submission": capabilities.submission,
                    "idempotency_keys": capabilities.idempotency_keys,
                },
            )
            if attempt.resend_of is not None:
                # The re-sent attempt's record stays held while its re-send is in flight.
                await store.update_reservation(conn, job.id, attempt.resend_of, horizon_at=horizon)
                await timers.schedule(
                    conn, attempts.HORIZON_TIMER, f"{job.id}:{attempt.resend_of}", horizon
                )
                logger.info(
                    "image job %s attempt %s: image_attempt_resent", job.id, attempt.resend_of
                )
        crash.hit("images.after_sending_commit")
        try:
            result = await handle.submit(request, key)
            outputs: Sequence[ImageOutput] = result.outputs or ()
            usage, cost = result.usage, result.cost
            attempt.attempt_ref = result.attempt_ref
            if capabilities.submission == "async":
                crash.hit("images.async.before_ack_commit")
                async with self.rt.db.begin() as conn:
                    await store.update_reservation(
                        conn,
                        job.id,
                        attempt.token,
                        send_state=SendState.ACCEPTED.value,
                        attempt_ref=result.attempt_ref,
                    )
                collected = await handle.collect(result.attempt_ref)
                outputs, usage, cost = collected.outputs, collected.usage, collected.cost
        except adapter_errors.ImageAdapterError as exc:
            return await self._adapter_failed(job, attempt, exc)
        applied = (result.applied_parameters or AppliedParameters()).model_dump(mode="json")
        applied.update({"seed": attempt.seed, "seed_source": attempt.seed_source})
        attempt.applied = applied
        attempt.usage, attempt.cost = _usage(usage), _cost(cost)
        n = requested_outputs(job)
        # Every returned output is staged; those beyond ``n`` count (#262: used, overage,
        # extra_outputs) and are deleted unchecked once the settlement commits.
        written = await asyncio.to_thread(self._stage, job.id, attempt.token, list(outputs))
        staged = [o for o in written if o.index < n]
        extras = [o for o in written if o.index >= n]
        late: AttemptOutcome | None = None
        fenced = False
        async with self.rt.db.begin() as conn:
            current = await store.read_job(conn, job.id, lock=True)
            if current is None or current.terminal or current.attempt_token != attempt.token:
                # #267: the one thing a fenced worker may do is settle its own attempt.
                fenced = True
                late = await attempts.settle_attempt_in(
                    conn, job.id, attempt.token, len(outputs), cost=cost, usage=usage
                )
        if fenced:
            await asyncio.to_thread(self._remove_attempt, job.id, attempt.token)
            return {
                "image_job": "fenced",
                "late": attempts.LATE_UNACCOUNTED if late is None else late.value,
            }
        async with self.rt.db.begin() as conn:
            # The job row first, then the records: the settlement path's lock order (#268).
            await store.read_job(conn, job.id, lock=True)
            await store.update_reservation(
                conn,
                job.id,
                attempt.token,
                send_state=SendState.RETURNED.value,
                attempt_ref=attempt.attempt_ref,
                applied_parameters={**applied, "usage": attempt.usage, "cost": attempt.cost},
                outputs_returned=len(outputs),
            )
            if attempt.resend_of is not None:
                # #268: a re-send's outputs count once, on the re-sent attempt's record.
                await attempts.settle_attempt_in(
                    conn,
                    job.id,
                    attempt.token,
                    len(outputs),
                    cost=cost,
                    usage=usage,
                    held=AttemptOutcome.RESENT,
                    released=AttemptOutcome.RESENT,
                )
                record = None
            else:
                record = await limits.read_record(conn, job.id, attempt.token, lock=True)
            if record is not None and record.state is ReservationState.HELD:
                record = await limits.settle(conn, record, len(outputs))
            elif attempt.resend_of is None:
                # Released by an ending while the call ran (a cancel); #267 settles late returns.
                logger.info("image job %s: a return for a record that is not held", job.id)
            if record is not None:
                # #269: billed outputs cost what they cost, whatever becomes of them.
                await costs.record_attempt(conn, record, attempts.cost_entry(cost), len(outputs))
            if not staged:
                await store.update_reservation(
                    conn, job.id, attempt.token, outcome=AttemptOutcome.DISCARDED.value
                )
            else:
                moved = await store.set_status(
                    conn, job.id, ImageJobStatus.CHECKING, token=attempt.token
                )
                if not moved:
                    staged = []
        for extra in extras:
            await asyncio.to_thread(extra.path.unlink, missing_ok=True)
        if not staged:
            fresh = await self.read()
            if fresh is None or fresh.terminal or fresh.attempt_token != attempt.token:
                await asyncio.to_thread(self._remove_attempt, job.id, attempt.token)
                return {"image_job": "fenced"}
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason="provider_empty_result",
                token=attempt.token,
            )
            return {"image_job": ImageJobStatus.FAILED.value}
        attempt.outputs = staged
        crash.hit("images.after_stage")
        return None

    async def _adapter_failed(
        self, job: ImageJob, attempt: _Attempt, exc: adapter_errors.ImageAdapterError
    ) -> dict[str, Any]:
        # #269: a refusal before generation costs zero; any other failure's cost is unknown.
        # A failed re-send's cost lands on the attempt it re-sent (#268).
        entry = costs.of_error(exc.status, ambiguous=exc.ambiguous)
        billed = None if exc.ambiguous else 0
        costed = attempt.resend_of or attempt.token
        async with self.rt.db.begin() as conn:
            current = await store.read_job(conn, job.id, lock=True)
            if current is None or current.terminal or current.attempt_token != attempt.token:
                # #267: a fenced attempt whose call failed: no result arrived, so a record still
                # held is released; nothing else is touched.
                record = await limits.read_record(conn, job.id, attempt.token, lock=True)
                if record is not None and record.state is ReservationState.HELD:
                    await limits.release(conn, record)
                    await store.update_reservation(
                        conn,
                        job.id,
                        attempt.token,
                        send_state=SendState.RETURNED.value,
                        outcome=AttemptOutcome.FAILED.value,
                    )
                found = await limits.read_record(conn, job.id, costed)
                if found is not None:
                    await costs.record_attempt(conn, found, entry, billed)
                fenced = True
            else:
                fenced = False
        if fenced:
            return {"image_job": "fenced", "error": exc.code}
        async with self.rt.db.begin() as conn:
            await store.read_job(conn, job.id, lock=True)  # the settlement path's lock order
            await store.update_reservation(
                conn,
                job.id,
                attempt.token,
                send_state=SendState.RETURNED.value,
                outcome=AttemptOutcome.FAILED.value,
            )
            # No provider result arrived: the attempt's reservation is released (#262), and a
            # failed re-send's (#268) the re-sent attempt's too.
            for token in (attempt.token, attempt.resend_of):
                if token is None:
                    continue
                record = await limits.read_record(conn, job.id, token, lock=True)
                if record is not None and record.state is ReservationState.HELD:
                    await limits.release(conn, record)
                    if token != attempt.token:
                        await store.update_reservation(
                            conn, job.id, token, outcome=AttemptOutcome.FAILED.value
                        )
            found = await limits.read_record(conn, job.id, costed)
            if found is not None:
                await costs.record_attempt(conn, found, entry, billed)
        if not exc.retryable:
            await end_job(
                self.rt, job.id, ImageJobStatus.FAILED, reason=_reason_of(exc), token=attempt.token
            )
            return {"image_job": ImageJobStatus.FAILED.value, "error": exc.code}
        if self.last_try:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason="provider_unavailable",
                token=attempt.token,
            )
            return {"image_job": ImageJobStatus.FAILED.value, "error": exc.code}
        async with self.rt.db.begin() as conn:
            begun = await self._next_attempt(conn, job, attempt.token)
            if isinstance(begun, str):
                await store.set_status(conn, job.id, ImageJobStatus.QUEUED, token=begun)
        if isinstance(begun, dict):
            await _delete_staging(self.rt.media, job.id)
            return begun
        raise ImageJobRetry(exc.code)

    # -- checks and promotion ---------------------------------------------------------------

    async def _check_and_promote(
        self,
        job: ImageJob,
        attempt: _Attempt,
        target: ImageTarget,
        settings: ImageJobSettings,
        handle: ImageGeneratorHandle,
    ) -> dict[str, Any] | _Attempt:
        rechecking = False
        if revalidates(target):
            early = await self._before_checks(job, attempt, target)
            if early is not None:
                return early
            fresh = await self.read()
            if fresh is None or fresh.terminal or fresh.attempt_token != attempt.token:
                return {"image_job": None if fresh is None else "fenced"}
            job = fresh
            rechecking = _passed_before(job, attempt.token)
        checks = image_checks()
        passing: list[tuple[StagedOutput, OutputVerdict]] = []
        results: list[dict[str, Any]] = []
        rejected: list[str] = []
        for output in attempt.outputs:
            try:
                verdict = await checks.check_output(self.rt, job, output)
            except ImageCheckUnconfigured as exc:
                await end_job(
                    self.rt,
                    job.id,
                    ImageJobStatus.FAILED,
                    reason=FAIL_CLOSED,
                    token=attempt.token,
                    policy_ids=exc.policy_ids,
                    outcome=AttemptOutcome.DISCARDED,
                )
                return {"image_job": ImageJobStatus.FAILED.value, "reason": FAIL_CLOSED}
            except Exception as exc:
                return await self._check_unavailable(job, attempt, exc)
            for v in verdict.per_policy:
                results.append(
                    {
                        "attempt_token": attempt.token,
                        "output": output.index,
                        "policy_id": v.policy_id,
                        "result": v.result,
                    }
                )
            if verdict.passed:
                passing.append((output, verdict))
                continue
            rejected.extend(p for p in verdict.violations if p not in rejected)
            await asyncio.to_thread(output.path.unlink, missing_ok=True)
            await audit.record(
                self.rt.db,
                AuditKind.IMAGE_OUTPUT_REJECTED,
                principal=SYSTEM_PRINCIPAL,
                story_id=job.story_id,
                policy_ids=verdict.violations,
                details={
                    "job_id": job.id,
                    "attempt_token": attempt.token,
                    "output": output.index,
                    "outcome": AttemptOutcome.REJECTED.value,
                },
            )
        async with self.rt.db.begin() as conn:
            await store.append_policy_results(conn, job.id, results)
        if rechecking and rejected:
            # #264: an output that had passed fails the policies in force now: the source
            # changed, so the job ends ``stale`` and nothing is generated again.
            return await self._stale(
                job, attempt, Stale("policy_changed", {"policy_ids": list(rejected)})
            )
        if passing:
            return await self._promote(job, attempt, target, passing, handle)
        return await self._retry_or_fail(job, attempt, settings, rejected)

    # -- revalidation (#264) ----------------------------------------------------------------

    async def _before_checks(
        self, job: ImageJob, attempt: _Attempt, target: ImageTarget
    ) -> dict[str, Any] | None:
        """Before a revalidating target's outputs are checked: its revalidation (a gone target,
        a pause or a stale ending no later state undoes ends or defers the work before any
        model call), then its ``refresh`` (the prompt check again, a fresh safety context and
        the policy set it ran under). The run's result when it stops here, else ``None``."""
        waited: dict[str, Any] | None = None
        async with self.rt.db.begin() as conn:
            try:
                found = await revalidate(target, conn, job)
            except TargetGone:
                found = Gone()
            if isinstance(found, Wait):
                waited = await self._wait_in(conn, job, attempt.token, found)
            elif job.waiting:
                await store.update_job(conn, job.id, status_detail=None)  # the pause is over
        if isinstance(found, Gone):
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.TARGET_GONE,
                token=attempt.token,
                outcome=AttemptOutcome.DISCARDED,
            )
            return {"image_job": ImageJobStatus.TARGET_GONE.value}
        if waited is not None:
            return waited
        if isinstance(found, Stale) and found.reason in EARLY_STALE:
            return await self._stale(job, attempt, found)
        refresh = getattr(target, "refresh")  # noqa: B009 - an optional seam (``revalidates``)
        try:
            stale = await refresh(self.rt, job)
        except Exception as exc:
            return await self._check_unavailable(job, attempt, exc)
        if stale is not None:
            return await self._stale(job, attempt, stale)
        return None

    async def _wait_in(
        self, conn: AsyncConnection, job: ImageJob, token: str, wait: Wait
    ) -> dict[str, Any]:
        """Defer the job in ``conn``'s transaction (``Wait``): ``checking`` with ``status_detail
        {waiting_on: pause, next_check_at}``, its first wait's time kept, its staged bytes and
        reservation untouched; the run's result asks #70 to run it again in ``recheck_s``."""
        moved = await store.set_status(
            conn,
            job.id,
            ImageJobStatus.CHECKING,
            token=token,
            status_detail={
                "waiting_on": WAITING_ON_PAUSE,
                "next_check_at": wait.next_check_at.isoformat(),
            },
            wait_started_at=func.coalesce(image_jobs.c.wait_started_at, clock.now()),
        )
        if not moved:
            return {"image_job": "fenced"}
        logger.info("image job %s waits on its paused story (%d s)", job.id, wait.recheck_s)
        return {
            "image_job": ImageJobStatus.CHECKING.value,
            "waiting_on": WAITING_ON_PAUSE,
            REQUEUE_IN: wait.recheck_s,
        }

    async def _stale(self, job: ImageJob, attempt: _Attempt, stale: Stale) -> dict[str, Any]:
        """End the job ``stale`` (its reason and detail, staged bytes deleted, reservations
        released, an audit row): nothing is attached."""
        ended = await end_job(
            self.rt,
            job.id,
            ImageJobStatus.STALE,
            reason=stale.reason,
            token=attempt.token,
            policy_ids=list(stale.detail.get("policy_ids") or ()),
            outcome=AttemptOutcome.DISCARDED,
            detail=stale.detail,
        )
        if not ended:
            return {"image_job": "fenced"}
        return {"image_job": ImageJobStatus.STALE.value, "reason": stale.reason}

    async def _check_unavailable(
        self, job: ImageJob, attempt: _Attempt, exc: Exception
    ) -> dict[str, Any]:
        logger.info("image job %s: output check unavailable: %s", job.id, type(exc).__name__)
        if self.last_try:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason=ImageCheckUnavailable.code,
                token=attempt.token,
                outcome=AttemptOutcome.DISCARDED,
            )
            return {"image_job": ImageJobStatus.FAILED.value, "reason": ImageCheckUnavailable.code}
        raise ImageJobRetry(ImageCheckUnavailable.code)

    async def _retry_or_fail(
        self,
        job: ImageJob,
        attempt: _Attempt,
        settings: ImageJobSettings,
        rejected: list[str],
    ) -> dict[str, Any] | _Attempt:
        async with self.rt.db.begin() as conn:
            await store.update_reservation(
                conn, job.id, attempt.token, outcome=AttemptOutcome.REJECTED.value
            )
            used = sum(
                1
                for r in await store.reservations(conn, job.id)
                if r.outcome is AttemptOutcome.REJECTED
            )
            begun: str | dict[str, Any] | None = None
            if used <= settings.image_retry_budget:
                begun = await self._next_attempt(conn, job, attempt.token)
                if isinstance(begun, str):
                    await store.set_status(conn, job.id, ImageJobStatus.GENERATING, token=begun)
        await asyncio.to_thread(self._remove_attempt, job.id, attempt.token)
        if isinstance(begun, dict):
            await _delete_staging(self.rt.media, job.id)
            return begun
        token = begun
        if token is None:
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.FAILED,
                reason="guardrail_violation",
                token=attempt.token,
                policy_ids=rejected,
            )
            return {"image_job": ImageJobStatus.FAILED.value, "reason": "guardrail_violation"}
        seed, source = self._seed(job)
        return _Attempt(token, seed, source)

    def _metadata(
        self, job: ImageJob, attempt: _Attempt, handle: ImageGeneratorHandle
    ) -> dict[str, Any]:
        return {
            "adapter": handle.adapter,
            "model": handle.model,
            "endpoint": attempt.applied.get("endpoint"),
            "prompt": job.prompt,
            "negative_prompt": job.negative_prompt,
            "requested_parameters": {
                k: job.params.get(k)
                for k in ("n", "seed", "size", "aspect_ratio", "quality", "reference_image_ids")
            },
            "applied_parameters": {
                k: v for k, v in attempt.applied.items() if k not in ("usage", "cost")
            },
            "reference_image_ids": list(job.params.get("reference_image_ids") or []),
            "usage": attempt.usage,
            "cost": attempt.cost,
        }

    async def _promote(
        self,
        job: ImageJob,
        attempt: _Attempt,
        target: ImageTarget,
        passing: Sequence[tuple[StagedOutput, OutputVerdict]],
        handle: ImageGeneratorHandle,
    ) -> dict[str, Any]:
        metadata = self._metadata(job, attempt, handle)
        promotable: list[PromotableOutput] = []
        for output, verdict in passing:
            copy = output.path.with_name(f".{output.index}.promote")
            try:
                await asyncio.to_thread(shutil.copyfile, output.path, copy)
            except FileNotFoundError:
                # A cancel (or another ending) removed the staging: this attempt is fenced.
                current = await self.read()
                if current is None or current.terminal or current.attempt_token != attempt.token:
                    logger.info("image job %s: promotion refused, the attempt was fenced", job.id)
                    return {"image_job": "fenced"}
                raise
            await asyncio.to_thread(
                self.rt.media.put, copy, output.sha256, content_type=output.media_type
            )
            promotable.append(
                PromotableOutput(
                    output.index,
                    output.sha256,
                    output.media_type,
                    output.size,
                    output.width,
                    output.height,
                    verdict,
                    metadata,
                )
            )
        asset_ids: list[str] = []
        try:
            fenced, asset_ids, outcome = await self._promote_in_transaction(
                job, attempt, target, promotable
            )
        except FencedToken:
            fenced, outcome = True, OK
        except TargetStale as exc:
            # Nothing was attached (the transaction rolled back): the placement failed.
            return await self._stale(job, attempt, Stale(exc.reason, exc.detail))
        except TargetGone:
            # Nothing was promoted (the transaction rolled back); the blobs already copied to
            # their keys are left to the media sweep's orphan step.
            await end_job(
                self.rt,
                job.id,
                ImageJobStatus.TARGET_GONE,
                token=attempt.token,
                outcome=AttemptOutcome.DISCARDED,
            )
            return {"image_job": ImageJobStatus.TARGET_GONE.value}
        if fenced:
            logger.info("image job %s: promotion refused, the attempt was fenced", job.id)
            await asyncio.to_thread(self._remove_attempt, job.id, attempt.token)
            return {"image_job": "fenced"}
        if isinstance(outcome, Wait):
            return {
                "image_job": ImageJobStatus.CHECKING.value,
                "waiting_on": WAITING_ON_PAUSE,
                REQUEUE_IN: outcome.recheck_s,
            }
        if isinstance(outcome, Stale):
            await _delete_staging(self.rt.media, job.id)
            logger.info("image job %s ended stale (%s)", job.id, outcome.reason)
            return {"image_job": ImageJobStatus.STALE.value, "reason": outcome.reason}
        if isinstance(outcome, NeedsEvaluation):
            self.evaluations += 1
            if self.evaluations > MAX_EVALUATIONS:
                raise ImageJobRetry("needs_evaluation")
            logger.info(
                "image job %s: %d policy(ies) to evaluate before promotion",
                job.id,
                len(outcome.policy_ids),
            )
            attempt.outputs = [output for output, _ in passing]
            current = await self.read()
            if current is None or current.terminal or current.attempt_token != attempt.token:
                return {"image_job": "fenced"}
            ended = await self._check_and_promote(current, attempt, target, self.settings, handle)
            if isinstance(ended, _Attempt):  # a story job never retries a passed output
                raise ImageJobRetry("needs_evaluation")
            return ended
        await _delete_staging(self.rt.media, job.id)
        logger.info("image job %s promoted %d output(s)", job.id, len(asset_ids))
        return {"image_job": target.success_status.value, "assets": len(asset_ids)}

    async def _promote_in_transaction(
        self,
        job: ImageJob,
        attempt: _Attempt,
        target: ImageTarget,
        promotable: Sequence[PromotableOutput],
    ) -> tuple[bool, list[str], Revalidation]:
        """The promotion's one transaction: ``(fenced, asset_ids, revalidation)``. A
        revalidating target is revalidated first (it locks its own row before the job's, #47's
        order): ``Gone`` raises ``TargetGone``, ``Wait`` defers the job and ``Stale`` ends it in
        this transaction, ``NeedsEvaluation`` attaches nothing. ``TargetGone`` or
        ``TargetStale`` from the target's ``promote`` rolls it back."""
        asset_ids: list[str] = []
        async with self.rt.db.begin() as conn:
            outcome: Revalidation = OK
            check = getattr(target, "revalidate", None) if revalidates(target) else None
            if check is not None:
                outcome = await check(conn, job)
                count_revalidation(outcome.outcome)
            current = await store.read_job(conn, job.id, lock=True)
            if (
                current is None
                or current.terminal
                or current.attempt_token != attempt.token
                or current.status is not ImageJobStatus.CHECKING
            ):
                fenced = True
            elif isinstance(outcome, Gone):
                raise TargetGone(f"job {job.id}")
            elif isinstance(outcome, Wait):
                fenced = (await self._wait_in(conn, current, attempt.token, outcome)).get(
                    "image_job"
                ) == "fenced"
            elif isinstance(outcome, Stale):
                fenced = not await _end_in(
                    conn,
                    job.id,
                    ImageJobStatus.STALE,
                    reason=outcome.reason,
                    token=attempt.token,
                    policy_ids=list(outcome.detail.get("policy_ids") or ()),
                    outcome=AttemptOutcome.DISCARDED,
                    detail=outcome.detail,
                )
            elif isinstance(outcome, NeedsEvaluation):
                fenced = False
            else:
                fenced = False
                # #267: promotion and attachment refuse a fenced token (``FencedToken``).
                await store.require_token(conn, job.id, attempt.token)
                for ready in promotable:
                    await lock_checksum(conn, ready.sha256)
                asset_ids = await target.promote(conn, current, promotable)
                await store.set_status(
                    conn,
                    job.id,
                    target.success_status,
                    token=attempt.token,
                    asset_ids=asset_ids,
                    status_reason=None,
                    status_detail=None,
                )
                await store.update_reservation(
                    conn, job.id, attempt.token, outcome=AttemptOutcome.PROMOTED.value
                )
                await limits.release_all(conn, job.id, current_token=attempt.token)
                ended = await store.read_job(conn, job.id)
                assert ended is not None  # noqa: S101 - locked above
                await target.on_terminal(conn, ended)
                crash.hit("images.promote.before_commit")
        return fenced, asset_ids, outcome


def _needs_preparing(job: ImageJob) -> bool:
    """Whether ``job`` is an automatic one (#265) that has not built its prompt yet."""
    auto = job.params.get("auto")
    return isinstance(auto, Mapping) and not auto.get("prepared")


def _passed_before(job: ImageJob, token: str) -> bool:
    """Whether an output of the attempt ``token`` has already passed its checks (#264: a
    re-check that fails it is ``stale``, never a regeneration)."""
    return any(
        r.get("attempt_token") == token and r.get("result") == "pass" for r in job.policy_results
    )


async def run_image_job(runtime: ImageRuntime, job_id: str, failures: int) -> dict[str, Any]:
    """Run one claimed image job (module docstring); the ``jobs`` row's result (codes and
    counts). Raises ``ImageJobRetry`` to have #70 back off and run it again."""
    return await ImageJobRunner(runtime, job_id, failures).run()


async def insert_output_asset(
    conn: AsyncConnection,
    job: ImageJob,
    output: PromotableOutput,
    *,
    state: MediaState,
    owner: str,
    content_rating_version_id: str,
    depicted_subjects: Sequence[Mapping[str, Any]] = (),
    source: MediaSource = MediaSource.GENERATED,
    viewpoint_basis: str | None = None,
    origin: MediaOrigin | None = None,
) -> str:
    """A target's promote: the output's ``media_assets`` row, its ``generation_metadata``
    holding adapter, model, endpoint, prompt, negative prompt, requested and applied parameters,
    reference image ids, usage and cost, and the output check's fields (#258: ``check_status``,
    the checker's model and endpoint host, the per-policy results; #266 adds the depicted
    subjects). ``source`` is ``generated`` for an Epistrel-built prompt and
    ``generated_from_consumer_prompt`` for a consumer's (the target knows which, #263), and a
    story image's ``viewpoint_basis`` (``filtered_inputs`` or ``protected_term_match``). The row
    names the output (#268: the job, the attempt token it is promoted under and its index in the
    provider's return order), whose unique index refuses a second promotion of it. A subject
    image's ``origin`` is ``subject`` (#830)."""
    verdict = output.verdict
    passed = verdict.check_status == "passed"
    return await store.insert_generated_asset(
        conn,
        checksum=output.sha256,
        mime_type=output.media_type,
        size=output.size,
        width=output.width,
        height=output.height,
        owner=owner,
        content_rating_version_id=content_rating_version_id,
        generation_metadata=output.generation_metadata,
        state=state,
        created_by=job.principal,
        alt_text=job.params.get("alt_text"),
        caption=job.params.get("caption"),
        depicted_subjects=depicted_subjects,
        source=source,
        check_status=CheckStatus.PASSED if passed else CheckStatus.UNCHECKED,
        check_model=verdict.model if passed else None,
        check_endpoint=verdict.endpoint if passed else None,
        policy_results=verdict.policy_results() if passed else [],
        viewpoint_basis=viewpoint_basis,
        source_job_id=job.id,
        source_attempt_token=job.attempt_token,
        output_index=output.index,
        origin=None if origin is None else origin.value,
    )


# -- the read -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptView:
    """One provider attempt as a job read reports it (#267): its token, its outcome (``None``
    while in flight), what it reserved, and once settled what it used, its overage and extra
    outputs (``None`` while ``held``). #268 adds its ``send_state`` (``ambiguous`` for an
    ambiguous send), its settlement ``basis`` (``actual``, ``presumed`` or ``corrected``) and,
    for an idempotent re-send, the attempt it re-sent (``resend_of``). #269: its ``image_costs``
    row's ``cost`` (a ``Decimal``; ``None`` unless known or zero), ``cost_status`` (``known``,
    ``zero`` or ``unknown``), ``currency``, ``outputs_billed`` and ``accounting``, all ``None``
    while the attempt has no cost row (held, never sent, or a re-send, whose cost is on the
    attempt it re-sent)."""

    attempt_token: str
    outcome: str | None
    requested: int
    used: int | None
    overage: int | None
    extra_outputs: int | None
    started_at: datetime | None
    send_state: str = SendState.SENDING.value
    basis: str = SettlementBasis.ACTUAL.value
    resend_of: str | None = None
    cost: Decimal | None = None
    cost_status: str | None = None
    currency: str | None = None
    outputs_billed: int | None = None
    accounting: str | None = None


@dataclass(frozen=True, slots=True)
class ImageJobView:
    """``GET /image-jobs/{id}``: the job's state. A story or subject job answers
    ``asset_ids``, a catalog job ``candidate_ids`` (``result_field``), always ``[]`` until it
    succeeds; ``policy_ids`` when it failed on a policy. ``attempts`` lists every provider
    attempt by ``started_at`` (#267)."""

    job_id: str
    kind: str
    status: str
    status_reason: str | None
    result_field: Literal["asset_ids", "candidate_ids"]
    result_ids: tuple[str, ...]
    attempts: tuple[AttemptView, ...]
    created_at: datetime
    updated_at: datetime
    policy_ids: tuple[str, ...] | None = None
    replayed: bool = False
    status_detail: dict[str, Any] | None = None
    waiting_on: str | None = None
    """#264: ``pause`` while a story job waits on its paused story (status ``checking``)."""
    next_check_at: datetime | None = None
    """#264: when a waiting job revalidates next."""
    waiting_for_horizon: bool = False
    """#267: a lease expiry fenced an attempt in flight; no attempt starts before its horizon."""
    confirm_deadline_at: datetime | None = None
    """#267: when a confirming adapter's status polling for a stalled attempt gives up."""
    confirm_fallback: bool = False
    """#267 (sticky): the job fell back to the non-confirming rule."""
    usage: costs.JobUsage | None = None
    """#269: the job's ``outputs_used``, ``overage``, ``extra_outputs``, ``ambiguous_sends`` and
    its cost totals (per currency, with the ``unknown`` and ``zero`` attempts), on read."""
    failures: tuple[Mapping[str, Any], ...] | None = None
    """#830: a completed subject job's outputs that were not stored, ``{index, reason}``
    (codes only); ``None`` for every other job."""


async def attempt_views(conn: AsyncConnection, job_id: str) -> tuple[AttemptView, ...]:
    """The job's provider attempts (records with a ``started_at``), by ``started_at``."""
    records = {r.attempt_token: r for r in await limits.records(conn, job_id)}
    billed = await costs.attempt_costs(conn, job_id)
    found = [r for r in await store.reservations(conn, job_id) if r.started_at is not None]
    found.sort(key=lambda r: (r.started_at or r.created_at, r.attempt_token))
    views: list[AttemptView] = []
    for row in found:
        record = records.get(row.attempt_token)
        held = record is None or record.state is ReservationState.HELD
        cost = billed.get(row.attempt_token)
        views.append(
            AttemptView(
                attempt_token=row.attempt_token,
                outcome=None if row.outcome is None else row.outcome.value,
                requested=0 if record is None else record.requested,
                used=None if held or record is None else record.used,
                overage=None if held or record is None else record.overage,
                extra_outputs=None if held or record is None else record.extra_outputs,
                started_at=row.started_at,
                send_state=row.send_state.value,
                basis=SettlementBasis.ACTUAL.value if record is None else record.basis.value,
                resend_of=row.resend_of,
                cost=None if cost is None else cost.cost,
                cost_status=None if cost is None else cost.cost_status.value,
                currency=None if cost is None else cost.currency,
                outputs_billed=None if cost is None else cost.outputs_billed,
                accounting=None if cost is None else cost.accounting.value,
            )
        )
    return tuple(views)


def _default_result(job: ImageJob) -> tuple[str, list[str]]:
    if job.kind is ImageJobKind.CATALOG:
        return "candidate_ids", list(job.asset_ids)
    return "asset_ids", list(job.asset_ids)


async def read_view(conn: AsyncConnection, job_id: str) -> ImageJobView:
    """The job's view; 404 ``image_job_not_found``."""
    job = await store.read_job(conn, job_id)
    if job is None:
        raise _not_found(job_id)
    handler = target_for(job.kind)
    field_name, ids = (
        await handler.result_ids(conn, job) if handler is not None else _default_result(job)
    )
    if job.status not in SUCCESSES:
        ids = []
    policy_ids = job.policy_ids if job.status is ImageJobStatus.FAILED else []
    detail = job.status_detail if job.status in DETAILED_ENDINGS else None
    waiting_on = next_check_at = None
    if job.waiting:
        assert job.status_detail is not None  # noqa: S101 - waiting
        waiting_on = str(job.status_detail["waiting_on"])
        found = job.status_detail.get("next_check_at")
        next_check_at = None if found is None else datetime.fromisoformat(str(found))
    return ImageJobView(
        job_id=job.id,
        kind=job.kind.value,
        status=job.status.value,
        status_reason=job.status_reason,
        result_field="candidate_ids" if field_name == "candidate_ids" else "asset_ids",
        result_ids=tuple(ids),
        attempts=await attempt_views(conn, job.id),
        created_at=job.created_at,
        updated_at=job.updated_at,
        policy_ids=tuple(policy_ids) if policy_ids else None,
        status_detail=detail,
        waiting_on=waiting_on,
        next_check_at=next_check_at,
        waiting_for_horizon=job.waiting_for_horizon,
        confirm_deadline_at=job.confirm_deadline_at,
        confirm_fallback=job.confirm_fallback,
        usage=await costs.job_usage(conn, job.id),
        failures=_failures(job),
    )


def _failures(job: ImageJob) -> tuple[Mapping[str, Any], ...] | None:
    if job.kind is not ImageJobKind.SUBJECT or job.status is not ImageJobStatus.COMPLETED:
        return None
    found = (job.result or {}).get("failures") or ()
    return tuple(
        {"index": int(f["index"]), "reason": str(f["reason"])}
        for f in found
        if isinstance(f, Mapping)
    )


SUCCESSES: frozenset[ImageJobStatus] = frozenset(
    {ImageJobStatus.PROMOTED, ImageJobStatus.CANDIDATES_READY, ImageJobStatus.COMPLETED}
)
"""The endings whose result ids a read answers: a story job's ``promoted``, a catalog job's
``candidates_ready`` and a subject job's ``completed`` (#830)."""
DETAILED_ENDINGS: frozenset[ImageJobStatus] = frozenset(
    {ImageJobStatus.CAP_REACHED, ImageJobStatus.STALE}
)
"""Endings whose ``status_detail`` a read answers: ``cap_reached`` (#262), ``stale`` (#264)."""
STATUSES: frozenset[str] = frozenset(s.value for s in ImageJobStatus)


def check_wait(wait: int | None, since_status: str | None) -> None:
    """422 ``validation_error`` for ``wait`` outside 1..30 or an unknown ``since_status``."""
    if wait is not None and not WAIT_MIN_S <= wait <= WAIT_MAX_S:
        raise InvalidRequest(
            f"wait must be {WAIT_MIN_S} to {WAIT_MAX_S} seconds", {"field": "query.wait"}
        )
    if since_status is not None and since_status not in STATUSES:
        raise InvalidRequest(
            "since_status is not an image job status", {"field": "query.since_status"}
        )


class ImageJobWaiters:
    """The long poll's waiters in this process (at most ``MAX_WAITERS``): one shared ``LISTEN``
    connection fans each job's notifications out to its waiters, and once a second one batched
    read of every waited-on job's status wakes any whose status changed, so a missed
    notification never makes a read wait longer. Waiters hold no pool connection while they
    wait. The background task runs only while someone waits."""

    def __init__(self, db: Callable[[], AsyncEngine]) -> None:
        self._db = db
        self._waiters: dict[str, list[tuple[str, asyncio.Event]]] = {}
        self._count = 0
        self._task: asyncio.Task[None] | None = None
        self.listen = True
        """Whether to ``LISTEN`` (tests turn it off to prove the polling fallback)."""

    @property
    def waiting(self) -> int:
        return self._count

    def _add(self, job_id: str, since: str) -> asyncio.Event:
        if self._count >= MAX_WAITERS:
            raise TooManyWaiters("too many long-polling reads are waiting", {"limit": MAX_WAITERS})
        event = asyncio.Event()
        self._waiters.setdefault(job_id, []).append((since, event))
        self._count += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="epistrel-image-job-waiters")
        return event

    def _remove(self, job_id: str, event: asyncio.Event) -> None:
        found = self._waiters.get(job_id, [])
        kept = [(s, e) for s, e in found if e is not event]
        if len(kept) != len(found):
            self._count -= 1
        if kept:
            self._waiters[job_id] = kept
        else:
            self._waiters.pop(job_id, None)

    def _wake(self, job_id: str, status: str | None) -> None:
        for since, event in self._waiters.get(job_id, []):
            if status is None or status != since:
                event.set()

    async def wait(self, job_id: str, since: str, timeout_s: float) -> None:
        """Until job ``job_id`` may have left ``since`` (woken), or ``timeout_s``."""
        event = self._add(job_id, since)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout_s)
        finally:
            self._remove(job_id, event)

    async def _poll(self) -> None:
        ids = list(self._waiters)
        if not ids:
            return
        async with self._db().connect() as conn:
            found = await store.read_statuses(conn, ids)
        for job_id in ids:
            self._wake(job_id, found.get(job_id, ""))

    async def _run(self) -> None:
        conn: psycopg.AsyncConnection[Any] | None = None
        listening: set[str] = set()
        try:
            while self._waiters:
                if self.listen and conn is None:
                    conn = await self._connect()
                    listening = set()
                if conn is not None:
                    try:
                        wanted = {store.channel(j) for j in self._waiters}
                        for name in sorted(wanted - listening):
                            await conn.execute(f'LISTEN "{name}"')
                        for name in sorted(listening - wanted):
                            await conn.execute(f'UNLISTEN "{name}"')
                        listening = wanted
                        async for note in conn.notifies(timeout=POLL_INTERVAL_S):
                            job_id = note.channel.removeprefix(store.CHANNEL_PREFIX)
                            for waited in list(self._waiters):
                                if waited.lower() == job_id:
                                    self._wake(waited, note.payload)
                    except Exception as exc:
                        logger.warning("image job LISTEN lost: %s", type(exc).__name__)
                        with contextlib.suppress(Exception):
                            await conn.close()
                        conn = None
                        await asyncio.sleep(LISTEN_RECONNECT_S)
                else:
                    await asyncio.sleep(POLL_INTERVAL_S)
                try:
                    await self._poll()
                except Exception as exc:
                    logger.warning("image job poll failed: %s", type(exc).__name__)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()

    async def _connect(self) -> psycopg.AsyncConnection[Any] | None:
        from epistrel.orchestrator.jobs.worker import listen_dsn  # here: the worker is heavy

        try:
            return await psycopg.AsyncConnection.connect(listen_dsn(self._db()), autocommit=True)
        except Exception as exc:
            logger.warning("image job LISTEN unavailable: %s", type(exc).__name__)
            return None

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def get_job(
    db: AsyncEngine,
    waiters: ImageJobWaiters,
    job_id: str,
    *,
    wait: int | None = None,
    since_status: str | None = None,
) -> ImageJobView:
    """The job's view now, or with ``wait``: once its status differs from ``since_status``
    (default: its status at the request) or after ``wait`` seconds, whichever is first."""
    check_wait(wait, since_status)
    async with db.connect() as conn:
        view = await read_view(conn, job_id)
    if wait is None:
        return view
    since = since_status or view.status
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while view.status == since:
        left = deadline - loop.time()
        if left <= 0:
            break
        await waiters.wait(job_id, since, left)
        async with db.connect() as conn:
            view = await read_view(conn, job_id)
    return view


# -- the staging sweep ----------------------------------------------------------------------

SWEEP_PAGE = 1_000


async def sweep_staging(db: AsyncEngine, media: BlobStore, *, ttl_s: float) -> dict[str, int]:
    """``image_staging_sweep``: delete each ``tmp/images/<job>/`` directory older than
    ``ttl_s`` whose job is terminal or missing; a non-terminal job's is skipped however old.
    Listed in pages of ``SWEEP_PAGE``, each delete on its own; counts only."""
    found = await asyncio.to_thread(lambda: list(media.staged_image_dirs()))
    terminal = {s.value for s in TERMINAL_STATUSES}
    removed = skipped = 0
    for start in range(0, len(found), SWEEP_PAGE):
        page = [(j, p, a) for j, p, a in found[start : start + SWEEP_PAGE] if a >= ttl_s]
        if not page:
            continue
        async with db.connect() as conn:
            statuses = await store.read_statuses(conn, [j for j, _, _ in page])
        for job_id, _, _ in page:
            status = statuses.get(job_id)
            if status is not None and status not in terminal:
                skipped += 1
                continue
            try:
                removed += await asyncio.to_thread(media.remove_image_staging, job_id)
            except Exception as exc:
                logger.warning("image staging %s not removed: %s", job_id, type(exc).__name__)
    logger.info("image staging sweep: %d removed, %d of running jobs kept", removed, skipped)
    return {"removed": removed, "skipped_running": skipped}


__all__ = [
    "IMAGE_JOB_CODEC",
    "IMAGE_JOB_KIND",
    "JOB_LOCATION",
    "MAX_EVALUATIONS",
    "MAX_WAITERS",
    "PRE_COMMIT_HOOKS",
    "REQUEUE_IN",
    "SUCCESSES",
    "WAITING_ON_PAUSE",
    "AttemptView",
    "DepictedSubject",
    "FailClosedChecks",
    "Gone",
    "ImageCheckRequired",
    "ImageCheckUnavailable",
    "ImageChecks",
    "ImageJobNotFound",
    "ImageJobQueued",
    "ImageJobRequest",
    "ImageJobRetry",
    "ImageJobView",
    "ImageJobWaiters",
    "ImageRuntime",
    "ImageTarget",
    "NeedsEvaluation",
    "Ok",
    "OutputVerdict",
    "PolicyVerdict",
    "PreparedImageJob",
    "PromotableOutput",
    "PromptCheck",
    "Revalidation",
    "StagedOutput",
    "Stale",
    "TargetGone",
    "TargetStale",
    "TooManyWaiters",
    "Wait",
    "attempt_views",
    "bind",
    "bound",
    "cancel_job",
    "check_request",
    "check_wait",
    "end_job",
    "enqueue_image_job",
    "get_job",
    "image_checks",
    "image_output_policy_set",
    "insert_image_job",
    "insert_output_asset",
    "prepare_image_job",
    "read_view",
    "register_target",
    "require_generation",
    "require_image_checker",
    "revalidate",
    "revalidates",
    "run_image_job",
    "set_image_checks",
    "sweep_staging",
    "target_for",
    "unregister_target",
]
