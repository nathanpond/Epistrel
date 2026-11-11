"""The image prompt check: every image prompt against the policies for image prompts, judged
by true ages, before any generation (FR-IMG-3,12,13; FR-GUARD-8,10,11; #257).

``check_image_prompt(runtime, check)`` is what ``prepare_image_job`` runs (through #256's
``ImageChecks`` seam), in the request, outside any transaction, under a total cap of
``PROMPT_CHECK_DEADLINE_S`` (30 s, then 503 ``model_unavailable``). Consumer prompts take exactly
the same path as builder prompts.

1. **The safety context** (``core.images.safety.build_safety_context``): declared or depicted
   subjects, characters named in the prompt, negative prompt, caption or alt text, and, for a
   story image, the characters authoritatively present at its position, each with its current
   true age. Read in one snapshot, closed before any model call.
2. **The policies**: the target's effective set for ``image_prompt`` — a story image's
   ``effective_policies`` (global, its rating's, the story's, and the character policies of
   the resolved subjects only, as #118's evaluator applies them), a catalog image's global
   policies and its entry's rating's (``creation_context``). An age-conditioned policy applies
   only when a subject is a minor (``SafetyContext.has_minor``).
3. **Figure classification** (``classify_figures``), only when an age-conditioned policy
   targets image prompts: one ``guardrail_check`` call per prompt (the ``figures`` variant,
   ``FigureClassification``) finds every depicted person who resolves to no character, with
   the prompt's own words for them, and classifies each adult, minor or indeterminate; it also
   returns the descriptors the prompt uses for the resolved subjects (a consumer prompt has no
   builder list). The resolved subjects are sent as labels with the prompt's own words for
   them, so the model leaves them out. A classification that fails after #53's retries (no
   answer, or none that validates) counts as one indeterminate figure: fail closed.
4. **The check** (``check_prompt``): every applicable policy, deterministic first (each text
   on its own), then one ``guardrail_check`` call per model-assisted policy. An ``Uncertain``
   result takes the policy's ``on_uncertain`` (the seeded minors policy: violation); a model
   that cannot answer after #53's retries is 503 ``model_unavailable`` and no job.
5. **A violation** raises FR-GUARD-10's 422 ``guardrail_violation`` listing every violated
   policy (``check.guardrail_violation``, the text input's envelope); an age-conditioned one
   says only that a minor is involved or present. Before it, a pending catalog-scope erasure
   that reaches a violated policy answers 503 ``erasure_pending`` (#447), and the refusal
   writes an ``image_prompt_rejected`` audit row (policy ids, principal, target, story).

**Payloads** (FR-IMG-13) are the registry's input schemas (``extra="forbid"``): the image
prompt, caption and alt text (and negative prompt; a subject image's consumer description and
style too, #830: they are the consumer's prompt), the policy's text, and each subject as an
opaque label (``subject_1`` …, ``figure_1`` …) with its true age or a figure's class and the
prompt's own visual descriptors. No story text, beliefs or other canon, and no character name
the texts do not contain. Logs carry counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from epistrel.core import audit
from epistrel.core.images.safety import (
    DeclaredSubject,
    Figure,
    InlineSubject,
    SafetyContext,
    SafetyRequest,
    build_safety_context,
    image_position,
)
from epistrel.core.membership import Position
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Scope, Target, Violation
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.images import ImageJobKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.errors import ModelError, ModelUnavailable
from epistrel.orchestrator.models.registry import (
    FIGURES_VARIANT,
    IMAGE_DESCRIPTORS_MAX,
    CheckedImageSubject,
    FigureClassification,
    GuardrailCheckInputs,
    GuardrailVerdict,
)
from epistrel.orchestrator.models.structured import (
    Uncertain,
    guardrail_deterministic_first,
    uncertain_verdict,
)
from epistrel.orchestrator.policies.age import policy_prompt
from epistrel.orchestrator.policies.check import guardrail_violation, refuse_withheld
from epistrel.orchestrator.policies.seeds.policies import THRESHOLD_PLACEHOLDER

if TYPE_CHECKING:
    from epistrel.core.images.store import ImageJob
    from epistrel.orchestrator.images.jobs import (
        ImageChecks,
        ImageRuntime,
        OutputVerdict,
        PromptCheck,
        StagedOutput,
    )
    from epistrel.orchestrator.models.access import ModelAccessLayer

logger = logging.getLogger(__name__)

PURPOSE = "guardrail_check"
PROMPT_CHECK_DEADLINE_S = 30.0
"""The prompt check's total cap (figure classification and every policy call)."""
FIGURES_INSTRUCTION = f"A minor is anyone under {THRESHOLD_PLACEHOLDER}."
"""The figure classification's age line, with the live threshold filled in."""
UNRESOLVED_FIGURE = Figure("a depicted person the check could not classify", "indeterminate")
"""What a failed figure classification counts as: one indeterminate figure (fail closed)."""
ENTRY_POINT = "image_prompt"
DESCRIPTOR_CHARS = 500
MENTION_CHARS = 200
MAX_SUBJECTS = 64
MAX_AGE = 150
SUBJECT_TEXTS = (("Subject description", "description"), ("Style", "style"))
"""A subject image's consumer texts (#830), each checked under its heading."""


