"""What Engine Core knows of external-data providers (FR-EXT-1, FR-STATE-2; #204).

The registry itself (manifest validation, template execution, the adapters) is the
Orchestrator's (``epistrel.orchestrator.providers``). Core reads the stored rows: whether a
provider is registered and which values its manifest owns (``core.states.define_state``
checks an externally owned State against them), the template placeholder grammar (a story's
binding must cover every ``{binding.<key>}`` its provider's manifest uses), each story's
bindings (``story_provider_bindings``), and which providers a story needs bound before a turn
(``unbound``): every provider an externally owned State of the story names, and every
provider its live ``required_providers`` setting lists.

**Placeholders.** A template string may hold ``{binding.<key>}``, ``{story.id}``,
``{value.scope_key}``, ``{effect.delta}``, ``{effect.value}``, ``{effect.idempotency_key}`` and
``{secret.<NAME>}``. Anything else between braces is an unknown placeholder (the manifest is
refused). ``effect.*`` belongs to write templates only.

**Bindings.** ``bind`` stores a story's ``external_ids`` at one provider (replacing any it had:
idempotent), refusing a provider that is not registered (404) and ids that leave a
``{binding.<key>}`` of its manifest uncovered (422 ``binding_incomplete``). A story created
before ``required_providers`` was delivered keeps the setting under ``settings.inactive``
(#122); its first binding moves it to the live setting (``activate_required_providers``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import StateOwner
from epistrel.core.errors import (
    BindingIncomplete,
    InvalidRequest,
    ProviderNotFound,
    ProviderUnregistered,
)
from epistrel.core.membership import active_only
from epistrel.core.tables.providers import external_providers, story_provider_bindings
from epistrel.core.tables.states import state_definitions
from epistrel.core.tables.stories import stories

PROVIDER_NAME_PATTERN: Final = r"^[a-z][a-z0-9_.-]{0,63}$"
"""A provider's name (the catalog's ``ProviderName``, which ``required_providers`` uses)."""

BINDING_KEY_PATTERN: Final = r"^[A-Za-z0-9_]{1,64}$"
SECRET_NAME_PATTERN: Final = r"^[A-Z0-9_]{1,64}$"  # noqa: S105 - a name pattern
EXTERNAL_ID_MAX: Final = 256
EXTERNAL_IDS_MAX: Final = 32

BRACES: Final = re.compile(r"\{([^{}]*)\}")
"""A placeholder candidate: anything between one pair of braces."""

READ_PLACEHOLDERS: Final = frozenset({"story.id", "value.scope_key"})
WRITE_PLACEHOLDERS: Final = READ_PLACEHOLDERS | frozenset(
    {"effect.delta", "effect.value", "effect.idempotency_key"}
)
PREFIXED: Final = {"binding": BINDING_KEY_PATTERN, "secret": SECRET_NAME_PATTERN}
"""Placeholders with a name of their own: ``{binding.<key>}`` and ``{secret.<NAME>}``."""

REQUIRED_PROVIDERS: Final = "required_providers"
PROVIDER_FAMILY: Final = "external_provider"
"""The ``config_changes`` setting family of a provider's registration (#204)."""
INACTIVE: Final = "inactive"


@dataclass(frozen=True, slots=True)
class Placeholder:
    """One placeholder: its full text (``binding.player``), its kind (``binding``, ``story``,
    ``value``, ``effect`` or ``secret``) and its name (``player``, ``id``, …)."""

    text: str
    kind: str
    name: str


def placeholder(text: str, *, write: bool = False) -> Placeholder | None:
    """``text`` (the inside of a pair of braces) as a placeholder a read template (``write``:
    a write template) may hold; ``None`` when it is not one."""
    kind, dot, name = text.partition(".")
    if not dot:
        return None
    if kind in PREFIXED:
        return Placeholder(text, kind, name) if re.match(PREFIXED[kind], name) else None
    allowed = WRITE_PLACEHOLDERS if write else READ_PLACEHOLDERS
    return Placeholder(text, kind, name) if text in allowed else None


def placeholders_in(text: str) -> Iterator[str]:
    """The inside of every pair of braces in ``text``, in order."""
    for match in BRACES.finditer(text):
        yield match.group(1)


