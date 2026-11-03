"""``dependency_edges``: why a record depends on a fact (FR-DIRECT-7, FR-REWRITE-11; #220,
migration 0108).

One row is one typed dependency: a **dependent** record (``dependent_table`` ∈ ``events``,
``utterances``, ``beliefs``, ``facts``, ``memories``, ``state_changes``, ``scheduled_events``,
``divergences``, ``amendment_parts``; ``dependent_id``) depends on a **target**
(``target_kind`` ``fact``, ``proposition``, ``entity``, ``amendment_part`` or ``state``;
``target_id``) by ``edge_type`` (``causes``, ``presupposes``, ``derives``, ``mentions``,
``asserts``). A fact or proposition target also carries its proposition key
(``target_subject_id``, ``target_predicate_id``) so a key search needs no join; a ``state``
target's ``target_subject_id`` is the scope key (``NULL`` for a story-scoped State). A
``derives`` edge carries the belief's ``acquisition_kind`` (#160's transition kind; CHECK: set
exactly on ``derives``). Edges hold ids and edge types only, never text.

A derived record of the operation that wrote it (#130's registry): truncation, swap, regenerate
and fork move it with its operation. Positioned at its dependent's position (the nullable
``position_*`` group: an amendment's edges are position-less). One edge per ``(edge_type,
dependent, target)`` (``uq_dependency_edges_edge``). Append-only
(``epistrel_dependency_edges_append_only``): an UPDATE may change only the membership pair; a
DELETE passes only under an erasure or the story's deletion. ``core.edges.record_edges`` is the
only writer (``check-dependency-edges``).
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text

from epistrel.core.enums import DependencyEdgeType, DependencyTarget
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    position_group,
    position_group_check,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

DEPENDENT_TABLES: frozenset[str] = frozenset(
    {
        "events",
        "utterances",
        "beliefs",
        "facts",
        "memories",
        "state_changes",
        "scheduled_events",
        "divergences",
        "amendment_parts",
    }
)
"""The records an edge may name as its dependent (#220)."""

MUTABLE_EDGE_COLUMNS: tuple[str, ...] = ("membership", "membership_tx")
"""The columns an UPDATE of ``dependency_edges`` may change: the membership pair."""

dependency_edges = Table(
    "dependency_edges",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("edge_type", pg_enum(DependencyEdgeType, "dependency_edge_type"), nullable=False),
    Column("dependent_table", Text, nullable=False),
    Column("dependent_id", ulid_type(), nullable=False),
    Column("target_kind", pg_enum(DependencyTarget, "dependency_target"), nullable=False),
    Column("target_id", ulid_type(), nullable=False),
    Column("target_subject_id", ulid_type(), nullable=True),
    Column("target_predicate_id", ulid_type(), nullable=True),
    Column("acquisition_kind", Text, nullable=True),
    *position_group("position", nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    *derived_columns(),
    CheckConstraint(
        "(edge_type = 'derives') = (acquisition_kind IS NOT NULL)", name="acquisition_kind"
    ),
    CheckConstraint(position_group_check("position"), name="position_group"),
    *derived_indexes("dependency_edges"),
)
Index(
    "uq_dependency_edges_edge",
    dependency_edges.c.edge_type,
    dependency_edges.c.dependent_table,
    dependency_edges.c.dependent_id,
    dependency_edges.c.target_kind,
    dependency_edges.c.target_id,
    unique=True,
)
Index(
    "ix_dependency_edges_story_target",
    dependency_edges.c.story_id,
    dependency_edges.c.target_kind,
    dependency_edges.c.target_id,
)
Index(
    "ix_dependency_edges_story_key",
    dependency_edges.c.story_id,
    dependency_edges.c.target_subject_id,
    dependency_edges.c.target_predicate_id,
)
Index(
    "ix_dependency_edges_story_dependent",
    dependency_edges.c.story_id,
    dependency_edges.c.dependent_table,
    dependency_edges.c.dependent_id,
)

DEPENDENCY_EDGES = register_derived(DerivedTable(dependency_edges))
"""``dependency_edges`` in ``core.membership.DERIVED_TABLES``: an edge follows the operation
that wrote it."""

__all__ = [
    "DEPENDENCY_EDGES",
    "DEPENDENT_TABLES",
    "MUTABLE_EDGE_COLUMNS",
    "dependency_edges",
]
