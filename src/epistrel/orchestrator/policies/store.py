"""The policy store's read path: listings and the effective set of a story (FR-GUARD-8,9).

Nothing here writes. **Effective policies** for a story are the union of four sets of live
policies: every global policy, the rating policies in the effective guardrails of the story's
rating (the closure from #48, nearest source first), and the story's own story-scoped and
character-scoped policies. Retired policies drop out. A lower scope only adds to the set:
nothing in it can remove or weaken a policy above it, because ``deny`` is the only effect
(``admin`` refuses anything else).

The set is cached in-process, keyed by the deployment ``config_revision`` (global and rating
policy writes and rating edits move it), the story's ``config_revision`` (story-scoped writes
move it), and the version ids of every rating in the story rating's closure.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_settings
from epistrel.core.errors import PolicyNotFound, StoryNotFound
from epistrel.core.policies.model import SCOPE_RANK, Policy, Scope
from epistrel.core.policies.rows import policy_of, select_policies
from epistrel.core.ratings import closure_versions, effective_guardrails, load_graph
from epistrel.core.tables import policies, stories

CACHE_SIZE = 1_024

type CacheKey = tuple[int, str, int, tuple[tuple[str, str], ...]]

_EFFECTIVE: OrderedDict[CacheKey, tuple[Policy, ...]] = OrderedDict()


def clear_effective_cache() -> None:
    _EFFECTIVE.clear()


def _order(policy: Policy) -> tuple[int, str, str, str]:
    return (SCOPE_RANK[policy.scope], policy.scope_id or "", policy.name.casefold(), policy.id)


async def list_policies(
    db: AsyncEngine,
    *,
    scope: Scope | None = None,
    story_id: str | None = None,
    include_retired: bool = False,
) -> list[Policy]:
    """Policies ordered by scope (global first), scope id, name; retired ones on request."""
    query = select_policies()
    if scope is not None:
        query = query.where(policies.c.scope == scope.value)
    if story_id is not None:
        query = query.where(policies.c.story_id == story_id)
    if not include_retired:
        query = query.where(policies.c.retired_at.is_(None))
    async with db.connect() as conn:
        found = [policy_of(row) for row in (await conn.execute(query)).all()]
    return sorted(found, key=_order)


async def load_policy(conn: AsyncConnection, policy_id: str) -> Policy | None:
    row = (await conn.execute(select_policies().where(policies.c.id == policy_id))).one_or_none()
    return None if row is None else policy_of(row)


async def get_policy(db: AsyncEngine, policy_id: str) -> Policy:
    """One policy, retired or not."""
    async with db.connect() as conn:
        policy = await load_policy(conn, policy_id)
    if policy is None:
        raise PolicyNotFound(f"policy {policy_id} does not exist", {"id": policy_id})
    return policy


async def effective_policies(db: AsyncEngine, story_id: str) -> tuple[Policy, ...]:
    """The story's effective policies: global, then rating (nearest source first), story, and
    character policies, live only. All reads come from one snapshot."""
    async with db.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            story = (
                await conn.execute(
                    select(stories.c.config_revision, stories.c.content_rating_id).where(
                        stories.c.id == story_id
                    )
                )
            ).one_or_none()
            if story is None:
                raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
            deployment = await read_settings(conn)
            graph = await load_graph(conn)
            key: CacheKey = (
                deployment.config_revision,
                story_id,
                int(story.config_revision),
                closure_versions(story.content_rating_id, graph),
            )
            cached = _EFFECTIVE.get(key)
            if cached is not None:
                _EFFECTIVE.move_to_end(key)
                return cached
            rating_ids = [g.policy_id for g in effective_guardrails(story.content_rating_id, graph)]
            found = await _live_for_story(conn, story_id, rating_ids)
    result = _arrange(found, rating_ids)
    _EFFECTIVE[key] = result
    if len(_EFFECTIVE) > CACHE_SIZE:
        _EFFECTIVE.popitem(last=False)
    return result


async def _live_for_story(
    conn: AsyncConnection, story_id: str, rating_ids: Sequence[str]
) -> list[Policy]:
    query = select_policies().where(
        policies.c.retired_at.is_(None),
        or_(
            policies.c.scope == Scope.GLOBAL.value,
            (policies.c.scope == Scope.RATING.value) & policies.c.id.in_(list(rating_ids)),
            policies.c.story_id == story_id,
        ),
    )
    return [policy_of(row) for row in (await conn.execute(query)).all()]


def _arrange(found: Sequence[Policy], rating_ids: Sequence[str]) -> tuple[Policy, ...]:
    position = {policy_id: index for index, policy_id in enumerate(rating_ids)}

    def key(policy: Policy) -> tuple[Any, ...]:
        rank = SCOPE_RANK[policy.scope]
        if policy.scope is Scope.RATING:
            return (rank, position[policy.id], "", "", "")
        return (rank, 0, policy.scope_id or "", policy.name.casefold(), policy.id)

    return tuple(sorted(found, key=key))
