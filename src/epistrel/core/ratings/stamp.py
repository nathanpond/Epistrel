"""The guardrail set a turn variant passed (FR-RATE-7; #118).

Every committed turn variant records, in ``operations.checked_guardrails``, the effective
guardrail set it was checked against: the story's rating and its version, and every live
policy in effect for the story, by scope, with its version::

    {"rating_id": ..., "rating_version_id": ...,
     "global": {policy_id: version}, "rating": {...}, "story": {...}, "character": {...}}

A turn is stamped when it commits, under the story lock, with the set in force then: a Lite
turn whose configuration changed mid-flight was re-checked under that set before it committed
(FR-CONC-16, ``core.config_dependencies``), and an ingested turn's input was gated under it. A
tightening of the story's rating that applies restamps every active turn variant with the new
set (``core.ratings.change``). #120's reactivation check compares a turn's stamp with the set
in force when the turn would become active again.

``live_policies`` is the effective set of a story at a given rating, read in the caller's
transaction without a cache: every live global policy, the live rating policies of the
rating's closure (#48), and the story's own story- and character-scoped policies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.policies.model import SCOPE_RANK, Policy, Scope
from epistrel.core.policies.rows import policy_of, select_policies
from epistrel.core.ratings import effective_guardrails, load_graph
from epistrel.core.tables import content_ratings, policies, stories

STAMP_SCOPES = (Scope.GLOBAL, Scope.RATING, Scope.STORY, Scope.CHARACTER)


async def live_policies(
    conn: AsyncConnection, story_id: str | None, rating_id: str | None
) -> tuple[Policy, ...]:
    """The live policies in effect for ``story_id`` at ``rating_id`` (``story_id`` ``None``:
    a story not yet created, so global and rating policies only), ordered global, rating
    (nearest source first), story, character."""
    graph = await load_graph(conn)
    rating_ids = (
        [] if rating_id is None else [g.policy_id for g in effective_guardrails(rating_id, graph)]
    )
    scopes = [
        policies.c.scope == Scope.GLOBAL.value,
        (policies.c.scope == Scope.RATING.value) & policies.c.id.in_(rating_ids),
    ]
    if story_id is not None:
        scopes.append(policies.c.story_id == story_id)
    query = select_policies().where(policies.c.retired_at.is_(None), or_(*scopes))
    found = [policy_of(row) for row in (await conn.execute(query)).all()]
    position = {policy_id: index for index, policy_id in enumerate(rating_ids)}

    def key(policy: Policy) -> tuple[Any, ...]:
        rank = SCOPE_RANK[policy.scope]
        if policy.scope is Scope.RATING:
            return (rank, position[policy.id], "", "", "")
        return (rank, 0, policy.scope_id or "", policy.name.casefold(), policy.id)

    return tuple(sorted(found, key=key))


def stamp_of(
    rating_id: str | None, rating_version_id: str | None, found: Iterable[Policy]
) -> dict[str, Any]:
    """The ``checked_guardrails`` value for a rating version and an effective policy set."""
    stamp: dict[str, Any] = {
        "rating_id": rating_id,
        "rating_version_id": rating_version_id,
        **{scope.value: {} for scope in STAMP_SCOPES},
    }
    for policy in found:
        stamp[policy.scope.value][policy.id] = policy.version
    return stamp


async def story_rating(conn: AsyncConnection, story_id: str) -> tuple[str | None, str | None]:
    """``(rating id, current version id)`` of ``story_id``'s rating (``None``s: no story)."""
    row = (
        await conn.execute(
            select(stories.c.content_rating_id, content_ratings.c.current_version_id)
            .outerjoin(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
            .where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        return None, None
    return row.content_rating_id, row.current_version_id


async def guardrail_stamp(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """The set in force for ``story_id`` now, as a turn variant records it."""
    rating_id, version_id = await story_rating(conn, story_id)
    return stamp_of(rating_id, version_id, await live_policies(conn, story_id, rating_id))


def policy_ids(found: Sequence[Policy]) -> frozenset[str]:
    return frozenset(policy.id for policy in found)


__all__ = ["guardrail_stamp", "live_policies", "policy_ids", "stamp_of", "story_rating"]
