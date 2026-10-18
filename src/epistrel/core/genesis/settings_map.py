"""Which capability each scenario setting needs (FR-SCN-11, FR-PROF-18; #122).

A scenario's ``story_settings_defaults`` (and each role's participation) are stored with the
story whatever its pipeline: a setting whose capability a story materialized at genesis can use
is applied; any other is kept ``inactive`` under ``stories.settings.inactive`` as ``{name:
{value, capability}}``, waiting for the capability, and listed in the creation report. The
usable capabilities (``genesis_capabilities``) are those the deployment has delivered (#106's
``DELIVERED``, as ``catalog.checks.base.delivered_capabilities`` names them) that need no
``structured`` pipeline (FR-PROF-4): the settings and the creation's checks are decided before
the profile is, so they are the Lite view for every story. A structured story's own
materialization (#149, ``core.genesis.structured``) runs the seed checks against its cast; the
one setting that needs ``structured`` (``player_authority``) has no story setting yet and stays
``inactive``.

``SETTING_CAPABILITIES`` maps every ``story_settings_defaults`` field to the capability it
needs, ``None`` for the settings every story is created with (the turn concurrency, the default
output viewpoint, the audit retention, the story-scoped policies, which Lite uses, and the
arrival policy, a story setting only the structured permission check reads, #144, kept in place
so a later upgrade finds it). A capability no #106 module covers is named by the setting's own
area (``history_rewrite``, ``providers``). The build-time check ``unmapped_settings`` keeps the
dict complete.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from epistrel.core.catalog.checks.base import delivered_capabilities
from epistrel.core.catalog.scenario_models import StorySettingsDefaults
from epistrel.core.profiles import schema

MULTI_CHARACTER: Final = "multi_character"

SETTING_CAPABILITIES: Final[Mapping[str, str | None]] = {
    "interaction_mode": MULTI_CHARACTER,
    "turn_concurrency": None,
    "player_authority": "structured",
    "default_output_viewpoint": None,
    "ambiguous_retcon_timing": "history_rewrite",
    "backfill_conflict_policy": "history_rewrite",
    "audit_retention_days": None,
    "allow_new_arrivals": None,
    "policies": None,
    "required_providers": "providers",
}
"""Each ``story_settings_defaults`` field and the capability it needs (``None``: applied at
genesis)."""

PARTICIPATION: Final = "participation"
"""A role's default participation (``cast.roles[].participation``) is stored as the inactive
setting ``participation.<role id>`` under ``multi_character``."""


def unmapped_settings() -> list[str]:
    """``story_settings_defaults`` fields with no entry in ``SETTING_CAPABILITIES`` (and entries
    naming no field): ``[]`` when the map is complete."""
    fields = set(StorySettingsDefaults.model_fields)
    return sorted(fields ^ set(SETTING_CAPABILITIES))


STRUCTURED: Final = "structured"


def genesis_capabilities() -> frozenset[str]:
    """The capabilities a story's settings may use at genesis (see the module docstring): the
    delivered ones, less the ``structured`` pipeline and every module that requires it at any
    level (read from ``schema.DEPENDENCIES``)."""
    structured_only = {
        rule.module
        for rule in schema.DEPENDENCIES
        if rule.requires == (schema.STRUCTURED,) and not rule.levels and rule.when is None
    }
    return frozenset(
        name
        for name in delivered_capabilities()
        if name != STRUCTURED and name not in structured_only
    )


def is_used(capability: str | None) -> bool:
    """Whether a setting needing ``capability`` is applied to a story at genesis."""
    return capability is None or capability in genesis_capabilities()


__all__ = [
    "MULTI_CHARACTER",
    "PARTICIPATION",
    "SETTING_CAPABILITIES",
    "genesis_capabilities",
    "is_used",
    "unmapped_settings",
]
