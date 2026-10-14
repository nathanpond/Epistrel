"""The policy store's **only** write path (FR-GUARD-1,3,8, NFR-SEC-1).

Two surfaces call it, both through ``epistrel.Engine``'s ``policies_*`` methods. The operator
surface (``/admin/policies``) writes global and rating policies. Story settings (#63's
``PATCH /stories/{id}/settings``) write story and character policies. Nothing reachable from
story content or model output imports this module: an import-linter contract forbids
``epistrel.orchestrator.lite`` and ``epistrel.core.events`` from importing it, and a test
checks that ``epistrel.engine`` is its only importer.

**Layering (FR-GUARD-8).** Lower scopes add, never relax. The only effect is ``deny``. Any
other ``effect`` (``allow``, ``exempt``, and so on) is refused with ``cannot_relax`` at every
scope, global included. A story or character item that names a policy of a higher scope is
also refused with ``cannot_relax``, whether a global or rating policy, or the story's own
policy named from character scope. Neither surface has an operation that removes or edits a
higher-scope policy.

**Recording.** Global and rating writes take the deployment lock, move the deployment
``config_revision`` by one, and record a ``config_changes`` row (``policy:<id>``, class
interpretive, the acting principal, old and new content). Story-scoped writes lock the story
row, move the story's ``config_revision`` by one per write, and record story-scope
``config_changes`` rows. Edits are made in place with ``version`` incremented. A write that
changes nothing records nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import pause
from epistrel.core.config_changes import (
    RETIRED,
    ConfigScope,
    bump_config_revision,
    lock_deployment,
    record_config_change,
)
from epistrel.core.enums import StoryStatus
from epistrel.core.erasure.records import index_rows, policy_record
from epistrel.core.errors import (
    CannotRelax,
    ConfigConflict,
    InvalidRequest,
    PolicyNotFound,
    StoryArchived,
    StoryNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.policies.model import (
    SCOPE_RANK,
    STORY_SCOPES,
    Effect,
    EntityListRule,
    Policy,
    Rule,
    Scope,
    Target,
    default_targets,
    kind_of,
)
from epistrel.core.policies.rows import content_of, rule_json
from epistrel.core.tables import characters, content_ratings, policies, stories
from epistrel.orchestrator.policies.schemas import (
    PolicyContent,
    PolicyFields,
    PolicyReplacement,
    StoryPolicyItem,
)
from epistrel.orchestrator.policies.store import load_policy


def policy_setting(policy_id: str) -> str:
    """The ``setting`` of a policy's ``config_changes`` rows."""
    return f"policy:{policy_id}"


@dataclass(frozen=True, slots=True)
class PolicyWrite:
    """A policy after a write and the revision the write left (deployment or story)."""

    policy: Policy
    config_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class StoryPoliciesWrite:
    policies: tuple[Policy, ...]
    config_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class _Content:
    name: str
    applies_to: tuple[Target, ...]
    rule: Rule
    age_condition: str | None
    message: str

    @classmethod
    def of(cls, policy: Policy) -> _Content:
        return cls(
            name=policy.name,
            applies_to=policy.applies_to,
            rule=policy.rule,
            age_condition=policy.age_condition,
            message=policy.message,
        )

    @classmethod
    def given(cls, body: PolicyContent, scope: Scope) -> _Content:
        targets = body.applies_to if body.applies_to is not None else default_targets(scope)
        return cls(
            name=body.name,
            applies_to=tuple(dict.fromkeys(targets)),
            rule=body.rule,
            age_condition=body.age_condition,
            message=body.message,
        )

    def values(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "applies_to": [t.value for t in self.applies_to],
            "kind": kind_of(self.rule).value,
            "rule": rule_json(self.rule),
            "age_condition": self.age_condition,
            "message": self.message,
        }


# -- checks -----------------------------------------------------------------------------------


def refuse_relaxing(effect: str, *, field: str = "body.effect") -> None:
    """``deny`` is the only effect; anything else would relax a policy (FR-GUARD-8)."""
    if effect != Effect.DENY.value:
        raise CannotRelax(
            "a policy can only deny; lower scopes add policies and never relax them",
            {"field": field, "reason": "effect", "effect": effect},
        )


def _check_rule(scope: Scope, rule: Rule, *, field: str) -> None:
    if scope is Scope.GLOBAL and isinstance(rule, EntityListRule):
        raise InvalidRequest(
            "entity_list rules name a story's characters and cannot be global",
            {"field": field, "reason": "entity_list_on_global"},
        )
    if isinstance(rule, EntityListRule) and not rule.names and scope not in STORY_SCOPES:
        raise InvalidRequest(
            "only a story or character entity_list may leave its names to the cast",
            {"field": f"{field}.names", "reason": "names_required"},
        )


