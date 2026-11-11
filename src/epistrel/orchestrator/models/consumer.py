"""Consumer purposes in the Model Access Layer: resolution and routing (FR-MODEL-14; #824).

A consumer purpose is a model purpose an operator registered through ``/admin/purposes``
(``core.purposes``): a name, and versions of a prompt template and an output schema. It is
not declared in the purpose registry (ADR-185): ``ModelAccessLayer.consumer_purpose`` resolves
a name that is not built-in through the store.

**Resolution** (``ConsumerPurposes``): the purposes' rows (name, archived, latest version) are
cached per deployment ``config_revision``, the ``ConfigCache`` pattern: each lookup reads the
revision on a short connection of its own and re-reads the rows only when it moved, and every
store write moves it, so the call after a write sees it. A version is immutable, so each
resolved version (its compiled templates and its output model) is cached by ``(name,
version)`` in a bounded LRU and never re-read. Resolving an unknown name is 422
``validation_error`` (``UnknownPurpose``, reason ``unknown``, as for any unknown purpose), an
archived purpose 410 ``purpose_archived``, and an unknown version 404 ``version_not_found``.

**What a consumer purpose is** (``spec_of``): ``CONSUMER_FLOOR`` (structured output: the reply
is always the purpose's output schema), ``ContentCategory.CONSUMER_INPUT`` as its only input,
``never_receives`` every other category (#826 adds the viewpoint context), no quality floor and
no certification (NFR-QUAL-13's stance, ADR-185), and no template file: its templates are the
version's own, rendered in ``core.purposes.validation.environment()``.

**Routing** (``config.parent_of``): a consumer purpose has its own ``model_config`` row (``PUT
/admin/model-config/{name}``), and every field it does not set is ``generation``'s
(``CONSUMER_PARENT``; ``sources`` reads ``inherited``). Engine-profile and story routing never
apply: a profile's or a story's ``models.purposes`` names only story purposes (a consumer name
is 422 there), so it is never a turn's purpose.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from jinja2 import Template
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import PurposeArchived, PurposeVersionNotFound
from epistrel.core.purposes import store
from epistrel.core.purposes.output import output_model
from epistrel.core.purposes.validation import check_template
from epistrel.orchestrator.models.adapter import UnknownPurpose
from epistrel.orchestrator.models.config import CONSUMER_PARENT, current_revision
from epistrel.orchestrator.models.registry import (
    DEFAULT_MODEL_KEY,
    DELIVERED,
    PORTS,
    RESERVED,
    CapabilityFloor,
    ContentCategory,
    StructuredOutput,
)

CONSUMER_FLOOR: Final = CapabilityFloor(structured_output=StructuredOutput.PARSE_VALIDATE_RETRY)
"""A consumer purpose's capability floor: structured output (constrained decoding, or output
reliable enough to parse, validate and retry; the weaker is the floor, as for
``guardrail_check``)."""

CONSUMER_NEVER_RECEIVES: Final = frozenset(ContentCategory) - {ContentCategory.CONSUMER_INPUT}
"""Everything but the call's own inputs (#826 adds the viewpoint context)."""

VERSION_CACHE_SIZE: Final = 256


def builtin_names() -> frozenset[str]:
    """Every name the registry uses: delivered and reserved purposes, ports and ``default``.
    None of them may be a consumer purpose's name."""
    return frozenset({*DELIVERED, *PORTS, *RESERVED, DEFAULT_MODEL_KEY})


def is_builtin(name: str) -> bool:
    return name in DELIVERED or name in PORTS or name in RESERVED or name == DEFAULT_MODEL_KEY


@dataclass(frozen=True)
class ConsumerPurposeSpec:
    """What the Model Access Layer knows of every consumer purpose (module docs)."""

    default_model_key: str = CONSUMER_PARENT
    capability_floor: CapabilityFloor = CONSUMER_FLOOR
    never_receives: frozenset[ContentCategory] = CONSUMER_NEVER_RECEIVES
    hot_path: bool = False
    story_scoped: bool = False
    """Never a turn's purpose: no engine profile or story routes it."""


