"""certifications: what an eval run certified for one configuration fingerprint (#233)

FR-PROF-15..17, NFR-QUAL-8,11 (docs/02 §4.11): ``core.tables.certifications`` describes the
tables.

- ``profile_certifications``: deployment-scoped certification records, append-only for their
  results. A trigger lets an UPDATE change only ``status`` and the ``invalidated_*`` columns
  (#234 marks one ``stale``) and refuses DELETE and TRUNCATE.
- ``certification_runs``: one row per certification run request (its estimate, job, status,
  report summary and the certification it recorded). A run row's lifecycle columns change;
  DELETE is refused (the rows are kept).

Downgrade drops both (refused while a certification exists).

Revision ID: 0113
Revises: 0112
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0113"
down_revision: str | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")
EMPTY_TEXTS = sa.text("'{}'::text[]")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_certifications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['status', 'invalidated_at', 'invalidated_reason',
                            'invalidated_by_run_id'];
BEGIN
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'profile_certifications' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '%: update of % refused', TG_TABLE_NAME, OLD.id USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION '%: TRUNCATE refused', TG_TABLE_NAME USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION '%: % of % refused', TG_TABLE_NAME, TG_OP, OLD.id USING ERRCODE = 'EPI01';
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "profile_certifications",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("profile_id", ULID, nullable=True),
        sa.Column("profile_version", sa.Integer, nullable=True),
        sa.Column("preset", sa.Text, nullable=True),
        sa.Column("models", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("quality_settings_hash", sa.Text, nullable=True),
        sa.Column(
            "floors_passed", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "floors_failed", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "floors_insufficient", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "intervention_types_covered",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=EMPTY_TEXTS,
        ),
        sa.Column(
            "intervention_types_insufficient",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=EMPTY_TEXTS,
        ),
        sa.Column("judge_calibrated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("sample_sizes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("seed_count", sa.Integer, nullable=False),
        sa.Column("model_tier", sa.Text, nullable=False, server_default=sa.text("'other'")),
        sa.Column("host", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("report_path", sa.Text, nullable=True),
        sa.Column("report_sha256", sa.Text, nullable=False),
        sa.Column("run_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("recorded_via", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'valid'")),
        sa.Column("invalidated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("invalidated_reason", sa.Text, nullable=True),
        sa.Column("invalidated_by_run_id", ULID, nullable=True),
        sa.CheckConstraint("status IN ('valid', 'stale')", name="ck_profile_certifications_status"),
        sa.CheckConstraint(
            "model_tier IN ('reference', 'other')", name="ck_profile_certifications_model_tier"
        ),
        sa.CheckConstraint(
            "recorded_via IN ('job', 'import')", name="ck_profile_certifications_recorded_via"
        ),
        sa.CheckConstraint(
            "invalidated_reason IS NULL OR invalidated_reason IN ('canary', 'drift', 'manual')",
            name="ck_profile_certifications_invalidated_reason",
        ),
    )
    op.create_index(
        "ix_profile_certifications_fingerprint",
        "profile_certifications",
        ["fingerprint", "status"],
    )
    op.create_table(
        "certification_runs",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'certification'")),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("profile_id", ULID, nullable=False),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("suites", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("seeds", sa.Integer, nullable=False),
        sa.Column("sample", sa.Integer, nullable=True),
        sa.Column("reference", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("estimate", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("job_id", ULID, nullable=True),
        sa.Column("certification_id", ULID, nullable=True),
        sa.Column("report", postgresql.JSONB, nullable=True),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint("kind IN ('certification')", name="ck_certification_runs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_certification_runs_status",
        ),
        sa.CheckConstraint("seeds BETWEEN 1 AND 5", name="ck_certification_runs_seeds"),
    )
    op.create_index(
        "ix_certification_runs_fingerprint", "certification_runs", ["fingerprint", "status"]
    )
    op.execute(GUARD)
    op.execute(
        "CREATE TRIGGER profile_certifications_guard BEFORE UPDATE OR DELETE ON "
        "profile_certifications FOR EACH ROW EXECUTE FUNCTION epistrel_certifications_guard()"
    )
    op.execute(
        "CREATE TRIGGER certification_runs_no_delete BEFORE DELETE ON certification_runs "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_certifications_guard()"
    )
    for table in ("profile_certifications", "certification_runs"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_certifications_guard()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM profile_certifications)")).scalar_one():
        raise RuntimeError(
            "0113 downgrade refused: certification records exist (they cannot be recreated)"
        )
    op.drop_table("certification_runs")
    op.drop_table("profile_certifications")
    op.execute("DROP FUNCTION IF EXISTS epistrel_certifications_guard()")
