"""Consumer purpose calls (FR-MODEL-15; #825).

``POST /purposes/{name}/calls`` calls a consumer purpose (registered under ``/admin/purposes``,
FR-MODEL-14) with **no story** and answers its structured output synchronously, after the same
safety path as everything else (``orchestrator.purposes.call``): the input gate on every string
input, the minors policy with the true ages ``depicted`` declares, structured output against the
purpose's schema, and the output gate on every string of the result under the call's content
rating, failing closed. ``Idempotency-Key`` is honoured: a repeat answers the first result with
no model call (FR-RESP-6). A consumer route: it takes the service credential, like every route.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.orchestrator.purposes.call import CallModel, CallUsage, PurposeCall, PurposeResult

router = APIRouter(prefix="/purposes", tags=["purposes"])

PurposeNamePath = Annotated[
    str,
    Path(max_length=64, description="The consumer purpose's name.", examples=["vote_reason"]),
]


class PurposeCallBody(DeploymentMutationResponse):
    """A consumer purpose call's answer: the purpose and the version that answered, its
    ``output`` (valid against that version's output schema), the model it went to, and what
    it used: the purpose's own model calls and tokens, and apart, the guardrail checks.
    ``config_revision`` is the deployment configuration it was routed by."""

    purpose: str
    version: int
    output: dict[str, Any]
    model: CallModel
    usage: CallUsage

    @classmethod
    def of(cls, result: PurposeResult) -> PurposeCallBody:
        return cls(
            config_revision=result.config_revision,
            purpose=result.purpose,
            version=result.version,
            output=result.output,
            model=result.model,
            usage=result.usage,
        )


@router.post(
    "/{name}/calls",
    response_model=PurposeCallBody,
    operation_id="call_purpose",
    summary="Call a consumer purpose with no story",
    description=(
        "Render the purpose's templates with `inputs` and call its model (routed by the "
        "purpose's own model configuration, else `generation`'s), and answer the output, "
        "valid against the purpose's output schema (FR-MODEL-15). Every string in `inputs` "
        "passes the input gate under `content_rating` (default: the deployment's default "
        "rating) first: a violation is 422 `guardrail_violation` and the purpose's model is "
        "not called. `depicted` declares the people the call depicts with their true ages, so "
        "the minors policy applies when one is a minor. Every string of the output passes the "
        "output gate under the same rating: a violation is retried once, then refused with 422 "
        "`output_policy_violation`, with no part of the output. 404 for an unknown purpose or "
        "version (`version_not_found`), 410 `purpose_archived`, 422 `template_render_error` "
        "when the template does not render for `inputs`, 502 `model_output_invalid` when no "
        "answer matches the schema, 503 `model_unavailable` (`details.reason: deadline` once "
        "`deadline_ms` passes)."
    ),
)
async def call_purpose(
    name: PurposeNamePath,
    body: PurposeCall,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PurposeCallBody:
    result = await engine.call_purpose(
        ctx,
        name,
        body.inputs,
        version=body.version,
        content_rating=body.content_rating,
        depicted=body.depicted,
        deadline_ms=body.deadline_ms,
    )
    return answered(PurposeCallBody.of(result), ctx, response, replayed=result.replayed)


__all__ = ["PurposeCallBody", "router"]
