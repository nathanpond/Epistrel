"""A consumer purpose's call (FR-MODEL-15; #825): rendered, gated, called and answered.

``call_purpose`` answers ``POST /purposes/{name}/calls`` (``Engine.call_purpose``) for a call
with **no story**: the purpose's structured output, synchronously, through the same safety path
as everything else. Each step is a function of its own, so a story-bound call (#826, with its
viewpoint context) and a call that keeps its result as a memory (#827) reuse them:

1. ``check_inputs``: ``inputs`` is a JSON object of at most ``INPUTS_MAX_BYTES`` as canonical
   JSON (422 ``validation_error``, ``details.reason`` ``too_large``) and depth
   ``INPUTS_MAX_DEPTH`` (the root object is 1; ``too_deep``). Inputs are not checked against
   the template.
2. ``prepare``: the purpose at the pinned version, else its latest (an unknown name is 404, an
   unknown version 404 ``version_not_found``, an archived purpose 410 ``purpose_archived``
   whatever version is pinned); the call's rating (``content_rating``, else the deployment's
   default; an unknown or retired one is 422 ``validation_error`` at ``body.content_rating``)
   and its guard context: the live global policies and the rating closure's
   (``store.creation_context``), with each ``depicted`` person as a ``CastMember``
   (``depicted:<n>``, present, aged their true age now), so the minors policy (FR-GUARD-11)
   applies exactly when one is under the threshold. The deadline (``deadline_ms``) starts at
   the call's start: the input gate's time counts toward it.
3. ``render``: the system message is Epistrel's fixed preamble (the rating's prompt guidance,
   first, as FR-RATE-4 puts it, and the instruction to answer with only the JSON object), then
   the purpose's ``system_template``; the user message is its ``template``. Both render in the
   sandbox (``core.purposes.validation.environment``) with ``inputs``, ``viewpoint`` and
   ``story`` (``None`` with no story). A render failure is 422 ``template_render_error``, before
   any model call.
4. ``gate_inputs``: the input gate (#56) over every string in ``inputs`` at any depth, each its
   own text at ``inputs.<path>[i]``; a violation is 422 ``guardrail_violation`` (with its
   ``guardrail_input_rejected`` audit row, entry point ``purpose_call``) and no call to the
   purpose's model is made. Model-assisted checks run on a budget of their own (G calls).
5. ``produce``: a structured call (``structured.structured_messages``) whose response schema is
   the version's output model; consumer purposes have no deterministic step (FR-MODEL-5's
   residual is the whole call, as for ``generation``). An ``Uncertain`` result is 502
   ``model_output_invalid`` (``details.reason``); a ``ModelUnavailable`` is 503, the passed
   deadline included (``reason: deadline``). The output gate then checks the result's string
   leaves (``gate_output``): each deterministic output policy against each leaf, and each
   model-assisted one against the leaves joined into one text, each labelled with its JSON path
   (``output.<path>``); keys are never gated. A violation retries once: a fresh structured call
   at the same params, the model not told why. A second violation fails closed: 422
   ``output_policy_violation`` naming the policies, with no part of the output, and a
   ``guardrail_output_rejected`` audit row. The purpose's budget is two structured calls, each
   with its ``structured_retry_budget`` retries.
6. ``assemble``: ``{purpose, version, output, model: {endpoint, model, version}, usage: {calls,
   tokens_in, tokens_out, guardrail_calls}, config_revision}``: ``usage`` counts the purpose's
   own model calls and, apart, the guardrail checks (``guardrail_calls``).

Nothing here logs, counts or traces an input, a template or an output (invariant 3): the audit
rows hold policy ids and lengths, and the model metrics label the call ``purpose="consumer"``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final

from jinja2 import Template
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import audit
from epistrel.core.errors import (
    InvalidRequest,
    ModelOutputInvalid,
    OutputPolicyViolation,
    PurposeNotFound,
    TemplateRenderError,
)
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target, Violation
from epistrel.core.purposes.output import output_of
from epistrel.core.purposes.store import VERSION_MAX
from epistrel.core.purposes.validation import canonical_json
from epistrel.core.tables import content_rating_versions, content_ratings, deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import reported_version
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message, UnknownPurpose
from epistrel.orchestrator.models.consumer import ResolvedPurpose
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.models.structured import Uncertain, structured_messages
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.check import guardrail_violation, refuse_withheld
from epistrel.orchestrator.policies.input_gate import (
    EntryPoint,
    GateText,
    InputPass,
    calls_needed,
    enforce_input,
    input_policies,
)
from epistrel.orchestrator.policies.metrics import GUARDRAIL_VIOLATIONS
from epistrel.orchestrator.policies.output_gate import check_output, output_policies
from epistrel.orchestrator.policies.store import GuardContext, creation_context

INPUTS_MAX_BYTES: Final = 64 * 1024
"""``inputs`` as canonical JSON (sorted keys, no whitespace), in UTF-8 bytes."""
INPUTS_MAX_DEPTH: Final = 6
"""The root object is depth 1; each nested object or array adds 1."""
DEFAULT_DEADLINE_MS: Final = 60_000
MAX_DEADLINE_MS: Final = 300_000
DEPICTED_MAX: Final = 20
LABEL_MAX: Final = 120
TRUE_AGE_MAX: Final = 150
OUTPUT_ATTEMPTS: Final = 2
"""The first structured call and the one retry an output violation earns."""
GUARDRAIL_PURPOSE: Final = "guardrail_check"
OUTPUT_STAGE: Final = "output"
TOO_LARGE: Final = "too_large"
TOO_DEEP: Final = "too_deep"
INPUTS_ROOT: Final = "inputs"
OUTPUT_ROOT: Final = "output"
DEPICTED_PREFIX: Final = "depicted:"

GUIDANCE_HEADING: Final = "## Content rating guidance"
PREAMBLE: Final = (
    "## Task\n"
    "You are completing a task for an application that calls you through Epistrel. Follow the "
    "instructions below, keep to the content rating guidance, and answer with only the JSON "
    "object the response schema describes, and nothing else."
)


# -- the request and the result --------------------------------------------------------------


def _is_json(value: Any, depth: int = 0) -> bool:
    if value is None or isinstance(value, str | bool | int | float):
        return not isinstance(value, float) or value == value  # NaN is no JSON number
    if depth > INPUTS_MAX_DEPTH * 4:
        return False  # far past the depth limit ``check_inputs`` reports
    if isinstance(value, list):
        return all(_is_json(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v, depth + 1) for k, v in value.items())
    return False


def _json_only(value: dict[str, Any]) -> dict[str, Any]:
    """``inputs`` holds JSON values only (a library caller may pass any Python object)."""
    if not _is_json(value):
        raise ValueError("inputs must hold JSON values only")
    return value


class Depicted(BaseModel):
    """A person the inputs or the output depict, with their true age (FR-GUARD-11)."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, StringConstraints(min_length=1, max_length=LABEL_MAX)] = Field(
        description=f"Who this is, unique within the call (at most {LABEL_MAX} characters). "
        "Never sent to a model."
    )
    true_age: StrictInt = Field(
        ge=0,
        le=TRUE_AGE_MAX,
        description="Their true age now. Below the deployment's `minor_age_threshold`, the "
        "minors policy applies to the call's inputs and output.",
    )


