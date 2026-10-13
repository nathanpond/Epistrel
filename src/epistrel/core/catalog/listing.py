"""Listing status: a consumer-defined publishing-workflow value on every catalog character and
scenario (FR-CAT-11; #110).

The deployment configures the values it allows, in order (``deployment_state
.listing_status_values``, default ``private``, ``in_review``, ``public``; ``PUT
/admin/settings``), and a new entry starts at the first. An entry's value changes only through
``set_listing_status`` (``CatalogService.set_listing_status``), to one of the configured values
(422 ``invalid_listing_status`` otherwise), and every change is recorded in ``ownership_events``
(kind ``listing_status``, ``old``/``new`` the bare values) with the acting principal and the
transaction time. A companion scenario always holds its character's value: it changes with the
character's, and never on its own (422 ``companion_follows_character``).

**Epistrel never interprets the value** (FR-AUTH-8): no read, story creation or transition
between values consults it. It is a label for the consumer's own workflow and a list filter
(``core.catalog.search``) that narrows a list only when a request names it.

**Values and races.** A write that sets a value, and a creation that takes the first value,
read the configured values with the deployment row held ``FOR SHARE``. An operator's write of
``listing_status_values`` takes the same row ``FOR UPDATE`` (``core.deployment.apply_settings``)
and refuses to remove a value any catalog row still holds (409 ``listing_status_in_use``,
``holders``), counting every row: both kinds, every status, companions included. So a value is
never removed while an entry holds it, or while a write that sets it is in flight.

**Hooks.** ``on_listing_status_change`` registers a callback that every change runs in its
transaction, once for each entry whose value it changes (the entry, then each companion that
follows it): ``hook(conn, entity_type, entry_id, old, new, principal)``. None is registered by
default.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.errors import InvalidListingStatus, ListingStatusInUse
from epistrel.core.settings_schema import DEFAULT_LISTING_STATUS_VALUES, ListingStatusValue
from epistrel.core.tables import deployment_state
from epistrel.core.tables.catalog import character_catalog, scenario_catalog
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import EntityType

SETTING = "listing_status_values"
"""The deployment setting (and ``deployment_state`` column) that lists the allowed values."""

CATALOG_TABLES = (character_catalog, scenario_catalog)
"""Every table whose rows hold a listing status."""


class ListingStatusRequest(BaseModel):
    """``PUT /catalog/{characters|scenarios}/{id}/listing-status``: the entry's new listing
    status, one of the deployment's ``listing_status_values`` (FR-CAT-11). Surrounding spaces
    and tabs are trimmed; values are case-sensitive. Epistrel never interprets it."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"value": "public"}]})

    value: ListingStatusValue = Field(
        description="One of the deployment's `listing_status_values` (default `private`, "
        "`in_review`, `public`): 1 to 40 characters, case-sensitive."
    )


@dataclass(frozen=True, slots=True)
class ListingStatusChange:
    """An entry's listing status after a write, and before it (equal, with ``changed`` false,
    when the request named the current value: nothing was recorded then). The deployment's
    ``config_revision``, which the write leaves as it is."""

    entity_type: EntityType
    id: str
    listing_status: str
    previous: str
    changed: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- the configured values ---------------------------------------------------------------------


async def allowed_values(conn: AsyncConnection, *, lock: bool = False) -> list[str]:
    """The deployment's listing status values, in order; with ``lock``, the deployment row is
    held ``FOR SHARE`` until ``conn``'s transaction ends, so no write removes one meanwhile."""
    query = select(deployment_state.c.listing_status_values).where(
        deployment_state.c.id == DEPLOYMENT_ROW_ID
    )
    if lock:
        query = query.with_for_update(read=True)
    found = await conn.scalar(query)
    return list(found) if found is not None else list(DEFAULT_LISTING_STATUS_VALUES)


def refuse_unknown(value: str, allowed: Sequence[str], *, field: str) -> None:
    """422 ``invalid_listing_status`` unless ``value`` is one of ``allowed``."""
    if value not in allowed:
        raise InvalidListingStatus(
            f"{value!r} is not one of the deployment's listing status values",
            {"value": value, "allowed_values": list(allowed), "field": field},
        )


async def require_allowed(db: AsyncEngine, value: str, *, field: str = "body.value") -> None:
    """422 ``invalid_listing_status`` unless ``value`` is one of the deployment's values now:
    the request's own check, made before its idempotency key is claimed (the write checks
    again under the lock)."""
    async with db.connect() as conn:
        allowed = await allowed_values(conn)
    refuse_unknown(value, allowed, field=field)


async def holders(conn: AsyncConnection, values: Sequence[str]) -> dict[str, int]:
    """How many catalog rows hold each of ``values`` (both kinds, every status, companions
    included); a value no row holds is left out."""
    if not values:
        return {}
    held = union_all(
        *(
            select(table.c.listing_status.label("value")).where(
                table.c.listing_status.in_(list(values))
            )
            for table in CATALOG_TABLES
        )
    ).subquery()
    rows = await conn.execute(select(held.c.value, func.count()).group_by(held.c.value))
    return {str(row[0]): int(row[1]) for row in rows}


async def refuse_removal(
    conn: AsyncConnection, before: Sequence[str], after: Sequence[str]
) -> None:
    """409 ``listing_status_in_use`` when ``after`` drops a value of ``before`` that a catalog
    row still holds (the first such value, in ``before``'s order). The caller holds the
    deployment row ``FOR UPDATE``."""
    removed = [value for value in before if value not in after]
    counts = await holders(conn, removed)
    for value in removed:
        if counts.get(value):
            raise ListingStatusInUse(
                f"catalog entries still hold the listing status {value!r}; move them to "
                "another value before removing it",
                {"value": value, "count": counts[value], "field": f"body.{SETTING}"},
            )


# -- hooks ----------------------------------------------------------------------------------------

ListingStatusHook = Callable[[AsyncConnection, EntityType, str, str, str, str], Awaitable[None]]
"""``hook(conn, entity_type, entry_id, old, new, principal)``, run in the change's
transaction."""

_HOOKS: list[ListingStatusHook] = []


def on_listing_status_change(hook: ListingStatusHook) -> ListingStatusHook:
    """Register ``hook`` for every listing status change (see the module docstring)."""
    if hook not in _HOOKS:
        _HOOKS.append(hook)
    return hook


def remove_listing_status_hook(hook: ListingStatusHook) -> None:
    if hook in _HOOKS:
        _HOOKS.remove(hook)


async def run_hooks(
    conn: AsyncConnection,
    entity_type: EntityType,
    entry_id: str,
    old: str,
    new: str,
    principal: str,
) -> None:
    for hook in list(_HOOKS):
        await hook(conn, entity_type, entry_id, old, new, principal)


__all__ = [
    "CATALOG_TABLES",
    "SETTING",
    "ListingStatusChange",
    "ListingStatusHook",
    "ListingStatusRequest",
    "allowed_values",
    "holders",
    "on_listing_status_change",
    "refuse_removal",
    "refuse_unknown",
    "remove_listing_status_hook",
    "require_allowed",
    "run_hooks",
]
