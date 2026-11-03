"""Each story's bindings to its providers (FR-EXT-1; #204).

- ``bind_story`` stores a story's external ids at one provider under the story lock (the
  rules are ``core.providers.bind``'s: 404 for an unregistered provider, 422
  ``binding_incomplete`` for ids that leave a ``{binding.<key>}`` uncovered). Binding the same
  ids again changes nothing but ``bound_at``. An archived story is 409 ``story_archived``. A
  story created before ``required_providers`` was delivered kept it under
  ``settings.inactive`` (#122): its first binding moves it to the live setting, recorded as the
  story-scoped interpretive configuration change ``required_providers`` (one new
  ``config_revision``). No binding moves the timeline revision. Binding a ``synchronous``
  write-capable provider (#214) answers the profile validator's ``below_safety_minimum``
  warning (``warnings``) when the story's effective call cap is below a turn's minimum
  counting the outcome step its turns may now need (O, ``core.profiles.reservation``); the
  binding is stored either way.
- ``require_bound`` is the turn-start guard: a turn of a story that needs a provider (an
  active externally owned State names it, or its live ``required_providers`` lists it) with no
  binding to it fails closed with 422 ``provider_unbound`` before anything is recorded (422
  ``provider_unregistered`` when this deployment has not registered it at all, as an imported
  story's may not have: #251). #205's
  read path calls it too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import providers as core_providers
from epistrel.core.config_changes import ConfigScope, record_config_change
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import ProviderNotFound, ProviderUnbound, StoryArchived, StoryNotFound
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.reservation import SafetyFacts
from epistrel.core.providers import REQUIRED_PROVIDERS, SYNCHRONOUS, ProviderBinding
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables.stories import stories


@dataclass(frozen=True, slots=True)
class BindingWrite:
    """A binding as stored, the story's ``timeline_revision`` (unchanged) and the live
    ``required_providers`` the binding activated (``None``: none was inactive)."""

    binding: ProviderBinding
    timeline_revision: int
    activated: list[str] | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""
    warnings: list[dict[str, Any]] = field(default_factory=list)
    """#214: ``below_safety_minimum`` when the provider confirms ``synchronous`` and the
    story's effective call cap is below a turn's minimum with its outcome step (O)."""


async def bind_story(
    db: AsyncEngine,
    story_id: str,
    name: str,
    request: core_providers.BindingRequest,
    *,
    principal: str,
) -> BindingWrite:
    """Bind the story to provider ``name`` (see the module docstring)."""
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(
                    stories.c.settings,
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
        if row.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        binding = await core_providers.bind(conn, story_id, name, request.external_ids)
        activated = await core_providers.activate_required_providers(conn, story_id, row.settings)
        if activated is not None:
            revision = int(row.config_revision) + 1
            await conn.execute(
                update(stories).where(stories.c.id == story_id).values(config_revision=revision)
            )
            await record_config_change(
                conn,
                config_revision=revision,
                setting=REQUIRED_PROVIDERS,
                old=None,
                new=activated,
                actor=principal,
                change_class=ConfigClass.interpretive,
                scope=ConfigScope.STORY,
                scope_id=story_id,
            )
        written = BindingWrite(binding, int(row.timeline_revision), activated)
    written.warnings[:] = await _safety_warnings(db, story_id, name)
    return written


async def _safety_warnings(db: AsyncEngine, story_id: str, name: str) -> list[dict[str, Any]]:
    """#214 (owner decision 13): binding a synchronous write-capable provider is a story-aware
    surface of the safety minimum, so it warns ``below_safety_minimum`` with O counted (G the
    story's own model-assisted policies, as its turns plan it)."""
    from epistrel.orchestrator.policies.store import story_guardrails

    async with db.connect() as conn:
        provider = await core_providers.find_provider(conn, name)
        if provider is None or not provider.write_capable or provider.confirmation != SYNCHRONOUS:
            return []
        configuration = await effective_configuration(conn, story_id, validate=False)
        synchronous = await core_providers.synchronous_bound(conn, story_id)
    guardrails = await story_guardrails(db, story_id)
    facts = SafetyFacts(
        guardrail_input=guardrails.calls_input,
        guardrail_output=guardrails.calls_output,
        synchronous=synchronous,
    )
    return facts.warnings(configuration.settings)


async def story_bindings(db: AsyncEngine, story_id: str) -> list[ProviderBinding]:
    """The story's bindings, by provider (404 for an unknown story)."""
    async with db.connect() as conn:
        known = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
        if known is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        return await core_providers.bindings(conn, story_id)


async def story_binding(db: AsyncEngine, story_id: str, name: str) -> ProviderBinding:
    """The story's binding to provider ``name`` (404 for an unknown story or no binding)."""
    found = None
    for binding in await story_bindings(db, story_id):
        if binding.provider == name:
            found = binding
    if found is None:
        raise ProviderNotFound(
            f"the story has no binding to provider {name!r}",
            {"story_id": story_id, "provider": name},
        )
    return found


async def require_bound(db: AsyncEngine | AsyncConnection, story_id: str) -> None:
    """422 ``provider_unregistered`` when a provider the story needs is not registered on this
    deployment (an imported story's, FR-EXT-19, #251: its bindings came with the bundle), else
    422 ``provider_unbound`` when one has no binding (see the module docstring). An unknown
    story passes: the caller's own checks answer 404."""

    async def missing_of(conn: AsyncConnection) -> list[str]:
        await core_providers.require_registered(conn, await core_providers.needed(conn, story_id))
        return await core_providers.unbound(conn, story_id)

    if isinstance(db, AsyncConnection):
        missing = await missing_of(db)
    else:
        async with db.connect() as conn:
            missing = await missing_of(conn)
    if missing:
        raise ProviderUnbound(
            "the story has no binding to providers it needs: " + ", ".join(missing),
            {"story_id": story_id, "providers": missing},
        )


__all__ = ["BindingWrite", "bind_story", "require_bound", "story_binding", "story_bindings"]
