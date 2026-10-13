"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #57 adds the Lite generation settings
(``generation_settings``, ``lite_window_messages``, overriding the deployment's
``LiteDefaults``); #63 adds ``turn_queue_max`` and ``turn_deadline`` (``turn_concurrency`` and
``turn_queue_max`` live in their own ``stories`` columns), #64 tags every setting
interpretive or prospective, and #69 adds ``max_model_calls_per_turn``. Settings are read
when an operation starts (prospective settings never affect work already in flight).

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
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator
from pydantic.fields import FieldInfo

from epistrel.core.seeds.profiles import LITE_PRESET_ID


class ConfigClass(StrEnum):
    """FR-CONC-15: how a setting's change reaches work in flight (see the module docstring)."""

    interpretive = "interpretive"
    prospective = "prospective"


INTERPRETIVE = ConfigClass.interpretive
PROSPECTIVE = ConfigClass.prospective

DEFAULT_REGENERATE_RETRY_BUDGET = 2
DEFAULT_AUDIT_RETENTION_DAYS = 90
AUDIT_RETENTION_MIN_DAYS = 1
AUDIT_RETENTION_MAX_DAYS = 3650
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
"""How many of the latest transcript rows a Lite prompt keeps verbatim before older ones are
summarised (FR-LITE-1,2)."""
MAX_LITE_WINDOW_MESSAGES = 500
DEFAULT_LITE_SUMMARY_CHUNK = 10
"""How many transcript rows one level-0 summary segment covers (FR-LITE-2; #72): two per turn,
so 5 turns."""
MAX_LITE_SUMMARY_CHUNK = 200
DEFAULT_LITE_SUMMARY_MAX_TOKENS = 2_000
"""The rolling summary's token cap (``len(text) // 4``), about a quarter of the 8k context floor;
above it adjacent segments merge into a coarser level (FR-LITE-2; #72)."""
MIN_LITE_SUMMARY_MAX_TOKENS = 100
MAX_LITE_SUMMARY_MAX_TOKENS = 100_000


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
    lite_summary_chunk: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_LITE_SUMMARY_CHUNK, ge=2, le=MAX_LITE_SUMMARY_CHUNK
    )
    """Transcript rows per level-0 summary segment (#72); chunks are whole turns, two rows
    each. Prospective: the next summary job uses the value in force when it starts."""
    lite_summary_max_tokens: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_LITE_SUMMARY_MAX_TOKENS,
        ge=MIN_LITE_SUMMARY_MAX_TOKENS,
        le=MAX_LITE_SUMMARY_MAX_TOKENS,
    )
    """The rolling summary's token cap (#72); above it segments merge. Prospective."""


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
MIN_CALLS_PER_TURN = 1
MIN_DEADLINE_S = 0.5
"""The shortest ``deadline_s`` a request's own budget may ask for (#69)."""
MAX_CALLS_PER_TURN = 50
LITE_BASE_CALLS = 9
"""NFR-PERF-6's ``4 + 5 per primary`` with one primary: the Lite default cap is this plus G."""
COLUMN_SETTINGS = ("turn_concurrency", "turn_queue_max")
"""Settings that live in their own ``stories`` columns (#63) rather than in ``settings``."""