class PurposeCall(BaseModel):
    """``POST /purposes/{name}/calls``: one call of a consumer purpose with no story."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "inputs": {"voter": "Ash", "target": "Briar", "votes": [3, 1]},
                    "content_rating": "00000000000000000000RAT002",
                    "depicted": [{"label": "Ash", "true_age": 34}],
                    "deadline_ms": 30000,
                }
            ]
        },
    )

    inputs: Annotated[dict[str, Any], AfterValidator(_json_only)] = Field(
        description="The template's `inputs`: a JSON object of strings, numbers, booleans, "
        "nulls, and arrays and objects of them (depth at most 6, at most 64 KB as canonical "
        "JSON). Every string in it passes the input gate."
    )
    version: StrictInt | None = Field(
        default=None,
        ge=1,
        le=VERSION_MAX,
        description="The purpose version to call (default: the latest).",
    )
    content_rating: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = Field(
        default=None,
        description="The id of the deployment content rating the call runs under (default: "
        "the deployment's default rating). Its policies gate the inputs and the output.",
    )
    depicted: list[Depicted] = Field(
        default_factory=list,
        max_length=DEPICTED_MAX,
        description=f"The people the inputs or the output depict (at most {DEPICTED_MAX}), "
        "each with a unique label and a true age.",
    )
    deadline_ms: StrictInt = Field(
        default=DEFAULT_DEADLINE_MS,
        ge=1,
        le=MAX_DEADLINE_MS,
        description="The call's deadline, from its start, the input gate included (default "
        "60 000, at most 300 000). Once it passes, the call is 503 `model_unavailable` with "
        "`details.reason: deadline`.",
    )

    @field_validator("depicted")
    @classmethod
    def _unique_labels(cls, value: list[Depicted]) -> list[Depicted]:
        labels = [d.label for d in value]
        if len(set(labels)) != len(labels):
            raise ValueError("each depicted label must be unique")
        return value


class CallModel(BaseModel):
    """The model the call went to: its endpoint, its model name, and the version the model
    reported (FR-PROF-16; ``null`` when none was reported)."""

    endpoint: str
    model: str
    version: str | None


class CallUsage(BaseModel):
    """The purpose's own model calls and their tokens, and, apart, the guardrail checks the
    call's gates made (model-assisted policies; FR-GUARD-9)."""

    calls: int
    tokens_in: int
    tokens_out: int
    guardrail_calls: int


