"""Per-story settings: the typed view of ``stories.settings`` (FR-CONC-15).

#47 creates the schema with the one setting it needs; #50 adds the two a story is created with
(``turn_concurrency``, ``default_output_viewpoint``); #57 adds the Lite generation settings
(``generation_settings``, ``lite_window_messages``, overriding the deployment's
``LiteDefaults``); #63 adds ``turn_queue_max`` and ``turn_deadline`` (``turn_concurrency`` and
``turn_queue_max`` live in their own ``stories`` columns), #64 tags every setting
interpretive or prospective, and #69 adds ``max_model_calls_per_turn``. Settings are read
when an operation starts (prospective settings never affect work already in flight).

**Engine dials live in the engine profile (FR-PROF-5; #113).** A story's settings describe the
story (FR-SCN-2's story settings defaults): its concurrency, its default output viewpoint, its
revalidation budget, its summary chunking and its audit retention. How hard the engine works is
the story's engine profile, with the deployment's ``profile_defaults`` under it and the story's
own overrides (``story_profile_overrides``) over it (``core.profiles.effective``). #113 moved
M2's engine dials there: the generation settings, the verbatim window, the summary's token cap,
the per-turn call cap and deadline, and the polish budget (``SUPERSEDED_STORY_SETTINGS``,
``SUPERSEDED_DEPLOYMENT_SETTINGS``). A write naming one of them is 422, and the error names the
profile setting that replaced it.

**Classification (FR-CONC-15, #64).** Every leaf setting of ``StorySettings`` and
``DeploymentSettings`` carries ``Annotated[..., ConfigClass.interpretive]`` or
``Annotated[..., ConfigClass.prospective]``: an *interpretive* change applies to every operation
that commits after it (a turn in flight is re-checked under it at commit, FR-CONC-16), a
*prospective* one only to operations that start after it. ``setting_class`` reads the tag; an
untagged setting is interpretive (``test_u_18`` fails the build when one is missing).
"""

from __future__ import annotations

import copy
import types
import typing
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    StrictBool,
    StrictFloat,
    StrictInt,
    WithJsonSchema,
    field_validator,
)
from pydantic.fields import FieldInfo