async def _freeze_entity_names(
    conn: AsyncConnection, story_id: str, scope: Scope, scope_id: str, rule: Rule
) -> Rule:
    """An ``entity_list`` with no names takes the story's characters' names, frozen now: the
    whole cast (in cast order) for a story policy, the character itself for a character one."""
    if not isinstance(rule, EntityListRule) or rule.names:
        return rule
    query = select(characters.c.name).where(characters.c.story_id == story_id)
    if scope is Scope.CHARACTER:
        query = query.where(characters.c.id == scope_id)
    names = (await conn.execute(query.order_by(characters.c.ordinal))).scalars().all()
    return rule.model_copy(update={"names": tuple(names)})


async def _check_character(
    conn: AsyncConnection, story_id: str, character_id: str, *, field: str
) -> None:
    """A character policy names a character of its own story (FR-STORE-14)."""
    found = await conn.scalar(
        select(characters.c.id).where(
            characters.c.id == character_id, characters.c.story_id == story_id
        )
    )
    if found is None:
        raise InvalidRequest(
            "a character policy names a character of its story in scope_id",
            {"field": field, "reason": "unknown_character"},
        )


async def _check_name(
    conn: AsyncConnection,
    scope: Scope,
    scope_id: str | None,
    name: str,
    *,
    policy_id: str,
    field: str = "body.name",
) -> None:
    clash = await conn.scalar(
        select(policies.c.id).where(
            policies.c.scope == scope.value,
            func.coalesce(policies.c.scope_id, "") == (scope_id or ""),
            func.lower(policies.c.name) == name.lower(),
            policies.c.retired_at.is_(None),
            policies.c.id != policy_id,
        )
    )
    if clash is not None:
        raise InvalidRequest(
            "another live policy of this scope has this name", {"field": field, "conflict": clash}
        )


async def _require(conn: AsyncConnection, policy_id: str) -> Policy:
    policy = await load_policy(conn, policy_id)
    if policy is None:
        raise PolicyNotFound(f"policy {policy_id} does not exist", {"id": policy_id})
    return policy


def _admin_scoped(policy: Policy) -> None:
    if policy.scope in STORY_SCOPES:
        raise ConfigConflict(
            f"policy {policy.id} belongs to a story; edit it through the story's settings",
            {"id": policy.id, "reason": "story_scoped"},
        )


def _live(policy: Policy) -> None:
    if policy.retired_at is not None:
        raise ConfigConflict(
            f"policy {policy.id} is retired", {"id": policy.id, "reason": "retired"}
        )


# -- row writes -------------------------------------------------------------------------------


async def _insert(
    conn: AsyncConnection,
    *,
    policy_id: str,
    scope: Scope,
    scope_id: str | None,
    story_id: str | None,
    content: _Content,
    principal: str,
    idempotency_key: str | None = None,
) -> Policy:
    await conn.execute(
        insert(policies).values(
            id=policy_id,
            scope=scope.value,
            scope_id=scope_id,
            story_id=story_id,
            created_by=principal,
            updated_by=principal,
            idempotency_key=idempotency_key,
            **content.values(),
        )
    )
    await _index(conn, policy_id)
    return await _require(conn, policy_id)


async def _index(conn: AsyncConnection, policy_id: str) -> None:
    """The policy's subject-reference rows (FR-ADMIN-6; #123): its name, message and rule."""
    await index_rows(conn, policies, policies.c.id == policy_id, policy_record)


async def _replace(
    conn: AsyncConnection, current: Policy, content: _Content, principal: str
) -> Policy:
    await conn.execute(
        update(policies)
        .where(policies.c.id == current.id)
        .values(
            version=current.version + 1,
            updated_by=principal,
            updated_at=func.transaction_timestamp(),
            **content.values(),
        )
    )
    await _index(conn, current.id)
    return await _require(conn, current.id)


# -- the operator surface: global and rating policies ----------------------------------------


