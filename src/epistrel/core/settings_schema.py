"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #57 adds the Lite generation settings
(``generation_settings``, ``lite_window_messages``, overriding the deployment's
``LiteDefaults``); #63 adds ``turn_queue_max`` and ``turn_deadline`` (``turn_concurrency`` and
``turn_queue_max`` live in their own ``stories`` columns) and #64 tags every setting
interpretive or prospective. Settings are read when an operation starts (prospective
settings never affect work already in flight).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


# -- story-level text caps (FR-LITE-3; #60) ------------------------------------------------------

DEFAULT_MAX_PINNED_MEMORIES = 50
MAX_PINNED_MEMORIES_LIMIT = 500
DEFAULT_MAX_CUSTOM_INSTRUCTIONS_CHARS = 4_000
MAX_CUSTOM_INSTRUCTIONS_CHARS_LIMIT = 20_000
PINNED_ITEM_MAX_CHARS = 2_000
"""The longest pinned memory, in code points after NFC normalisation and trimming."""


class StoryTextCaps(BaseModel):
    """The deployment's caps on pinned memories and custom instructions, stored under
    ``deployment_state.settings`` (#60). Prospective: an edit is checked against the caps in
    force when it starts, and a lowered cap never touches what is already stored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_pinned_memories: int = Field(
        default=DEFAULT_MAX_PINNED_MEMORIES, ge=1, le=MAX_PINNED_MEMORIES_LIMIT
    )
    max_custom_instructions_chars: int = Field(
        default=DEFAULT_MAX_CUSTOM_INSTRUCTIONS_CHARS, ge=1, le=MAX_CUSTOM_INSTRUCTIONS_CHARS_LIMIT
    )


# -- the idempotency result window (FR-CONC-6; #61) ----------------------------------------------

DEFAULT_IDEMPOTENCY_RESULT_WINDOW_HOURS = 24
MAX_IDEMPOTENCY_RESULT_WINDOW_HOURS = 24 * 30


class IdempotencySettings(BaseModel):
    """How long a mutating request's full result is kept under its ``Idempotency-Key``, in
    hours, stored under ``deployment_state.settings`` (#61). Prospective: a request keeps the
    window in force when it started; after it only the tombstone remains."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    idempotency_result_window: int = Field(
        default=DEFAULT_IDEMPOTENCY_RESULT_WINDOW_HOURS,
        ge=1,
        le=MAX_IDEMPOTENCY_RESULT_WINDOW_HOURS,
    )


TurnConcurrency = Literal["reject", "queue"]
TURN_QUEUE_MAX_MIN = 1
TURN_QUEUE_MAX_LIMIT = 50
DEFAULT_TURN_QUEUE_MAX = 5
"""The deployment's default bound on a ``queue`` story's waiting turns (FR-CONC-5; #63)."""
DEFAULT_TURN_DEADLINE_S = 45.0
"""``turn_deadline``'s default in seconds (NFR-PERF-6)."""
MAX_TURN_DEADLINE_S = 600.0
COLUMN_SETTINGS = ("turn_concurrency", "turn_queue_max")
"""Settings that live in their own ``stories`` columns (#63) rather than in ``settings``."""


class TurnQueueDefaults(BaseModel):
    """The deployment's default ``turn_queue_max``, stored under ``deployment_state.settings``
    (#63; #64 exposes it). A story's own ``turn_queue_max`` overrides it. Prospective."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    turn_queue_max: int = Field(
        default=DEFAULT_TURN_QUEUE_MAX, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT
    )


CHARACTER_VIEWPOINT_PREFIX = "character:"
OMNISCIENT = "omniscient"


class StorySettings(BaseModel):
    """A story's settings. Unknown keys are kept so later stories' settings survive a read."""

    model_config = ConfigDict(extra="allow", frozen=True)

    regenerate_retry_budget: int = Field(default=DEFAULT_REGENERATE_RETRY_BUDGET, ge=0, le=10)
    """How many times a turn whose read set was touched is regenerated before the call fails
    with ``revision_conflict`` (FR-CONC-4c). Prospective."""

    turn_concurrency: TurnConcurrency = "reject"
    """Same-story turn concurrency (FR-CONC-5): ``reject`` refuses an overlapping turn as busy
    (423 ``story_busy``), ``queue`` queues it in a per-story FIFO (#63). Stored in its own
    ``stories`` column. Prospective: a change applies to requests that arrive after it."""

    turn_queue_max: int | None = Field(default=None, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT)
    """How many turns may wait behind the running one under ``queue`` (``None``: the
    deployment's ``turn_queue_max``, default 5); one more is 423 ``story_busy``. Stored in its
    own ``stories`` column. Prospective."""

    turn_deadline: float = Field(default=DEFAULT_TURN_DEADLINE_S, gt=0, le=MAX_TURN_DEADLINE_S)
    """A turn's wall-clock deadline in seconds (NFR-PERF-6; #69 adds the call budget). The turn
    slot's lease is ``turn_deadline`` + 10 s, and a queued request waits at most this long
    before it is answered 202. Prospective."""

    default_output_viewpoint: str | None = None
    """The viewpoint a read or respond falls back to when the request names none and has no
    actor (FR-RESP-12): ``character:<id>``, never ``omniscient`` (#59 resolves it)."""

    generation_settings: GenerationSettings | None = None
    """The story's override of the deployment's Lite generation defaults (FR-LITE-4)."""

    lite_window_messages: int | None = Field(default=None, ge=1, le=MAX_LITE_WINDOW_MESSAGES)
    """The story's override of how many transcript rows a Lite prompt carries verbatim."""

    @field_validator("default_output_viewpoint")
    @classmethod
    def _never_omniscient(cls, value: str | None) -> str | None:
        """``omniscient`` is never a default (FR-RESP-12): every settings write refuses it."""
        if value is not None and value.strip().casefold() == OMNISCIENT:
            raise ValueError("a story's default output viewpoint must be a character")
        return value

    @classmethod
    def from_row(cls, value: dict[str, Any] | None, **columns: Any) -> StorySettings:
        """The settings of a ``stories`` row: its ``settings`` document, with the column-backed
        settings (``turn_concurrency``, ``turn_queue_max``) given as keywords when read."""
        document = dict(value or {})
        document.update({k: v for k, v in columns.items() if k in COLUMN_SETTINGS})
        return cls.model_validate(document)