from epistrel.core.context import CONTROL_CHARACTERS as _CONTROLS
from epistrel.core.errors import InvalidRequest
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.seeds.profiles import profile_defaults as seeded_profile_defaults


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
"""M2's default token cap of the rolling summary (``lite_summary_max_tokens``, #72: above it
adjacent segments merge into a coarser level, FR-LITE-2). #113 moved the cap to the profile's
``memory.rolling_summary.compress_threshold_tokens``, whose Lite value is 6,000."""
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
"""M2's Lite generation defaults, which the Lite preset's ``models.generation`` carries (#106);
``resolve_generation`` falls back to its length when no layer names one."""


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
    """Apply ``layers`` lowest first (the story's effective configuration, then the request,
    FR-LITE-4): each set key wins."""
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
    """The deployment's Lite summary chunking, stored under ``deployment_state.settings`` (#57,
    #72). The generation defaults, the verbatim window and the summary's token cap moved to the
    deployment's ``profile_defaults`` (#113)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    lite_summary_chunk: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_LITE_SUMMARY_CHUNK, ge=2, le=MAX_LITE_SUMMARY_CHUNK
    )
    """Transcript rows per level-0 summary segment (#72); chunks are whole turns, two rows
    each. Prospective: the next summary job uses the value in force when it starts."""


# -- story-level text caps (FR-LITE-3; #60) ------------------------------------------------------

DEFAULT_MAX_PINNED_MEMORIES = 50
MAX_PINNED_MEMORIES_LIMIT = 500
DEFAULT_MAX_CUSTOM_INSTRUCTIONS_CHARS = 4_000
MAX_CUSTOM_INSTRUCTIONS_CHARS_LIMIT = 20_000
PINNED_ITEM_MAX_CHARS = 2_000
"""The longest pinned memory, in code points after NFC normalisation and trimming."""


class StoryTextCaps(BaseModel):
    """The deployment's caps on pinned memories and custom instructions, stored under
    ``deployment_state.settings`` (#60). ``max_pinned_memories`` is a ceiling: a story may hold
    no more pinned memories than it, nor than its effective configuration's ``pins.max_pinned``
    (#113). Prospective: an edit is checked against the caps in force when it starts, and a
    lowered cap never touches what is already stored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_pinned_memories: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MAX_PINNED_MEMORIES, ge=1, le=MAX_PINNED_MEMORIES_LIMIT
    )
    max_custom_instructions_chars: Annotated[StrictInt, PROSPECTIVE] = Field(
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

    idempotency_result_window: Annotated[StrictInt, PROSPECTIVE] = Field(
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
"""A turn's default deadline in seconds (NFR-PERF-6): the Lite preset's ``budgets.deadline_ms``
(45,000) and the deadline of work outside a turn's budget."""
MAX_TURN_DEADLINE_S = 600.0
MIN_CALLS_PER_TURN = 1
MIN_DEADLINE_S = 0.5
"""The shortest ``deadline_s`` a request's own budget may ask for (#69)."""
MAX_CALLS_PER_TURN = 50
LITE_BASE_CALLS = 9
"""NFR-PERF-6's ``4 + 5 per primary`` with one primary: the Lite default cap is this plus G."""
COLUMN_SETTINGS = ("turn_concurrency", "turn_queue_max")
"""Settings that live in their own ``stories`` columns (#63) rather than in ``settings``."""

SUPERSEDED_STORY_SETTINGS: Mapping[str, str] = types.MappingProxyType(
    {
        "generation_settings": "models.generation",
        "lite_window_messages": "memory.rolling_summary.window_messages",
        "lite_summary_max_tokens": "memory.rolling_summary.compress_threshold_tokens",
        "max_model_calls_per_turn": "budgets.model_calls",
        "turn_deadline": "budgets.deadline_ms",
        "guardrail_retry_budget": "repair.polish.max_regenerations",
    }
)
"""M2's story settings that were engine dials, with the profile setting that replaced each
(FR-PROF-5; #113): a story overrides it through ``PATCH /stories/{id}/profile-overrides``. A
story settings write naming one is 422. ``turn_deadline`` was seconds; ``budgets.deadline_ms``
is milliseconds."""


def superseded(key: str, field: str) -> InvalidRequest:
    """The 422 for one of M2's story engine dials (``SUPERSEDED_STORY_SETTINGS``), naming the
    profile setting that replaced it (``details.replacement``, ``reason: superseded``)."""
    replacement = SUPERSEDED_STORY_SETTINGS[key]
    return InvalidRequest(
        f"{key!r} is an engine dial of the story's profile now: override "
        f"{replacement} with PATCH /stories/{{id}}/profile-overrides",
        {"field": field, "reason": "superseded", "replacement": replacement},
    )


SUPERSEDED_DEPLOYMENT_SETTINGS: Mapping[str, str] = types.MappingProxyType(
    {
        "lite_generation_defaults": "models.generation",
        "lite_window_messages": "memory.rolling_summary.window_messages",
        "lite_summary_max_tokens": "memory.rolling_summary.compress_threshold_tokens",
        "max_model_calls_per_turn": "budgets.model_calls",
    }
)
"""M2's deployment settings that were engine dials, with the ``profile_defaults`` setting that
replaced each (FR-PROF-5; #113). ``PUT /admin/settings`` naming one is 422."""


class TurnQueueDefaults(BaseModel):
    """The deployment's default ``turn_queue_max``, stored under ``deployment_state.settings``
    (#63; #64 exposes it). A story's own ``turn_queue_max`` overrides it. Prospective."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    turn_queue_max: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_TURN_QUEUE_MAX, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT
    )


CHARACTER_VIEWPOINT_PREFIX = "character:"
OMNISCIENT = "omniscient"


class StorySettings(BaseModel):
    """A story's settings: what the story is, never how hard the engine works (FR-PROF-5; the
    engine dials are its profile's, ``SUPERSEDED_STORY_SETTINGS``). Unknown keys are kept so
    later stories' settings survive a read."""

    model_config = ConfigDict(extra="allow", frozen=True, use_attribute_docstrings=True)

    regenerate_retry_budget: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_REGENERATE_RETRY_BUDGET, ge=0, le=10
    )
    """How many times a turn whose read set was touched is regenerated before the call fails
    with ``revision_conflict`` (FR-CONC-4c). Prospective."""

    turn_concurrency: Annotated[TurnConcurrency, PROSPECTIVE] = "reject"
    """Same-story turn concurrency (FR-CONC-5): ``reject`` refuses an overlapping turn as busy
    (423 ``story_busy``), ``queue`` queues it in a per-story FIFO (#63). Stored in its own
    ``stories`` column. Prospective: a change applies to requests that arrive after it."""

    turn_queue_max: Annotated[StrictInt | None, PROSPECTIVE] = Field(
        default=None, ge=TURN_QUEUE_MAX_MIN, le=TURN_QUEUE_MAX_LIMIT
    )
    """How many turns may wait behind the running one under ``queue`` (``None``: the
    deployment's ``turn_queue_max``, default 5); one more is 423 ``story_busy``. Stored in its
    own ``stories`` column. Prospective."""

    default_output_viewpoint: Annotated[str | None, INTERPRETIVE] = None
    """The viewpoint a read or respond falls back to when the request names none and has no
    actor (FR-RESP-12): ``character:<id>``, never ``omniscient`` (#59 resolves it).
    Interpretive: a Lite turn depends on it (#64)."""

    lite_summary_chunk: Annotated[StrictInt | None, PROSPECTIVE] = Field(
        default=None, ge=2, le=MAX_LITE_SUMMARY_CHUNK
    )
    """The story's override of the transcript rows per level-0 summary segment (#72)."""

    audit_retention_days: Annotated[StrictInt | None, PROSPECTIVE] = Field(
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

    structured_retry_budget: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_STRUCTURED_RETRY_BUDGET, ge=0, le=10
    )
    uncertain_below: Annotated[StrictFloat, INTERPRETIVE] = Field(
        default=DEFAULT_UNCERTAIN_BELOW, ge=0.0, le=1.0
    )


# -- the safe fallback (FR-GUARD-6; #68) --------------------------------------------------------

NAME_PLACEHOLDER = "{name}"
DEFAULT_SAFE_FALLBACK = "{name} changes the subject."
SAFE_FALLBACK_MAX_CHARS = 500


DEFAULT_RATING_CHECK_MODEL_CALLS = 2
"""Model calls a rating tightening's history check may spend per record (#118)."""
RATING_CHECK_MODEL_CALLS_MAX = 100


class GuardrailSettings(BaseModel):
    """The deployment's safe fallback line, stored under ``deployment_state.settings`` (#68):
    what a character says when every regenerated reply broke an output policy and it has no
    ``safe_fallback_text`` of its own. ``{name}`` becomes the first render character's name.
    Prospective: a turn uses the line in force when it started.

    ``rating_check_model_calls_per_record`` (#118): how many ``guardrail_check`` calls a
    tightening's history check may spend on one record's model-assisted policies, outside any
    turn budget (FR-RATE-7); a policy it cannot afford follows its ``on_uncertain``.
    Prospective: a check reads the value when it runs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    default_safe_fallback: Annotated[str, PROSPECTIVE] = Field(
        default=DEFAULT_SAFE_FALLBACK, min_length=1, max_length=SAFE_FALLBACK_MAX_CHARS
    )
    rating_check_model_calls_per_record: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_RATING_CHECK_MODEL_CALLS, ge=0, le=RATING_CHECK_MODEL_CALLS_MAX
    )


# -- retention and worker health (FR-STORE-30, NFR-REL-5; #71) ---------------------------------

DEFAULT_JOB_RETENTION_DAYS = 7
DEFAULT_WORKER_HEARTBEAT_TIMEOUT_S = 60.0
DEFAULT_TIMER_LAG_ALERT_S = 300.0


DEFAULT_OFFBOARDING_REPORT_RETENTION_DAYS = 90
"""How long a finished offboarding's report is kept (#117)."""
DEFAULT_RATING_CHECK_RETENTION_DAYS = 30
"""How long a finished rating check's status and violations are kept (#118)."""
DEFAULT_READSET_CHANGE_RETENTION_DAYS = 7
"""How long the structured read set's change index keeps a row (#136): a turn whose base is older
than that cannot rebase and conflicts instead."""


class RetentionSettings(BaseModel):
    """How long the ``retention_sweep`` keeps what may expire (FR-STORE-30; #71), stored under
    ``deployment_state.settings``: audit records (``audit`` events and ``audit_log`` rows) for
    ``audit_retention_days`` (a story's own value overrides it for that story's records), and
    finished (``done``/``failed``) job rows for ``job_retention_days``, and a finished principal
    offboarding's status and report (``offboarding_jobs``, #117) for
    ``offboarding_report_retention_days``, and a finished rating check's status and violations
    (``rating_check_jobs``, #118) for ``rating_check_retention_days``, and the structured read
    set's change index (``proposition_changes``, #136) for ``readset_change_retention_days``.
    Prospective: each sweep applies the values in force when it runs; story history never
    expires."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    audit_retention_days: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_AUDIT_RETENTION_DAYS,
        ge=AUDIT_RETENTION_MIN_DAYS,
        le=AUDIT_RETENTION_MAX_DAYS,
    )
    job_retention_days: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_JOB_RETENTION_DAYS, ge=1, le=AUDIT_RETENTION_MAX_DAYS
    )
    offboarding_report_retention_days: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_OFFBOARDING_REPORT_RETENTION_DAYS, ge=1, le=AUDIT_RETENTION_MAX_DAYS
    )
    rating_check_retention_days: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_RATING_CHECK_RETENTION_DAYS, ge=1, le=AUDIT_RETENTION_MAX_DAYS
    )
    readset_change_retention_days: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_READSET_CHANGE_RETENTION_DAYS, ge=1, le=AUDIT_RETENTION_MAX_DAYS
    )


class WorkerHealthSettings(BaseModel):
    """When ``/health`` reports ``degraded`` (NFR-REL-5; #71), stored under
    ``deployment_state.settings``: no worker heartbeat within ``worker_heartbeat_timeout``
    seconds, or a timer, recurring task or job overdue by more than ``timer_lag_alert`` seconds.
    Prospective."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    worker_heartbeat_timeout: Annotated[StrictFloat, PROSPECTIVE] = Field(
        default=DEFAULT_WORKER_HEARTBEAT_TIMEOUT_S, ge=1.0, le=3600.0
    )
    timer_lag_alert: Annotated[StrictFloat, PROSPECTIVE] = Field(
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
    media_max_bytes: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MAX_BYTES, ge=1, le=MEDIA_MAX_BYTES_LIMIT
    )
    media_moderation_webhook_url: Annotated[HttpUrl | None, PROSPECTIVE] = None
    media_moderation_timeout_ms: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MODERATION_TIMEOUT_MS,
        ge=MEDIA_MODERATION_TIMEOUT_MIN_MS,
        le=MEDIA_MODERATION_TIMEOUT_MAX_MS,
    )
    media_sweep_interval: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_SWEEP_INTERVAL_S,
        ge=MEDIA_SWEEP_INTERVAL_MIN_S,
        le=MEDIA_SWEEP_INTERVAL_MAX_S,
    )
    public_base_url: Annotated[HttpUrl | None, PROSPECTIVE] = None
    media_signed_url_ttl: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_SIGNED_URL_TTL_S,
        ge=MEDIA_SIGNED_URL_TTL_MIN_S,
        le=MEDIA_SIGNED_URL_TTL_MAX_S,
    )
    media_multipart_threshold: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MEDIA_MULTIPART_THRESHOLD,
        ge=MEDIA_MULTIPART_THRESHOLD_MIN,
        le=MEDIA_MAX_BYTES_LIMIT,
    )
    media_upload_ttl: Annotated[StrictInt, PROSPECTIVE] = Field(
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


# -- catalog validation (FR-SCN-5, FR-GUARD-10; #111) ------------------------------------------

DEFAULT_VALIDATION_MODEL_CALLS = 0
MAX_VALIDATION_MODEL_CALLS = 1_000


class CatalogValidationSettings(BaseModel):
    """How a catalog validation (a publish, or a dry run) runs, stored under
    ``deployment_state.settings``:

    - ``validation_model_calls`` (#111): how many model calls it may spend on model-assisted
      input policies, one call per policy and text, default 0. Deterministic policies always
      run; model-assisted ones the budget does not reach are listed ``unavailable``
      (``input_policy_model_assisted``) and run when a story is created from the scenario,
      inside its creation budget (#119).
    - ``require_primary_image`` (FR-CAT-4; #115): whether publishing a catalog character needs
      a primary image (``primary_image_required`` otherwise). Off by default, so a text-only or
      accessibility-focused deployment never needs an image; a visual product turns it on.

    Prospective: a validation uses the values in force when it starts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validation_model_calls: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_VALIDATION_MODEL_CALLS, ge=0, le=MAX_VALIDATION_MODEL_CALLS
    )
    require_primary_image: Annotated[bool, PROSPECTIVE] = False


# -- message feedback (FR-FB-1,2; #112) -----------------------------------------------------------

DEFAULT_FEEDBACK_TAGS = (
    "out_of_character",
    "repetitive",
    "forgot_something",
    "factual_error",
    "ignored_my_action",
    "too_long",
    "too_short",
    "should_have_been_blocked",
    "other",
)
"""FR-FB-1's nine seeded reason tags: a deployment starts with them and may add its own
(``feedback_tags``)."""
FEEDBACK_TAG_MAX_CHARS = 40
FEEDBACK_TAG_PATTERN = rf"^[a-z0-9_]{{1,{FEEDBACK_TAG_MAX_CHARS}}}$"
"""A deployment's reason tag: 1 to 40 of ``a-z``, ``0-9`` and ``_``."""
FEEDBACK_TAGS_MAX = 50
DEFAULT_FEEDBACK_TEXT_MAX_CHARS = 2_000
FEEDBACK_TEXT_MAX_CHARS_LIMIT = 20_000
StoryLifetime = Literal["story_lifetime"]
STORY_LIFETIME: StoryLifetime = "story_lifetime"
"""``feedback_retention``'s default: a rating is kept until its story is deleted."""
FEEDBACK_RETENTION_MAX_DAYS = AUDIT_RETENTION_MAX_DAYS

FeedbackTag = Annotated[str, Field(pattern=FEEDBACK_TAG_PATTERN)]
"""One reason tag of the deployment's set (``feedback_tags``)."""


def feedback_retention_value(value: Any) -> int | str:
    """``story_lifetime``, or a number of days from 1 to 3650: an integer, never a boolean or a
    numeric string. ``ValueError`` (a 422 at the setting itself) for anything else."""
    if value == STORY_LIFETIME and isinstance(value, str):
        return STORY_LIFETIME
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and AUDIT_RETENTION_MIN_DAYS <= value <= FEEDBACK_RETENTION_MAX_DAYS
    ):
        return value
    raise ValueError(
        f"must be {STORY_LIFETIME!r} or a number of days from {AUDIT_RETENTION_MIN_DAYS} to "
        f"{FEEDBACK_RETENTION_MAX_DAYS}"
    )


