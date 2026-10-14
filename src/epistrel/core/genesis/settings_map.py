"""Which capability each scenario setting needs (FR-SCN-11, FR-PROF-18; #122).

A scenario's ``story_settings_defaults`` (and each role's participation) are stored with the
story whatever its pipeline: a setting whose capability the deployment has delivered (#106's
``DELIVERED``, as ``catalog.checks.base.delivered_capabilities`` names them) is applied; any
other is kept ``inactive`` under ``stories.settings.inactive`` as ``{name: {value,
capability}}``, waiting for the capability, and listed in the creation report.

``SETTING_CAPABILITIES`` maps every ``story_settings_defaults`` field to the capability it
needs, ``None`` for the settings a Lite story uses (the turn concurrency, the default output
viewpoint, the audit retention, the story-scoped policies). A capability no #106 module covers
is named by the setting's own area (``history_rewrite``, ``providers``). The build-time check
``unmapped_settings`` keeps the dict complete.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from epistrel.core.catalog.checks.base import delivered_capabilities
from epistrel.core.catalog.scenario_models import StorySettingsDefaults

MULTI_CHARACTER: Final = "multi_character"

SETTING_CAPABILITIES: Final[Mapping[str, str | None]] = {
    "interaction_mode": MULTI_CHARACTER,
    "turn_concurrency": None,
    "player_authority": "structured",
    "default_output_viewpoint": None,
    "ambiguous_retcon_timing": "history_rewrite",
    "backfill_conflict_policy": "history_rewrite",
    "audit_retention_days": None,
    "policies": None,
    "required_providers": "providers",
}
"""Each ``story_settings_defaults`` field and the capability it needs (``None``: used by Lite)."""

PARTICIPATION: Final = "participation"
"""A role's default participation (``cast.roles[].participation``) is stored as the inactive
setting ``participation.<role id>`` under ``multi_character``."""


def unmapped_settings() -> list[str]:
    """``story_settings_defaults`` fields with no entry in ``SETTING_CAPABILITIES`` (and entries
    naming no field): ``[]`` when the map is complete."""
    fields = set(StorySettingsDefaults.model_fields)
    return sorted(fields ^ set(SETTING_CAPABILITIES))


def is_used(capability: str | None) -> bool:
    """Whether a setting needing ``capability`` is applied on this deployment."""
    return capability is None or capability in delivered_capabilities()


__all__ = [
    "MULTI_CHARACTER",
    "PARTICIPATION",
    "SETTING_CAPABILITIES",
    "is_used",
    "unmapped_settings",
]
