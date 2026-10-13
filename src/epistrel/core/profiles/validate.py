"""Validating an engine profile's settings (FR-PROF-2,4; #106).

``validate_profile(document)`` checks a settings document in three steps:

1. **Shape.** The document is validated strictly (JSON's own types, no coercion) against
   ``ProfileSettings``, filling every setting it leaves out with the model's default; a shape
   error is 422 ``validation_error`` at its field (``body.settings.<path>``).
2. **Delivered.** A key that names a module this deployment has not delivered, or a value that
   picks an undelivered level (``pipeline: structured``), is reported in ``undelivered``,
   whatever the value: the published schema has no place for it. So is a model purpose the
   Orchestrator reserves for a later milestone; one it has never heard of is a shape error.
3. **Dependencies.** Every rule of ``schema.DEPENDENCIES`` that applies to the settings and is
   not met is reported in ``missing_dependencies``, with the requirements it lacks.

``require_valid`` raises 422 ``invalid_profile`` with both lists when either is non-empty and
returns the document normalised (the settings it names, as stored). The same function validates
every profile version and, from #113, every story's effective configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from epistrel.core.errors import InvalidProfile, InvalidRequest
from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings, Purposes


class RequiredModule(BaseModel):
    """A module a rule requires, at one of ``levels`` (any level that is on when empty)."""

    model_config = ConfigDict(frozen=True)

    module: str
    levels: list[str]


class MissingDependency(BaseModel):
    """A module that is on (at ``level``) without what it requires (FR-PROF-4)."""

    model_config = ConfigDict(frozen=True)

    module: str
    level: str
    requires: list[RequiredModule]


class UndeliveredSetting(BaseModel):
    """A setting naming a module, or a level of one, this deployment has not delivered:
    ``setting`` is its dotted path in the settings, ``level`` the level its value picks."""

    model_config = ConfigDict(frozen=True)

    module: str
    level: str | None
    setting: str


class ProfileValidation(BaseModel):
    """The outcome of validating a profile's settings: ``valid`` exactly when both lists are
    empty."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    missing_dependencies: list[MissingDependency]
    undelivered: list[UndeliveredSetting]


def parse_settings(
    document: Mapping[str, Any], *, field: tuple[str, ...] = ("body", "settings")
) -> ProfileSettings:
    """``document`` as ``ProfileSettings`` (strictly: JSON's types, no coercion), every
    omitted setting at its default; 422 ``validation_error`` under ``field``."""
    try:
        return ProfileSettings.model_validate(document, strict=True)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=field) from None


def normalised(settings: ProfileSettings) -> dict[str, Any]:
    """The settings a parsed document names, as JSON (what a profile version stores)."""
    return settings.model_dump(mode="json", exclude_unset=True)


def canonical(document: Mapping[str, Any]) -> str:
    """One text per settings document, whatever its key order (to compare two versions)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _purposes_problems(
    settings: ProfileSettings, purposes: Purposes | None, field: tuple[str, ...]
) -> list[UndeliveredSetting]:
    if purposes is None:
        return []
    undelivered: list[UndeliveredSetting] = []
    for name in settings.models.purposes:
        if name in purposes.delivered:
            continue
        setting = f"models.purposes.{name}"
        if name in purposes.reserved:
            undelivered.append(UndeliveredSetting(module="models", level=None, setting=setting))
            continue
        raise InvalidRequest(
            f"{name!r} is not a model purpose",
            {"field": ".".join((*field, setting)), "purpose": name},
        )
    return undelivered


def undelivered_settings(
    document: Mapping[str, Any],
    settings: ProfileSettings,
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "settings"),
) -> list[UndeliveredSetting]:
    """What ``document`` (already parsed as ``settings``) names that is not delivered: every
    undelivered setting it names, with its module and the level its value picks (a group it
    names empty counts as a setting), and every switch of a delivered module whose value picks
    an undelivered level."""
    found: list[UndeliveredSetting] = []
    for path, value in schema.walk(document):
        switch = schema.SWITCHES.get(path)
        level = None if switch is None else switch.level(schema.value_at(settings, path))
        if schema.exposed(path):
            undelivered_level = (
                switch is not None
                and level is not None
                and not schema.is_delivered(switch.name, level)
            )
            if switch is not None and undelivered_level:
                found.append(UndeliveredSetting(module=switch.name, level=level, setting=path))
            continue
        if path not in schema.LEAF_PATHS and value:
            continue  # a group with members: they are reported one by one
        owner = schema.owner_of(path)
        assert owner is not None  # noqa: S101 - every undelivered setting or group has an owner
        module, owned_level = owner
        found.append(UndeliveredSetting(module=module, level=level or owned_level, setting=path))
    return found + _purposes_problems(settings, purposes, field)


def missing_dependencies(settings: ProfileSettings) -> list[MissingDependency]:
    """Every dependency rule that applies to ``settings`` and is not met, with what it lacks
    (``schema.DEPENDENCIES``, read now: a test may add a rule)."""
    found: list[MissingDependency] = []
    for rule in schema.DEPENDENCIES:
        unmet = rule.unmet(settings)
        if unmet:
            found.append(
                MissingDependency(
                    module=rule.module,
                    level=schema.level_of(settings, rule.module) or schema.ON,
                    requires=[
                        RequiredModule(module=r.module, levels=list(r.levels)) for r in unmet
                    ],
                )
            )
    return found


def validate_profile(
    document: Mapping[str, Any],
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "settings"),
) -> ProfileValidation:
    """Validate a settings document (see the module docstring); a shape error raises 422
    ``validation_error`` under ``field``, anything else is reported."""
    settings = parse_settings(document, field=field)
    undelivered = undelivered_settings(document, settings, purposes=purposes, field=field)
    missing = missing_dependencies(settings)
    return ProfileValidation(
        valid=not (undelivered or missing), missing_dependencies=missing, undelivered=undelivered
    )


def refusal(result: ProfileValidation) -> InvalidProfile:
    """The 422 ``invalid_profile`` for an invalid result, naming every finding."""
    names = [item.module for item in result.undelivered] + [
        item.module for item in result.missing_dependencies
    ]
    return InvalidProfile(
        f"the profile cannot run: {', '.join(dict.fromkeys(names))}",
        {
            "missing_dependencies": [
                item.model_dump(mode="json") for item in result.missing_dependencies
            ],
            "undelivered": [item.model_dump(mode="json") for item in result.undelivered],
        },
    )


def require_valid(
    document: Mapping[str, Any],
    *,
    purposes: Purposes | None = None,
    field: tuple[str, ...] = ("body", "settings"),
) -> dict[str, Any]:
    """``document`` normalised, when it is a valid profile; 422 ``validation_error`` for its
    shape and 422 ``invalid_profile`` for what it names or lacks, otherwise."""
    result = validate_profile(document, purposes=purposes, field=field)
    if not result.valid:
        raise refusal(result)
    return normalised(parse_settings(document, field=field))


__all__ = [
    "MissingDependency",
    "ProfileValidation",
    "RequiredModule",
    "UndeliveredSetting",
    "canonical",
    "missing_dependencies",
    "normalised",
    "parse_settings",
    "refusal",
    "require_valid",
    "undelivered_settings",
    "validate_profile",
]