class TurnBudgetDefaults(BaseModel):
    """The deployment's default ``max_model_calls_per_turn`` (NFR-PERF-6; #69), stored under
    ``deployment_state.settings``; a story's own value overrides it. ``None``: the Lite formula,
    ``4 + 5 + G`` (one primary plus the story's model-assisted guardrail calls). Prospective."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_model_calls_per_turn: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=MIN_CALLS_PER_TURN, le=MAX_CALLS_PER_TURN
    )


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

    model_config = ConfigDict(extra="allow", frozen=True, use_attribute_docstrings=True)

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

    max_model_calls_per_turn: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=MIN_CALLS_PER_TURN, le=MAX_CALLS_PER_TURN
    )
    """The most model calls one turn may make (NFR-PERF-6; #69), 1 to 50. ``None``: the
    deployment's default, else the Lite formula ``4 + 5 + G``. A cap below one generation plus
    the story's model-assisted guardrail calls refuses every turn with 422
    ``budget_below_safety_minimum``. Prospective."""

    default_output_viewpoint: Annotated[str | None, INTERPRETIVE] = None
    """The viewpoint a read or respond falls back to when the request names none and has no
    actor (FR-RESP-12): ``character:<id>``, never ``omniscient`` (#59 resolves it).
    Interpretive: a Lite turn depends on it (#64)."""

    generation_settings: GenerationSettings | None = None
    """The story's override of the deployment's Lite generation defaults (FR-LITE-4)."""

    lite_window_messages: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=1, le=MAX_LITE_WINDOW_MESSAGES
    )
    """The story's override of how many transcript rows a Lite prompt keeps verbatim before
    older ones are summarised (#72)."""

    lite_summary_chunk: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=2, le=MAX_LITE_SUMMARY_CHUNK
    )
    """The story's override of the transcript rows per level-0 summary segment (#72)."""

    lite_summary_max_tokens: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=MIN_LITE_SUMMARY_MAX_TOKENS, le=MAX_LITE_SUMMARY_MAX_TOKENS
    )
    """The story's override of the rolling summary's token cap (#72)."""

    guardrail_retry_budget: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_GUARDRAIL_RETRY_BUDGET, ge=0, le=MAX_GUARDRAIL_RETRY_BUDGET
    )
    """How many times a turn whose reply broke an output policy is generated again before the
    safe fallback is used (FR-GUARD-6; #68), over the whole turn; ``0`` goes straight to the
    fallback. Prospective."""

    audit_retention_days: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=AUDIT_RETENTION_MIN_DAYS, le=AUDIT_RETENTION_MAX_DAYS
    )
    """How many days this story's audit records are kept before the ``retention_sweep`` deletes
    them (FR-STORE-30; #71), 1 to 3650. ``None``: the deployment's ``audit_retention_days``
    (default 90). Never affects story history. Prospective."""

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


# -- retention and worker health (FR-STORE-30, NFR-REL-5; #71) ---------------------------------

DEFAULT_JOB_RETENTION_DAYS = 7
DEFAULT_WORKER_HEARTBEAT_TIMEOUT_S = 60.0
DEFAULT_TIMER_LAG_ALERT_S = 300.0


class RetentionSettings(BaseModel):
    """How long the ``retention_sweep`` keeps what may expire (FR-STORE-30; #71), stored under
    ``deployment_state.settings``: audit records (``audit`` events and ``audit_log`` rows) for
    ``audit_retention_days`` (a story's own value overrides it for that story's records), and
    finished (``done``/``failed``) job rows for ``job_retention_days``. Prospective: each sweep
    applies the values in force when it runs; story history never expires."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    audit_retention_days: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_AUDIT_RETENTION_DAYS,
        ge=AUDIT_RETENTION_MIN_DAYS,
        le=AUDIT_RETENTION_MAX_DAYS,
    )
    job_retention_days: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_JOB_RETENTION_DAYS, ge=1, le=AUDIT_RETENTION_MAX_DAYS
    )


class WorkerHealthSettings(BaseModel):
    """When ``/health`` reports ``degraded`` (NFR-REL-5; #71), stored under
    ``deployment_state.settings``: no worker heartbeat within ``worker_heartbeat_timeout``
    seconds, or a timer, recurring task or job overdue by more than ``timer_lag_alert`` seconds.
    Prospective."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    worker_heartbeat_timeout: Annotated[float, PROSPECTIVE] = Field(
        default=DEFAULT_WORKER_HEARTBEAT_TIMEOUT_S, ge=1.0, le=3600.0
    )
    timer_lag_alert: Annotated[float, PROSPECTIVE] = Field(
        default=DEFAULT_TIMER_LAG_ALERT_S, ge=1.0, le=86_400.0
    )


# -- media assets (FR-MEDIA-1,2; #105) ----------------------------------------------------------

