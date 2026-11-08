"""The image-output check: every generated or uploaded image against the policies that apply to
image outputs, in private staging, before anything makes it addressable (FR-IMG-3,10,12,13;
NFR-QUAL-12; #258).

``check_output(models, db, image, prompt, caption, alt_text, safety_context, policies)`` judges
one image against the applicable ``policies`` (``output_policies``: those for ``image_output``,
the seeded minors policy always while it exists, a character policy only for a resolved subject)
and answers an ``OutputVerdict``:

- **Deterministic** policies run locally on the prompt, caption and alt text, each on its own.
- **Model-assisted** policies are judged together in **one** ``image_check`` call (a
  vision-capable model, #254): the payload is exactly #257's (the prompt, caption and alt text,
  each policy's id and text, the safety context's subjects as opaque labels with true ages or a
  figure's class and the prompt's own descriptors) plus the image as a base64 ``data:`` URL
  downscaled to at most ``MAX_SIDE`` px, sent as an image part of the user message
  (``prompts.with_images``). The answer is ``{verdicts: {<policy_id>: pass|violation|
  uncertain}}``; an omitted id, or an answer that never validates after #53's retries, counts as
  ``uncertain``. A model that cannot answer (``ModelError``) propagates: the caller reports
  ``image_check_unavailable``.
- An **uncertain** result follows the policy's ``on_uncertain`` (#49's ``rule.on_uncertain``:
  ``violation`` is ``block``, ``pass`` is ``allow``): ``block`` → a violation, ``allow`` → a
  stored pass with ``uncertain: true``.
- **Certification** (NFR-QUAL-12): each result says whether the checker is certified for that
  policy: the newest ``valid`` ``image_check_certifications`` record for ``(checker_fingerprint,
  policy_id)`` (``core.profiles.certification.image_check_certified``; #270: an E-11 run at the
  policy's current version, or for the minors policy an external attestation, ADR-182). The
  stored flag is a snapshot of check time: a later invalidation changes only later checks.
  Never one status for the checker. ``checker_fingerprint`` is the first 12 hex of the
  SHA-256 of ``canonical_json({base_url_host, model, reported_version, template_sha256})``: the
  endpoint's host, the model id, the version the Model Access Layer last recorded for that model
  (``reported_version.latest``; ``null`` when none was reported) and the SHA-256 of
  ``image_check.j2``, so an edited checker prompt loses its certification (FR-PROF-16). #270 and
  #271 compute the same fingerprint (``checker_fingerprint``).

With no applicable policy no call is made, and the image stays ``unchecked``.

**Where it runs.** ``OutputChecks`` is the default ``check_output`` behind #256's
``ImageChecks`` seam: a job's output is checked with the job's stored safety context
(``SafetyContext.from_json``) against the target's effective policies (a story's, or a catalog
entry's global and rating policies), certification lookups cached per job; ``image_check`` unset
while a policy applies fails closed (``ImageCheckUnconfigured``), and an unavailable checker is
``ImageCheckUnavailable`` (retried on the same bytes). ``UploadCheck`` is the check
``POST /media`` and a resumable upload's completion run on staged image bytes and on a video's
poster (``core.media.assets.prepare``): with no checker configured the upload is ``unchecked``,
as FR-IMG-3 allows for uploads, and the consumer's moderation hook is the control.

The check is a **backstop with measured error rates** (NFR-QUAL-12), not an assurance that every
image is safe. Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from importlib import resources
from typing import TYPE_CHECKING, Literal

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import audit
from epistrel.core.errors import EpistrelError, StoryNotFound
from epistrel.core.images.safety import (
    SafetyContext,
    UploadSubject,
    build_upload_safety_context,
)
from epistrel.core.media.assets import ImageCheckRecord, MediaUpload, Staged
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Scope, Target, Violation
from epistrel.core.profiles.certification import image_check_certified
from epistrel.core.profiles.fingerprint import canonical_json
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.media import CheckStatus, MediaKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.images.jobs import (
    MINORS_POLICY_ID,
    ImageCheckUnavailable,
    OutputVerdict,
    PolicyVerdict,
)
from epistrel.orchestrator.images.registry import ImageCheckUnconfigured
from epistrel.orchestrator.models import reported_version
from epistrel.orchestrator.models.config import endpoint_of
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import (
    IMAGE_CHECK,
    IMAGE_DESCRIPTORS_MAX,
    ImageCheckInputs,
    ImageCheckPolicy,
    ImageCheckVerdict,
    ImageSubject,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.policies.age import policy_prompt

if TYPE_CHECKING:
    from epistrel.core.images.store import ImageJob
    from epistrel.orchestrator.images.jobs import ImageRuntime, PromptCheck, StagedOutput
    from epistrel.orchestrator.models.access import ModelAccessLayer

logger = logging.getLogger(__name__)

PURPOSE = IMAGE_CHECK.name
MAX_SIDE = 1024
"""The longest side of the image an ``image_check`` call is sent (px)."""
SENT_AS_IS = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
"""Types a vision endpoint takes as they are; anything else (or anything larger than
``MAX_SIDE``) is re-encoded as PNG."""
FINGERPRINT_CHARS = 12
CHECK_DEADLINE_S = 180.0
"""The output check's total cap: the purpose's per-call timeout (default 60 s) and #53's
retries fit under it."""
TEXT_MAX = 8_000
CAPTION_MAX = 2_000
DESCRIPTOR_CHARS = 500
MAX_SUBJECTS = 64
MAX_AGE = 150
CACHE_JOBS = 256
"""How many jobs' certification lookups ``OutputChecks`` keeps (per job, then dropped)."""
UPLOAD_ENTRY_POINT = "media_upload"

