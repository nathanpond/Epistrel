"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #63 and #64 add theirs and tag every
setting interpretive or prospective. Settings are read when an operation starts (prospective
settings never affect work already in flight).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_REGENERATE_RETRY_BUDGET = 2

TurnConcurrency = Literal["reject", "queue"]
CHARACTER_VIEWPOINT_PREFIX = "character:"


class StorySettings(BaseModel):
    """A story's settings. Unknown keys are kept so later stories' settings survive a read."""

    model_config = ConfigDict(extra="allow", frozen=True)

    regenerate_retry_budget: int = Field(default=DEFAULT_REGENERATE_RETRY_BUDGET, ge=0, le=10)
    """How many times a turn whose read set was touched is regenerated before the call fails
    with ``revision_conflict`` (FR-CONC-4c). Prospective."""

    turn_concurrency: TurnConcurrency = "reject"
    """Same-story turn concurrency (FR-CONC-5): ``reject`` refuses an overlapping turn as busy,
    ``queue`` queues it (#63 enforces it). Prospective."""

    default_output_viewpoint: str | None = None
    """The viewpoint a read or respond falls back to when the request names none and has no
    actor (FR-RESP-12): ``character:<id>``, never ``omniscient`` (#59 resolves it)."""

    @classmethod
    def from_row(cls, value: dict[str, Any] | None) -> StorySettings:
        return cls.model_validate(value or {})
