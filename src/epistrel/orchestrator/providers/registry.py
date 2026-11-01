"""The provider registry: ``/admin/external-providers`` and resolving a provider for a call
(FR-EXT-1,6; #204).

**Writes.** ``register``, ``replace`` and ``delete`` each take the deployment lock
(``lock_deployment``), move ``config_revision`` by one when something changes and record it
through ``record_config_change`` as deployment-scoped *interpretive* rows (a provider's
ownership, write capability and confirmation mode change how work is verified and committed;
FR-CONC-15): ``external_provider:<name>`` for a registration (old ``null``) and a deletion (new
``null``), and ``external_provider:<name>.<field>`` for each field a ``PUT`` changes. The
manifest is validated against the deployment's ``provider_allowed_hosts`` as it stands in the
write's transaction. A ``PUT`` that changes nothing writes nothing. Registering a name already
registered is 409 ``provider_exists``; replacing or deleting an unknown one is 404; deleting one
a story's State names (``state_definitions.provider_id``, any membership) is 409
``provider_in_use`` listing the stories.

**Runtime updates** (FR-EXT-6, docs/02 §4.8). Nothing is cached: ``provider_for`` reads the row
(and the allowed hosts) when it is called, so a change takes effect at the next operation that
resolves the provider, with no restart. An operation that resolved its provider before the
change keeps the object it resolved and completes under it; #210's dispatcher resolves again
when it claims a row.

Responses carry the manifest as registered: template text, with secret *references* only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import providers as core_providers
from epistrel.core.config_changes import (
    bump_config_revision,
    lock_deployment,
    read_section,
    read_settings,
    record_config_change,
)
from epistrel.core.errors import ProviderExists, ProviderInUse, ProviderNotFound
from epistrel.core.providers import ProviderRow
from epistrel.core.settings_schema import ConfigClass, ProviderSettings
from epistrel.core.tables.providers import external_providers
from epistrel.orchestrator.providers.http_manifest import HttpManifestProvider
from epistrel.orchestrator.providers.in_process import InProcessProvider, InProcessRegistry
from epistrel.orchestrator.providers.manifest import (
    SPEC_FIELDS,
    ProviderRegistration,
    ProviderSpec,
    validate_spec,
)
from epistrel.orchestrator.providers.port import ExternalProvider

SETTING_FAMILY = core_providers.PROVIDER_FAMILY
IN_USE_LISTED_MAX = 100
"""The most story ids a ``provider_in_use`` refusal lists."""


def setting(name: str, field: str | None = None) -> str:
    """The ``config_changes.setting`` of a provider's change: ``external_provider:<name>`` or
    ``external_provider:<name>.<field>``."""
    return f"{SETTING_FAMILY}:{name}" if field is None else f"{SETTING_FAMILY}:{name}.{field}"


@dataclass(frozen=True, slots=True)
class ProviderWrite:
    """A registry write: the provider as it now stands (``None`` once deleted), the deployment's
    ``config_revision`` after it, and whether anything changed."""

    provider: ProviderRow | None
    config_revision: int
    changed: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def allowed_hosts(conn: AsyncConnection | AsyncEngine) -> list[str]:
    """The deployment's ``provider_allowed_hosts`` as it stands."""
    return list((await read_section(conn, ProviderSettings)).provider_allowed_hosts)


def _document(row: ProviderRow) -> dict[str, Any]:
    spec = ProviderSpec.model_validate(
        {
            "trust_tier": row.trust_tier,
            "transport": row.transport,
            "manifest": row.manifest,
            "write_capable": row.write_capable,
            "confirmation": row.confirmation,
            "dedupes_dispatch": row.dedupes_dispatch,
            "returns_resulting_values": row.returns_resulting_values,
            "unavailability": row.unavailability,
            "freshness_minutes": row.freshness_minutes,
            "confirm_wait_max_seconds": row.confirm_wait_max_seconds,
        }
    )
    return spec.model_dump(mode="json")


