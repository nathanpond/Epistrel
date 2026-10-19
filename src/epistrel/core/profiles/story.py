"""A story's engine profile and its overrides (FR-PROF-1,5,7; FR-API-6; #113).

- **Assign** (``PUT /stories/{id}/engine-profile {profile_id}``): the story runs under another
  profile, at its latest version, from its next turn. The story's overrides are kept and the
  new combination is validated as a whole (422 ``invalid_profile`` naming them, ``details
  .overrides``, when it cannot run). Naming the story's own profile changes nothing (200): the
  story keeps the version it runs on. A profile that is deleted or unknown is 422
  ``profile_deleted`` at ``body.profile_id``, as the impact preview says.
- **Override** (``PATCH /stories/{id}/profile-overrides {settings, expected_revision?}``):
  ``settings`` is a JSON merge patch (RFC 7396) over the story's overrides
  (``effective.patched``): a key present sets that dial (an object merges into an object), and
  ``null`` removes the override, so the dial follows the profile again. A removal must name a
  setting or group the published schema has (422 ``validation_error`` at its field, or
  ``invalid_profile`` for an undelivered one). The overrides that result are checked as a
  settings document (strictly: 422 ``validation_error`` at ``body.settings.<path>``; every name
  delivered: 422 ``invalid_profile``) and the whole configuration as one (422
  ``invalid_profile``); nothing is stored otherwise. They are stored without empty objects,
  which name no setting. ``expected_revision``, when sent, must be the overrides' current
  revision (409 ``revision_conflict``, ``reason: overrides_revision``). A patch that changes no
  setting (one that removes an override the story does not have, or names an empty group)
  writes nothing: no revision moves and no row is recorded. The impact preview applies its
  ``overrides`` the same way (``effective.resolve``), so it answers what this write would do.

Both run through #109's two stages (``impact.two_stage``, which logs each report at INFO, a
call that changes nothing included) with a one-story impact report from the story's
configuration now to the one it would run under: a change the report warns of (a capability
gained or lost, an upgrade unavailable) applies only with ``ignore_warnings``.

**A write** is a story-scoped prospective configuration change (FR-CONC-14,15): under the story
lock it moves the story's ``config_revision`` by one, records one ``config_changes`` row per
changed setting (``engine_profile_id`` for an assignment; ``profile_overrides.<path>`` for each
override set, changed or removed, with that setting's class), the write's ``control`` operation
and a ``config_changed`` event per row (``core.config_changes``). An assignment clears the
story's pin (it follows its new profile's latest version). A turn already running finishes under
the configuration it captured at its start; the next turn runs under the new one. An archived
story's configuration is read-only (409 ``story_archived``), as its settings are (#63), when the
call would change something: a call that changes nothing (the story's own profile, a patch that
changes no setting) answers 200 and writes nothing.

**M3's interim rule.** A change that adds data-producing capability to a story with history is
``upgrade_unavailable`` (``impact``): confirmed, the story keeps its previous configuration, and
nothing is written for it.

**Locks.** A write reads the deployment row ``FOR SHARE`` (the defaults cannot change under it),
then holds the profiles it validates against ``FOR SHARE`` (a profile edit, which holds its
profile ``FOR NO KEY UPDATE``, waits, and so validates the overrides this write stores), then
the story row ``FOR UPDATE``: the order of a profile edit, which then locks its following
stories.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ConfigDict, Field, StrictInt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import ConfigScope, record_config_change
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidProfile, RevisionConflict, StoryArchived, StoryNotFound
from epistrel.core.ids import Ulid
from epistrel.core.profiles import effective, impact, schema
from epistrel.core.profiles.effective import Configuration, EffectiveConfig, StoryProfile
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.impact import ImpactReport, TwoStageRequest, TwoStageResult
from epistrel.core.profiles.schema import PATCH_SCHEMA, ProfileSettings, Purposes, SettingsPatch
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables import deployment_state, engine_profiles, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

ASSIGNMENT_SETTING = "engine_profile_id"
"""The ``config_changes`` setting an assignment records (old and new profile ids)."""
OVERRIDES_SETTING = "profile_overrides"
"""The prefix of the ``config_changes`` settings an overrides write records, one per setting:
``profile_overrides.<dotted path>``."""
REVISION_REASON = "overrides_revision"

OverridesPatchDocument = SettingsPatch
"""A JSON merge patch over a story's overrides (``schema.SettingsPatch``): the delivered
settings schema (``PATCH_SCHEMA``), with ``null`` allowed for every member (it removes that
override)."""

EXAMPLE_PATCH: dict[str, Any] = {
    "memory": {"rolling_summary": {"window_messages": 12}},
    "models": {"generation": {"temperature": None}},
}


class ProfileAssignment(TwoStageRequest):
    """``PUT /stories/{id}/engine-profile``: the profile the story runs under from its next turn
    (its latest version). ``ignore_warnings`` (``ignoreWarnings``) confirms a change the impact
    report warns of (FR-PROF-7)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"profile_id": "00000000000000000000PRF001"}]},
    )

    profile_id: Ulid


