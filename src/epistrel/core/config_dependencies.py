"""The configuration an operation runs under, and what it depends on (FR-CONC-14,16; #64).

Every story operation records, at start (``Operation.begin``, in its REPEATABLE READ read
phase), ``operations.config_revisions``: the story's and the deployment's configuration
revisions, the story rating's version, and the values of the interpretive settings its kind
depends on. The dependency set is static per operation kind in M2:

- a generated ``turn`` (a Lite turn) depends on ``content_rating_id`` (which rating the story
  has: a change of it, even to a rating with the same guardrails, re-checks the turn under the
  new rating and records its version, FR-RATE-6; #118), ``rating_guardrail_set`` (the story
  rating's effective guardrails, #48's closure), ``policy_set`` (the output-applicable
  effective policies: global, the rating closure's, the story's and its characters', each with
  its version), ``minor_age_threshold`` and the story's ``default_output_viewpoint``;
- an ingested operation records ``policy_set`` only (its input was gated before it opened;
  nothing is re-checked at its commit in M2);
- every other kind records the revisions and nothing more.

A **structured** turn (one whose read set is keyed, #136; ``Operation.use_read_set``) depends on
the Lite turn's members and on what its deterministic checks interpret (FR-CONC-16, M4):
``predicate_registry`` (the story's predicates as registered: an auto-registration, an approved
proposal or an author edit changes it) and ``verification_settings`` (the effective engine
profile's interpretive ``verification.*`` settings, ``engine_profile:<id>.<path>`` changes,
#113; a prospective one such as ``verification.entailment_gate`` is not a member). A change to
either re-checks the turn: its policy checks and its ``REBASE_CHECKS`` (#140's verify, #144's
permission checks) run under the new configuration. #144 adds what its permission checks
interpret: ``allow_new_arrivals`` (the story's arrival policy, a story setting) and
``author_locks`` (a hash of the current ``author_locked`` fact versions' ``(id, subject,
predicate)``: an author locking or unlocking canon, #145, changes it). #161 adds
``perception_level`` (the level routing runs at under the effective profile,
``core.epistemic.scene.level_of``: ``none``, ``co_location`` or ``full``): a change re-checks the
turn, whose commit then routes under the new level, and ``core.epistemic.scene.recheck`` (a
``REBASE_CHECKS`` entry) regenerates a turn begun with partitions that no longer has them.
#180 adds ``player_authority`` (FR-RESP-11, a story setting): how the turn treated the player's
input about other characters (M6's rebase case needs it). #185 adds ``state_definitions`` (a hash
of every active State declaration's interpretive members, ``model.INTERPRETIVE_MEMBERS``,
like ``author_locks``): an interpretive definition update re-checks the turn under the new
definition (#187's ``REBASE_CHECKS``); a prospective one (aliases, unit) does not move it.

The set values are hashes (``hash_member``), so detection at commit is a compare. At commit,
under the story lock, ``changed`` names the dependencies whose value moved since the capture:
a non-empty answer is an interpretive change the turn must be re-checked under (FR-CONC-16).
A prospective change moves a revision but no dependency, so it never triggers a re-check.
Rating *guidance* is not here: it is a read-set member (``guidance_text``, #47), so a
guidance-only edit regenerates.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.concurrency import hash_member
from epistrel.core.enums import OperationKind
from epistrel.core.policies.model import Scope, Target
from epistrel.core.policies.rows import policy_of, select_policies
from epistrel.core.ratings import effective_guardrails, load_graph
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.tables import content_ratings, deployment_state, policies, predicates, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

REVISIONS = ("story", "deployment", "rating_version")
"""Recorded by every story operation."""

LITE_DEPENDENCIES = (
    "content_rating_id",
    "rating_guardrail_set",
    "policy_set",
    "minor_age_threshold",
    "default_output_viewpoint",
)
"""What a Lite turn's commit is re-checked against when it changes (M4 widens it)."""

STRUCTURED_DEPENDENCIES = (
    *LITE_DEPENDENCIES,
    "predicate_registry",
    "verification_settings",
    "allow_new_arrivals",
    "author_locks",
    "perception_level",
    "player_authority",
    "state_definitions",
)
"""What a structured turn's commit is re-checked against when it changes (#136, #144, #161,
#180, #185)."""

VERIFICATION_PREFIX = "verification."

INGEST_CAPTURED = ("policy_set",)


