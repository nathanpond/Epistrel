"""Image generation jobs and their provider attempts (FR-IMG-3,4,11; docs/02 §4.13; #256).

``image_jobs`` holds one row per generation request: its ``kind`` (``catalog`` or ``story``),
``story_id`` (set for a story job, null for a catalog job: the plain column whole-story deletion
enumerates, ``core.stories.STORY_TABLES``), the ``target`` it attaches to and the ``origin`` it
came from (jsonb: a story job's ``{story_id, operation_id, turn, variant, position, viewpoint,
timeline_revision}``, a catalog job's ``{entry_kind, entry_id, draft_revision,
image_input_fingerprint}``), the audience ``viewpoint``, the ``prompt`` and ``negative_prompt``
the adapter is sent, the requested ``params`` (``{n, seed, size, aspect_ratio, quality,
reference_image_ids}``), its ``status`` and ``status_reason`` (a bare code), the current
``attempt_token`` (a uuid, independent of the ``jobs`` row's integer fence: promotion and
attachment compare it, so a fenced or cancelled worker never promotes), how many provider
``attempts`` it made (its ``image_reservations`` rows), the per-output ``policy_results``
(``[{attempt_token, output, policy_id, result}]``), the promoted ``asset_ids``, the
``idempotency_key`` of the request (a reference to #61's table, not unique here), and the
requesting ``principal``. A story job generates one image (``params.n = 1``); only a story job
is ``promoted`` and only a catalog job is ``candidates_ready``.

``image_reservations`` holds one row per provider attempt, unique on ``(job_id,
attempt_token)``: when it was ``started_at`` and its ``horizon_at`` (null until the worker
sends), its ``send_state`` (``prepared`` until the worker sends, ``sending`` committed before
``submit``, ``accepted`` once an ``async`` adapter acknowledged it, ``returned``
with a ``sync`` adapter's results, ``ambiguous`` for #268), the provider's ``attempt_ref``, the
attempt's ``idempotency_key``, the ``applied_parameters`` the adapter reported, how many
``outputs_returned`` and its ``outcome`` (``promoted``, ``rejected``, ``failed`` or
``discarded``). #262 adds the accounting columns (FR-IMG-11): the attribution ``story_id``,
``entry_kind`` and ``entry_id`` (for usage reports, never counter scopes), the immutable
``counter_keys`` snapshot (``[{scope, scope_key, period}]`` in lock order) and ``requested``,
then ``used``, ``overage``, ``extra_outputs``, the settlement ``basis`` (``actual``,
``presumed``, ``corrected``), the ``state`` (``held``, ``settled``, ``released``) and
``settled_at``. A trigger (``image_reservation_immutable``) refuses any change of ``job_id``,
``attempt_token``, ``requested``, ``counter_keys``, ``story_id``, ``entry_kind``, ``entry_id``
or ``created_at``, except that a story's deletion nulls ``story_id`` (the records are deployment
accounting: ``core.stories.STORY_ID_EXCEPTIONS``). #267/#268 extend it.

``image_limit_counters`` (#262) holds the stored sums of those records per limit scope
(``deployment`` or ``entry``), ``scope_key`` (``deployment`` or ``<entry_kind>:<entry_id>``)
and ``period`` (the local date in ``deployment_time_zone`` when the record was made):
``reserved`` sums ``requested`` over ``held`` records, ``used``, ``overage`` and
``extra_outputs`` sum over ``settled`` ones. ``limit_value`` is the limit seen at the last
admission check (informational) and ``last_record_at`` the last record change touching the row.
``image_jobs.status_detail`` (#262) is a ``cap_reached`` job's ``{scope, period}``, a
``stale`` one's ``{cause, policy_ids, amendment_id}`` and a story job waiting on a paused
story's ``{waiting_on, next_check_at}`` (#264). #264 adds ``wait_started_at`` (the first wait on
a paused story, set once) and ``checked_policies`` (the story's image policy set,
``[[policy_id, version], …]``, at the job's last prompt and output check).

#267 (FR-IMG-11's late returns) adds ``image_jobs.waiting_for_horizon`` (a lease expiry fenced
an attempt in flight, and no attempt starts before its horizon), ``confirm_deadline_at`` (when a
``confirms_completion`` adapter's status polling gives up: the horizon plus
``image_confirm_wait_max_s``) and ``confirm_fallback`` (sticky: the job fell back to the
non-confirming rule), and ``image_reservations.cancel_result`` (what the one ``cancel`` after the
horizon answered: ``accepted``, ``not_supported`` or ``error``) and ``last_status`` (the last
``status`` answer). ``horizon_at`` is ``started_at + request_timeout_s + image_attempt_grace_s``.

#268 (FR-IMG-11's ambiguous sends) adds ``image_reservations.resend_of`` (an idempotent re-send's
row, ``requested = 0`` with no ``counter_keys``, names the stalled attempt whose key it re-sent:
that attempt's record keeps the reservation and takes the settlement), ``request_snapshot`` (the
``ImageRequest`` the attempt sent, as JSON, without its prompt and negative prompt: the records
are deployment accounting and hold no story content; a re-send takes the job's prompt) and
``capabilities_snapshot`` (the adapter's ``{submission, idempotency_keys}`` at the ``sending``
commit; recovery reads it, never the live adapter). ``idempotency_key`` is
``sha256("<job_id>:<attempt_token>")`` in lowercase hex, written at the ``sending`` commit (a
re-send carries its stalled attempt's). ``send_state = ambiguous``
marks an ambiguous send: a lease expiry found it ``sending`` and it could not be re-sent.

#269 (FR-IMG-11's cost) adds ``image_costs``: one row per provider attempt that reached the
provider, unique on ``(job_id, attempt_token)`` (a re-send's cost lands on the attempt it
re-sent). It holds the attribution ``story_id``, ``entry_kind`` and ``entry_id`` (copied from the
attempt's reservation record), the ``cost`` (``numeric(18,8)``, null exactly when
``cost_status = unknown``, 0 when ``zero``), its ``currency`` (ISO 4217, upper case; set only
when ``known``), ``outputs_billed`` (the outputs returned; null when nobody knows, such as a
presumption or a horizon release), the ``accounting`` (``normal``, or ``late_unaccounted`` for a
late return whose reservation record accounting retention had purged), ``period_date`` (the
deployment counter's period in the record's ``counter_keys``; a ``late_unaccounted`` row's is
its ``recorded_at``'s local day), ``recorded_at`` and ``filled_at`` (the one ``unknown`` → known
or zero fill-in). No foreign keys: the rows are deployment accounting with no story content,
outlive the job, the story (``core.stories.STORY_ID_EXCEPTIONS``: deletion nulls ``story_id``)
and the entry, and are purged with the accounting retention (``limits.retain``).

``image_jobs.result`` (#260) holds what a successful job made beyond its ``asset_ids``: a catalog
job's ``{candidate_ids}``, which outlive the candidate rows an attach or a discard deletes.

``catalog.image_candidates`` (#260; FR-IMG-7) holds one row per catalog candidate: its id (a ULID
of its own, never the asset's), the entry it belongs to (``entry_kind`` and ``entry_id``, no
foreign key: the entry's delete removes its candidates explicitly), the ``job_id`` that made it,
its ``asset_id`` (a ``media_assets`` row in state ``candidate``; deleting the asset deletes the
row), the ``slot`` it was requested for (a hint: it may attach to any image slot of its entry),
the ``draft_revision`` and ``image_input_fingerprint`` it was generated from, and when it
``expires_at`` (fixed at promotion: ``candidate_ttl`` later). Only the candidate expiry, the
entry's delete, a discard and an attach remove a row.

Neither job table holds image bytes: generated bytes live in private staging under the media root
(``tmp/images/<job_id>/<attempt_token>/<k>``, ``core.media.store``) until promotion.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from epistrel.core.tables.base import metadata, ulid_type
from epistrel.core.tables.catalog import CATALOG_SCHEMA


class ImageJobKind(StrEnum):
    CATALOG = "catalog"
    STORY = "story"


class ImageJobStatus(StrEnum):
    """An image job's status (docs/02 §4.13)."""

    QUEUED = "queued"
    GENERATING = "generating"
    CHECKING = "checking"
    PROMOTED = "promoted"
    CANDIDATES_READY = "candidates_ready"
    FAILED = "failed"
    STALE = "stale"
    TARGET_GONE = "target_gone"
    CAP_REACHED = "cap_reached"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[ImageJobStatus] = frozenset(
    {
        ImageJobStatus.PROMOTED,
        ImageJobStatus.CANDIDATES_READY,
        ImageJobStatus.FAILED,
        ImageJobStatus.STALE,
        ImageJobStatus.TARGET_GONE,
        ImageJobStatus.CAP_REACHED,
        ImageJobStatus.CANCELLED,
    }
)
"""The statuses a job never leaves."""