async def create_policy(
    db: AsyncEngine,
    fields: PolicyFields,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> PolicyWrite:
    """Create a global or rating policy at version 1.

    A repeated ``Idempotency-Key`` returns the policy the first call created and writes
    nothing. A client id that is already taken is 409 (``reason = id_taken``).
    """
    refuse_relaxing(fields.effect)
    scope = Scope(fields.scope)
    _check_rule(scope, fields.rule, field="body.rule")
    content = _Content.given(fields, scope)
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        if idempotency_key is not None:
            replay = (
                await conn.execute(
                    select(policies.c.id).where(policies.c.idempotency_key == idempotency_key)
                )
            ).one_or_none()
            if replay is not None:
                return PolicyWrite(
                    policy=await _require(conn, replay.id),
                    config_revision=state.config_revision,
                    changed=False,
                )
        await _check_scope_id(conn, scope, fields.scope_id)
        policy_id = fields.id or new_ulid()
        if await load_policy(conn, policy_id) is not None:
            raise ConfigConflict(
                f"policy id {policy_id} is taken", {"id": policy_id, "reason": "id_taken"}
            )
        await _check_name(conn, scope, fields.scope_id, content.name, policy_id=policy_id)
        policy = await _insert(
            conn,
            policy_id=policy_id,
            scope=scope,
            scope_id=fields.scope_id,
            story_id=None,
            content=content,
            principal=principal,
            idempotency_key=idempotency_key,
        )
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=policy_setting(policy_id),
            old=None,
            new=content_of(policy),
            actor=principal,
        )
    return PolicyWrite(policy=policy, config_revision=revision, changed=True)


async def _check_scope_id(conn: AsyncConnection, scope: Scope, scope_id: str | None) -> None:
    if scope is Scope.GLOBAL:
        if scope_id is not None:
            raise InvalidRequest(
                "a global policy has no scope_id", {"field": "body.scope_id", "reason": "global"}
            )
        return
    if scope_id is None:
        raise InvalidRequest(
            "a rating policy names its rating in scope_id",
            {"field": "body.scope_id", "reason": "required"},
        )
    found = await conn.scalar(select(content_ratings.c.id).where(content_ratings.c.id == scope_id))
    if found is None:
        raise InvalidRequest(
            f"content rating {scope_id} does not exist",
            {"field": "body.scope_id", "reason": "unknown", "unknown": [scope_id]},
        )


async def update_policy(
    db: AsyncEngine, policy_id: str, fields: PolicyReplacement, *, principal: str
) -> PolicyWrite:
    """Replace a global or rating policy's content (``version`` + 1); identical content writes
    nothing. Retired policies are 409 ``retired``; story policies are 409 ``story_scoped``."""
    refuse_relaxing(fields.effect)
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        current = await _require(conn, policy_id)
        _admin_scoped(current)
        _live(current)
        _check_rule(current.scope, fields.rule, field="body.rule")
        content = _Content.given(fields, current.scope)
        if content == _Content.of(current):
            return PolicyWrite(policy=current, config_revision=state.config_revision, changed=False)
        await _check_name(conn, current.scope, current.scope_id, content.name, policy_id=policy_id)
        policy = await _replace(conn, current, content, principal)
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=policy_setting(policy_id),
            old=content_of(current),
            new=content_of(policy),
            actor=principal,
        )
    return PolicyWrite(policy=policy, config_revision=revision, changed=True)


async def retire_policy(db: AsyncEngine, policy_id: str, *, principal: str) -> PolicyWrite:
    """Retire a global or rating policy: it drops out of every effective set and stays readable.
    Retiring a retired policy changes nothing."""
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        current = await _require(conn, policy_id)
        _admin_scoped(current)
        if current.retired_at is not None:
            return PolicyWrite(policy=current, config_revision=state.config_revision, changed=False)
        await conn.execute(
            update(policies)
            .where(policies.c.id == policy_id)
            .values(
                retired_at=func.transaction_timestamp(),
                retired_by=principal,
                updated_by=principal,
                updated_at=func.transaction_timestamp(),
            )
        )
        policy = await _require(conn, policy_id)
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=policy_setting(policy_id),
            old=content_of(current),
            new=RETIRED,
            actor=principal,
        )
    return PolicyWrite(policy=policy, config_revision=revision, changed=True)


# -- story settings: story and character policies ---------------------------------------------