@dataclass(frozen=True)
class PurposeResult:
    """A consumer purpose call's answer (``Engine.call_purpose``)."""

    purpose: str
    version: int
    output: dict[str, Any]
    model: CallModel
    usage: CallUsage
    config_revision: int
    """The deployment ``config_revision`` the call's configuration was read at."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (FR-RESP-6)."""


# -- 1. the inputs ---------------------------------------------------------------------------


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def check_inputs(inputs: Mapping[str, Any]) -> None:
    """422 ``validation_error`` at ``body.inputs`` when ``inputs`` is too large or too deep."""
    size = len(canonical_json(inputs).encode())
    if size > INPUTS_MAX_BYTES:
        raise InvalidRequest(
            f"inputs are {size} bytes as canonical JSON; at most {INPUTS_MAX_BYTES}",
            {"field": "body.inputs", "reason": TOO_LARGE, "limit": INPUTS_MAX_BYTES},
        )
    depth = _depth(dict(inputs))
    if depth > INPUTS_MAX_DEPTH:
        raise InvalidRequest(
            f"inputs nest {depth} deep; at most {INPUTS_MAX_DEPTH}",
            {"field": "body.inputs", "reason": TOO_DEEP, "limit": INPUTS_MAX_DEPTH},
        )


def _leaves(value: Any, path: str) -> list[GateText]:
    """Every string in ``value`` with its path (``root.key[0].key``)."""
    if isinstance(value, str):
        return [GateText(locator=path, text=value)]
    found: list[GateText] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_leaves(item, f"{path}.{key}"))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(_leaves(item, f"{path}[{index}]"))
    return found


def input_texts(inputs: Mapping[str, Any]) -> list[GateText]:
    """Every string input at any depth, located at ``inputs.<path>[i]``."""
    return _leaves(inputs, INPUTS_ROOT)


def output_texts(output: Mapping[str, Any]) -> list[GateText]:
    """Every string leaf of an output, located at ``output.<path>[i]`` (keys are not text)."""
    return _leaves(output, OUTPUT_ROOT)


# -- 2. prepare ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallRating:
    """The rating a call runs under and its prompt guidance at the current version."""

    rating_id: str
    guidance: str