def strings_in(value: Any) -> Iterator[str]:
    """Every string inside a JSON value (object keys excluded), in document order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings_in(item)


def binding_keys(manifest: Mapping[str, Any]) -> frozenset[str]:
    """Every ``{binding.<key>}`` a stored manifest uses: the keys a story's binding must give."""
    found: set[str] = set()
    for text in strings_in(manifest):
        for inner in placeholders_in(text):
            kind, dot, name = inner.partition(".")
            if kind == "binding" and dot:
                found.add(name)
    return frozenset(found)


# -- the registered rows ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderRow:
    """A registered provider as stored (see ``core.tables.providers``)."""

    name: str
    trust_tier: str
    transport: str
    manifest: dict[str, Any]
    write_capable: bool
    confirmation: str | None
    dedupes_dispatch: bool | None
    returns_resulting_values: bool | None
    unavailability: str
    freshness_minutes: int | None
    confirm_wait_max_seconds: int
    config_revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: Any) -> ProviderRow:
        return cls(
            name=str(row.name),
            trust_tier=str(row.trust_tier),
            transport=str(row.transport),
            manifest=dict(row.manifest),
            write_capable=bool(row.write_capable),
            confirmation=row.confirmation,
            dedupes_dispatch=row.dedupes_dispatch,
            returns_resulting_values=row.returns_resulting_values,
            unavailability=str(row.unavailability),
            freshness_minutes=row.freshness_minutes,
            confirm_wait_max_seconds=int(row.confirm_wait_max_seconds),
            config_revision=int(row.config_revision),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def values(self) -> dict[str, dict[str, Any]]:
        """The manifest's tracked values, by name."""
        found = self.manifest.get("values")
        return {str(k): dict(v) for k, v in found.items()} if isinstance(found, Mapping) else {}


async def find_provider(
    conn: AsyncConnection, name: str, *, lock: bool = False
) -> ProviderRow | None:
    """The provider registered as ``name`` (``lock``: its row ``FOR UPDATE``), else ``None``."""
    query = select(external_providers).where(external_providers.c.name == name)
    if lock:
        query = query.with_for_update()
    found = (await conn.execute(query)).one_or_none()
    return None if found is None else ProviderRow.of(found)


async def get_provider(conn: AsyncConnection, name: str) -> ProviderRow:
    """The provider registered as ``name``; 404 ``not_found`` otherwise."""
    found = await find_provider(conn, name)
    if found is None:
        raise ProviderNotFound(f"no provider is registered as {name!r}", {"provider": name})
    return found


async def list_providers(conn: AsyncConnection) -> list[ProviderRow]:
    """Every registered provider, by name."""
    found = await conn.execute(select(external_providers).order_by(external_providers.c.name))
    return [ProviderRow.of(row) for row in found]


async def require_registered(conn: AsyncConnection, names: Sequence[str]) -> None:
    """422 ``provider_unregistered`` naming every one of ``names`` not registered."""
    if not names:
        return
    known = set(
        await conn.scalars(
            select(external_providers.c.name).where(external_providers.c.name.in_(list(names)))
        )
    )
    missing = sorted(set(names) - known)
    if missing:
        raise ProviderUnregistered(
            "the story requires providers this deployment has not registered: "
            + ", ".join(missing),
            {"providers": missing},
        )


async def stories_naming(conn: AsyncConnection, name: str) -> list[str]:
    """The stories with a State (on any membership) that names provider ``name``, by id."""
    d = state_definitions.c
    found = await conn.scalars(
        select(d.story_id).where(d.provider_id == name).distinct().order_by(d.story_id)
    )
    return [str(story_id) for story_id in found]


async def check_external_value(
    conn: AsyncConnection, provider: str, value: str, state_type: str
) -> None:
    """422 ``validation_error`` unless ``provider`` is registered and its manifest owns
    ``value`` with type ``state_type`` (``core.states.define_state``; FR-STATE-2)."""
    found = await find_provider(conn, provider)
    if found is None:
        raise InvalidRequest(
            f"no provider is registered as {provider!r}",
            {"field": "provider_id", "reason": "unknown_provider"},
        )
    owned = found.values().get(value)
    if owned is None:
        raise InvalidRequest(
            f"provider {provider!r} owns no value {value!r}",
            {"field": "external_value_name", "reason": "unknown_value"},
        )
    if owned.get("type") != state_type:
        raise InvalidRequest(
            f"provider {provider!r} declares {value!r} as {owned.get('type')}, not {state_type}",
            {"field": "type", "reason": "type_mismatch"},
        )


# -- bindings -----------------------------------------------------------------------------------


BindingKey = Annotated[str, Field(pattern=BINDING_KEY_PATTERN)]
ExternalId = Annotated[str, Field(min_length=1, max_length=EXTERNAL_ID_MAX)]
ProviderName = Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)]


