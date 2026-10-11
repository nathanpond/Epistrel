"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #57 adds the Lite generation settings
(``generation_settings``, ``lite_window_messages``, overriding the deployment's
``LiteDefaults``); #63 and #64 add theirs and tag every
setting interpretive or prospective. Settings are read when an operation starts (prospective
settings never affect work already in flight).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_REGENERATE_RETRY_BUDGET = 2

# -- Lite generation settings (FR-LITE-4, FR-RESP-3; #57) -------------------------------------

LengthName = Literal["short", "medium", "long"]
LENGTH_TOKENS: Mapping[str, int] = {"short": 150, "medium": 400, "long": 800}
"""``max_tokens`` of each named response length."""
MAX_LENGTH_TOKENS = 4_000
Length = LengthName | Annotated[int, Field(ge=1, le=MAX_LENGTH_TOKENS)]
"""A response length: a name (``short``/``medium``/``long``) or a token cap."""

DEFAULT_LITE_WINDOW_MESSAGES = 30
"""How many of the latest transcript rows a Lite prompt carries verbatim (FR-LITE-1)."""
MAX_LITE_WINDOW_MESSAGES = 500


class GenerationSettings(BaseModel):
    """Generation settings at one layer: deployment Lite defaults, story override, or a
    request's ``prompt_params``. ``None`` inherits from the layer below."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    length: Length | None = None


DEFAULT_LITE_GENERATION = GenerationSettings(temperature=0.8, top_p=0.95, length="medium")
"""The deployment's Lite defaults until an operator changes them (``lite_generation_defaults``)."""


class ResolvedGeneration(BaseModel):
    """The generation settings a turn runs with, every layer applied (recorded on the
    operation's ``config_revisions.generation_settings``)."""

    model_config = ConfigDict(frozen=True)

    temperature: float | None
    top_p: float | None
    length: Length
    max_tokens: int


def max_tokens_for(length: Length) -> int:
    return LENGTH_TOKENS[length] if isinstance(length, str) else length


def resolve_generation(*layers: GenerationSettings | None) -> ResolvedGeneration:
    """Apply ``layers`` lowest first (deployment → story → request): each set key wins."""
    values: dict[str, Any] = {}
    for layer in layers:
        if layer is not None:
            values.update(layer.model_dump(exclude_none=True))
    length: Length = values.get("length", DEFAULT_LITE_GENERATION.length or "medium")
    return ResolvedGeneration(
        temperature=values.get("temperature"),
        top_p=values.get("top_p"),
        length=length,
        max_tokens=max_tokens_for(length),
    )


class LiteDefaults(BaseModel):
    """The deployment's Lite defaults, stored under ``deployment_state.settings`` (#57)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    lite_generation_defaults: GenerationSettings = DEFAULT_LITE_GENERATION
    lite_window_messages: int = Field(
        default=DEFAULT_LITE_WINDOW_MESSAGES, ge=1, le=MAX_LITE_WINDOW_MESSAGES
    )


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

    generation_settings: GenerationSettings | None = None
    """The story's override of the deployment's Lite generation defaults (FR-LITE-4)."""

    lite_window_messages: int | None = Field(default=None, ge=1, le=MAX_LITE_WINDOW_MESSAGES)
    """The story's override of how many transcript rows a Lite prompt carries verbatim."""

    @classmethod
    def from_row(cls, value: dict[str, Any] | None) -> StorySettings:
        return cls.model_validate(value or {})