async def call_rating(db: AsyncEngine, rating_id: str | None) -> CallRating:
    """The named rating, else the deployment default; 422 at ``body.content_rating`` when it
    is unknown or retired."""
    async with db.connect() as conn:
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
                select(content_ratings.c.retired_at, content_rating_versions.c.prompt_guidance)
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
    return CallRating(rating_id=rating_id, guidance=str(row.prompt_guidance or ""))


def depicted_cast(depicted: Sequence[Depicted]) -> tuple[CastMember, ...]:
    """Each depicted person as a cast member the age condition reads: ``depicted:<n>``,
    present, aged their true age now (no world clock runs outside a story)."""
    return tuple(
        CastMember(
            character_id=f"{DEPICTED_PREFIX}{index}",
            ordinal=index,
            true_age=person.true_age,
            present=True,
        )
        for index, person in enumerate(depicted, start=1)
    )


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """What a call runs with: the resolved purpose version, its rating and guidance, the guard
    context, the endpoint its configuration names, and its deadline on the monotonic clock."""

    purpose: ResolvedPurpose
    rating: CallRating
    context: GuardContext
    endpoint: str
    model: str
    config_revision: int
    started: float
    deadline_s: float

    def budget(self, max_calls: int) -> TurnBudget:
        """A budget of ``max_calls`` under the call's deadline (every budget of the call shares
        its start)."""
        return TurnBudget(max_calls=max_calls, deadline_s=self.deadline_s, started=self.started)

    def expired(self) -> bool:
        return time.monotonic() - self.started >= self.deadline_s


async def resolve(models: ModelAccessLayer, name: str, version: int | None) -> ResolvedPurpose:
    """The purpose version a call names: 404 for an unknown name (a built-in purpose's
    included), 404 ``version_not_found``, 410 ``purpose_archived``."""
    try:
        return await models.consumer_purpose(name, version)
    except UnknownPurpose:
        raise PurposeNotFound(
            f"no consumer purpose is registered as {name!r}", {"purpose": name}
        ) from None


async def prepare(
    db: AsyncEngine,
    models: ModelAccessLayer,
    name: str,
    request: PurposeCall,
    *,
    started: float,
    cast: Sequence[CastMember] = (),
) -> PreparedCall:
    """Step 2 (module docs). ``cast`` adds members beside the depicted ones (#826: a story's)."""
    purpose = await resolve(models, name, request.version)
    rating = await call_rating(db, request.content_rating)
    context = await creation_context(
        db, rating.rating_id, (*cast, *depicted_cast(request.depicted))
    )
    endpoint, model, revision = await models.endpoint(name)
    return PreparedCall(
        purpose=purpose,
        rating=rating,
        context=context,
        endpoint=endpoint,
        model=model,
        config_revision=revision,
        started=started,
        deadline_s=request.deadline_ms / 1000,
    )


# -- 3. render -------------------------------------------------------------------------------


def preamble(guidance: str) -> str:
    """Epistrel's fixed system preamble: the rating's guidance (when it has any), then the
    task instruction."""
    if guidance.strip():
        return f"{GUIDANCE_HEADING}\n{guidance.strip()}\n\n{PREAMBLE}"
    return PREAMBLE


def _render(template: Template, field: str, purpose: ResolvedPurpose, **variables: Any) -> str:
    try:
        return template.render(**variables)
    except Exception as exc:  # any failure inside the sandbox is the template's, not ours
        raise TemplateRenderError(
            f"the {field} of consumer purpose {purpose.name!r} version {purpose.version} did "
            f"not render for these inputs ({type(exc).__name__})",
            {
                "purpose": purpose.name,
                "version": purpose.version,
                "field": field,
                "error": type(exc).__name__,
            },
        ) from None