async def set_story_policies(
    db: AsyncEngine, story_id: str, items: Sequence[StoryPolicyItem], *, principal: str
) -> StoryPoliciesWrite:
    """Upsert the story's own policies (``PATCH /stories/{id}/settings {policies}``, #63).

    The whole list is refused if any item relaxes a policy (``cannot_relax``). A write that
    changes anything moves the story's ``config_revision`` by one.
    """
    for index, item in enumerate(items):
        refuse_relaxing(item.effect, field=f"body.policies[{index}].effect")
    async with db.begin() as conn:
        story = (
            await conn.execute(
                select(stories.c.config_revision, stories.c.status)
                .where(stories.c.id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if story is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        if story.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        await pause.refuse(conn, story_id, story.status)
        revision = int(story.config_revision) + 1
        written: list[Policy] = []
        changes: list[tuple[str, Any, Any]] = []
        for index, item in enumerate(items):
            policy, change = await _upsert_story_item(
                conn, story_id, item, principal=principal, field=f"body.policies[{index}]"
            )
            written.append(policy)
            if change is not None:
                changes.append(change)
        if not changes:
            return StoryPoliciesWrite(
                policies=tuple(written), config_revision=revision - 1, changed=False
            )
        await conn.execute(
            update(stories).where(stories.c.id == story_id).values(config_revision=revision)
        )
        for setting, old, new in changes:
            await record_config_change(
                conn,
                config_revision=revision,
                setting=setting,
                old=old,
                new=new,
                actor=principal,
                scope=ConfigScope.STORY,
                scope_id=story_id,
            )
    return StoryPoliciesWrite(policies=tuple(written), config_revision=revision, changed=True)


async def upsert_story_item(
    conn: AsyncConnection,
    story_id: str,
    item: StoryPolicyItem,
    *,
    principal: str,
    field: str,
) -> tuple[Policy, tuple[str, Any, Any] | None]:
    """Upsert one story or character policy of ``story_id`` inside the caller's transaction
    (``set_story_policies``' item step; #119's genesis writes a scenario's policies as the new
    story's with it, in the genesis transaction): the policy, and the configuration change it
    made (``None`` when it changed nothing)."""
    return await _upsert_story_item(conn, story_id, item, principal=principal, field=field)


async def _upsert_story_item(
    conn: AsyncConnection,
    story_id: str,
    item: StoryPolicyItem,
    *,
    principal: str,
    field: str,
) -> tuple[Policy, tuple[str, Any, Any] | None]:
    scope = Scope(item.scope)
    scope_id = _story_scope_id(story_id, scope, item.scope_id, field=field)
    _check_rule(scope, item.rule, field=f"{field}.rule")
    current = None if item.id is None else await load_policy(conn, item.id)
    if current is not None:
        _check_target(current, story_id, scope, scope_id, field=field)
    if scope is Scope.CHARACTER:
        await _check_character(conn, story_id, scope_id, field=f"{field}.scope_id")
    rule = await _freeze_entity_names(conn, story_id, scope, scope_id, item.rule)
    if rule is not item.rule:
        item = item.model_copy(update={"rule": rule})
    content = _Content.given(item, scope)
    if current is not None:
        _live(current)
        await _check_name(
            conn, scope, scope_id, content.name, policy_id=current.id, field=f"{field}.name"
        )
        if content == _Content.of(current):
            return current, None
        policy = await _replace(conn, current, content, principal)
        return policy, (policy_setting(policy.id), content_of(current), content_of(policy))
    policy_id = item.id or new_ulid()
    await _check_name(
        conn, scope, scope_id, content.name, policy_id=policy_id, field=f"{field}.name"
    )
    policy = await _insert(
        conn,
        policy_id=policy_id,
        scope=scope,
        scope_id=scope_id,
        story_id=story_id,
        content=content,
        principal=principal,
    )
    return policy, (policy_setting(policy_id), None, content_of(policy))


def _story_scope_id(story_id: str, scope: Scope, given: str | None, *, field: str) -> str:
    if scope is Scope.STORY:
        if given is not None and given != story_id:
            raise InvalidRequest(
                "a story policy's scope_id is its story",
                {"field": f"{field}.scope_id", "reason": "story"},
            )
        return story_id
    if given is None:
        raise InvalidRequest(
            "a character policy names its character in scope_id",
            {"field": f"{field}.scope_id", "reason": "required"},
        )
    return given


def _check_target(
    current: Policy, story_id: str, scope: Scope, scope_id: str, *, field: str
) -> None:
    """An item naming an existing policy may only edit this story's policy of the same scope;
    naming a higher-scope policy would relax it (FR-GUARD-8)."""
    if SCOPE_RANK[current.scope] < SCOPE_RANK[scope] or current.scope not in STORY_SCOPES:
        raise CannotRelax(
            f"policy {current.id} belongs to a higher scope; a lower scope cannot change it",
            {"field": f"{field}.id", "reason": "targets_higher_scope", "policy_id": current.id},
        )
    if current.story_id != story_id:
        raise ConfigConflict(
            f"policy id {current.id} is taken", {"id": current.id, "reason": "id_taken"}
        )
    if current.scope is not scope or current.scope_id != scope_id:
        raise InvalidRequest(
            "a policy's scope cannot change",
            {"field": f"{field}.scope", "reason": "scope_immutable"},
        )
