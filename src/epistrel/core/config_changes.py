"""The deployment's configuration revision and the single write path of the ``config_changes``
log (FR-CONC-14,15; #48, #64).

Every configuration write — a rating created, edited or retired, a policy written, a deployment
or story setting changed, a model configured — takes its scope's lock first:
``lock_deployment`` (one row, held ``FOR UPDATE``) for deployment scope, the story row for
story scope. Writes are serialised (last write wins), the scope's ``config_revision`` moves by
exactly one per write, and checks such as the inheritance cycle check see no concurrent change.
The write then calls ``record_config_change`` once per changed setting, in the same
transaction, with the revision that write produced, the setting's class (``ConfigClass``,
FR-CONC-15) and the acting principal.

``record_config_change`` is the only writer of ``config_changes``. The first change of a write
also records the write's committed ``control`` operation (story-less for deployment scope;
#61 binds it to the call's ``Idempotency-Key``), and a story-scoped change appends a
``config_changed`` event to the story under the lock the write holds. No configuration write
moves a story's timeline revision.

``list_config_changes`` serves ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``: newest first, filtered by setting, class and time, with
secret-bearing values redacted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, func, insert, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import control
from epistrel.core.enums import Authority, EventCategory, Provenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import config_change_record, event_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid, ulid_after
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.seeds.profiles import profile_defaults as seeded_profile_defaults
from epistrel.core.settings_schema import (
    DEFAULT_FEEDBACK_TAGS,
    DEFAULT_LISTING_STATUS_VALUES,
    DEFAULT_MAX_CHARACTERS_AT_START,
    DEPLOYMENT_COLUMNS,
    ConfigClass,
    DeploymentSettings,
    GuardrailSettings,
    LiteDefaults,
    StoryTextCaps,
)
from epistrel.core.tables import config_changes, deployment_state, events
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

ChangeClass = ConfigClass
"""The class a ``config_changes`` row records (#48's name for ``ConfigClass``)."""


class ConfigScope(StrEnum):
    DEPLOYMENT = "deployment"
    STORY = "story"


log = logging.getLogger(__name__)

RETIRED = "retired"
"""The ``new`` value recorded when a rating is retired."""

CONFIG_CHANGED = "config_changed"
"""The event a story-scoped configuration write appends per changed setting (#64)."""

_WRITE_OPERATION = "epistrel.config_write_operation"
"""``conn.info`` key: ``(transaction id, operation id)`` of the write in progress."""
_LAST_CHANGE = "epistrel.config_last_change"
"""``conn.info`` key: ``(transaction id, change id)`` of the write's latest row (#113)."""


def rating_setting(rating_id: str) -> str:
    """The ``setting`` of a rating's rows: ``rating:<id>``; old/new are version ids."""
    return f"rating:{rating_id}"


async def _write_operation(
    conn: AsyncConnection, *, story_id: str | None, actor: str
) -> tuple[str, bool]:
    """The ``control`` operation of the configuration write running in ``conn``'s transaction,
    recorded (committed, with the write, its key bound; ``core.control``) at its first change;
    ``(id, created now)``."""
    transaction = int(await conn.scalar(select(func.txid_current())) or 0)
    known = conn.info.get(_WRITE_OPERATION)
    if known is not None and known[0] == transaction:
        return str(known[1]), False
    operation_id = await control.record_control_operation(conn, actor=actor, story_id=story_id)
    conn.info[_WRITE_OPERATION] = (transaction, operation_id)
    return operation_id, True


async def write_operation(conn: AsyncConnection, *, story_id: str | None, actor: str) -> str:
    """The id of the configuration write's ``control`` operation in ``conn``'s transaction,
    recorded now if the write has none yet: a caller that records more of the write's events
    under it (a rating change's ``story_resumed``, #118) asks for it before its first change."""
    operation_id, _ = await _write_operation(conn, story_id=story_id, actor=actor)
    return operation_id


async def _story_event(
    conn: AsyncConnection,
    *,
    story_id: str,
    operation_id: str,
    setting: str,
    old: Any,
    new: Any,
    actor: str,
) -> None:
    """Append one ``config_changed`` event to the story (the caller holds the story lock)."""
    # The payload is ``ConfigChanged``'s (registered in ``core.events.types``; built here
    # without the registry, which imports this module through ``Operation``).
    payload = {"setting": setting, "old": old, "new": new}
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=operation_id,
            category=EventCategory.CONTROL.value,
            principal=actor,
            type=CONFIG_CHANGED,
            payload=payload,
            provenance=Provenance.CONSUMER.value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await index_records(
        conn,
        [
            event_record(
                SimpleNamespace(id=event_id, story_id=story_id, character_id=None, payload=payload)
            )
        ],
    )


async def record_config_change(
    conn: AsyncConnection,
    *,
    config_revision: int,
    setting: str,
    old: Any,
    new: Any,
    actor: str,
    change_class: ConfigClass = ConfigClass.interpretive,
    scope: ConfigScope = ConfigScope.DEPLOYMENT,
    scope_id: str | None = None,
) -> str:
    """Append one change row in the caller's transaction; return its id. See the module
    docstring for the write's ``control`` operation and the story's ``config_changed`` event."""
    story_id = scope_id if scope is ConfigScope.STORY else None
    operation_id, _ = await _write_operation(conn, story_id=story_id, actor=actor)
    if story_id is not None:
        await _story_event(
            conn,
            story_id=story_id,
            operation_id=operation_id,
            setting=setting,
            old=old,
            new=new,
            actor=actor,
        )
    change_id = _change_id(conn)
    await conn.execute(
        insert(config_changes).values(
            change_id=change_id,
            scope=scope.value,
            scope_id=scope_id,
            config_revision=config_revision,
            setting=setting,
            old=null() if old is None else old,
            new=null() if new is None else new,
            **{"class": ConfigClass(change_class).value},
            actor=actor,
        )
    )
    await index_records(
        conn,
        [
            config_change_record(
                SimpleNamespace(
                    change_id=change_id, scope=scope.value, scope_id=scope_id, old=old, new=new
                )
            )
        ],
    )
    return change_id


def _change_id(conn: AsyncConnection) -> str:
    """The id of the write's next row, after its previous one: the rows of one write (one
    revision) list in the order they were recorded, newest first (#113: a profile version's
    rows; before, ids minted in one millisecond sorted at random)."""
    transaction = conn.info[_WRITE_OPERATION][0]
    last = conn.info.get(_LAST_CHANGE)
    change_id = ulid_after(last[1]) if last is not None and last[0] == transaction else new_ulid()
    conn.info[_LAST_CHANGE] = (transaction, change_id)
    return change_id


# -- the deployment's state ----------------------------------------------------------------------

SECTIONS: tuple[type[BaseModel], ...] = tuple(
    base for base in DeploymentSettings.__bases__ if issubclass(base, BaseModel)
)
"""The views of ``deployment_state.settings`` that ``document_settings`` keeps: every section
``DeploymentSettings`` is built from, derived from its bases so that a new section cannot be
missed (#315: a hand-kept list lacked ``RetentionSettings``, ``WorkerHealthSettings`` and
``ErasureSettings``, so ``/admin/settings`` reported their defaults and the next write reset
them)."""


def section[M: BaseModel](view: type[M], document: Mapping[str, Any] | None) -> M:
    """One view of a ``deployment_state.settings`` document; an invalid stored value falls back
    to the view's defaults (a warning names only the keys, never the values)."""
    values = {k: v for k, v in (document or {}).items() if k in view.model_fields}
    try:
        return view.model_validate(values)
    except ValidationError:
        log.warning("invalid deployment settings %s; using the defaults", sorted(values))
        return view()


@dataclass(frozen=True, slots=True)
class DeploymentState:
    """``deployment_state``: the configuration revision, the column settings
    (``DEPLOYMENT_COLUMNS``) and the ``settings`` document (every section, defaults filled in)
    — what ``/admin/settings`` returns."""

    config_revision: int
    default_content_rating_id: str
    minor_age_threshold: int
    max_characters_at_start_default: int = DEFAULT_MAX_CHARACTERS_AT_START
    max_characters_at_start_ceiling: int | None = None
    default_engine_profile_id: str = LITE_PRESET_ID
    listing_status_values: list[str] = field(
        default_factory=lambda: list(DEFAULT_LISTING_STATUS_VALUES)
    )
    feedback_tags: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDBACK_TAGS))
    profile_defaults: dict[str, Any] = field(default_factory=seeded_profile_defaults)
    """The engine profiles' deployment defaults (``deployment_state.profile_defaults``, #113)."""
    settings: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""

    def as_settings(self) -> DeploymentSettings:
        """Every deployment setting as the tagged schema."""
        return DeploymentSettings.model_validate(
            {
                **self.settings,
                "default_content_rating_id": self.default_content_rating_id,
                "minor_age_threshold": self.minor_age_threshold,
                "max_characters_at_start_default": self.max_characters_at_start_default,
                "max_characters_at_start_ceiling": self.max_characters_at_start_ceiling,
                "default_engine_profile_id": self.default_engine_profile_id,
                "listing_status_values": self.listing_status_values,
                "feedback_tags": self.feedback_tags,
                "profile_defaults": self.profile_defaults,
            }
        )