type PolicyResult = Literal["pass", "violation"]


class UploadCheckUnavailable(EpistrelError):
    """503 ``image_check_unavailable``: the image checker could not answer an upload's check
    after its retries (or ran out of time). Nothing was stored."""

    code = "image_check_unavailable"
    http_status = 503


# -- the checker's identity -------------------------------------------------------------------


def template_sha256() -> str:
    """The SHA-256 of ``image_check.j2`` as it ships."""
    source = (
        resources.files("epistrel.orchestrator.models")
        .joinpath("templates")
        .joinpath(IMAGE_CHECK.template)
        .read_bytes()
    )
    return hashlib.sha256(source).hexdigest()


TEMPLATE_SHA256 = template_sha256()


def checker_fingerprint(
    base_url_host: str | None,
    model: str,
    reported: str | None,
    template: str = TEMPLATE_SHA256,
) -> str:
    """The checker's fingerprint (module docstring): 12 hex of the SHA-256 of its canonical
    JSON."""
    document = {
        "base_url_host": base_url_host,
        "model": model,
        "reported_version": reported,
        "template_sha256": template,
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


@dataclass(frozen=True, slots=True)
class Checker:
    """The configured ``image_check`` model: its id, its endpoint's host (the adapter's name
    when no ``base_url`` is set) and its fingerprint."""

    model: str
    endpoint: str
    fingerprint: str


async def checker(models: ModelAccessLayer, db: AsyncEngine) -> Checker | None:
    """The configured checker, or ``None`` while ``image_check`` has no model."""
    config = await models.resolve(PURPOSE)
    if config.model is None:
        return None
    host = endpoint_of(config.base_url)
    async with db.connect() as conn:
        reported = (await reported_version.latest(conn)).get(config.model)
    return Checker(
        model=config.model,
        endpoint=host or models.adapter.name,
        fingerprint=checker_fingerprint(host, config.model, reported),
    )


# -- the payload ------------------------------------------------------------------------------


def data_url(image: bytes, media_type: str) -> str:
    """``image`` as a base64 ``data:`` URL no side of which exceeds ``MAX_SIDE`` px: as it is
    when it fits and is a type vision endpoints take, else downscaled (aspect kept) and
    re-encoded as PNG (an animation: its first frame)."""
    with Image.open(io.BytesIO(image)) as opened:
        fits = max(opened.size) <= MAX_SIDE and media_type in SENT_AS_IS
        if fits:
            return f"data:{media_type};base64,{base64.b64encode(image).decode('ascii')}"
        opened.seek(0)
        frame = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    frame.save(out, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode('ascii')}"


def payload_subjects(context: SafetyContext) -> list[ImageSubject]:
    """The safety context's subjects as ``image_check`` receives them (#257's labels)."""
    return [
        ImageSubject(
            label=s.label,
            true_age=None if s.true_age is None else min(max(s.true_age, 0), MAX_AGE),
            classification=s.classification,
            visual_descriptors=[
                d[:DESCRIPTOR_CHARS] for d in s.visual_descriptors[:IMAGE_DESCRIPTORS_MAX]
            ],
        )
        for s in context.subjects[:MAX_SUBJECTS]
    ]


def policy_text(policy: Policy, threshold: int) -> str:
    """A model-assisted policy's text as the checker reads it (the live threshold filled in)."""
    rule = policy.rule
    if not isinstance(rule, ModelAssistedRule):
        raise TypeError(f"policy {policy.id} is not model-assisted")
    return policy_prompt(rule.prompt, threshold)[:TEXT_MAX]


def check_inputs(
    image_url: str,
    prompt: str | None,
    caption: str | None,
    alt_text: str | None,
    context: SafetyContext,
    policies: Sequence[Policy],
) -> ImageCheckInputs:
    """One ``image_check`` request (FR-IMG-13): #257's payload plus the image."""
    return ImageCheckInputs(
        image=image_url,
        prompt=(prompt or "")[:TEXT_MAX],
        caption=(caption or "")[:CAPTION_MAX],
        alt_text=(alt_text or "")[:CAPTION_MAX],
        subjects=payload_subjects(context),
        policies=[
            ImageCheckPolicy(policy_id=p.id, text=policy_text(p, context.threshold))
            for p in policies
        ],
    )


# -- the check --------------------------------------------------------------------------------


def output_policies(policies: Sequence[Policy], context: SafetyContext) -> tuple[Policy, ...]:
    """The policies of ``policies`` that judge an image output: those for ``image_output`` (the
    seeded minors policy whatever its targets, while it exists), a character policy only for a
    resolved subject. An age-conditioned policy always applies: the checker judges what the
    image shows, whoever the prompt named."""
    subjects = set(context.character_ids)
    found: dict[str, Policy] = {}
    for p in policies:
        if Target.IMAGE_OUTPUT not in p.applies_to and p.id != MINORS_POLICY_ID:
            continue
        if p.scope is Scope.CHARACTER and p.scope_id not in subjects:
            continue
        found.setdefault(p.id, p)
    return tuple(found.values())


def _uncertain(policy: Policy) -> PolicyResult:
    """``on_uncertain`` as the output check reads it: ``violation`` → ``block``, ``pass`` →
    ``allow``."""
    rule = policy.rule
    allow = isinstance(rule, ModelAssistedRule) and rule.on_uncertain == "pass"
    return "pass" if allow else "violation"


async def check_output(
    models: ModelAccessLayer,
    db: AsyncEngine,
    image: bytes,
    media_type: str,
    *,
    prompt: str | None,
    caption: str | None,
    alt_text: str | None,
    safety_context: SafetyContext,
    policies: Sequence[Policy],
    found_checker: Checker | None = None,
    certified: frozenset[str] | None = None,
) -> OutputVerdict:
    """Judge ``image`` (module docstring). ``policies`` are already the applicable ones
    (``output_policies``); none → ``OutputVerdict()`` (``unchecked``) and no call. Raises
    ``ImageCheckUnconfigured`` while ``image_check`` has no model, and ``ModelError`` when it
    cannot answer. ``certified`` (the policy ids the checker is certified for) skips the lookup
    when a caller has it cached."""
    if not policies:
        return OutputVerdict()
    found = found_checker or await checker(models, db)
    if found is None:
        raise ImageCheckUnconfigured([p.id for p in policies])
    results: dict[str, tuple[PolicyResult, bool]] = {}
    deterministic = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    for value in (prompt, caption, alt_text):
        if value:
            for violation in evaluate(value, deterministic, Target.IMAGE_OUTPUT):
                results.setdefault(violation.policy_id, ("violation", False))
    for p in deterministic:
        results.setdefault(p.id, ("pass", False))
    assisted = [p for p in policies if isinstance(p.rule, ModelAssistedRule)]
    if assisted:
        inputs = check_inputs(
            data_url(image, media_type), prompt, caption, alt_text, safety_context, assisted
        )
        answer = await models.structured_call(
            PURPOSE,
            ImageCheckVerdict,
            inputs,
            deterministic_first=lambda: None,
            budget=TurnBudget(deadline_s=CHECK_DEADLINE_S),
        )
        verdicts = {} if isinstance(answer, Uncertain) else answer.verdicts
        for p in assisted:
            said = verdicts.get(p.id, "uncertain")
            if said == "uncertain":
                results[p.id] = (_uncertain(p), True)
            else:
                results[p.id] = (said, False)
    if certified is None:
        async with db.connect() as conn:
            certified = await image_check_certified(
                conn,
                found.fingerprint,
                [p.id for p in policies],
                {p.id: p.version for p in policies},
            )
    return OutputVerdict(
        per_policy=tuple(
            PolicyVerdict(
                policy_id=p.id,
                result=results[p.id][0],
                uncertain=results[p.id][1],
                certified=p.id in certified,
                policy_version=p.version,
                policy_name=p.name,
            )
            for p in policies
        ),
        model=found.model,
        endpoint=found.endpoint,
        checker_fingerprint=found.fingerprint,
    )


def record_of(verdict: OutputVerdict) -> ImageCheckRecord:
    """A passing verdict as ``media_assets`` stores it (``unchecked`` without a checker)."""
    if verdict.check_status == CheckStatus.UNCHECKED.value:
        return ImageCheckRecord()
    return ImageCheckRecord(
        status=CheckStatus.PASSED,
        model=verdict.model,
        endpoint=verdict.endpoint,
        policy_results=tuple(verdict.policy_results()),
    )


# -- jobs (#256's seam) -----------------------------------------------------------------------


class OutputChecks:
    """The pipeline's default checks: #257's prompt check, and this module's output check on a
    job's staged output with its stored safety context (module docstring)."""

    def __init__(self) -> None:
        self._certified: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()

    async def check_prompt(self, runtime: ImageRuntime, check: PromptCheck) -> SafetyContext:
        from epistrel.orchestrator.images.safety_checks import check_image_prompt

        return await check_image_prompt(runtime, check)

    async def _cached(
        self, db: AsyncEngine, job_id: str, found: Checker, policies: Sequence[Policy]
    ) -> frozenset[str]:
        key = (job_id, found.fingerprint)
        hit = self._certified.get(key)
        if hit is None:
            async with db.connect() as conn:
                hit = await image_check_certified(
                    conn,
                    found.fingerprint,
                    [p.id for p in policies],
                    {p.id: p.version for p in policies},
                )
            self._certified[key] = hit
            while len(self._certified) > CACHE_JOBS:
                self._certified.popitem(last=False)
        return hit

    async def check_output(
        self, runtime: ImageRuntime, job: ImageJob, output: StagedOutput
    ) -> OutputVerdict:
        from epistrel.orchestrator.images.safety_checks import target_policies

        context = SafetyContext.from_json(job.safety_context or {})
        try:
            found_policies = await target_policies(runtime, context, job.story_id)
        except StoryNotFound:
            # The story went while the job ran: promotion ends it ``target_gone``, and the
            # output is still judged by the policies no story can lift.
            found_policies = await target_policies(runtime, replace(context, kind="catalog"), None)
        policies = output_policies(found_policies, context)
        if not policies:
            return OutputVerdict()
        found = await checker(runtime.models, runtime.db)
        if found is None:
            raise ImageCheckUnconfigured([p.id for p in policies])
        certified = await self._cached(runtime.db, job.id, found, policies)
        image = await _read(output)
        try:
            return await check_output(
                runtime.models,
                runtime.db,
                image,
                output.media_type,
                prompt=job.prompt,
                caption=job.params.get("caption"),
                alt_text=job.params.get("alt_text"),
                safety_context=context,
                policies=policies,
                found_checker=found,
                certified=certified,
            )
        except ModelError as exc:
            raise ImageCheckUnavailable(exc.code) from exc


async def _read(output: StagedOutput) -> bytes:
    return await asyncio.to_thread(output.path.read_bytes)


# -- uploads ----------------------------------------------------------------------------------


async def upload_policies(
    db: AsyncEngine, rating_id: str, context: SafetyContext
) -> tuple[Policy, ...]:
    """An upload's applicable policies (planner): the global policies + its content rating's +
    the character policies of the declared subjects that are story characters."""
    from epistrel.orchestrator.policies.store import creation_context, list_policies

    found = list((await creation_context(db, rating_id, ())).policies)
    subjects = set(context.character_ids)
    if subjects:
        found.extend(
            p for p in await list_policies(db, scope=Scope.CHARACTER) if p.scope_id in subjects
        )
    return output_policies(found, context)


@dataclass(frozen=True, slots=True)
class UploadCheck:
    """The check an upload's staged image (or a video's poster) passes before the moderation
    hook (``core.media.assets.prepare``; module docstring)."""

    models: ModelAccessLayer
    db: AsyncEngine

    async def __call__(
        self, staged: Staged, request: MediaUpload, *, principal: str, operation_id: str | None
    ) -> ImageCheckRecord:
        """``passed`` with its per-policy results; ``unchecked`` with no checker or no
        applicable policy. Raises 422 ``guardrail_violation`` (an ``image_upload_rejected``
        audit row written) or 503 ``image_check_unavailable``."""
        if staged.kind is not MediaKind.IMAGE:
            return ImageCheckRecord()
        found = await checker(self.models, self.db)
        if found is None:
            return ImageCheckRecord()
        declared = [UploadSubject(s.kind.value, s.id) for s in request.depicted_subjects or []]
        async with self.db.connect() as conn, conn.begin():
            context = await build_upload_safety_context(
                conn, declared, caption=request.caption, alt_text=request.alt_text
            )
        policies = await upload_policies(self.db, request.content_rating_id, context)
        if not policies:
            return ImageCheckRecord()
        image = await asyncio.to_thread(staged.path.read_bytes)
        prompt = (
            request.generation_metadata.prompt if request.generation_metadata is not None else None
        )
        try:
            verdict = await check_output(
                self.models,
                self.db,
                image,
                staged.mime_type,
                prompt=prompt,
                caption=request.caption,
                alt_text=request.alt_text,
                safety_context=context,
                policies=policies,
                found_checker=found,
            )
        except ModelError as exc:
            logger.info("upload image check unavailable: %s", type(exc).__name__)
            raise UploadCheckUnavailable(
                "the image check could not reach its model", {"reason": exc.code}
            ) from exc
        if verdict.violations:
            await refuse(self.db, verdict, policies, principal=principal, operation_id=operation_id)
        return record_of(verdict)


async def refuse(
    db: AsyncEngine,
    verdict: OutputVerdict,
    policies: Sequence[Policy],
    *,
    principal: str,
    operation_id: str | None,
) -> None:
    """Refuse an upload that failed the check: the audit row (policy ids, principal, operation
    id; never the image or its hash), then FR-GUARD-10's 422 ``guardrail_violation`` listing
    every violated policy (an age-conditioned one says only that a minor is involved or
    present)."""
    from epistrel.orchestrator.policies.check import guardrail_violation

    ids = verdict.violations
    by_id = {p.id: p for p in policies}
    await audit.record(
        db,
        AuditKind.IMAGE_UPLOAD_REJECTED,
        principal=principal,
        policy_ids=ids,
        details={"entry_point": UPLOAD_ENTRY_POINT, "operation_id": operation_id},
    )
    logger.info("upload refused by the image check: violations=%d", len(ids))
    raise guardrail_violation(
        [
            Violation(
                policy_id=i,
                name=by_id[i].name,
                message=by_id[i].message,
                kind=by_id[i].kind,
            )
            for i in ids
        ],
        policies,
        what="Image",
    )


__all__ = [
    "CHECK_DEADLINE_S",
    "MAX_SIDE",
    "TEMPLATE_SHA256",
    "Checker",
    "OutputChecks",
    "UploadCheck",
    "UploadCheckUnavailable",
    "check_inputs",
    "check_output",
    "checker",
    "checker_fingerprint",
    "data_url",
    "output_policies",
    "payload_subjects",
    "record_of",
    "refuse",
    "template_sha256",
    "upload_policies",
]
