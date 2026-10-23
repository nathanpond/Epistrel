"""``facts``: objective canon, bitemporal and visibility-scoped (FR-BITEMP-1..5, FR-STORE-9;
docs/02 §4.4, §4.5; #133, migration 0049).

One row is one **version** of a fact: a proposition (``propositions``, #131) wrapped with

- **valid-time** (story-time, integer story-minutes): ``valid_from``/``valid_to`` are the outer
  (possible) bounds, ``NULL`` open-ended; ``valid_from_inner``/``valid_to_inner`` the inner
  (definite) bounds, equal to the outer ones when exact; ``valid_span`` is
  ``[valid_from, valid_to)`` as an ``int8range`` (written by the application, checked equal,
  GiST-indexed). A fact's own valid-time is authoritative: the proposition's declared bounds
  are the shape's, and a retcon with a wider span is a new version over the same proposition.
- **transaction-time** (record-time): ``tx_from`` when the version was recorded, ``tx_to``
  when it was first closed (``NULL`` while open), ``[tx_from, tx_to)``. Superseding closes a
  version (``tx_to``, ``superseded_by``) and never deletes it (FR-BITEMP-2). ``lineage_id`` is
  the first version's id, shared by every later version of the fact; ``supersedes`` lists the
  versions the row closed when it was written.
- **narrative position and membership**: the derived-record columns (``core.membership``,
  #130): the producing operation, the source events, ``narrative_turn``/``turn_variant`` (from
  the operation, never from ``seq``), plus ``beat`` and ``slot_k``; ``membership`` follows the
  operation (truncation flips it; valid-time and transaction-time stay as they are,
  FR-BITEMP-5).
- **provenance, authority, visibility**: ``provenance`` (``record_provenance``), ``authority``
  (``fact_authority``) and ``author_locked`` (set by #145), and ``visibility``
  ``{"scope": objective|public|engine|characters, "characters": [...]}``.

``subject_id``, ``predicate_id`` and ``holder`` (always ``canon``) are copied from the
proposition for the ``(story_id, subject_id, predicate_id, holder)`` index. ``closed_set_id``
wraps a closed set instead of a proposition (exactly one of the two). ``anchored_to`` is a
related fact, carried forward on supersede. ``commit_state``, ``committed_at_position``,
``thunk_id`` (#146) and ``resolution_level`` (#150; ``NULL`` means ``consequential``) are
created here for the stories that own them.

Append-only (``epistrel_facts_append_only``): an UPDATE may set ``tx_to`` and ``superseded_by``
once each (``NULL`` to a value) and change ``membership``/``membership_tx``; an erasure
(``epistrel.erasure = on``) may also rewrite ``visibility``. A DELETE passes only under an
erasure or the story's deletion (``epistrel.allow_story_delete``); TRUNCATE is refused.
``core.canon.facts`` is the only writer.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB

from epistrel.core.enums import FactAuthority, FactCommitState, HolderKind, RecordProvenance
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

VISIBILITY_SCOPES: tuple[str, ...] = ("objective", "public", "engine", "characters")
"""The visibility scopes a fact may carry (``visibility->>'scope'``, CHECK)."""

MUTABLE_FACT_COLUMNS: tuple[str, ...] = ("tx_to", "superseded_by", "membership", "membership_tx")
"""The columns an UPDATE of ``facts`` may change (``tx_to``/``superseded_by`` once each)."""

_SCOPES = ", ".join(f"'{s}'" for s in VISIBILITY_SCOPES)

facts = Table(
    "facts",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("lineage_id", ulid_type(), nullable=False),
    Column("proposition_id", ulid_type(), nullable=True),
    Column("closed_set_id", ulid_type(), nullable=True),
    Column("subject_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column(
        "holder",
        pg_enum(HolderKind, "holder_kind"),
        nullable=False,
        server_default=text("'canon'"),
    ),
    Column("valid_from", BigInteger, nullable=True),
    Column("valid_to", BigInteger, nullable=True),
    Column("valid_from_inner", BigInteger, nullable=True),
    Column("valid_to_inner", BigInteger, nullable=True),
    Column("valid_span", INT8RANGE, nullable=False),
    Column("tx_from", TIMESTAMP(timezone=True), nullable=False),
    Column("tx_to", TIMESTAMP(timezone=True), nullable=True),
    Column("provenance", pg_enum(RecordProvenance, "record_provenance"), nullable=False),
    Column("authority", pg_enum(FactAuthority, "fact_authority"), nullable=False),
    Column("author_locked", Boolean, nullable=False, server_default=text("false")),
    Column(
        "visibility",
        JSONB,
        nullable=False,
        server_default=text("""'{"scope": "objective"}'::jsonb"""),
    ),
    *derived_columns(),
    Column("beat", SmallInteger, nullable=True),
    Column("slot_k", SmallInteger, nullable=True),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column("superseded_by", ulid_type(), nullable=True),
    Column("supersedes", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("anchored_to", ulid_type(), nullable=True),
    Column(
        "commit_state",
        pg_enum(FactCommitState, "fact_commit_state"),
        nullable=False,
        server_default=text("'committed'"),
    ),
    Column("committed_at_position", JSONB(none_as_null=True), nullable=True),
    Column("thunk_id", ulid_type(), nullable=True),
    Column("resolution_level", Text, nullable=True),
    CheckConstraint("num_nonnulls(proposition_id, closed_set_id) = 1", name="one_wrapped"),
    CheckConstraint(
        "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to", name="valid_order"
    ),
    CheckConstraint(
        "(valid_from IS NULL) = (valid_from_inner IS NULL) "
        "AND (valid_to IS NULL) = (valid_to_inner IS NULL) "
        "AND (valid_from IS NULL OR valid_from <= valid_from_inner) "
        "AND (valid_to IS NULL OR valid_to_inner <= valid_to)",
        name="inner_bounds",
    ),
    CheckConstraint("valid_span = int8range(valid_from, valid_to, '[)')", name="valid_span"),
    CheckConstraint("tx_to IS NULL OR tx_from < tx_to", name="tx_order"),
    CheckConstraint(f"visibility->>'scope' IN ({_SCOPES})", name="visibility_scope"),
    CheckConstraint(
        "(visibility->>'scope' = 'characters') = "
        "(jsonb_typeof(visibility->'characters') = 'array' "
        "AND jsonb_array_length(visibility->'characters') > 0)",
        name="visibility_characters",
    ),
    *derived_indexes("facts"),
)
Index(
    "ix_facts_story_subject_predicate",
    facts.c.story_id,
    facts.c.subject_id,
    facts.c.predicate_id,
    facts.c.holder,
)
Index("ix_facts_valid_span", facts.c.valid_span, postgresql_using="gist")
Index("ix_facts_story_lineage", facts.c.story_id, facts.c.lineage_id)
Index("ix_facts_story_open", facts.c.story_id, postgresql_where=text("tx_to IS NULL"))
Index("ix_facts_supersedes", facts.c.supersedes, postgresql_using="gin")

FACTS = register_derived(
    DerivedTable(facts, valid_time=("valid_from", "valid_to"), tx_time=("tx_from", "tx_to"))
)
"""``facts`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap, regenerate
and fork move its rows with their operations."""

__all__ = ["FACTS", "MUTABLE_FACT_COLUMNS", "VISIBILITY_SCOPES", "facts"]
