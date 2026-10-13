"""``audit_log``: deployment-level audit records that outlive the story they mention (#56).

One row per audited act that has no operation of its own: an input rejected by a guardrail
(``guardrail_input_rejected``, FR-GUARD-10), a story deletion (``story_deleted``, #50), a
catalog entry deletion (``catalog_entry_deleted``, #103) and a persona deletion
(``persona_deleted``, #114). A row holds policy ids, the story, the principal and small
structured details, and **never** the text that was rejected (NFR-SEC-4).

The table is append-only (trigger ``audit_log_append_only``). Two changes are allowed:
deleting a story nulls ``story_id`` on that story's rows, which is the only update the trigger
lets through, and #71's retention sweep deletes rows under ``epistrel.allow_audit_expiry``.
``story_id`` has no foreign key: it is the one declared exception to story deletion
(``core.stories.STORY_ID_EXCEPTIONS``).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type


class AuditKind(StrEnum):
    GUARDRAIL_INPUT_REJECTED = "guardrail_input_rejected"
    STORY_DELETED = "story_deleted"
    CATALOG_ENTRY_DELETED = "catalog_entry_deleted"
    PERSONA_DELETED = "persona_deleted"


KIND_CHECK = "kind IN (" + ", ".join(f"'{k.value}'" for k in AuditKind) + ")"

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column(
        "at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("story_id", ulid_type(), nullable=True),
    Column("principal", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("policy_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint(KIND_CHECK, name="kind"),
)

Index("ix_audit_log_story_at", audit_log.c.story_id, audit_log.c.at)
Index("ix_audit_log_at", audit_log.c.at)
