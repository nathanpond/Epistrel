"""The configuration an operation runs under, and what it depends on (FR-CONC-14,16; #64).

Every story operation records, at start (``Operation.begin``, in its REPEATABLE READ read
phase), ``operations.config_revisions``: the story's and the deployment's configuration
revisions, the story rating's version, and the values of the interpretive settings its kind
depends on. The dependency set is static per operation kind in M2:

- a generated ``turn`` (a Lite turn) depends on ``rating_guardrail_set`` (the story rating's
  effective guardrails, #48's closure), ``policy_set`` (the output-applicable effective
  policies: global, the rating closure's, the story's and its characters', each with its
  version), ``minor_age_threshold`` and the story's ``default_output_viewpoint``;
- an ingested operation records ``policy_set`` only (its input was gated before it opened;
  nothing is re-checked at its commit in M2);
- every other kind records the revisions and nothing more.

The set values are hashes (``hash_member``), so detection at commit is a compare. At commit,
under the story lock, ``changed`` names the dependencies whose value moved since the capture:
a non-empty answer is an interpretive change the turn must be re-checked under (FR-CONC-16).
A prospective change moves a revision but no dependency, so it never triggers a re-check.
Rating *guidance* is not here: it is a read-set member (``guidance_text``, #47), so a
guidance-only edit regenerates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import hash_member
from epistrel.core.enums import OperationKind
from epistrel.core.policies.model import Scope, Target
from epistrel.core.policies.rows import policy_of, select_policies
from epistrel.core.ratings import effective_guardrails, load_graph
from epistrel.core.settings_schema import StorySettings
from epistrel.core.tables import content_ratings, deployment_state, policies, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

REVISIONS = ("story", "deployment", "rating_version")
"""Recorded by every story operation."""

LITE_DEPENDENCIES = (
    "rating_guardrail_set",
    "policy_set",
    "minor_age_threshold",
    "default_output_viewpoint",
)
"""What a Lite turn's commit is re-checked against when it changes (M4 widens it)."""

INGEST_CAPTURED = ("policy_set",)


def captured_for(kind: OperationKind, origin: str) -> tuple[str, ...]:
    """The dependency members an operation records at start."""
    if origin == "ingested":
        return INGEST_CAPTURED
    if kind is OperationKind.TURN:
        return LITE_DEPENDENCIES
    return ()


def dependencies_for(kind: OperationKind, origin: str) -> tuple[str, ...]:
    """The members compared at commit: a generated turn's; nothing for any other kind."""
    if kind is OperationKind.TURN and origin == "generated":
        return LITE_DEPENDENCIES
    return ()


async def _policy_set(
    conn: AsyncConnection, story_id: str, rating_policy_ids: Sequence[str]
) -> str:
    """Hash of the live output-applicable effective policies as sorted ``[id, version]``."""
    scopes = [
        policies.c.scope == Scope.GLOBAL.value,
        (policies.c.scope == Scope.RATING.value) & policies.c.id.in_(list(rating_policy_ids)),
        policies.c.story_id == story_id,
    ]
    found = [
        policy_of(row)
        for row in (
            await conn.execute(
                select_policies().where(policies.c.retired_at.is_(None), or_(*scopes))
            )
        ).all()
    ]
    return hash_member(
        sorted([p.id, p.version] for p in found if Target.TEXT_OUTPUT in p.applies_to)
    )


async def capture(
    conn: AsyncConnection, story_id: str, members: Sequence[str]
) -> dict[str, Any] | None:
    """``config_revisions`` for an operation of ``story_id`` recording ``members`` (``None``
    when the story does not exist)."""
    story = (
        await conn.execute(
            select(
                stories.c.config_revision,
                stories.c.content_rating_id,
                stories.c.settings,
                content_ratings.c.current_version_id,
            )
            .outerjoin(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
            .where(stories.c.id == story_id)
        )
    ).one_or_none()
    if story is None:
        return None
    deployment = (
        await conn.execute(
            select(
                deployment_state.c.config_revision, deployment_state.c.minor_age_threshold
            ).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        )
    ).one()
    captured: dict[str, Any] = {
        "story": int(story.config_revision),
        "deployment": int(deployment.config_revision),
        "rating_version": story.current_version_id,
    }
    if "rating_guardrail_set" in members or "policy_set" in members:
        graph = await load_graph(conn)
        guardrails = [g.policy_id for g in effective_guardrails(story.content_rating_id, graph)]
        if "rating_guardrail_set" in members:
            captured["rating_guardrail_set"] = hash_member(guardrails)
        if "policy_set" in members:
            captured["policy_set"] = await _policy_set(conn, story_id, guardrails)
    if "minor_age_threshold" in members:
        captured["minor_age_threshold"] = int(deployment.minor_age_threshold)
    if "default_output_viewpoint" in members:
        captured["default_output_viewpoint"] = StorySettings.from_row(
            story.settings
        ).default_output_viewpoint
    return captured


def changed(
    captured: Mapping[str, Any] | None, current: Mapping[str, Any] | None, members: Sequence[str]
) -> tuple[str, ...]:
    """The dependency members whose value differs between the capture and now."""
    before, now = captured or {}, current or {}
    return tuple(name for name in members if before.get(name) != now.get(name))


__all__ = [
    "LITE_DEPENDENCIES",
    "REVISIONS",
    "capture",
    "captured_for",
    "changed",
    "dependencies_for",
]
