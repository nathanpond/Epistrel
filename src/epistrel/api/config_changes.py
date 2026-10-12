"""The body of the configuration-change listings (#64): ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from epistrel.core.config_changes import ConfigChangePage
from epistrel.core.settings_schema import ConfigClass, DeploymentSettings, StorySettings, flatten


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


def _setting_names(model: type[BaseModel]) -> tuple[str, ...]:
    """Every name a ``setting`` filter can take for ``model``: each leaf's dotted path and each
    of its parents (``generation_settings`` filters on all of its leaves)."""
    names: set[str] = set()
    for leaf in flatten(model, None):
        parts = leaf.split(".")
        names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return tuple(sorted(names))


def _setting_filter(model: type[BaseModel], prefixes: tuple[str, ...]) -> Any:
    """A ``setting`` query item for a scope (#279): one of ``model``'s setting names, or a row
    family written by that scope: ``<prefix>:`` alone matches the whole family and
    ``<prefix>:<name>`` one member. The closed vocabulary lets an unknown name be refused (422)
    instead of silently matching nothing."""
    names = _setting_names(model)
    known = frozenset(names)
    pattern = rf"^({'|'.join(prefixes)}):\S*$"

    def check(value: str) -> str:
        if value in known or re.fullmatch(pattern, value):
            return value
        families = ", ".join(f"{p}:" for p in prefixes)
        raise ValueError(f"must be a setting name or start with one of {families}")

    return Annotated[
        str,
        AfterValidator(check),
        WithJsonSchema(
            {
                "anyOf": [
                    {"type": "string", "enum": list(names)},
                    {"type": "string", "pattern": pattern},
                ]
            }
        ),
    ]


DeploymentSettingFilter = _setting_filter(DeploymentSettings, ("model_config", "policy", "rating"))
"""A deployment-scope ``setting`` filter: a deployment setting, or a model configuration, global
policy or rating row family."""

StorySettingFilter = _setting_filter(StorySettings, ("policy",))
"""A story-scope ``setting`` filter: a story setting, or the story's policy rows."""


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