class SendState(StrEnum):
    """Where one provider attempt's send is (FR-IMG-11; #268 recovers the ambiguous ones)."""

    PREPARED = "prepared"
    SENDING = "sending"
    ACCEPTED = "accepted"
    RETURNED = "returned"
    AMBIGUOUS = "ambiguous"


class ReservationState(StrEnum):
    """A reservation record's accounting state (FR-IMG-11; #262)."""

    HELD = "held"
    SETTLED = "settled"
    RELEASED = "released"


class SettlementBasis(StrEnum):
    """How a settled record's ``used`` was known: ``actual`` outputs, ``presumed`` (#268) or
    ``corrected`` once after a presumption (FR-IMG-11)."""

    ACTUAL = "actual"
    PRESUMED = "presumed"
    CORRECTED = "corrected"


class LimitScope(StrEnum):
    """An image limit's scope, in lock order (FR-IMG-11 as amended by #262)."""

    DEPLOYMENT = "deployment"
    ENTRY = "entry"


class AttemptOutcome(StrEnum):
    """What became of one provider attempt (``image_reservations.outcome``, null while it is in
    flight). #256's four, then #267's: ``settled`` (its record settled with no other outcome),
    ``adopted`` (a confirming adapter's ``succeeded`` taken over by the job after the horizon),
    ``cancelled`` (the provider confirmed the cancel), ``released_at_horizon`` (it never returned
    by its horizon), ``abandoned_fallback`` (``image_confirm_wait_max_s`` passed with no
    definitive status), and the fenced worker's late returns: ``late_discarded`` (before the
    horizon), ``late_after_horizon`` (after it) and ``late_duplicate`` (onto a settled record;
    reported, never stored over the earlier outcome). #268 adds ``presumed`` (an ambiguous send
    settled at its horizon as if it had returned its requested outputs), ``resent`` (an
    idempotent re-send under a later token returned its result onto this record) and
    ``late_after_presumed`` (the actual result corrected a presumed settlement once)."""

    PROMOTED = "promoted"
    REJECTED = "rejected"
    FAILED = "failed"
    DISCARDED = "discarded"
    SETTLED = "settled"
    ADOPTED = "adopted"
    CANCELLED = "cancelled"
    RELEASED_AT_HORIZON = "released_at_horizon"
    ABANDONED_FALLBACK = "abandoned_fallback"
    LATE_DISCARDED = "late_discarded"
    LATE_AFTER_HORIZON = "late_after_horizon"
    LATE_DUPLICATE = "late_duplicate"
    PRESUMED = "presumed"
    RESENT = "resent"
    LATE_AFTER_PRESUMED = "late_after_presumed"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v.value}'" for v in values) + ")"