class ProfileOverridesPatch(TwoStageRequest):
    """``PATCH /stories/{id}/profile-overrides``: a JSON merge patch (RFC 7396) over the story's
    overrides of its profile's dials (``null`` removes one), and optionally the overrides'
    ``expected_revision``. ``ignore_warnings`` (``ignoreWarnings``) confirms a change the impact
    report warns of (FR-PROF-7)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"settings": EXAMPLE_PATCH, "expected_revision": 1}]},
    )

    settings: OverridesPatchDocument
    expected_revision: StrictInt | None = Field(
        default=None,
        ge=0,
        le=2**31 - 1,
        description="The overrides' revision the patch was made against (0: none yet); another "
        "is 409 `revision_conflict` and nothing is written.",
    )


# -- locks and reads ------------------------------------------------------------------------------


async def _lock(conn: AsyncConnection, story_id: str, *, also: str | None = None) -> Any:
    """The write's locks in order (see the module docstring): the deployment row ``FOR SHARE``,
    the profiles it validates against ``FOR SHARE`` (the story's current one, and ``also``),
    then the story row ``FOR UPDATE``. Returns the story's row."""
    await conn.execute(
        select(deployment_state.c.id)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .with_for_update(read=True)
    )
    current = await conn.scalar(select(stories.c.engine_profile_id).where(stories.c.id == story_id))
    if current is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    held = {str(current)} | ({also} if also is not None else set())
    await _share(conn, held)
    row = (
        await conn.execute(
            select(
                stories.c.engine_profile_id,
                stories.c.status,
                stories.c.config_revision,
                stories.c.timeline_revision,
            )
            .where(stories.c.id == story_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    if str(row.engine_profile_id) not in held:  # reassigned between the read and the lock
        await _share(conn, {str(row.engine_profile_id)})
    return row


async def _share(conn: AsyncConnection, profile_ids: set[str]) -> None:
    await conn.execute(
        select(engine_profiles.c.id)
        .where(engine_profiles.c.id.in_(sorted(profile_ids)))
        .order_by(engine_profiles.c.id)
        .with_for_update(read=True)
    )


def _refuse_archived(row: Any, story_id: str) -> None:
    if row.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )


async def _report(
    conn: AsyncConnection,
    story_id: str,
    before: Configuration,
    after: Configuration,
    facts: RuntimeFacts,
) -> tuple[ImpactReport, bool]:
    """The one-story report of the change, and whether its upgrade is unavailable."""
    change = impact.delta(before, after, facts)
    history = await impact.with_history(conn, [story_id]) if change.increases else set()
    sizes = await impact.transcripts(conn, history)
    entry = impact.affected(
        story_id, change, history=story_id in history, transcript=sizes.get(story_id, (0, 0))
    )
    return impact.single_report(entry, change), entry.upgrade_unavailable


async def _bump(conn: AsyncConnection, story_id: str, revision: int, **values: Any) -> None:
    await conn.execute(
        update(stories).where(stories.c.id == story_id).values(config_revision=revision, **values)
    )


async def _staged(
    conn: AsyncConnection,
    story_id: str,
    row: Any,
    change: tuple[Configuration, Configuration],
    *,
    facts: RuntimeFacts,
    ignore_warnings: bool,
    kept: StoryProfile,
    write: Callable[[], Awaitable[EffectiveConfig]] | None = None,
) -> TwoStageResult[EffectiveConfig]:
    """The write through #109's two stages (``impact.two_stage``, which logs the report): the
    one-story report of ``change`` (the configuration now, and the one the story would run
    under), then, unless it warns and the request did not set ``ignore_warnings``, ``write``.
    No ``write`` (a call that changes nothing), or an upgrade the report finds unavailable
    (M3's interim rule), answers ``kept``, the configuration as it is (viewed only then), and
    writes nothing. The
    result carries the story's revisions after the call."""
    before, after = change
    unavailable = False

    async def report() -> ImpactReport:
        nonlocal unavailable
        found, unavailable = await _report(conn, story_id, before, after, facts)
        return found

    async def apply(_: ImpactReport) -> EffectiveConfig:
        if write is None or unavailable:
            return effective.view(kept, facts)
        return await write()

    outcome = await impact.two_stage(report, apply, ignore_warnings=ignore_warnings)
    revision = await conn.scalar(select(stories.c.config_revision).where(stories.c.id == story_id))
    return dataclasses.replace(
        outcome,
        config_revision=int(revision if revision is not None else row.config_revision),
        timeline_revision=int(row.timeline_revision),
    )


# -- assign ---------------------------------------------------------------------------------------


async def assign_profile(
    db: AsyncEngine,
    story_id: str,
    request: ProfileAssignment,
    *,
    principal: str,
    facts: RuntimeFacts,
    purposes: Purposes | None = None,
) -> TwoStageResult[EffectiveConfig]:
    """Assign ``request.profile_id`` to the story (see the module docstring)."""
    async with db.begin() as conn:
        row = await _lock(conn, story_id, also=request.profile_id)
        current = await effective.effective_configuration(conn, story_id, validate=False)
        if request.profile_id == current.profile_id:  # nothing to change: nothing is written
            return await _staged(
                conn,
                story_id,
                row,
                (current.configuration(), current.configuration()),
                facts=facts,
                ignore_warnings=request.ignore_warnings,
                kept=current,
            )
        target = await effective.assignable(conn, request.profile_id)
        try:
            settings = effective.resolve_layers(
                current.defaults,
                target.document,
                current.overrides,
                purposes=purposes,
                field=("body", "profile_id"),
            )
        except InvalidProfile as exc:
            exc.details["overrides"] = dict(current.overrides)
            raise
        _refuse_archived(row, story_id)
        moved = current.with_profile(target.profile_id, target.version, target.document, settings)

        async def write() -> EffectiveConfig:
            revision = int(row.config_revision) + 1
            await _bump(
                conn,
                story_id,
                revision,
                engine_profile_id=target.profile_id,
                engine_profile_version_pin=None,
            )
            await record_config_change(
                conn,
                config_revision=revision,
                setting=ASSIGNMENT_SETTING,
                old=current.profile_id,
                new=target.profile_id,
                actor=principal,
                change_class=ConfigClass.prospective,
                scope=ConfigScope.STORY,
                scope_id=story_id,
            )
            return effective.view(await effective.effective_configuration(conn, story_id), facts)

        return await _staged(
            conn,
            story_id,
            row,
            (current.configuration(), moved.configuration()),
            facts=facts,
            ignore_warnings=request.ignore_warnings,
            kept=current,
            write=write,
        )


# -- override -------------------------------------------------------------------------------------


def leaf_values(document: Mapping[str, Any]) -> dict[str, Any]:
    """Every setting a settings document names, by dotted path, at its value."""
    return {path: value for path, value in schema.walk(document) if path in schema.LEAF_PATHS}


def changed_settings(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """The settings two overrides documents name differently (set, changed or removed), by
    dotted path in order: what a write records, one row each. An empty list means the patch
    changes nothing a turn reads, whatever empty groups either document holds, and nothing is
    written."""
    before, after = leaf_values(old), leaf_values(new)
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _change_class(path: str) -> ConfigClass:
    return schema.setting_tags()[path].change_class


async def set_overrides(
    db: AsyncEngine,
    story_id: str,
    request: ProfileOverridesPatch,
    *,
    principal: str,
    facts: RuntimeFacts,
    purposes: Purposes | None = None,
) -> TwoStageResult[EffectiveConfig]:
    """Apply ``request.settings`` to the story's overrides (see the module docstring)."""
    field = ("body", "settings")
    async with db.begin() as conn:
        row = await _lock(conn, story_id)
        current = await effective.effective_configuration(conn, story_id, validate=False)
        expected = request.expected_revision
        if expected is not None and expected != current.overrides_revision:
            raise RevisionConflict(
                f"expected overrides revision {expected}, the story's are at "
                f"{current.overrides_revision}",
                {
                    "current_revision": current.overrides_revision,
                    "attempts": 0,
                    "reason": REVISION_REASON,
                    "field": "body.expected_revision",
                },
            )
        wanted = effective.patched(
            current.overrides, request.settings, purposes=purposes, field=field
        )
        settings: ProfileSettings = effective.resolve_layers(
            current.defaults, current.document, wanted, purposes=purposes, field=field
        )
        before = current.configuration()
        changed = changed_settings(current.overrides, wanted)
        if not changed:  # no setting set, changed or removed: nothing is written
            return await _staged(
                conn,
                story_id,
                row,
                (before, before),
                facts=facts,
                ignore_warnings=request.ignore_warnings,
                kept=current,
            )
        _refuse_archived(row, story_id)

        async def write() -> EffectiveConfig:
            revision = int(row.config_revision) + 1
            await effective.store_overrides(conn, story_id, wanted, current.overrides_revision + 1)
            await _bump(conn, story_id, revision)
            old, new = leaf_values(current.overrides), leaf_values(wanted)
            for path in changed:
                await record_config_change(
                    conn,
                    config_revision=revision,
                    setting=f"{OVERRIDES_SETTING}.{path}",
                    old=old.get(path),
                    new=new.get(path),
                    actor=principal,
                    change_class=_change_class(path),
                    scope=ConfigScope.STORY,
                    scope_id=story_id,
                )
            return effective.view(await effective.effective_configuration(conn, story_id), facts)

        return await _staged(
            conn,
            story_id,
            row,
            (before, Configuration(current.profile_id, current.version, settings)),
            facts=facts,
            ignore_warnings=request.ignore_warnings,
            kept=current,
            write=write,
        )


__all__ = [
    "ASSIGNMENT_SETTING",
    "OVERRIDES_SETTING",
    "PATCH_SCHEMA",
    "OverridesPatchDocument",
    "ProfileAssignment",
    "ProfileOverridesPatch",
    "assign_profile",
    "changed_settings",
    "leaf_values",
    "set_overrides",
]