# -- payloads -------------------------------------------------------------------------------


def checked_text(request: SafetyRequest) -> str:
    """The text a model-assisted check judges: the prompt, then each other text the request
    has, each on its own labelled line."""
    lines = [f"Prompt: {request.prompt}"]
    for label, value in (
        ("Negative prompt", request.negative_prompt),
        ("Caption", request.caption),
        ("Alt text", request.alt_text),
    ):
        if value:
            lines.append(f"{label}: {value}")
    for label, value in request.consumer_texts:
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def payload_subjects(
    context: SafetyContext, *, mentions: bool = False
) -> list[CheckedImageSubject]:
    """The context's subjects as a safety call receives them: opaque labels, true ages (or a
    figure's class) and the prompt's own descriptors; with ``mentions``, the texts' own words
    for each (the figure classification only)."""
    return [
        CheckedImageSubject(
            label=s.label,
            true_age=None if s.true_age is None else min(max(s.true_age, 0), MAX_AGE),
            classification=s.classification,
            visual_descriptors=[
                d[:DESCRIPTOR_CHARS] for d in s.visual_descriptors[:IMAGE_DESCRIPTORS_MAX]
            ],
            mentioned_as=(
                [m[:MENTION_CHARS] for m in s.mentions[:IMAGE_DESCRIPTORS_MAX]] if mentions else []
            ),
        )
        for s in context.subjects[:MAX_SUBJECTS]
    ]


def policy_inputs(text: str, policy: Policy, context: SafetyContext) -> GuardrailCheckInputs:
    """One model-assisted policy's request: the subjects only for an age-conditioned policy
    (as a text check's, #56)."""
    rule = policy.rule
    if not isinstance(rule, ModelAssistedRule):
        raise TypeError(f"policy {policy.id} is not model-assisted")
    if policy.age_condition is None:
        return GuardrailCheckInputs(text=text, policy_prompt=rule.prompt)
    return GuardrailCheckInputs(
        text=text,
        policy_prompt=policy_prompt(rule.prompt, context.threshold),
        image_subjects=payload_subjects(context),
    )


def figure_inputs(text: str, context: SafetyContext) -> GuardrailCheckInputs:
    """The figure classification's request: the texts, the age line and the resolved subjects
    with the texts' own words for them."""
    return GuardrailCheckInputs(
        text=text,
        policy_prompt=policy_prompt(FIGURES_INSTRUCTION, context.threshold),
        image_subjects=payload_subjects(context, mentions=True),
    )


# -- the steps ------------------------------------------------------------------------------