FeedbackRetention = Annotated[
    int | str,
    PlainValidator(feedback_retention_value),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "enum": [STORY_LIFETIME]},
                {
                    "type": "integer",
                    "minimum": AUDIT_RETENTION_MIN_DAYS,
                    "maximum": FEEDBACK_RETENTION_MAX_DAYS,
                },
            ]
        }
    ),
]
"""How long feedback is kept: ``story_lifetime``, or a number of days (1 to 3650). One
validator decides, so a refusal is reported at the setting itself."""


DEFAULT_FEEDBACK_REGEN_WINDOW_S = 300
FEEDBACK_REGEN_WINDOW_MIN_S = 1
FEEDBACK_REGEN_WINDOW_MAX_S = 86_400


class FeedbackSettings(BaseModel):
    """Message feedback (FR-FB-1,2; #112), stored under ``deployment_state.settings``:
    ``feedback_text_max_chars``, the most characters a rating's free text may hold once trimmed
    (default 2000; a longer text is 422), and ``feedback_retention``, how long ratings and their
    history are kept: ``story_lifetime`` (the default: until the story is deleted) or a number of
    days, after which the ``retention_sweep`` deletes a rating by when it was first given and a
    history entry by when it was replaced. Feedback has its own retention, never the audit
    records' (FR-STORE-30). Both are prospective: a rating uses the cap in force when it is
    written, and each sweep the retention in force when it runs. ``feedback_export`` (#126)
    switches the operator's export of labelled cases on (off by default), and
    ``feedback_regen_window_s`` (#126) is the window of the aggregates' ``regen_after_low_rating``
    signal."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    feedback_text_max_chars: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_FEEDBACK_TEXT_MAX_CHARS, ge=1, le=FEEDBACK_TEXT_MAX_CHARS_LIMIT
    )
    feedback_retention: Annotated[FeedbackRetention, PROSPECTIVE] = STORY_LIFETIME
    feedback_export: Annotated[StrictBool, PROSPECTIVE] = False
    """Whether ``POST /admin/feedback/export`` may export rated messages as labelled cases
    (FR-FB-4; #126). Off by default: the route answers 403 ``feedback_export_disabled``."""
    feedback_regen_window_s: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_FEEDBACK_REGEN_WINDOW_S,
        ge=FEEDBACK_REGEN_WINDOW_MIN_S,
        le=FEEDBACK_REGEN_WINDOW_MAX_S,
    )
    """How soon after a rating of 1 or 2 a regeneration of the rated message counts as
    ``regen_after_low_rating`` in the feedback aggregates (FR-FB-3; #126), in seconds (default
    300). Each report uses the value in force when it is read."""