def captured_for(kind: OperationKind, origin: str, *, structured: bool = False) -> tuple[str, ...]:
    """The dependency members an operation records at start."""
    if origin == "ingested":
        return INGEST_CAPTURED
    if kind is OperationKind.TURN:
        return STRUCTURED_DEPENDENCIES if structured else LITE_DEPENDENCIES
    return ()


def dependencies_for(
    kind: OperationKind, origin: str, *, structured: bool = False
) -> tuple[str, ...]:
    """The members compared at commit: a generated turn's; nothing for any other kind."""
    if kind is OperationKind.TURN and origin == "generated":
        return STRUCTURED_DEPENDENCIES if structured else LITE_DEPENDENCIES
    return ()


async def _predicate_registry(
    conn: AsyncConnection, story_id: str, own: Collection[str] = ()
) -> str:
    """Hash of the story's own predicate rows (the core vocabulary is the same for every story
    and changes only with a release), without ``own``: the ones the capturing operation
    registered itself (``core.own_predicates``)."""
    query = select(predicates).where(predicates.c.story_id == story_id)
    if own:
        query = query.where(predicates.c.id.not_in(sorted(own)))
    found = await conn.execute(query.order_by(predicates.c.id))
    return hash_member([list(map(str, row)) for row in found.all()])


async def _verification_settings(conn: AsyncConnection, story_id: str) -> str:
    """Hash of the story's effective interpretive ``verification.*`` profile settings."""
    from epistrel.core.profiles import schema  # here: profiles import the settings schema
    from epistrel.core.profiles.effective import effective_configuration

    profile = await effective_configuration(conn, story_id, validate=False)
    paths = sorted(
        path
        for path, tags in schema.setting_tags().items()
        if path.startswith(VERIFICATION_PREFIX) and tags.change_class is ConfigClass.interpretive
    )
    return hash_member([[path, schema.value_at(profile.settings, path)] for path in paths])


async def _perception_level(conn: AsyncConnection, story_id: str) -> str:
    """The perception level routing runs at under the story's effective profile (#161)."""
    from epistrel.core.epistemic.scene import current_level  # here: it imports the read set

    return await current_level(conn, story_id)


async def state_definitions_hash(conn: AsyncConnection, story_id: str) -> str:
    """Hash of every active State declaration's interpretive members, by name (#185)."""
    from epistrel.core.states import definitions  # here: core.states imports the config log

    return hash_member([state.interpretive() for state in await definitions(conn, story_id)])


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
    conn: AsyncConnection,
    story_id: str,
    members: Sequence[str],
    *,
    own_predicates: Collection[str] = (),
) -> dict[str, Any] | None:
    """``config_revisions`` for an operation of ``story_id`` recording ``members`` (``None``
    when the story does not exist). ``own_predicates`` (the operation's own auto-registrations,
    ``core.own_predicates``) are left out of ``predicate_registry``."""
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
    if "content_rating_id" in members:
        captured["content_rating_id"] = story.content_rating_id
    if "rating_guardrail_set" in members or "policy_set" in members:
        graph = await load_graph(conn)
        guardrails = [g.policy_id for g in effective_guardrails(story.content_rating_id, graph)]
        if "rating_guardrail_set" in members:
            captured["rating_guardrail_set"] = hash_member(guardrails)
        if "policy_set" in members:
            captured["policy_set"] = await _policy_set(conn, story_id, guardrails)
    if "minor_age_threshold" in members:
        captured["minor_age_threshold"] = int(deployment.minor_age_threshold)
    if "predicate_registry" in members:
        captured["predicate_registry"] = await _predicate_registry(conn, story_id, own_predicates)
    if "verification_settings" in members:
        captured["verification_settings"] = await _verification_settings(conn, story_id)
    if "perception_level" in members:
        captured["perception_level"] = await _perception_level(conn, story_id)
    if "allow_new_arrivals" in members:
        captured["allow_new_arrivals"] = StorySettings.from_row(story.settings).allow_new_arrivals
    if "player_authority" in members:
        captured["player_authority"] = StorySettings.from_row(story.settings).player_authority
    if "author_locks" in members:
        from epistrel.core.canon.facts import locked_keys  # here: canon imports the read set

        captured["author_locks"] = hash_member(
            [list(key) for key in await locked_keys(conn, story_id)]
        )
    if "state_definitions" in members:
        captured["state_definitions"] = await state_definitions_hash(conn, story_id)
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
    "STRUCTURED_DEPENDENCIES",
    "capture",
    "captured_for",
    "changed",
    "dependencies_for",
    "state_definitions_hash",
]