DEFAULT_MEDIA_ALLOWED_TYPES = ("image/png", "image/jpeg", "image/webp", "video/mp4")
DEFAULT_MEDIA_MAX_BYTES = 50 * 1024 * 1024
MEDIA_MAX_BYTES_LIMIT = 10 * 1024 * 1024 * 1024
DEFAULT_MEDIA_MODERATION_TIMEOUT_MS = 5_000
MEDIA_MODERATION_TIMEOUT_MIN_MS = 100
MEDIA_MODERATION_TIMEOUT_MAX_MS = 60_000
DEFAULT_MEDIA_SWEEP_INTERVAL_S = 3_600
MEDIA_SWEEP_INTERVAL_MIN_S = 60
MEDIA_SWEEP_INTERVAL_MAX_S = 7 * 86_400
DEFAULT_MEDIA_SIGNED_URL_TTL_S = 300
MEDIA_SIGNED_URL_TTL_MIN_S = 1
MEDIA_SIGNED_URL_TTL_MAX_S = 86_400
DEFAULT_MEDIA_MULTIPART_THRESHOLD = 8 * 1024 * 1024
MEDIA_MULTIPART_THRESHOLD_MIN = 1024
DEFAULT_MEDIA_UPLOAD_TTL_S = 86_400
MEDIA_UPLOAD_TTL_MIN_S = 60
MEDIA_UPLOAD_TTL_MAX_S = 7 * 86_400
URL_MAX_CHARS = 2_048
MIME_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}$"
"""A MIME type as ``media_allowed_types`` lists it: ``type/subtype``, lowercase, no
parameters."""

MimeType = Annotated[str, Field(pattern=MIME_TYPE_PATTERN)]


def _http_url(value: str | None) -> str | None:
    """``value`` when it is an absolute ``http``/``https`` URL with a host; ``ValueError``
    otherwise."""
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or any(ord(c) < 0x21 or ord(c) == 0x7F for c in value)
    ):
        raise ValueError("must be an absolute http:// or https:// URL")
    return value


HttpUrl = Annotated[str, Field(max_length=URL_MAX_CHARS), AfterValidator(_http_url)]


class MediaSettings(BaseModel):
    """The deployment's media settings, stored under ``deployment_state.settings`` (#105):
    which MIME types an upload may be (``media_allowed_types``) and how large
    (``media_max_bytes``); the optional moderation hook every upload is sent to before it is
    stored (``media_moderation_webhook_url``; ``null``: none) and how long it may take
    (``media_moderation_timeout_ms``); how often the media sweep runs (``media_sweep_interval``,
    seconds); and the base URL the hook's ``content_url`` and ``GET /media/{id}/url``'s signed
    URLs start with (``public_base_url``; ``null``: the hook's URL is relative to Epistrel's root,
    and a signed URL cannot be made). From #107 (FR-MEDIA-3): how long a signed URL works
    (``media_signed_url_ttl``, seconds), the size above which an upload must use a resumable
    session (``media_multipart_threshold``, bytes), and how long an upload session lives after its
    last activity (``media_upload_ttl``, seconds). Prospective: an upload or sweep uses the
    values in force when it starts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    media_allowed_types: Annotated[list[MimeType], PROSPECTIVE] = Field(
        default_factory=lambda: list(DEFAULT_MEDIA_ALLOWED_TYPES), min_length=1, max_length=50
    )
    media_max_bytes: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MAX_BYTES, ge=1, le=MEDIA_MAX_BYTES_LIMIT
    )
    media_moderation_webhook_url: Annotated[HttpUrl | None, PROSPECTIVE] = None
    media_moderation_timeout_ms: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MODERATION_TIMEOUT_MS,
        ge=MEDIA_MODERATION_TIMEOUT_MIN_MS,
        le=MEDIA_MODERATION_TIMEOUT_MAX_MS,
    )
    media_sweep_interval: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_SWEEP_INTERVAL_S,
        ge=MEDIA_SWEEP_INTERVAL_MIN_S,
        le=MEDIA_SWEEP_INTERVAL_MAX_S,
    )
    public_base_url: Annotated[HttpUrl | None, PROSPECTIVE] = None
    media_signed_url_ttl: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_SIGNED_URL_TTL_S,
        ge=MEDIA_SIGNED_URL_TTL_MIN_S,
        le=MEDIA_SIGNED_URL_TTL_MAX_S,
    )
    media_multipart_threshold: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MULTIPART_THRESHOLD,
        ge=MEDIA_MULTIPART_THRESHOLD_MIN,
        le=MEDIA_MAX_BYTES_LIMIT,
    )
    media_upload_ttl: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_UPLOAD_TTL_S, ge=MEDIA_UPLOAD_TTL_MIN_S, le=MEDIA_UPLOAD_TTL_MAX_S
    )

    @field_validator("media_allowed_types")
    @classmethod
    def _unique_types(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("lists a MIME type more than once")
        return value


MEDIA_SWEEP_TASK = "media_sweep"
"""The media sweep's recurring task (and job kind; ``orchestrator.jobs.handlers.media_sweep``)."""