def render(
    prepared: PreparedCall,
    inputs: Mapping[str, Any],
    *,
    viewpoint: Any = None,
    story: Any = None,
) -> list[Message]:
    """Step 3 (module docs): the system and user messages. ``viewpoint`` and ``story`` are
    ``None`` with no story (#826 passes a story-bound call's)."""
    purpose = prepared.purpose
    variables = {"inputs": dict(inputs), "viewpoint": viewpoint, "story": story}
    system = preamble(prepared.rating.guidance)
    if purpose.system_template is not None:
        extra = _render(purpose.system_template, "system_template", purpose, **variables)
        if extra.strip():
            system = f"{system}\n\n{extra.strip()}"
    user = _render(purpose.template, "template", purpose, **variables)
    return [Message(role="system", content=system), Message(role="user", content=user)]


# -- 4. the input gate -----------------------------------------------------------------------


async def gate_inputs(
    db: AsyncEngine,
    models: ModelAccessLayer,
    prepared: PreparedCall,
    inputs: Mapping[str, Any],
    *,
    principal: str,
    story_id: str | None = None,
) -> InputPass:
    """Step 4 (module docs): 422 ``guardrail_violation`` before any call to the purpose's
    model."""
    texts = input_texts(inputs)
    budget = prepared.budget(calls_needed(input_policies(prepared.context), texts))
    return await enforce_input(
        db,
        models,
        prepared.context,
        texts,
        entry_point=EntryPoint.PURPOSE_CALL,
        principal=principal,
        story_id=story_id,
        budget=budget,
    )


# -- 5. the model call and the output gate ---------------------------------------------------


async def generate(
    models: ModelAccessLayer,
    prepared: PreparedCall,
    messages: Sequence[Message],
    *,
    budget: TurnBudget,
) -> dict[str, Any]:
    """One structured call (and its parse-validate retries): the output, by the schema's
    property names. 502 ``model_output_invalid`` when no answer validates."""
    purpose = prepared.purpose
    result = await structured_messages(
        models, purpose.name, purpose.output, messages, budget=budget
    )
    if isinstance(result, Uncertain):
        raise ModelOutputInvalid(
            f"the model gave consumer purpose {purpose.name!r} no answer matching its output "
            f"schema ({result.reason})",
            {"purpose": purpose.name, "reason": result.reason, "attempts": result.attempts},
        )
    return output_of(result)


def _assisted(policies: Sequence[Policy]) -> tuple[Policy, ...]:
    return tuple(p for p in policies if isinstance(p.rule, ModelAssistedRule))


async def gate_output(
    models: ModelAccessLayer, prepared: PreparedCall, output: Mapping[str, Any]
) -> tuple[Violation, ...]:
    """The output's violations (none: it passes). Each deterministic output policy against each
    string leaf; each model-assisted one, on a budget of its own, against the leaves joined and
    labelled with their paths."""
    leaves = output_texts(output)
    if not leaves:
        return ()
    policies = output_policies(prepared.context)
    deterministic = [p for p in policies if not isinstance(p.rule, ModelAssistedRule)]
    found: dict[str, Violation] = {}
    for leaf in leaves:
        for violation in evaluate(leaf.text, deterministic, Target.TEXT_OUTPUT):
            found.setdefault(violation.policy_id, violation)
    assisted = _assisted(policies)
    if assisted:
        labelled = "\n".join(f"{leaf.locator}: {leaf.text}" for leaf in leaves)
        checked = await check_output(
            models, labelled, assisted, prepared.context, budget=prepared.budget(len(assisted))
        )
        for violation in checked.violations:
            found.setdefault(violation.policy_id, violation)
    return tuple(found.values())


@dataclass(frozen=True, slots=True)
class Produced:
    """The output the gate released and how many structured calls it took (1 or 2)."""

    output: dict[str, Any]
    attempts: int


