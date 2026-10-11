"""The deployment's configuration revision and the single write path of the ``config_changes``
log (FR-CONC-14; #64 builds on it).

Every deployment-scoped configuration write — a rating created, edited or retired, a deployment
setting changed — takes ``lock_deployment`` first: one row, held ``FOR UPDATE`` for the write's
transaction, so writes are serialised (last write wins), the revision moves by exactly one per
write, and checks such as the inheritance cycle check see no concurrent change. It then appends
one ``config_changes`` row per changed setting, in the same transaction, with the revision that
write produced and the acting principal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.tables import config_changes, deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID


class ChangeClass(StrEnum):
    """FR-CONC-15: an interpretive change applies to operations committing after it; a
    prospective one only to operations starting after it. Untagged settings are interpretive."""

    INTERPRETIVE = "interpretive"
    PROSPECTIVE = "prospective"


class ConfigScope(StrEnum):
    DEPLOYMENT = "deployment"
    STORY = "story"


RETIRED = "retired"
"""The ``new`` value recorded when a rating is retired."""


def rating_setting(rating_id: str) -> str:
    """The ``setting`` of a rating's rows: ``rating:<id>``; old/new are version ids."""
    return f"rating:{rating_id}"


async def record_config_change(
    conn: AsyncConnection,
    *,
    config_revision: int,
    setting: str,
    old: Any,
    new: Any,
    actor: str,
    change_class: ChangeClass = ChangeClass.INTERPRETIVE,
    scope: ConfigScope = ConfigScope.DEPLOYMENT,
    scope_id: str | None = None,
) -> str:
    """Append one change row in the caller's transaction; return its id."""
    change_id = new_ulid()
    await conn.execute(
        insert(config_changes).values(
            change_id=change_id,
            scope=scope.value,
            scope_id=scope_id,
            config_revision=config_revision,
            setting=setting,
            old=null() if old is None else old,
            new=null() if new is None else new,
            **{"class": change_class.value},
            actor=actor,
        )
    )
    return change_id


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    """The deployment-state fields ``GET /admin/settings`` returns."""

    config_revision: int
    default_content_rating_id: str
    minor_age_threshold: int


def _settings(row: Any) -> DeploymentSettings:
    return DeploymentSettings(
        config_revision=int(row.config_revision),
        default_content_rating_id=row.default_content_rating_id,
        minor_age_threshold=int(row.minor_age_threshold),
    )


_COLUMNS = (
    deployment_state.c.config_revision,
    deployment_state.c.default_content_rating_id,
    deployment_state.c.minor_age_threshold,
)


async def read_settings(db: AsyncEngine | AsyncConnection) -> DeploymentSettings:
    query = select(*_COLUMNS).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if isinstance(db, AsyncConnection):
        return _settings((await db.execute(query)).one())
    async with db.connect() as conn:
        return _settings((await conn.execute(query)).one())


async def lock_deployment(conn: AsyncConnection) -> DeploymentSettings:
    """Take the deployment row ``FOR UPDATE`` (every configuration write starts here)."""
    query = select(*_COLUMNS).where(deployment_state.c.id == DEPLOYMENT_ROW_ID).with_for_update()
    return _settings((await conn.execute(query)).one())


async def bump_config_revision(conn: AsyncConnection, current: DeploymentSettings) -> int:
    """Advance the deployment's ``config_revision`` by one (caller holds the lock)."""
    revision = current.config_revision + 1
    await conn.execute(
        update(deployment_state)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .values(config_revision=revision)
    )
    return revision
