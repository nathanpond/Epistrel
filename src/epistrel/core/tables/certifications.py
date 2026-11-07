"""``profile_certifications`` and ``certification_runs``: what an eval run certified for one
configuration fingerprint, and the runs that produce them (FR-PROF-15..17, NFR-QUAL-8,11;
docs/02 §4.11; #233, migration 0113); and ``image_check_certifications``: which image-output
policies an image checker is certified for (NFR-QUAL-12; #258, migration 0134; #270 fills it).

A **certification** is a deployment-scoped record that an eval run against one configuration
fingerprint passed the floors it lists, with their sample sizes and bounds
(``floors_passed``; failed and insufficient floors are recorded too, never hidden), the model
each purpose used, the intervention types NFR-QUAL-14's floor covers (E-12), whether its judge
was calibrated, the run's host and report hash, and how it was recorded (``job`` or
``import``). Its results are append-only: a trigger lets only ``status`` and the
``invalidated_*`` columns change (#234 marks one ``stale``), and refuses DELETE.

A **certification run** is one request to certify a profile version or a story's effective
configuration (``kind`` ``certification``; #234 adds ``canary``): what it runs (suites, seeds,
sample), its estimate, its job, its status and report summary, and the certification it
recorded (``null`` when no floor passed or the run failed). Neither table holds story content:
``requested_by`` is a principal id kept as attribution (FR-AUTH-7).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")

profile_certifications = Table(
    "profile_certifications",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("fingerprint", Text, nullable=False),
    Column("profile_id", ulid_type(), nullable=True),
    Column("profile_version", Integer, nullable=True),
    Column("preset", Text, nullable=True),
    Column("models", JSONB, nullable=False, server_default=text("'{}'")),
    Column("quality_settings_hash", Text, nullable=True),
    Column("floors_passed", JSONB, nullable=False, server_default=text("'[]'")),
    Column("floors_failed", JSONB, nullable=False, server_default=text("'[]'")),
    Column("floors_insufficient", JSONB, nullable=False, server_default=text("'[]'")),
    Column(
        "intervention_types_covered",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column(
        "intervention_types_insufficient",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    ),
    Column("judge_calibrated", Boolean, nullable=False, server_default=text("false")),
    Column("sample_sizes", JSONB, nullable=False, server_default=text("'{}'")),
    Column("seed_count", Integer, nullable=False),
    Column("model_tier", Text, nullable=False, server_default=text("'other'")),
    Column("host", JSONB, nullable=False, server_default=text("'{}'")),
    Column("report_path", Text, nullable=True),
    Column("report_sha256", Text, nullable=False),
    Column("run_at", TIMESTAMP(timezone=True), nullable=False),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("recorded_via", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'valid'")),
    Column("invalidated_at", TIMESTAMP(timezone=True), nullable=True),
    Column("invalidated_reason", Text, nullable=True),
    Column("invalidated_by_run_id", ulid_type(), nullable=True),
    CheckConstraint("status IN ('valid', 'stale')", name="status"),
    CheckConstraint("model_tier IN ('reference', 'other')", name="model_tier"),
    CheckConstraint("recorded_via IN ('job', 'import')", name="recorded_via"),
    CheckConstraint(
        "invalidated_reason IS NULL OR invalidated_reason IN ('canary', 'drift', 'manual')",
        name="invalidated_reason",
    ),
    Index("ix_profile_certifications_fingerprint", "fingerprint", "status"),
)

certification_runs = Table(
    "certification_runs",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("kind", Text, nullable=False, server_default=text("'certification'")),
    Column("fingerprint", Text, nullable=False),
    Column("profile_id", ulid_type(), nullable=False),
    Column("profile_version", Integer, nullable=False),
    Column("story_id", ulid_type(), nullable=True),
    Column("suites", ARRAY(Text), nullable=False),
    Column("seeds", Integer, nullable=False),
    Column("sample", Integer, nullable=True),
    Column("checks_certification_id", ulid_type(), nullable=True),
    Column("outcome", Text, nullable=True),
    Column("reference", Boolean, nullable=False, server_default=text("false")),
    Column("estimate", JSONB, nullable=False, server_default=text("'{}'")),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("job_id", ulid_type(), nullable=True),
    Column("certification_id", ulid_type(), nullable=True),
    Column("report", JSONB, nullable=True),
    Column("requested_by", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("started_at", TIMESTAMP(timezone=True), nullable=True),
    Column("finished_at", TIMESTAMP(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    CheckConstraint("kind IN ('certification', 'canary')", name="kind"),
    CheckConstraint(
        "outcome IS NULL OR outcome IN ('held', 'missed', 'inconclusive')", name="outcome"
    ),
    CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="status"),
    CheckConstraint("seeds BETWEEN 1 AND 5", name="seeds"),
    Index("ix_certification_runs_fingerprint", "fingerprint", "status"),
)

model_reported_versions = Table(
    "model_reported_versions",
    metadata,
    Column("base_url_hash", Text, primary_key=True),
    Column("model_id", Text, primary_key=True),
    Column("reported_version", Text, primary_key=True),
    Column("first_seen_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("last_seen_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Index("ix_model_reported_versions_model", "model_id", "last_seen_at"),
)
"""#234: each version a model reported (``orchestrator.models.reported_version``)."""

image_check_certifications = Table(
    "image_check_certifications",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("checker_fingerprint", Text, nullable=False),
    Column("policy_id", Text, nullable=False),
    Column("policy_version", Integer, nullable=True),
    Column("model_id", Text, nullable=True),
    Column("metrics", JSONB, nullable=False, server_default=text("'{}'")),
    Column("report_sha256", Text, nullable=True),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("recorded_via", Text, nullable=False, server_default=text("'job'")),
    Column("status", Text, nullable=False, server_default=text("'valid'")),
    CheckConstraint("status IN ('valid', 'stale')", name="status"),
    CheckConstraint("recorded_via IN ('job', 'import')", name="recorded_via"),
    CheckConstraint("checker_fingerprint ~ '^[0-9a-f]{12}$'", name="checker_fingerprint"),
    Index(
        "ix_image_check_certifications_fingerprint", "checker_fingerprint", "policy_id", "status"
    ),
)
"""#258: that an image checker (``checker_fingerprint``: ``orchestrator.images.output_check``'s
12 hex of its endpoint host, model, reported version and ``image_check`` template) is certified
for one image-output policy (NFR-QUAL-12): a ``valid`` row makes that policy's result
``certified: true`` on every image it checks. Empty until #270 records E-11's runs (its
``metrics`` and ``report_sha256``); no story content."""

__all__ = [
    "certification_runs",
    "image_check_certifications",
    "model_reported_versions",
    "profile_certifications",
]