async def register(
    db: AsyncEngine, registration: ProviderRegistration, *, principal: str
) -> ProviderWrite:
    """Register a new provider (see the module docstring)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        validate_spec(registration, await allowed_hosts(conn))
        if await core_providers.find_provider(conn, registration.name) is not None:
            raise ProviderExists(
                f"a provider is already registered as {registration.name!r}",
                {"provider": registration.name},
            )
        revision = await bump_config_revision(conn, current)
        await conn.execute(
            insert(external_providers).values(
                name=registration.name, config_revision=revision, **registration.columns()
            )
        )
        stored = await core_providers.get_provider(conn, registration.name)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting(registration.name),
            old=None,
            new=_document(stored),
            actor=principal,
            change_class=ConfigClass.interpretive,
        )
        return ProviderWrite(stored, revision)


async def replace(
    db: AsyncEngine, name: str, spec: ProviderSpec, *, principal: str
) -> ProviderWrite:
    """Replace a provider's registration (see the module docstring)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        found = await core_providers.find_provider(conn, name, lock=True)
        if found is None:
            raise ProviderNotFound(f"no provider is registered as {name!r}", {"provider": name})
        validate_spec(spec, await allowed_hosts(conn))
        before = _document(found)
        after = spec.model_dump(mode="json")
        changed = [field for field in SPEC_FIELDS if before.get(field) != after.get(field)]
        if not changed:
            return ProviderWrite(found, current.config_revision, changed=False)
        revision = await bump_config_revision(conn, current)
        await conn.execute(
            update(external_providers)
            .where(external_providers.c.name == name)
            .values(config_revision=revision, updated_at=func.clock_timestamp(), **spec.columns())
        )
        for field in changed:
            await record_config_change(
                conn,
                config_revision=revision,
                setting=setting(name, field),
                old=before.get(field),
                new=after.get(field),
                actor=principal,
                change_class=ConfigClass.interpretive,
            )
        return ProviderWrite(await core_providers.get_provider(conn, name), revision)


async def delete(db: AsyncEngine, name: str, *, principal: str) -> ProviderWrite:
    """Delete a provider no story's State names (see the module docstring)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        found = await core_providers.find_provider(conn, name, lock=True)
        if found is None:
            raise ProviderNotFound(f"no provider is registered as {name!r}", {"provider": name})
        using = await core_providers.stories_naming(conn, name)
        if using:
            raise ProviderInUse(
                f"stories' States name provider {name!r}; it cannot be deleted",
                {"provider": name, "stories": using[:IN_USE_LISTED_MAX]},
            )
        revision = await bump_config_revision(conn, current)
        await conn.execute(sql_delete(external_providers).where(external_providers.c.name == name))
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting(name),
            old=_document(found),
            new=None,
            actor=principal,
            change_class=ConfigClass.interpretive,
        )
        return ProviderWrite(None, revision)


async def listing(db: AsyncEngine) -> tuple[int, list[ProviderRow]]:
    """The deployment's ``config_revision`` and every registered provider, by name."""
    async with db.connect() as conn:
        revision = (await read_settings(conn)).config_revision
        return revision, await core_providers.list_providers(conn)


async def entry(db: AsyncEngine, name: str) -> tuple[int, ProviderRow]:
    """The deployment's ``config_revision`` and one provider (404 when unknown)."""
    async with db.connect() as conn:
        revision = (await read_settings(conn)).config_revision
        return revision, await core_providers.get_provider(conn, name)


async def provider_for(
    conn: AsyncConnection,
    name: str,
    *,
    in_process: InProcessRegistry,
    transport: httpx2.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExternalProvider:
    """The adapter for provider ``name`` as registered now (404 when unknown): an
    ``HttpManifestProvider`` with the allowed hosts read now, or an ``InProcessProvider`` over
    ``in_process``. ``transport`` and ``environ`` reach the HTTP adapter (tests)."""
    row = await core_providers.get_provider(conn, name)
    if row.transport == "in_process":
        return InProcessProvider(row, in_process)
    return HttpManifestProvider(
        row, await allowed_hosts(conn), transport=transport, environ=environ
    )


__all__ = [
    "IN_USE_LISTED_MAX",
    "SETTING_FAMILY",
    "ProviderWrite",
    "allowed_hosts",
    "delete",
    "entry",
    "listing",
    "provider_for",
    "register",
    "replace",
    "setting",
]
