"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #57 adds the Lite generation settings
(``generation_settings``, ``lite_window_messages``, overriding the deployment's
``LiteDefaults``); #63 adds ``turn_queue_max`` and ``turn_deadline`` (``turn_concurrency`` and
``turn_queue_max`` live in their own ``stories`` columns) and #64 tags every setting
interpretive or prospective. Settings are read when an operation starts (prospective
settings never affect work already in flight).

**Classification (FR-CONC-15, #64).** Every leaf setting of ``StorySettings`` and
``DeploymentSettings`` carries ``Annotated[..., ConfigClass.interpretive]`` or
``Annotated[..., ConfigClass.prospective]``: an *interpretive* change applies to every operation
that commits after it (a turn in flight is re-checked under it at commit, FR-CONC-16), a
*prospective* one only to operations that start after it. ``setting_class`` reads the tag; an
untagged setting is interpretive (``test_u_18`` fails the build when one is missing).
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.fields import FieldInfo


class ConfigClass(StrEnum):
    """FR-CONC-15: how a setting's change reaches work in flight (see the module docstring)."""

    interpretive = "interpretive"
    prospective = "prospective"


INTERPRETIVE = ConfigClass.interpretive
PROSPECTIVE = ConfigClass.prospective

DEFAULT_REGENERATE_RETRY_BUDGET = 2
DEFAULT_GUARDRAIL_RETRY_BUDGET = 2
MAX_GUARDRAIL_RETRY_BUDGET = 5

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

    temperature: Annotated[float | None, PROSPECTIVE] = Field(default=None, ge=0.0, le=2.0)
    top_p: Annotated[float | None, PROSPECTIVE] = Field(default=None, ge=0.0, le=1.0)
    length: Annotated[Length | None, PROSPECTIVE] = None


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
    lite_window_messages: Annotated[int, PROSPECTIVE] = Field(
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

    max_pinned_memories: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MAX_PINNED_MEMORIES, ge=1, le=MAX_PINNED_MEMORIES_LIMIT
    )
    max_custom_instructions_chars: Annotated[int, PROSPECTIVE] = Field(
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

    idempotency_result_window: Annotated[int, PROSPECTIVE] = Field(
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

    turn_queue_max: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_TURN_QUEUE_MAX, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT
    )


CHARACTER_VIEWPOINT_PREFIX = "character:"
OMNISCIENT = "omniscient"


class StorySettings(BaseModel):
    """A story's settings. Unknown keys are kept so later stories' settings survive a read."""

    model_config = ConfigDict(extra="allow", frozen=True)

    regenerate_retry_budget: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_REGENERATE_RETRY_BUDGET, ge=0, le=10
    )
    """How many times a turn whose read set was touched is regenerated before the call fails
    with ``revision_conflict`` (FR-CONC-4c). Prospective."""

    turn_concurrency: Annotated[TurnConcurrency, PROSPECTIVE] = "reject"
    """Same-story turn concurrency (FR-CONC-5): ``reject`` refuses an overlapping turn as busy
    (423 ``story_busy``), ``queue`` queues it in a per-story FIFO (#63). Stored in its own
    ``stories`` column. Prospective: a change applies to requests that arrive after it."""

    turn_queue_max: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT
    )
    """How many turns may wait behind the running one under ``queue`` (``None``: the
    deployment's ``turn_queue_max``, default 5); one more is 423 ``story_busy``. Stored in its
    own ``stories`` column. Prospective."""

    turn_deadline: Annotated[float, PROSPECTIVE] = Field(
        default=DEFAULT_TURN_DEADLINE_S, gt=0, le=MAX_TURN_DEADLINE_S
    )
    """A turn's wall-clock deadline in seconds (NFR-PERF-6; #69 adds the call budget). The turn
    slot's lease is ``turn_deadline`` + 10 s, and a queued request waits at most this long
    before it is answered 202. Prospective."""

    default_output_viewpoint: Annotated[str | None, INTERPRETIVE] = None
    """The viewpoint a read or respond falls back to when the request names none and has no
    actor (FR-RESP-12): ``character:<id>``, never ``omniscient`` (#59 resolves it).
    Interpretive: a Lite turn depends on it (#64)."""

    generation_settings: GenerationSettings | None = None
    """The story's override of the deployment's Lite generation defaults (FR-LITE-4)."""

    lite_window_messages: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=1, le=MAX_LITE_WINDOW_MESSAGES
    )
    """The story's override of how many transcript rows a Lite prompt carries verbatim."""

    guardrail_retry_budget: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_GUARDRAIL_RETRY_BUDGET, ge=0, le=MAX_GUARDRAIL_RETRY_BUDGET
    )
    """How many times a turn whose reply broke an output policy is generated again before the
    safe fallback is used (FR-GUARD-6; #68), over the whole turn; ``0`` goes straight to the
    fallback. Prospective."""

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


# -- structured model results (FR-MODEL-6,7; #53) ------------------------------------------------

DEFAULT_STRUCTURED_RETRY_BUDGET = 2
DEFAULT_UNCERTAIN_BELOW = 0.5


