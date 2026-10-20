"""The policy store's read path: listings and the effective set of a story (FR-GUARD-8,9).

Nothing here writes. **Effective policies** for a story are the union of four sets of live
policies: every global policy, the rating policies in the effective guardrails of the story's
rating (the closure from #48, nearest source first), and the story's own story-scoped and
character-scoped policies. Retired policies drop out. A lower scope only adds to the set:
nothing in it can remove or weaken a policy above it, because ``deny`` is the only effect
(``admin`` refuses anything else).

``guard_context`` adds what age-conditioned policies need (#55): the story's cast with true
ages and the live ``minor_age_threshold``, and splits the set into the policies that apply and
those whose age condition is false (``age.partition``).

The set is cached in-process, keyed by the deployment ``config_revision`` (global and rating
policy writes and rating edits move it), the story's ``config_revision`` (story-scoped writes
move it), and the version ids of every rating in the story rating's closure.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_settings
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import PolicyNotFound, StoryNotFound
from epistrel.core.policies.model import (
    SCOPE_RANK,
    ModelAssistedRule,
    Policy,
    Scope,
    Target,
    WithheldPolicy,
)
from epistrel.core.policies.rows import policy_of, select_policies
from epistrel.core.ratings import closure_versions, effective_guardrails, load_graph
from epistrel.core.tables import characters, policies, stories
from epistrel.orchestrator.policies.age import CastMember, partition

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
    exclude_blocked: bool = False,
) -> list[Policy]:
    """Policies ordered by scope (global first), scope id, name; retired ones on request.
    ``exclude_blocked`` leaves out the story and character policies of a story the erasure
    reconciler holds ``blocked_by_erasure`` (#363)."""
    query = select_policies()
    if exclude_blocked:
        query = query.where(
            or_(
                policies.c.story_id.is_(None),
                exists(
                    select(stories.c.id).where(
                        stories.c.id == policies.c.story_id,
                        stories.c.status != StoryStatus.BLOCKED_BY_ERASURE.value,
                    )
                ),
            )
        )
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
    conn: AsyncConnection, story_id: str | None, rating_ids: Sequence[str]
) -> list[Policy]:
    """Live global policies, the listed rating policies and (with ``story_id``) the story's own."""
    scopes = [
        policies.c.scope == Scope.GLOBAL.value,
        (policies.c.scope == Scope.RATING.value) & policies.c.id.in_(list(rating_ids)),
    ]
    if story_id is not None:
        scopes.append(policies.c.story_id == story_id)
    query = select_policies().where(policies.c.retired_at.is_(None), or_(*scopes))
    return [policy_of(row) for row in (await conn.execute(query)).all()]


def _arrange(found: Sequence[Policy], rating_ids: Sequence[str]) -> tuple[Policy, ...]:
    position = {policy_id: index for index, policy_id in enumerate(rating_ids)}

    def key(policy: Policy) -> tuple[Any, ...]:
        rank = SCOPE_RANK[policy.scope]
        if policy.scope is Scope.RATING:
            return (rank, position[policy.id], "", "", "")
        return (rank, 0, policy.scope_id or "", policy.name.casefold(), policy.id)

    return tuple(sorted(found, key=key))


@dataclass(frozen=True, slots=True)
class GuardContext:
    """What a guardrail gate needs for one story: its effective policies, its cast, and the
    deployment's live ``minor_age_threshold``."""

    policies: tuple[Policy, ...]
    cast: tuple[CastMember, ...]
    threshold: int

    @property
    def active(self) -> tuple[Policy, ...]:
        """The effective policies that apply: age-conditioned ones only with a minor present."""
        return partition(self.policies, self.cast, self.threshold)[0]

    @property
    def inactive(self) -> tuple[Policy, ...]:
        """Age-conditioned policies whose condition is false for this cast."""
        return partition(self.policies, self.cast, self.threshold)[1]