KIND_CHECK = _in("kind", ImageJobKind)
STATUS_CHECK = _in("status", ImageJobStatus)
STORY_CHECK = "(story_id IS NOT NULL) = (kind = 'story')"
STORY_N_CHECK = "kind <> 'story' OR (params->>'n')::int = 1"
PROMOTED_CHECK = "status <> 'promoted' OR kind = 'story'"
CANDIDATES_CHECK = "status <> 'candidates_ready' OR kind = 'catalog'"
SEND_STATE_CHECK = _in("send_state", SendState)
OUTCOME_CHECK = "outcome IS NULL OR " + _in("outcome", AttemptOutcome)
STATE_CHECK = _in("state", ReservationState)
BASIS_CHECK = _in("basis", SettlementBasis)
SCOPE_CHECK = _in("scope", LimitScope)
ENTRY_KIND_CHECK = "entry_kind IS NULL OR entry_kind IN ('character', 'scenario')"
ACCOUNTING_CHECK = "requested >= 0 AND used >= 0 AND overage >= 0 AND extra_outputs >= 0"
COUNTER_CHECK = "reserved >= 0 AND used >= 0 AND overage >= 0 AND extra_outputs >= 0"
IMMUTABLE_ERROR = "image_reservation_immutable"
"""The message the immutability trigger raises (migration 0137)."""