class StructuredSettings(BaseModel):
    """Deployment settings for structured model results (FR-MODEL-6,7), stored under
    ``deployment_state.settings``: retries after the first attempt (prospective, a budget), and
    the ``confidence`` below which a result is ``Uncertain`` (interpretive: it decides whether a
    guardrail verdict counts, #64). Other keys in that document are ignored here."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    structured_retry_budget: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_STRUCTURED_RETRY_BUDGET, ge=0, le=10
    )
    uncertain_below: Annotated[float, INTERPRETIVE] = Field(
        default=DEFAULT_UNCERTAIN_BELOW, ge=0.0, le=1.0
    )


# -- the safe fallback (FR-GUARD-6; #68) --------------------------------------------------------

NAME_PLACEHOLDER = "{name}"
DEFAULT_SAFE_FALLBACK = "{name} changes the subject."
SAFE_FALLBACK_MAX_CHARS = 500


class GuardrailSettings(BaseModel):
    """The deployment's safe fallback line, stored under ``deployment_state.settings`` (#68):
    what a character says when every regenerated reply broke an output policy and it has no
    ``safe_fallback_text`` of its own. ``{name}`` becomes the first render character's name.
    Prospective: a turn uses the line in force when it started."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    default_safe_fallback: Annotated[str, PROSPECTIVE] = Field(
        default=DEFAULT_SAFE_FALLBACK, min_length=1, max_length=SAFE_FALLBACK_MAX_CHARS
    )


# -- the deployment's settings (FR-CONC-14,15; #64) ---------------------------------------------

DEFAULT_MINOR_AGE_THRESHOLD = 18
MINOR_AGE_THRESHOLD_MIN = 0
MINOR_AGE_THRESHOLD_MAX = 120
DEPLOYMENT_COLUMNS = ("default_content_rating_id", "minor_age_threshold")
"""Deployment settings kept in their own ``deployment_state`` columns (#48) rather than in its
``settings`` document."""


class DeploymentSettings(
    LiteDefaults,
    StoryTextCaps,
    IdempotencySettings,
    TurnQueueDefaults,
    StructuredSettings,
    GuardrailSettings,
):
    """Every deployment setting ``/admin/settings`` reads and writes (#64): the two
    ``deployment_state`` columns and the ``settings`` document's sections, each of which earlier
    stories read through its own view (``LiteDefaults``, ``StoryTextCaps``, …)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_content_rating_id: Annotated[str, INTERPRETIVE] = Field(min_length=1, max_length=64)
    """The rating a story created without one gets (FR-RATE-1). Interpretive."""

    minor_age_threshold: Annotated[int, INTERPRETIVE] = Field(
        default=DEFAULT_MINOR_AGE_THRESHOLD, ge=MINOR_AGE_THRESHOLD_MIN, le=MINOR_AGE_THRESHOLD_MAX
    )
    """Below this true age a character is a minor (docs/02 §4.12). Interpretive."""

    def document(self) -> dict[str, Any]:
        """The fields stored in ``deployment_state.settings`` (JSON)."""
        return self.model_dump(mode="json", exclude=set(DEPLOYMENT_COLUMNS))


def merge_patch(target: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """RFC 7396: ``null`` removes a key, an object merges into an object, anything else
    replaces."""
    merged = dict(target)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping):
            current = merged.get(key)
            merged[key] = merge_patch(current if isinstance(current, Mapping) else {}, value)
        else:
            merged[key] = value
    return merged


# -- classification (FR-CONC-15; #64) ------------------------------------------------------------


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The model a field holds (``Model`` or ``Model | None``), else ``None``."""
    candidates = (
        typing.get_args(annotation)
        if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union
        else (annotation,)
    )
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def tag_of(info: FieldInfo) -> ConfigClass | None:
    """The ``ConfigClass`` a field is annotated with, if any."""
    return next((item for item in info.metadata if isinstance(item, ConfigClass)), None)


def leaves(model: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, FieldInfo]]:
    """Every leaf setting of ``model`` as ``(dotted path, field)``, nested models walked."""
    for name, info in model.model_fields.items():
        nested = _nested_model(info.annotation)
        if nested is not None:
            yield from leaves(nested, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", info


def untagged(model: type[BaseModel]) -> list[str]:
    """The leaf settings of ``model`` that carry no ``ConfigClass`` (``test_u_18``)."""
    return [path for path, info in leaves(model) if tag_of(info) is None]


def setting_class(model: type[BaseModel], path: str) -> ConfigClass:
    """The class of the leaf setting at ``path``; untagged or unknown → interpretive."""
    current: type[BaseModel] | None = model
    info: FieldInfo | None = None
    for part in path.split("."):
        if current is None or part not in current.model_fields:
            return ConfigClass.interpretive
        info = current.model_fields[part]
        current = _nested_model(info.annotation)
    if info is None or current is not None:
        return ConfigClass.interpretive
    return tag_of(info) or ConfigClass.interpretive


def flatten(model: type[BaseModel], values: Mapping[str, Any] | None) -> dict[str, Any]:
    """``values`` (a JSON dump of ``model``) as one entry per leaf setting, keyed by dotted path;
    a nested model that is ``None`` contributes ``None`` for each of its leaves."""
    flat: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        value = (values or {}).get(name)
        nested = _nested_model(info.annotation)
        if nested is None:
            flat[name] = value
            continue
        inner = flatten(nested, value if isinstance(value, Mapping) else None)
        flat.update({f"{name}.{key}": item for key, item in inner.items()})
    return flat
