"""``POST /stories/{id}/respond``: play one Lite turn (FR-RESP-1; #57). A thin handler over
``Engine.respond``: the actor's input in, one finished reply out."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, RespondAsyncDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.history import ItemModel, item_model
from epistrel.core.profiles.effective import ProfileVersionRef
from epistrel.core.profiles.fingerprint import QualityStatus
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models.metrics import ModelCall

router = APIRouter(tags=["stories"])

RESPOND_DESCRIPTION = (
    "Play one turn of a story. The request names the acting character (any character of the "
    "story; who acts as whom is your decision), what they say and/or do (`player_input`, the "
    "turn's first beat and only ever the actor's own words), who answers "
    "(`render_character_ids`: default every other character, voiced in the order given and "
    "composed into one message), the output "
    "viewpoint (`character:<id>` or `omniscient`; default the actor, then the story's default; "
    "echoed as `output_viewpoint`, and `beats` are produced for it), prompt parameters "
    "(`length`, `temperature`, `top_p`) and optional scene context."
    "\n\nThe consumer does no retrieval or memory work: the prompt is assembled from the story's "
    "own records. Scene context is checked against the cast and then ignored: it is never "
    "stored and never placed in the model's context.\n\nThe response is the **finished** reply: "
    "under the `lite` profile it is generated in one model call (plus one `guardrail_check` "
    "per model-assisted output policy) and none of its claims is checked against the story's "
    "facts. Before it is released the reply is checked against the "
    "story's output policies: a reply that breaks one is generated again with the policy's "
    "message as a constraint, up to the story's polish budget (its configuration's "
    "`repair.polish.max_regenerations`; none with the polish pass off), and then the "
    "character's safe fallback line is returned instead (`guardrail.fallback_used`, "
    '`degradation: ["safe_fallback"]`). Rejected drafts are audit records only. '
    "Several characters are voiced in one generation, and "
    "their knowledge is kept apart only by instructions in that shared prompt."
    "\n\nEvery turn runs under a model-call budget and a deadline from the story's effective "
    "configuration (`GET /stories/{id}/effective-config`): `budgets.model_calls` (`null`, "
    "Lite's: `9 + G`, where G is one call per model-assisted input policy plus one per "
    "model-assisted output policy) and `budgets.deadline_ms`, or a smaller "
    "`budget: {max_calls, deadline_s}` in the request (above the story's is 422). A budget "
    "below one generation plus G is refused with 422 `budget_below_safety_minimum` "
    "(`details.minimum_required`) before any model call. The response reports `model_calls` "
    "(every attempt, failed ones included), `call_counts` by purpose, `elapsed_ms`, `budget` "
    "`{max_calls, reserved, used}` and each `degradation` step with its cause; a 503 carries "
    "`model_calls` and `elapsed_ms` in `details`. `queued_ms` is how long the turn waited in "
    "the story's turn queue (null when it never queued) and `first_call_offset_ms` the delay "
    "from the turn reaching the front (a queued turn starting, else the request arriving) to "
    "its first model call; `elapsed_ms` starts at that same moment."
    "\n\nThe turn captures the story's configuration when it starts and runs under it to the "
    "end: the deployment's profile defaults, then the story's engine profile at its latest "
    "version, then the story's overrides (FR-PROF-5,6). The generation settings come from it, "
    "then from the request's prompt parameters (FR-LITE-4). Every response states that "
    "configuration (FR-PROF-16): the engine `profile` (`{id, version}`) and the story's "
    "`config_revision` then, its `fingerprint` (`sha256:` over the profile version, each "
    "purpose's model and every quality-relevant setting, prompt templates included), the "
    "`capabilities` it enables, and its `quality_status` (`uncertified` until a certification "
    "matches the fingerprint, which no M3 configuration can have)."
    "\n\nOptional headers: `Idempotency-Key` (a retry returns the identical response) and "
    "`X-Epistrel-Expected-Revision` (a stale revision is refused with 409 and nothing written)."
    "\n\nWhile another turn of the story runs, a story with `turn_concurrency: reject` (the "
    "default) answers 423 `story_busy` with `Retry-After`, and one with `queue` queues the turn "
    "and plays the queued turns in order, each after the previous one finished. The request "
    "waits for its queued turn up to the story's turn deadline (`budgets.deadline_ms`), then "
    "answers 202 "
    "`{operation_id, position}` with `Location: /operations/{id}` while the turn stays queued; "
    "`Prefer: respond-async` answers 202 at once. A retry under the same `Idempotency-Key` "
    "waits for the same turn. A full queue (`turn_queue_max`) is 423."
)


class MessageBody(BaseModel):
    """The finished reply: the id of its transcript row and its text."""

    message_id: str
    text: str


class GuardrailViolationBody(BaseModel):
    """An output policy the released reply breaks."""

    policy_id: str
    name: str
    message: str
    kind: str


class GuardrailBody(BaseModel):
    """The output gate's outcome: the released reply's violations (empty unless even the
    last-resort fallback line broke a policy) and whether the reply is the safe fallback."""

    violations: list[GuardrailViolationBody]
    fallback_used: bool


class BudgetBody(BaseModel):
    """The turn's model-call budget: its cap, the guardrail calls it reserved
    (`guardrail_input`, `guardrail_output`) and the calls it made, which equal the entries of
    `model_calls`."""

    max_calls: int | None
    reserved: dict[str, int]
    used: int


class DegradationBody(BaseModel):
    """A degradation step and its cause (`retry_budget`, `call_budget` or `deadline`)."""

    step: str
    cause: str


class RespondResponse(MutationResponse):
    """One finished turn: the reply, the turn's beats for the resolved viewpoint, its position,
    the story's new revision, the model calls it made, where its time went, and the
    configuration it ran under: ``fingerprint``, ``capabilities`` and ``quality_status``
    (FR-PROF-16; null only on a replay of a turn stored before fingerprints existed), the engine
    ``profile`` and version it captured at its start, and the story's ``config_revision`` then
    (#113, C-16; null only on a replay of a turn stored before #113)."""

    message: MessageBody
    beats: list[ItemModel]
    turn: int
    variant: int
    output_viewpoint: str
    audience_filter: Literal["none", "co_location", "full"]
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)
    degradation: list[str]
    degradation_details: list[DegradationBody]
    guardrail: GuardrailBody
    call_counts: dict[str, int]
    budget: BudgetBody | None
    queued_ms: int | None = None
    first_call_offset_ms: int | None = None
    fingerprint: str | None
    capabilities: list[str]
    quality_status: QualityStatus | None
    profile: ProfileVersionRef | None
    config_revision: int | None

    @classmethod
    def of(cls, result: RespondResult) -> RespondResponse:
        return cls(
            message=MessageBody(message_id=result.message.message_id, text=result.message.text),
            beats=[item_model(b) for b in result.beats],
            turn=result.turn,
            variant=result.variant,
            timeline_revision=result.timeline_revision,
            output_viewpoint=result.output_viewpoint,
            audience_filter=result.audience_filter,
            model_calls=result.model_calls,
            elapsed_ms=result.elapsed_ms,
            degradation=result.degradation,
            degradation_details=[
                DegradationBody(step=d.step, cause=d.cause) for d in result.degradation_details
            ],
            call_counts=result.call_counts,
            queued_ms=result.queued_ms,
            first_call_offset_ms=result.first_call_offset_ms,
            fingerprint=result.fingerprint,
            capabilities=list(result.capabilities),
            quality_status=result.quality_status,
            profile=result.profile,
            config_revision=result.config_revision,
            budget=(
                None
                if result.budget is None
                else BudgetBody(
                    max_calls=result.budget.max_calls,
                    reserved=dict(result.budget.reserved),
                    used=result.budget.used,
                )
            ),
            guardrail=GuardrailBody(
                violations=[
                    GuardrailViolationBody(
                        policy_id=v.policy_id, name=v.name, message=v.message, kind=v.kind
                    )
                    for v in result.guardrail.violations
                ],
                fallback_used=result.guardrail.fallback_used,
            ),
        )


@router.post(
    "/stories/{story_id}/respond",
    response_model=RespondResponse,
    operation_id="respond",
    summary="Play one turn and return the finished reply",
    description=RESPOND_DESCRIPTION,
)
async def respond(
    story_id: StoryId,
    body: RespondRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    respond_async: RespondAsyncDep,
) -> RespondResponse:
    result = await engine.respond(ctx, story_id, body, respond_async=respond_async)
    return answered(RespondResponse.of(result), ctx, response, replayed=result.replayed)