image_jobs = Table(
    "image_jobs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("target", JSONB, nullable=False),
    Column("origin", JSONB, nullable=False),
    Column("viewpoint", Text, nullable=True),
    # Null once an erasure redacted it (#266, migration 0139); empty for an automatic job until
    # it prepares (#265).
    Column("prompt", Text, nullable=True),
    Column("negative_prompt", Text, nullable=True),
    Column("params", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("status_reason", Text, nullable=True),
    Column("attempt_token", UUID(as_uuid=False), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("policy_results", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("asset_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("idempotency_key", Text, nullable=True),
    Column("principal", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("safety_context", JSONB, nullable=True),
    Column("result", JSONB, nullable=True),
    Column("status_detail", JSONB, nullable=True),
    Column("wait_started_at", TIMESTAMP(timezone=True), nullable=True),
    Column("checked_policies", JSONB, nullable=True),
    Column("waiting_for_horizon", Boolean, nullable=False, server_default=text("false")),
    Column("confirm_deadline_at", TIMESTAMP(timezone=True), nullable=True),
    Column("confirm_fallback", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint(KIND_CHECK, name="kind"),
    CheckConstraint(STATUS_CHECK, name="status"),
    CheckConstraint(STORY_CHECK, name="story"),
    CheckConstraint(STORY_N_CHECK, name="story_n"),
    CheckConstraint(PROMOTED_CHECK, name="promoted_story"),
    CheckConstraint(CANDIDATES_CHECK, name="candidates_catalog"),
    CheckConstraint("attempts >= 0", name="attempts"),
)

Index("ix_image_jobs_status", image_jobs.c.status)
Index("ix_image_jobs_idempotency_key", image_jobs.c.idempotency_key)
Index("ix_image_jobs_story_id", image_jobs.c.story_id)

image_reservations = Table(
    "image_reservations",
    metadata,
    Column("job_id", ulid_type(), nullable=False),
    Column("attempt_token", UUID(as_uuid=False), nullable=False),
    Column("started_at", TIMESTAMP(timezone=True), nullable=True),
    Column("horizon_at", TIMESTAMP(timezone=True), nullable=True),
    Column("send_state", Text, nullable=False, server_default=text("'prepared'")),
    Column("attempt_ref", Text, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Column("applied_parameters", JSONB, nullable=True),
    Column("outputs_returned", Integer, nullable=True),
    Column("outcome", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("entry_kind", Text, nullable=True),
    Column("entry_id", ulid_type(), nullable=True),
    Column("counter_keys", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("requested", Integer, nullable=False, server_default=text("0")),
    Column("used", Integer, nullable=False, server_default=text("0")),
    Column("overage", Integer, nullable=False, server_default=text("0")),
    Column("extra_outputs", Integer, nullable=False, server_default=text("0")),
    Column("basis", Text, nullable=False, server_default=text("'actual'")),
    Column("state", Text, nullable=False),
    Column("settled_at", TIMESTAMP(timezone=True), nullable=True),
    Column("cancel_result", Text, nullable=True),
    Column("last_status", Text, nullable=True),
    Column("resend_of", UUID(as_uuid=False), nullable=True),
    Column("request_snapshot", JSONB, nullable=True),
    Column("capabilities_snapshot", JSONB, nullable=True),
    PrimaryKeyConstraint("job_id", "attempt_token", name="pk_image_reservations"),
    CheckConstraint(SEND_STATE_CHECK, name="send_state"),
    CheckConstraint(OUTCOME_CHECK, name="outcome"),
    CheckConstraint(STATE_CHECK, name="state"),
    CheckConstraint(BASIS_CHECK, name="basis"),
    CheckConstraint(ENTRY_KIND_CHECK, name="entry_kind"),
    CheckConstraint(ACCOUNTING_CHECK, name="accounting"),
)

Index(
    "ix_image_reservations_held",
    image_reservations.c.job_id,
    postgresql_where=text("state = 'held'"),
)
Index("ix_image_reservations_story_id", image_reservations.c.story_id)
Index("ix_image_reservations_created_at", image_reservations.c.created_at)

image_limit_counters = Table(
    "image_limit_counters",
    metadata,
    Column("scope", Text, nullable=False),
    Column("scope_key", Text, nullable=False),
    Column("period", Date, nullable=False),
    Column("limit_value", Integer, nullable=False),
    Column("reserved", Integer, nullable=False, server_default=text("0")),
    Column("used", Integer, nullable=False, server_default=text("0")),
    Column("overage", Integer, nullable=False, server_default=text("0")),
    Column("extra_outputs", Integer, nullable=False, server_default=text("0")),
    Column("last_record_at", TIMESTAMP(timezone=True), nullable=True),
    PrimaryKeyConstraint("scope", "scope_key", "period", name="pk_image_limit_counters"),
    CheckConstraint(SCOPE_CHECK, name="scope"),
    CheckConstraint(COUNTER_CHECK, name="counts"),
    CheckConstraint("limit_value >= 0", name="limit_value"),
)


class CostStatus(StrEnum):
    """What is known about one attempt's cost (FR-IMG-11; #269): ``known`` (the provider said),
    ``zero`` (the provider refused before generating, so nothing was billed) or ``unknown``
    (never summed as 0)."""

    KNOWN = "known"
    ZERO = "zero"
    UNKNOWN = "unknown"


class CostAccounting(StrEnum):
    """``normal``, or ``late_unaccounted``: a late return whose reservation record accounting
    retention had already purged (#268, #269)."""

    NORMAL = "normal"
    LATE_UNACCOUNTED = "late_unaccounted"


COST_STATUS_CHECK = _in("cost_status", CostStatus)
COST_ACCOUNTING_CHECK = _in("accounting", CostAccounting)
COST_VALUE_CHECK = (
    "(cost IS NULL) = (cost_status = 'unknown') AND (cost_status <> 'zero' OR cost = 0) "
    "AND (cost IS NULL OR cost >= 0)"
)
COST_CURRENCY_CHECK = (
    "(currency IS NULL OR currency ~ '^[A-Z]{3}$') "
    "AND ((currency IS NOT NULL) = (cost_status = 'known'))"
)
COST_OUTPUTS_CHECK = (
    "(outputs_billed IS NULL OR outputs_billed >= 0) "
    "AND (cost_status <> 'zero' OR outputs_billed = 0)"
)

image_costs = Table(
    "image_costs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("job_id", ulid_type(), nullable=False),
    Column("attempt_token", UUID(as_uuid=False), nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("entry_kind", Text, nullable=True),
    Column("entry_id", ulid_type(), nullable=True),
    Column("cost", Numeric(18, 8), nullable=True),
    Column("cost_status", Text, nullable=False),
    Column("currency", Text, nullable=True),
    Column("outputs_billed", Integer, nullable=True),
    Column("accounting", Text, nullable=False, server_default=text("'normal'")),
    Column("period_date", Date, nullable=False),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False),
    Column("filled_at", TIMESTAMP(timezone=True), nullable=True),
    UniqueConstraint("job_id", "attempt_token", name="uq_image_costs_attempt"),
    CheckConstraint(COST_STATUS_CHECK, name="cost_status"),
    CheckConstraint(COST_ACCOUNTING_CHECK, name="accounting"),
    CheckConstraint(COST_VALUE_CHECK, name="cost_value"),
    CheckConstraint(COST_CURRENCY_CHECK, name="currency"),
    CheckConstraint(COST_OUTPUTS_CHECK, name="outputs_billed"),
    CheckConstraint(ENTRY_KIND_CHECK, name="entry_kind"),
)

Index("ix_image_costs_period_date", image_costs.c.period_date)
Index("ix_image_costs_story_id", image_costs.c.story_id)
Index("ix_image_costs_entry", image_costs.c.entry_kind, image_costs.c.entry_id)

CANDIDATE_KIND_CHECK = "entry_kind IN ('character', 'scenario')"

image_candidates = Table(
    "image_candidates",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("entry_kind", Text, nullable=False),
    Column("entry_id", ulid_type(), nullable=False),
    Column("job_id", ulid_type(), nullable=False),
    Column(
        "asset_id",
        ulid_type(),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("slot", Text, nullable=False),
    Column("draft_revision", Integer, nullable=False),
    Column("image_input_fingerprint", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(CANDIDATE_KIND_CHECK, name="entry_kind"),
    CheckConstraint("slot IN ('primary', 'gallery', 'cover')", name="slot"),
    schema=CATALOG_SCHEMA,
)

Index("ix_image_candidates_entry", image_candidates.c.entry_kind, image_candidates.c.entry_id)
Index("ix_image_candidates_job_id", image_candidates.c.job_id)
Index("ix_image_candidates_expires_at", image_candidates.c.expires_at)
Index("ix_image_candidates_asset_id", image_candidates.c.asset_id)

__all__ = [
    "TERMINAL_STATUSES",
    "AttemptOutcome",
    "CostAccounting",
    "CostStatus",
    "ImageJobKind",
    "ImageJobStatus",
    "LimitScope",
    "ReservationState",
    "SendState",
    "SettlementBasis",
    "image_candidates",
    "image_costs",
    "image_jobs",
    "image_limit_counters",
    "image_reservations",
]
