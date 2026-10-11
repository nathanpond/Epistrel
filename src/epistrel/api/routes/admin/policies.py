"""``/admin/policies``: global and rating policies (FR-GUARD-1,8,9).

Create, list (``?scope``, ``?story_id``, retired ones only with ``?include_retired=1``), get,
replace and retire (``DELETE``). Every policy body exposes ``kind``, so an operator can see
which policies cost a model call (FR-GUARD-9). Story and character policies are listed here
but written through story settings (#63). Writes return the deployment ``config_revision``
and accept an optional ``Idempotency-Key`` (a repeated ``POST`` returns the first result).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.policies.model import Policy, PolicyKind, Scope, Target
from epistrel.core.policies.rows import rule_json
from epistrel.orchestrator.policies.schemas import PolicyFields, PolicyReplacement

router = APIRouter(prefix="/policies")


class PolicyBody(BaseModel):
    id: str
    scope: Scope
    scope_id: str | None
    story_id: str | None
    name: str
    applies_to: list[Target]
    kind: PolicyKind
    effect: str
    rule: dict[str, Any]
    age_condition: str | None
    message: str
    version: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None

    @classmethod
    def of(cls, policy: Policy) -> PolicyBody:
        return cls(
            id=policy.id,
            scope=policy.scope,
            scope_id=policy.scope_id,
            story_id=policy.story_id,
            name=policy.name,
            applies_to=list(policy.applies_to),
            kind=policy.kind,
            effect=policy.effect.value,
            rule=rule_json(policy.rule),
            age_condition=policy.age_condition,
            message=policy.message,
            version=policy.version,
            created_by=policy.created_by,
            updated_by=policy.updated_by,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
            retired_at=policy.retired_at,
        )


class PolicyList(BaseModel):
    items: list[PolicyBody]


class PolicyMutation(PolicyBody, DeploymentMutationResponse):
    """A policy after a write, with the deployment's ``config_revision``."""

    @classmethod
    def written(cls, policy: Policy, config_revision: int) -> PolicyMutation:
        return cls(**PolicyBody.of(policy).model_dump(), config_revision=config_revision)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PolicyMutation)
async def create_policy(body: PolicyFields, ctx: ContextDep, engine: EngineDep) -> PolicyMutation:
    write = await engine.policies_create(ctx, body)
    return PolicyMutation.written(write.policy, write.config_revision)


@router.get("", response_model=PolicyList)
async def list_policies(
    ctx: ContextDep,
    engine: EngineDep,
    scope: Scope | None = None,
    story_id: str | None = None,
    include_retired: bool = False,
) -> PolicyList:
    found = await engine.policies_list(
        ctx, scope=scope, story_id=story_id, include_retired=include_retired
    )
    return PolicyList(items=[PolicyBody.of(policy) for policy in found])


@router.get("/{policy_id}", response_model=PolicyBody)
async def get_policy(policy_id: str, ctx: ContextDep, engine: EngineDep) -> PolicyBody:
    return PolicyBody.of(await engine.policies_get(ctx, policy_id))


@router.put("/{policy_id}", response_model=PolicyMutation)
async def update_policy(
    policy_id: str, body: PolicyReplacement, ctx: ContextDep, engine: EngineDep
) -> PolicyMutation:
    write = await engine.policies_update(ctx, policy_id, body)
    return PolicyMutation.written(write.policy, write.config_revision)


@router.delete(
    "/{policy_id}",
    response_model=PolicyMutation,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Already retired; nothing changed."}},
)
async def retire_policy(
    policy_id: str, ctx: ContextDep, engine: EngineDep
) -> PolicyMutation | Response:
    """Retire the policy (200); 204 when it was already retired."""
    write = await engine.policies_retire(ctx, policy_id)
    if not write.changed:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return PolicyMutation.written(write.policy, write.config_revision)
