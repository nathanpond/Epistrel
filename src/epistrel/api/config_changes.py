"""The body of the configuration-change listings (#64): ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from epistrel.core.config_changes import ConfigChangePage
from epistrel.core.settings_schema import ConfigClass


def _iso_only(value: Any) -> Any:
    """``since`` is an RFC 3339 date-time, as documented: pydantic would also take a number of
    seconds since the epoch, which the schema does not promise (FR-API-4)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return value
    raise ValueError("must be an RFC 3339 date-time, e.g. 2026-10-11T12:00:00Z")


IsoDateTime = Annotated[datetime, BeforeValidator(_iso_only)]
"""A query ``date-time``: only the RFC 3339 form is accepted."""


class ConfigChangeItem(BaseModel):
    """One change: the revision it produced, the setting, its class, the old and new values
    (secret-bearing values redacted), who made it and when."""

    model_config = ConfigDict(populate_by_name=True)

    config_revision: int
    setting: str
    change_class: ConfigClass = Field(serialization_alias="class", alias="class")
    old: Any
    new: Any
    actor: str
    at: datetime


class ConfigChangesBody(BaseModel):
    """A page of configuration changes, newest first, and the cursor to the next page."""

    items: list[ConfigChangeItem]
    next: str | None
    """Pass as ``after`` for the next page; ``null`` on the last."""

    @classmethod
    def of(cls, page: ConfigChangePage) -> ConfigChangesBody:
        return cls(
            items=[
                ConfigChangeItem(
                    config_revision=item.config_revision,
                    setting=item.setting,
                    change_class=item.change_class,
                    old=item.old,
                    new=item.new,
                    actor=item.actor,
                    at=item.at,
                )
                for item in page.items
            ],
            next=page.next,
        )
