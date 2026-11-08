"""Image-checker certification evidence, and the external-evidence-only policy flag (#270)

NFR-QUAL-12 (as amended by ADR-182), FR-PROF-15. ``policies`` gains ``external_evidence_only``:
true only for the seeded ``minors_no_sexual_content`` (a CHECK keeps it so, a trigger keeps it
immutable), the policy an image checker is certified for only by recorded third-party evidence.
``image_check_certifications`` (#258's table, empty until now) gains the evidence a record rests
on: ``evidence_kind`` (``eval_run`` or ``external_attestation``), ``evidence_ref`` (the eval
report's path, hash and per-policy verdict, or the attestation's title, URL, publisher, date and
quoted figure), ``content_hash`` (unique: recording the same evidence again stores nothing),
``recorded_by`` and ``invalidated_at``/``invalidated_by``/``invalidated_reason``. A trigger makes
the table append-only except ``status`` and the ``invalidated_*`` columns, and refuses DELETE.
``audit_log`` gains ``image_check_certification_recorded`` and
``image_check_certification_invalidated`` (ids only).

Revision ID: 0143
Revises: 0142
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0143"
down_revision: str | None = "0142"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "image_check_certifications"
MINORS = "minors_no_sexual_content"
KINDS = (
    "guardrail_input_rejected",
    "story_deleted",
    "catalog_entry_deleted",
    "persona_deleted",
    "rating_change_refused",
    "erasure",
    "story_import",
    "backfill_cancelled",
    "backfill_failed",
    "image_output_rejected",
    "image_job_ended",
    "image_prompt_rejected",
    "image_upload_rejected",
    "image_counters_reconciled",
)
ADDED_KINDS = ("image_check_certification_recorded", "image_check_certification_invalidated")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_image_check_certifications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['status', 'invalidated_at', 'invalidated_by', 'invalidated_reason'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
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

FLAG_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_policies_evidence_flag_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.external_evidence_only IS DISTINCT FROM OLD.external_evidence_only THEN
        RAISE EXCEPTION 'policies: external_evidence_only of % is immutable', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    RETURN NEW;
END
$$;
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.add_column(
        "policies",
        sa.Column(
            "external_evidence_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute(
        "UPDATE policies SET external_evidence_only = true WHERE id = 'minors_no_sexual_content'"
    )
    op.create_check_constraint(
        "ck_policies_external_evidence_only",
        "policies",
        f"NOT external_evidence_only OR id = '{MINORS}'",
    )
    op.execute(FLAG_GUARD)
    op.execute(
        "CREATE TRIGGER policies_evidence_flag_guard BEFORE UPDATE ON policies "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_policies_evidence_flag_guard()"
    )

    op.add_column(
        TABLE, sa.Column("evidence_kind", sa.Text(), nullable=False, server_default="eval_run")
    )
    op.add_column(
        TABLE,
        sa.Column(
            "evidence_ref", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.add_column(TABLE, sa.Column("content_hash", sa.Text(), nullable=True))
    op.execute("UPDATE image_check_certifications SET content_hash = id WHERE content_hash IS NULL")
    op.alter_column(TABLE, "content_hash", nullable=False)
    op.add_column(
        TABLE, sa.Column("recorded_by", sa.Text(), nullable=False, server_default="system")
    )
    op.add_column(TABLE, sa.Column("invalidated_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("invalidated_by", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("invalidated_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_image_check_certifications_evidence_kind",
        TABLE,
        _in("evidence_kind", ("eval_run", "external_attestation")),
    )
    op.create_check_constraint(
        "ck_image_check_certifications_invalidated_reason",
        TABLE,
        "invalidated_reason IS NULL OR invalidated_reason IN ('drift', 'manual')",
    )
    op.create_index(
        "uq_image_check_certifications_content_hash", TABLE, ["content_hash"], unique=True
    )
    op.execute(GUARD)
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION epistrel_image_check_certifications_guard()"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
        "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_image_check_certifications_guard()"
    )

    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_log_kind", "audit_log", _in("kind", (*KINDS, *ADDED_KINDS))
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM audit_log WHERE kind IN "
        "('image_check_certification_recorded', 'image_check_certification_invalidated')"
    )
    op.drop_constraint("ck_audit_log_kind", "audit_log", type_="check")
    op.create_check_constraint("ck_audit_log_kind", "audit_log", _in("kind", KINDS))

    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_guard ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS epistrel_image_check_certifications_guard()")
    op.drop_index("uq_image_check_certifications_content_hash", TABLE)
    op.drop_constraint("ck_image_check_certifications_invalidated_reason", TABLE, type_="check")
    op.drop_constraint("ck_image_check_certifications_evidence_kind", TABLE, type_="check")
    for column in (
        "invalidated_reason",
        "invalidated_by",
        "invalidated_at",
        "recorded_by",
        "content_hash",
        "evidence_ref",
        "evidence_kind",
    ):
        op.drop_column(TABLE, column)

    op.execute("DROP TRIGGER IF EXISTS policies_evidence_flag_guard ON policies")
    op.execute("DROP FUNCTION IF EXISTS epistrel_policies_evidence_flag_guard()")
    op.drop_constraint("ck_policies_external_evidence_only", "policies", type_="check")
    op.drop_column("policies", "external_evidence_only")