async def produce(
    db: AsyncEngine,
    models: ModelAccessLayer,
    prepared: PreparedCall,
    messages: Sequence[Message],
    *,
    principal: str,
    story_id: str | None = None,
) -> Produced:
    """Step 5 (module docs): generate, gate, retry once, then fail closed."""
    name = prepared.purpose.name
    retries = (await models.structured_settings()).structured_retry_budget
    budget = prepared.budget(OUTPUT_ATTEMPTS * (1 + retries))
    violations: tuple[Violation, ...] = ()
    for attempt in range(1, OUTPUT_ATTEMPTS + 1):
        output = await generate(models, prepared, messages, budget=budget)
        if prepared.expired():
            # An unchecked output is never released: the deadline ends the call (503).
            raise ModelUnavailable(name, "deadline", attempts=budget.used)
        violations = await gate_output(models, prepared, output)
        if not violations:
            return Produced(output=output, attempts=attempt)
        # A violated policy a pending catalog-scope erasure reaches is not named (#447).
        await refuse_withheld(db, [v.policy_id for v in violations])
        for violation in violations:
            GUARDRAIL_VIOLATIONS.labels(kind=violation.kind.value, stage=OUTPUT_STAGE).inc()
    policy_ids = tuple(v.policy_id for v in violations)
    await audit.record(
        db,
        audit.AuditKind.GUARDRAIL_OUTPUT_REJECTED,
        principal=principal,
        story_id=story_id,
        policy_ids=policy_ids,
        details={
            "entry_point": EntryPoint.PURPOSE_CALL.value,
            "purpose": name,
            "version": prepared.purpose.version,
            "attempts": OUTPUT_ATTEMPTS,
        },
    )
    refused = guardrail_violation(violations, prepared.context.policies, what="Output")
    raise OutputPolicyViolation(
        refused.message, {**refused.details, "purpose": name, "attempts": OUTPUT_ATTEMPTS}
    )


# -- 6. the answer ---------------------------------------------------------------------------


async def assemble(
    db: AsyncEngine, prepared: PreparedCall, produced: Produced, log: CallLog
) -> PurposeResult:
    """Step 6 (module docs): the result and its usage from the call's log."""
    name = prepared.purpose.name
    own = [c for c in log.calls if c.purpose == name and not c.probe]
    checks = [c for c in log.calls if c.purpose == GUARDRAIL_PURPOSE and not c.probe]
    model = own[-1].model if own else prepared.model
    async with db.connect() as conn:
        reported = (await reported_version.latest(conn)).get(model)
    return PurposeResult(
        purpose=name,
        version=prepared.purpose.version,
        output=produced.output,
        model=CallModel(endpoint=prepared.endpoint, model=model, version=reported),
        usage=CallUsage(
            calls=len(own),
            tokens_in=sum(c.tokens_in for c in own),
            tokens_out=sum(c.tokens_out for c in own),
            guardrail_calls=len(checks),
        ),
        config_revision=prepared.config_revision,
    )


async def call_purpose(
    db: AsyncEngine,
    models: ModelAccessLayer,
    name: str,
    request: PurposeCall,
    *,
    principal: str,
) -> PurposeResult:
    """A story-less call of consumer purpose ``name`` (module docs)."""
    started = time.monotonic()
    check_inputs(request.inputs)
    with recording() as log:
        prepared = await prepare(db, models, name, request, started=started)
        messages = render(prepared, request.inputs)
        await gate_inputs(db, models, prepared, request.inputs, principal=principal)
        if prepared.expired():
            raise ModelUnavailable(name, "deadline", attempts=0)
        produced = await produce(db, models, prepared, messages, principal=principal)
        return await assemble(db, prepared, produced, log)


__all__ = [
    "DEFAULT_DEADLINE_MS",
    "DEPICTED_MAX",
    "GUIDANCE_HEADING",
    "INPUTS_MAX_BYTES",
    "INPUTS_MAX_DEPTH",
    "MAX_DEADLINE_MS",
    "OUTPUT_ATTEMPTS",
    "PREAMBLE",
    "CallModel",
    "CallRating",
    "CallUsage",
    "Depicted",
    "PreparedCall",
    "Produced",
    "PurposeCall",
    "PurposeResult",
    "assemble",
    "call_purpose",
    "call_rating",
    "check_inputs",
    "depicted_cast",
    "gate_inputs",
    "gate_output",
    "generate",
    "input_texts",
    "output_texts",
    "preamble",
    "prepare",
    "produce",
    "render",
    "resolve",
]
