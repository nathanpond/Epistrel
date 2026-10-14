"""``erasure_ledger``: one row per applied erasure (FR-ADMIN-2,7; #124; docs/02 §4.10).

A row records what was erased and by whom, never what it said: the subject (``subject_kind``,
``subject_id``), the ``scope`` (``story`` or ``catalog``), the story it ran in (``story_ref``,
null for the catalog scope), the acting ``principal``, the erasure's ``control`` operation, the
counts it reported, and ``scan_term_hashes``: a keyed SHA-256 (HMAC under
``EPISTREL_CONFIG_KEY``) of each term the residue scan looked for, so a later check can look for
the same terms without the ledger holding a name. ``ledger_hash_prev``/``ledger_hash`` are null
until #125 chains the rows (its file outside the database is the authority), and ``applied_at``
is when the erasure was applied to this database.

The story column is ``story_ref``, not ``story_id``: story deletion empties every ``story_id``
table (``core.stories.STORY_TABLES``), and the ledger must outlive the story it names. A fork
does not copy it.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

SCOPE_CHECK = "scope IN ('story', 'catalog')"
SUBJECT_KIND_CHECK = "subject_kind IN ('character', 'catalog_character', 'persona')"
STORY_REF_CHECK = "(story_ref IS NOT NULL) = (scope = 'story')"

erasure_ledger = Table(
    "erasure_ledger",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("subject_kind", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("story_ref", ulid_type(), nullable=True),
    Column("principal", Text, nullable=False),
    Column("operation_id", ulid_type(), nullable=True),
    Column("counts", JSONB, nullable=False),
    Column("scan_term_hashes", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("ledger_hash_prev", Text, nullable=True),
    Column("ledger_hash", Text, nullable=True),
    Column("applied_at", TIMESTAMP(timezone=True), nullable=True),
    Column(
        "at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint(SCOPE_CHECK, name="scope"),
    CheckConstraint(SUBJECT_KIND_CHECK, name="subject_kind"),
    CheckConstraint(STORY_REF_CHECK, name="story_ref"),
)

Index("ix_erasure_ledger_subject", erasure_ledger.c.subject_kind, erasure_ledger.c.subject_id)
Index("ix_erasure_ledger_story_ref", erasure_ledger.c.story_ref)