async def read_cast(conn: AsyncConnection, story_id: str) -> tuple[CastMember, ...]:
    """The story's cast in creation order, with true ages (never leaves the engine)."""
    rows = (
        await conn.execute(
            select(
                characters.c.id,
                characters.c.ordinal,
                characters.c.true_age,
                characters.c.age_recorded_at,
            )
            .where(characters.c.story_id == story_id)
            .order_by(characters.c.ordinal)
        )
    ).all()
    return tuple(
        CastMember(
            character_id=row.id,
            ordinal=int(row.ordinal),
            true_age=int(row.true_age),
            age_recorded_at=row.age_recorded_at,
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class StoryGuardrails:
    """What ``GET /stories/{id}/settings`` shows of a story's guardrails (FR-GUARD-9): the
    active and inactive effective policies, the source of each, and the model calls they add.

    ``calls_input`` is per text checked with no character acting (a character-scoped input
    policy adds one call when its character acts); ``calls_output`` per output checkpoint."""

    active: tuple[Policy | WithheldPolicy, ...]
    inactive: tuple[Policy | WithheldPolicy, ...]
    sources: dict[str, str]
    calls_input: int
    calls_output: int

    def withholding(self, withheld: Collection[str]) -> StoryGuardrails:
        """These guardrails with each policy in ``withheld`` (the ids a pending catalog-scope
        erasure reaches) shown as a ``WithheldPolicy``: no name, message or rule until it is
        reapplied (#447, ADR-161). The call counts are the enforced set's, unchanged."""
        if not withheld:
            return self
        return replace(
            self,
            active=withhold(self.active, withheld),
            inactive=withhold(self.inactive, withheld),
        )

    def source(self, policy: Policy | WithheldPolicy) -> str:
        """``global``, ``rating:<id>`` (the contributing rating), ``story`` or
        ``character:<id>``."""
        if policy.scope is Scope.RATING:
            return f"rating:{self.sources.get(policy.id, policy.scope_id)}"
        if policy.scope is Scope.CHARACTER:
            return f"character:{policy.scope_id}"
        return policy.scope.value


def withhold(
    found: Sequence[Policy | WithheldPolicy], withheld: Collection[str]
) -> tuple[Policy | WithheldPolicy, ...]:
    """``found`` in order, each policy in ``withheld`` as its ``WithheldPolicy`` (#447)."""
    return tuple(
        WithheldPolicy.of(p) if isinstance(p, Policy) and p.id in withheld else p for p in found
    )


def _assisted_count(found: Sequence[Policy], target: Target) -> int:
    return sum(
        1
        for p in found
        if isinstance(p.rule, ModelAssistedRule)
        and target in p.applies_to
        and not (target is Target.TEXT_INPUT and p.scope is Scope.CHARACTER)
    )


async def story_guardrails(db: AsyncEngine, story_id: str) -> StoryGuardrails:
    context = await guard_context(db, story_id)
    sources = await rating_sources(db, story_id)
    active = context.active
    return StoryGuardrails(
        active=active,
        inactive=context.inactive,
        sources=sources,
        calls_input=_assisted_count(active, Target.TEXT_INPUT),
        calls_output=_assisted_count(active, Target.TEXT_OUTPUT),
    )


async def deployment_guardrail_calls(db: AsyncEngine) -> tuple[int, int]:
    """G as the deployment's global policies state it (the profile validator's budget warning,
    #156): its active model-assisted policies applying to input and to output."""
    found = await list_policies(db, scope=Scope.GLOBAL)
    return _assisted_count(found, Target.TEXT_INPUT), _assisted_count(found, Target.TEXT_OUTPUT)


async def rating_sources(db: AsyncEngine, story_id: str) -> dict[str, str]:
    """Per rating policy in the story's effective set, the rating that contributes it (the
    nearest in the closure, #48)."""
    async with db.connect() as conn, conn.begin():
        rating_id = await conn.scalar(
            select(stories.c.content_rating_id).where(stories.c.id == story_id)
        )
        if rating_id is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        graph = await load_graph(conn)
    return {g.policy_id: g.source_rating_id for g in effective_guardrails(rating_id, graph)}


async def creation_context(
    db: AsyncEngine, rating_id: str | None, cast: Sequence[CastMember]
) -> GuardContext:
    """The guard context of a story about to be created with ``rating_id`` and ``cast``: the
    live global policies and the rating closure's (a new story has no policies of its own),
    the cast as given, and the live threshold. Without a rating (a catalog entry that names
    none, #111), the global policies only."""
    async with db.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            graph = await load_graph(conn)
            rating_ids = (
                []
                if rating_id is None
                else [g.policy_id for g in effective_guardrails(rating_id, graph)]
            )
            found = await _live_for_story(conn, None, rating_ids)
            threshold = (await read_settings(conn)).minor_age_threshold
    return GuardContext(policies=_arrange(found, rating_ids), cast=tuple(cast), threshold=threshold)


async def guard_context(db: AsyncEngine, story_id: str) -> GuardContext:
    """The story's effective policies, cast and live threshold (``StoryNotFound`` if absent)."""
    found = await effective_policies(db, story_id)
    async with db.connect() as conn, conn.begin():
        cast = await read_cast(conn, story_id)
        threshold = (await read_settings(conn)).minor_age_threshold
    return GuardContext(policies=found, cast=cast, threshold=threshold)
