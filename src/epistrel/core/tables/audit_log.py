"""``audit_log``: deployment-level audit records that outlive the story they mention (#56).

One row per audited act that has no operation of its own: an input rejected by a guardrail
(``guardrail_input_rejected``, FR-GUARD-10), a story deletion (``story_deleted``, #50), a
catalog entry deletion (``catalog_entry_deleted``, #103), a persona deletion
(``persona_deleted``, #114) and a refused tightening of a story's rating
(``rating_change_refused``, #118: the job id, the target rating and the violation count) and an
erasure (``erasure``, #124: the ledger entry's id, the subject's kind and id, the scope and the
counts), and an image output rejected or an image job ended without an image
(``image_output_rejected``, ``image_job_ended``, #256). A row holds policy ids, the story, the
principal and small structured details, and **never** the text that was rejected (NFR-SEC-4).

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
    RATING_CHANGE_REFUSED = "rating_change_refused"
    ERASURE = "erasure"
    STORY_IMPORT = "story_import"
    """#250: a finished import, ready or refused (job id, bundle checksum, story id)."""
    BACKFILL_CANCELLED = "backfill_cancelled"
    """#228: a backfill cancelled (its id, kind, capabilities, range and progress counts)."""
    BACKFILL_FAILED = "backfill_failed"
    """#228: a backfill ended ``failed`` after its last attempt (the same, and the error's
    exception name)."""
    IMAGE_OUTPUT_REJECTED = "image_output_rejected"
    """#256: a generated image failed an output check (job id, attempt token, the output's
    index, ``outcome: rejected``; the policy ids). Never the image or its prompt."""
    IMAGE_JOB_ENDED = "image_job_ended"
    """#256: an image job ended without an image: failed closed, cancelled, stale or its target
    gone (job id, attempt token, ``outcome``: the status, and its ``reason`` code; the policy
    ids). Never the image or its prompt."""
    IMAGE_PROMPT_REJECTED = "image_prompt_rejected"
    """#257: an image prompt refused by the prompt check before any job (the target's kind and
    id, the entry point; the policy ids). Never the prompt (FR-GUARD-10, NFR-SEC-4)."""


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
