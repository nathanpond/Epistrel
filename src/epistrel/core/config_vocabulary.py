"""The closed vocabulary of the configuration-change listings' ``setting`` filter (#279).

Each scope lists the setting names it records and the row families it writes. An unknown name
is refused (422 ``validation_error`` at ``query.setting``) instead of silently matching nothing.
REST checks the query parameter against these types (``api/config_changes.py``), and the library
checks ``Engine.config_changes(settings=...)`` against the same ones (#435), so the two surfaces
refuse alike (FR-API-3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, WithJsonSchema

from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.profiles.store import PROFILE_FAMILY
from epistrel.core.profiles.story import ASSIGNMENT_SETTING, OVERRIDES_SETTING, VERSION_SETTING
from epistrel.core.providers import PROVIDER_FAMILY, REQUIRED_PROVIDERS
from epistrel.core.ratings.change import RATING_SETTING
from epistrel.core.settings_schema import (
    PROFILE_DEFAULTS,
    SUPERSEDED_DEPLOYMENT_SETTINGS,
    SUPERSEDED_STORY_SETTINGS,
    DeploymentSettings,
    GenerationSettings,
    StorySettings,
    flatten,
)


def _setting_names(model: type[BaseModel], prefix: str = "") -> tuple[str, ...]:
    """Every name a ``setting`` filter can take for ``model``: each leaf's dotted path and each
    of its parents (``profile_overrides`` filters on all of its leaves), under ``prefix``."""
    names: set[str] = set()
    for leaf in flatten(model, None):
        parts = f"{prefix}{leaf}".split(".")
        names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return tuple(sorted(names))


def _setting_filter(
    model: type[BaseModel], prefixes: tuple[str, ...], extra: tuple[str, ...] = ()
) -> Any:
    """A ``setting`` query item for a scope (#279): one of ``model``'s setting names (or of
    ``extra``), or a row family written by that scope: ``<prefix>:`` alone matches the whole
    family and ``<prefix>:<name>`` one member. The closed vocabulary lets an unknown name be
    refused (422) instead of silently matching nothing."""
    names = tuple(sorted({*_setting_names(model), *extra}))
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


def _superseded_names(names: Mapping[str, str], generation: str) -> tuple[str, ...]:
    """M2's names of the engine dials #113 moved to the profiles, which the rows written before
    the move keep: each name, and the generation settings' leaves under ``generation``."""
    leaves = _setting_names(GenerationSettings, f"{generation}.")
    return tuple(sorted({*names, *leaves}))


DeploymentSettingFilter = _setting_filter(
    DeploymentSettings,
    (
        "model_config",
        "policy",
        "rating",
        PROFILE_FAMILY,
        "verify_gate_threshold",
        PROVIDER_FAMILY,
    ),
    (
        *_setting_names(ProfileSettings, f"{PROFILE_DEFAULTS}."),
        *_superseded_names(SUPERSEDED_DEPLOYMENT_SETTINGS, "lite_generation_defaults"),
    ),
)
"""A deployment-scope ``setting`` filter: a deployment setting (one of the engine profiles'
defaults, ``profile_defaults.<path>``, included), or a model configuration, global policy,
rating or engine profile version (#113: ``engine_profile:<id>``, ``engine_profile:<id>.<path>``)
row family, or a shape's live gate threshold (#155: ``verify_gate_threshold:<shape>``), or an
external-data provider's registration (#204: ``external_provider:<name>``,
``external_provider:<name>.<field>``)."""

StorySettingFilter = _setting_filter(
    StorySettings,
    ("policy", "participation"),
    (
        ASSIGNMENT_SETTING,
        VERSION_SETTING,
        RATING_SETTING,
        REQUIRED_PROVIDERS,
        *_setting_names(ProfileSettings, f"{OVERRIDES_SETTING}."),
        *_superseded_names(SUPERSEDED_STORY_SETTINGS, "generation_settings"),
    ),
)
"""A story-scope ``setting`` filter: a story setting, its engine profile's assignment
(``engine_profile_id``, and ``engine_profile_version_pin`` for a pin, #181) or one of its
overrides (``profile_overrides.<path>``; #113), its rating (``content_rating_id``; #118), the
story's policy rows, or a character's participation role (#176: ``participation:<character
id>``), and the story's live ``required_providers`` (#204: moved there by its first provider
binding when genesis kept it inactive). M2's names of the dials #113 moved stay filterable:
rows written before the move keep them."""


__all__ = ["DeploymentSettingFilter", "StorySettingFilter"]