def document_settings(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every section of a stored ``settings`` document, each validated (or its defaults)."""
    merged: dict[str, Any] = {}
    for view in SECTIONS:
        merged.update(section(view, document).model_dump(mode="json"))
    return {k: v for k, v in merged.items() if k not in DEPLOYMENT_COLUMNS}


def _state(row: Any) -> DeploymentState:
    return DeploymentState(
        config_revision=int(row.config_revision),
        default_content_rating_id=row.default_content_rating_id,
        minor_age_threshold=int(row.minor_age_threshold),
        max_characters_at_start_default=int(row.max_characters_at_start_default),
        max_characters_at_start_ceiling=row.max_characters_at_start_ceiling,
        default_engine_profile_id=row.default_engine_profile_id,
        listing_status_values=list(row.listing_status_values),
        feedback_tags=list(row.feedback_tags),
        profile_defaults=dict(row.profile_defaults),
        settings=document_settings(row.settings),
    )


_COLUMNS = (
    deployment_state.c.config_revision,
    deployment_state.c.default_content_rating_id,
    deployment_state.c.minor_age_threshold,
    deployment_state.c.max_characters_at_start_default,
    deployment_state.c.max_characters_at_start_ceiling,
    deployment_state.c.default_engine_profile_id,
    deployment_state.c.listing_status_values,
    deployment_state.c.feedback_tags,
    deployment_state.c.profile_defaults,
    deployment_state.c.settings,
)


async def read_settings(db: AsyncEngine | AsyncConnection) -> DeploymentState:
    query = select(*_COLUMNS).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return _state((await db.execute(query)).one())
    async with db.connect() as conn:
        return _state((await conn.execute(query)).one())


async def lock_deployment(conn: AsyncConnection) -> DeploymentState:
    """Take the deployment row ``FOR UPDATE`` (every deployment configuration write starts
    here)."""
    query = select(*_COLUMNS).where(deployment_state.c.id == DEPLOYMENT_ROW_ID).with_for_update()
    return _state((await conn.execute(query)).one())


async def raw_document(conn: AsyncConnection) -> dict[str, Any]:
    """The stored ``settings`` document as is (keys of other stories kept on write)."""
    found = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return dict(found or {})


async def bump_config_revision(conn: AsyncConnection, current: DeploymentState) -> int:
    """Advance the deployment's ``config_revision`` by one (caller holds the lock)."""
    revision = current.config_revision + 1
    await conn.execute(
        update(deployment_state)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .values(config_revision=revision)
    )
    return revision


# -- the deployment's Lite defaults (FR-LITE-4; #57) -------------------------------------------


def lite_defaults(document: Mapping[str, Any] | None) -> LiteDefaults:
    """The Lite defaults in a ``deployment_state.settings`` document (defaults if invalid)."""
    return section(LiteDefaults, document)


async def read_lite_defaults(db: AsyncEngine | AsyncConnection) -> LiteDefaults:
    query = select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return lite_defaults(await db.scalar(query))
    async with db.connect() as conn:
        return lite_defaults(await conn.scalar(query))


async def read_guardrail_settings(db: AsyncEngine | AsyncConnection) -> GuardrailSettings:
    """The deployment's safe fallback line (#68; defaults if invalid)."""
    query = select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return section(GuardrailSettings, await db.scalar(query))
    async with db.connect() as conn:
        return section(GuardrailSettings, await conn.scalar(query))


async def read_section[M: BaseModel](db: AsyncEngine | AsyncConnection, view: type[M]) -> M:
    """One view of the deployment's settings document, read now (defaults if invalid), e.g.
    ``RetentionSettings`` or ``WorkerHealthSettings`` (#71)."""
    query = select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return section(view, await db.scalar(query))
    async with db.connect() as conn:
        return section(view, await conn.scalar(query))


# -- the deployment's story-level text caps (FR-LITE-3; #60) ----------------------------------


def story_text_caps(document: Mapping[str, Any] | None) -> StoryTextCaps:
    """The pinned-memory and custom-instruction caps (defaults if invalid)."""
    return section(StoryTextCaps, document)


async def read_story_text_caps(db: AsyncEngine | AsyncConnection) -> StoryTextCaps:
    query = select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return story_text_caps(await db.scalar(query))
    async with db.connect() as conn:
        return story_text_caps(await conn.scalar(query))


# -- the listings (#64) ------------------------------------------------------------------------

DEFAULT_PAGE = 50
MAX_PAGE = 200
REDACTED = "***"
_SECRET_KEY = re.compile(r"api[_-]?key|secret|password|passwd|token|authori[sz]ation|credential")


def redact(value: Any) -> Any:
    """``value`` with every secret-bearing key's value replaced by ``***`` (nested)."""
    if isinstance(value, Mapping):
        return {
            k: REDACTED if _SECRET_KEY.search(str(k).casefold()) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """One ``config_changes`` row as a listing shows it."""

    config_revision: int
    setting: str
    change_class: ConfigClass
    old: Any
    new: Any
    actor: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ConfigChangePage:
    items: list[ConfigChange]
    next: str | None
    """The cursor of the following page (``None`` on the last)."""


def _cursor(change_revision: int, change_id: str) -> str:
    return f"{change_revision}.{change_id}"


def _parse_cursor(cursor: str) -> tuple[int, str]:
    revision, _, change_id = cursor.partition(".")
    if not revision.isdigit() or not change_id:
        raise InvalidRequest("not a config-changes cursor", {"field": "after"})
    return int(revision), change_id


async def list_config_changes(
    db: AsyncEngine,
    *,
    scope: ConfigScope,
    scope_id: str | None = None,
    settings: Sequence[str] = (),
    change_class: ConfigClass | None = None,
    since: datetime | None = None,
    after: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> ConfigChangePage:
    """A page of the scope's changes, newest first (by revision, then row). ``settings`` keeps
    rows whose setting is one of them or a leaf under one of them (``profile_overrides``
    matches ``profile_overrides.models.generation.temperature``); a name ending in ``:`` is a
    prefix
    (``rating:`` matches every rating's rows)."""
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidRequest(f"limit must be 1 to {MAX_PAGE}", {"field": "limit"})
    c = config_changes.c
    where = [c.scope == scope.value]
    where.append(c.scope_id.is_(None) if scope_id is None else c.scope_id == scope_id)
    if settings:
        where.append(
            or_(
                *(
                    or_(c.setting == name, c.setting.startswith(f"{name}."))
                    if not name.endswith(":")
                    else c.setting.startswith(name)
                    for name in settings
                )
            )
        )
    if change_class is not None:
        where.append(c["class"] == change_class.value)
    if since is not None:
        where.append(c.at >= since)
    if after is not None:
        revision, change_id = _parse_cursor(after)
        where.append(
            or_(
                c.config_revision < revision,
                and_(c.config_revision == revision, c.change_id < change_id),
            )
        )
    query = (
        select(c.change_id, c.config_revision, c.setting, c["class"], c.old, c.new, c.actor, c.at)
        .where(*where)
        .order_by(c.config_revision.desc(), c.change_id.desc())
        .limit(limit + 1)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
    page = rows[:limit]
    items = [
        ConfigChange(
            config_revision=int(row.config_revision),
            setting=row.setting,
            change_class=ConfigClass(row[3]),
            old=redact(row.old),
            new=redact(row.new),
            actor=row.actor,
            at=row.at,
        )
        for row in page
    ]
    following = (
        _cursor(int(page[-1].config_revision), page[-1].change_id) if len(rows) > limit else None
    )
    return ConfigChangePage(items=items, next=following)
