"""``extraction_shadow``: what ``gated`` claim extraction would have missed (FR-OBS-4,
FR-VERIFY-13; #148, migration 0062).

Under ``verification.extraction_mode = gated`` the extraction backstop runs only on the beats a
detector flagged. A deployment-set share of renders (``extraction_shadow_sample_rate``) still
runs it after the turn commits (``orchestrator.verify.extraction``'s ``extraction_shadow``
job), and records here the claims the extraction found that no declared or detector claim
covered: one row per sampled render (or composed message).

- **where it came from**: the derived-record columns of ``core.membership``, ``operation_id``
  being the **sampled turn's** operation, so a truncated turn's sample leaves the timeline with
  it (#130).
- **what it found**: ``status`` (``sampled``, or ``failed`` when the extraction call failed),
  ``extraction_claims`` (every claim the shadow extraction made), ``missed`` (the uncovered
  ones: ``[{beat, start, end, category, predicate_id, shape}]``, spans into the turn's beats,
  a predicate id and a shape hash, never text) and ``missed_count``.

No foreign key beyond ``operation_id`` (``core.stories.STORY_TABLES`` empties it with its
story). The rows hold no story text, so the subject index exempts them; the beats their spans
point into are indexed and erased.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Integer, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

SHADOW_STATUSES: tuple[str, ...] = ("sampled", "failed")

_STATUSES = ", ".join(f"'{s}'" for s in SHADOW_STATUSES)

extraction_shadow = Table(
    "extraction_shadow",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    *derived_columns(),
    Column("status", Text, nullable=False),
    Column("extraction_claims", Integer, nullable=False, server_default=text("0")),
    Column("missed", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("missed_count", Integer, nullable=False, server_default=text("0")),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    CheckConstraint(f"status IN ({_STATUSES})", name="status"),
    *derived_indexes("extraction_shadow"),
)
Index(
    "ix_extraction_shadow_story_created",
    extraction_shadow.c.story_id,
    extraction_shadow.c.created_at,
)
Index("ix_extraction_shadow_created", extraction_shadow.c.created_at)

EXTRACTION_SHADOW = register_derived(DerivedTable(extraction_shadow))
"""``extraction_shadow`` in ``core.membership.DERIVED_TABLES``: a sample follows the turn it
sampled."""

__all__ = ["EXTRACTION_SHADOW", "SHADOW_STATUSES", "extraction_shadow"]