class BindingRequest(BaseModel):
    """A story's ids at one provider: ``{binding.<key>}`` of its manifest substitutes
    ``external_ids[<key>]`` (``PUT /stories/{id}/providers/{name}/binding``, and each entry of
    a story creation's ``provider_bindings``)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"external_ids": {"player_id": "p-48151623"}}]},
    )

    external_ids: dict[BindingKey, ExternalId] = Field(max_length=EXTERNAL_IDS_MAX)


ProviderBindings = dict[ProviderName, BindingRequest]
"""A story creation's ``provider_bindings``: ``{provider: {external_ids}}``."""


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """A story's binding to one provider: the story's external ids there and when it was
    (last) bound."""

    story_id: str
    provider: str
    external_ids: dict[str, str]
    bound_at: datetime


def _binding(row: Any) -> ProviderBinding:
    return ProviderBinding(
        story_id=str(row.story_id),
        provider=str(row.provider_name),
        external_ids={str(k): str(v) for k, v in dict(row.external_ids).items()},
        bound_at=row.bound_at,
    )


async def bindings(conn: AsyncConnection, story_id: str) -> list[ProviderBinding]:
    """The story's bindings, by provider name."""
    b = story_provider_bindings.c
    found = await conn.execute(
        select(story_provider_bindings).where(b.story_id == story_id).order_by(b.provider_name)
    )
    return [_binding(row) for row in found]


async def find_binding(conn: AsyncConnection, story_id: str, name: str) -> ProviderBinding | None:
    b = story_provider_bindings.c
    found = (
        await conn.execute(
            select(story_provider_bindings).where(b.story_id == story_id, b.provider_name == name)
        )
    ).one_or_none()
    return None if found is None else _binding(found)


def check_external_ids(external_ids: Mapping[str, Any]) -> dict[str, str]:
    """``external_ids`` checked: at most ``EXTERNAL_IDS_MAX`` keys matching
    ``BINDING_KEY_PATTERN``, each a non-empty string of at most ``EXTERNAL_ID_MAX``
    characters; 422 ``validation_error`` at the offending key."""
    if len(external_ids) > EXTERNAL_IDS_MAX:
        raise InvalidRequest(
            f"a binding holds at most {EXTERNAL_IDS_MAX} ids",
            {"field": "body.external_ids", "reason": "too_many"},
        )
    found: dict[str, str] = {}
    for key, value in external_ids.items():
        field = f"body.external_ids.{key}"
        if not re.match(BINDING_KEY_PATTERN, str(key)):
            raise InvalidRequest(
                "a binding key is letters, digits and underscores (at most 64)",
                {"field": field, "reason": "key"},
            )
        if not isinstance(value, str) or not value or len(value) > EXTERNAL_ID_MAX:
            raise InvalidRequest(
                f"an external id is a non-empty string of at most {EXTERNAL_ID_MAX} characters",
                {"field": field, "reason": "value"},
            )
        if "\x00" in value:
            raise InvalidRequest("an external id holds no NUL", {"field": field, "reason": "nul"})
        found[str(key)] = value
    return found


async def bind(
    conn: AsyncConnection, story_id: str, name: str, external_ids: Mapping[str, Any]
) -> ProviderBinding:
    """Store the story's ``external_ids`` at provider ``name`` in the caller's transaction (the
    caller holds the story lock), replacing any it had; see the module docstring."""
    checked = check_external_ids(external_ids)
    provider = await get_provider(conn, name)
    missing = sorted(binding_keys(provider.manifest) - set(checked))
    if missing:
        raise BindingIncomplete(
            f"the binding to {name!r} lacks the ids its manifest substitutes: "
            + ", ".join(missing),
            {"provider": name, "missing": missing},
        )
    values = {
        "story_id": story_id,
        "provider_name": name,
        "external_ids": checked,
        "bound_at": func.clock_timestamp(),
    }
    statement = insert(story_provider_bindings).values(**values)
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=["story_id", "provider_name"],
            set_={"external_ids": checked, "bound_at": func.clock_timestamp()},
        )
    )
    found = await find_binding(conn, story_id, name)
    assert found is not None  # noqa: S101 - written just above
    return found