def safety_request(check: PromptCheck) -> tuple[SafetyRequest, Position | None]:
    """The safety path's view of a request, and a story image's position."""
    request = check.request
    base = SafetyRequest(
        kind="story",
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        caption=request.caption,
        alt_text=request.alt_text,
        subjects=tuple(
            DeclaredSubject(s.character_id, tuple(s.visual_descriptors), s.depicted)
            for s in request.depicted_subjects
        ),
    )
    if check.kind is ImageJobKind.SUBJECT:
        # #830: the declared subject inline, and the consumer's description and style checked
        # with the built prompt (a prompt writer may drop what the consumer asked for).
        origin = check.origin
        return (
            replace(
                base,
                kind="subject",
                entry_kind="subject",
                entry_id=str(origin["subject_id"]),
                inline=InlineSubject(
                    str(origin["subject_id"]), str(origin["label"]), int(origin["true_age"])
                ),
                rating_id=_str(check.target.get("content_rating_id")),
                consumer_texts=tuple(
                    (heading, str(origin[key]))
                    for heading, key in SUBJECT_TEXTS
                    if isinstance(origin.get(key), str) and origin[key]
                ),
            ),
            None,
        )
    if check.kind is ImageJobKind.CATALOG:
        entry_kind, entry_id = check.origin.get("entry_kind"), check.origin.get("entry_id")
        version = check.origin.get("version")
        return (
            replace(
                base,
                kind="catalog",
                entry_kind=None if entry_kind is None else str(entry_kind),
                entry_id=None if entry_id is None else str(entry_id),
                entry_version=version if isinstance(version, int) else None,
            ),
            None,
        )
    viewpoint = check.viewpoint or check.origin.get("viewpoint")
    # The safety path's viewpoint is the audience's character (an actor of #541's presence
    # rule): ``character:<id>`` names it, ``omniscient`` none (#263).
    found = None if viewpoint is None else str(viewpoint).removeprefix("character:")
    return (
        replace(
            base,
            story_id=str(check.origin["story_id"]),
            viewpoint=None if found == "omniscient" else found,
        ),
        image_position(check.origin),
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def applicable(policies: Sequence[Policy], context: SafetyContext) -> tuple[Policy, ...]:
    """The policies of ``policies`` that judge this prompt: those for ``image_prompt``, a
    character policy only for a resolved subject, and an age-conditioned one only when a
    subject is a minor (indeterminate included)."""
    subjects = set(context.character_ids)
    return tuple(
        p
        for p in policies
        if Target.IMAGE_PROMPT in p.applies_to
        and (p.scope is not Scope.CHARACTER or p.scope_id in subjects)
        and (p.age_condition is None or context.has_minor)
    )


async def target_policies(
    runtime: ImageRuntime, context: SafetyContext, story_id: str | None
) -> tuple[Policy, ...]:
    """The target's effective policies (see the module docstring)."""
    from epistrel.orchestrator.policies.store import (  # here: the store imports models
        creation_context,
        effective_policies,
    )

    if context.kind == "story" and story_id is not None:
        return await effective_policies(runtime.db, story_id)
    return (await creation_context(runtime.db, context.rating_id, ())).policies


async def classify_figures(
    models: ModelAccessLayer, request: SafetyRequest, context: SafetyContext, budget: TurnBudget
) -> SafetyContext:
    """``context`` with the prompt's figures classified, and the descriptors the prompt uses for
    its resolved subjects (see the module docstring)."""
    try:
        found = await models.structured_call(
            PURPOSE,
            FigureClassification,
            figure_inputs(checked_text(request), context),
            deterministic_first=lambda: None,
            budget=budget,
            variant=FIGURES_VARIANT,
        )
    except ModelError:
        found = None
    if found is None or isinstance(found, Uncertain):
        return context.with_figures([UNRESOLVED_FIGURE])
    labels = {s.label for s in context.characters}
    descriptors = {
        s.label: [d.strip() for d in s.visual_descriptors if d.strip()]
        for s in found.subjects
        if s.label in labels
    }
    figures = [Figure(f.descriptor.strip(), f.classification) for f in found.figures]
    return context.with_descriptors(descriptors).with_figures(figures)


async def check_prompt(
    models: ModelAccessLayer,
    request: SafetyRequest,
    context: SafetyContext,
    policies: Sequence[Policy],
    *,
    budget: TurnBudget,
) -> list[Violation]:
    """Every violation of ``policies`` (the applicable ones, ``applicable``) by the request's
    texts: deterministic first, then one model call per model-assisted policy. ``ModelError``
    propagates (the caller answers 503)."""
    found: dict[str, Violation] = {}
    deterministic = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    for value in request.texts():
        if value:
            for violation in evaluate(value, deterministic, Target.IMAGE_PROMPT):
                found.setdefault(violation.policy_id, violation)
    text = checked_text(request)
    for policy in policies:
        rule = policy.rule
        if not isinstance(rule, ModelAssistedRule):
            continue
        result = await models.structured_call(
            PURPOSE,
            GuardrailVerdict,
            policy_inputs(text, policy, context),
            deterministic_first=guardrail_deterministic_first(text, (), Target.IMAGE_PROMPT),
            budget=budget,
        )
        verdict = uncertain_verdict(rule.on_uncertain) if isinstance(result, Uncertain) else result
        if verdict.violation:
            found.setdefault(
                policy.id,
                Violation(
                    policy_id=policy.id, name=policy.name, message=policy.message, kind=policy.kind
                ),
            )
    order = {p.id: n for n, p in enumerate(policies)}
    return sorted(found.values(), key=lambda v: order.get(v.policy_id, len(order)))


@dataclass(frozen=True, slots=True)
class PromptOutcome:
    """A checked prompt: its safety context and what judged it (counts, for the log)."""

    context: SafetyContext
    policies: int
    model_calls: int


async def _run(runtime: ImageRuntime, check: PromptCheck) -> PromptOutcome:
    request, position = safety_request(check)
    async with runtime.db.connect() as conn, conn.begin():
        context = await build_safety_context(conn, request, position)
    found = await target_policies(runtime, context, request.story_id)
    budget = TurnBudget(deadline_s=PROMPT_CHECK_DEADLINE_S)
    if any(Target.IMAGE_PROMPT in p.applies_to and p.age_condition is not None for p in found):
        context = await classify_figures(runtime.models, request, context, budget)
    judged = applicable(found, context)
    try:
        violations = await check_prompt(runtime.models, request, context, judged, budget=budget)
    except ModelError as exc:
        reason = exc.reason if isinstance(exc, ModelUnavailable) else exc.code
        raise ModelUnavailable(
            PURPOSE, reason, "the image prompt check could not reach its model"
        ) from exc
    if violations:
        ids = [v.policy_id for v in violations]
        await refuse_withheld(runtime.db, ids)
        await audit.record(
            runtime.db,
            AuditKind.IMAGE_PROMPT_REJECTED,
            principal=check.principal,
            story_id=request.story_id,
            policy_ids=ids,
            details={
                "entry_point": ENTRY_POINT,
                "target_kind": check.kind.value,
                "target_id": request.story_id if request.story_id else request.entry_id,
                "subjects": len(context.characters),
                "figures": len(context.figures),
            },
        )
        logger.info(
            "image prompt refused: subjects=%d figures=%d violations=%d",
            len(context.characters),
            len(context.figures),
            len(violations),
        )
        raise guardrail_violation(violations, judged, what="Image prompt")
    logger.debug(
        "image prompt passed: subjects=%d figures=%d policies=%d",
        len(context.characters),
        len(context.figures),
        len(judged),
    )
    return PromptOutcome(context, len(judged), budget.used)


async def check_image_prompt(runtime: ImageRuntime, check: PromptCheck) -> SafetyContext:
    """Check one image request's prompt (see the module docstring); its safety context."""
    try:
        async with asyncio.timeout(PROMPT_CHECK_DEADLINE_S):
            outcome = await _run(runtime, check)
    except TimeoutError:
        raise ModelUnavailable(
            PURPOSE, "deadline", "the image prompt check ran out of time"
        ) from None
    return outcome.context


class PromptChecks:
    """#257's prompt check with another ``ImageChecks``' output check (the fail-closed default
    until #258, or a test's scripted one)."""

    def __init__(self, output: ImageChecks) -> None:
        self.output = output

    async def check_prompt(self, runtime: ImageRuntime, check: PromptCheck) -> SafetyContext:
        return await check_image_prompt(runtime, check)

    async def check_output(
        self, runtime: ImageRuntime, job: ImageJob, output: StagedOutput
    ) -> OutputVerdict:
        return await self.output.check_output(runtime, job, output)


__all__ = [
    "FIGURES_INSTRUCTION",
    "PROMPT_CHECK_DEADLINE_S",
    "UNRESOLVED_FIGURE",
    "PromptChecks",
    "PromptOutcome",
    "applicable",
    "check_image_prompt",
    "check_prompt",
    "checked_text",
    "classify_figures",
    "figure_inputs",
    "payload_subjects",
    "policy_inputs",
    "safety_request",
    "target_policies",
]