# -- erasure (FR-ADMIN-6, FR-MEDIA-2; #124) ----------------------------------------------------

ErasureSharedMedia = Literal["purge", "retire"]
DEFAULT_ERASURE_SHARED_MEDIA: ErasureSharedMedia = "purge"


class ErasureSettings(BaseModel):
    """What a catalog-scope erasure does with a media asset that depicts or names only the
    erased subject but that a record the erasure leaves untouched still references (a story's
    snapshot, another catalog entry), stored under ``deployment_state.settings`` (#124; the
    owner's decision on #124): ``purge`` (the default) deletes the row and, when no other asset
    shares them, its bytes, so the reference dangles; ``retire`` keeps the row and its bytes
    ``retired``, with the subject removed from ``depicted_subjects`` and its caption and alt
    text redacted. An asset nothing else references is always purged. Interpretive: it decides
    what an erasure removes."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    erasure_shared_media: Annotated[ErasureSharedMedia, INTERPRETIVE] = DEFAULT_ERASURE_SHARED_MEDIA


DEFAULT_IMPORTANCE_HALF_LIFE_TURNS = 200
"""A memory's importance halves every this many committed turns (FR-MEM-7; #135)."""
IMPORTANCE_HALF_LIFE_MAX = 1_000_000


class MemorySettings(BaseModel):
    """How memory importance decays (FR-MEM-7; #135), stored under
    ``deployment_state.settings``: ``memory_importance_half_life_turns`` committed turns of the
    active chain halve a memory's importance (``core.memory.records.decayed_importance``, read
    by #141's ranking and #147's retirement). Prospective: a ranking or retirement run reads
    the value when it starts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    memory_importance_half_life_turns: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_IMPORTANCE_HALF_LIFE_TURNS, ge=1, le=IMPORTANCE_HALF_LIFE_MAX
    )


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

# -- catalog listing status (FR-CAT-11; #110) ---------------------------------------------------

DEFAULT_LISTING_STATUS_VALUES = ("private", "in_review", "public")
"""The listing status values a deployment starts with; a new catalog entry takes the first."""
LISTING_STATUS_VALUE_MAX = 40
LISTING_STATUS_VALUES_MAX = 50
LISTING_STATUS_PATTERN = (
    rf"^[\t ]*[^{_CONTROLS}\t ]([^{_CONTROLS}]{{0,{LISTING_STATUS_VALUE_MAX - 2}}}"
    rf"[^{_CONTROLS}\t ])?[\t ]*$"
)
"""A listing status value as sent: 1 to 40 characters without control characters (C0, DEL or
C1), between optional spaces and tabs (which are trimmed). ``listing_status_value`` adds the
rest of the rule, which a pattern cannot state: every character is printable. Case-sensitive:
``Public`` is not ``public``."""


def listing_status_value(value: str) -> str:
    """A listing status value as sent (``LISTING_STATUS_PATTERN`` already matched), as stored:
    trimmed of the spaces and tabs the pattern lets surround it. ``ValueError`` unless every
    character is printable (``str.isprintable``): a format character (a zero-width space, a
    direction override) or a separator other than the space (a no-break space) is refused, so
    two values cannot differ by one of those alone."""
    trimmed = value.strip(" \t")
    if not trimmed.isprintable():
        raise ValueError(
            "a listing status value holds printable characters only: no control or format "
            "character, and no separator but the space"
        )
    return trimmed


ListingStatusValue = Annotated[
    str, Field(pattern=LISTING_STATUS_PATTERN), AfterValidator(listing_status_value)
]
"""One listing status value (FR-CAT-11): consumer-defined, never interpreted; 1 to 40 printable
characters, trimmed."""

DEPLOYMENT_COLUMNS = (
    "default_content_rating_id",
    "minor_age_threshold",
    "max_characters_at_start_default",
    "max_characters_at_start_ceiling",
    "default_engine_profile_id",
    "listing_status_values",
    "feedback_tags",
    "profile_defaults",
)
"""Deployment settings kept in their own ``deployment_state`` columns (#48; the starting-cast
limits, #108; the default engine profile, #106; the listing status values, #110; the feedback
reason tags, #112; the engine profiles' defaults, #113) rather than in its ``settings``
document."""

PROFILE_DEFAULTS = "profile_defaults"


def _profile_settings_schema(schema: dict[str, Any]) -> None:
    """``profile_defaults``' JSON Schema: the delivered profile settings schema (read when the
    schema is generated, as ``core.profiles.schema`` imports this module)."""
    from epistrel.core.profiles.schema import SETTINGS_SCHEMA  # here: it imports this module

    schema.clear()
    schema.update(copy.deepcopy(SETTINGS_SCHEMA))


class DeploymentSettings(
    LiteDefaults,
    StoryTextCaps,
    IdempotencySettings,
    TurnQueueDefaults,
    StructuredSettings,
    GuardrailSettings,
    RetentionSettings,
    WorkerHealthSettings,
    MediaSettings,
    CatalogValidationSettings,
    FeedbackSettings,
    ErasureSettings,
    MemorySettings,
):
    """Every deployment setting ``/admin/settings`` reads and writes (#64): the
    ``deployment_state`` columns (``DEPLOYMENT_COLUMNS``) and the ``settings`` document's
    sections, each of which earlier stories read through its own view (``LiteDefaults``,
    ``StoryTextCaps``, …)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_content_rating_id: Annotated[str, INTERPRETIVE] = Field(min_length=1, max_length=64)
    """The rating a story created without one gets (FR-RATE-1). Interpretive."""

    minor_age_threshold: Annotated[StrictInt, INTERPRETIVE] = Field(
        default=DEFAULT_MINOR_AGE_THRESHOLD, ge=MINOR_AGE_THRESHOLD_MIN, le=MINOR_AGE_THRESHOLD_MAX
    )
    """Below this true age a character is a minor (docs/02 §4.12). Interpretive."""

    max_characters_at_start_default: Annotated[StrictInt, PROSPECTIVE] = Field(
        default=DEFAULT_MAX_CHARACTERS_AT_START, ge=CAST_LIMIT_MIN, le=CAST_LIMIT_MAX
    )
    """The most character slots, player slots included, a story created from a scenario starts
    with when the scenario sets none (FR-SCN-4; default 16). Prospective: a story created after
    a change uses the new value."""

    max_characters_at_start_ceiling: Annotated[StrictInt | None, PROSPECTIVE] = Field(
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

    listing_status_values: Annotated[list[ListingStatusValue], PROSPECTIVE] = Field(
        default_factory=lambda: list(DEFAULT_LISTING_STATUS_VALUES),
        min_length=1,
        max_length=LISTING_STATUS_VALUES_MAX,
    )
    """The listing status values a catalog character or scenario may hold (FR-CAT-11; #110),
    in order: a new entry starts at the first. Consumer-defined publishing-workflow values that
    Epistrel never interprets: they restrict no read, no story creation and no transition
    (FR-AUTH-8). Each is 1 to 40 printable characters (trimmed of surrounding spaces and tabs;
    no control or format character, and no separator but the space), the list holds no value
    twice, and the values are case-sensitive. A write replaces the whole list (a
    reorder is a change); removing a value that any catalog entry still holds is 409
    ``listing_status_in_use``. Prospective: a change applies to the writes that start after
    it."""

    feedback_tags: Annotated[list[FeedbackTag], PROSPECTIVE] = Field(
        default_factory=lambda: list(DEFAULT_FEEDBACK_TAGS), max_length=FEEDBACK_TAGS_MAX
    )
    """The reason tags a message rating may carry (FR-FB-1; #112), seeded with FR-FB-1's nine.
    Each is 1 to 40 of ``a-z``, ``0-9`` and ``_``; the list holds no tag twice and may be empty.
    A write replaces the whole list; removing a tag that a rating or its history still carries is
    409 ``feedback_tag_in_use``. Prospective: a rating checks its tags against the list in force
    when it is written."""

    profile_defaults: Annotated[dict[str, Any], PROSPECTIVE] = Field(
        default_factory=seeded_profile_defaults, json_schema_extra=_profile_settings_schema
    )
    """The deployment's defaults for every engine profile setting (FR-PROF-5; #113): the bottom
    layer of each story's effective configuration, under its profile's version and its own
    overrides. A complete settings document (every delivered setting named), seeded from the
    Lite preset with every other delivered module off. A write is a partial merge (an object
    merges into an object; any other value, ``null`` included, replaces) that must leave a
    complete document every profile and story can still run under (422 ``validation_error``
    or ``invalid_profile`` otherwise). Each changed setting is recorded as
    ``profile_defaults.<path>`` with its own class. Prospective: a story's next turn runs under
    the new defaults, wherever its profile and overrides leave a setting out."""

    @field_validator("listing_status_values")
    @classmethod
    def _distinct_listing_status_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("lists a listing status value more than once")
        return value

    @field_validator("feedback_tags")
    @classmethod
    def _distinct_feedback_tags(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("lists a feedback tag more than once")
        return value

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