async def bind_all(
    conn: AsyncConnection, story_id: str, given: Mapping[str, BindingRequest]
) -> list[ProviderBinding]:
    """``bind`` for each of a story creation's ``provider_bindings``, by name."""
    return [await bind(conn, story_id, name, given[name].external_ids) for name in sorted(given)]


def live_required(settings: Mapping[str, Any] | None) -> list[str]:
    """The providers a story's settings require live (not those kept ``inactive``)."""
    found = (settings or {}).get(REQUIRED_PROVIDERS)
    return [str(name) for name in found] if isinstance(found, list) else []


def inactive_required(settings: Mapping[str, Any] | None) -> list[str] | None:
    """The ``required_providers`` a story created before #204 keeps under
    ``settings.inactive`` (#122), else ``None``."""
    inactive = (settings or {}).get(INACTIVE)
    if not isinstance(inactive, Mapping):
        return None
    item = inactive.get(REQUIRED_PROVIDERS)
    if not isinstance(item, Mapping) or not isinstance(item.get("value"), list):
        return None
    return [str(name) for name in item["value"]]


async def activate_required_providers(
    conn: AsyncConnection, story_id: str, settings: Mapping[str, Any] | None
) -> list[str] | None:
    """Move an inactive ``required_providers`` to the live setting (the caller holds the story
    lock and records the change); answers the value moved, or ``None`` when there was none."""
    value = inactive_required(settings)
    if value is None:
        return None
    document = dict(settings or {})
    inactive = dict(document[INACTIVE])
    del inactive[REQUIRED_PROVIDERS]
    if inactive:
        document[INACTIVE] = inactive
    else:
        del document[INACTIVE]
    document[REQUIRED_PROVIDERS] = value
    await conn.execute(update(stories).where(stories.c.id == story_id).values(settings=document))
    return value


async def needed(conn: AsyncConnection, story_id: str) -> list[str]:
    """The providers a turn of the story needs bound: every provider an active externally owned
    State names, and every one its live ``required_providers`` lists, by name."""
    d = state_definitions.c
    named = set(
        await conn.scalars(
            select(d.provider_id).where(
                d.story_id == story_id,
                d.owner == StateOwner.EXTERNAL.value,
                d.provider_id.is_not(None),
                active_only(state_definitions),
            )
        )
    )
    settings = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    named.update(live_required(settings))
    return sorted(str(name) for name in named)


async def unbound(conn: AsyncConnection, story_id: str) -> list[str]:
    """The providers a turn of the story needs (``needed``) that it has no binding to."""
    wanted = await needed(conn, story_id)
    if not wanted:
        return []
    b = story_provider_bindings.c
    bound = set(
        await conn.scalars(
            select(b.provider_name).where(b.story_id == story_id, b.provider_name.in_(wanted))
        )
    )
    return [name for name in wanted if name not in bound]


__all__ = [
    "BINDING_KEY_PATTERN",
    "PROVIDER_FAMILY",
    "PROVIDER_NAME_PATTERN",
    "READ_PLACEHOLDERS",
    "REQUIRED_PROVIDERS",
    "SECRET_NAME_PATTERN",
    "WRITE_PLACEHOLDERS",
    "BindingRequest",
    "Placeholder",
    "ProviderBinding",
    "ProviderBindings",
    "ProviderRow",
    "activate_required_providers",
    "bind",
    "bind_all",
    "binding_keys",
    "bindings",
    "check_external_ids",
    "check_external_value",
    "find_binding",
    "find_provider",
    "get_provider",
    "inactive_required",
    "list_providers",
    "live_required",
    "needed",
    "placeholder",
    "placeholders_in",
    "require_registered",
    "stories_naming",
    "strings_in",
    "unbound",
]
