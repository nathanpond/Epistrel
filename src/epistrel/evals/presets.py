"""The preset an eval run plays its scratch stories under (NFR-QUAL-11; #227).

``epistrel evals run --preset lite|standard|full|max`` (default ``full``) selects the profile
every scratch story is assigned (``scenes.structured``) and the configuration the run's
fingerprint names, in place of the seeded ``Structured`` profile. A suite runs only when the
preset enables what it measures (``needs``): the structured pipeline (every suite plays
structured turns), the modules ``Suite.requires`` names, and every module its guarantee rows need
(``Suite.guarantees``, the guarantee table's rows, which ``CAPABILITY_MAP`` decides). A suite the
preset cannot run is reported ``not_applicable`` with the modules it needs and is never executed.
"""

from __future__ import annotations

import contextvars
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from epistrel.core.profiles import schema, tiers
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.guarantees import GUARANTEE
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.seeds import profiles as seeds

PresetName = Literal["lite", "standard", "full", "max"]
PRESET_NAMES: tuple[PresetName, ...] = ("lite", "standard", "full", "max")
DEFAULT_PRESET: PresetName = "full"
STRUCTURED = "structured"


@dataclass(frozen=True, slots=True)
class Preset:
    """One preset as the eval harness reads it: its CLI name, display name, profile id, the
    version a fresh deployment holds and that version's settings (the seed constants, never a
    live row)."""

    name: PresetName
    label: str
    profile_id: str
    settings: Mapping[str, Any]
    version: int = 1

    def effective(self, dials: Mapping[str, Any] | None = None) -> ProfileSettings:
        """Its settings over the seeded defaults, ``dials`` (a merge patch) on top, with
        ``"@strongest"`` routes left to each purpose's own model (the run's)."""
        settings = merged_settings(seeds.profile_defaults(), self.settings, dials)
        return tiers.resolved(settings, None)

    @property
    def described(self) -> str:
        return f"{self.label} v{self.version}"


PRESETS: Mapping[PresetName, Preset] = {
    "lite": Preset("lite", seeds.LITE_NAME, seeds.LITE_PRESET_ID, seeds.LITE_SETTINGS),
    "standard": Preset(
        "standard", seeds.STANDARD_NAME, seeds.STANDARD_PRESET_ID, seeds.STANDARD_SETTINGS
    ),
    "full": Preset(
        "full", seeds.FULL_NAME, seeds.FULL_PRESET_ID, seeds.FULL_SETTINGS, seeds.FULL_VERSION
    ),
    "max": Preset("max", seeds.MAX_NAME, seeds.MAX_PRESET_ID, seeds.MAX_SETTINGS),
}

RUN_PRESET: contextvars.ContextVar[Preset] = contextvars.ContextVar(
    "evals_run_preset", default=PRESETS[DEFAULT_PRESET]
)
"""The preset of the run in progress: ``scenes.structured`` assigns its profile."""


def needs(
    preset: Preset,
    *,
    requires: Collection[str] = (),
    guarantees: Collection[str] = (),
    dials: Mapping[str, Any] | None = None,
) -> list[str]:
    """What ``preset`` lacks for a suite with ``requires`` (module names) and ``guarantees``
    (guarantee-table row ids), in order and without repeats; empty when the suite applies."""
    effective = preset.effective(dials)
    missing: list[str] = []
    if schema.level_of(effective, "pipeline") != STRUCTURED:
        missing.append(STRUCTURED)
    missing += [m for m in requires if schema.level_of(effective, m) is None]
    for row_id in guarantees:
        row = GUARANTEE[row_id]
        if row.provided(effective) is not False:
            continue
        unmet = [need.describe() for need in row.requires if not need.met(effective)]
        if row.setting is not None and not row.setting.met(effective):
            unmet.append(row.setting.describe())
        missing += unmet or [row_id]
    return list(dict.fromkeys(missing))


__all__ = [
    "DEFAULT_PRESET",
    "PRESETS",
    "PRESET_NAMES",
    "RUN_PRESET",
    "Preset",
    "PresetName",
    "needs",
]