CONSUMER_SPEC: Final = ConsumerPurposeSpec()


@dataclass(frozen=True)
class ResolvedPurpose:
    """One version of a consumer purpose, ready for a call: its compiled ``template`` and
    ``system_template`` (``None`` when it has none) and ``output``, the pydantic model a reply
    is validated against (by alias: ``core.purposes.output.output_of`` gives the consumer's
    JSON object back)."""

    name: str
    version: int
    latest_version: int
    template: Template
    system_template: Template | None
    output_schema: dict[str, Any]
    output: type[BaseModel]
    spec: ConsumerPurposeSpec = field(default=CONSUMER_SPEC)


@dataclass(frozen=True)
class _Row:
    archived_at: datetime | None
    latest_version: int


class ConsumerPurposes:
    """The resolution cache (module docs)."""

    def __init__(self) -> None:
        self._revision: int | None = None
        self._rows: dict[str, _Row] = {}
        self._versions: OrderedDict[tuple[str, int], ResolvedPurpose] = OrderedDict()

    def clear(self) -> None:
        self._revision, self._rows = None, {}
        self._versions.clear()

    async def _row(self, db: AsyncEngine, name: str) -> _Row | None:
        async with db.connect() as conn:
            revision = await current_revision(conn)
            if revision != self._revision:
                self._revision, self._rows = revision, {}
            if name not in self._rows:
                found = await store.find(conn, name)
                if found is None:
                    return None
                self._rows[name] = _Row(found.archived_at, found.latest_version)
        return self._rows[name]

    async def resolve(
        self, db: AsyncEngine, name: str, version: int | None = None
    ) -> ResolvedPurpose:
        """``name`` at ``version`` (default: its latest), refused when unknown (422), archived
        (410) or the version is missing (404)."""
        row = await self._row(db, name)
        if row is None:
            raise UnknownPurpose(
                f"unknown model purpose {name!r}", details={"purpose": name, "reason": "unknown"}
            )
        if row.archived_at is not None:
            raise PurposeArchived(f"consumer purpose {name!r} is archived", {"purpose": name})
        wanted = version or row.latest_version
        if wanted > row.latest_version:
            raise PurposeVersionNotFound(
                f"consumer purpose {name!r} has no version {wanted}",
                {"purpose": name, "version": wanted},
            )
        key = (name, wanted)
        cached = self._versions.get(key)
        if cached is None:
            async with db.connect() as conn:
                stored = await store.read(conn, name, wanted)
            cached = compile_version(stored)
            self._versions[key] = cached
            while len(self._versions) > VERSION_CACHE_SIZE:
                self._versions.popitem(last=False)
        else:
            self._versions.move_to_end(key)
        if cached.latest_version != row.latest_version:
            cached = ResolvedPurpose(
                name=cached.name,
                version=cached.version,
                latest_version=row.latest_version,
                template=cached.template,
                system_template=cached.system_template,
                output_schema=cached.output_schema,
                output=cached.output,
            )
        return cached


def compile_version(purpose: store.ConsumerPurpose) -> ResolvedPurpose:
    """A stored version compiled for calls: its templates and its output model. (The store
    checked both at save, so a failure here is a stored row edited by hand.)"""
    system = purpose.system_template
    return ResolvedPurpose(
        name=purpose.name,
        version=purpose.version,
        latest_version=purpose.latest_version,
        template=check_template(purpose.template, "template"),
        system_template=None if system is None else check_template(system, "system_template"),
        output_schema=purpose.output_schema,
        output=output_model(purpose.output_schema),
    )


__all__ = [
    "CONSUMER_FLOOR",
    "CONSUMER_NEVER_RECEIVES",
    "CONSUMER_SPEC",
    "ConsumerPurposeSpec",
    "ConsumerPurposes",
    "ResolvedPurpose",
    "builtin_names",
    "compile_version",
    "is_builtin",
]
