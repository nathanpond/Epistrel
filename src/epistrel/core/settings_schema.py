"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #63 and #64 add theirs and tag every
setting interpretive or prospective. Settings are read when an operation starts (prospective
settings never affect work already in flight).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_REGENERATE_RETRY_BUDGET = 2


class StorySettings(BaseModel):
    """A story's settings. Unknown keys are kept so later stories' settings survive a read."""

    model_config = ConfigDict(extra="allow", frozen=True)

    regenerate_retry_budget: int = Field(default=DEFAULT_REGENERATE_RETRY_BUDGET, ge=0, le=10)
    """How many times a turn whose read set was touched is regenerated before the call fails
    with ``revision_conflict`` (FR-CONC-4c). Prospective."""

    @classmethod
    def from_row(cls, value: dict[str, Any] | None) -> StorySettings:
        return cls.model_validate(value or {})