RECURRING_INTERVAL_SETTINGS: Mapping[str, str] = {"media_sweep_interval": MEDIA_SWEEP_TASK}
"""Deployment settings (seconds) that set a recurring task's interval, by setting: the task's
name. A settings write moves the task's ``recurring_tasks`` row, and every worker's start-up
reconcile reads the setting (``orchestrator.jobs.timers.reconcile``)."""


# -- the deployment's settings (FR-CONC-14,15; #64) ---------------------------------------------

DEFAULT_MINOR_AGE_THRESHOLD = 18
MINOR_AGE_THRESHOLD_MIN = 0
MINOR_AGE_THRESHOLD_MAX = 120
DEFAULT_MAX_CHARACTERS_AT_START = 16
"""FR-SCN-4's deployment default for the starting cast, player slots included (#108)."""
CAST_LIMIT_MIN = 1
CAST_LIMIT_MAX = 1_000
"""The bounds of every starting-cast limit: the deployment's default and ceiling, a scenario's
own value and (#119) a story creation's override."""
DEPLOYMENT_COLUMNS = (
    "default_content_rating_id",
    "minor_age_threshold",
    "max_characters_at_start_default",
    "max_characters_at_start_ceiling",
    "default_engine_profile_id",
)
"""Deployment settings kept in their own ``deployment_state`` columns (#48; the starting-cast
limits, #108; the default engine profile, #106) rather than in its ``settings`` document."""


class DeploymentSettings(
    LiteDefaults,
    StoryTextCaps,
    IdempotencySettings,
    TurnQueueDefaults,
    TurnBudgetDefaults,
    StructuredSettings,
    GuardrailSettings,
    RetentionSettings,
    WorkerHealthSettings,
    MediaSettings,
):
    """Every deployment setting ``/admin/settings`` reads and writes (#64): the
    ``deployment_state`` columns (``DEPLOYMENT_COLUMNS``) and the ``settings`` document's
    sections, each of which earlier stories read through its own view (``LiteDefaults``,
    ``StoryTextCaps``, …)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_content_rating_id: Annotated[str, INTERPRETIVE] = Field(min_length=1, max_length=64)
    """The rating a story created without one gets (FR-RATE-1). Interpretive."""

    minor_age_threshold: Annotated[int, INTERPRETIVE] = Field(
        default=DEFAULT_MINOR_AGE_THRESHOLD, ge=MINOR_AGE_THRESHOLD_MIN, le=MINOR_AGE_THRESHOLD_MAX
    )
    """Below this true age a character is a minor (docs/02 §4.12). Interpretive."""

    max_characters_at_start_default: Annotated[int, PROSPECTIVE] = Field(
        default=DEFAULT_MAX_CHARACTERS_AT_START, ge=CAST_LIMIT_MIN, le=CAST_LIMIT_MAX
    )
    """The most character slots, player slots included, a story created from a scenario starts
    with when the scenario sets none (FR-SCN-4; default 16). Prospective: a story created after
    a change uses the new value."""

    max_characters_at_start_ceiling: Annotated[int | None, PROSPECTIVE] = Field(
        default=None, ge=CAST_LIMIT_MIN, le=CAST_LIMIT_MAX
    )
    """An optional deployment-wide ceiling (``null``: none) on every starting-cast limit: a
    scenario saved above it is 422 ``cast_limit_exceeded``, and a story creation's override is
    capped by it (FR-SCN-4). Lowering it below a stored scenario's value is allowed; validation
    and story creation catch that scenario. Prospective."""

    default_engine_profile_id: Annotated[str, PROSPECTIVE] = Field(
        default=LITE_PRESET_ID, min_length=1, max_length=64
    )
    """The engine profile a story is created under when it names none (FR-PROF-1; #106; the
    Lite preset until an operator chooses another). It must be a profile that exists and is not
    deleted (422 ``profile_deleted``), and the default cannot be deleted. Prospective: a story
    created after a change runs under the new default; existing stories keep theirs."""

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
