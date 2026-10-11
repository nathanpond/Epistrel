"""Helpers for the policy-store tests: in-memory policies, rows hashed in the database, and
policies written through the library (``Engine.policies_*``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.ids import new_ulid
from epistrel.core.policies.model import (
    RULE_ADAPTER,
    Effect,
    Policy,
    Scope,
    Target,
    default_targets,
    kind_of,
)
from epistrel.core.ratings import RatingFields, RatingReplacement, create_rating, update_rating
from epistrel.orchestrator.policies.schemas import PolicyFields, StoryPolicyItem
from tests.event_support import rows

OPERATOR = RequestContext(principal="p-operator")
AUTHOR = RequestContext(principal="p-author")
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def policy(
    rule: dict[str, Any],
    *,
    policy_id: str | None = None,
    scope: Scope = Scope.GLOBAL,
    applies_to: tuple[Target, ...] | None = None,
    name: str | None = None,
) -> Policy:
    """An in-memory policy for the pure evaluator."""
    parsed = RULE_ADAPTER.validate_python(rule)
    pid = policy_id or new_ulid()
    return Policy(
        id=pid,
        scope=scope,
        scope_id=None if scope is Scope.GLOBAL else "s",
        story_id=None,
        name=name or pid,
        applies_to=applies_to or default_targets(scope),
        kind=kind_of(parsed),
        effect=Effect.DENY,
        rule=parsed,
        age_condition=None,
        message=f"{pid} says no",
        version=1,
        created_by="p",
        updated_by="p",
        created_at=EPOCH,
        updated_at=EPOCH,
        retired_at=None,
    )


def words(*entries: str) -> dict[str, Any]:
    return {"type": "wordlist", "words": list(entries)}


def unique(prefix: str) -> str:
    return f"{prefix}-{new_ulid().lower()}"


async def row_hashes(engine: AsyncEngine) -> dict[str, str]:
    """``md5(row::text)`` of every ``policies`` row, retired ones included."""
    found = await rows(engine, "SELECT id, md5(p::text) FROM policies p ORDER BY id")
    return {r[0]: r[1] for r in found}


def library(settings: Settings, engine: AsyncEngine) -> Engine:
    return Engine(settings, db=engine)


async def global_policy(lib: Engine, rule: dict[str, Any], **fields: Any) -> Policy:
    fields.setdefault("name", unique("global"))
    fields.setdefault("message", "Not here.")
    body = PolicyFields.model_validate({"scope": "global", "rule": rule, **fields})
    return (await lib.policies_create(OPERATOR, body)).policy


async def rated_policy_rating(
    lib: Engine, engine: AsyncEngine, rule: dict[str, Any]
) -> tuple[str, Policy]:
    """A new rating (inheriting from nothing) that lists one new rating policy."""
    rating = await create_rating(
        engine, RatingFields(name=unique("rating")), principal="p-operator"
    )
    body = PolicyFields.model_validate(
        {
            "scope": "rating",
            "scope_id": rating.rating.id,
            "name": unique("rating-policy"),
            "rule": rule,
            "message": "Not at this rating.",
        }
    )
    made = (await lib.policies_create(OPERATOR, body)).policy
    current = rating.rating
    await update_rating(
        engine,
        current.id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[made.id],
            inherits_from=[],
        ),
        principal="p-operator",
    )
    return current.id, made


def story_item(rule: dict[str, Any], **fields: Any) -> StoryPolicyItem:
    fields.setdefault("name", unique("story"))
    fields.setdefault("message", "Not in this story.")
    return StoryPolicyItem.model_validate({"rule": rule, **fields})
